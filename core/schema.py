# -*- coding: utf-8 -*-
"""The pinned data contract — one serialisable dict the whole app speaks.

Four ingestion adapters (DXF, PDF, image, manual entry) will all produce EXACTLY
these shapes, and every module downstream — the rules engine, the price book, the
Arabic client report — reads them without ever re-interpreting the geometry. The
field names here are therefore frozen: renaming one is a breaking change to the
whole product, not a refactor.

Three invariants are deliberate:

* **Nothing silently returns 0.** A space with neither a polygon nor walls raises
  :class:`GeometryError` instead of reporting a 0 m perimeter. A silent 0 becomes a
  silent under-quote, and an under-quote is the single worst failure mode of this
  product — the contractor eats the difference and the office eats the liability.
* **Warnings never crash.** ``validate()`` returns Arabic strings for suspicious
  geometry (a drawing is dirty far more often than it is fatal) so the surveyor can
  see and judge, rather than losing the take-off to an exception.
* **No LLM ever computes a number here.** Every value is arithmetic over these
  fields and is reproducible from the JSON alone.

Arabic is the deliverable language (space names, warnings, breakdown lines);
identifiers and comments stay English.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

__all__ = [
    "GeometryError", "Opening", "Wall", "FinishAssignment", "Space", "Project",
    "BOQLine", "PricedLine", "price_line",
    "OPENING_KINDS", "SPACE_TYPES", "SURFACES", "UNITS", "HOLE_KINDS",
    "OPENING_KINDS_AR", "SPACE_TYPES_AR", "SURFACES_AR", "UNITS_AR", "REVEAL_RUN",
]

# --------------------------------------------------------------------------- #
# Registries: stable string code -> behaviour / Arabic label.                   #
# --------------------------------------------------------------------------- #

# An opening's kind decides how it behaves in EVERY rule, so the vocabulary is
# closed. "niche" is a recess, not a hole: it removes no wall face on balance and
# is added (back + four sides), never deducted — see HOLE_KINDS below.
OPENING_KINDS_AR = {
    "door": "باب",
    "window": "شباك",
    "arch": "قنطرة",
    "niche": "كوّة",
}
OPENING_KINDS = tuple(OPENING_KINDS_AR)

# The kinds that are genuine holes in a wall (the ones a deduction may apply to).
HOLE_KINDS = ("door", "window", "arch")

SPACE_TYPES_AR = {
    "living": "صالون / معيشة",
    "bedroom": "غرفة نوم",
    "kitchen": "مطبخ",
    "bath": "حمّام",
    "corridor": "ممر",
    "balcony": "بلكونة",
    "stair": "درج",
}
SPACE_TYPES = tuple(SPACE_TYPES_AR)

SURFACES_AR = {
    "floor": "أرضيات",
    "wall": "جدران",
    "ceiling": "أسقف",
    "skirting": "وزرة (نعلة)",
    "facade": "واجهات",
}
SURFACES = tuple(SURFACES_AR)

# Unit codes as they are written in a Levant bill of quantities.
UNITS_AR = {
    "m2": "م٢",
    "m": "م.ط",
    "no": "عدد",
    "kg": "كغم",
    "L": "لتر",
}
UNITS = tuple(UNITS_AR)

# kind -> exposed running metres of ONE opening's reveal (jambs + head [+ sill]).
# A door has no sill reveal (the floor runs through); a window is measured on all
# four sides; a niche is measured on its full internal perimeter.
REVEAL_RUN = {
    "door": lambda o: 2.0 * o.height_m + o.width_m,
    "arch": lambda o: 2.0 * o.height_m + o.width_m,
    "window": lambda o: 2.0 * o.height_m + 2.0 * o.width_m,
    "niche": lambda o: 2.0 * (o.width_m + o.height_m),
}

# Geometry sanity limits — conventions, not physics; they only ever raise warnings.
MIN_ROOM_HEIGHT_M = 2.0
MAX_ROOM_HEIGHT_M = 6.0
POLYGON_CLOSE_TOL_M = 0.05      # first/last vertex closer than this = a closed ring
POLYGON_NEAR_MISS_M = 0.50      # a gap in this band is an unsnapped vertex, not an open ring
AREA_DISAGREE_PCT = 0.05        # polygon vs given floor_area_m2 mismatch worth a warning
_ND = 6                         # internal rounding: kills float noise, keeps mm


class GeometryError(ValueError):
    """A quantity could not be derived from the drawing.

    Raised INSTEAD of returning 0.0. Every call site must either fix the input or
    surface the message — never swallow it into a quantity.
    """


# --------------------------------------------------------------------------- #
# Pure helpers                                                                  #
# --------------------------------------------------------------------------- #
def _r(x: float, nd: int = _ND) -> float:
    return round(float(x), nd)


def _f(v: Any, default: float = 0.0) -> float:
    """Best-effort float; never raises, never silently swallows a real number."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _pt(p: Any) -> tuple[float, float]:
    x, y = p[0], p[1]
    return (_f(x), _f(y))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _ring(polygon) -> list[tuple[float, float]]:
    """Normalise a polygon to an OPEN ring (no duplicated closing vertex)."""
    pts = [_pt(p) for p in (polygon or [])]
    while len(pts) >= 2 and _dist(pts[0], pts[-1]) <= POLYGON_CLOSE_TOL_M:
        pts = pts[:-1]
    return pts


def _shoelace_area(polygon) -> float:
    """Signed-area (shoelace) magnitude of a simple polygon, in m²."""
    pts = _ring(polygon)
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _polygon_perimeter(polygon) -> float:
    pts = _ring(polygon)
    if len(pts) < 3:
        return 0.0
    return sum(_dist(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))


def _polygon_gap(polygon) -> float:
    """Distance between first and last vertex as GIVEN (0.0 if <3 points)."""
    pts = [_pt(p) for p in (polygon or [])]
    return _dist(pts[0], pts[-1]) if len(pts) >= 3 else 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# The contract                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class Opening:
    """A hole (or recess) in a wall. ``count`` means identical repeats."""

    id: str
    kind: str                       # "door" | "window" | "arch" | "niche"
    width_m: float
    height_m: float
    count: int = 1
    wall_id: Optional[str] = None

    # -- derived ------------------------------------------------------------ #
    @property
    def area_m2(self) -> float:
        """Area of ONE opening — the value a deduction threshold is tested against."""
        return _r(_f(self.width_m) * _f(self.height_m))

    @property
    def total_area_m2(self) -> float:
        return _r(self.area_m2 * int(self.count or 0))

    @property
    def is_hole(self) -> bool:
        return self.kind in HOLE_KINDS

    @property
    def reveal_run_m(self) -> float:
        """Exposed reveal length of ONE opening (jambs + head [+ sill])."""
        fn = REVEAL_RUN.get(self.kind)
        return _r(fn(self)) if fn else 0.0

    @property
    def total_reveal_run_m(self) -> float:
        return _r(self.reveal_run_m * int(self.count or 0))

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "width_m": _f(self.width_m),
                "height_m": _f(self.height_m), "count": int(self.count or 0),
                "wall_id": self.wall_id}

    @classmethod
    def from_dict(cls, d: dict) -> "Opening":
        return cls(id=str(d.get("id", "")), kind=str(d.get("kind", "")),
                   width_m=_f(d.get("width_m")), height_m=_f(d.get("height_m")),
                   count=int(_f(d.get("count", 1), 1)), wall_id=d.get("wall_id"))


@dataclass
class Wall:
    id: str
    length_m: float
    height_m: float
    is_external: bool = False
    openings: list[Opening] = field(default_factory=list)

    @property
    def gross_area_m2(self) -> float:
        return _r(_f(self.length_m) * _f(self.height_m))

    @property
    def openings_area_m2(self) -> float:
        """Holes only — a niche is a recess and is never subtracted here."""
        return _r(sum(o.total_area_m2 for o in self.openings if o.is_hole))

    @property
    def net_area_m2(self) -> float:
        return _r(self.gross_area_m2 - self.openings_area_m2)

    def to_dict(self) -> dict:
        return {"id": self.id, "length_m": _f(self.length_m),
                "height_m": _f(self.height_m), "is_external": bool(self.is_external),
                "openings": [o.to_dict() for o in self.openings]}

    @classmethod
    def from_dict(cls, d: dict) -> "Wall":
        return cls(id=str(d.get("id", "")), length_m=_f(d.get("length_m")),
                   height_m=_f(d.get("height_m")),
                   is_external=bool(d.get("is_external", False)),
                   openings=[Opening.from_dict(o) for o in (d.get("openings") or [])])


@dataclass
class FinishAssignment:
    """Which finish system is applied to which surface of a space.

    ``system`` is a rule key in ``core/rules/finishes.yaml``; ``spec`` carries the
    per-project parameters (tile size, coats, deduction threshold overrides, ...).
    """

    surface: str                    # "floor" | "wall" | "ceiling" | "skirting" | "facade"
    system: str                     # rule key, e.g. "ceramic_tile_floor"
    spec: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"surface": self.surface, "system": self.system, "spec": dict(self.spec or {})}

    @classmethod
    def from_dict(cls, d: dict) -> "FinishAssignment":
        return cls(surface=str(d.get("surface", "")), system=str(d.get("system", "")),
                   spec=dict(d.get("spec") or {}))


@dataclass
class Space:
    """One room / zone. Everything the rules engine measures hangs off this."""

    id: str
    name: str                       # Arabic, e.g. "صالون"
    type: str                       # SPACE_TYPES
    height_m: float
    walls: list[Wall] = field(default_factory=list)
    floor_area_m2: Optional[float] = None       # None => derive from polygon
    polygon: Optional[list[tuple[float, float]]] = None
    finishes: list[FinishAssignment] = field(default_factory=list)

    # -- derived geometry --------------------------------------------------- #
    @property
    def openings(self) -> list[Opening]:
        return [o for w in self.walls for o in w.openings]

    def iter_openings(self) -> Iterator[tuple[Wall, Opening]]:
        for w in self.walls:
            for o in w.openings:
                yield w, o

    @property
    def floor_area(self) -> float:
        """m² — shoelace of the polygon when present, else the given area.

        The polygon wins on purpose: it is the measured drawing, the number is the
        typed assumption.
        """
        if self.polygon and len(_ring(self.polygon)) >= 3:
            return _r(_shoelace_area(self.polygon))
        if self.floor_area_m2 is not None:
            return _r(_f(self.floor_area_m2))
        raise GeometryError(
            f"الفراغ «{self.name}» ({self.id}): لا يوجد مضلع ولا مساحة أرضية مُدخلة — "
            f"تعذّر حساب مساحة الأرضية. | space {self.id!r}: floor area unavailable "
            f"(no polygon, no floor_area_m2); refusing to return 0."
        )

    @property
    def perimeter_m(self) -> float:
        """m — polygon perimeter when present, else the sum of wall lengths."""
        if self.polygon and len(_ring(self.polygon)) >= 3:
            return _r(_polygon_perimeter(self.polygon))
        total = sum(_f(w.length_m) for w in self.walls)
        if total > 0:
            return _r(total)
        raise GeometryError(
            f"الفراغ «{self.name}» ({self.id}): لا يوجد مضلع ولا جدران بأطوال موجبة — "
            f"تعذّر حساب المحيط (الوزرة والقصارة تعتمد عليه). | space {self.id!r}: "
            f"perimeter unavailable (no polygon, no positive wall lengths); a silent 0 "
            f"here becomes a silent under-quote."
        )

    @property
    def gross_wall_area(self) -> float:
        """m² — perimeter × clear height (the BOQ basis, not the sum of wall faces)."""
        return _r(self.perimeter_m * _f(self.height_m))

    @property
    def openings_area(self) -> float:
        """m² — total area of the HOLES (doors, windows, arches). Niches excluded."""
        return _r(sum(o.total_area_m2 for o in self.openings if o.is_hole))

    @property
    def niches_area(self) -> float:
        """m² — face area of the recesses only (added by plaster, never deducted)."""
        return _r(sum(o.total_area_m2 for o in self.openings if o.kind == "niche"))

    @property
    def net_wall_area(self) -> float:
        return _r(self.gross_wall_area - self.openings_area)

    @property
    def openings_perimeter(self) -> float:
        """m — total exposed reveal run of every opening (jambs + head [+ sill])."""
        return _r(sum(o.total_reveal_run_m for o in self.openings))

    @property
    def wall_area_sum(self) -> float:
        """m² — Σ(length × height) per wall; differs from gross when heights differ."""
        return _r(sum(w.gross_area_m2 for w in self.walls))

    @property
    def external_wall_length(self) -> float:
        return _r(sum(_f(w.length_m) for w in self.walls if w.is_external))

    @property
    def external_wall_area(self) -> float:
        return _r(sum(w.gross_area_m2 for w in self.walls if w.is_external))

    # -- validation --------------------------------------------------------- #
    def validate(self) -> list[str]:
        """Arabic warnings for suspicious geometry. Warns, never crashes."""
        w_out: list[str] = []
        tag = f"الفراغ «{self.name}» ({self.id})"

        if self.type not in SPACE_TYPES:
            w_out.append(f"{tag}: نوع الفراغ «{self.type}» غير معروف — "
                         f"المعروف: {'، '.join(SPACE_TYPES)}.")

        h = _f(self.height_m)
        if h <= 0:
            w_out.append(f"{tag}: الارتفاع {h:.2f} م غير صالح (≤ 0) — كل بنود الجدران ستكون صفراً.")
        elif not (MIN_ROOM_HEIGHT_M <= h <= MAX_ROOM_HEIGHT_M):
            w_out.append(f"{tag}: الارتفاع {h:.2f} م خارج المدى المعقول "
                         f"({MIN_ROOM_HEIGHT_M:.2f}–{MAX_ROOM_HEIGHT_M:.2f} م) — تحقّق من المخطط.")

        # -- floor geometry
        ring = _ring(self.polygon)
        if self.polygon and len(ring) < 3:
            w_out.append(f"{tag}: المضلع يحتوي على {len(ring)} نقطة فقط — لا يصلح لحساب مساحة.")
        elif ring:
            # A polygon may legitimately be given as an OPEN ring, so a large gap is
            # normal. A SMALL non-zero gap is the real defect: an unsnapped vertex
            # from the DXF/trace, which quietly moves the area.
            gap = _polygon_gap(self.polygon)
            if POLYGON_CLOSE_TOL_M < gap <= POLYGON_NEAR_MISS_M:
                w_out.append(f"{tag}: المضلع شبه مغلق — فجوة {gap:.3f} م بين أول نقطة "
                             f"وآخرها (نقطة غير مثبّتة في المخطط)؛ سيُغلق حسابياً وقد "
                             f"تتغيّر المساحة.")
            poly_a = _shoelace_area(self.polygon)
            if poly_a <= 0:
                w_out.append(f"{tag}: مساحة المضلع تساوي صفراً — نقاط على استقامة واحدة أو مكرّرة.")
            if self.floor_area_m2 is not None and poly_a > 0:
                given = _f(self.floor_area_m2)
                if given > 0 and abs(poly_a - given) / given > AREA_DISAGREE_PCT:
                    w_out.append(f"{tag}: مساحة المضلع {poly_a:.2f} م٢ تختلف عن المساحة "
                                 f"المُدخلة {given:.2f} م٢ بأكثر من "
                                 f"{AREA_DISAGREE_PCT * 100:.0f}% — المضلع هو المعتمد.")
        elif self.floor_area_m2 is None:
            w_out.append(f"{tag}: لا مضلع ولا مساحة أرضية — بنود الأرضيات والأسقف لا يمكن حصرها.")
        elif _f(self.floor_area_m2) <= 0:
            w_out.append(f"{tag}: مساحة الأرضية المُدخلة {_f(self.floor_area_m2):.2f} م٢ (≤ 0).")

        if not ring and not self.walls:
            w_out.append(f"{tag}: لا مضلع ولا جدران — المحيط غير معروف، "
                         f"وبنود الوزرة والقصارة والدهان لا يمكن حصرها.")

        # -- walls & openings
        wall_ids = {w.id for w in self.walls}
        for wl in self.walls:
            wtag = f"{tag} الجدار «{wl.id}»"
            if _f(wl.length_m) <= 0:
                w_out.append(f"{wtag}: الطول {_f(wl.length_m):.2f} م غير صالح (≤ 0).")
            if _f(wl.height_m) <= 0:
                w_out.append(f"{wtag}: الارتفاع {_f(wl.height_m):.2f} م غير صالح (≤ 0).")
            if wl.gross_area_m2 > 0 and wl.openings_area_m2 > wl.gross_area_m2:
                w_out.append(f"{wtag}: مجموع مساحة الفتحات {wl.openings_area_m2:.2f} م٢ "
                             f"يتجاوز مساحة الجدار {wl.gross_area_m2:.2f} م٢ — خصم مستحيل.")
            for o in wl.openings:
                otag = f"{tag} الفتحة «{o.id}»"
                if o.kind not in OPENING_KINDS:
                    w_out.append(f"{otag}: النوع «{o.kind}» غير معروف — "
                                 f"المعروف: {'، '.join(OPENING_KINDS)}.")
                if _f(o.width_m) <= 0 or _f(o.height_m) <= 0:
                    w_out.append(f"{otag}: أبعاد غير صالحة "
                                 f"(عرض {_f(o.width_m):.2f} × ارتفاع {_f(o.height_m):.2f} م).")
                if int(o.count or 0) <= 0:
                    w_out.append(f"{otag}: العدد {int(o.count or 0)} ≤ 0 — لن تُحصر الفتحة.")
                if _f(o.width_m) > _f(wl.length_m) > 0:
                    w_out.append(f"{otag}: عرض الفتحة {_f(o.width_m):.2f} م أكبر من طول "
                                 f"الجدار {_f(wl.length_m):.2f} م.")
                if _f(o.height_m) > _f(wl.height_m) > 0:
                    w_out.append(f"{otag}: ارتفاع الفتحة {_f(o.height_m):.2f} م أكبر من "
                                 f"ارتفاع الجدار {_f(wl.height_m):.2f} م.")
                if o.wall_id and o.wall_id != wl.id:
                    w_out.append(f"{otag}: مُسندة للجدار «{o.wall_id}» لكنها مدرجة تحت "
                                 f"«{wl.id}» — تعارض في الإسناد.")
                if o.wall_id and o.wall_id not in wall_ids:
                    w_out.append(f"{otag}: تشير إلى جدار «{o.wall_id}» غير موجود في الفراغ.")

        # -- finishes
        for fa in self.finishes:
            if fa.surface not in SURFACES:
                w_out.append(f"{tag}: التشطيب «{fa.system}» مُسند لسطح «{fa.surface}» "
                             f"غير معروف — المعروف: {'، '.join(SURFACES)}.")
        return w_out

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "type": self.type,
            "height_m": _f(self.height_m),
            "walls": [w.to_dict() for w in self.walls],
            "floor_area_m2": (None if self.floor_area_m2 is None else _f(self.floor_area_m2)),
            "polygon": (None if self.polygon is None
                        else [[_f(p[0]), _f(p[1])] for p in self.polygon]),
            "finishes": [f.to_dict() for f in self.finishes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Space":
        poly = d.get("polygon")
        return cls(
            id=str(d.get("id", "")), name=str(d.get("name", "")),
            type=str(d.get("type", "")), height_m=_f(d.get("height_m")),
            walls=[Wall.from_dict(w) for w in (d.get("walls") or [])],
            floor_area_m2=(None if d.get("floor_area_m2") is None
                           else _f(d.get("floor_area_m2"))),
            polygon=(None if poly is None else [_pt(p) for p in poly]),
            finishes=[FinishAssignment.from_dict(f) for f in (d.get("finishes") or [])],
        )


@dataclass
class Project:
    id: str
    name: str
    client_name: str
    region: str
    currency: str
    spaces: list[Space] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)

    def validate(self) -> list[str]:
        """Arabic warnings for the whole project. Warns, never crashes."""
        out: list[str] = []
        if not self.spaces:
            out.append("المشروع لا يحتوي على أي فراغ — لا يوجد ما يُحصر.")
        seen: set[str] = set()
        for s in self.spaces:
            if s.id in seen:
                out.append(f"مُعرّف الفراغ «{s.id}» مكرر — الازدواج في الحصر خطر مباشر.")
            seen.add(s.id)
            out.extend(s.validate())
        if not str(self.currency or "").strip():
            out.append("عملة المشروع غير محددة — لا يمكن إصدار جدول أسعار.")
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "client_name": self.client_name,
            "region": self.region, "currency": self.currency,
            "created_at": self.created_at,
            "spaces": [s.to_dict() for s in self.spaces],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        return cls(
            id=str(d.get("id", "")), name=str(d.get("name", "")),
            client_name=str(d.get("client_name", "")), region=str(d.get("region", "")),
            currency=str(d.get("currency", "")),
            spaces=[Space.from_dict(s) for s in (d.get("spaces") or [])],
            created_at=str(d.get("created_at") or _now_iso()),
        )

    # JSON is the wire format every ingestion adapter writes and reads.
    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Project":
        return cls.from_dict(json.loads(text))


# --------------------------------------------------------------------------- #
# Engine output                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class BOQLine:
    """One measured line of the bill of quantities.

    ``breakdown`` is a MANDATORY Arabic audit trail — one entry per arithmetic
    step, with the substituted values. A quantity nobody can trace is a quantity
    nobody can defend in an interim certificate.
    """

    code: str                       # e.g. "TIL-CER-FLR"
    name_ar: str
    space_id: str
    space_name: str
    surface: str
    unit: str                       # "m2" | "m" | "no" | "kg" | "L"
    net_qty: float
    waste_pct: float
    gross_qty: float
    rule_key: str
    breakdown: list[str] = field(default_factory=list)

    @property
    def unit_ar(self) -> str:
        return UNITS_AR.get(self.unit, self.unit)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name_ar": self.name_ar, "space_id": self.space_id,
            "space_name": self.space_name, "surface": self.surface, "unit": self.unit,
            "net_qty": _f(self.net_qty), "waste_pct": _f(self.waste_pct),
            "gross_qty": _f(self.gross_qty), "rule_key": self.rule_key,
            "breakdown": list(self.breakdown),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BOQLine":
        return cls(
            code=str(d.get("code", "")), name_ar=str(d.get("name_ar", "")),
            space_id=str(d.get("space_id", "")), space_name=str(d.get("space_name", "")),
            surface=str(d.get("surface", "")), unit=str(d.get("unit", "")),
            net_qty=_f(d.get("net_qty")), waste_pct=_f(d.get("waste_pct")),
            gross_qty=_f(d.get("gross_qty")), rule_key=str(d.get("rule_key", "")),
            breakdown=list(d.get("breakdown") or []),
        )


@dataclass
class PricedLine:
    """A BOQ line with money on it, and the price-book id+version that put it there."""

    line: BOQLine
    material_rate: float = 0.0
    labor_rate: float = 0.0
    material_total: float = 0.0
    labor_total: float = 0.0
    total: float = 0.0
    currency: str = ""
    rate_source: str = ""           # price-book id + version, e.g. "levant-2026Q1@1.3"
    qty_basis: str = "net"          # "net" (payment) | "gross" (procurement)

    def to_dict(self) -> dict:
        return {
            "line": self.line.to_dict(),
            "material_rate": _f(self.material_rate), "labor_rate": _f(self.labor_rate),
            "material_total": _f(self.material_total), "labor_total": _f(self.labor_total),
            "total": _f(self.total), "currency": self.currency,
            "rate_source": self.rate_source, "qty_basis": self.qty_basis,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PricedLine":
        return cls(
            line=BOQLine.from_dict(d.get("line") or {}),
            material_rate=_f(d.get("material_rate")), labor_rate=_f(d.get("labor_rate")),
            material_total=_f(d.get("material_total")), labor_total=_f(d.get("labor_total")),
            total=_f(d.get("total")), currency=str(d.get("currency", "")),
            rate_source=str(d.get("rate_source", "")),
            qty_basis=str(d.get("qty_basis") or "net"),
        )


def price_line(line: BOQLine, material_rate: float, labor_rate: float,
               currency: str, rate_source: str, qty_basis: str = "net") -> PricedLine:
    """Deterministic pricing of one BOQ line.

    ``qty_basis="net"`` is the contractual default: the interim certificate pays the
    measured net quantity, and the waste allowance is expected to sit INSIDE the
    material rate. ``qty_basis="gross"`` prices the procurement quantity instead —
    use it for a purchase order, never for a payment certificate, or the waste gets
    paid twice. Labour is always on the net quantity: nobody lays the offcuts.
    """
    qty = _f(line.gross_qty) if qty_basis == "gross" else _f(line.net_qty)
    mat = round(qty * _f(material_rate), 2)
    lab = round(_f(line.net_qty) * _f(labor_rate), 2)
    return PricedLine(
        line=line, material_rate=_f(material_rate), labor_rate=_f(labor_rate),
        material_total=mat, labor_total=lab, total=round(mat + lab, 2),
        currency=currency, rate_source=rate_source, qty_basis=qty_basis,
    )
