# -*- coding: utf-8 -*-
"""The client quote — one Arabic HTML file the office can defend line by line.

Why this module exists: the take-off is only half the product. The other half is
the piece of paper the client actually holds, argues with, and signs. That paper
decides whether the office gets paid, so it is built here under the same rules as
the engine that feeds it:

* **Nothing is invented.** Every number printed comes from a :class:`PricedLine`
  or from the ``totals`` mapping the pricing module produced. This module does
  arithmetic in exactly two places, and both are *checks*, not estimates: it
  re-adds the lines and re-adds the totals, and it says so loudly in Arabic when
  its own sum disagrees with the one it was handed. A quote whose totals do not
  add up must never leave the office looking tidy.
* **Placeholder rates can never look sendable.** If the price book says it is a
  placeholder — or if the rate source cannot be identified at all — the document
  stamps itself as a draft, watermarks every page, and disables the signature
  block. An under-priced quote signed off placeholder rates is money the
  contractor eats and liability the office eats.
* **The audit trail is the product.** ``detailed=True`` prints each line's Arabic
  ``breakdown`` under its own row: the geometry, the deduction convention, the
  waste, then the money. That block is what ends the argument at the site meeting
  — the client can follow the number back to the drawing without trusting anyone.
* **Deterministic and self-contained.** Same inputs, byte-identical output: the
  issue date defaults to ``project.created_at``, never to "today". One file, inline
  CSS, no fonts, no images, no scripts, no network — it survives being emailed,
  opened offline, and printed to A4 five years later during a dispute.

Arabic is the deliverable language (headings, terms, audit trail); identifiers and
comments stay English. Nothing here calls an LLM. Dependencies: Python stdlib.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Sequence

from .schema import (
    SPACE_TYPES_AR,
    SURFACES_AR,
    UNITS_AR,
    BOQLine,
    GeometryError,
    PricedLine,
    Project,
    Space,
)

__all__ = [
    "ProposalOptions", "render_html",
    "trade_of", "trade_label", "group_lines",
    "TRADES_AR", "TRADE_OTHER", "BOQ_COLUMNS", "TOTALS_ROWS", "TOTALS_ALIASES",
    "TOTALS_STEP_CODES",
    "TERMS_AR", "DEFAULT_TERMS", "CHECKS", "MONTHS_AR",
    "DEFAULT_VALIDITY_DAYS", "MONEY_TOL",
]

# Money tolerance when this module re-adds what it was handed. Five components
# each rounded to 2 decimals cannot drift further than this; anything beyond it is
# a real disagreement, not rounding.
MONEY_TOL = 0.05
DEFAULT_VALIDITY_DAYS = 30
_ND_MONEY = 2


# --------------------------------------------------------------------------- #
# Registries: stable string code -> Arabic label / behaviour                    #
# --------------------------------------------------------------------------- #

# BOQ code prefix -> trade. The INSERTION ORDER is the order the trades print in,
# and it is the order the work is executed on site (waterproofing before screed
# before plaster before tiling, paint near the end). Changing this dict changes the
# document's structure and nothing else.
TRADES_AR = {
    "WPR": "أعمال العزل المائي",
    "SCR": "أعمال الفرشات والصبّات",
    "PLS": "أعمال القصارة (اللياسة)",
    "GYP": "أعمال الجبس والديكور",
    "TIL": "أعمال البلاط والسيراميك والبورسلان",
    "STN": "أعمال الحجر والرخام",
    "PRQ": "أعمال الباركيه والأرضيات الخشبية",
    "FIN": "أعمال الأرضيات الراتنجية (إيبوكسي)",
    "GRT": "أعمال الروبة (التجليط)",
    "SEL": "أعمال المواد المانعة للتسرّب",
    "WOD": "أعمال الخشب والكسوة الخشبية",
    "ACP": "أعمال الألمنيوم المركّب (كومبوزيت)",
    "PNT": "أعمال الدهان",
    "ACC": "إكسسوارات وملحقات التشطيب",
}
TRADE_OTHER = "بنود أخرى"

# Levant month names — the client reads دمشق/عمّان/بيروت dates, not "July".
MONTHS_AR = {
    1: "كانون الثاني", 2: "شباط", 3: "آذار", 4: "نيسان", 5: "أيار", 6: "حزيران",
    7: "تموز", 8: "آب", 9: "أيلول", 10: "تشرين الأول", 11: "تشرين الثاني",
    12: "كانون الأول",
}

# The totals block, top to bottom. ``kind`` decides the sign in the re-addition
# check: "base" starts it, "add" increases it, "sub" decreases it, "grand" is the
# figure we verify against. ``required`` rows print even when missing (as "—" plus
# a loud warning) because a silently absent VAT line is a silently wrong price.
TOTALS_ROWS = (
    {"key": "subtotal",    "label_ar": "مجموع بنود الأعمال",        "rate_key": None,             "kind": "base",  "required": True},
    {"key": "discount",    "label_ar": "خصم",                        "rate_key": "discount_pct",   "kind": "sub",   "required": False},
    {"key": "overhead",    "label_ar": "مصاريف إدارية وعامة",       "rate_key": "overhead_pct",   "kind": "add",   "required": True},
    {"key": "profit",      "label_ar": "ربح المقاول",                "rate_key": "profit_pct",     "kind": "add",   "required": True},
    {"key": "contingency", "label_ar": "احتياطي طوارئ",              "rate_key": "contingency_pct","kind": "add",   "required": False},
    {"key": "vat",         "label_ar": "ضريبة القيمة المضافة",      "rate_key": "vat_pct",        "kind": "add",   "required": True},
    {"key": "grand_total", "label_ar": "الإجمالي النهائي",           "rate_key": None,             "kind": "grand", "required": True},
)

# The pricing module owns the field names; this document must not force them.
# Aliases are read in order — the FIRST present key wins.
TOTALS_ALIASES = {
    "subtotal":        ("subtotal", "sub_total", "lines_total", "net_total", "works_total"),
    "discount":        ("discount", "discount_amount"),
    "overhead":        ("overhead", "overhead_amount", "overheads"),
    "profit":          ("profit", "profit_amount"),
    "contingency":     ("contingency", "contingency_amount"),
    "vat":             ("vat", "vat_amount", "tax", "tax_amount"),
    "grand_total":     ("grand_total", "grand", "total", "final_total"),
    "discount_pct":    ("discount_pct", "discount_rate"),
    "overhead_pct":    ("overhead_pct", "overhead_rate"),
    "profit_pct":      ("profit_pct", "profit_rate"),
    "contingency_pct": ("contingency_pct", "contingency_rate"),
    "vat_pct":         ("vat_pct", "vat_rate", "tax_pct", "tax_rate"),
}

# The same figures, as the pricing module actually carries them: a CASCADE of steps
# (``Quote.steps``, each a ``QuoteStep`` with ``.code`` and ``.amount``) plus the
# percentages on ``Quote.terms``. A flat ``quote.overhead`` attribute does not exist,
# so reading only the flat names made every required row resolve to None, collapsed
# the re-addition to the bare subtotal, and printed «الإجمالي لا يساوي مكوّناته» over
# a quote whose arithmetic was perfect. This document must bend to the pricing shape,
# not the other way round.
# proposal row key -> the step code pricing uses for it
TOTALS_STEP_CODES = {
    "discount": "discount", "overhead": "overhead", "profit": "profit",
    "contingency": "contingency", "vat": "tax",
}


# --------------------------------------------------------------------------- #
# Small pure helpers                                                            #
# --------------------------------------------------------------------------- #
def _num(v: Any, default: float = 0.0) -> float:
    """Best-effort float; never raises, never turns a real number into 0 silently."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _esc(v: Any) -> str:
    """EVERY interpolated value goes through here. No exceptions, ever."""
    return html.escape("" if v is None else str(v), quote=True)


def _money(x: Any) -> str:
    """Western digits with thousands separators — how a Levant quote prints money."""
    return f"{_num(x):,.{_ND_MONEY}f}"


def _fmt(x: Any) -> str:
    return f"{_num(x):.2f}"


def _cnt(x: Any) -> str:
    return str(int(round(_num(x))))


def _pct(x: Any) -> str:
    """Percentage POINTS in, trailing zeros out: 10 -> «10%», 12.50 -> «12.5%»."""
    s = f"{_num(x):.2f}".rstrip("0").rstrip(".")
    return (s or "0") + "%"


def _qty(x: Any, unit: str = "") -> str:
    """Counts read as integers; every other unit as a two-decimal quantity."""
    if unit == "no" and abs(_num(x) - round(_num(x))) < 1e-9:
        return _cnt(x)
    return _fmt(x)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off a mapping OR off an object attribute — the pricing module
    may hand us a dict today and a dataclass tomorrow; both are the same contract."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _first(obj: Any, keys: Sequence[str], default: Any = None) -> Any:
    for k in keys:
        v = _get(obj, k, None)
        if v is not None:
            return v
    return default


def _get_step(totals: Any, code: str) -> Any:
    """One step of the commercial cascade, matched on its stable ``code``."""
    steps = _get(totals, "steps", None)
    if not isinstance(steps, (list, tuple)):
        return None
    return next((s for s in steps if str(_get(s, "code", "")) == code), None)


def _totals_get(totals: Any, key: str) -> Optional[float]:
    """Alias-tolerant read of one totals figure. ``None`` means ABSENT, not zero.

    Three shapes are accepted, in that order: a flat attribute or key
    (``totals["overhead"]``), a step inside the cascade (``Quote.steps``), and the rate
    on ``Quote.terms``. The last two matter because a rate of zero produces NO step at
    all — the figure is then a computed 0, not a missing number, and printing it as «—»
    would raise a critical banner over a quote that is arithmetically perfect.
    """
    v = _first(totals, TOTALS_ALIASES.get(key, (key,)), None)
    if v is not None:
        return _num(v)

    is_rate = key.endswith("_pct")
    code = TOTALS_STEP_CODES.get(key[:-4] if is_rate else key)
    if code is None:
        return None

    step = _get_step(totals, code)
    if step is not None:
        return _num(_get(step, "pct" if is_rate else "amount", None))

    terms = _get(totals, "terms", None)
    rate = None if terms is None else _get(terms, f"{code}_pct", None)
    if rate is None:
        return None                      # genuinely absent — say so, do not invent a 0
    return _num(rate) if is_rate else 0.0


def _boq(pl: Any) -> BOQLine:
    """The measured line inside a priced line (tolerates a bare BOQLine)."""
    inner = _get(pl, "line", None)
    return inner if inner is not None else pl


def _total_of(pl: Any) -> float:
    return _num(_get(pl, "total", 0.0))


def _as_date(v: Any) -> Optional[date]:
    """ISO-ish string / date / datetime -> date. Never raises."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    if len(s) >= 10:
        try:
            return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
        except (ValueError, TypeError):
            return None
    return None


def _date_ar(d: Optional[date], with_iso: bool = True) -> str:
    """«26 تموز 2026 (2026-07-26)» — readable to the client, exact for the file."""
    if d is None:
        return "—"
    head = f"{d.day} {MONTHS_AR.get(d.month, '')} {d.year}"
    return f"{head} ({d.isoformat()})" if with_iso else head


def _geom(space: Space, prop: str) -> Optional[float]:
    """A derived geometry property, or None when the drawing cannot yield it.

    :class:`GeometryError` is the engine refusing to return a silent 0; in a
    client document that refusal prints as «—» and as a warning, never as a number.
    """
    try:
        return _num(getattr(space, prop))
    except (GeometryError, ValueError, TypeError, ZeroDivisionError):
        return None


def _g(v: Optional[float], unit: str = "") -> str:
    return "—" if v is None else _qty(v, unit)


def trade_of(code: str) -> str:
    """Stable trade key from a BOQ code: ``"TIL-CER-FLR"`` -> ``"TIL"``."""
    head = str(code or "").split("-", 1)[0].strip().upper()
    return head or "?"


def trade_label(key: str) -> str:
    """Arabic trade heading. Unknown prefixes keep their code so nothing hides."""
    if key in TRADES_AR:
        return TRADES_AR[key]
    return TRADE_OTHER if key in ("", "?") else f"{TRADE_OTHER} ({key})"


def _trade_rank(key: str) -> int:
    order = list(TRADES_AR)
    return order.index(key) if key in order else len(order)


def group_lines(project: Project, priced_lines: Sequence[PricedLine]) -> list[dict]:
    """Group priced lines by space (project order) then by trade (execution order).

    Returns one dict per space::

        {"space_id", "space_name", "space": Space|None, "orphan": bool,
         "total": float, "trades": [{"key", "label_ar", "lines": [...], "total"}]}

    Spaces with no priced lines are KEPT with an empty list: a room that quietly
    vanished from the quote is a room the contractor executes for free.
    """
    by_space: dict[str, list[Any]] = {}
    for pl in priced_lines:
        by_space.setdefault(str(_boq(pl).space_id), []).append(pl)

    ordered_ids: list[str] = [s.id for s in project.spaces]
    for pl in priced_lines:                      # orphans keep first-seen order
        sid = str(_boq(pl).space_id)
        if sid not in ordered_ids:
            ordered_ids.append(sid)

    spaces_by_id = {s.id: s for s in project.spaces}
    out: list[dict] = []
    for sid in ordered_ids:
        lines = by_space.get(sid, [])
        space = spaces_by_id.get(sid)
        name = space.name if space is not None else (
            _boq(lines[0]).space_name if lines else sid)
        buckets: dict[str, list[Any]] = {}
        for pl in lines:
            buckets.setdefault(trade_of(_boq(pl).code), []).append(pl)
        trades = [
            {"key": k, "label_ar": trade_label(k), "lines": v,
             "total": round(sum(_total_of(p) for p in v), _ND_MONEY)}
            for k, v in sorted(buckets.items(), key=lambda kv: (_trade_rank(kv[0]), kv[0]))
        ]
        out.append({
            "space_id": sid, "space_name": name, "space": space,
            "orphan": space is None,
            "total": round(sum(_total_of(p) for p in lines), _ND_MONEY),
            "trades": trades,
        })
    return out


# --------------------------------------------------------------------------- #
# Options                                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class ProposalOptions:
    """Everything about the DOCUMENT; nothing about the numbers.

    The numbers arrive already computed in ``priced_lines`` and ``totals``. This
    object only decides what the paper says around them — and, critically, whether
    the paper is allowed to look sendable (``price_book``/``draft``).
    """

    detailed: bool = False                      # print the Arabic audit trail per line
    quote_version: str = "1"
    quote_no: str = ""                          # defaults to project.id
    issue_date: Any = None                      # defaults to project.created_at (deterministic)
    validity_days: int = DEFAULT_VALIDITY_DAYS
    office_name: str = ""
    office_contact: str = ""
    doc_title: str = "عرض سعر — أعمال تشطيبات"
    price_book: Any = None                      # dict/obj: id, version, fx_rate, fx_base, is_placeholder
    draft: bool = False                         # force the draft stamp regardless of the price book
    terms: Optional[Sequence[str]] = None       # TERMS_AR codes, or literal Arabic sentences
    notes_ar: Sequence[str] = field(default_factory=tuple)
    show_geometry: bool = True                  # the geometry caption under each space
    lang_note: bool = True

    @classmethod
    def from_any(cls, v: Any) -> "ProposalOptions":
        if v is None:
            return cls()
        if isinstance(v, cls):
            return v
        if isinstance(v, dict):
            known = {f for f in cls.__dataclass_fields__}      # noqa: SLF001 - dataclass API
            return cls(**{k: val for k, val in v.items() if k in known})
        return cls(**{k: _get(v, k) for k in cls.__dataclass_fields__
                      if _get(v, k, None) is not None})


# --------------------------------------------------------------------------- #
# Price book provenance + the placeholder gate                                  #
# --------------------------------------------------------------------------- #
_PLACEHOLDER_MARKERS = ("placeholder", "draft", "dummy", "sample", "tbd",
                        "مبدئي", "مؤقت", "تجريبي")


def _price_book(opts: ProposalOptions, totals: Any, priced_lines: Sequence[Any]) -> dict:
    """Identify the price book behind these numbers. Reproducibility lives here."""
    pb = opts.price_book if opts.price_book is not None else _get(totals, "price_book")
    sources = sorted({str(_get(pl, "rate_source", "") or "") for pl in priced_lines} - {""})
    pb_id = _first(pb, ("id", "book_id", "price_book_id"), "") or _first(
        totals, ("price_book_id",), "") or ""
    pb_ver = _first(pb, ("version", "ver"), "") or _first(totals, ("price_book_version",), "") or ""
    label = f"{pb_id}@{pb_ver}" if (pb_id and pb_ver) else (pb_id or pb_ver or "")
    if not label:
        label = sources[0] if len(sources) == 1 else ""
    return {
        "id": str(pb_id), "version": str(pb_ver),
        "label": str(label) or "غير معروف",
        "fx_rate": _first(pb, ("fx_rate", "fx"), None) if pb is not None else _first(totals, ("fx_rate",), None),
        "fx_base": _first(pb, ("fx_base", "base_currency"), "") if pb is not None else _first(totals, ("fx_base",), ""),
        "fx_date": _first(pb, ("fx_date", "fx_as_of"), "") if pb is not None else "",
        "is_placeholder": _first(pb, ("is_placeholder", "placeholder"), None),
        "sources": tuple(sources),
        "raw": pb,
    }


def _draft_reasons(opts: ProposalOptions, meta: dict, priced_lines: Sequence[Any]) -> list[str]:
    """Arabic reasons this document must NOT be sent. Empty list = sendable."""
    out: list[str] = []
    if opts.draft:
        out.append("صدر هذا المستند بوضع «مسودة» بطلب صريح من مُعِدّه.")
    if bool(meta.get("is_placeholder")):
        out.append("قاعدة الأسعار مُعلَّمة كأسعار مبدئية (placeholder) — أرقامها للتجربة "
                   "الداخلية فقط ولا تمثّل عرضاً ملزماً.")
    blob = " ".join([meta.get("label", ""), meta.get("id", ""), meta.get("version", ""),
                     " ".join(meta.get("sources", ()))]).lower()
    if any(m in blob for m in _PLACEHOLDER_MARKERS):
        out.append("مصدر الأسعار يحمل علامة مبدئية/تجريبية في اسمه أو إصداره.")
    if meta.get("label") == "غير معروف" and not meta.get("sources"):
        out.append("تعذّر تحديد قاعدة الأسعار وإصدارها — عرض لا يمكن إعادة إنتاجه أو "
                   "مراجعته لاحقاً لا يجوز إرساله.")
    if priced_lines and all(
            _num(_get(pl, "material_rate", 0)) == 0 and _num(_get(pl, "labor_rate", 0)) == 0
            for pl in priced_lines):
        out.append("كل أسعار الوحدات أصفار — لم تُسعَّر البنود فعلياً بعد.")
    return out


# --------------------------------------------------------------------------- #
# Deterministic self-checks: code -> (severity, fn(ctx) -> Arabic sentence)      #
# --------------------------------------------------------------------------- #
def _chk_no_lines(c: dict) -> str:
    if not c["lines"]:
        return "لا يوجد أي بند مسعّر في هذا العرض — لا يصلح للإرسال."
    return ""


def _chk_subtotal(c: dict) -> str:
    given = c["figures"].get("subtotal")
    if given is None or not c["lines"]:
        return ""
    if abs(given - c["lines_sum"]) > MONEY_TOL:
        return (f"مجموع بنود الجدول كما أعدنا جمعه = {_money(c['lines_sum'])} "
                f"{c['currency']}، بينما المجموع الوارد في كتلة المجاميع = "
                f"{_money(given)} {c['currency']} — فرق {_money(abs(given - c['lines_sum']))}. "
                f"لا يجوز إرسال العرض قبل تسوية هذا الفرق.")
    return ""


def _chk_missing_totals(c: dict) -> str:
    missing = [r["label_ar"] for r in TOTALS_ROWS
               if r["required"] and c["figures"].get(r["key"]) is None]
    if missing:
        return ("بنود المجاميع التالية غير موجودة في المدخلات وطُبعت «—»: "
                + "، ".join(missing) + " — أرقام ناقصة في كتلة المجاميع تعني سعراً خاطئاً.")
    return ""


def _chk_grand(c: dict) -> str:
    grand = c["figures"].get("grand_total")
    if grand is None:
        return ""
    expected = c["recomputed_grand"]
    if expected is None:
        return ""
    if abs(grand - expected) > MONEY_TOL:
        return (f"الإجمالي النهائي الوارد {_money(grand)} {c['currency']} لا يساوي حاصل "
                f"جمع مكوّناته {_money(expected)} {c['currency']} — فرق "
                f"{_money(abs(grand - expected))}. الرقم المطبوع هو الوارد كما هو، "
                f"ولم نصحّحه: التصحيح قرار المكتب لا قرار المستند.")
    return ""


def _chk_currency(c: dict) -> str:
    cur = {str(_get(pl, "currency", "") or "") for pl in c["lines"]} - {""}
    if len(cur) > 1:
        return ("بنود العرض مسعّرة بأكثر من عملة (" + "، ".join(sorted(_esc(x) for x in cur))
                + ") — الجمع بين عملتين في جدول واحد لا معنى له.")
    proj_cur = str(c["project"].currency or "").strip()
    if cur and proj_cur and next(iter(cur)) != proj_cur:
        return (f"عملة البنود «{next(iter(cur))}» تخالف عملة المشروع «{proj_cur}».")
    return ""


def _chk_duplicate_exact(c: dict) -> str:
    seen: dict[tuple, int] = {}
    for pl in c["lines"]:
        b = _boq(pl)
        k = (b.space_id, b.rule_key, b.unit, round(_num(b.net_qty), 3))
        seen[k] = seen.get(k, 0) + 1
    dups = [k for k, n in seen.items() if n > 1]
    if dups:
        names = "، ".join(f"«{k[1]}» في الفراغ «{k[0]}»" for k in dups[:5])
        return (f"بنود مكرّرة حرفياً (نفس الفراغ ونفس القاعدة ونفس الكمية): {names}"
                + (" وغيرها" if len(dups) > 5 else "")
                + " — مطالبة مزدوجة بنفس المساحة.")
    return ""


def _chk_duplicate_rule(c: dict) -> str:
    seen: dict[tuple, int] = {}
    for pl in c["lines"]:
        b = _boq(pl)
        seen[(b.space_id, b.rule_key)] = seen.get((b.space_id, b.rule_key), 0) + 1
    dups = [k for k, n in seen.items() if n > 1]
    if dups:
        names = "، ".join(f"«{k[1]}» في الفراغ «{k[0]}»" for k in dups[:5])
        return (f"نفس قاعدة الحصر تتكرّر بكميات مختلفة: {names}"
                + (" وغيرها" if len(dups) > 5 else "")
                + " — قد يكون تقسيماً مقصوداً، وقد يكون ازدواجاً في الحصر. راجعه.")
    return ""


def _chk_orphan_space(c: dict) -> str:
    orphans = [g["space_id"] for g in c["groups"] if g["orphan"]]
    if orphans:
        return ("بنود مسعّرة تشير إلى فراغات غير موجودة في المشروع: "
                + "، ".join(f"«{o}»" for o in orphans[:6])
                + " — مصدر الكميات غير قابل للمراجعة.")
    return ""


def _chk_unpriced_space(c: dict) -> str:
    empty = [g["space_name"] for g in c["groups"] if not g["trades"]]
    if empty:
        return ("فراغات بلا أي بند مسعّر: " + "، ".join(f"«{n}»" for n in empty[:8])
                + " — إمّا أنها خارج نطاق العمل صراحةً، أو أنها بنود منسيّة.")
    return ""


def _chk_missing_breakdown(c: dict) -> str:
    n = sum(1 for pl in c["lines"] if not (_boq(pl).breakdown or []))
    if n:
        return (f"{n} بنداً بلا بيان حسابي (breakdown) — كمية لا يمكن تتبّعها لا يمكن "
                f"الدفاع عنها في مستخلص.")
    return ""


def _chk_zero_priced(c: dict) -> str:
    n = sum(1 for pl in c["lines"]
            if _num(_get(pl, "material_rate", 0)) == 0 and _num(_get(pl, "labor_rate", 0)) == 0)
    if n and n != len(c["lines"]):
        return f"{n} بنداً بسعر وحدة صفر — إمّا بند مشمول في غيره، أو سعر لم يُدخل بعد."
    return ""


def _chk_zero_qty(c: dict) -> str:
    """Critical, not advisory: a printed zero is the product's worst failure mode.

    A zero quantity means «not measured», never «no work» — the room whose height was
    entered as 0, the stair with no steps_count, the parent a follower never found. The
    client cannot tell those apart from a deliberately empty line, so the document must
    say it in EVERY mode, not only when the office asks for the detailed print.
    """
    bad = [_boq(pl) for pl in c["lines"] if _num(_boq(pl).net_qty) <= 0]
    if not bad:
        return ""
    names = "، ".join(f"«{b.code}» في الفراغ «{b.space_name or b.space_id}»"
                      for b in bad[:6])
    return (f"{len(bad)} بنداً بكمية صافية تساوي صفراً أو أقل ({names}"
            + (" وغيرها" if len(bad) > 6 else "")
            + ") — راجع البيان الحسابي لكل منها: إن كان الصفر مقصوداً (قطعة محمَّلة على "
              "الفراغ المجاور، وزرة حلّت محلها تكسية) فاحذف البند من العرض، وإن كان معناه "
              "«غير مقيس» فأصلح الحصر. لا يجوز أن يظهر صفر في عرض للزبون على أي حال.")


def _chk_rate_source(c: dict) -> str:
    src = c["meta"]["sources"]
    if len(src) > 1:
        return ("البنود مسعّرة من أكثر من مصدر أسعار: " + "، ".join(f"«{s}»" for s in src)
                + " — العرض لا يعود قابلاً لإعادة الإنتاج من إصدار واحد.")
    return ""


def _chk_gross_basis(c: dict) -> str:
    n = sum(1 for pl in c["lines"] if str(_get(pl, "qty_basis", "net")) == "gross")
    if n:
        return (f"{n} بنداً مسعّراً على كمية التوريد (gross) لا على الكمية الصافية — "
                f"هذا أساس أمر شراء، وفي مستخلص الدفع يعني دفع الهدر مرتين.")
    return ""


def _chk_line_arithmetic(c: dict) -> str:
    bad = []
    for pl in c["lines"]:
        b = _boq(pl)
        basis = str(_get(pl, "qty_basis", "net"))
        qty = _num(b.gross_qty) if basis == "gross" else _num(b.net_qty)
        mat = round(qty * _num(_get(pl, "material_rate", 0)), _ND_MONEY)
        lab = round(_num(b.net_qty) * _num(_get(pl, "labor_rate", 0)), _ND_MONEY)
        if (abs(mat - _num(_get(pl, "material_total", 0))) > 0.01
                or abs(lab - _num(_get(pl, "labor_total", 0))) > 0.01
                or abs(_total_of(pl) - (_num(_get(pl, "material_total", 0))
                                        + _num(_get(pl, "labor_total", 0)))) > 0.01):
            bad.append(b.code)
    if bad:
        return ("بنود لا تتطابق فيها القيمة المطبوعة مع حاصل الكمية × السعر: "
                + "، ".join(f"«{x}»" for x in bad[:6])
                + (" وغيرها" if len(bad) > 6 else "") + ".")
    return ""


def _chk_geometry(c: dict) -> str:
    try:
        warns = c["project"].validate()
    except (ValueError, TypeError, AttributeError):
        return ""
    if warns:
        return (f"المخطط يحمل {len(warns)} ملاحظة هندسية (تُعرض أدناه في وضع البيان "
                f"المفصّل) — الكميات مبنية على مخطط فيه ما يستحق المراجعة.")
    return ""


# code -> (severity, check). "critical" prints in EVERY mode: a client must never
# receive a quote whose own arithmetic disagrees with itself. "advisory" prints in
# detailed mode only — it is the office's internal read of its own document.
CHECKS: dict[str, tuple[str, Callable[[dict], str]]] = {
    "no_lines":          ("critical", _chk_no_lines),
    "subtotal_mismatch": ("critical", _chk_subtotal),
    "missing_totals":    ("critical", _chk_missing_totals),
    "grand_mismatch":    ("critical", _chk_grand),
    "currency_mixed":    ("critical", _chk_currency),
    "duplicate_exact":   ("critical", _chk_duplicate_exact),
    "orphan_space":      ("critical", _chk_orphan_space),
    "line_arithmetic":   ("critical", _chk_line_arithmetic),
    "zero_qty":          ("critical", _chk_zero_qty),
    "duplicate_rule":    ("advisory", _chk_duplicate_rule),
    "unpriced_space":    ("advisory", _chk_unpriced_space),
    "missing_breakdown": ("advisory", _chk_missing_breakdown),
    "zero_priced":       ("advisory", _chk_zero_priced),
    "rate_source_mixed": ("advisory", _chk_rate_source),
    "gross_basis":       ("advisory", _chk_gross_basis),
    "geometry":          ("advisory", _chk_geometry),
}


def _run_checks(ctx: dict, severity: str) -> list[str]:
    out = []
    for _code, (sev, fn) in CHECKS.items():
        if sev != severity:
            continue
        try:
            msg = fn(ctx)
        except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError):
            msg = ""
        if msg:
            out.append(msg)
    return out


# --------------------------------------------------------------------------- #
# Terms & conditions: stable code -> Arabic sentence (lambda table)             #
# --------------------------------------------------------------------------- #
# A term that has no data to stand on returns "" and is dropped — the document
# never prints a condition it cannot fill in.
TERMS_AR: dict[str, Callable[[dict], str]] = {
    "currency": lambda c: (
        f"جميع الأسعار الواردة في هذا العرض بعملة {c['currency']}، وهي أسعار وحدة "
        f"شاملة التوريد والتركيب والعمالة والأدوات ما لم يُنص على خلاف ذلك في وصف البند."),
    "validity": lambda c: (
        "هذه النسخة مسودة داخلية: لا مدة سريان لها ولا تُعدّ عرضاً ملزماً بأي حال."
        if c["is_draft"] else
        f"هذا العرض سارٍ لمدة {c['validity_days']} يوماً من تاريخ إصداره "
        f"({c['issue_short']})، وينتهي سريانه بتاريخ {c['expiry_short']}. بعد هذا "
        f"التاريخ تُعاد مراجعة الأسعار قبل أي التزام."),
    "quantities_provisional": lambda c: (
        "الكميات الواردة تقديرية مبنية على المخططات والقياسات المعتمدة وقت إعداد العرض، "
        "وتُسوّى نهائياً على القياس الفعلي في الموقع وفق نفس أعراف القياس المبيّنة في "
        "البيان الحسابي لكل بند."),
    "measurement_conventions": lambda c: (
        "أعراف القياس المعتمدة (حدّ خصم الفتحات، إضافة الكشفات والمرتدات، نسب الهدر، "
        "أساس قياس كل سطح) مثبتة لكل بند في بيانه الحسابي؛ أي عرف قياس مخالف يجب "
        "الاتفاق عليه كتابةً قبل بدء التنفيذ."),
    "waste_included": lambda c: (
        "نسب الهدر مضمّنة داخل سعر المادة، والكميات المستحقة في المستخلصات هي الكميات "
        "الصافية المقاسة — لا تُحتسب كمية التوريد مرتين."),
    "excluded": lambda c: (
        "لا يشمل هذا العرض: أعمال الكهرباء والصحية والتكييف، أعمال الهدم وترحيل "
        "المخلفات، الأعمال المعدنية والألمنيوم والزجاج، الأثاث الثابت، ورسوم التراخيص "
        "والاعتمادات — إلا ما ورد منها بنداً صريحاً في الجدول."),
    "substrate": lambda c: (
        "الأسعار مبنية على تسلّم أسطح جاهزة ومستوية ضمن السماحات المتعارف عليها؛ أي "
        "معالجة إضافية للأسطح (تسوية، ترميم، تقوية) تُسعّر بأمر تغييري منفصل."),
    "samples": lambda c: (
        "تُعتمد الألوان والمقاسات وأنماط التركيب بعيّنات موقّعة قبل التوريد، وفروق التدرّج "
        "اللوني بين دفعات الإنتاج ضمن حدود المصنع لا تُعدّ عيباً."),
    "variations": lambda c: (
        "أي تعديل في المواصفة أو المقاس أو نمط التركيب أو نطاق العمل بعد اعتماد هذا "
        "العرض يُعامل كأمر تغييري ويُسعّر بشكل منفصل قبل تنفيذه."),
    "site_readiness": lambda c: (
        "الأسعار مبنية على تسليم الموقع جاهزاً (كهرباء وماء ومسارات مرور آمنة وتخزين "
        "مغلق)؛ التعطيل الخارج عن إرادة المنفّذ يعدّل البرنامج الزمني دون غرامة."),
    "payment": lambda c: (
        "الدفعات وفق ما يُتفق عليه في العقد، وتُصرف المستخلصات على أساس الكميات المنفذة "
        "فعلياً والمقاسة في الموقع، لا على كميات هذا العرض."),
    "fx": lambda c: (
        f"سعر الصرف المعتمد في احتساب هذا العرض هو {c['fx_rate']}"
        + (f" مقابل {c['fx_base']}" if c.get("fx_base") else "")
        + (f" بتاريخ {c['fx_date']}" if c.get("fx_date") else "")
        + "؛ أي تغيّر جوهري في سعر الصرف قبل التوريد يستوجب إعادة تسعير المواد المستوردة."
    ) if c.get("fx_rate") else "",
    "price_book": lambda c: (
        f"الأسعار مستخرجة من قاعدة أسعار المكتب «{c['price_book_label']}»، ورقم إصدارها "
        f"مثبت في تذييل هذا المستند ليكون العرض قابلاً لإعادة الإنتاج والمراجعة بند بند."),
    "taxes": lambda c: (
        "الضرائب والرسوم الحكومية تُحتسب حسب النافذ وقت الفوترة؛ أي تغيير تشريعي بعد "
        "تاريخ العرض يُعدَّل على الفاتورة."),
    # Silent in a draft: a document with a disabled signature block must not carry
    # a clause explaining how signing it works.
    "acceptance": lambda c: ("" if c["is_draft"] else
        "يُعتبر هذا العرض مقبولاً بتوقيع الطرفين على خانة التوقيع أدناه، ويصبح عندها "
        "جزءاً لا يتجزأ من العقد بكامل جداوله وبياناته الحسابية."),
}

DEFAULT_TERMS = (
    "currency", "validity", "quantities_provisional", "measurement_conventions",
    "waste_included", "excluded", "substrate", "samples", "variations",
    "site_readiness", "payment", "fx", "price_book", "taxes", "acceptance",
)


def _terms(opts: ProposalOptions, ctx: dict) -> list[str]:
    """Render the term codes. An unknown code is treated as a literal Arabic
    sentence, so the office can add a one-off condition without touching Python."""
    out: list[str] = []
    for code in (opts.terms if opts.terms is not None else DEFAULT_TERMS):
        fn = TERMS_AR.get(str(code))
        text = fn(ctx) if fn else str(code)
        if text:
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# BOQ table columns: stable key -> header + cell renderer                       #
# --------------------------------------------------------------------------- #
def _cell_item(pl: Any, ctx: dict) -> str:
    b = _boq(pl)
    return (f'<span class="code">{_esc(b.code)}</span> '
            f'<span class="iname">{_esc(b.name_ar)}</span>')


BOQ_COLUMNS = (
    {"key": "idx",      "label_ar": "م",              "cls": "num idx", "detailed": False,
     "render": lambda pl, ctx: _esc(ctx["idx"])},
    {"key": "item",     "label_ar": "البند",           "cls": "item",    "detailed": False,
     "render": _cell_item},
    {"key": "surface",  "label_ar": "السطح",           "cls": "",        "detailed": True,
     "render": lambda pl, ctx: _esc(SURFACES_AR.get(_boq(pl).surface, _boq(pl).surface))},
    {"key": "unit",     "label_ar": "الوحدة",          "cls": "unit",    "detailed": False,
     "render": lambda pl, ctx: _esc(UNITS_AR.get(_boq(pl).unit, _boq(pl).unit))},
    {"key": "net_qty",  "label_ar": "الكمية",          "cls": "num",     "detailed": False,
     "render": lambda pl, ctx: _esc(_qty(_boq(pl).net_qty, _boq(pl).unit))},
    {"key": "waste",    "label_ar": "الهدر",           "cls": "num",     "detailed": True,
     "render": lambda pl, ctx: _esc(_pct(_num(_boq(pl).waste_pct) * 100))},
    {"key": "gross_qty", "label_ar": "كمية التوريد",   "cls": "num",     "detailed": True,
     "render": lambda pl, ctx: _esc(_qty(_boq(pl).gross_qty, _boq(pl).unit))},
    {"key": "mat_rate", "label_ar": "سعر المادة",      "cls": "num",     "detailed": False,
     "render": lambda pl, ctx: _esc(_money(_get(pl, "material_rate", 0)))},
    {"key": "lab_rate", "label_ar": "سعر الأجرة",      "cls": "num",     "detailed": False,
     "render": lambda pl, ctx: _esc(_money(_get(pl, "labor_rate", 0)))},
    {"key": "total",    "label_ar": "الإجمالي",         "cls": "num strong", "detailed": False,
     "render": lambda pl, ctx: _esc(_money(_total_of(pl)))},
)


def _columns(detailed: bool) -> tuple[dict, ...]:
    return tuple(c for c in BOQ_COLUMNS if detailed or not c["detailed"])


# --------------------------------------------------------------------------- #
# The audit trail — the reason a client stops arguing                           #
# --------------------------------------------------------------------------- #
def _pricing_steps(pl: Any, currency: str) -> list[str]:
    """The MONEY half of the audit trail, rebuilt from the stored rates.

    The engine's ``breakdown`` proves the quantity; these lines prove the price.
    They are recomputed here on purpose: if the recomputation disagrees with the
    stored total, the disagreement is printed rather than hidden.
    """
    b = _boq(pl)
    basis = str(_get(pl, "qty_basis", "net"))
    unit_ar = UNITS_AR.get(b.unit, b.unit)
    qty = _num(b.gross_qty) if basis == "gross" else _num(b.net_qty)
    basis_ar = ("كمية التوريد (شاملة الهدر)" if basis == "gross" else "الكمية الصافية المقاسة")
    mat_rate, lab_rate = _num(_get(pl, "material_rate", 0)), _num(_get(pl, "labor_rate", 0))
    mat, lab = _num(_get(pl, "material_total", 0)), _num(_get(pl, "labor_total", 0))
    steps = [
        f"أساس التسعير: {basis_ar} = {_qty(qty, b.unit)} {unit_ar}.",
        f"قيمة المواد = {_qty(qty, b.unit)} {unit_ar} × {_money(mat_rate)} "
        f"{currency}/{unit_ar} = {_money(mat)} {currency}.",
        f"قيمة الأجور = {_qty(b.net_qty, b.unit)} {unit_ar} (الصافي دائماً — لا أحد "
        f"يركّب الهدر) × {_money(lab_rate)} {currency}/{unit_ar} = {_money(lab)} {currency}.",
        f"إجمالي البند = {_money(mat)} + {_money(lab)} = {_money(_total_of(pl))} {currency}.",
    ]
    exp_mat, exp_lab = round(qty * mat_rate, _ND_MONEY), round(_num(b.net_qty) * lab_rate, _ND_MONEY)
    if abs(exp_mat - mat) > 0.01 or abs(exp_lab - lab) > 0.01:
        steps.append(f"⚠ إعادة الاحتساب تعطي مواد {_money(exp_mat)} وأجور {_money(exp_lab)} "
                     f"— تختلف عن القيم المخزّنة أعلاه؛ راجع مصدر التسعير قبل الاعتماد.")
    if abs(_total_of(pl) - (mat + lab)) > 0.01:
        steps.append("⚠ إجمالي البند المخزّن لا يساوي مجموع المواد والأجور.")
    src = str(_get(pl, "rate_source", "") or "")
    steps.append(f"مصدر السعر: {src or 'غير محدّد'} · قاعدة الحصر: {b.rule_key or 'غير محدّدة'}.")
    return steps


def _audit_row(pl: Any, ncols: int, currency: str) -> str:
    b = _boq(pl)
    bd = list(b.breakdown or [])
    items = "".join(f"<li>{_esc(s)}</li>" for s in bd)
    if not items:
        items = ('<li class="bad">لا يوجد بيان حسابي لهذا البند — كمية غير قابلة '
                 'للتتبّع، ولا يجوز اعتمادها في مستخلص.</li>')
    money = "".join(f"<li>{_esc(s)}</li>" for s in _pricing_steps(pl, currency))
    return (f'<tr class="audit"><td colspan="{ncols}">'
            f'<div class="audit-box">'
            f'<div class="audit-h">البيان الحسابي للكمية — {_esc(b.code)}</div>'
            f'<ol>{items}</ol>'
            f'<div class="audit-h">احتساب القيمة</div>'
            f'<ol>{money}</ol>'
            f'</div></td></tr>')


# --------------------------------------------------------------------------- #
# Section builders                                                              #
# --------------------------------------------------------------------------- #
def _kv(label: str, value: str) -> str:
    return f'<div class="kv"><b>{_esc(label)}:</b> {value}</div>'


def _cover(project: Project, opts: ProposalOptions, ctx: dict) -> str:
    p = project
    left = "".join([
        _kv("المشروع", _esc(p.name)),
        _kv("الزبون", _esc(p.client_name)),
        _kv("المنطقة", _esc(p.region)),
        _kv("رقم المشروع", _esc(p.id)),
    ])
    right = "".join([
        _kv("رقم العرض", _esc(ctx["quote_no"])),
        _kv("إصدار العرض", _esc(ctx["quote_version"])),
        _kv("تاريخ الإصدار", _esc(ctx["issue_ar"])),
        _kv("سريان العرض حتى", _esc(ctx["expiry_ar"])),
    ])
    third = "".join([
        _kv("العملة", _esc(ctx["currency"])),
        _kv("عدد الفراغات", _esc(_cnt(len(p.spaces)))),
        _kv("عدد البنود", _esc(_cnt(len(ctx["lines"])))),
        _kv("قاعدة الأسعار", _esc(ctx["meta"]["label"])),
    ])
    grand = ctx["figures"].get("grand_total")
    headline = (f'<div class="headline"><span>الإجمالي النهائي</span>'
                f'<b class="num">{_esc(_money(grand))} {_esc(ctx["currency"])}</b></div>'
                if grand is not None else
                '<div class="headline bad"><span>الإجمالي النهائي</span>'
                '<b>غير متوفّر في المدخلات</b></div>')
    office = ""
    if opts.office_name or opts.office_contact:
        office = (f'<div class="office">{_esc(opts.office_name)}'
                  + (f' <span class="muted">· {_esc(opts.office_contact)}</span>'
                     if opts.office_contact else "") + "</div>")
    return f'''<header>
  <div class="head-row">
    <div><h1>{_esc(opts.doc_title)}</h1>{office}</div>
    <div class="stamp">عرض سعر رقم {_esc(ctx["quote_no"])} — إصدار {_esc(ctx["quote_version"])}</div>
  </div>
</header>
<div class="box grid">
  <div>{left}</div><div>{right}</div><div>{third}</div>
</div>
{headline}'''


def _draft_banner(reasons: Sequence[str]) -> str:
    if not reasons:
        return ""
    items = "".join(f"<li>{_esc(r)}</li>" for r in reasons)
    return f'''<div class="draft-banner">
  <div class="draft-title">مسودة داخلية — لا تُرسل هذه النسخة إلى الزبون</div>
  <p>الأسعار الواردة أدناه <b>غير نهائية وغير ملزِمة</b>، ولا تصلح للتوقيع ولا للتعاقد
  ولا للاستناد إليها في مستخلص. السبب:</p>
  <ul>{items}</ul>
  <p class="draft-foot">أعد إصدار العرض بعد ربطه بقاعدة أسعار حقيقية ومُصدَّرة برقم إصدار،
  ثم أرسله. إرسال أسعار مبدئية كأنها عرض نهائي خسارة مالية مباشرة ومسؤولية قانونية.</p>
</div>'''


def _warn_box(title: str, msgs: Sequence[str], kind: str) -> str:
    if not msgs:
        return ""
    items = "".join(f"<li>{_esc(m)}</li>" for m in msgs)
    return (f'<div class="warn {kind}"><div class="warn-title">{_esc(title)}</div>'
            f'<ul>{items}</ul></div>')


def _scope(project: Project, ctx: dict, opts: ProposalOptions) -> str:
    """Scope summary per space: what is measured, and what it costs."""
    rows = []
    base = ctx["lines_sum"] or 0.0
    for g in ctx["groups"]:
        sp: Optional[Space] = g["space"]
        trades = "، ".join(t["label_ar"] for t in g["trades"]) or "—"
        n_lines = sum(len(t["lines"]) for t in g["trades"])
        share = (g["total"] / base * 100.0) if base else 0.0
        if sp is not None:
            type_ar = SPACE_TYPES_AR.get(sp.type, sp.type)
            area, per, net_w = _geom(sp, "floor_area"), _geom(sp, "perimeter_m"), _geom(sp, "net_wall_area")
            h, n_open = _fmt(sp.height_m), _cnt(sum(int(o.count or 0) for o in sp.openings))
        else:
            type_ar, area, per, net_w, h, n_open = "—", None, None, None, "—", "—"
        orphan_tag = '' if not g["orphan"] else ' <span class="bad">(فراغ غير معرّف)</span>'
        rows.append(
            "<tr>"
            f'<td>{_esc(g["space_name"])}{orphan_tag}</td>'
            f"<td>{_esc(type_ar)}</td>"
            f'<td class="num">{_esc(_g(area))}</td>'
            f'<td class="num">{_esc(_g(per))}</td>'
            f'<td class="num">{_esc(h)}</td>'
            f'<td class="num">{_esc(_g(net_w))}</td>'
            f'<td class="num">{_esc(n_open)}</td>'
            f"<td>{_esc(trades)}</td>"
            f'<td class="num">{_esc(_cnt(n_lines))}</td>'
            f'<td class="num strong">{_esc(_money(g["total"]))}</td>'
            f'<td class="num">{_esc(_pct(share))}</td>'
            "</tr>")
    head = ("<tr><th>الفراغ</th><th>النوع</th><th>مساحة الأرضية (م٢)</th>"
            "<th>المحيط (م.ط)</th><th>الارتفاع (م)</th><th>صافي الجدران (م٢)</th>"
            "<th>عدد الفتحات</th><th>الأعمال المشمولة</th><th>عدد البنود</th>"
            f'<th>القيمة ({_esc(ctx["currency"])})</th><th>النسبة</th></tr>')
    foot = (f'<tr class="sub"><td colspan="9">مجموع بنود الأعمال (محسوب من الجدول)</td>'
            f'<td class="num strong">{_esc(_money(ctx["lines_sum"]))}</td>'
            f'<td class="num">100%</td></tr>')
    return (f'<h2>نطاق العمل حسب الفراغ</h2>'
            f'<p class="muted">الأبعاد أدناه هي الأساس الذي حُسبت عليه كل كمية في الجدول '
            f'التالي؛ «—» تعني أن المخطط لا يسمح باشتقاق القيمة (ولا تعني صفراً).</p>'
            f'<div class="scroll"><table><thead>{head}</thead><tbody>{"".join(rows)}</tbody>'
            f'<tfoot>{foot}</tfoot></table></div>')


def _space_geometry_caption(sp: Optional[Space]) -> str:
    if sp is None:
        return ('<div class="caption bad">لا يوجد فراغ مطابق لهذه البنود في المشروع — '
                'الكميات غير قابلة للمراجعة على المخطط.</div>')
    bits = [
        f"مساحة الأرضية {_g(_geom(sp, 'floor_area'))} م٢",
        f"المحيط {_g(_geom(sp, 'perimeter_m'))} م.ط",
        f"الارتفاع {_fmt(sp.height_m)} م",
        f"إجمالي الجدران {_g(_geom(sp, 'gross_wall_area'))} م٢",
        f"الفتحات {_g(_geom(sp, 'openings_area'))} م٢",
        f"صافي الجدران {_g(_geom(sp, 'net_wall_area'))} م٢",
    ]
    return f'<div class="caption">{_esc(" · ".join(bits))}</div>'


def _boq_section(ctx: dict, opts: ProposalOptions) -> str:
    cols = _columns(opts.detailed)
    ncols = len(cols)
    head = "".join(f'<th class="{_esc(c["cls"])}">{_esc(c["label_ar"])}</th>' for c in cols)
    cur = ctx["currency"]
    out = [f'<h2>جدول الكميات والأسعار</h2>',
           f'<p class="muted">الأسعار بعملة {_esc(cur)} · «الكمية» هي الكمية الصافية '
           f'المقاسة وهي أساس الاستحقاق'
           + (' · «كمية التوريد» تشمل الهدر وهي أساس الشراء لا أساس الدفع'
              if opts.detailed else '')
           + '.</p>']
    idx = 0
    for g in ctx["groups"]:
        out.append('<section class="space-block">')
        out.append(f'<h3>الفراغ: {_esc(g["space_name"])}</h3>')
        if opts.show_geometry:
            out.append(_space_geometry_caption(g["space"]))
        if not g["trades"]:
            out.append('<p class="muted">لا توجد بنود مسعّرة لهذا الفراغ في هذا العرض.</p>'
                       '</section>')
            continue
        out.append('<div class="scroll"><table><thead><tr>' + head + '</tr></thead><tbody>')
        for t in g["trades"]:
            out.append(f'<tr class="trade-head"><td colspan="{ncols}">{_esc(t["label_ar"])}</td></tr>')
            for pl in t["lines"]:
                idx += 1
                cells = "".join(
                    f'<td class="{_esc(c["cls"])}">{c["render"](pl, {"idx": idx})}</td>'
                    for c in cols)
                out.append(f"<tr>{cells}</tr>")
                if opts.detailed:
                    out.append(_audit_row(pl, ncols, cur))
            out.append(f'<tr class="sub"><td colspan="{ncols - 1}">مجموع {_esc(t["label_ar"])}</td>'
                       f'<td class="num strong">{_esc(_money(t["total"]))}</td></tr>')
        out.append(f'<tr class="sub space-sum"><td colspan="{ncols - 1}">'
                   f'مجموع الفراغ «{_esc(g["space_name"])}»</td>'
                   f'<td class="num strong">{_esc(_money(g["total"]))}</td></tr>')
        out.append("</tbody></table></div></section>")
    return "".join(out)


def _totals_block(ctx: dict, opts: ProposalOptions) -> str:
    cur = ctx["currency"]
    rows = []
    for r in TOTALS_ROWS:
        v = ctx["figures"].get(r["key"])
        # An optional row prints only when it carries money. A «خصم −0.00» line invites
        # the client to ask what the discount was; a required row prints regardless,
        # because a silently absent VAT line is a silently wrong price.
        if not r["required"] and (v is None or abs(_num(v)) < 0.005):
            continue
        rate = ctx["figures"].get(r["rate_key"]) if r["rate_key"] else None
        label = r["label_ar"] + (f" ({_pct(_num(rate) * 100)})" if rate is not None else "")
        sign = "−" if r["kind"] == "sub" else ""
        cls = "grand" if r["kind"] == "grand" else ""
        val = "—" if v is None else f"{sign}{_money(v)}"
        cell_cls = "num strong" + (" bad" if v is None else "")
        rows.append(f'<tr class="{cls}"><td>{_esc(label)}</td>'
                    f'<td class="{cell_cls}">{_esc(val)}</td></tr>')
    if opts.detailed:
        rows.insert(0, f'<tr class="chk"><td>مجموع البنود كما أعدنا جمعه من الجدول</td>'
                       f'<td class="num">{_esc(_money(ctx["lines_sum"]))}</td></tr>')
        if ctx["recomputed_grand"] is not None:
            rows.append(f'<tr class="chk"><td>الإجمالي كما أعدنا جمعه من مكوّناته</td>'
                        f'<td class="num">{_esc(_money(ctx["recomputed_grand"]))}</td></tr>')
    return (f'<h2>المجاميع</h2>'
            f'<div class="totals-wrap"><table class="totals"><thead>'
            f'<tr><th>البيان</th><th class="num">القيمة ({_esc(cur)})</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _terms_section(opts: ProposalOptions, ctx: dict) -> str:
    items = "".join(f"<li>{_esc(t)}</li>" for t in _terms(opts, ctx))
    notes = ""
    if opts.notes_ar:
        notes = ('<h3>ملاحظات خاصة بهذا المشروع</h3><ul class="notes">'
                 + "".join(f"<li>{_esc(n)}</li>" for n in opts.notes_ar) + "</ul>")
    return (f'<section class="terms"><h2>الشروط والأحكام</h2><ol>{items}</ol>{notes}</section>')


def _validity_block(ctx: dict) -> str:
    if ctx["draft_reasons"]:
        return ('<div class="box validity bad"><b>مدة سريان العرض:</b> لا يوجد — '
                'هذه النسخة مسودة داخلية بأسعار غير نهائية، ولا تسري على أحد ولا '
                'تُلزم أحداً. لا تُصدر مدة سريان إلا لعرض مبني على قاعدة أسعار مُصدَّرة.</div>')
    return (f'<div class="box validity"><b>مدة سريان العرض:</b> '
            f'{_esc(_cnt(ctx["validity_days"]))} يوماً — من {_esc(ctx["issue_ar"])} '
            f'حتى {_esc(ctx["expiry_ar"])}. بعد هذا التاريخ يسقط الالتزام بالأسعار '
            f'الواردة أعلاه وتُعاد التسعيرة على قاعدة الأسعار السارية وقتها.</div>')


def _signature(project: Project, opts: ProposalOptions, ctx: dict) -> str:
    if ctx["draft_reasons"]:
        return ('<section class="sig-disabled"><h2>التوقيع والاعتماد</h2>'
                '<p>خانة التوقيع <b>معطّلة</b> في هذه النسخة: المستند مسودة داخلية مبنية '
                'على أسعار غير نهائية، ولا يجوز توقيعه ولا اعتماده ولا إرساله للزبون. '
                'أعد الإصدار بعد ربطه بقاعدة أسعار مُصدَّرة برقم إصدار.</p></section>')
    office = opts.office_name or "المكتب / المقاول"
    return f'''<section class="sig-wrap"><h2>التوقيع والاعتماد</h2>
<p class="muted">بتوقيع الطرفين يصبح هذا العرض بجداوله وبياناته الحسابية جزءاً من العقد.</p>
<div class="sig">
  <div><b>عن {_esc(office)}</b><div class="muted">الاسم والصفة</div><div class="line"></div>
    <div class="muted">التوقيع والختم</div><div class="line"></div></div>
  <div><b>الزبون: {_esc(project.client_name)}</b><div class="muted">الاسم والصفة</div>
    <div class="line"></div><div class="muted">التوقيع</div><div class="line"></div></div>
  <div><b>التاريخ</b><div class="muted">تاريخ الاعتماد</div><div class="line"></div>
    <div class="muted">رقم العرض: {_esc(ctx["quote_no"])} — إصدار {_esc(ctx["quote_version"])}</div></div>
</div></section>'''


def _footer(project: Project, opts: ProposalOptions, ctx: dict) -> str:
    """Reproducibility, in one strip: which price book, which fx, which version."""
    m = ctx["meta"]
    bits = [
        f"قاعدة الأسعار: {m['label']}",
        f"إصدار العرض: {ctx['quote_version']}",
        f"تاريخ الإصدار: {ctx['issue_iso']}",
    ]
    if m.get("fx_rate") is not None:
        fx = f"سعر الصرف: {_fmt(m['fx_rate'])}"
        if m.get("fx_base"):
            fx += f" مقابل {m['fx_base']}"
        if m.get("fx_date"):
            fx += f" ({m['fx_date']})"
        bits.append(fx)
    else:
        bits.append("سعر الصرف: غير مطبَّق")
    if m.get("sources"):
        bits.append("مصادر الأسعار في البنود: " + "، ".join(m["sources"]))
    bits.append(f"مُعرّف المشروع: {project.id}")
    bits.append(f"تاريخ إنشاء ملف المشروع: {project.created_at}")
    note = ("كل رقم في هذا المستند ناتج عن حساب حسابي مثبّت خطوة بخطوة في البيان الحسابي "
            "لكل بند، ومشتق من إصدار قاعدة الأسعار المذكور أعلاه — لا يوجد فيه تقدير غير "
            "موثّق." if opts.lang_note else "")
    return (f'<footer><div class="fbits">{_esc(" · ".join(bits))}</div>'
            + (f'<div class="fnote">{_esc(note)}</div>' if note else "")
            + "</footer>")


# --------------------------------------------------------------------------- #
# Style — inline, self-contained, print-first                                   #
# --------------------------------------------------------------------------- #
_CSS = """
:root{
  --ink:#1c2430; --muted:#6b7685; --line:#dde3ec; --panel:#fbfcfe; --head:#f2f5f9;
  --accent:#123c63; --danger:#c62828; --ok:#137333; --warn:#b06000;
}
*{box-sizing:border-box}
body{
  font-family:"IBM Plex Sans Arabic","Noto Naskh Arabic","Segoe UI",Tahoma,Arial,sans-serif;
  color:var(--ink);background:#fff;max-width:1000px;margin:0 auto;padding:26px 20px;
  line-height:1.7;font-size:13.5px;
  -webkit-print-color-adjust:exact;print-color-adjust:exact;
}
h1{font-size:23px;margin:0 0 3px}
h2{font-size:16.5px;margin:26px 0 10px;border-inline-start:4px solid var(--accent);
   padding-inline-start:10px}
h3{font-size:14px;margin:18px 0 4px;color:var(--accent)}
p{margin:6px 0}
ul,ol{margin:6px 0;padding-inline-start:20px}
li{margin:3px 0}
.muted{color:var(--muted)}
.bad{color:var(--danger)}
.strong{font-weight:700}
.head-row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;
  flex-wrap:wrap;border-bottom:2px solid var(--accent);padding-bottom:10px}
.office{color:var(--muted);font-size:12.5px}
.stamp{border:1px solid var(--accent);color:var(--accent);border-radius:8px;
  padding:6px 12px;font-size:12px;font-weight:700;white-space:nowrap}
.box{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0;
  background:var(--panel)}
.grid{display:flex;flex-wrap:wrap;gap:8px 18px}
.grid>div{flex:1 1 240px}
.kv{margin:3px 0;font-size:13px}
.kv b{color:var(--muted);font-weight:600}
.headline{display:flex;justify-content:space-between;align-items:center;gap:12px;
  background:var(--accent);color:#fff;border-radius:10px;padding:12px 16px;margin:10px 0 4px;
  font-size:15px}
.headline b{font-size:20px;direction:ltr}
.headline.bad{background:var(--danger)}
.caption{color:var(--muted);font-size:12px;margin:2px 0 6px}
.scroll{overflow-x:auto;max-width:100%}
table{width:100%;border-collapse:collapse;font-size:12px;margin:4px 0 12px}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:start;vertical-align:top}
th{background:var(--head);font-weight:700}
tbody tr:nth-child(even) td{background:#fafbfd}
td.num,th.num{direction:ltr;text-align:end;font-variant-numeric:tabular-nums;white-space:nowrap}
td.idx{width:34px;color:var(--muted)}
td.unit{white-space:nowrap;width:52px}
td.item{min-width:220px}
.code{font-family:Consolas,"Courier New",monospace;font-size:11px;color:var(--muted);
  direction:ltr;display:inline-block}
.iname{display:inline}
tr.trade-head td{background:#e8eef6;color:var(--accent);font-weight:700}
tr.sub td{background:#f4f7fb;font-weight:700}
tr.space-sum td{background:#e9eef5}
tr.audit td{background:#fffdf4;border-top:0}
.audit-box{padding:2px 4px}
.audit-h{font-size:11.5px;font-weight:700;color:var(--warn);margin:4px 0 2px}
tr.audit ol{margin:2px 0;padding-inline-start:20px}
tr.audit li{font-size:11.5px;color:#3a4553;line-height:1.65}
.totals-wrap{display:flex;justify-content:flex-start}
table.totals{width:auto;min-width:min(460px,100%)}
table.totals td:first-child{min-width:260px}
table.totals tr.grand td{background:var(--accent);color:#fff;font-size:14.5px}
table.totals tr.chk td{background:#fffdf4;color:#3a4553;font-size:11.5px;font-weight:400}
.warn{border-radius:10px;padding:10px 14px;margin:12px 0;font-size:12.5px}
.warn-title{font-weight:700;margin-bottom:4px}
.warn.critical{border:2px solid var(--danger);background:#fff2f2;color:#7f1414}
.warn.advisory{border:1px solid var(--warn);background:#fffaf0;color:#6b4400}
.draft-banner{border:3px solid var(--danger);background:#fff1f1;color:#7f1414;
  border-radius:12px;padding:14px 16px;margin:0 0 14px}
.draft-title{font-size:18px;font-weight:800;letter-spacing:.5px;margin-bottom:6px}
.draft-foot{font-size:12px;margin-top:8px}
.watermark{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  pointer-events:none;z-index:9999}
.watermark span{font-size:58px;font-weight:800;color:rgba(198,40,40,.12);
  transform:rotate(-28deg);white-space:nowrap;letter-spacing:6px}
.validity{border-inline-start:4px solid var(--accent)}
.terms ol{padding-inline-start:22px}
.terms li{margin:5px 0;font-size:12.5px}
.notes li{font-size:12.5px}
.sig{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}
.sig>div{flex:1 1 240px;border:1px solid var(--line);border-radius:10px;padding:12px;
  background:var(--panel)}
.sig .line{border-bottom:1px dotted #98a3b3;height:30px;margin:6px 0 4px}
.sig .muted{font-size:11.5px}
.sig-disabled{border:2px dashed var(--danger);border-radius:10px;padding:10px 14px;
  background:#fff5f5;color:#7f1414;margin-top:16px}
.sig-disabled h2{border:0;padding:0;margin:0 0 6px;color:#7f1414}
footer{border-top:1px solid var(--line);margin-top:26px;padding-top:10px;
  font-size:11px;color:var(--muted)}
.fbits{direction:rtl}
.fnote{margin-top:4px}
@page{size:A4;margin:14mm 12mm}
@media print{
  body{padding:0;max-width:none;font-size:11.5px}
  h2{margin-top:16px}
  table{font-size:10.5px;page-break-inside:auto}
  thead{display:table-header-group}
  tfoot{display:table-footer-group}
  tr,td,th{page-break-inside:avoid;break-inside:avoid}
  h1,h2,h3,.trade-head{page-break-after:avoid;break-after:avoid}
  .box,.headline,.warn,.draft-banner,.sig>div,.sig-disabled,.validity,.audit-box,
  .totals-wrap,.caption{page-break-inside:avoid;break-inside:avoid}
  .terms li{page-break-inside:avoid;break-inside:avoid}
  .page-break{page-break-before:always;break-before:page}
  .scroll{overflow:visible}
  a[href]:after{content:""}
}
"""


# --------------------------------------------------------------------------- #
# The public entry point                                                        #
# --------------------------------------------------------------------------- #
def render_html(project: Project,
                priced_lines: Iterable[PricedLine],
                totals: Any,
                *,
                options: Any = None) -> str:
    """Render the whole quote as ONE self-contained RTL Arabic HTML document.

    ``priced_lines`` are :class:`~core.schema.PricedLine` objects, ``totals`` is the
    mapping (or object) the pricing module produced — ``subtotal``, ``overhead``,
    ``profit``, ``vat``, ``grand_total`` and optionally the percentage rates behind
    them. ``options`` is a :class:`ProposalOptions` (or a plain dict of its fields).

    The function is pure: no clock, no filesystem, no network, no LLM. The issue
    date defaults to ``project.created_at``, so re-rendering the same project a year
    later produces the same bytes — which is the only reason the document is worth
    anything in a dispute.
    """
    opts = ProposalOptions.from_any(options)
    lines = list(priced_lines or [])

    currency = str(_first(totals, ("currency",), "") or project.currency or "").strip()
    meta = _price_book(opts, totals, lines)
    draft_reasons = _draft_reasons(opts, meta, lines)

    issue = _as_date(opts.issue_date) or _as_date(project.created_at)
    validity_days = max(0, int(_num(opts.validity_days, DEFAULT_VALIDITY_DAYS)))
    expiry = (issue + timedelta(days=validity_days)) if issue else None

    figures: dict[str, Optional[float]] = {}
    for r in TOTALS_ROWS:
        figures[r["key"]] = _totals_get(totals, r["key"])
        if r["rate_key"]:
            figures[r["rate_key"]] = _totals_get(totals, r["rate_key"])

    lines_sum = round(sum(_total_of(pl) for pl in lines), _ND_MONEY)
    if figures.get("subtotal") is None and lines:
        # Not an invention: it is the same addition the client would do by hand,
        # and it is labelled as ours everywhere it appears.
        figures["subtotal"] = lines_sum

    recomputed_grand: Optional[float] = None
    if figures.get("subtotal") is not None:
        acc = float(figures["subtotal"])
        for r in TOTALS_ROWS:
            v = figures.get(r["key"])
            if v is None:
                continue
            if r["kind"] == "add":
                acc += float(v)
            elif r["kind"] == "sub":
                acc -= float(v)
        recomputed_grand = round(acc, _ND_MONEY)

    groups = group_lines(project, lines)

    ctx = {
        "project": project, "lines": lines, "groups": groups, "totals": totals,
        "figures": figures, "lines_sum": lines_sum, "recomputed_grand": recomputed_grand,
        "currency": currency or "—", "meta": meta, "draft_reasons": draft_reasons,
        "is_draft": bool(draft_reasons),
        "quote_no": str(opts.quote_no or project.id or ""),
        "quote_version": str(opts.quote_version or "1"),
        "issue_ar": _date_ar(issue), "issue_iso": (issue.isoformat() if issue else "—"),
        "issue_short": _date_ar(issue, with_iso=False),
        "expiry_ar": _date_ar(expiry), "expiry_short": _date_ar(expiry, with_iso=False),
        "validity_days": validity_days,
        "price_book_label": meta["label"],
        "fx_rate": (None if meta.get("fx_rate") is None else _fmt(meta["fx_rate"])),
        "fx_base": meta.get("fx_base") or "", "fx_date": meta.get("fx_date") or "",
        "client": project.client_name, "region": project.region,
    }

    critical = _run_checks(ctx, "critical")
    advisory = _run_checks(ctx, "advisory") if opts.detailed else []
    geom_warns: list[str] = []
    if opts.detailed:
        try:
            geom_warns = list(project.validate())
        except (ValueError, TypeError, AttributeError):
            geom_warns = []

    title = ("مسودة — " if draft_reasons else "") + f"{opts.doc_title} — {project.name}"
    watermark = ('<div class="watermark"><span>مسودة — غير صالحة للإرسال</span></div>'
                 if draft_reasons else "")

    body = [
        watermark,
        _draft_banner(draft_reasons),
        _cover(project, opts, ctx),
        _warn_box("تنبيه: هذا المستند لا يتّسق مع نفسه", critical, "critical"),
        _scope(project, ctx, opts),
        _boq_section(ctx, opts),
        _totals_block(ctx, opts),
        _warn_box("ملاحظات مراجعة داخلية (تظهر في وضع البيان المفصّل فقط)",
                  advisory, "advisory"),
        _warn_box("ملاحظات على المخطط استُخرجت منه الكميات", geom_warns, "advisory"),
        _validity_block(ctx),
        _terms_section(opts, ctx),
        _signature(project, opts, ctx),
        _footer(project, opts, ctx),
    ]

    return (
        '<!doctype html>\n<html lang="ar" dir="rtl"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        + "\n".join(x for x in body if x)
        + "\n</body></html>"
    )
