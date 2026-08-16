# -*- coding: utf-8 -*-
"""The take-off engine: a project of rooms in, a defensible bill of quantities out.

:mod:`core.schema` pins the geometry and :mod:`core.rules_loader` runs one rule
over one space. This module is the part in between — the part that decides *which*
rules run, *in what order*, *with which conventions*, and *how the resulting number
is justified in Arabic*. It is the core IP of the product, so three things are true
of every line it emits:

* **Nothing here is estimated.** Every quantity is restricted arithmetic over the
  dataclasses in :mod:`core.schema`, driven by the data in ``core/rules/finishes.yaml``.
  No LLM, no randomness, no clock, no network. The same JSON always produces the same
  bill, byte for byte — which is what makes the bill arguable in front of a contractor.
* **Rounding happens once, at the very end.** ``net_qty`` is the raw formula result and
  ``gross_qty`` is ``net_qty × (1 + waste_pct)``; both are rounded to 2 dp only when the
  line is written. Every value handed from one line to another (a grout item copying its
  tile item, a plaster item subtracting a cladding item) travels at FULL precision.
  Rounding an intermediate is how a 200-line villa quietly drifts by a square metre.
* **Every number carries its arithmetic, in Arabic.** ``BOQLine.breakdown`` shows the
  substituted values of each input, how that input was itself derived, the deduction
  convention that was applied, any override the user forced, the formula, the same
  formula with the numbers in it, and then net → waste → gross. A wrong quantity is real
  money lost and real legal liability; a number nobody can trace is a number nobody can
  defend in an interim certificate.

Two failure policies, inherited from the rest of the core and never bent:

* **Warnings never crash.** An unknown rule key, a space with no polygon, an undeclared
  spec parameter — each skips that one finish and leaves a zero-quantity WARNING line in
  the bill saying so in Arabic. A take-off that dies on room 14 of 30 is useless; a
  take-off with a visible hole in it is honest.
* **Nothing silently returns 0.** When a quantity cannot be measured, the reason is
  written into the line. A silent 0 becomes a silent under-quote, and an under-quote is
  the single worst failure mode of this product.

Arabic is the deliverable language (names, breakdown, warnings); identifiers and
comments stay English. Dependencies: Python stdlib + PyYAML (via rules_loader).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .schema import (
    OPENING_KINDS_AR,
    SPACE_TYPES_AR,
    SURFACES_AR,
    UNITS_AR,
    BOQLine,
    FinishAssignment,
    GeometryError,
    Project,
    Space,
)
from .rules_loader import (
    FormulaError,
    RuleError,
    build_env,
    formula_names,
    load_rules,
    safe_eval,
)

# Private helpers imported ON PURPOSE. The measurement CONVENTIONS — what the deduction
# threshold currently is, which openings it applies to, how a banded height is resolved,
# when a linear item flips to m² — must have exactly ONE implementation in this product.
# Re-deriving them here would create a second place to change a convention, and the two
# copies would disagree on the day somebody edits only one of them. That disagreement is
# a wrong quantity, i.e. money.
from .rules_loader import (  # noqa: F401  (deliberate cross-module reuse)
    WARN_PREFIX,
    _THRESHOLD_ALIASES,
    _Ctx,
    _band,
    _banded_opening_run,
    _cnt,
    _flag,
    _floor_reaching_windows,
    _fmt,
    _fmt_auto,
    _is_deductible,
    _num,
    _overlap,
    _pct,
    _resolve_unit,
    _sill,
    _sp,
    _threshold,
    _waste_pct,
)

__all__ = [
    "takeoff", "takeoff_space", "summarize",
    "resolve_rule_key", "is_warning", "warnings_of",
    "WARNING_CODE", "WARN_PREFIX", "QTY_DP",
    "OVERRIDABLE", "CROSS_LINKS", "SUM_SOURCES", "DEFAULT_PARENTS",
    "DEFAULT_CEILING_PARENTS", "WALL_COVERING_SOURCES", "FLOOR_REACHING_WALL_COVERINGS",
    "FACADE_CLADDING_SOURCES", "FLOOR_TILE_RULES",
    "SPACE_TYPE_GUARDS", "CONFLICTS", "DERIVATIONS",
]

# --------------------------------------------------------------------------- #
# Constants                                                                     #
# --------------------------------------------------------------------------- #
QTY_DP = 2                      # a BOQ prints two decimals — and rounds ONCE, at the end
MAX_MERGE_HOPS = 5              # cycle guard on merged_into redirects
WARNING_CODE = "WRN-BOQ"        # the code a skipped finish carries into the bill
# WARN_PREFIX marks a breakdown entry as a warning, for summarize(). It is DEFINED in
# rules_loader and imported above: the rules layer emits warnings too (a guessed
# quantity, a zero one), and one marker string is what lets them reach warnings_of().

_M2 = UNITS_AR["m2"]
_ML = UNITS_AR["m"]


# --------------------------------------------------------------------------- #
# Small pure helpers                                                            #
# --------------------------------------------------------------------------- #
def _r2(x: float) -> float:
    """The ONLY rounding in the engine. Applied once, to a finished quantity."""
    return round(float(x), QTY_DP) + 0.0      # + 0.0 kills a signed zero


def _u(unit: str) -> str:
    return UNITS_AR.get(unit, unit)


def _surface_ar(surface: str) -> str:
    return SURFACES_AR.get(surface, surface)


def _truthy(v: Any, default: bool = False) -> bool:
    """Spec flags arrive from YAML/JSON as bool, 0/1 or 'true'/'false'."""
    if v is None:
        return bool(default)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def _terms(parts, empty: str = "", limit: int = 8) -> str:
    """Join the per-item arithmetic, truncating so one hall does not print a page."""
    parts = [p for p in parts if p]
    if not parts:
        return empty
    if len(parts) <= limit:
        return " + ".join(parts)
    return " + ".join(parts[:limit]) + f" + … (و{len(parts) - limit} بنداً آخر)"


def _is_warn(entry: str) -> bool:
    """Warnings are indented when they sit inside a breakdown section, so match anywhere."""
    return WARN_PREFIX in entry


def _clean_warn(entry: str) -> str:
    return entry.lstrip(" –-")


def _short_label(label: str) -> str:
    """Drop a trailing parenthetical, e.g. 'مساحة الجدران الإجمالية (المحيط × الارتفاع)'.

    Only used where a derivation sentence follows, which states the same thing with
    real numbers in it — repeating it symbolically would be noise.
    """
    if label.endswith(")") and " (" in label:
        return label[:label.rindex(" (")]
    return label


def _op_label(o) -> str:
    return f"{OPENING_KINDS_AR.get(o.kind, o.kind)} «{o.id}»"


def _n(o) -> int:
    return int(o.count or 0)


# --------------------------------------------------------------------------- #
# Registry: derived field -> the Arabic arithmetic that produced it.            #
#                                                                               #
# GEOMETRY_FIELDS in rules_loader gives a field its VALUE and its label; this    #
# gives the same field its ARITHMETIC, with the real substituted numbers, so the #
# surveyor can re-do it on paper. A field with no entry here simply prints its   #
# value — that is only acceptable for inputs that ARE the raw measurement.       #
# Every function is pure and must never mutate the context.                     #
# --------------------------------------------------------------------------- #
def _d_floor_area(c: _Ctx) -> str:
    s = c.space
    if s.polygon and len(s.polygon) >= 3:
        return f"مساحة مضلع الأرضية بطريقة «رباط الحذاء» من {len(s.polygon)} نقطة مساحية"
    return "المساحة المُدخلة يدوياً في بيانات الفراغ (لا يوجد مضلع مرسوم)"


def _d_perimeter_m(c: _Ctx) -> str:
    s = c.space
    if s.polygon and len(s.polygon) >= 3:
        return f"مجموع أضلاع مضلع الأرضية ({len(s.polygon)} نقطة مساحية)"
    return "مجموع أطوال الجدران: " + _terms(
        (f"{_fmt(_num(w.length_m))}" for w in s.walls), "لا توجد جدران")


def _d_gross_wall_area(c: _Ctx) -> str:
    return (f"المحيط {_fmt(c.space.perimeter_m)} م × "
            f"الارتفاع {_fmt(_num(c.space.height_m))} م")


def _d_net_wall_area(c: _Ctx) -> str:
    return (f"مساحة الجدران الإجمالية {_fmt(c.space.gross_wall_area)} {_M2} − "
            f"مساحة الفتحات {_fmt(c.space.openings_area)} {_M2}")


def _d_openings_area(c: _Ctx) -> str:
    return _terms(
        (f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_fmt(_num(o.height_m))}×{_n(o)}"
         for o in c.space.openings if o.is_hole), "لا توجد فتحات في جدران هذا الفراغ")


def _d_openings_perimeter(c: _Ctx) -> str:
    return _terms((f"{_op_label(o)} كشفة {_fmt(o.reveal_run_m)} م×{_n(o)}"
                   for o in c.space.openings), "لا توجد فتحات")


def _d_reveal_run(c: _Ctx) -> str:
    return _terms((f"{_op_label(o)} {_fmt(o.reveal_run_m)} م×{_n(o)}"
                   for o in c.space.openings if o.kind != "niche"), "لا توجد فتحات")


def _d_wall_area_sum(c: _Ctx) -> str:
    return _terms((f"«{w.id}» {_fmt(_num(w.length_m))}×{_fmt(_num(w.height_m))}"
                   for w in c.space.walls), "لا توجد جدران مُدخلة")


def _d_external_wall_area(c: _Ctx) -> str:
    return _terms((f"«{w.id}» {_fmt(_num(w.length_m))}×{_fmt(_num(w.height_m))}"
                   for w in c.space.walls if w.is_external), "لا يوجد جدار خارجي")


def _d_external_wall_length(c: _Ctx) -> str:
    return _terms((f"«{w.id}» {_fmt(_num(w.length_m))}"
                   for w in c.space.walls if w.is_external), "لا يوجد جدار خارجي")


def _d_deductible_openings_area(c: _Ctx) -> str:
    lim = _threshold(c)
    kept = [o for o in c.space.openings if _is_deductible(c, o)]
    if not kept:
        return f"لا توجد فتحة تبلغ حدّ الخصم {_fmt(lim)} {_M2} — لم يُخصم شيء"
    return (_terms(f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_fmt(_num(o.height_m))}"
                   f"={_fmt(o.area_m2)}×{_n(o)}" for o in kept)
            + f" (كلٌّ منها ≥ حدّ الخصم {_fmt(lim)} {_M2})")


def _d_deductible_reveal_run(c: _Ctx) -> str:
    kept = [o for o in c.space.openings if _is_deductible(c, o)]
    if not kept:
        return "لا تُقاس كشفات لأن أياً من الفتحات لم يُخصم (الكشفة تتبع الخصم)"
    return _terms(f"{_op_label(o)} {_fmt(o.reveal_run_m)} م×{_n(o)}" for o in kept)


def _d_band_height(c: _Ctx) -> str:
    h = _num(c.space.height_m)
    full = c.rule.get("full_height_param")
    if full and _flag(c, full, False):
        return f"كامل ارتفاع الفراغ {_fmt(h)} م ({full} = نعم)"
    p = c.rule.get("band_height_param")
    if not p:
        return f"كامل ارتفاع الفراغ {_fmt(h)} م"
    v = _sp(c, p, 0.0)
    if v > 0:
        return f"الأصغر بين ارتفاع الفراغ {_fmt(h)} م والنطاق المطلوب {p} = {_fmt(v)} م"
    return f"كامل ارتفاع الفراغ {_fmt(h)} م ({p} غير مُحدد في المواصفة)"


def _d_banded_gross_area(c: _Ctx) -> str:
    return (f"المحيط {_fmt(c.space.perimeter_m)} م × "
            f"ارتفاع النطاق المعالَج {_fmt(_band(c))} م")


def _d_banded_openings_deduct(c: _Ctx) -> str:
    top = _band(c)
    parts = []
    for o in c.space.openings:
        if not _is_deductible(c, o):
            continue
        lo = _sill(c, o)
        inside = _overlap(lo, lo + _num(o.height_m), top)
        parts.append(f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_fmt(inside)}×{_n(o)}")
    if not parts:
        return f"لا جزء من أي فتحة مخصومة يقع داخل النطاق حتى {_fmt(top)} م"
    return _terms(parts) + f" (الجزء الواقع داخل النطاق حتى {_fmt(top)} م فقط)"


def _d_banded_run(c: _Ctx, with_sill: bool) -> str:
    """The per-opening arithmetic of a reveal/arris clipped to the banded height.

    This is the number the band gets wrong most easily — a window that sills inside the
    tiling and heads above it — so it may never print as a bare figure.
    """
    top = _band(c)
    parts = []
    for o in c.space.openings:
        if not _is_deductible(c, o):
            continue
        lo, h, w = _sill(c, o), _num(o.height_m), _num(o.width_m)
        bits = [f"جانبان 2×{_fmt(_overlap(lo, lo + h, top))}"]
        if with_sill and o.kind == "window" and lo < top:
            bits.append(f"جلسة {_fmt(w)}")
        if lo + h <= top:
            bits.append(f"عتب {_fmt(w)}")
        else:
            bits.append(f"بلا عتب — رأس الفتحة عند {_fmt(lo + h)} م فوق حدّ النطاق")
        parts.append(f"{_op_label(o)} (جلسة {_fmt(lo)} م) [{' + '.join(bits)}]×{_n(o)}")
    if not parts:
        return f"لا فتحة مخصومة يقع أي جزء من كشفتها داخل النطاق حتى {_fmt(top)} م"
    return _terms(parts) + f" — المقيس هو ما يقع داخل النطاق حتى {_fmt(top)} م فقط"


def _d_banded_reveal_run(c: _Ctx) -> str:
    return _d_banded_run(c, True)


def _d_banded_arris_run(c: _Ctx) -> str:
    return _d_banded_run(c, False)


def _d_reveal_run_with_sill(c: _Ctx, only_deducted: bool) -> str:
    """Linear reveal item: jambs + head, plus the sill for a window, per opening."""
    sill_on = _flag(c, "include_sill", True)
    parts = []
    for o in c.space.openings:
        if only_deducted and not (o.kind == "niche" or _is_deductible(c, o)):
            continue
        w, h = _num(o.width_m), _num(o.height_m)
        if o.kind == "niche":
            bits = f"محيط داخلي 2×({_fmt(w)}+{_fmt(h)})"
        elif o.kind == "window":
            bits = (f"جانبان 2×{_fmt(h)} + عتب {_fmt(w)}"
                    + (f" + جلسة {_fmt(w)}" if sill_on else " (بلا جلسة)"))
        else:
            bits = f"جانبان 2×{_fmt(h)} + عتب {_fmt(w)} (بلا جلسة)"
        parts.append(f"{_op_label(o)} [{bits}]×{_n(o)}")
    if not parts:
        return ("لا تُقاس كشفات: لم تُخصم أي فتحة من بند الجدار — الكشفة تتبع الخصم"
                if only_deducted else "لا توجد فتحات في هذا الفراغ")
    return _terms(parts)


def _d_reveal_run_with_sill_all(c: _Ctx) -> str:
    return _d_reveal_run_with_sill(c, False)


def _d_deductible_reveal_run_with_sill(c: _Ctx) -> str:
    return (_d_reveal_run_with_sill(c, True)
            + " — الكوّة تُقاس دائماً (إضافة لا خصم)، والفتحة تُقاس كشفتها فقط إن خُصمت")


def _d_door_run(c: _Ctx) -> str:
    return _terms((f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_n(o)}"
                   for o in c.space.openings if o.kind in ("door", "arch")),
                  "لا توجد أبواب أو قناطر في هذا الفراغ")


def _d_door_only_run(c: _Ctx) -> str:
    return _terms((f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_n(o)}"
                   for o in c.space.openings if o.kind == "door"), "لا توجد أبواب")


def _d_window_run(c: _Ctx) -> str:
    return _terms((f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_n(o)}"
                   for o in c.space.openings if o.kind == "window"), "لا توجد شبابيك")


def _d_skirting_door_deduct(c: _Ctx) -> str:
    if not _flag(c, "skirting_deduct_doors", True):
        return "لا خصم (skirting_deduct_doors = لا) — عرف تساهلي، تأكّد من المواصفة"
    return _d_door_run(c) + " — بعرضها الكامل بلا حدّ أدنى (بند طولي)"


def _d_full_height_window_run(c: _Ctx) -> str:
    want = _floor_reaching_windows(c)
    if want is not None:
        hit = [o for o in c.space.openings if o.kind == "window" and str(o.id) in want]
        if not hit:
            return (f"لا شباك من المسمّاة في window_to_floor_ids ({'، '.join(sorted(want))}) "
                    f"موجود في هذا الفراغ — لا خصم")
        return (_terms(f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_n(o)}" for o in hit)
                + " — الشبابيك المسمّاة في window_to_floor_ids وحدها، لا كل شبابيك الفراغ")
    if not _flag(c, "window_to_floor", False):
        return "لا شباك نازل حتى الأرضية (window_to_floor = لا) — لا خصم"
    return (_d_window_run(c) + " — window_to_floor مفتاح على مستوى الفراغ فشمل الخصم كل "
            "شبابيكه؛ لتحديد النازل منها فعلاً استعمل window_to_floor_ids")


def _d_niche_area(c: _Ctx) -> str:
    d = _sp(c, "niche_depth_m", 0.12)
    return _terms(
        (f"{_op_label(o)} خلفية {_fmt(_num(o.width_m))}×{_fmt(_num(o.height_m))} + "
         f"جوانب 2×({_fmt(_num(o.width_m))}+{_fmt(_num(o.height_m))})×عمق {_fmt(d)}، ×{_n(o)}"
         for o in c.space.openings if o.kind == "niche"),
        "لا توجد كوّات في هذا الفراغ — الكوّة تُضاف ولا تُخصم")


def _d_door_reveal_strip(c: _Ctx) -> str:
    if not _flag(c, "tile_door_reveal", True):
        return "غير مضاف (tile_door_reveal = لا) — العتبة تُقاس ببند مستقل"
    run = sum(_num(o.width_m) * _n(o) for o in c.space.openings if o.kind in ("door", "arch"))
    return (f"مجموع عروض الأبواب والقناطر {_fmt(run)} م × "
            f"عمق المرتد {_fmt(_sp(c, 'door_reveal_depth_m', 0.20))} م")


def _d_facade_gross_area(c: _Ctx) -> str:
    t = _sp(c, "slab_thickness_m", 0.30)
    body = _terms((f"«{w.id}» {_fmt(_num(w.length_m))}×({_fmt(_num(w.height_m))}+{_fmt(t)})"
                   for w in c.space.walls if w.is_external), "")
    if not body:
        return "لا يوجد جدار مُعلَّم كخارجي في هذا الفراغ — الواجهة صفر"
    return body + " — من ظهر بلاطة لظهر بلاطة، لا الارتفاع الصافي الداخلي"


def _d_parapet_area(c: _Ctx) -> str:
    return (f"ارتفاع الدروة {_fmt(_sp(c, 'parapet_height_m', 0.0))} م × "
            f"طول الجدران الخارجية {_fmt(c.space.external_wall_length)} م × "
            f"{_cnt(_sp(c, 'parapet_faces', 2.0))} وجه")


def _d_external_openings_deduct(c: _Ctx) -> str:
    lim = _threshold(c)
    kept = [(w, o) for w, o in c.space.iter_openings()
            if w.is_external and _is_deductible(c, o)]
    if not kept:
        return f"لا توجد فتحة خارجية تبلغ حدّ الخصم {_fmt(lim)} {_M2}"
    return _terms(f"{_op_label(o)} {_fmt(_num(o.width_m))}×{_fmt(_num(o.height_m))}×{_n(o)}"
                  for _w, o in kept) + f" (≥ حدّ الخصم {_fmt(lim)} {_M2})"


def _d_skirting_band_area(c: _Ctx) -> str:
    if not _flag(c, "deduct_skirting_band", False):
        return "غير مطروح (deduct_skirting_band = لا)"
    return (f"المحيط {_fmt(c.space.perimeter_m)} م × "
            f"ارتفاع شريط الوزرة {_fmt(_sp(c, 'skirting_band_m', 0.10))} م")


def _d_beam_side_area(c: _Ctx) -> str:
    if not _flag(c, "include_beam_sides", False):
        return "غير محتسب (include_beam_sides = لا)"
    return (f"طول الجسر {_fmt(_sp(c, 'beam_run_m', 0.0))} م × "
            f"سقوطه {_fmt(_sp(c, 'beam_drop_m', 0.0))} م × وجهين")


def _d_false_ceiling_band_area(c: _Ctx) -> str:
    if _sp(c, "false_ceiling_area_m2", 0.0) <= 0:
        return "لا يوجد سقف مستعار (المساحة صفر) — لا شريط محيطي"
    return (f"المحيط {_fmt(c.space.perimeter_m)} م × "
            f"عرض الشريط المحيطي {_fmt(_sp(c, 'perimeter_band_m', 0.30))} م")


def _d_backsplash_band_height(c: _Ctx) -> str:
    return (f"أعلى من (ارتفاع الخزائن العلوية {_fmt(_sp(c, 'upper_cabinet_height_m', 1.50))} م − "
            f"ارتفاع سطح العمل {_fmt(_sp(c, 'counter_height_m', 0.90))} م) و"
            f"الحدّ الأدنى {_fmt(_sp(c, 'min_band_m', 0.60))} م")


# stable field name -> pure Arabic derivation sentence (real numbers substituted)
DERIVATIONS: dict[str, Callable[[_Ctx], str]] = {
    "floor_area": _d_floor_area,
    "perimeter_m": _d_perimeter_m,
    "gross_wall_area": _d_gross_wall_area,
    "net_wall_area": _d_net_wall_area,
    "openings_area": _d_openings_area,
    "openings_perimeter": _d_openings_perimeter,
    "reveal_run": _d_reveal_run,
    "wall_area_sum": _d_wall_area_sum,
    "external_wall_area": _d_external_wall_area,
    "external_wall_length": _d_external_wall_length,
    "deductible_openings_area": _d_deductible_openings_area,
    "deductible_reveal_run": _d_deductible_reveal_run,
    "reveal_run_with_sill": _d_reveal_run_with_sill_all,
    "deductible_reveal_run_with_sill": _d_deductible_reveal_run_with_sill,
    "band_height": _d_band_height,
    "banded_gross_area": _d_banded_gross_area,
    "banded_openings_deduct": _d_banded_openings_deduct,
    "banded_reveal_run": _d_banded_reveal_run,
    "banded_arris_run": _d_banded_arris_run,
    "door_run": _d_door_run,
    "door_only_run": _d_door_only_run,
    "window_run": _d_window_run,
    "skirting_door_deduct": _d_skirting_door_deduct,
    "full_height_window_run": _d_full_height_window_run,
    "niche_area": _d_niche_area,
    "door_reveal_strip": _d_door_reveal_strip,
    "facade_gross_area": _d_facade_gross_area,
    "parapet_area": _d_parapet_area,
    "external_openings_deduct": _d_external_openings_deduct,
    "skirting_band_area": _d_skirting_band_area,
    "beam_side_area": _d_beam_side_area,
    "false_ceiling_band_area": _d_false_ceiling_band_area,
    "backsplash_band_height": _d_backsplash_band_height,
}


# --------------------------------------------------------------------------- #
# Registry: what a user may override, and what counts as a sane value.          #
#                                                                               #
# Both of these are CONVENTIONS, not physics — the office must be able to move   #
# them per project. Both move real money (the threshold moves net_qty, i.e. the  #
# paid quantity; the waste moves gross_qty, i.e. the purchase order), so an      #
# override is never silent: it is written into the breakdown of every line it    #
# touched, with the old value beside the new one.                               #
# name -> (Arabic label, validator, Arabic formatter, Arabic range description)  #
# --------------------------------------------------------------------------- #
OVERRIDABLE: dict[str, tuple[str, Callable[[float], bool], Callable[[float], str], str]] = {
    "waste_pct": ("نسبة الهدر", lambda v: 0.0 <= v <= 1.0,
                  lambda v: _pct(v * 100.0), "بين 0 و1 (0.10 تعني 10%)"),
    "deduct_threshold_m2": ("حدّ الخصم", lambda v: 0.0 <= v <= 50.0,
                            lambda v: f"{_fmt(v)} {_M2}", f"بين 0 و50 {_M2}"),
}


# --------------------------------------------------------------------------- #
# Registries: which line feeds which line (the anti-double-count wiring).       #
#                                                                               #
# The golden rule of finishes.yaml is "any area that appears in two items is a   #
# double claim". Dependent items (grinding, bedding, grout, skim, putty, paint)  #
# therefore COPY their parent's net instead of re-measuring it, and plaster      #
# items SUBTRACT the area a cladding item already claimed. The parent lists      #
# below come straight from each rule's own deduction_rule_ar.                    #
# --------------------------------------------------------------------------- #
FLOOR_TILE_RULES = (
    "ceramic_tile_floor", "porcelain_tile_floor", "porcelain_large_format_floor",
    "marble_floor_slab", "granite_floor", "terrazzo_tile_floor", "vinyl_spc_floor",
    "external_antislip_tile_floor",
)

# follower rule key -> candidate parents, most-preferred first
DEFAULT_PARENTS: dict[str, tuple[str, ...]] = {
    "floor_grinding_polishing": ("terrazzo_tile_floor", "marble_floor_slab", "granite_floor"),
    "mortar_bedding_screed": FLOOR_TILE_RULES,
    "epoxy_grout_floor_joints": FLOOR_TILE_RULES,
    "plaster_backing_behind_tile": ("wall_tile_ceramic_wet", "kitchen_backsplash_tile"),
    "gypsum_skim_coat": ("plaster_internal_wall_cement", "gypsum_board_wall_lining"),
    "putty_filler_internal": ("gypsum_skim_coat", "plaster_internal_wall_cement",
                              "gypsum_board_wall_lining"),
    "paint_emulsion_ceiling": ("plaster_internal_ceiling", "gypsum_board_ceiling_flat"),
    "paint_external_facade": ("plaster_external_render",),
}

# the ceiling leg of gypsum_skim_coat (apply_to_ceiling)
DEFAULT_CEILING_PARENTS = ("plaster_internal_ceiling", "gypsum_board_ceiling_flat")

# items whose net area must be SUBTRACTED from internal plaster / paint
WALL_COVERING_SOURCES = (
    "wall_tile_ceramic_wet", "kitchen_backsplash_tile",
    "stone_cladding_internal_wall", "wood_cladding_wall",
)

# The subset of the above that starts AT THE FLOOR and therefore replaces the skirting.
# A kitchen backsplash is a band above the counter: it hides no skirting, so a kitchen
# with a backsplash still needs its full skirting run measured.
FLOOR_REACHING_WALL_COVERINGS = (
    "wall_tile_ceramic_wet", "stone_cladding_internal_wall", "wood_cladding_wall",
)

# items whose net area must be SUBTRACTED from external render
FACADE_CLADDING_SOURCES = ("stone_cladding_external_facade", "aluminium_composite_cladding")

# spec param -> (Arabic label, kind, guard flag that switches it off entirely)
# kind: "parent" = copy ONE parent's net | "sum" = subtract the SUM of matching items
CROSS_LINKS: dict[str, tuple[str, str, Optional[str]]] = {
    "parent_net_qty": ("الكمية الصافية لبند الأصل", "parent", None),
    "parent_ceiling_net_qty": ("الكمية الصافية لبند السقف الأصل", "ceiling", "apply_to_ceiling"),
    "tiled_wall_area_m2": ("مساحة الجدران المكسوّة المطروحة", "sum", "exclude_tiled_area"),
    "clad_area_m2": ("مساحة الواجهة المكسوّة المطروحة", "sum", None),
    "floor_clad_area_m2": ("مساحة التكسية الجدارية النازلة حتى الأرضية", "sum", None),
}

# which "sum" link reads which source list — data, not an if/else chain
SUM_SOURCES: dict[str, tuple[str, ...]] = {
    "clad_area_m2": FACADE_CLADDING_SOURCES,
    "floor_clad_area_m2": FLOOR_REACHING_WALL_COVERINGS,
    "tiled_wall_area_m2": WALL_COVERING_SOURCES,
}


# --------------------------------------------------------------------------- #
# Registry: professional guards. These never change a quantity — they only make  #
# the surveyor look. The engine measures what it is told and says what worries   #
# it; deciding is the architect's job, not the software's.                       #
# rule key -> (space types where the assignment is wrong, Arabic reason)         #
# --------------------------------------------------------------------------- #
SPACE_TYPE_GUARDS: dict[str, tuple[tuple[str, ...], str]] = {
    "laminate_parquet_floor": (
        ("bath", "kitchen"),
        "الباركيه اللامينيت (HDF) لا يُركّب في الحمامات والمطابخ في العرف المحلي — ينتفخ "
        "بالرطوبة. راجع الإسناد أو انتقل إلى فينيل SPC.",
    ),
    "engineered_wood_parquet_floor": (
        ("bath",),
        "الخشب الطبيعي/المهندس في حمّام — حساس للرطوبة ويحتاج نصاً خاصاً في المواصفة.",
    ),
    "mdf_pvc_skirting": (
        ("bath",),
        "وزرة MDF في حمّام — تنتفخ بالرطوبة؛ الأنسب PVC أو وزرة سيراميك/رخام.",
    ),
    "wood_skirting": (
        ("bath",),
        "وزرة خشبية في حمّام — تنتفخ بالرطوبة؛ راجع المواصفة.",
    ),
}


# --------------------------------------------------------------------------- #
# Registry: pairs of items that, together, may bill the same work twice.        #
# (keys_a, keys_b, predicate over their merged specs, Arabic warning)           #
# --------------------------------------------------------------------------- #
CONFLICTS: tuple[tuple[tuple[str, ...], tuple[str, ...],
                       Callable[[dict, dict], bool], str], ...] = (
    (
        ("plaster_internal_wall_cement",), ("plaster_reveals_jambs",),
        lambda a, b: str(a.get("reveal_mode", "area")).strip().lower() == "area",
        "بند قصارة الكشفات (PLS-REV-JMB) مقيس في نفس الفراغ بينما القصارة الداخلية تضيف "
        "الكشفات داخل سعرها (reveal_mode = area) — الكشفات محسوبة مرتين. اضبط "
        "reveal_mode = linear في القصارة الداخلية، أو احذف بند الكشفات المستقل.",
    ),
    (
        FLOOR_TILE_RULES, ("marble_door_threshold",),
        lambda a, b: _truthy(a.get("tile_door_reveal", True), True),
        "شريط البلاط داخل فتحة الباب محسوب ضمن بند الأرضية (tile_door_reveal = نعم) بينما "
        "تُقاس عتبة رخام للباب نفسه — نفس المساحة في بندين. اضبط tile_door_reveal = لا في "
        "بند الأرضية، أو احذف بند العتبة.",
    ),
    (
        ("mortar_bedding_screed",),
        ("laminate_parquet_floor", "engineered_wood_parquet_floor"),
        lambda a, b: True,
        "فرشة المونة (SCR-MOR-BED) لا تُقاس تحت باركيه عائم — الباركيه يحتاج صبة تسوية "
        "ذاتية لا فرشة رملة. راجع البندين قبل إصدار العرض.",
    ),
    (
        ("plaster_internal_wall_cement", "paint_emulsion_internal_wall"),
        WALL_COVERING_SOURCES,
        lambda a, b: (_num(a.get("tiled_wall_area_m2", 0.0)) <= 0.0
                      and _truthy(a.get("exclude_tiled_area", True), True)),
        "يوجد تكسية جدارية مقيسة في نفس الفراغ ولم تُطرح مساحتها من القصارة/الدهان "
        "(tiled_wall_area_m2 = 0) — الجدار المكسوّ مقيس مرتين. ثبّت المساحة المطروحة.",
    ),
)


# --------------------------------------------------------------------------- #
# Internal working shapes                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class _Plan:
    """One finish assignment resolved to a rule, waiting to be measured."""

    index: int                          # position in space.finishes — the output order
    fa: FinishAssignment
    rule_key: str
    rule: dict
    notes: list[str] = field(default_factory=list)


@dataclass
class _Measured:
    """A measured line plus the FULL-PRECISION net that downstream lines consume."""

    plan: _Plan
    line: BOQLine
    net_raw: float
    spec: dict


@dataclass(frozen=True)
class _Run:
    """Everything a measurement needs that is constant for the whole take-off."""

    rules: dict
    index: dict          # code / alias / key (lower-cased) -> rule key
    overrides: dict
    # space_id -> the opening ids THIS space pays for. ``None`` == single-space
    # measurement, where every space owns everything it can see.
    owned: Optional[dict] = None


# --------------------------------------------------------------------------- #
# Rule resolution                                                               #
# --------------------------------------------------------------------------- #
def _code_index(rules: dict) -> dict[str, str]:
    """Accept a rule key, a BOQ code, or a historical alias — first match wins."""
    idx: dict[str, str] = {}
    for key, rule in rules.items():
        tokens = [key, (rule or {}).get("code")]
        tokens.extend((rule or {}).get("aliases") or [])
        for token in tokens:
            t = str(token or "").strip().lower()
            if t and t not in idx:
                idx[t] = key
    return idx


def _lookup(name: Any, rules: dict, index: dict) -> Optional[str]:
    k = str(name or "").strip()
    if not k:
        return None
    if k in rules:
        return k
    return index.get(k.lower())


def _follow_merges(key: Optional[str], rules: dict) -> tuple[Optional[str], list[str]]:
    """A merged rule is a stub kept so a historical code still resolves.

    Following it is what stops the same skirting being billed twice under two codes.
    """
    notes: list[str] = []
    seen: set[str] = set()
    for _ in range(MAX_MERGE_HOPS):
        if key is None or key not in rules:
            return key, notes
        target = (rules[key] or {}).get("merged_into")
        if not target or target not in rules or target in seen:
            return key, notes
        notes.append(
            f"البند «{key}» ({rules[key].get('code')}) مدمج في «{target}» "
            f"({rules[target].get('code')}) منعاً للازدواج في جدول الكميات — "
            f"حُوِّل الحصر تلقائياً إلى البند المدمَج فيه."
        )
        seen.add(key)
        key = target
    return key, notes


def resolve_rule_key(name: Any, rules: Optional[dict] = None) -> Optional[str]:
    """Rule key / BOQ code / alias -> the canonical rule key, or None if unknown."""
    rules = rules if rules is not None else load_rules()
    key, _ = _follow_merges(_lookup(name, rules, _code_index(rules)), rules)
    return key


# --------------------------------------------------------------------------- #
# Overrides                                                                     #
# --------------------------------------------------------------------------- #
def _override_for(overrides: Optional[dict], space_id: str, rule_key: str) -> dict:
    """Merge the four scopes, least specific first, so the narrowest one wins.

    Accepted keys: ``"*"``, ``"<space_id>:*"``, ``"<rule_key>"``, ``"<space_id>:<rule_key>"``.
    """
    out: dict = {}
    for scope in ("*", f"{space_id}:*", rule_key, f"{space_id}:{rule_key}"):
        d = (overrides or {}).get(scope)
        if isinstance(d, dict):
            out.update(d)
    return out


def _apply_overrides(rule: dict, spec: dict, ovr: dict, notes: list[str]) -> dict:
    """Return the rule as overridden, recording every accepted and refused change."""
    if not ovr:
        return rule
    for name, (label, ok, show, rng) in OVERRIDABLE.items():
        if name not in ovr:
            continue
        old = _num(rule.get(name), 0.0)
        raw = ovr[name]
        try:
            new = float(raw)
        except (TypeError, ValueError):
            new = float("nan")
        if not (new == new) or not ok(new):          # NaN-safe validity test
            notes.append(
                WARN_PREFIX + f"تعديل يدوي مرفوض: {label} = {raw!r} خارج المدى المسموح "
                f"({rng}) — أُبقيت القيمة المعتمدة في ملف القواعد {show(old)}."
            )
            continue
        if name == "deduct_threshold_m2":
            # The threshold is read from the SPEC first (rules_loader._THRESHOLD_ALIASES),
            # so it must be injected there too — otherwise a spec-level alias such as
            # min_deduct_area_m2 would quietly outrank the value the user just demanded.
            old = next((_num(spec[k]) for k in _THRESHOLD_ALIASES if spec.get(k) is not None),
                       old)
            spec[name] = new
        rule = {**rule, name: new}
        if new == old:
            continue                                  # a redundant override is not news
        notes.append(
            f"تعديل يدوي معتمد بأمر المستخدم: {label} {show(old)} ← {show(new)} — "
            f"مسجَّل هنا في سجل التدقيق ولا يُخفى عن العميل."
        )
    for name in sorted(k for k in ovr if k not in OVERRIDABLE):
        notes.append(
            WARN_PREFIX + f"تعديل يدوي غير مدعوم: «{name}» — القابل للتعديل هنا هو "
            f"{'، '.join(OVERRIDABLE)} فقط، وأي بارامتر آخر يُضبط في مواصفة البند (spec)."
        )
    return rule


# --------------------------------------------------------------------------- #
# Cross-line links (parents and subtractions)                                   #
# --------------------------------------------------------------------------- #
def _needed_links(plan: _Plan) -> list[str]:
    """Link params this rule declares and the user has NOT pinned by hand."""
    declared = (plan.rule.get("spec_params") or {})
    user = plan.fa.spec or {}
    return [p for p in CROSS_LINKS if p in declared and user.get(p) is None]


def _rule_units(rule: dict) -> set[str]:
    """Every unit a rule may emit — ``unit_switch`` and ``unit_by_mode`` included.

    A rule is not always measured in its declared unit (a 0.35 m reveal flips from م.ط
    to م٢), so "same unit" has to mean "could produce the same unit", not "declares it".
    """
    out = {rule.get("unit")}
    sw = rule.get("unit_switch") or {}
    out.add(sw.get("unit"))
    out.update((rule.get("unit_by_mode") or {}).values())
    return {u for u in out if u}


def _unit_ok(m: "_Measured", plan: _Plan) -> bool:
    """May ``m``'s net be copied into ``plan``? Only if it measures the same KIND of thing.

    A parent's net is copied VERBATIM, so surface alone is not enough: a stair item and a
    floor-polishing item share the surface «floor» while one counts running metres of
    tread and the other square metres of slab. Copying 18.00 م.ط into an م٢ line bills
    18 m² of polishing that was never laid — the unit is the guard against that.
    """
    return m.line.unit in _rule_units(plan.rule)


def _sources_for(param: str, plan: _Plan) -> tuple[str, ...]:
    """Which measured items a "sum" link subtracts — ONE resolution, used everywhere.

    ``SUM_SOURCES`` is the registry; a facade rule reads the facade list whatever the
    param is called. Re-deriving this in ``_resolve_links`` is how ``floor_clad_area_m2``
    silently picked up a kitchen backsplash and deleted the kitchen's skirting.
    """
    if plan.rule.get("surface") == "facade":
        return FACADE_CLADDING_SOURCES
    return SUM_SOURCES.get(param, WALL_COVERING_SOURCES)


def _is_source(param: str, plan: _Plan, cand_key: str, cand_rule: dict) -> bool:
    """Could ``cand_key`` legitimately feed ``param`` of ``plan``?"""
    if cand_key == plan.rule_key:
        return False
    kind = CROSS_LINKS[param][1]
    if kind in ("parent", "ceiling") and not (_rule_units(cand_rule) & _rule_units(plan.rule)):
        return False                     # never copy a quantity across units
    if kind == "parent":
        if cand_key in DEFAULT_PARENTS.get(plan.rule_key, ()):
            return True
        return (cand_rule.get("surface") == plan.rule.get("surface")
                and not cand_rule.get("follows_rule"))
    if kind == "ceiling":
        return cand_key in DEFAULT_CEILING_PARENTS or cand_rule.get("surface") == "ceiling"
    return cand_key in _sources_for(param, plan)


def _ready(plan: _Plan, pending: list[_Plan]) -> bool:
    """A finish may be measured once no UNMEASURED finish could still feed it."""
    for param in _needed_links(plan):
        for other in pending:
            if other is not plan and _is_source(param, plan, other.rule_key, other.rule):
                return False
    return True


def _pick_parent(plan: _Plan, measured: list[_Measured], run: _Run,
                 prefer: tuple[str, ...], surface: Optional[str]) -> tuple[Optional[float], str]:
    """One parent, chosen explicitly or by the registry — never guessed."""
    want_unit = _u(plan.rule.get("unit"))
    want = (plan.fa.spec or {}).get("parent_rule")
    if want:
        key, _ = _follow_merges(_lookup(want, run.rules, run.index), run.rules)
        hits = [m for m in measured if m.plan.rule_key == key]
        if hits and not _unit_ok(hits[0], plan):
            return None, (WARN_PREFIX + f"بند الأصل المحدد «{want}» ({hits[0].line.code}) "
                          f"مقيس بوحدة {_u(hits[0].line.unit)} بينما هذا البند بوحدة "
                          f"{want_unit} — الكمية لا تُنسخ من وحدة إلى أخرى، والبند لن "
                          f"يُنتج كمية. صحّح parent_rule.")
        if hits:
            return hits[0].net_raw, (f"مصدر الكمية: بند «{hits[0].line.name_ar}» "
                                     f"({hits[0].line.code}) بتحديد المستخدم (parent_rule).")
        return None, (WARN_PREFIX + f"بند الأصل المحدد «{want}» غير مقيس في هذا الفراغ — "
                      f"لا يمكن استنساخ كميته، والبند لن يُنتج كمية.")
    wrong_unit: list[_Measured] = []
    for key in prefer:
        hits = [m for m in measured if m.plan.rule_key == key]
        if not hits:
            continue
        if not _unit_ok(hits[0], plan):
            wrong_unit.append(hits[0])
            continue
        note = (f"مصدر الكمية: بند «{hits[0].line.name_ar}» ({hits[0].line.code}) — "
                f"البند التابع يستنسخ صافي أصله ولا يُعيد قياسه، منعاً للازدواج.")
        others = [m for m in measured if m.plan.rule_key in prefer
                  and m.plan.rule_key != key and _unit_ok(m, plan)]
        if others:
            note = (WARN_PREFIX + note + " ويوجد في الفراغ بند أصل محتمل آخر ("
                    + "، ".join(f"«{m.line.code}»" for m in others)
                    + ") — إن كان هو المقصود فحدّده صراحةً بـ parent_rule.")
        return hits[0].net_raw, note
    cands = [m for m in measured
             if m.plan.rule["surface"] == (surface or plan.rule["surface"])
             and not m.plan.rule.get("follows_rule")]
    same = [m for m in cands if _unit_ok(m, plan)]
    wrong_unit.extend(m for m in cands if not _unit_ok(m, plan))
    if len(same) == 1:
        return same[0].net_raw, (f"مصدر الكمية: بند «{same[0].line.name_ar}» "
                                 f"({same[0].line.code}) — البند الوحيد المقيس على هذا "
                                 f"السطح في الفراغ بوحدة {want_unit}.")
    rejected = ""
    if wrong_unit:
        rejected = (" وتوجد على هذا السطح بنود بوحدات أخرى ("
                    + "، ".join(f"«{m.line.code}» {_u(m.line.unit)}" for m in wrong_unit)
                    + f") لا تصلح أصلاً لبند بوحدة {want_unit} — استنساخ كمية من وحدة إلى "
                      f"أخرى يحوّل المتر الطولي إلى متر مربع ويضاعف القيمة بلا عمل مقابل.")
    if not same:
        return None, (WARN_PREFIX + f"لا يوجد بند أصل مقيس على هذا السطح في الفراغ بوحدة "
                      f"{want_unit} — البند التابع لا يُنتج كمية. أضِف البند الأصل أو احذف "
                      f"هذا البند." + rejected)
    names = "، ".join(f"«{m.plan.rule_key}»" for m in same)
    return None, (WARN_PREFIX + f"أكثر من بند أصل محتمل في نفس الفراغ ({names}) — المحرك لا "
                  f"يخمّن أيها الأصل. حدّد parent_rule في مواصفة البند." + rejected)


def _sum_sources(sources: tuple[str, ...], measured: list[_Measured],
                 label: str) -> tuple[float, str]:
    """Σ of the AREAS a cladding item already claimed, so plaster does not claim them again.

    Only م٢ lines are summed: the label says م٢ and the caller subtracts the result from an
    area, so folding a running metre into the sum would subtract length from area.
    """
    hits = [m for m in measured if m.plan.rule_key in sources]
    areas = [m for m in hits if m.line.unit == "m2"]
    other = [m for m in hits if m.line.unit != "m2"]
    tail = ""
    if other:
        tail = (WARN_PREFIX + f"لم تُطرح ({'، '.join(f'«{m.line.code}» ' + _u(m.line.unit) for m in other)}) "
                f"لأن وحدتها ليست {_M2} ولا يجوز طرحها من مساحة — راجعها يدوياً.")
    if not areas:
        return 0.0, (f"{label} = 0 (لا يوجد بند تكسية مقيس في هذا الفراغ يُطرح منه). "
                     + tail).strip()
    total = sum(m.net_raw for m in areas)
    parts = "، ".join(f"«{m.line.name_ar}» ({m.line.code}) {_fmt(m.net_raw)} {_M2}"
                      for m in areas)
    return total, (f"{label} = {_fmt(total)} {_M2} — مطروحة تلقائياً من: {parts}. "
                   f"المساحة المكسوّة لا تُقاس مرتين. " + tail).strip()


def _resolve_links(plan: _Plan, spec: dict, measured: list[_Measured],
                   run: _Run, notes: list[str]) -> None:
    """Fill every unpinned link param in ``spec``, loudly, before the formula runs."""
    for param in _needed_links(plan):
        label, kind, guard = CROSS_LINKS[param]
        if guard and not _truthy(spec.get(guard), _truthy(
                (plan.rule.get("spec_params") or {}).get(guard), True)):
            spec[param] = 0.0
            notes.append(f"{label} = 0 لأن المفتاح «{guard}» مُطفأ في المواصفة.")
            continue
        if kind == "parent":
            value, note = _pick_parent(plan, measured, run,
                                       DEFAULT_PARENTS.get(plan.rule_key, ()), None)
        elif kind == "ceiling":
            value, note = _pick_parent(plan, measured, run, DEFAULT_CEILING_PARENTS, "ceiling")
        else:
            value, note = _sum_sources(_sources_for(param, plan), measured, label)
        spec[param] = 0.0 if value is None else float(value)
        notes.append(note)


# --------------------------------------------------------------------------- #
# Guards                                                                        #
# --------------------------------------------------------------------------- #
def _guard_notes(plan: _Plan, space: Space) -> list[str]:
    out: list[str] = []
    forbidden, reason = SPACE_TYPE_GUARDS.get(plan.rule_key, ((), ""))
    forbidden = tuple(plan.rule.get("forbidden_space_types") or forbidden)
    if space.type in forbidden:
        out.append(WARN_PREFIX + f"البند مُسند إلى فراغ من نوع "
                                 f"«{SPACE_TYPES_AR.get(space.type, space.type)}»: {reason} "
                                 f"الكمية محسوبة كما طُلبت — القرار للمعماري لا للبرنامج.")
    if plan.fa.surface and plan.fa.surface != plan.rule.get("surface"):
        out.append(
            WARN_PREFIX + f"الإسناد يقول سطح «{_surface_ar(plan.fa.surface)}» بينما القاعدة "
            f"«{plan.rule_key}» تقيس سطح «{_surface_ar(plan.rule.get('surface'))}» — "
            f"اعتُمد سطح القاعدة. تحقّق من الإسناد."
        )
    return out


def _apply_conflicts(measured: list[_Measured]) -> None:
    """Append a double-claim warning to BOTH lines of every conflicting pair."""
    for keys_a, keys_b, pred, message in CONFLICTS:
        for ma in measured:
            if ma.plan.rule_key not in keys_a:
                continue
            for mb in measured:
                if mb.plan.rule_key not in keys_b or mb is ma:
                    continue
                try:
                    hit = bool(pred(ma.spec, mb.spec))
                except Exception:                    # a guard must never break a take-off
                    hit = False
                if not hit:
                    continue
                text = (WARN_PREFIX + f"ازدواج مطالبة محتمل بين «{ma.line.code}» و"
                        f"«{mb.line.code}»: {message}")
                for line in (ma.line, mb.line):
                    if text not in line.breakdown:
                        line.breakdown.append(text)


# --------------------------------------------------------------------------- #
# Warning lines                                                                 #
# --------------------------------------------------------------------------- #
def _warning_line(space: Space, surface: str, rule_key: str,
                  headline: str, detail: list[str] | None = None) -> BOQLine:
    """A visible hole in the bill. Zero quantity, and the reason in Arabic.

    Emitted INSTEAD of raising: a take-off that dies on room 14 of 30 is useless,
    and a finish that vanishes silently is an under-quote nobody notices.
    """
    return BOQLine(
        code=WARNING_CODE,
        name_ar="تنبيه حصر — بند لم يُقَس",
        space_id=space.id,
        space_name=space.name,
        surface=surface or "",
        unit="no",
        net_qty=0.0,
        waste_pct=0.0,
        gross_qty=0.0,
        rule_key=rule_key,
        breakdown=[WARN_PREFIX + headline, *(detail or []),
                   "لم تُحتسب أي كمية لهذا البند — الصفر هنا معناه «غير مقيس»، وليس "
                   "«لا يوجد عمل». يجب معالجته قبل إصدار العرض."],
    )


def is_warning(line: BOQLine) -> bool:
    return line.code == WARNING_CODE


def warnings_of(lines) -> list[str]:
    """Every Arabic warning in the bill: skipped items + flagged lines, in order."""
    out: list[str] = []
    for line in lines or []:
        for entry in line.breakdown:
            if not _is_warn(entry):
                continue
            text = _clean_warn(entry)
            if text not in out:
                out.append(text)
    return out


# --------------------------------------------------------------------------- #
# The measurement of ONE finish                                                 #
# --------------------------------------------------------------------------- #
def _input_lines(ctx: _Ctx, trail) -> list[str]:
    """One Arabic bullet per formula input: label = arithmetic = value + unit."""
    out: list[str] = []
    for name, label, unit_ar, value in trail:
        shown = _fmt_auto(value, unit_ar)
        suffix = f" {unit_ar}" if unit_ar else ""
        if label == "بارامتر المواصفة":          # rules_loader's generic label for spec values
            label = f"بارامتر المواصفة «{name}»"
        deriv = DERIVATIONS.get(name)
        text = ""
        if deriv is not None:
            try:
                text = deriv(ctx) or ""
            except (GeometryError, ZeroDivisionError, ValueError, TypeError, IndexError):
                text = ""                        # a derivation may never break a quantity
        if text:
            out.append(f"  • {_short_label(label)} = {text} = {shown}{suffix}   [{name}]")
        else:
            out.append(f"  • {label} = {shown}{suffix}   [{name}]")
    return out


def _measure(space: Space, plan: _Plan, measured: list[_Measured], run: _Run) -> _Measured:
    """Measure one rule over one space at full precision, then round exactly once."""
    rule = plan.rule
    notes: list[str] = list(plan.notes)
    notes.extend(_guard_notes(plan, space))

    # spec = the rule's declared defaults, overlaid with the project's own spec
    spec: dict = dict(rule.get("spec_params") or {})
    spec.update({k: v for k, v in (plan.fa.spec or {}).items() if v is not None})

    rule = _apply_overrides(rule, spec, _override_for(run.overrides, space.id, plan.rule_key),
                            notes)
    _resolve_links(plan, spec, measured, run, notes)

    ctx = _Ctx(space=space, rule=rule, spec=spec,
               owned_opening_ids=(run.owned or {}).get(space.id))
    formula = rule["formula"]
    names = formula_names(formula)
    env, trail = build_env(ctx, names)
    value, substituted = safe_eval(formula, env)

    # ---- full-precision pipeline: nothing below is rounded until the very end ----
    net_raw = float(value)
    negative = net_raw < 0.0
    if negative:
        net_raw = 0.0
    unit, net_raw, switch_lines = _resolve_unit(ctx, net_raw)
    # The rule's rate PLUS whatever the chosen spec actually costs: a diagonal or
    # herringbone lay throws away several more points of the purchase order, and each
    # rule's waste_modifiers table says by how much. Reading rule["waste_pct"] raw here
    # is how those promised uplifts silently never happened.
    waste, waste_lines = _waste_pct(ctx)
    unit_ar = _u(unit)

    bd: list[str] = [
        f"البند: {rule['name_ar']} ({rule['code']}) — الفراغ «{space.name}» ({space.id}) — "
        f"السطح: {_surface_ar(rule.get('surface'))} — الوحدة: {unit_ar}",
        f"القاعدة المعتمدة: «{plan.rule_key}» من ملف قواعد التشطيبات "
        f"(core/rules/finishes.yaml) — حصر حسابي بحت، لا تقدير ولا نموذج لغوي.",
        "المُدخلات وكيف اشتُقّ كل رقم منها:",
    ]
    bd.extend(_input_lines(ctx, trail))

    if rule.get("deduct_openings"):
        bd.append(
            f"قاعدة الخصم المطبَّقة: تُخصم الفتحة إذا بلغت مساحتها حدّ الخصم "
            f"{_fmt(_threshold(ctx))} {_M2} فأكثر — وهو عُرف مهني متفق عليه بين المكاتب "
            f"وقابل للضبط، وليس قاعدة معيارية مُلزِمة."
        )
    if notes:
        bd.append("قرارات المحرك وتنبيهاته:")
        bd.extend(f"  – {n}" for n in notes)
    if ctx.notes:
        bd.append("ملاحظات الحصر من ملف القواعد:")
        bd.extend(f"  – {n}" for n in ctx.notes)

    bd.append(f"المعادلة: {' '.join(str(formula).split())}")
    bd.append(f"بالأرقام: {substituted} = {_fmt(value)}")
    if negative:
        bd.append(
            WARN_PREFIX + f"نتيجة المعادلة سالبة ({_fmt(value)}) وصُفِّرت — الخصومات تتجاوز "
            f"المساحة المقاسة. راجع المُدخلات قبل الاعتماد."
        )
    bd.extend(switch_lines)
    bd.extend(waste_lines)

    # ---- the single rounding step -------------------------------------------- #
    if unit == "no":
        net = float(round(net_raw))              # nobody buys 3.4 عتبات
        if rule.get("waste_on_count"):
            gross = float(math.ceil(net * (1.0 + waste)))
            bd.append(f"الصافي = {_cnt(net)} عدد | الهدر {_pct(waste * 100)} مع التقريب "
                      f"لأعلى ⇒ الإجمالي (كمية التوريد) = {_cnt(gross)} عدد")
        else:
            gross = net
            bd.append(f"الصافي = {_cnt(net)} عدد | لا يُضاف الهدر إلى عددٍ مدفوع "
                      f"(نسبة {_pct(waste * 100)} تُطبَّق على مساحة الشراء فقط) ⇒ "
                      f"الإجمالي = {_cnt(gross)} عدد")
        bd.append("التقريب: بند بالعدد — الكمية عدد صحيح، ولا معنى لكسر قطعة في أمر شراء.")
    else:
        net = _r2(net_raw)
        gross = _r2(net_raw * (1.0 + waste))
        bd.append(f"الصافي = {_fmt(net)} {unit_ar} — وهي الكمية المدفوعة في المستخلص.")
        bd.append(f"الهدر {_pct(waste * 100)}: {_fmt(net)} × (1 + {_fmt(waste)}) = "
                  f"{_fmt(gross)} {unit_ar}؛ مقدار الهدر = {_fmt(gross - net)} {unit_ar}.")
        bd.append(f"الإجمالي (كمية التوريد وأمر الشراء) = {_fmt(gross)} {unit_ar} — "
                  f"الدفع في المستخلص على الصافي لا على الإجمالي، وإلا دُفع الهدر مرتين.")
        bd.append("التقريب: خانتان عشريتان، مرة واحدة في نهاية الحساب فقط — لم تُقرَّب أي "
                  "قيمة وسيطة، ولذلك لا يتراكم الخطأ عبر بنود المشروع.")

    line = BOQLine(
        code=rule["code"], name_ar=rule["name_ar"], space_id=space.id, space_name=space.name,
        surface=rule["surface"], unit=unit, net_qty=net, waste_pct=waste, gross_qty=gross,
        rule_key=plan.rule_key, breakdown=bd,
    )
    # net_raw, NOT net: a dependent line copies the unrounded number so that the two
    # printed quantities still agree after each is rounded on its own.
    return _Measured(plan=plan, line=line, net_raw=float(net_raw), spec=spec)


# --------------------------------------------------------------------------- #
# The take-off                                                                  #
# --------------------------------------------------------------------------- #
def _plan_space(space: Space, run: _Run) -> tuple[list[_Plan], list[tuple[int, BOQLine]]]:
    """Resolve every assignment to a rule; unknown ones become warning lines."""
    plans: list[_Plan] = []
    skipped: list[tuple[int, BOQLine]] = []
    for i, fa in enumerate(space.finishes or []):
        key, notes = _follow_merges(_lookup(fa.system, run.rules, run.index), run.rules)
        if key is None or key not in run.rules:
            skipped.append((i, _warning_line(
                space, fa.surface, str(fa.system or ""),
                f"نظام التشطيب «{fa.system}» المُسند لسطح "
                f"«{_surface_ar(fa.surface)}» غير معروف في ملف القواعد — تُخطّي البند.",
                ["راجع مفتاح القاعدة أو كود البند؛ يمكن الإسناد بمفتاح القاعدة أو بكود "
                 "جدول الكميات أو بأحد الأكواد القديمة (aliases)."],
            )))
            continue
        plans.append(_Plan(index=i, fa=fa, rule_key=key, rule=run.rules[key], notes=notes))
    return plans, skipped


def _geometry_warnings(space: Space) -> list[str]:
    """``Space.validate()`` folded into the take-off, so bad geometry cannot pass silently.

    A room entered with ``height_m = 0`` measures 0 م٢ of plaster without one negative
    number appearing anywhere: no clamp fires, no formula fails, and the line prints a
    perfectly ordinary zero. The schema already knows this is wrong — but nothing was
    asking it. A silent 0 is a silent under-quote, so the answer must travel WITH the
    lines, through ``warnings_of()`` and ``summarize()``, not sit in a method nobody calls.
    """
    try:
        return [WARN_PREFIX + str(w).strip()
                for w in (space.validate() or []) if str(w).strip()]
    except (GeometryError, ValueError, TypeError, AttributeError):
        return []                        # a guard must never break a take-off


def _ownership(project: Project) -> dict[str, set[str]]:
    """Which space carries each physically-single opening (see ``_Ctx.owned_opening_ids``).

    A door on a party wall is modelled in BOTH adjoining rooms — each Space owns its own
    walls, and the plaster and skirting of both rooms need it there. A per-PIECE item
    (a marble threshold, the tile strip inside the opening) would therefore be billed
    twice for one physical piece. The first space to declare an opening id owns it, unless
    a space claims it outright with ``owns_opening_ids`` in any of its finish specs.
    """
    claimed: dict[str, str] = {}
    for space in project.spaces or []:
        for fa in space.finishes or []:
            for oid in ((fa.spec or {}).get("owns_opening_ids") or []):
                claimed.setdefault(str(oid), space.id)
    owned: dict[str, set[str]] = {}
    first: dict[str, str] = {}
    for space in project.spaces or []:
        mine: set[str] = set()
        for o in space.openings:
            oid = str(getattr(o, "id", "") or "")
            if not oid:
                continue                 # an unnamed opening cannot be matched across rooms
            if (claimed.get(oid) or first.setdefault(oid, space.id)) == space.id:
                mine.add(oid)
        owned[space.id] = mine
    return owned


def _takeoff_space(space: Space, run: _Run) -> list[BOQLine]:
    geom = _geometry_warnings(space)
    if not (space.finishes or []):
        return [_warning_line(space, "", "",
                              f"الفراغ «{space.name}» لا يحتوي على أي تشطيب مُسند — "
                              f"لا يوجد ما يُحصر فيه.", geom)]

    plans, out = _plan_space(space, run)
    for plan in plans:
        plan.notes.extend(geom)          # every quantity in this room rests on that geometry
    if geom and not plans:
        out.append((len(space.finishes or []), _warning_line(
            space, "", "",
            f"الفراغ «{space.name}» يحمل ملاحظات هندسية ولم يُقَس فيه أي بند.", geom)))
    pending = list(plans)
    measured: list[_Measured] = []

    # Dependency-ordered measurement: a dependent item is measured only once every item
    # that could feed it already exists, so it copies a real number instead of a zero.
    while pending:
        ready = [p for p in pending if _ready(p, pending)]
        if not ready:
            # Mutually waiting items (only reachable via a hand-edited rules file).
            ready = [pending[0]]
            ready[0].notes.append(
                WARN_PREFIX + "تعذّر ترتيب تبعية هذا البند مع بنود الفراغ الأخرى (حلقة "
                "تبعية) — قِيس أولاً وقد لا يجد كمية أصله. راجع ترتيب البنود."
            )
        done = {id(p) for p in ready}
        for plan in ready:
            try:
                measured.append(_measure(space, plan, measured, run))
            except (GeometryError, FormulaError, RuleError) as exc:
                out.append((plan.index, _warning_line(
                    space, plan.rule.get("surface", plan.fa.surface), plan.rule_key,
                    f"تعذّر حصر البند «{plan.rule.get('name_ar', plan.rule_key)}» "
                    f"({plan.rule.get('code', '')}): {exc}",
                    ["رُفض الاستمرار بكمية صفرية صامتة — الصفر الصامت يتحوّل إلى تسعير "
                     "ناقص يتحمّله المقاول والمكتب."],
                )))
        pending = [p for p in pending if id(p) not in done]

    _apply_conflicts(measured)
    out.extend((m.plan.index, m.line) for m in measured)
    # Emit in the order the architect wrote the finishes — the bill reads as authored,
    # even though it was measured in dependency order.
    out.sort(key=lambda pair: pair[0])
    return [line for _i, line in out]


def takeoff_space(space: Space, *, overrides: Optional[dict] = None,
                  rules: Optional[dict] = None,
                  rules_path: Optional[str] = None) -> list[BOQLine]:
    """Measure every finish of ONE space. Deterministic; never raises on bad geometry.

    ``overrides`` is ``{scope: {"waste_pct": float, "deduct_threshold_m2": float}}`` with
    ``scope`` one of ``"*"``, ``"<space_id>:*"``, ``"<rule_key>"`` or
    ``"<space_id>:<rule_key>"`` — narrowest scope wins, and every applied or refused
    override is written into the affected line's ``breakdown``.
    """
    rules = rules if rules is not None else load_rules(rules_path)
    run = _Run(rules=rules, index=_code_index(rules), overrides=dict(overrides or {}))
    return _takeoff_space(space, run)


def takeoff(project: Project, *, overrides: Optional[dict] = None,
            rules: Optional[dict] = None,
            rules_path: Optional[str] = None) -> list[BOQLine]:
    """Measure a whole project: per-space lines, in the order they were authored.

    Lines are NEVER aggregated here — a bill of quantities is measured room by room and
    must stay auditable room by room. Use :func:`summarize` for the totals.
    """
    rules = rules if rules is not None else load_rules(rules_path)
    # Ownership is a PROJECT fact — it is the only place that can see the same door in
    # two rooms — so it is resolved once here and handed to every space.
    run = _Run(rules=rules, index=_code_index(rules), overrides=dict(overrides or {}),
               owned=_ownership(project))

    out: list[BOQLine] = []
    seen: set[str] = set()
    for space in project.spaces or []:
        if space.id in seen:
            out.append(_warning_line(
                space, "", "",
                f"مُعرّف الفراغ «{space.id}» مكرر في المشروع — الفراغ محسوب أكثر من مرة "
                f"وهذا ازدواج مباشر في الكميات والقيمة.",
                ["وحّد المعرّفات قبل إصدار العرض؛ لم يُمنع الحصر حتى لا تختفي كميات، "
                 "لكن المجاميع أعلى من الحقيقة بمقدار هذا الفراغ."],
            ))
        seen.add(space.id)
        out.extend(_takeoff_space(space, run))
    return out


# --------------------------------------------------------------------------- #
# Summary                                                                       #
# --------------------------------------------------------------------------- #
def _blank_total(unit: str) -> dict:
    return {"unit": unit, "unit_ar": _u(unit), "net_qty": 0.0, "gross_qty": 0.0, "lines": 0}


def _add(bucket: dict, line: BOQLine) -> None:
    bucket["net_qty"] += float(line.net_qty)
    bucket["gross_qty"] += float(line.gross_qty)
    bucket["lines"] += 1


def _round_bucket(bucket: dict) -> dict:
    bucket["net_qty"] = _r2(bucket["net_qty"])
    bucket["gross_qty"] = _r2(bucket["gross_qty"])
    return bucket


def _bucket_key(buckets: dict, line: BOQLine) -> str:
    """One bucket per rule_key — unless a unit_switch gave the same rule two units.

    Adding a linear stair item to a square-metre stair item would be arithmetic on two
    different things, so the second unit gets its own bucket instead.
    """
    key = line.rule_key
    b = buckets.get(key)
    if b is not None and b["unit"] != line.unit:
        return f"{key}@{line.unit}"
    return key


def summarize(lines: list[BOQLine]) -> dict:
    """Totals per rule_key and per space, without ever losing the per-space lines.

    Aggregation adds the ROUNDED line quantities on purpose: a printed bill must add up
    to its own printed total, and a client who re-adds the column by hand must get the
    same number the engine printed. (The line quantities themselves were rounded once,
    from full-precision arithmetic — this is the only place two rounded numbers meet.)

    Quantities are only ever summed WITHIN one unit — م٢ and م.ط are never added — and
    warning lines are excluded from every total and listed separately instead, so a
    skipped item can never look like a measured zero.
    """
    lines = list(lines or [])
    real = [ln for ln in lines if not is_warning(ln)]

    by_rule: dict[str, dict] = {}
    by_space: dict[str, dict] = {}
    by_surface: dict[str, dict] = {}
    by_unit: dict[str, dict] = {}
    split: list[str] = []

    def _space_of(line: BOQLine) -> dict:
        s = by_space.get(line.space_id)
        if s is None:
            s = by_space[line.space_id] = {
                "space_id": line.space_id, "space_name": line.space_name,
                "lines": 0, "warnings": 0, "by_rule": {}, "by_unit": {},
                "warnings_ar": [],
            }
        return s

    for line in real:
        # ---- per rule_key, aggregated across every space
        key = _bucket_key(by_rule, line)
        if key != line.rule_key and key not in by_rule:
            split.append(WARN_PREFIX + f"البند «{line.rule_key}» ({line.code}) ظهر بوحدتين "
                                       f"مختلفتين في المشروع — فُصل المجموع لكل وحدة على "
                                       f"حدة، ولا يجوز جمعهما.")
        r = by_rule.get(key)
        if r is None:
            r = by_rule[key] = {
                "rule_key": line.rule_key, "code": line.code, "name_ar": line.name_ar,
                "surface": line.surface, "surface_ar": _surface_ar(line.surface),
                **_blank_total(line.unit), "waste_pct": float(line.waste_pct),
                "spaces": [], "_rates": set(),
            }
        _add(r, line)
        r["_rates"].add(round(float(line.waste_pct), 6))
        if line.space_id not in r["spaces"]:
            r["spaces"].append(line.space_id)

        # ---- per space, so every line stays addressable where it was measured
        s = _space_of(line)
        s["lines"] += 1
        skey = _bucket_key(s["by_rule"], line)
        sr = s["by_rule"].get(skey)
        if sr is None:
            sr = s["by_rule"][skey] = {
                "rule_key": line.rule_key, "code": line.code, "name_ar": line.name_ar,
                "surface": line.surface, **_blank_total(line.unit),
                "waste_pct": float(line.waste_pct),
            }
        _add(sr, line)
        _add(s["by_unit"].setdefault(line.unit, _blank_total(line.unit)), line)
        for w in line.breakdown:
            if _is_warn(w) and _clean_warn(w) not in s["warnings_ar"]:
                s["warnings_ar"].append(_clean_warn(w))

        # ---- project roll-ups, always keyed by unit so nothing incompatible is added
        surf = by_surface.setdefault(line.surface, {
            "surface": line.surface, "surface_ar": _surface_ar(line.surface), "by_unit": {}})
        _add(surf["by_unit"].setdefault(line.unit, _blank_total(line.unit)), line)
        _add(by_unit.setdefault(line.unit, _blank_total(line.unit)), line)

    # A skipped item belongs to its space too, so nothing hides at project level.
    for line in lines:
        if not is_warning(line):
            continue
        s = _space_of(line)
        s["warnings"] += 1
        for w in line.breakdown:
            if _is_warn(w) and _clean_warn(w) not in s["warnings_ar"]:
                s["warnings_ar"].append(_clean_warn(w))

    for r in by_rule.values():
        _round_bucket(r)
        # An override can give the same rule a different waste in different rooms. When the
        # rate was uniform the exact rate is reported; when it was not, the rate the money
        # actually implies is reported instead — never a rate nobody was charged.
        rates = r.pop("_rates")
        r["waste_pct"] = (round(rates.pop(), 4) if len(rates) == 1 else
                          (round((r["gross_qty"] - r["net_qty"]) / r["net_qty"], 4)
                           if r["net_qty"] > 0 else round(r["waste_pct"], 4)))
    for s in by_space.values():
        for bucket in list(s["by_rule"].values()) + list(s["by_unit"].values()):
            _round_bucket(bucket)
    for surf in by_surface.values():
        for bucket in surf["by_unit"].values():
            _round_bucket(bucket)
    for bucket in by_unit.values():
        _round_bucket(bucket)

    return {
        "by_rule": by_rule,
        "by_space": by_space,
        "by_surface": by_surface,
        "by_unit": by_unit,
        "totals": {
            "lines": len(real),
            "spaces": len(by_space),
            "rules": len(by_rule),
            "warnings": sum(1 for ln in lines if is_warning(ln)),
        },
        "warnings": split + warnings_of(lines),
        "basis_ar": (
            "الكميات الصافية هي أساس الدفع في المستخلص؛ الكميات الإجمالية (بعد الهدر) هي "
            f"أساس التوريد وأمر الشراء فقط. المجاميع تُجمع داخل الوحدة الواحدة ولا يُجمع "
            f"{_M2} مع {_ML}. التقريب خانتان عشريتان مرة واحدة في نهاية حساب كل بند، "
            "ومجموع العمود يساوي حاصل جمع البنود المطبوعة تماماً."
        ),
    }
