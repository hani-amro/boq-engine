# -*- coding: utf-8 -*-
"""Loads ``rules/finishes.yaml`` and turns one rule + one space into a quantity.

Why this module exists at all: the measurement conventions of a Levant BOQ are
*data*, not logic. The deduction threshold, the waste allowance, whether reveals
are paid — these are office conventions the architect must be able to change
without touching Python, and must be able to defend line by line in front of a
contractor. So the arithmetic lives in the YAML and this module is only the safe,
auditable way to run it.

Two hard requirements shape the design:

* **No ``eval``.** A formula is parsed with :mod:`ast` and walked node by node.
  Only ``+ - * /``, unary sign, parentheses, ``min``/``max``, numeric literals and
  a CLOSED whitelist of names are accepted. Attribute access, subscripting, calls,
  comprehensions, imports — anything else is rejected with a clear error. A rules
  file is editable by a non-programmer, so it must never become a code path.
* **Every number carries its arithmetic.** ``evaluate()`` returns the value *and*
  an Arabic step-by-step ``breakdown``: each input with its Arabic label and value,
  the symbolic formula, the same formula with the numbers substituted, the net, the
  waste and the gross. ``BOQLine.breakdown`` is a product requirement, not a debug
  aid — a wrong quantity is real money lost and real legal liability, and a number
  nobody can trace is a number nobody can defend in an interim certificate.

Nothing here calls an LLM. Dependencies: Python stdlib + PyYAML.
"""

from __future__ import annotations

import ast
import math
import operator
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import yaml

from .schema import UNITS, UNITS_AR, Space, BOQLine

__all__ = [
    "RuleError", "FormulaError", "RULES_PATH", "load_rules", "get_rule",
    "validate_rules", "safe_eval", "build_env", "evaluate", "boq_line",
    "GEOMETRY_FIELDS", "ALLOWED_FUNCS", "WARN_PREFIX",
]

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "finishes.yaml")

# Marks a note as a WARNING rather than an explanation. It lives HERE, at the lowest
# layer that can produce one, and boq_engine imports it: a note that announces a zero
# quantity or a guessed one must round-trip through warnings_of()/summarize(), and it
# can only do that if both modules agree on a single marker string.
WARN_PREFIX = "⚠ تنبيه: "

# Anti-DoS / anti-cleverness ceilings on a hand-edited data file.
MAX_FORMULA_LEN = 600
MAX_FORMULA_NODES = 300

REQUIRED_FIELDS = ("code", "name_ar", "surface", "unit", "formula",
                   "deduct_openings", "deduct_threshold_m2", "add_reveals",
                   "waste_pct", "spec_params", "notes_ar")

DEFAULT_SILL_M = 0.90           # window sill above finished floor when unstated
DEFAULT_NICHE_DEPTH_M = 0.12
_ND = 3                         # stored precision of a quantity (mm-ish)


class RuleError(ValueError):
    """The rules file itself is wrong (missing field, bad unit, name collision)."""


class FormulaError(ValueError):
    """A formula is not a restricted arithmetic expression over known names."""


# --------------------------------------------------------------------------- #
# Small pure helpers                                                            #
# --------------------------------------------------------------------------- #
def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _r(x: float, nd: int = _ND) -> float:
    return round(float(x), nd)


def _fmt(x: float) -> str:
    """Western digits, 2 decimals — how a Levant BOQ prints a quantity."""
    return f"{float(x):.2f}"


def _cnt(x: float) -> str:
    return str(int(round(float(x))))


def _pct(x: float) -> str:
    """Percentage without trailing zeros: 10% / 12.5%."""
    s = f"{float(x):.2f}".rstrip("0").rstrip(".")
    return (s or "0") + "%"


def _fmt_auto(x: float, unit_ar: str) -> str:
    """Counts and on/off switches read as integers; everything else as a quantity."""
    if unit_ar in ("", "عدد") and abs(float(x) - round(float(x))) < 1e-9:
        return _cnt(x)
    return _fmt(x)


def _overlap(lo: float, hi: float, top: float) -> float:
    """Length of [lo, hi] that falls inside [0, top]."""
    return max(0.0, min(hi, top) - min(lo, top))


# --------------------------------------------------------------------------- #
# Evaluation context                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class _Ctx:
    """Everything a derived field may read: the space, the rule, the merged spec.

    ``owned_opening_ids`` is the project-level answer to "who pays for this door?".
    A door on a party wall is modelled in BOTH adjoining spaces (each Space owns its
    own walls), so a per-PIECE item — a marble threshold, the tile strip inside the
    opening — would be billed twice for one physical piece. ``None`` means "this space
    owns everything it can see", which is the correct answer for a single-space
    measurement; :func:`core.boq_engine.takeoff` narrows it per space.
    """

    space: Space
    rule: dict
    spec: dict
    notes: list[str] = field(default_factory=list)
    owned_opening_ids: Optional[set[str]] = None


def _sp(ctx: _Ctx, key: str, default: float = 0.0) -> float:
    """Numeric spec parameter (user spec wins over the rule's declared default)."""
    if key in ctx.spec and ctx.spec[key] is not None:
        return _num(ctx.spec[key], default)
    return default


def _flag(ctx: _Ctx, key: str, default: bool = False) -> float:
    v = ctx.spec.get(key, default)
    if v is None:
        v = default
    if isinstance(v, str):
        v = v.strip().lower() in ("true", "yes", "1")
    return 1.0 if v else 0.0


def _txt(ctx: _Ctx, key: str, default: str = "") -> str:
    v = ctx.spec.get(key, default)
    return default if v is None else str(v)


def _seq(ctx: _Ctx, key: str) -> list[float]:
    v = ctx.spec.get(key)
    if isinstance(v, (list, tuple)):
        return [_num(a) for a in v]
    return []


# The threshold is a CONVENTION, so the project spec may override it under any of
# the names the three trade families use for it. Rule value is the fallback.
_THRESHOLD_ALIASES = ("deduct_threshold_m2", "min_deduct_area_m2",
                      "facade_deduct_threshold_m2", "acp_deduct_threshold_m2")


def _threshold(ctx: _Ctx) -> float:
    for k in _THRESHOLD_ALIASES:
        if ctx.spec.get(k) is not None:
            return _num(ctx.spec[k])
    return _num(ctx.rule.get("deduct_threshold_m2"), 0.0)


def _ceiling_threshold(ctx: _Ctx) -> float:
    if ctx.spec.get("ceiling_deduct_threshold_m2") is not None:
        return _num(ctx.spec["ceiling_deduct_threshold_m2"])
    return _num(ctx.rule.get("deduct_threshold_m2"), 0.0)


def _deducts_openings(ctx: _Ctx) -> bool:
    return bool(ctx.rule.get("deduct_openings", False))


def _sill(ctx: _Ctx, o) -> float:
    """Height of an opening's bottom edge above the finished floor."""
    return _sp(ctx, "sill_height_m", DEFAULT_SILL_M) if o.kind == "window" else 0.0


def _band(ctx: _Ctx) -> float:
    """The banded height a wall rule works over (tiling / cladding / paint band)."""
    h = _num(ctx.space.height_m)
    if ctx.rule.get("full_height_param") and _flag(ctx, ctx.rule["full_height_param"], False):
        return h
    p = ctx.rule.get("band_height_param")
    if not p:
        return h
    v = _sp(ctx, p, 0.0)
    return min(h, v) if v > 0 else h


def _is_deductible(ctx: _Ctx, o) -> bool:
    """The convention in one place: a hole is deducted only above the threshold."""
    return (_deducts_openings(ctx) and o.is_hole
            and o.area_m2 >= _threshold(ctx) and int(o.count or 0) > 0)


def _external_deductible(ctx: _Ctx, wall, o) -> bool:
    return bool(wall.is_external) and _is_deductible(ctx, o)


def _warn(ctx: _Ctx, text: str) -> None:
    """A note that MUST reach the warning channel: a zero, a guess, a dropped item."""
    ctx.notes.append(WARN_PREFIX + text)


def _owns(ctx: _Ctx, o) -> bool:
    """Does THIS space carry the cost of this physical piece? (see _Ctx.owned_opening_ids)"""
    if ctx.owned_opening_ids is None:
        return True
    oid = str(getattr(o, "id", "") or "")
    return (not oid) or oid in ctx.owned_opening_ids


# --------------------------------------------------------------------------- #
# Derived geometry registry: stable name -> (Arabic label, unit, extractor).     #
# A formula may reference ONLY these names plus the rule's own spec_params.      #
# --------------------------------------------------------------------------- #
def _f_manual_deduct(ctx: _Ctx) -> float:
    """Σ of the surveyor's listed solid areas that reach the threshold."""
    lim = _threshold(ctx)
    keep = [a for a in _seq(ctx, "deduct_areas_m2") if a >= lim]
    drop = [a for a in _seq(ctx, "deduct_areas_m2") if a < lim]
    for a in drop:
        ctx.notes.append(f"لا تُخصم مساحة {_fmt(a)} م٢ لأنها دون حدّ الخصم {_fmt(lim)} م٢ "
                         f"(عُرف مهني قابل للضبط).")
    return sum(keep)


def _f_door_reveal_strip(ctx: _Ctx) -> float:
    """The strip of floor tile that runs inside the door opening (m²).

    One opening = one strip, so a door shared with the next room is measured in the
    owning space only — otherwise the same 0.18 m² is laid twice on paper.
    """
    if not _flag(ctx, "tile_door_reveal", True):
        return 0.0
    run, shared = 0.0, []
    for o in ctx.space.openings:
        if o.kind not in ("door", "arch"):
            continue
        if _owns(ctx, o):
            run += _num(o.width_m) * int(o.count or 0)
        else:
            shared.append(str(o.id))
    if shared:
        ctx.notes.append(f"لم يُحتسب شريط البلاط داخل الفتحات المشتركة ({'، '.join(shared)}) "
                         f"لأنها محمَّلة على الفراغ المجاور — القطعة الواحدة تُقاس مرة واحدة "
                         f"في المشروع.")
    return run * _sp(ctx, "door_reveal_depth_m", 0.20)


def _f_slope_factor(ctx: _Ctx) -> float:
    """Falls are measured on plan — unless the slope exceeds the 15% convention."""
    s = _sp(ctx, "slope_pct", 0.0)
    if s <= 15.0:
        return 1.0
    f = 1.0 / math.cos(math.atan(s / 100.0))
    ctx.notes.append(f"الميل {_pct(s)} يتجاوز 15% — قيست المساحة المائلة الحقيقية "
                     f"بمعامل {f:.4f} بدل المسقط الأفقي.")
    return f


def _f_deductible_openings_area(ctx: _Ctx) -> float:
    total = 0.0
    for o in ctx.space.openings:
        if not o.is_hole:
            continue
        if _is_deductible(ctx, o):
            total += o.total_area_m2
        elif _deducts_openings(ctx) and int(o.count or 0) > 0:
            ctx.notes.append(f"لا تُخصم {o.id} ({_fmt(o.width_m)}×{_fmt(o.height_m)} = "
                             f"{_fmt(o.area_m2)} م٢) لأنها دون حدّ الخصم "
                             f"{_fmt(_threshold(ctx))} م٢ — عُرف قابل للضبط.")
    return total


def _f_deductible_reveal_run(ctx: _Ctx) -> float:
    """The golden rule: reveals are measured ONLY for openings actually deducted."""
    return sum(o.total_reveal_run_m for o in ctx.space.openings if _is_deductible(ctx, o))


def _f_reveal_run(ctx: _Ctx) -> float:
    return sum(o.total_reveal_run_m for o in ctx.space.openings if o.kind != "niche")


def _reveal_with_sill_of(ctx: _Ctx, o) -> float:
    """Reveal run of ONE opening for the LINEAR reveal item (sill included on demand)."""
    sill = _flag(ctx, "include_sill", True)
    if o.kind in ("door", "arch"):
        return 2 * _num(o.height_m) + _num(o.width_m)
    if o.kind == "window":
        return 2 * _num(o.height_m) + _num(o.width_m) + sill * _num(o.width_m)
    if o.kind == "niche":
        return 2 * (_num(o.width_m) + _num(o.height_m))
    return 0.0


def _f_reveal_run_with_sill(ctx: _Ctx) -> float:
    """Linear reveal item: window gets its sill too when include_sill is on."""
    return sum(int(o.count or 0) * _reveal_with_sill_of(ctx, o) for o in ctx.space.openings)


def _f_deductible_reveal_run_with_sill(ctx: _Ctx) -> float:
    """The golden rule, applied to the LINEAR reveal item.

    A hole small enough to stay inside the wall item's paid area has already been paid
    for; billing its jambs again as a separate running metre is the same hole claimed
    twice. A niche is exempt by nature: it is never deducted, it is an addition, so its
    internal perimeter is always measured.
    """
    total = 0.0
    for o in ctx.space.openings:
        n = int(o.count or 0)
        if o.kind == "niche" or _is_deductible(ctx, o):
            total += n * _reveal_with_sill_of(ctx, o)
        elif o.is_hole and n > 0:
            ctx.notes.append(f"لا تُقاس كشفات {o.id} ({_fmt(o.width_m)}×{_fmt(o.height_m)} = "
                             f"{_fmt(o.area_m2)} م٢) لأنها لم تُخصم من بند الجدار "
                             f"(دون حدّ الخصم {_fmt(_threshold(ctx))} م٢) — الكشفة تتبع الخصم.")
    return total


def _f_niche_area(ctx: _Ctx) -> float:
    d = _sp(ctx, "niche_depth_m", DEFAULT_NICHE_DEPTH_M)
    return sum(int(o.count or 0) * (o.area_m2 + 2 * (_num(o.width_m) + _num(o.height_m)) * d)
               for o in ctx.space.openings if o.kind == "niche")


def _f_band_height(ctx: _Ctx) -> float:
    return _band(ctx)


def _f_banded_gross_area(ctx: _Ctx) -> float:
    return ctx.space.perimeter_m * _band(ctx)


def _f_banded_openings_deduct(ctx: _Ctx) -> float:
    """Only the part of each opening that falls INSIDE the banded height."""
    top = _band(ctx)
    total = 0.0
    for o in ctx.space.openings:
        if not _is_deductible(ctx, o):
            continue
        lo = _sill(ctx, o)
        total += _num(o.width_m) * _overlap(lo, lo + _num(o.height_m), top) * int(o.count or 0)
    return total


def _banded_opening_run(ctx: _Ctx, o, top: float, with_sill: bool) -> float:
    """Reveal run of ONE opening that actually lies inside the band [0, top].

    The band is an ELEVATION, not a height budget: a window sills at 0.90 and heads at
    2.30 has only the 0.90→top slice of each jamb inside a band that stops at 2.10, and
    its head is not tiled at all. This is the same clipping ``_f_banded_openings_deduct``
    performs on the deduction; the two must describe one geometry or the reveal pays for
    tiling that was never laid.
    """
    w, h, lo = _num(o.width_m), _num(o.height_m), _sill(ctx, o)
    run = 2.0 * _overlap(lo, lo + h, top)                 # the two jambs, clipped
    if with_sill and o.kind == "window" and lo < top:     # the sill soffit, if reached
        run += w
    if lo + h <= top:                                     # the head, only if under the line
        run += w
    return run


def _f_banded_reveal_run(ctx: _Ctx) -> float:
    return sum(int(o.count or 0) * _banded_opening_run(ctx, o, _band(ctx), True)
               for o in ctx.space.openings if _is_deductible(ctx, o))


def _f_banded_arris_run(ctx: _Ctx) -> float:
    """Arris length (two jambs + head) of the openings inside the band."""
    return sum(int(o.count or 0) * _banded_opening_run(ctx, o, _band(ctx), False)
               for o in ctx.space.openings if _is_deductible(ctx, o))


def _f_door_run(ctx: _Ctx) -> float:
    return sum(_num(o.width_m) * int(o.count or 0)
               for o in ctx.space.openings if o.kind in ("door", "arch"))


def _f_door_only_run(ctx: _Ctx) -> float:
    return sum(_num(o.width_m) * int(o.count or 0)
               for o in ctx.space.openings if o.kind == "door")


def _f_window_run(ctx: _Ctx) -> float:
    return sum(_num(o.width_m) * int(o.count or 0)
               for o in ctx.space.openings if o.kind == "window")


def _floor_reaching_windows(ctx: _Ctx) -> Optional[set[str]]:
    """Ids of the windows that reach the floor, or None when the space says «all»."""
    ids = ctx.spec.get("window_to_floor_ids")
    if isinstance(ids, (list, tuple)) and ids:
        return {str(x) for x in ids}
    return None


def _f_full_height_window_run(ctx: _Ctx) -> float:
    """French windows / balcony doors reaching the floor kill the skirting run.

    Reaching the floor is a property of ONE opening, not of the room: a salon with a
    French window and an ordinary sill-height window would otherwise lose the skirting
    that runs straight under the second one. ``window_to_floor_ids`` names the openings;
    the older space-wide ``window_to_floor`` flag still works but says so out loud.
    """
    want = _floor_reaching_windows(ctx)
    if want is not None:
        run, seen = 0.0, set()
        for o in ctx.space.openings:
            if o.kind == "window" and str(o.id) in want:
                run += _num(o.width_m) * int(o.count or 0)
                seen.add(str(o.id))
        for missing in sorted(want - seen):
            _warn(ctx, f"الشباك «{missing}» مذكور في window_to_floor_ids لكنه غير موجود بين "
                       f"شبابيك هذا الفراغ — لم يُخصم من الوزرة.")
        return run
    if not _flag(ctx, "window_to_floor", False):
        return 0.0
    n = sum(1 for o in ctx.space.openings if o.kind == "window")
    if n > 1:
        _warn(ctx, f"window_to_floor مفتاح على مستوى الفراغ فخُصمت عروض شبابيك الفراغ "
                   f"كلها ({n} شباك) من الوزرة. إن كان النازل حتى الأرضية بعضها فقط "
                   f"فحدّدها بالأسماء في window_to_floor_ids.")
    return _f_window_run(ctx)


def _f_full_height_opening_run(ctx: _Ctx) -> float:
    h = _num(ctx.space.height_m)
    return sum(_num(o.width_m) * int(o.count or 0) for o in ctx.space.openings
               if o.kind == "arch" or (o.is_hole and _num(o.height_m) >= h - 0.05))


def _f_skirting_door_deduct(ctx: _Ctx) -> float:
    """Doors and arches are deducted in FULL width — no threshold on a linear item."""
    if not _flag(ctx, "skirting_deduct_doors", True):
        ctx.notes.append("لم تُخصم فتحات الأبواب من الوزرة بناءً على "
                         "skirting_deduct_doors=false — عرف تساهلي، تأكّد من المواصفة.")
        return 0.0
    return _f_door_run(ctx)


def _f_door_jamb_returns(ctx: _Ctx) -> float:
    extra = 0.0
    if _flag(ctx, "count_door_returns", False):
        n = sum(int(o.count or 0) for o in ctx.space.openings if o.kind == "door")
        extra = n * 2 * _sp(ctx, "door_return_m", 0.05)
    return _sp(ctx, "jamb_returns_m", 0.0) + extra


def _f_wet_room_factor(ctx: _Ctx) -> float:
    """No skirting where a wall covering actually runs DOWN TO THE FLOOR.

    The old test was the room type, which is wrong twice over: a kitchen is tiled with a
    backsplash BAND above the counter — its walls never reach the floor and its skirting
    is genuinely required — while a bath whose walls were never modelled as clad has no
    covering to replace the skirting either. So the test is the measured evidence:
    ``floor_clad_area_m2`` is filled by the engine from the wall coverings that start at
    the floor (tile / stone / wood cladding — never a backsplash).
    """
    if not _flag(ctx, "skip_wet_rooms", False) or ctx.space.type not in ("bath", "kitchen"):
        return 1.0
    clad = _sp(ctx, "floor_clad_area_m2", 0.0)
    if clad > 0:
        # The whole item goes to zero here. That is the right answer, but a BOQ item
        # vanishing from the bill is never allowed to do so quietly — the surveyor must
        # see WHY the skirting he expected is not there.
        _warn(ctx, f"صُفِّر بند الوزرة كاملاً في هذا الفراغ: توجد تكسية جدارية نازلة حتى "
                   f"الأرضية مقيسة فيه ({_fmt(clad)} م٢) تحل محل الوزرة — "
                   f"skip_wet_rooms=true. إن كانت التكسية لا تغطي كل الجدران فأطفئ "
                   f"skip_wet_rooms واطرح الأطوال المكسوّة بـ wall_tiled_len_m.")
        return 0.0
    ctx.notes.append("skip_wet_rooms=true لكن لا توجد تكسية جدارية نازلة حتى الأرضية مقيسة "
                     "في هذا الفراغ (شريط ظهر المطبخ يقع فوق سطح العمل ولا يلغي الوزرة) — "
                     "قِيست الوزرة كاملة. لطرح الجدران المكسوّة استعمل wall_tiled_len_m.")
    return 1.0


def _f_reveals_on(ctx: _Ctx) -> float:
    """Master switch for every reveal/return addition in the rule."""
    if not bool(ctx.rule.get("add_reveals", False)):
        return 0.0
    if _txt(ctx, "reveal_mode", "area") in ("none", "linear"):
        ctx.notes.append("الكشفات غير مضافة هنا لأن reveal_mode = "
                         f"«{_txt(ctx, 'reveal_mode', 'area')}» (تُقاس ببند مستقل م.ط).")
        return 0.0
    return 1.0


def _f_reveal_item_on(ctx: _Ctx) -> float:
    if _flag(ctx, "reveals_included_in_wall_rate", False):
        ctx.notes.append("الكشفات مدرجة ضمن سعر متر التكسية — البند صفر منعاً للازدواج.")
        return 0.0
    return 1.0


def _f_tiled_wall_area(ctx: _Ctx) -> float:
    if not _flag(ctx, "exclude_tiled_area", True):
        return 0.0
    return _sp(ctx, "tiled_wall_area_m2", 0.0)


def _f_skirting_band_area(ctx: _Ctx) -> float:
    if not _flag(ctx, "deduct_skirting_band", False):
        return 0.0
    return ctx.space.perimeter_m * _sp(ctx, "skirting_band_m", 0.10)


def _f_bathtub_deduct(ctx: _Ctx) -> float:
    if _flag(ctx, "tile_behind_bathtub", True):
        return 0.0
    return _sp(ctx, "bathtub_run_m", 0.0) * 0.50


def _f_ceiling_void_area(ctx: _Ctx) -> float:
    lim = _ceiling_threshold(ctx)
    total = 0.0
    for a in _seq(ctx, "ceiling_openings_m2"):
        if a >= lim:
            total += a
        else:
            ctx.notes.append(f"لا تُخصم فتحة سقفية {_fmt(a)} م٢ لأنها دون حدّ الخصم "
                             f"{_fmt(lim)} م٢ (عرف قابل للضبط).")
    return total


def _f_beam_side_area(ctx: _Ctx) -> float:
    if not _flag(ctx, "include_beam_sides", False):
        return 0.0
    return _sp(ctx, "beam_run_m", 0.0) * _sp(ctx, "beam_drop_m", 0.0) * 2.0


def _f_false_ceiling_area(ctx: _Ctx) -> float:
    return _sp(ctx, "false_ceiling_area_m2", 0.0)


def _f_false_ceiling_band_area(ctx: _Ctx) -> float:
    """Above a false ceiling only a perimeter band of the slab is plastered."""
    if _sp(ctx, "false_ceiling_area_m2", 0.0) <= 0:
        return 0.0
    return ctx.space.perimeter_m * _sp(ctx, "perimeter_band_m", 0.30)


def _f_facade_gross_area(ctx: _Ctx) -> float:
    """External faces are measured slab-back to slab-back, not clear height."""
    t = _sp(ctx, "slab_thickness_m", 0.30)
    return sum(_num(w.length_m) * (_num(w.height_m) + t)
               for w in ctx.space.walls if w.is_external)


def _f_parapet_area(ctx: _Ctx) -> float:
    return (_sp(ctx, "parapet_height_m", 0.0) * ctx.space.external_wall_length
            * _sp(ctx, "parapet_faces", 2.0))


def _f_external_openings_deduct(ctx: _Ctx) -> float:
    total = 0.0
    for wall, o in ctx.space.iter_openings():
        if _external_deductible(ctx, wall, o):
            total += o.total_area_m2
    return total


def _f_external_reveal_run(ctx: _Ctx) -> float:
    return sum(o.total_reveal_run_m for wall, o in ctx.space.iter_openings()
               if _external_deductible(ctx, wall, o))


def _f_external_window_sill_run(ctx: _Ctx) -> float:
    return sum(_num(o.width_m) * int(o.count or 0) for wall, o in ctx.space.iter_openings()
               if o.kind == "window" and _external_deductible(ctx, wall, o))


def _f_stair_qty(ctx: _Ctx) -> float:
    n = _sp(ctx, "steps_count", 0.0)
    w = _sp(ctx, "step_width_m", 0.0)
    if n <= 0 or w <= 0:
        _warn(ctx, "عدد الدرجات أو عرض الدرجة غير مُدخل — الكمية صفر، "
                   "يجب تثبيتهما من المخطط. الصفر هنا معناه «غير مقيس» لا «لا يوجد عمل».")
    mode = _txt(ctx, "measure_mode", "linear")
    if mode == "developed_area":
        g = (_sp(ctx, "tread_depth_m", 0.0) + _sp(ctx, "riser_height_m", 0.0)
             + _sp(ctx, "nosing_m", 0.03))
        ctx.notes.append(f"نمط القياس: المسطح المنشور = العدد {_cnt(n)} × العرض {_fmt(w)} "
                         f"× (النائم + القائم + الأنف = {_fmt(g)} م).")
        return n * w * g
    if mode == "count":
        ctx.notes.append("نمط القياس: بالعدد (درجة كاملة).")
        return n
    ctx.notes.append(f"نمط القياس: المتر الطولي = العدد {_cnt(n)} × عرض الدرجة {_fmt(w)} م "
                     f"(شامل القائم والنائم والأنف).")
    return n * w


def _f_stair_rake_run(ctx: _Ctx) -> float:
    n = _sp(ctx, "steps_count", 0.0)
    t = _sp(ctx, "tread_depth_m", 0.0)
    r = _sp(ctx, "riser_height_m", 0.0)
    sides = _sp(ctx, "sides", 1.0)
    rake = math.hypot(t, r)
    ctx.notes.append(f"وتر الدرجة = الجذر التربيعي لـ({_fmt(t)}² + {_fmt(r)}²) = "
                     f"{rake:.3f} م لكل درجة، × {_cnt(n)} درجة × {_cnt(sides)} جانب.")
    if n <= 0:
        _warn(ctx, "عدد الدرجات غير مُدخل — الكمية من الدرج صفر، يجب تثبيته من المخطط.")
    return sides * n * rake + _sp(ctx, "landing_skirt_m", 0.0)


def _f_threshold_count(ctx: _Ctx) -> float:
    """One physical threshold per opening — charged to ONE space, never to both."""
    kinds = ctx.spec.get("apply_to_kinds") or ("door",)
    kinds = tuple(str(k) for k in kinds)
    total, detail, shared = 0.0, [], []
    proj = _sp(ctx, "side_projection_m", 0.05)
    for o in ctx.space.openings:
        if o.kind not in kinds:
            continue
        if not _owns(ctx, o):
            shared.append(str(o.id))
            continue
        total += int(o.count or 0)
        detail.append(f"{o.id}: طول القطعة = {_fmt(o.width_m)} + 2×{_fmt(proj)} = "
                      f"{_fmt(_num(o.width_m) + 2 * proj)} م × {int(o.count or 0)}")
    if detail:
        ctx.notes.append("مقاسات العتبات — " + " | ".join(detail))
    if shared:
        ctx.notes.append(f"لم تُحتسب عتبة الفتحات المشتركة ({'، '.join(shared)}) في هذا "
                         f"الفراغ لأنها محمَّلة على الفراغ المجاور — العتبة الواحدة تُحمَّل "
                         f"على فراغ واحد فقط حتى لا تُحسب مرتين في المشروع.")
    return total


def _f_piece_count(ctx: _Ctx) -> float:
    n = _sp(ctx, "count", 0.0)
    if n <= 0:
        _warn(ctx, "العدد غير مُدخل (count = 0) — البند لا يُنتج كمية، والصفر هنا معناه "
                   "«غير مقيس» لا «لا يوجد عمل».")
    return n


def _f_internal_corners(ctx: _Ctx) -> float:
    n = _sp(ctx, "internal_corners_count", 0.0)
    if n > 0:
        return n
    n = float(max(4, len(ctx.space.walls)))
    _warn(ctx, f"تقدير: عدد الأركان الداخلية غير مُدخل — استُعمل عدد جدران الفراغ "
               f"({_cnt(n)})، يلزم تثبيته من المخطط قبل التسعير.")
    return n


def _f_top_edge_on(ctx: _Ctx) -> float:
    if _flag(ctx, "include_top_edge", False):
        return 1.0
    if _band(ctx) < _num(ctx.space.height_m) - 1e-9:
        ctx.notes.append("ارتفاع التبليط أقل من ارتفاع الفراغ — احتُسب بروفيل الحدّ العلوي "
                         "تلقائياً.")
        return 1.0
    return 0.0


def _f_backsplash_band_height(ctx: _Ctx) -> float:
    h = _sp(ctx, "upper_cabinet_height_m", 1.50) - _sp(ctx, "counter_height_m", 0.90)
    return max(h, _sp(ctx, "min_band_m", 0.60))


def _f_counter_run_effective(ctx: _Ctx) -> float:
    v = _sp(ctx, "counter_run_m", 0.0)
    if v > 0:
        return v
    ratio = _sp(ctx, "counter_run_ratio", 0.45)
    est = ctx.space.perimeter_m * ratio
    _warn(ctx, f"تقدير: مسار سطح العمل = {_pct(ratio * 100)} من محيط المطبخ = "
               f"{_fmt(est)} م.ط — يلزم تثبيته من المخطط قبل التسعير.")
    return est


def _f_soffit_area(ctx: _Ctx) -> float:
    v = _sp(ctx, "soffit_area_m2", 0.0)
    if v > 0:
        return v
    _warn(ctx, "تقدير: مساحة السوفيت غير مُدخلة — استُعملت مساحة أرضية الفراغ "
               "(المسقط الأفقي)، يلزم تثبيتها من المخطط.")
    return ctx.space.floor_area


def _f_bulkhead_run(ctx: _Ctx) -> float:
    v = _sp(ctx, "bulkhead_run_m", 0.0)
    if v > 0:
        return v
    _warn(ctx, "تقدير: طول النزلة غير مُدخل — استُعمل محيط الفراغ كنزلة محيطية، "
               "يلزم تثبيته من المخطط.")
    return ctx.space.perimeter_m


# --------------------------------------------------------------------------- #
# Waste: the rule's rate PLUS what the chosen spec actually costs                #
#                                                                               #
# ``waste_pct`` is a property of the work, not only of the material: the same    #
# tile laid diagonally or in herringbone throws away several more points of the  #
# purchase order, and every rule's own waste_rationale already says so in words. #
# ``waste_modifiers`` turns those words into data — {spec_param: {value: delta}} #
# — so the surveyor who writes «herringbone» gets the waste he was promised      #
# instead of a purchase order that is six square metres short of a material that #
# cannot be re-ordered in a matching tone.                                       #
# --------------------------------------------------------------------------- #
def _waste_pct(ctx: _Ctx) -> tuple[float, list[str]]:
    """Return (waste fraction, Arabic lines explaining every uplift applied)."""
    base = _num(ctx.rule.get("waste_pct"))
    mods = ctx.rule.get("waste_modifiers") or {}
    total, lines = base, []
    if not isinstance(mods, dict):
        return base, lines
    for param in sorted(mods):
        table = mods[param]
        if not isinstance(table, dict):
            continue
        chosen = str(ctx.spec.get(param, "") or "").strip()
        delta = _num(table.get(chosen), 0.0) if chosen in table else 0.0
        if not chosen or delta == 0.0:
            continue
        total += delta
        lines.append(f"«{param} = {chosen}» يرفع الهدر بمقدار {_pct(delta * 100)} فوق "
                     f"معدّل البند {_pct(base * 100)} (نمط تركيب أكثر قصّاً وفرزاً).")
    total = _r(min(max(total, 0.0), 1.0), 6)     # 0.10 + 0.05 must stay 0.15, not 0.15000000000000002
    if lines:
        lines.append(f"نسبة الهدر المعتمدة بعد التعديل = {_pct(total * 100)}.")
    return total, lines


# name -> (Arabic label, Arabic unit, extractor)
GEOMETRY_FIELDS: dict[str, tuple[str, str, Callable[[_Ctx], float]]] = {
    # --- raw space geometry
    "floor_area": ("مساحة الأرضية", "م٢", lambda c: c.space.floor_area),
    "perimeter_m": ("محيط الفراغ", "م.ط", lambda c: c.space.perimeter_m),
    "height_m": ("ارتفاع الفراغ", "م", lambda c: _num(c.space.height_m)),
    "gross_wall_area": ("مساحة الجدران الإجمالية (المحيط × الارتفاع)", "م٢",
                        lambda c: c.space.gross_wall_area),
    "net_wall_area": ("مساحة الجدران الصافية", "م٢", lambda c: c.space.net_wall_area),
    "openings_area": ("مساحة الفتحات الكلية", "م٢", lambda c: c.space.openings_area),
    "openings_perimeter": ("محيط الفتحات المكشوف", "م.ط", lambda c: c.space.openings_perimeter),
    "wall_area_sum": ("مجموع مساحات الجدران", "م٢", lambda c: c.space.wall_area_sum),
    "external_wall_area": ("مساحة الجدران الخارجية", "م٢", lambda c: c.space.external_wall_area),
    "external_wall_length": ("طول الجدران الخارجية", "م.ط",
                             lambda c: c.space.external_wall_length),
    "wall_count": ("عدد الجدران", "", lambda c: float(len(c.space.walls))),
    "opening_count": ("عدد الفتحات", "", lambda c: float(sum(int(o.count or 0)
                                                             for o in c.space.openings))),
    # --- conventions made visible
    "deduct_threshold_m2": ("حدّ الخصم المعتمد (عُرف قابل للضبط)", "م٢", _threshold),
    "waste_pct": ("نسبة الهدر", "", lambda c: _waste_pct(c)[0]),
    "reveals_on": ("مفتاح إضافة الكشفات", "", _f_reveals_on),
    "reveal_item_on": ("مفتاح إفراد بند الكشفات", "", _f_reveal_item_on),
    "wet_room_factor": ("معامل الفراغ الرطب", "", _f_wet_room_factor),
    # --- openings, threshold-aware
    "deductible_openings_area": ("مساحة الفتحات المخصومة (≥ حدّ الخصم)", "م٢",
                                 _f_deductible_openings_area),
    "deductible_reveal_run": ("طول كشفات الفتحات المخصومة فقط", "م.ط", _f_deductible_reveal_run),
    "reveal_run": ("طول كشفات كل الفتحات", "م.ط", _f_reveal_run),
    "reveal_run_with_sill": ("طول الكشفات شاملاً جلسات الشبابيك", "م.ط", _f_reveal_run_with_sill),
    "deductible_reveal_run_with_sill": ("طول كشفات الفتحات المخصومة شاملاً الجلسات", "م.ط",
                                        _f_deductible_reveal_run_with_sill),
    "niche_area": ("مساحة الكوّات (الخلفية + الجوانب)", "م٢", _f_niche_area),
    "door_run": ("مجموع عروض الأبواب والقناطر", "م.ط", _f_door_run),
    "door_only_run": ("مجموع عروض الأبواب", "م.ط", _f_door_only_run),
    "window_run": ("مجموع عروض الشبابيك", "م.ط", _f_window_run),
    "full_height_window_run": ("عروض الشبابيك النازلة حتى الأرضية", "م.ط",
                               _f_full_height_window_run),
    "full_height_opening_run": ("عروض الفتحات كاملة الارتفاع", "م.ط", _f_full_height_opening_run),
    "skirting_door_deduct": ("خصم عروض الأبواب من الوزرة", "م.ط", _f_skirting_door_deduct),
    "door_jamb_returns": ("مرتدات جوانب الأبواب", "م.ط", _f_door_jamb_returns),
    # --- banded (partial-height) wall work
    "band_height": ("ارتفاع النطاق المعالَج", "م", _f_band_height),
    "banded_gross_area": ("المساحة الإجمالية ضمن النطاق", "م٢", _f_banded_gross_area),
    "banded_openings_deduct": ("خصم الفتحات ضمن النطاق فقط", "م٢", _f_banded_openings_deduct),
    "banded_reveal_run": ("طول كشفات الفتحات ضمن النطاق", "م.ط", _f_banded_reveal_run),
    "banded_arris_run": ("طول أحرف الفتحات (جانبان + عتب)", "م.ط", _f_banded_arris_run),
    # --- per-trade derived
    "manual_deduct_area": ("خصومات مساحية مُدخلة (≥ حدّ الخصم)", "م٢", _f_manual_deduct),
    "door_reveal_strip": ("شريط البلاط داخل فتحات الأبواب", "م٢", _f_door_reveal_strip),
    "slope_factor": ("معامل الميل", "", _f_slope_factor),
    "tiled_wall_area": ("المساحة المبلَّطة المطروحة", "م٢", _f_tiled_wall_area),
    "skirting_band_area": ("شريط الوزرة المطروح من الدهان", "م٢", _f_skirting_band_area),
    "bathtub_deduct": ("خصم ما خلف البانيو", "م٢", _f_bathtub_deduct),
    "ceiling_void_area": ("فتحات السقف المخصومة", "م٢", _f_ceiling_void_area),
    "beam_side_area": ("مساحة وجهي الجسر الساقط", "م٢", _f_beam_side_area),
    "false_ceiling_area": ("مساحة السقف المستعار المطروحة", "م٢", _f_false_ceiling_area),
    "false_ceiling_band_area": ("الشريط المحيطي فوق حدّ الجبس", "م٢", _f_false_ceiling_band_area),
    "facade_gross_area": ("مساحة الواجهة من ظهر بلاطة لظهر بلاطة", "م٢", _f_facade_gross_area),
    "parapet_area": ("مساحة الدروة (الجدار الساتر)", "م٢", _f_parapet_area),
    "external_openings_deduct": ("خصم فتحات الواجهة (≥ حدّ الخصم)", "م٢",
                                 _f_external_openings_deduct),
    "external_reveal_run": ("طول كشفات فتحات الواجهة المخصومة", "م.ط", _f_external_reveal_run),
    "external_window_sill_run": ("طول جلسات شبابيك الواجهة", "م.ط", _f_external_window_sill_run),
    "stair_qty": ("كمية الدرج حسب نمط القياس", "", _f_stair_qty),
    "stair_rake_run": ("طول وزرة الدرج على الميل", "م.ط", _f_stair_rake_run),
    "threshold_count": ("عدد العتبات", "عدد", _f_threshold_count),
    "piece_count": ("عدد القطع", "عدد", _f_piece_count),
    "internal_corners": ("عدد الأركان الداخلية", "", _f_internal_corners),
    "top_edge_on": ("مفتاح بروفيل الحدّ العلوي", "", _f_top_edge_on),
    "backsplash_band_height": ("ارتفاع شريط ظهر المطبخ", "م", _f_backsplash_band_height),
    "counter_run_effective": ("مسار سطح العمل المعتمد", "م.ط", _f_counter_run_effective),
    "soffit_area": ("مساحة السوفيت", "م٢", _f_soffit_area),
    "bulkhead_run": ("طول النزلة", "م.ط", _f_bulkhead_run),
}

# The only callables a formula may use.
ALLOWED_FUNCS: dict[str, Callable[..., float]] = {"min": min, "max": max}

_BINOPS = {
    ast.Add: (operator.add, "+"),
    ast.Sub: (operator.sub, "-"),
    ast.Mult: (operator.mul, "*"),
    ast.Div: (operator.truediv, "/"),
}


# --------------------------------------------------------------------------- #
# The safe evaluator                                                            #
# --------------------------------------------------------------------------- #
def _parse(formula: str) -> ast.Expression:
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("المعادلة فارغة. | empty formula")
    if len(formula) > MAX_FORMULA_LEN:
        raise FormulaError(f"المعادلة أطول من {MAX_FORMULA_LEN} حرفاً. | formula too long")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"المعادلة غير صالحة نحوياً: {exc}. | invalid formula syntax") from exc
    n = sum(1 for _ in ast.walk(tree))
    if n > MAX_FORMULA_NODES:
        raise FormulaError(f"المعادلة معقّدة أكثر من اللازم ({n} عقدة). | formula too complex")
    _check_nodes(tree)
    return tree


# Every AST node type a measurement formula may contain. Anything absent from this
# tuple — attribute access, subscripts, comparisons, comprehensions, lambdas — is
# refused, whatever it is called and however harmless it looks.
_ALLOWED_NODES: tuple = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.Call,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
)


def _check_nodes(tree: ast.AST) -> None:
    """Static whitelist walk, applied at PARSE time — before any value exists.

    ``_eval_node`` refuses the same shapes while evaluating, but that is one room too
    late: a rules file carrying ``floor_area.__class__`` would load, validate clean,
    and only fail on the day the take-off reached the room that used it — where it
    degrades into a zero-quantity warning line, i.e. a silent under-quote. Checking
    the shape here is what lets ``validate_rules``/``load_rules`` reject the FILE.
    """
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            _reject(node)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError("القيم الثابتة المسموحة أرقام فقط. | only numeric literals")
        elif isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                _reject(node)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS:
                _reject(node)
            if node.keywords:
                raise FormulaError("لا تُقبل الوسائط المسماة. | keyword arguments not allowed")
            if len(node.args) < 2:
                raise FormulaError("min/max تحتاج وسيطين على الأقل. | min/max need 2+ args")


def formula_names(formula: str) -> list[str]:
    """Names referenced by a formula, in source order (no duplicates).

    Source order, not walk order: the audit trail must read the way the formula
    reads, or the surveyor cannot follow it.
    """
    tree = _parse(formula)
    seen: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCS:
            pos = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
            if node.id not in seen or pos < seen[node.id]:
                seen[node.id] = pos
    return sorted(seen, key=lambda k: seen[k])


def _reject(node: ast.AST) -> None:
    kind = type(node).__name__
    raise FormulaError(
        f"عنصر غير مسموح به في المعادلة: {kind}. المسموح فقط: الأرقام والأسماء المعرّفة "
        f"و + - * / والأقواس و min/max. | disallowed node {kind}: this evaluator accepts "
        f"numbers, whitelisted names, + - * /, parentheses and min/max only — no attribute "
        f"access, no calls, no subscripts, no comprehensions."
    )


def _eval_node(node: ast.AST, env: dict[str, float]) -> tuple[float, str]:
    """Return (value, text-with-values-substituted) for one AST node."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("القيم الثابتة المسموحة أرقام فقط. | only numeric literals")
        return float(node.value), _fmt(float(node.value))

    if isinstance(node, ast.Name):
        if node.id not in env:
            raise FormulaError(
                f"اسم غير معروف في المعادلة: «{node.id}». | unknown name {node.id!r}; "
                f"known: {', '.join(sorted(env))}"
            )
        return env[node.id], _fmt(env[node.id])

    if isinstance(node, ast.BinOp):
        spec = _BINOPS.get(type(node.op))
        if spec is None:
            _reject(node.op)
        fn, sym = spec
        lv, lt = _eval_node(node.left, env)
        rv, rt = _eval_node(node.right, env)
        if sym == "/" and abs(rv) < 1e-12:
            raise FormulaError("قسمة على صفر في المعادلة. | division by zero")
        return fn(lv, rv), f"({lt} {sym} {rt})"

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            v, t = _eval_node(node.operand, env)
            return -v, f"(-{t})"
        if isinstance(node.op, ast.UAdd):
            return _eval_node(node.operand, env)
        _reject(node.op)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS:
            _reject(node)
        if node.keywords:
            raise FormulaError("لا تُقبل الوسائط المسماة. | keyword arguments not allowed")
        vals, txts = [], []
        for a in node.args:
            v, t = _eval_node(a, env)
            vals.append(v)
            txts.append(t)
        if len(vals) < 2:
            raise FormulaError("min/max تحتاج وسيطين على الأقل. | min/max need 2+ args")
        return ALLOWED_FUNCS[node.func.id](*vals), f"{node.func.id}({', '.join(txts)})"

    _reject(node)


def safe_eval(formula: str, env: dict[str, float]) -> tuple[float, str]:
    """Evaluate a restricted arithmetic formula. Returns (value, substituted text)."""
    value, text = _eval_node(_parse(formula), env)
    if not math.isfinite(value):
        raise FormulaError("نتيجة المعادلة ليست رقماً منتهياً. | non-finite result")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]           # cosmetic: drop the outermost parentheses
    return value, text


# --------------------------------------------------------------------------- #
# Loading + validation                                                          #
# --------------------------------------------------------------------------- #
_CACHE: dict[str, dict] = {}


def load_rules(path: Optional[str] = None, refresh: bool = False) -> dict[str, dict]:
    """Parse finishes.yaml, validate it, and return {rule_key: rule}."""
    p = os.path.abspath(path or RULES_PATH)
    if not refresh and p in _CACHE:
        return _CACHE[p]
    if not os.path.exists(p):
        raise RuleError(f"ملف القواعد غير موجود: {p} | rules file not found")
    with open(p, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    rules = doc.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise RuleError(f"{p}: لا يحتوي على قسم rules. | no 'rules' mapping")
    problems = validate_rules(rules)
    if problems:
        raise RuleError(f"{p}: أخطاء في ملف القواعد:\n- " + "\n- ".join(problems))
    _CACHE[p] = rules
    return rules


def validate_rules(rules: dict) -> list[str]:
    """Structural checks. Returns a list of problems (empty == the file is sound)."""
    out: list[str] = []
    seen_codes: dict[str, str] = {}
    for key, rule in rules.items():
        if not isinstance(rule, dict):
            out.append(f"{key}: ليس قاموساً. | not a mapping")
            continue
        for f in REQUIRED_FIELDS:
            if f not in rule:
                out.append(f"{key}: ينقصه الحقل «{f}». | missing field {f}")
        unit = rule.get("unit")
        if unit not in UNITS:
            out.append(f"{key}: وحدة غير معروفة «{unit}». | unknown unit")
        w = rule.get("waste_pct")
        if not isinstance(w, (int, float)) or not (0.0 <= float(w) <= 1.0):
            out.append(f"{key}: waste_pct يجب أن يكون بين 0 و1. | waste_pct out of range")
        params = rule.get("spec_params") or {}
        if not isinstance(params, dict):
            out.append(f"{key}: spec_params يجب أن يكون قاموساً. | spec_params must be a mapping")
            params = {}
        mods = rule.get("waste_modifiers")
        if mods is not None:
            if not isinstance(mods, dict):
                out.append(f"{key}: waste_modifiers يجب أن يكون قاموساً. "
                           f"| waste_modifiers must be a mapping")
            else:
                for pname, table in mods.items():
                    if pname not in params:
                        out.append(f"{key}: waste_modifiers يشير إلى بارامتر «{pname}» غير "
                                   f"معلَن في spec_params. | modifier on undeclared param")
                    if not isinstance(table, dict) or not table:
                        out.append(f"{key}: waste_modifiers[«{pname}»] يجب أن يكون قاموس "
                                   f"قيمة ← زيادة. | modifier table must be a value->delta map")
                        continue
                    for val, delta in table.items():
                        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
                            out.append(f"{key}: زيادة الهدر لـ«{pname} = {val}» ليست رقماً. "
                                       f"| modifier delta must be numeric")
                        elif not (0.0 <= _num(w) + float(delta) <= 1.0):
                            out.append(f"{key}: زيادة الهدر لـ«{pname} = {val}» تُخرج النسبة "
                                       f"عن المدى 0–1. | modified waste out of range")
        code = rule.get("code")
        if code in seen_codes and not rule.get("merged_into"):
            out.append(f"{key}: الكود «{code}» مستعمل أيضاً في {seen_codes[code]} — "
                       f"ازدواج بند. | duplicate code")
        seen_codes.setdefault(code, key)
        # A spec param shadowing a derived field would silently change a quantity.
        for name in params:
            if name in GEOMETRY_FIELDS:
                out.append(f"{key}: بارامتر المواصفة «{name}» يتعارض مع اسم هندسي محجوز. "
                           f"| spec param shadows derived field")
        try:
            names = formula_names(rule.get("formula", ""))
        except FormulaError as exc:
            out.append(f"{key}: {exc}")
            continue
        for n in names:
            if n not in GEOMETRY_FIELDS and n not in params:
                out.append(f"{key}: المعادلة تستعمل «{n}» وهو ليس حقلاً هندسياً ولا بارامتر "
                           f"مواصفة معلَناً. | formula name {n!r} is undeclared")
        sw = rule.get("unit_switch")
        if sw is not None:
            if not isinstance(sw, dict) or not {"param", "above", "unit", "multiply_by"} <= set(sw):
                out.append(f"{key}: unit_switch ناقص. | unit_switch needs param/above/unit/multiply_by")
            elif sw["unit"] not in UNITS:
                out.append(f"{key}: unit_switch يشير لوحدة غير معروفة. | unknown switch unit")
    return out


def get_rule(rule_key: str, rules: Optional[dict] = None) -> dict:
    rules = rules if rules is not None else load_rules()
    if rule_key not in rules:
        raise RuleError(f"قاعدة غير معروفة: «{rule_key}». | unknown rule key; "
                        f"known: {', '.join(sorted(rules))}")
    return rules[rule_key]


# --------------------------------------------------------------------------- #
# Environment + evaluation                                                      #
# --------------------------------------------------------------------------- #
def build_env(ctx: _Ctx, names: list[str]) -> tuple[dict[str, float], list[tuple[str, str, str, float]]]:
    """Resolve ONLY the names a formula asks for.

    Lazy on purpose: a floor rule must not explode because the space has no walls,
    and the audit trail should list the inputs that actually moved the number.
    Returns (env, [(name, label_ar, unit_ar, value), ...]).
    """
    env: dict[str, float] = {}
    trail: list[tuple[str, str, str, float]] = []
    for name in names:
        if name in GEOMETRY_FIELDS:
            label, unit, fn = GEOMETRY_FIELDS[name]
            value = _num(fn(ctx))
        elif name in ctx.spec:
            raw = ctx.spec[name]
            if raw is None:
                raise FormulaError(
                    f"البارامتر «{name}» غير محدد في المواصفة ولا قيمة افتراضية له — "
                    f"رفض الاستمرار بصفر صامت. | spec param {name!r} is null; refusing to "
                    f"substitute a silent 0."
                )
            if isinstance(raw, (list, tuple, dict)) or (isinstance(raw, str)
                                                        and not _is_numeric_str(raw)):
                raise FormulaError(
                    f"البارامتر «{name}» ليس رقماً ({raw!r}) ولا يصلح داخل معادلة. "
                    f"| spec param {name!r} is not numeric"
                )
            value = _num(raw)
            label, unit = "بارامتر المواصفة", ""
        else:
            raise FormulaError(
                f"اسم غير معروف في المعادلة: «{name}». | unknown name {name!r}"
            )
        env[name] = value
        trail.append((name, label, unit, value))
    return env, trail


def _is_numeric_str(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _resolve_unit(ctx: _Ctx, net: float) -> tuple[str, float, list[str]]:
    """Apply unit_by_mode / unit_switch. Returns (unit, net, extra breakdown lines)."""
    rule, lines = ctx.rule, []
    unit = rule.get("unit")

    mode_param = rule.get("mode_param")
    if mode_param:
        mode = _txt(ctx, mode_param, str(rule.get("spec_params", {}).get(mode_param, "")))
        by_mode = rule.get("unit_by_mode") or {}
        if mode in by_mode:
            unit = by_mode[mode]
            lines.append(f"نمط القياس «{mode}» ⇒ الوحدة {UNITS_AR.get(unit, unit)}.")

    sw = rule.get("unit_switch")
    if sw:
        v = _sp(ctx, sw["param"], _num(rule.get("spec_params", {}).get(sw["param"])))
        if v > _num(sw["above"]):
            mult = _sp(ctx, sw["multiply_by"],
                       _num(rule.get("spec_params", {}).get(sw["multiply_by"])))
            before, unit = net, sw["unit"]
            net = net * mult
            lines.append(
                f"{sw['param']} = {_fmt(v)} م يتجاوز {_fmt(_num(sw['above']))} م ⇒ يُحوَّل "
                f"البند إلى {UNITS_AR.get(unit, unit)}: {_fmt(before)} × {_fmt(mult)} = "
                f"{_fmt(net)} (عُرف قياس)."
            )
    return unit, net, lines


def evaluate(space: Space, rule_key: str, spec: Optional[dict] = None,
             rules: Optional[dict] = None) -> dict:
    """Measure one rule over one space.

    Returns a plain dict (net_qty, waste_pct, gross_qty, unit, breakdown, ...) —
    the same serialisable shape the whole app speaks.
    """
    rule = get_rule(rule_key, rules)
    merged = dict(rule.get("spec_params") or {})
    merged.update({k: v for k, v in (spec or {}).items() if v is not None})
    ctx = _Ctx(space=space, rule=rule, spec=merged)

    formula = rule["formula"]
    names = formula_names(formula)
    env, trail = build_env(ctx, names)
    value, substituted = safe_eval(formula, env)

    unit_ar = UNITS_AR.get(rule.get("unit"), rule.get("unit"))
    bd: list[str] = [
        f"البند: {rule['name_ar']} ({rule['code']}) — الفراغ «{space.name}» — "
        f"الوحدة: {unit_ar}",
        "المُدخلات:",
    ]
    for name, label, unit, val in trail:
        suffix = f" {unit}" if unit else ""
        bd.append(f"  • {label} [{name}] = {_fmt_auto(val, unit)}{suffix}")
    if rule.get("deduct_openings"):
        bd.append(f"  • حدّ الخصم المعتمد = {_fmt(_threshold(ctx))} م٢ "
                  f"(عُرف مهني قابل للضبط، ليس قاعدة مُلزِمة)")
    if ctx.notes:
        bd.append("ملاحظات الحصر:")
        bd.extend(f"  – {n}" for n in ctx.notes)
    bd.append(f"المعادلة: {' '.join(formula.split())}")
    bd.append(f"بالأرقام: {substituted} = {_fmt(value)}")

    net = value
    if net < 0:
        bd.append(f"تنبيه: الصافي سالب ({_fmt(net)}) — صُفِّر. راجع المُدخلات، فالخصومات "
                  f"تتجاوز المساحة المقاسة.")
        net = 0.0

    unit, net, switch_lines = _resolve_unit(ctx, net)
    bd.extend(switch_lines)

    waste, waste_lines = _waste_pct(ctx)
    bd.extend(waste_lines)
    net = _r(net)
    if unit == "no":
        if rule.get("waste_on_count"):
            gross = float(math.ceil(net * (1.0 + waste)))
            bd.append(f"الصافي = {_cnt(net)} عدد | هدر {_pct(waste * 100)} مع التقريب لأعلى "
                      f"⇒ الإجمالي = {_cnt(gross)} عدد")
        else:
            gross = float(round(net))
            bd.append(f"الصافي = {_cnt(net)} عدد | لا يُضاف الهدر إلى العدد المدفوع "
                      f"(نسبة {_pct(waste * 100)} تُطبَّق على مساحة الشراء فقط) ⇒ "
                      f"الإجمالي = {_cnt(gross)} عدد")
    else:
        gross = _r(net * (1.0 + waste))
        bd.append(f"الصافي = {_fmt(net)} {UNITS_AR.get(unit, unit)}")
        bd.append(f"الهدر {_pct(waste * 100)} = {_fmt(_r(gross - net))} "
                  f"{UNITS_AR.get(unit, unit)}")
        bd.append(f"الإجمالي (كمية التوريد) = {_fmt(gross)} {UNITS_AR.get(unit, unit)} — "
                  f"الدفع في المستخلص على الصافي لا على الإجمالي.")

    return {
        "rule_key": rule_key,
        "code": rule["code"],
        "name_ar": rule["name_ar"],
        "surface": rule["surface"],
        "unit": unit,
        "net_qty": net,
        "waste_pct": waste,
        "gross_qty": _r(gross),
        "breakdown": bd,
    }


def boq_line(space: Space, rule_key: str, spec: Optional[dict] = None,
             rules: Optional[dict] = None) -> BOQLine:
    """Measure one rule over one space and wrap it in the pinned BOQLine shape."""
    r = evaluate(space, rule_key, spec, rules)
    return BOQLine(
        code=r["code"], name_ar=r["name_ar"], space_id=space.id, space_name=space.name,
        surface=r["surface"], unit=r["unit"], net_qty=r["net_qty"],
        waste_pct=r["waste_pct"], gross_qty=r["gross_qty"], rule_key=r["rule_key"],
        breakdown=r["breakdown"],
    )
