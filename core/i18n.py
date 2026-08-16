# -*- coding: utf-8 -*-
"""Arabic terminology and number formatting — the single vocabulary of the deliverable.

The product's output is an Arabic bill of quantities that a contractor argues over
and an owner pays against. That makes Arabic a **deliverable language, not a UI
skin**: the word for a skirting must be the same word in the BOQ line, in the audit
trail, in the search box and in the exported glossary, or the office loses an
argument it should have won. So the wording lives here, once, keyed by the same
stable English codes the rest of the app already speaks.

Three rules shaped this module:

* **A number is never printed as a bare float.** ``0.1 + 0.2`` is
  ``0.30000000000000004`` and ``12.1 * 1`` can print as ``12.100000000000001``.
  A quantity that prints like that in front of a client destroys the credibility of
  every other number on the page. Everything goes through :func:`fmt_num`, which
  quantises with :mod:`decimal` (ROUND_HALF_UP — commercial rounding, not banker's)
  and groups thousands.
* **Formatting never computes.** ``fmt_money`` chooses digits per currency because
  Syrian pounds have no meaningful fractions and Jordanian dinars have three; that
  is a *display* decision. The stored number is untouched — rounding for payment is
  the pricing module's job and must stay auditable there.
* **Nothing here is ever guessed by an LLM.** Every string is a table lookup, every
  number is arithmetic. Pure functions, stdlib only, no side effects.

Ownership note vs :mod:`core.schema`: the schema carries minimal Arabic labels so the
contract can stand alone, and some of them are the *chapter/plural* forms
("أرضيات", "أسقف"). This module owns the **line-level** labels (singular: "أرضية",
"سقف") and the chapter titles separately in :data:`SURFACE_CHAPTERS_AR`. When the two
disagree, the report uses these — schema's copies are the fallback, not the source.

Code identifiers and comments stay English; every user-visible string is Arabic.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Callable, Iterable, Optional

__all__ = [
    # tables
    "UNITS_AR", "UNITS", "UNIT_DIGITS", "SPACE_TYPES_AR", "SPACE_TYPES",
    "SURFACES_AR", "SURFACES", "SURFACE_CHAPTERS_AR", "OPENING_KINDS_AR",
    "CURRENCIES", "GLOSSARY", "GLOSSARY_GROUPS_AR", "GLOSSARY_EN_AR",
    "GLOSSARY_AR_EN", "AR_SYNONYMS", "WARNING_CODES", "MISSING",
    "ARABIC_INDIC_DIGITS",
    # labels
    "unit_ar", "space_type_ar", "surface_ar", "surface_chapter_ar",
    "opening_kind_ar", "currency_symbol_ar", "currency_name_ar",
    # numbers
    "fmt_num", "fmt_int", "fmt_pct", "fmt_qty", "fmt_money", "fmt_rate",
    "fmt_dims", "to_arabic_digits",
    # glossary
    "ar_term", "en_term", "search_glossary", "glossary_rows", "glossary_conflicts",
    "normalize_ar",
    # warnings
    "warn_ar", "warns_ar",
]

# --------------------------------------------------------------------------- #
# Registries: stable string code -> Arabic label.                              #
# --------------------------------------------------------------------------- #

# Unit codes exactly as a Levant BOQ writes them. "م.ط" is المتر الطولي — writing a
# linear item as "م" alone is the single most common way a running metre gets read
# (and paid) as a square metre.
UNITS_AR = {
    "m2": "م²",
    "m": "م.ط",
    "no": "عدد",
    "kg": "كغ",
    "L": "لتر",
}
UNITS = tuple(UNITS_AR)

# How many decimals each unit prints with. A count with decimals is nonsense and
# invites an argument; an area without two decimals hides up to 5 m² over a flat.
UNIT_DIGITS = {"m2": 2, "m": 2, "no": 0, "kg": 2, "L": 2}

SPACE_TYPES_AR = {
    "living": "صالون/معيشة",
    "bedroom": "غرفة نوم",
    "kitchen": "مطبخ",
    "bath": "حمام",
    "corridor": "ممر",
    "balcony": "شرفة",
    "stair": "درج",
}
SPACE_TYPES = tuple(SPACE_TYPES_AR)

# Line-level surface labels (singular — they sit in a "السطح" column next to one
# measured line). Chapter headings are plural and live below.
SURFACES_AR = {
    "floor": "أرضية",
    "wall": "جدران",
    "ceiling": "سقف",
    "skirting": "وزرة",
    "facade": "واجهة",
}
SURFACES = tuple(SURFACES_AR)

# Chapter titles for the BOQ table of contents / group headers.
SURFACE_CHAPTERS_AR = {
    "floor": "أعمال الأرضيات",
    "wall": "أعمال الجدران",
    "ceiling": "أعمال الأسقف",
    "skirting": "أعمال الوزرة (النعلة)",
    "facade": "أعمال الواجهات",
}

OPENING_KINDS_AR = {
    "door": "باب",
    "window": "شباك",
    "arch": "قنطرة",
    "niche": "كوّة",
}

# Currency registry: ISO code -> display behaviour.
#   digits  = decimals SHOWN, a regional convention (SYP/LBP/IQD have no live
#             fraction; the Gulf dinars carry three fils digits).
#   place   = where the symbol sits relative to the amount.
# Display only — the stored amount is never rounded by this table.
CURRENCIES: dict[str, dict[str, Any]] = {
    "SYP": {"symbol_ar": "ل.س", "name_ar": "ليرة سورية", "digits": 0, "place": "suffix"},
    "USD": {"symbol_ar": "$", "name_ar": "دولار أمريكي", "digits": 2, "place": "prefix"},
    "SAR": {"symbol_ar": "ر.س", "name_ar": "ريال سعودي", "digits": 2, "place": "suffix"},
    "AED": {"symbol_ar": "د.إ", "name_ar": "درهم إماراتي", "digits": 2, "place": "suffix"},
    "EGP": {"symbol_ar": "ج.م", "name_ar": "جنيه مصري", "digits": 2, "place": "suffix"},
    # The rest of the Levant + the Gulf the office actually quotes in.
    "JOD": {"symbol_ar": "د.أ", "name_ar": "دينار أردني", "digits": 3, "place": "suffix"},
    "LBP": {"symbol_ar": "ل.ل", "name_ar": "ليرة لبنانية", "digits": 0, "place": "suffix"},
    "ILS": {"symbol_ar": "₪", "name_ar": "شيكل", "digits": 2, "place": "suffix"},
    "IQD": {"symbol_ar": "د.ع", "name_ar": "دينار عراقي", "digits": 0, "place": "suffix"},
    "QAR": {"symbol_ar": "ر.ق", "name_ar": "ريال قطري", "digits": 2, "place": "suffix"},
    "KWD": {"symbol_ar": "د.ك", "name_ar": "دينار كويتي", "digits": 3, "place": "suffix"},
    "OMR": {"symbol_ar": "ر.ع", "name_ar": "ريال عماني", "digits": 3, "place": "suffix"},
    "BHD": {"symbol_ar": "د.ب", "name_ar": "دينار بحريني", "digits": 3, "place": "suffix"},
    "EUR": {"symbol_ar": "€", "name_ar": "يورو", "digits": 2, "place": "prefix"},
}

# placement code -> renderer. Registry, not an if/else, so a new currency is data.
_PLACEMENT: dict[str, Callable[[str, str], str]] = {
    "prefix": lambda amount, sym: f"{sym}{amount}",
    "suffix": lambda amount, sym: f"{amount} {sym}",
}

# What a missing / non-numeric value prints as. Never "0" — a silent zero in a BOQ
# is a silent under-quote, and never blank — a blank cell reads as "nothing here".
MISSING = "—"

ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"

_MAX_DIGITS = 6
_DEC_PREC = 60          # wide enough that quantising a hyperinflated total works


# --------------------------------------------------------------------------- #
# Small pure helpers                                                            #
# --------------------------------------------------------------------------- #
def _dec(v: Any) -> Optional[Decimal]:
    """Best-effort finite Decimal, or None. Goes through ``str`` on purpose.

    ``Decimal(str(12.100000000000001))`` keeps the noise where ``Decimal(float)``
    would add more of it; quantising afterwards removes it deterministically.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        d = v
    else:
        try:
            d = Decimal(str(v).strip())
        except (InvalidOperation, ValueError, TypeError, ArithmeticError):
            return None
    return d if d.is_finite() else None


def _clamp_digits(digits: Any) -> int:
    try:
        n = int(digits)
    except (TypeError, ValueError):
        return 2
    return max(0, min(_MAX_DIGITS, n))


def _quantize(d: Decimal, digits: int) -> Decimal:
    """Commercial rounding (half away from zero), with -0.00 normalised to 0.00."""
    try:
        with localcontext() as ctx:
            ctx.prec = _DEC_PREC
            q = d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, ArithmeticError):
        return d                                    # absurd magnitude: format as-is
    return q if q != 0 else abs(q)


def _cur(currency: Any) -> dict[str, Any]:
    """Currency record, or a neutral one that shows the raw code rather than lying."""
    code = str(currency or "").strip().upper()
    return CURRENCIES.get(code) or {
        "symbol_ar": code, "name_ar": code, "digits": 2, "place": "suffix",
    }


# --------------------------------------------------------------------------- #
# Labels                                                                        #
# --------------------------------------------------------------------------- #
def unit_ar(unit: Any) -> str:
    """Arabic unit label; an unknown code is echoed, never swallowed."""
    return UNITS_AR.get(str(unit or ""), str(unit or ""))


def space_type_ar(space_type: Any) -> str:
    return SPACE_TYPES_AR.get(str(space_type or ""), str(space_type or ""))


def surface_ar(surface: Any) -> str:
    return SURFACES_AR.get(str(surface or ""), str(surface or ""))


def surface_chapter_ar(surface: Any) -> str:
    return SURFACE_CHAPTERS_AR.get(str(surface or ""), surface_ar(surface))


def opening_kind_ar(kind: Any) -> str:
    return OPENING_KINDS_AR.get(str(kind or ""), str(kind or ""))


def currency_symbol_ar(currency: Any) -> str:
    return str(_cur(currency)["symbol_ar"])


def currency_name_ar(currency: Any) -> str:
    return str(_cur(currency)["name_ar"])


# --------------------------------------------------------------------------- #
# Numbers                                                                       #
# --------------------------------------------------------------------------- #
def fmt_num(v: Any, digits: int = 2) -> str:
    """Format one number the way a BOQ prints it: 1,234.50 — never 1234.5000000001.

    Western (ASCII) digits on purpose: Levant contracts, invoices and interim
    certificates print Western digits inside Arabic text, and a number that has to
    be re-typed into Excel must survive the copy. Use :func:`to_arabic_digits` if a
    particular surface really wants Arabic-Indic numerals.

    ``None``, NaN and infinity render as :data:`MISSING` — a value we do not have is
    never allowed to look like a zero we measured.
    """
    d = _dec(v)
    if d is None:
        return MISSING
    n = _clamp_digits(digits)
    return f"{_quantize(d, n):,.{n}f}"


def fmt_int(v: Any) -> str:
    """Counts: rounded to a whole number and grouped ("3", "1,240")."""
    return fmt_num(v, 0)


def fmt_pct(v: Any, digits: int = 2, of_one: bool = True) -> str:
    """Percentage without trailing zeros: 0.10 -> "10%", 0.125 -> "12.5%".

    ``of_one`` matches the rules file, where ``waste_pct: 0.10`` means 10%. Pass
    ``of_one=False`` for a value already expressed in percent.
    """
    d = _dec(v)
    if d is None:
        return MISSING
    if of_one:
        d = d * 100
    s = fmt_num(d, digits)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return (s or "0") + "%"


def fmt_qty(v: Any, unit: Any, digits: Optional[int] = None) -> str:
    """A measured quantity with its unit: ``fmt_qty(55.2, "m2") -> "55.20 م²"``.

    Decimals follow the unit (:data:`UNIT_DIGITS`) unless overridden. The unit is
    printed even when the value is missing — "— م²" is a visible gap, a bare "—" is
    an invitation to assume zero.
    """
    code = str(unit or "")
    n = UNIT_DIGITS.get(code, 2) if digits is None else digits
    return f"{fmt_num(v, n)} {unit_ar(code)}".strip()


def fmt_money(v: Any, currency: Any, digits: Optional[int] = None) -> str:
    """An amount with the right symbol on the right side for its currency.

    ``fmt_money(1250, "USD") -> "$1,250.00"`` · ``fmt_money(1250, "SYP") -> "1,250 ل.س"``
    ``fmt_money(1250, "JOD") -> "1,250.000 د.أ"``

    An unknown currency code is printed as-is beside the amount rather than dropped:
    a total with no currency on it is a total nobody can honour.
    """
    rec = _cur(currency)
    n = rec["digits"] if digits is None else digits
    amount = fmt_num(v, n)
    if amount == MISSING:
        return f"{MISSING} {rec['symbol_ar']}".strip()
    place = _PLACEMENT.get(str(rec["place"]), _PLACEMENT["suffix"])
    return place(amount, str(rec["symbol_ar"]))


def fmt_rate(v: Any, currency: Any, unit: Any, digits: Optional[int] = None) -> str:
    """A unit rate: ``fmt_rate(45, "USD", "m2") -> "$45.00 / م²"``.

    The unit belongs to the rate as much as the currency does — "45 دولار" without
    "/ م²" is the second most expensive ambiguity in a price book, after a missing
    currency.
    """
    return f"{fmt_money(v, currency, digits)} / {unit_ar(unit)}"


def fmt_dims(*values: Any, digits: int = 2, sep: str = "×") -> str:
    """Dimensions as the trade writes them: ``fmt_dims(1.7, 0.7) -> "1.70×0.70"``."""
    return sep.join(fmt_num(v, digits) for v in values)


_AR_DIGIT_MAP = {ord(str(i)): ARABIC_INDIC_DIGITS[i] for i in range(10)}


def to_arabic_digits(text: Any) -> str:
    """Western digits -> Arabic-Indic. Presentation only.

    Never apply this to anything that will be parsed, exported to a spreadsheet or
    compared: the BOQ itself keeps Western digits.
    """
    return str(text).translate(_AR_DIGIT_MAP)


# --------------------------------------------------------------------------- #
# Glossary: the QS vocabulary of finishes.yaml, both directions.                 #
# --------------------------------------------------------------------------- #
# group code -> {stable english term: Arabic term}. Grouped so an export reads like
# a document instead of an alphabet soup, and so search can say where a hit came
# from. English keys are stable identifiers: rename one and you break a saved query.
GLOSSARY: dict[str, dict[str, str]] = {
    # -- how a quantity is measured ---------------------------------------- #
    "measure": {
        "bill_of_quantities": "جدول الكميات",
        "take_off": "الحصر",
        "measurement": "القياس",
        "net_quantity": "الكمية الصافية",
        "gross_quantity": "الكمية الإجمالية (كمية التوريد)",
        "waste_percentage": "نسبة الهدر",
        "deduction": "خصم",
        "deduction_threshold": "حدّ الخصم",
        "addition": "إضافة",
        "breakdown": "البيان الحسابي",
        "double_counting": "ازدواج القياس",
        "trade_convention": "عُرف مهني",
        "linear_metre": "متر طولي",
        "square_metre": "متر مربع",
        "cubic_metre": "متر مكعب",
        "by_count": "بالعدد",
        "plan_projection": "المسقط الأفقي",
        "developed_area": "المسطح المنشور",
        "clear_dimension": "القياس الصافي",
        "centre_line": "محور الجدار",
        "measure_mode": "نمط القياس",
    },
    # -- the commercial side ------------------------------------------------ #
    "commercial": {
        "boq_item": "بند",
        "item_code": "كود البند",
        "item_description": "وصف البند",
        "specification": "المواصفة",
        "unit_rate": "سعر الوحدة",
        "material_rate": "سعر المادة",
        "labor_rate": "سعر العمالة",
        "supply_and_install": "توريد وتركيب",
        "labor_included": "شامل العمالة",
        "price_book": "دليل الأسعار",
        "interim_certificate": "المستخلص",
        "purchase_order": "أمر الشراء",
        "variation_order": "أمر تغييري",
        "provisional_sum": "مبلغ احتياطي",
        "contractor": "المقاول",
        "employer": "المالك",
        "consultant": "المهندس المشرف",
        "handover_condition": "شرط تسليم",
    },
    # -- geometry of a space and its openings ------------------------------- #
    "geometry": {
        "space": "فراغ",
        "polygon": "مضلع الفراغ",
        "floor_area": "مساحة الأرضية",
        "perimeter": "المحيط",
        "clear_height": "الارتفاع الصافي",
        "gross_wall_area": "مساحة الجدران الإجمالية",
        "net_wall_area": "مساحة الجدران الصافية",
        "external_wall": "جدار خارجي",
        "opening": "فتحة",
        "door": "باب",
        "window": "شباك",
        "arch": "قنطرة",
        "niche": "كوّة",
        "reveal": "كشفة",
        "return": "مرتد",
        "jamb": "جنب الفتحة",
        "lintel_head": "عتب الفتحة",
        "window_sill": "جلسة الشباك",
        "door_threshold": "عتبة الباب",
        "column": "عمود",
        "downstand_beam": "جسر ساقط",
        "shaft": "منور",
        "expansion_joint": "فاصل تمدد",
        "floor_drain": "صفاية",
    },
    # -- built elements ----------------------------------------------------- #
    "elements": {
        "skirting": "وزرة",
        "raking_skirting": "وزرة درج مائلة",
        "stair_flight": "قلبة الدرج",
        "stair_tread": "نائم الدرجة",
        "stair_riser": "قائم الدرجة",
        "stair_nosing": "أنف الدرجة",
        "stair_landing": "بسطة الدرج",
        "antislip_groove": "حزوز مانعة للانزلاق",
        "parapet": "دروة (جدار ساتر)",
        "soffit": "سوفيت (باطن البروز)",
        "bulkhead": "نزلة (كشفة رأسية)",
        "cornice": "كرنيش",
        "rosette": "وردة جبس",
        "access_hatch": "باب تفتيش",
        "fitted_units": "خزائن ثابتة",
        "countertop": "سطح العمل",
        "backsplash": "ظهر المطبخ",
        "handrail": "درابزين",
    },
    # -- floor finishes ----------------------------------------------------- #
    "floors": {
        "ceramic_tile": "سيراميك",
        "porcelain_tile": "بورسلان",
        "large_format_tile": "بلاط كبير المقاس",
        "rectified_tile": "بلاط مقصوص",
        "marble": "رخام",
        "granite": "جرانيت",
        "terrazzo_tile": "بلاط موزاييك",
        "laminate_parquet": "باركيه لامينيت",
        "engineered_wood": "باركيه خشب مهندس",
        "vinyl_spc": "فينيل SPC",
        "epoxy_coating": "دهان إيبوكسي",
        "mortar_bedding": "فرشة مونة",
        "self_levelling_screed": "صبة تسوية ذاتية",
        "underlay": "طبقة عازلة سفلية",
        "transition_profile": "بروفيل انتقالي",
        "grinding_polishing": "جلي وتلميع",
        "crystallization": "كريستلة",
        "laying_pattern": "نمط الفرش",
        "herringbone": "فرش عظمة السمكة",
        "diagonal_laying": "فرش قطري",
        "expansion_gap": "فجوة تمدد",
        "vein_matching": "مطابقة العروق",
        "slip_rating": "درجة مقاومة الانزلاق",
        "falls_slope": "ميول التصريف",
        "drip_edge": "قطرة (حافة تنقيط)",
    },
    # -- plaster, putty and paint ------------------------------------------- #
    "walls_plaster": {
        "cement_plaster": "قصارة إسمنتية",
        "scratch_coat": "طرطشة",
        "dots_and_screeds": "بؤج ومرابيع",
        "backing_coat": "بطانة",
        "finish_coat": "وجه ناعم",
        "external_render": "بياض خارجي",
        "gypsum_skim": "تلبيسة جبس",
        "putty_filler": "معجونة",
        "sanding": "صنفرة",
        "primer_sealer": "سيلر (وجه أساس)",
        "emulsion_paint": "دهان إيمولسيون",
        "oil_enamel_paint": "دهان زيتي",
        "acrylic_facade_paint": "دهان أكريليك خارجي",
        "paint_coat": "وجه دهان",
        "dry_film_thickness": "سماكة الفيلم الجاف",
        "corner_bead": "زاوية معدنية (كورنر بيد)",
        "arris": "حرف",
        "flatness_tolerance": "تسامح الاستواء",
    },
    # -- waterproofing and wet rooms ---------------------------------------- #
    "waterproof": {
        "waterproofing": "عزل مائي",
        "cementitious_membrane": "عزل إسمنتي مرن",
        "bituminous_membrane": "ممبرين بيتوميني",
        "liquid_membrane": "عزل سائل",
        "upstand": "رفعة على الجدار",
        "overlap": "تراكب",
        "flood_test": "اختبار الغمر",
        "wet_room": "فراغ رطب",
        "shower_zone": "منطقة الرش",
        "bathtub": "بانيو",
        "silicone_sealant": "سيليكون مانع للعفن",
        "protection_layer": "طبقة حماية",
    },
    # -- gypsum board and false ceilings ------------------------------------ #
    "gypsum": {
        "gypsum_board": "جبس بورد",
        "false_ceiling": "سقف مستعار",
        "metal_frame": "هيكل معدني",
        "stud": "قائم معدني",
        "furring_channel": "أوميغا",
        "hanger": "معلاق",
        "joint_tape": "شريط الفواصل",
        "moisture_resistant_board": "لوح مقاوم للرطوبة",
        "fire_rated_board": "لوح مقاوم للحريق",
        "wall_lining": "تلبيس جدران",
        "partition": "قاطع",
    },
    # -- cladding and facades ----------------------------------------------- #
    "cladding": {
        "stone_cladding": "تكسية حجرية",
        "stone_tabzeh": "حجر طبزة",
        "stone_msamsam": "حجر مسمسم",
        "stone_mfazza": "حجر مفزّع",
        "course_height": "ارتفاع المدماك",
        "pointing": "ترويب الفواصل",
        "aluminium_composite": "ألوكوبوند",
        "fascia": "فاشيا",
        "wood_cladding": "تكسية خشبية",
        "wood_veneer": "قشرة خشب طبيعية",
        "slatted_panel": "شرائح مشرّحة",
        "backing_frame": "هيكل خلفي",
        "internal_cladding": "تكسية داخلية",
    },
    # -- materials and consumables ------------------------------------------ #
    "materials": {
        "tile_adhesive": "لاصق بلاط",
        "cement_mortar": "مونة إسمنتية",
        "mix_ratio": "نسبة الخلط",
        "grout": "روبة",
        "epoxy_grout": "روبة إيبوكسي",
        "joint_width": "عرض الفاصل",
        "tile_trim_profile": "بروفيل إنهاء",
        "levelling_clips": "كلبسات استواء",
        "back_buttering": "دهن ظهر البلاطة",
        "blade_kerf": "سماكة القص",
        "batch_tone": "دفعة/درجة اللون",
        "offcut": "قصاصة",
        "breakage": "كسر",
        "spare_stock": "احتياطي الصيانة",
    },
    # -- spaces, with their regional names ---------------------------------- #
    "spaces": {
        "living_room": "صالون/معيشة",
        "bedroom": "غرفة نوم",
        "kitchen": "مطبخ",
        "bathroom": "حمام",
        "corridor": "ممر",
        "balcony": "شرفة",
        "staircase": "درج",
        "terrace": "تراس",
        "entrance_lobby": "مدخل",
    },
}

GLOSSARY_GROUPS_AR = {
    "measure": "القياس والحصر",
    "commercial": "البنود والتسعير",
    "geometry": "هندسة الفراغ والفتحات",
    "elements": "العناصر الإنشائية والإنهائية",
    "floors": "الأرضيات",
    "walls_plaster": "القصارة والدهان",
    "waterproof": "العزل والمناطق الرطبة",
    "gypsum": "الجبس والأسقف المستعارة",
    "cladding": "التكسيات والواجهات",
    "materials": "المواد واللوازم",
    "spaces": "الفراغات",
}

# Flattened both ways. Registry order is preserved so an export is stable across
# runs — a glossary whose row order changes is a diff nobody can review.
GLOSSARY_EN_AR: dict[str, str] = {
    en: ar for group in GLOSSARY.values() for en, ar in group.items()
}
GLOSSARY_AR_EN: dict[str, str] = {}
for _en, _ar in GLOSSARY_EN_AR.items():
    GLOSSARY_AR_EN.setdefault(_ar, _en)

# Regional and colloquial spellings the surveyor actually types, mapped onto the
# canonical English key. Syria/Lebanon say «نعلة», Jordan/Palestine «وزرة»; Iraq
# says «كاشي» for terrazzo. Search must find the line either way.
AR_SYNONYMS: dict[str, str] = {
    "نعلة": "skirting",
    "سكيرتنج": "skirting",
    "لياسة": "cement_plaster",
    "بياض": "cement_plaster",
    "قصارة": "cement_plaster",
    "طينة": "cement_plaster",
    "جبصين": "gypsum_skim",
    "قصارة جبسية": "gypsum_skim",
    "كاشي": "terrazzo_tile",
    "بلاط سماقي": "terrazzo_tile",
    "موزاييك": "terrazzo_tile",
    "قيشاني": "ceramic_tile",
    "بلكونة": "balcony",
    "بلكون": "balcony",
    "حمّام": "bathroom",
    "بوية": "emulsion_paint",
    "بلاستيك مائي": "emulsion_paint",
    "ديكورا": "acrylic_facade_paint",
    "كورنر بيد": "corner_bead",
    "كورنيش بلدي": "cornice",
    "جبس عربي": "cornice",
    "روزيتة": "rosette",
    "بلك هيد": "bulkhead",
    "عتبة": "door_threshold",
    "درجة": "stair_tread",
    "بردورة": "transition_profile",
    "كادر": "transition_profile",
    "ممبرين": "bituminous_membrane",
    "رولات بيتومينية": "bituminous_membrane",
    "ألواح ألمنيوم مركبة": "aluminium_composite",
    "بالوعة أرضية": "floor_drain",
    "مرتد": "return",
    "كشفة": "reveal",
}


# -- Arabic-aware normalisation for search ---------------------------------- #
# Tashkeel, tatweel and the alef/ya/ta-marbuta variants are typing noise: «حمّام»
# and «حمام», «وزره» and «وزرة» are the same word to a human and must be the same
# key to the search box.
_AR_DIACRITICS = "".join(chr(c) for c in range(0x064B, 0x0653))
_FOLD_MAP = {ord(c): None for c in _AR_DIACRITICS + "ـٰٕٓٔ"}
_FOLD_MAP.update({
    ord("أ"): "ا", ord("إ"): "ا", ord("آ"): "ا", ord("ٱ"): "ا",
    ord("ى"): "ي", ord("ئ"): "ي", ord("ؤ"): "و", ord("ة"): "ه",
})
_FOLD_MAP.update({ord(ARABIC_INDIC_DIGITS[i]): str(i) for i in range(10)})


def normalize_ar(text: Any) -> str:
    """Fold an Arabic (or mixed) string to a comparable search key."""
    s = str(text or "").translate(_FOLD_MAP).lower()
    s = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in s)
    return " ".join(s.split())


def _norm_en(term: Any) -> str:
    return " ".join(str(term or "").replace("_", " ").lower().split())


_SEARCH_INDEX: list[tuple[str, str, str, str, str]] = [
    (group, en, ar, normalize_ar(ar), _norm_en(en))
    for group, terms in GLOSSARY.items()
    for en, ar in terms.items()
]
_SYNONYM_INDEX: dict[str, str] = {normalize_ar(k): v for k, v in AR_SYNONYMS.items()}


def ar_term(en: Any, default: Optional[str] = None) -> str:
    """English QS term -> Arabic. Falls back to the term itself, never to blank."""
    key = str(en or "")
    return GLOSSARY_EN_AR.get(key, key if default is None else default)


def en_term(ar: Any, default: Optional[str] = None) -> str:
    """Arabic term (or a regional variant) -> the stable English key."""
    raw = str(ar or "")
    if raw in GLOSSARY_AR_EN:
        return GLOSSARY_AR_EN[raw]
    if raw in AR_SYNONYMS:
        return AR_SYNONYMS[raw]
    n = normalize_ar(raw)
    if n in _SYNONYM_INDEX:
        return _SYNONYM_INDEX[n]
    for _g, en, _ar, nar, _nen in _SEARCH_INDEX:
        if nar == n:
            return en
    return raw if default is None else default


def search_glossary(query: Any, limit: int = 20) -> list[dict[str, str]]:
    """Search both directions. Exact hits first, then prefixes, then substrings.

    Deterministic ordering (rank, then English key) so the same query always returns
    the same list — a search that reshuffles is a search nobody trusts.
    """
    nq_ar, nq_en = normalize_ar(query), _norm_en(query)
    if not nq_ar and not nq_en:
        return []
    syn = _SYNONYM_INDEX.get(nq_ar)
    hits: list[tuple[int, str, dict[str, str]]] = []
    for group, en, ar, nar, nen in _SEARCH_INDEX:
        rank = None
        if nar == nq_ar or nen == nq_en or en == syn:
            rank = 0
        elif nar.startswith(nq_ar) or nen.startswith(nq_en):
            rank = 1
        elif (nq_ar and nq_ar in nar) or (nq_en and nq_en in nen):
            rank = 2
        if rank is not None:
            hits.append((rank, en, {"group": group, "group_ar": GLOSSARY_GROUPS_AR[group],
                                    "en": en, "ar": ar}))
    hits.sort(key=lambda h: (h[0], h[1]))
    n = len(hits) if limit is None or limit <= 0 else limit
    return [h[2] for h in hits[:n]]


def glossary_rows() -> list[dict[str, str]]:
    """The whole glossary as flat rows, in registry order — for CSV/XLSX export."""
    return [{"group": g, "group_ar": GLOSSARY_GROUPS_AR[g], "en": en, "ar": ar}
            for g, terms in GLOSSARY.items() for en, ar in terms.items()]


def glossary_conflicts() -> list[str]:
    """Arabic terms claimed by more than one English key (should be empty).

    Pure, so a test can assert on it: a two-way glossary with a collision silently
    loses one direction, and a term that resolves to the wrong key mistranslates a
    BOQ line.
    """
    seen: dict[str, str] = {}
    out: list[str] = []
    for en, ar in GLOSSARY_EN_AR.items():
        if ar in seen:
            out.append(f"«{ar}» مستعمل لـ {seen[ar]} و{en} — تعارض في المعجم.")
        else:
            seen[ar] = en
    for ar, en in AR_SYNONYMS.items():
        if en not in GLOSSARY_EN_AR:
            out.append(f"المرادف «{ar}» يشير إلى مصطلح غير موجود: {en}.")
    return out


# --------------------------------------------------------------------------- #
# Warnings: stable code -> Arabic sentence.                                     #
# --------------------------------------------------------------------------- #
# Same shape as the reference i18n: a table of lambdas over one kwargs dict. The
# codes are the contract — a caller emits ``("height_out_of_range", {...})`` and the
# wording, the units and the rounding are decided here, once. Warnings never crash
# and never hide: an unknown code still prints, carrying its data.
def _t(k: dict, key: str, default: str = MISSING) -> str:
    v = k.get(key)
    s = "" if v is None else str(v).strip()
    return s or default


def _n(k: dict, key: str, digits: int = 2) -> str:
    return fmt_num(k.get(key), digits)


def _tag(k: dict) -> str:
    """«الفراغ «صالون» (S1)» — the prefix every space-level warning carries."""
    name = _t(k, "space", "") or _t(k, "space_name", "")
    sid = _t(k, "space_id", "") or _t(k, "id", "")
    if name and sid:
        return f"الفراغ «{name}» ({sid})"
    if name:
        return f"الفراغ «{name}»"
    if sid:
        return f"الفراغ ({sid})"
    return "الفراغ"


def _wtag(k: dict) -> str:
    return f"{_tag(k)} الجدار «{_t(k, 'wall_id')}»"


def _otag(k: dict) -> str:
    return f"{_tag(k)} الفتحة «{_t(k, 'opening_id')}»"


def _known(codes: Iterable[str]) -> str:
    return "، ".join(str(c) for c in codes)


_WARN_AR: dict[str, Callable[[dict], str]] = {
    # ---------------- project level (Project.validate) ---------------------- #
    "project_no_spaces": lambda k: "المشروع لا يحتوي على أي فراغ — لا يوجد ما يُحصر.",
    "space_id_duplicate": lambda k: (
        f"مُعرّف الفراغ «{_t(k, 'space_id')}» مكرر — الازدواج في الحصر مطالبة مزدوجة "
        f"ومال يُدفع مرتين."),
    "currency_missing": lambda k: "عملة المشروع غير محددة — لا يمكن إصدار جدول أسعار.",

    # ---------------- space identity & height ------------------------------- #
    "space_type_unknown": lambda k: (
        f"{_tag(k)}: نوع الفراغ «{_t(k, 'type')}» غير معروف — "
        f"المعروف: {_known(SPACE_TYPES)}."),
    "height_invalid": lambda k: (
        f"{_tag(k)}: الارتفاع {_n(k, 'height')} م غير صالح (≤ 0) — كل بنود الجدران "
        f"والوزرة والدهان ستكون صفراً."),
    "height_out_of_range": lambda k: (
        f"{_tag(k)}: الارتفاع {_n(k, 'height')} م خارج المدى المعقول "
        f"({fmt_num(k.get('min', 2.0))}–{fmt_num(k.get('max', 6.0))} م) — تحقّق من المخطط."),

    # ---------------- floor geometry ---------------------------------------- #
    "polygon_too_few_points": lambda k: (
        f"{_tag(k)}: المضلع يحتوي على {fmt_int(k.get('points'))} نقطة فقط — "
        f"لا يصلح لحساب مساحة (يلزم ثلاث نقاط على الأقل)."),
    "polygon_near_closed": lambda k: (
        f"{_tag(k)}: المضلع شبه مغلق — فجوة {_n(k, 'gap', 3)} م بين أول نقطة وآخرها "
        f"(نقطة غير مثبّتة في المخطط)؛ سيُغلق حسابياً وقد تتغيّر المساحة."),
    "polygon_zero_area": lambda k: (
        f"{_tag(k)}: مساحة المضلع تساوي صفراً — نقاط على استقامة واحدة أو مكرّرة."),
    "area_disagreement": lambda k: (
        f"{_tag(k)}: مساحة المضلع {_n(k, 'polygon_area')} م² تختلف عن المساحة المُدخلة "
        f"{_n(k, 'given_area')} م² بأكثر من {fmt_pct(k.get('tolerance', 0.05), 0)} — "
        f"المضلع هو المعتمد لأنه المقاس من المخطط."),
    "floor_area_missing": lambda k: (
        f"{_tag(k)}: لا مضلع ولا مساحة أرضية مُدخلة — بنود الأرضيات والأسقف لا يمكن حصرها."),
    "floor_area_invalid": lambda k: (
        f"{_tag(k)}: مساحة الأرضية المُدخلة {_n(k, 'area')} م² (≤ 0) — راجع المُدخلات."),
    "perimeter_unavailable": lambda k: (
        f"{_tag(k)}: لا مضلع ولا جدران — المحيط غير معروف، وبنود الوزرة والقصارة "
        f"والدهان لا يمكن حصرها."),

    # ---------------- walls -------------------------------------------------- #
    "wall_length_invalid": lambda k: (
        f"{_wtag(k)}: الطول {_n(k, 'length')} م غير صالح (≤ 0)."),
    "wall_height_invalid": lambda k: (
        f"{_wtag(k)}: الارتفاع {_n(k, 'height')} م غير صالح (≤ 0)."),
    "openings_exceed_wall": lambda k: (
        f"{_wtag(k)}: مجموع مساحة الفتحات {_n(k, 'openings_area')} م² يتجاوز مساحة "
        f"الجدار {_n(k, 'wall_area')} م² — خصم مستحيل."),

    # ---------------- openings ---------------------------------------------- #
    "opening_kind_unknown": lambda k: (
        f"{_otag(k)}: النوع «{_t(k, 'kind')}» غير معروف — "
        f"المعروف: {_known(OPENING_KINDS_AR)}."),
    "opening_dims_invalid": lambda k: (
        f"{_otag(k)}: أبعاد غير صالحة (عرض {_n(k, 'width')} × ارتفاع {_n(k, 'height')} م)."),
    "opening_count_invalid": lambda k: (
        f"{_otag(k)}: العدد {fmt_int(k.get('count'))} ≤ 0 — لن تُحصر الفتحة ولن تُخصم."),
    "opening_wider_than_wall": lambda k: (
        f"{_otag(k)}: عرض الفتحة {_n(k, 'width')} م أكبر من طول الجدار "
        f"{_n(k, 'wall_length')} م."),
    "opening_taller_than_wall": lambda k: (
        f"{_otag(k)}: ارتفاع الفتحة {_n(k, 'height')} م أكبر من ارتفاع الجدار "
        f"{_n(k, 'wall_height')} م."),
    "opening_wall_mismatch": lambda k: (
        f"{_otag(k)}: مُسندة للجدار «{_t(k, 'wall_id')}» لكنها مدرجة تحت "
        f"«{_t(k, 'listed_wall_id')}» — تعارض في الإسناد."),
    "opening_wall_missing": lambda k: (
        f"{_otag(k)}: تشير إلى جدار «{_t(k, 'wall_id')}» غير موجود في الفراغ."),

    # ---------------- finishes ----------------------------------------------- #
    "finish_surface_unknown": lambda k: (
        f"{_tag(k)}: التشطيب «{_t(k, 'system')}» مُسند لسطح «{_t(k, 'surface')}» غير "
        f"معروف — المعروف: {_known(SURFACES)}."),

    # ---------------- reserved for the rules engine / pricing ---------------- #
    # Same vocabulary, emitted downstream of validate(); kept here so there is one
    # warning table in the product, not three.
    "rule_unknown": lambda k: (
        f"{_tag(k)}: قاعدة غير معروفة «{_t(k, 'rule_key')}» — البند لن يُحصر."),
    "rule_merged": lambda k: (
        f"البند «{_t(k, 'rule_key')}» مدمج في «{_t(k, 'into')}» منعاً للازدواج — "
        f"حُوِّل الإسناد تلقائياً ولم يُنتج بنداً ثانياً."),
    "surface_mismatch": lambda k: (
        f"{_tag(k)}: القاعدة «{_t(k, 'rule_key')}» تخص سطح "
        f"«{surface_ar(k.get('rule_surface'))}» لكنها مُسندة إلى "
        f"«{surface_ar(k.get('surface'))}» — راجع الإسناد."),
    "parent_qty_missing": lambda k: (
        f"{_tag(k)}: البند التابع «{_t(k, 'rule_key')}» لم يجد كمية بنده الأصل "
        f"(parent_net_qty) — الكمية صفر بدل أن تُقاس مرتين؛ اربطه ببند الأرضية."),
    "negative_net_zeroed": lambda k: (
        f"{_tag(k)}: الصافي المحسوب سالب ({_n(k, 'qty')}) وصُفِّر — الخصومات تتجاوز "
        f"المساحة المقاسة، راجع المُدخلات."),
    "threshold_overridden": lambda k: (
        f"{_tag(k)}: حدّ الخصم المعتمد {_n(k, 'value')} م² بدل الافتراضي "
        f"{_n(k, 'default')} م² — عُرف مهني قابل للضبط، يجب إعلانه في مقدمة جدول الكميات."),
    "wet_room_material": lambda k: (
        f"{_tag(k)}: «{_t(k, 'system')}» غير مناسب لفراغ رطب "
        f"({space_type_ar(k.get('type'))}) في العرف المحلي — راجع المواصفة."),
    "skirting_skipped_wet_room": lambda k: (
        f"{_tag(k)}: لا تُقاس وزرة هنا (فراغ مكسوّ جدارياً حتى الأرضية) — "
        f"skip_wet_rooms=true."),
    "double_count_risk": lambda k: (
        f"{_tag(k)}: البند «{_t(k, 'code')}» قد يكرّر مساحة محسوبة في "
        f"«{_t(k, 'other_code')}» — أي مساحة تظهر في بندين مطالبة مزدوجة."),
    "unit_unknown": lambda k: (
        f"وحدة غير معروفة «{_t(k, 'unit')}» في البند «{_t(k, 'code')}» — "
        f"المعروف: {_known(UNITS)}."),
    "estimate_used": lambda k: (
        f"{_tag(k)}: «{_t(k, 'field')}» غير مُدخل — استُعمل تقدير "
        f"({_t(k, 'value')})؛ يلزم تثبيته من المخطط قبل التسعير."),
}

WARNING_CODES = tuple(sorted(_WARN_AR))


def warn_ar(code: str, **kw: Any) -> str:
    """Render one stable warning code into an Arabic sentence.

    Unknown codes and missing keyword data degrade to a readable line instead of
    raising: a take-off must never be lost to a warning, and a warning must never be
    silently dropped either — see the "warnings never crash" invariant in schema.py.
    """
    key = str(code or "")
    fn = _WARN_AR.get(key)
    if fn is None:
        detail = "، ".join(f"{a}={b}" for a, b in sorted(kw.items()) if b is not None)
        return f"تحذير [{key}]" + (f" — {detail}" if detail else "")
    try:
        return fn(kw)
    except Exception:               # deliberately broad: rendering must not throw
        return f"تحذير [{key}]"


def warns_ar(items: Iterable[Any]) -> list[str]:
    """Render a sequence of warnings.

    Accepts a bare code, ``(code, kwargs)`` or ``{"code": ..., **kwargs}`` — the
    three shapes the ingestion adapters, the engine and the report already produce.
    """
    out: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            out.append(warn_ar(item))
        elif isinstance(item, dict):
            data = dict(item)
            out.append(warn_ar(str(data.pop("code", "")), **data))
        elif isinstance(item, (tuple, list)) and item:
            code = str(item[0])
            data = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            out.append(warn_ar(code, **data))
    return out
