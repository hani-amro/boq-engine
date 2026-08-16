# -*- coding: utf-8 -*-
"""Where quantities become money: the versioned, region-swappable price book.

The quantities are physics — a square metre is a square metre in Amman and in
Riyadh. The *rates* are the office's accumulated knowledge, and they are the only
part of this product a competitor cannot copy from a textbook. So the price book
is treated as an asset, not as a config file:

* **A book is data, versioned like a contract.** ``core/prices/<region>.yaml`` is
  the working book; a published book is frozen and a new price list is a NEW FILE
  (``<region>-v1.2.0.yaml``), never an edit. Every book carries a content
  fingerprint, and every quote records ``book id + version + fingerprint``, so an
  interim certificate raised eighteen months ago can be recomputed to the fils —
  and :func:`verify_reproducible` will say so out loud if someone edited a
  published file underneath it.
* **A rate nobody can trace is a rate nobody can defend.** Exactly like
  ``BOQLine.breakdown``, every priced line carries the Arabic arithmetic that put
  the money on it (quantity × rate = total), and the project totals carry their
  own step-by-step Arabic trail (subtotal → overhead → profit → tax → grand
  total). A client who asks "where did this number come from?" gets an answer, in
  Arabic, from the file — not from a salesman.
* **Currency volatility is a first-class citizen.** In the Levant a price list
  denominated in local currency is worthless within a quarter. A book may store
  its rates in a stable ANCHOR currency and convert at quote time using an
  exchange rate that is *captured*, timestamped and recorded on every line. The
  module refuses to invent an exchange rate the way :mod:`core.schema` refuses to
  invent a zero area.
* **Placeholder rates can never leave the office.** The two starter books ship
  with ``meta.is_placeholder: true``. :func:`assert_client_ready` raises on any
  quote built on them, so the UI can grey out "send to client" until the
  architect has entered real, surveyed numbers.

Nothing here calls an LLM, and nothing here guesses. Dependencies: Python stdlib
+ PyYAML. Arabic is the deliverable language; identifiers and comments stay
English.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

import yaml

from .schema import UNITS, UNITS_AR, BOQLine, PricedLine, Project, price_line
# The ONE marker that distinguishes a zero the engine is unhappy about (unmeasured
# geometry, a parent it never found) from a zero it decided on purpose (a piece charged
# to the neighbouring space). Both print; only the first may leave the office.
from .rules_loader import WARN_PREFIX

__all__ = [
    "PriceError", "BookError", "RateMissingError", "UnitMismatchError",
    "FxError", "PlaceholderRatesError",
    "PRICES_DIR", "Rate", "PriceBook", "FxRate", "QuoteTerms", "QuoteStep", "Quote",
    "load_book", "list_books", "list_versions", "regions", "parse_book",
    "validate_book", "get_rate", "missing_rates", "coverage",
    "price", "price_one", "apply_terms", "quote",
    "assert_client_ready", "client_block_reason_ar", "verify_reproducible",
    "CURRENCY_AR", "TOTAL_STEPS", "TOTAL_STEPS_BY_CODE", "TOTAL_BASES",
    "TOTAL_BASES_AR", "MISSING_RATE_POLICIES", "MISSING_RATE_POLICIES_AR",
    "BOOK_SCHEMA", "MONEY_ND", "RATE_ND", "PRICING_MARK",
]

PRICES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices")

BOOK_SCHEMA = "price-book/1"
REQUIRED_META = ("id", "region", "currency", "version", "effective_date",
                 "is_placeholder", "source_note_ar")
REQUIRED_RATE_FIELDS = ("material_rate", "labor_rate", "unit")

# Money is rounded to 2 decimals to match the pinned kernel ``schema.price_line``.
# Rates are kept at 4 so an FX-converted rate stays exact enough that the client
# can recompute (printed quantity × printed rate = printed total) by hand.
MONEY_ND = 2
RATE_ND = 4

# Advisory ages. They only ever raise Arabic warnings — a stale book is a business
# problem, not a crash.
FX_STALE_HOURS = 24.0
BOOK_STALE_DAYS = 180.0

# Section marker inside a BOQLine.breakdown. Everything from this line down was
# appended by pricing, so re-pricing an already-priced line can strip it and stay
# byte-identical (see _strip_pricing / verify_reproducible).
PRICING_MARK = "——— التسعير ———"


class PriceError(ValueError):
    """Money could not be put on a quantity — raised INSTEAD of returning 0."""


class BookError(PriceError):
    """The price book file itself is wrong (missing meta, bad unit, duplicate version)."""


class RateMissingError(PriceError):
    """The book has no rate for a rule key that the BOQ actually measured."""


class UnitMismatchError(PriceError):
    """The book's rate unit and the measured line's unit disagree — arithmetic nonsense."""


class FxError(PriceError):
    """An exchange rate is required, missing, or does not match the book's currency."""


class PlaceholderRatesError(PriceError):
    """A client-facing document was requested from placeholder rates."""


# --------------------------------------------------------------------------- #
# Registries: stable string code -> Arabic label / behaviour.                   #
# --------------------------------------------------------------------------- #

# Currency code -> the Arabic name that is printed on the quote.
CURRENCY_AR = {
    "USD": "دولار أمريكي",
    "EUR": "يورو",
    "JOD": "دينار أردني",
    "SYP": "ليرة سورية",
    "LBP": "ليرة لبنانية",
    "ILS": "شيكل",
    "IQD": "دينار عراقي",
    "EGP": "جنيه مصري",
    "SAR": "ريال سعودي",
    "AED": "درهم إماراتي",
    "QAR": "ريال قطري",
    "KWD": "دينار كويتي",
    "BHD": "دينار بحريني",
    "OMR": "ريال عُماني",
}

# What a percentage step is applied TO. "running" compounds (profit is earned on
# cost + overhead, which is how a contractor actually prices); "subtotal" applies
# flat to the bare cost, which some offices prefer for the tax line. The choice is
# the architect's, and it is printed on the quote either way.
TOTAL_BASES: dict[str, Callable[[float, float], float]] = {
    "running": lambda subtotal, running: running,
    "subtotal": lambda subtotal, running: subtotal,
}

TOTAL_BASES_AR = {
    "running": "المتجمِّع",
    "subtotal": "المجموع الأولي",
}

# The project-level cascade, IN ORDER. Order is not cosmetic: tax on top of profit
# and profit on top of overhead is a different number from the reverse, and the
# sequence below is the Levant/Gulf contracting convention.
TOTAL_STEPS: tuple[dict, ...] = (
    {"code": "overhead", "field": "overhead_pct", "sign": +1.0, "base": "running",
     "label_ar": "مصاريف إدارية وعامة (أوفرهيد)",
     "note_ar": "نسبة تشغيل المكتب والموقع: إشراف، نقل، تأمينات، ضمانات."},
    {"code": "profit", "field": "profit_pct", "sign": +1.0, "base": "running",
     "label_ar": "ربح المقاول",
     "note_ar": "تُحسب على التكلفة بعد المصاريف الإدارية — هذا هو العرف المتبع."},
    {"code": "contingency", "field": "contingency_pct", "sign": +1.0, "base": "running",
     "label_ar": "احتياطي طوارئ",
     "note_ar": "مخصص للبنود غير المنظورة؛ يُعلَن للعميل ولا يُخفى داخل الفئات."},
    {"code": "discount", "field": "discount_pct", "sign": -1.0, "base": "running",
     "label_ar": "خصم تجاري",
     "note_ar": "خصم معلن يُطرح قبل احتساب الضريبة."},
    {"code": "tax", "field": "tax_pct", "sign": +1.0, "base": "running",
     "label_ar": "ضريبة القيمة المضافة",
     "note_ar": "تُحسب على القيمة النهائية بعد الربح والخصم."},
)

TOTAL_STEPS_BY_CODE = {s["code"]: s for s in TOTAL_STEPS}


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


def _r(x: float, nd: int = MONEY_ND) -> float:
    return round(float(x), nd)


def _money(x: float) -> str:
    """Western digits with thousands separators — how a BOQ prints money."""
    return f"{float(x):,.2f}"


def _fmt(x: float) -> str:
    return f"{float(x):.2f}"


def _rate_fmt(x: float) -> str:
    """A rate, printed exactly as it was used (trailing zeros trimmed)."""
    s = f"{float(x):,.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _plain(x: float) -> str:
    """Same number, no thousands separators — for machine-readable tokens."""
    s = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _pct(x: float) -> str:
    """Percentage without trailing zeros: 10% / 12.5%. Takes an ALREADY-×100 value."""
    s = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return (s or "0") + "%"


def _cur_ar(code: str) -> str:
    return CURRENCY_AR.get(str(code or "").upper(), str(code or ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 / date parse. Returns None rather than raising."""
    txt = str(s or "").strip()
    if not txt:
        return None
    txt = txt.replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,
                  lambda t: datetime.strptime(t, "%Y-%m-%d")):
        try:
            dt = parse(txt)
        except (TypeError, ValueError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _age_hours(earlier: Any, later: Any) -> Optional[float]:
    a, b = _parse_iso(earlier), _parse_iso(later)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def _version_key(v: str) -> tuple:
    """Sort key for a version string. A pre-release tag sorts BELOW its release."""
    nums: list[int] = []
    tail: list[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", str(v or "")):
        if not part:
            continue
        if part.isdigit():
            nums.append(int(part))
        else:
            tail.append(part.lower())
    return (tuple(nums), 0 if tail else 1, tuple(tail))


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: Any) -> str:
    """Content fingerprint of a book. Immutability you can actually check."""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


def _strip_pricing(line: BOQLine) -> BOQLine:
    """Return the line with any previously appended pricing trail removed."""
    bd = list(line.breakdown or [])
    for i, txt in enumerate(bd):
        if str(txt).startswith(PRICING_MARK):
            return replace(line, breakdown=bd[:i])
    return replace(line, breakdown=bd)


# --------------------------------------------------------------------------- #
# The book                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rate:
    """One rule key's money, in the book's own currency.

    ``waste_in_rate`` decides which quantity the MATERIAL is priced on. True (the
    Levant default) means the waste allowance already sits inside the material
    rate, so the line is paid on the measured net quantity — the same convention
    ``schema.price_line`` documents. False means the rate is a bare supply rate
    and the material must be priced on the procurement (gross) quantity, or the
    contractor eats the offcuts.
    """

    rule_key: str
    material_rate: float
    labor_rate: float
    unit: str
    waste_in_rate: bool = True
    note_ar: str = ""
    is_missing: bool = False        # set only by the "zero" missing-rate policy

    @property
    def total_rate(self) -> float:
        return _r(_num(self.material_rate) + _num(self.labor_rate), RATE_ND)

    @property
    def unit_ar(self) -> str:
        return UNITS_AR.get(self.unit, self.unit)

    def to_dict(self) -> dict:
        return {"rule_key": self.rule_key, "material_rate": _num(self.material_rate),
                "labor_rate": _num(self.labor_rate), "unit": self.unit,
                "waste_in_rate": bool(self.waste_in_rate), "note_ar": self.note_ar}


@dataclass(frozen=True)
class PriceBook:
    """An immutable, published set of rates for one region at one version.

    ``fingerprint`` is a hash of the meta + rates as loaded. It is recorded on
    every quote, which is what turns "we used version 1.2.0" from a claim into a
    verifiable fact.
    """

    id: str                         # e.g. "levant-base"
    region: str
    currency: str                   # the currency the RATES ARE STORED IN
    version: str
    effective_date: str
    source_note_ar: str
    is_placeholder: bool
    rates: dict[str, Rate] = field(default_factory=dict)
    is_anchor: bool = False         # rates held in a stable anchor currency
    requires_conversion: bool = False   # refuse to quote without a captured FX rate
    fx_note_ar: str = ""
    path: str = ""
    fingerprint: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def rate_source(self) -> str:
        """The provenance token stamped on every PricedLine, e.g. ``levant-base@1.2.0``."""
        return f"{self.id}@{self.version}"

    @property
    def currency_ar(self) -> str:
        return _cur_ar(self.currency)

    @property
    def placeholder_warning_ar(self) -> str:
        return str(self.meta.get("placeholder_warning_ar") or "").strip()

    def __contains__(self, rule_key: str) -> bool:
        return rule_key in self.rates

    def to_dict(self) -> dict:
        return {"id": self.id, "region": self.region, "currency": self.currency,
                "version": self.version, "effective_date": self.effective_date,
                "source_note_ar": self.source_note_ar,
                "is_placeholder": bool(self.is_placeholder),
                "is_anchor": bool(self.is_anchor),
                "requires_conversion": bool(self.requires_conversion),
                "fingerprint": self.fingerprint, "path": self.path,
                "rates": {k: v.to_dict() for k, v in sorted(self.rates.items())}}


@dataclass(frozen=True)
class FxRate:
    """An exchange rate CAPTURED at quote time, not looked up later.

    In Damascus or Beirut the rate that matters is the one the office actually
    used on the day it signed, together with where that number came from. All
    four fields travel with the quote for exactly that reason.
    """

    anchor: str                     # the book's storage currency, e.g. "USD"
    quote: str                      # what the client is billed in, e.g. "SYP"
    rate: float                     # 1 anchor = <rate> quote
    captured_at: str = ""           # ISO-8601, the moment the rate was pinned
    source_ar: str = ""             # bank / exchange office / central bank bulletin

    @classmethod
    def capture(cls, anchor: str, quote: str, rate: float, source_ar: str = "",
                captured_at: Optional[str] = None) -> "FxRate":
        return cls(anchor=str(anchor).upper(), quote=str(quote).upper(),
                   rate=_num(rate), captured_at=captured_at or _now_iso(),
                   source_ar=source_ar)

    def validate(self) -> None:
        if not self.anchor or not self.quote:
            raise FxError("سعر الصرف ناقص: يجب تحديد عملة المرساة وعملة الفوترة. "
                          "| fx needs both anchor and quote currency")
        if _num(self.rate) <= 0:
            raise FxError(f"سعر صرف غير صالح ({self.rate!r}) — رفض التسعير بسعر صفر أو سالب. "
                          f"| non-positive fx rate; refusing to price")
        if not str(self.captured_at or "").strip():
            raise FxError("سعر الصرف بلا ختم زمني — لا يمكن إعادة إنتاج العرض لاحقاً. "
                          "| fx rate has no captured_at timestamp")

    @property
    def label_ar(self) -> str:
        return (f"1 {_cur_ar(self.anchor)} = {_rate_fmt(self.rate)} {_cur_ar(self.quote)}")

    @property
    def token(self) -> str:
        """Compact machine-readable provenance, carried inside ``rate_source``."""
        return f"fx:{self.anchor}/{self.quote}={_plain(self.rate)}@{self.captured_at}"

    def to_dict(self) -> dict:
        return {"anchor": self.anchor, "quote": self.quote, "rate": _num(self.rate),
                "captured_at": self.captured_at, "source_ar": self.source_ar}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["FxRate"]:
        if not d:
            return None
        return cls(anchor=str(d.get("anchor", "")), quote=str(d.get("quote", "")),
                   rate=_num(d.get("rate")), captured_at=str(d.get("captured_at", "")),
                   source_ar=str(d.get("source_ar", "")))


# --------------------------------------------------------------------------- #
# Loading + validation                                                          #
# --------------------------------------------------------------------------- #
# Caches are keyed by a filesystem signature, never by path alone. A cache that
# survives an edit would hide the one thing this module exists to detect: a
# published price book changed underneath the quotes that cite it.
_BOOK_CACHE: dict[str, tuple[tuple, PriceBook]] = {}
_INDEX_CACHE: dict[str, tuple[tuple, list[PriceBook]]] = {}


def _file_sig(path: str) -> tuple:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _dir_sig(dir_path: str) -> tuple:
    return tuple(
        (name,) + _file_sig(os.path.join(dir_path, name))
        for name in sorted(os.listdir(dir_path))
        if name.lower().endswith((".yaml", ".yml")) and not name.startswith("_")
    )


def validate_book(doc: dict) -> list[str]:
    """Structural checks on a raw book document. Returns problems (empty == sound)."""
    out: list[str] = []
    if not isinstance(doc, dict):
        return ["ملف الأسعار ليس قاموساً. | book is not a mapping"]

    meta = doc.get("meta")
    if not isinstance(meta, dict):
        return ["ملف الأسعار ينقصه قسم meta. | book has no 'meta' mapping"]

    schema = str(meta.get("schema") or BOOK_SCHEMA)
    if schema != BOOK_SCHEMA:
        out.append(f"مخطط غير مدعوم «{schema}» (المتوقع {BOOK_SCHEMA}). | unsupported schema")
    for f in REQUIRED_META:
        if meta.get(f) is None:
            out.append(f"meta ينقصه الحقل «{f}». | missing meta field {f}")
    if not str(meta.get("currency") or "").strip():
        out.append("meta.currency فارغ — لا يمكن إصدار عرض بلا عملة. | empty currency")
    if not isinstance(meta.get("is_placeholder"), bool):
        out.append("meta.is_placeholder يجب أن يكون true أو false صراحةً — "
                   "الغموض هنا يعني إرسال أسعار وهمية للعميل. | is_placeholder must be a bool")

    rates = doc.get("rates")
    if not isinstance(rates, dict) or not rates:
        out.append("ملف الأسعار لا يحتوي على قسم rates. | no 'rates' mapping")
        return out

    for key, r in rates.items():
        if not isinstance(r, dict):
            out.append(f"{key}: ليس قاموساً. | not a mapping")
            continue
        for f in REQUIRED_RATE_FIELDS:
            if f not in r:
                out.append(f"{key}: ينقصه الحقل «{f}». | missing field {f}")
        if r.get("unit") not in UNITS:
            out.append(f"{key}: وحدة غير معروفة «{r.get('unit')}». | unknown unit")
        for f in ("material_rate", "labor_rate"):
            v = r.get(f)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                out.append(f"{key}.{f}: يجب أن يكون رقماً ({v!r}). | must be numeric")
            elif float(v) < 0:
                out.append(f"{key}.{f}: قيمة سالبة ({v}). | negative rate")
    return out


def parse_book(doc: dict, path: str = "") -> PriceBook:
    """Turn a validated raw document into an immutable :class:`PriceBook`."""
    problems = validate_book(doc)
    if problems:
        where = path or "<inline>"
        raise BookError(f"{where}: أخطاء في ملف الأسعار:\n- " + "\n- ".join(problems))

    meta = dict(doc["meta"])
    defaults = dict(meta.get("defaults") or {})
    default_waste_in_rate = bool(defaults.get("waste_in_rate", True))
    fx_meta = dict(meta.get("fx") or {})

    rates: dict[str, Rate] = {}
    for key, r in dict(doc["rates"]).items():
        rates[str(key)] = Rate(
            rule_key=str(key),
            material_rate=_num(r.get("material_rate")),
            labor_rate=_num(r.get("labor_rate")),
            unit=str(r.get("unit")),
            waste_in_rate=bool(r.get("waste_in_rate", default_waste_in_rate)),
            note_ar=str(r.get("note_ar") or ""),
        )

    # The fingerprint covers exactly what moves money: the identity of the book and
    # every rate in it. Comments and prose notes are deliberately excluded.
    payload = {
        "id": str(meta.get("id")), "region": str(meta.get("region")),
        "currency": str(meta.get("currency")), "version": str(meta.get("version")),
        "effective_date": str(meta.get("effective_date")),
        "is_placeholder": bool(meta.get("is_placeholder")),
        "rates": {k: [v.material_rate, v.labor_rate, v.unit, v.waste_in_rate]
                  for k, v in sorted(rates.items())},
    }

    return PriceBook(
        id=str(meta.get("id")), region=str(meta.get("region")),
        currency=str(meta.get("currency")).upper(), version=str(meta.get("version")),
        effective_date=str(meta.get("effective_date")),
        source_note_ar=str(meta.get("source_note_ar") or ""),
        is_placeholder=bool(meta.get("is_placeholder")),
        rates=rates,
        is_anchor=bool(fx_meta.get("is_anchor", False)),
        requires_conversion=bool(fx_meta.get("requires_conversion", False)),
        fx_note_ar=str(fx_meta.get("note_ar") or ""),
        path=os.path.abspath(path) if path else "",
        fingerprint=_fingerprint(payload),
        meta=meta,
    )


def _read_book_file(path: str) -> PriceBook:
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise BookError(f"ملف الأسعار غير موجود: {p} | price book not found")
    sig = _file_sig(p)
    cached = _BOOK_CACHE.get(p)
    if cached is not None and cached[0] == sig:
        return cached[1]
    with open(p, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    book = parse_book(doc, p)
    _BOOK_CACHE[p] = (sig, book)
    return book


def list_books(dir_path: Optional[str] = None, refresh: bool = False) -> list[PriceBook]:
    """Every published book on disk, newest version first within each region.

    Discovery is by CONTENT, not by filename: the region and version come from the
    file's own ``meta``. A filename is a convenience for humans (``levant.yaml``
    for the working book, ``levant-v1.2.0.yaml`` for a frozen one); the file is
    the authority on what it is.
    """
    d = os.path.abspath(dir_path or PRICES_DIR)
    if not os.path.isdir(d):
        raise BookError(f"مجلد جداول الأسعار غير موجود: {d} | prices directory not found")
    if refresh:
        _BOOK_CACHE.clear()
        _INDEX_CACHE.pop(d, None)
    sig = _dir_sig(d)
    cached = _INDEX_CACHE.get(d)
    if cached is not None and cached[0] == sig:
        return list(cached[1])

    books: list[PriceBook] = []
    seen: dict[tuple[str, str], str] = {}
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith((".yaml", ".yml")) or name.startswith("_"):
            continue
        book = _read_book_file(os.path.join(d, name))
        ident = (book.region, book.version)
        if ident in seen:
            raise BookError(
                f"إصدار مكرر: «{book.region}» الإصدار {book.version} معرَّف في "
                f"{os.path.basename(seen[ident])} وفي {name} — جدول الأسعار المنشور لا "
                f"يُعدَّل، والإصدار الجديد ملف جديد بإصدار جديد. | duplicate region@version"
            )
        seen[ident] = book.path
        books.append(book)

    books.sort(key=lambda b: (b.region, _version_key(b.version)), reverse=False)
    _INDEX_CACHE[d] = (sig, books)
    return list(books)


def regions(dir_path: Optional[str] = None) -> list[str]:
    return sorted({b.region for b in list_books(dir_path)})


def list_versions(region: str, dir_path: Optional[str] = None) -> list[str]:
    """Every version published for a region, oldest first."""
    return [b.version for b in list_books(dir_path) if b.region == region]


def load_book(region: str, version: str = "latest",
              dir_path: Optional[str] = None, refresh: bool = False) -> PriceBook:
    """Load one region's price book at one version.

    ``version="latest"`` picks the highest version number for the region — the
    right default for a NEW quote. An OLD quote must always be reloaded at its own
    recorded version, which is why the version travels on :class:`Quote`.
    """
    books = [b for b in list_books(dir_path, refresh=refresh) if b.region == region]
    if not books:
        known = ", ".join(regions(dir_path)) or "—"
        raise BookError(f"لا يوجد جدول أسعار للمنطقة «{region}». المتاح: {known}. "
                        f"| no price book for region {region!r}")
    if version in (None, "", "latest"):
        return max(books, key=lambda b: _version_key(b.version))
    for b in books:
        if b.version == version:
            return b
    have = ", ".join(sorted((b.version for b in books), key=_version_key))
    raise BookError(f"المنطقة «{region}» لا تملك الإصدار «{version}». المتاح: {have}. "
                    f"| unknown version for region")


# --------------------------------------------------------------------------- #
# Rate lookup                                                                   #
# --------------------------------------------------------------------------- #
def get_rate(book: PriceBook, rule_key: str) -> Optional[Rate]:
    return book.rates.get(rule_key)


def missing_rates(lines: Iterable[BOQLine], book: PriceBook) -> list[str]:
    """The rule keys this BOQ needs and this book does not have. Sorted, unique."""
    return sorted({ln.rule_key for ln in lines if ln.rule_key not in book.rates})


def coverage(lines: Iterable[BOQLine], book: PriceBook) -> dict:
    """How much of a BOQ this book can actually price — the buyer's first question."""
    keys = sorted({ln.rule_key for ln in lines})
    missing = [k for k in keys if k not in book.rates]
    priced = len(keys) - len(missing)
    return {
        "rule_keys": len(keys),
        "priced": priced,
        "missing": missing,
        "pct": _r(100.0 * priced / len(keys), 1) if keys else 0.0,
    }


def _missing_raise(line: BOQLine, book: PriceBook) -> Rate:
    raise RateMissingError(
        f"لا يوجد سعر للبند «{line.name_ar}» (المفتاح {line.rule_key}) في جدول "
        f"«{book.id}» الإصدار {book.version} — رفض التسعير بصفر صامت، فالبند الناقص "
        f"يخرج من العرض ويدفعه المقاول من جيبه. | no rate for rule_key "
        f"{line.rule_key!r} in {book.rate_source}; refusing to price it at 0."
    )


def _missing_zero(line: BOQLine, book: PriceBook) -> Rate:
    return Rate(rule_key=line.rule_key, material_rate=0.0, labor_rate=0.0,
                unit=line.unit, waste_in_rate=True, is_missing=True,
                note_ar="بند بلا سعر في جدول الأسعار — أُدرج بقيمة صفر ووُسم للمراجعة.")


def _missing_skip(line: BOQLine, book: PriceBook) -> None:
    return None


# Policy code -> what to do when the book has no rate for a measured line. The
# default is "raise": a missing price is a missing scope, and a silent zero is the
# same failure mode as a silent zero quantity.
MISSING_RATE_POLICIES: dict[str, Callable[[BOQLine, PriceBook], Optional[Rate]]] = {
    "raise": _missing_raise,
    "zero": _missing_zero,
    "skip": _missing_skip,
}

MISSING_RATE_POLICIES_AR = {
    "raise": "رفض إصدار العرض إذا نقص سعر بند",
    "zero": "إدراج البند بصفر مع وسم واضح للمراجعة",
    "skip": "استبعاد البند من العرض وذكره في التحذيرات",
}


# --------------------------------------------------------------------------- #
# FX                                                                            #
# --------------------------------------------------------------------------- #
def _resolve_fx(book: PriceBook, fx: Optional[FxRate]) -> tuple[str, float, list[str]]:
    """(billing currency, multiplier, Arabic breakdown lines). Never invents a rate."""
    if fx is None:
        if book.requires_conversion:
            raise FxError(
                f"جدول «{book.id}» مخزَّن بعملة المرساة {_cur_ar(book.currency)} ويشترط "
                f"تحويلاً عند التسعير، ولم يُزوَّد سعر صرف — رفض افتراض سعر صرف من عندنا. "
                f"| book requires a captured FX rate; refusing to assume one."
            )
        return book.currency, 1.0, []

    fx.validate()
    if fx.anchor.upper() != book.currency.upper():
        raise FxError(
            f"سعر الصرف من {_cur_ar(fx.anchor)} بينما فئات الجدول مخزَّنة بـ"
            f"{_cur_ar(book.currency)} — تحويل بعملة خاطئة يعني رقماً خاطئاً بالكامل. "
            f"| fx anchor {fx.anchor!r} != book currency {book.currency!r}"
        )
    if fx.quote.upper() == book.currency.upper():
        return book.currency, 1.0, [
            f"عملة الفوترة هي نفسها عملة الجدول ({_cur_ar(book.currency)}) — لا تحويل."
        ]
    lines = [
        f"تحويل العملة: {fx.label_ar} — سعر مثبَّت بتاريخ {fx.captured_at}"
        + (f" (المصدر: {fx.source_ar})" if fx.source_ar else "")
        + ".",
        f"الفئات مخزَّنة بعملة المرساة {_cur_ar(book.currency)} وتُحوَّل إلى "
        f"{_cur_ar(fx.quote)} بهذا السعر وحده؛ أي تغيّر لاحق في الصرف لا يغيّر هذا العرض.",
    ]
    return fx.quote.upper(), _num(fx.rate), lines


def _rate_source(book: PriceBook, fx: Optional[FxRate], is_missing: bool = False) -> str:
    """The provenance stamped on the PricedLine: book id + version [+ fx + flags]."""
    parts = [book.rate_source]
    if fx is not None and fx.quote.upper() != book.currency.upper():
        parts.append(fx.token)
    if book.is_placeholder:
        parts.append("placeholder")
    if is_missing:
        parts.append("no-rate")
    return "|".join(parts)


# --------------------------------------------------------------------------- #
# Pricing one line                                                              #
# --------------------------------------------------------------------------- #
def _price_breakdown(line: BOQLine, rate: Rate, priced: PricedLine, book: PriceBook,
                     currency: str, fx_lines: list[str], fx: Optional[FxRate]) -> list[str]:
    """The Arabic money trail appended under the quantity trail."""
    unit_ar = UNITS_AR.get(line.unit, line.unit)
    cur_ar = _cur_ar(currency)
    mat_qty = _num(line.gross_qty) if priced.qty_basis == "gross" else _num(line.net_qty)
    basis_ar = ("كمية التوريد (شاملة الهدر)" if priced.qty_basis == "gross"
                else "الكمية الصافية المدفوعة في المستخلص")

    bd = [
        PRICING_MARK,
        f"مصدر الفئات: جدول «{book.id}» الإصدار {book.version} "
        f"(سريان {book.effective_date}، بصمة المحتوى {book.fingerprint}).",
    ]
    bd.extend(fx_lines)

    if rate.is_missing:
        bd.append("تحذير: لا يوجد سعر لهذا البند في الجدول — أُدرج بقيمة صفر. "
                  "لا يجوز إرسال العرض قبل تسعيره.")
    if book.is_placeholder:
        bd.append("تحذير: الفئات المستعملة أولية (PLACEHOLDER) وليست أسعار السوق — "
                  "للعرض الفني فقط، لا تُرسل للعميل.")
    if rate.note_ar:
        bd.append(f"ملاحظة الفئة: {rate.note_ar}")

    bd.append(f"  • فئة المواد = {_rate_fmt(priced.material_rate)} {cur_ar} / {unit_ar}")
    bd.append(f"  • فئة العمالة = {_rate_fmt(priced.labor_rate)} {cur_ar} / {unit_ar}")
    if priced.qty_basis == "gross":
        bd.append("الفئة لا تتضمن الهدر ⇒ تُسعَّر المواد على كمية التوريد لا على الصافي.")
    bd.append(f"المواد: {_fmt(mat_qty)} {unit_ar} ({basis_ar}) × "
              f"{_rate_fmt(priced.material_rate)} = {_money(priced.material_total)} {cur_ar}")
    bd.append(f"العمالة: {_fmt(_num(line.net_qty))} {unit_ar} (الصافي — لا أحد يركّب "
              f"الهدر) × {_rate_fmt(priced.labor_rate)} = "
              f"{_money(priced.labor_total)} {cur_ar}")
    bd.append(f"إجمالي البند = {_money(priced.material_total)} + "
              f"{_money(priced.labor_total)} = {_money(priced.total)} {cur_ar}")
    return bd


def price_one(line: BOQLine, book: PriceBook, *, fx: Optional[FxRate] = None,
              qty_basis: Optional[str] = None,
              on_missing: str = "raise") -> Optional[PricedLine]:
    """Put money on ONE measured line. Returns None only under ``on_missing="skip"``."""
    if on_missing not in MISSING_RATE_POLICIES:
        raise PriceError(f"سياسة غير معروفة للبنود بلا سعر: «{on_missing}». المتاح: "
                         f"{', '.join(MISSING_RATE_POLICIES)}. | unknown on_missing policy")

    base = _strip_pricing(line)
    rate = get_rate(book, base.rule_key)
    if rate is None:
        rate = MISSING_RATE_POLICIES[on_missing](base, book)
        if rate is None:
            return None

    if not rate.is_missing and rate.unit != base.unit:
        raise UnitMismatchError(
            f"البند «{base.name_ar}» ({base.rule_key}) مقيس بوحدة "
            f"{UNITS_AR.get(base.unit, base.unit)} بينما فئة الجدول بوحدة "
            f"{rate.unit_ar} — ضرب كمية بوحدة مختلفة يُنتج رقماً بلا معنى، وقد يكون "
            f"البند قد بدّل وحدته بقاعدة unit_switch. | unit mismatch: line "
            f"{base.unit!r} vs rate {rate.unit!r}"
        )

    currency, factor, fx_lines = _resolve_fx(book, fx)
    # Rates are converted and rounded BEFORE the multiplication, so the rate the
    # client reads on the quote is the rate that produced the total.
    mat_rate = _r(_num(rate.material_rate) * factor, RATE_ND)
    lab_rate = _r(_num(rate.labor_rate) * factor, RATE_ND)
    basis = qty_basis or ("net" if rate.waste_in_rate else "gross")
    if basis not in ("net", "gross"):
        raise PriceError(f"أساس كمية غير معروف: «{basis}» (المتاح: net / gross). "
                         f"| unknown qty_basis")

    priced = price_line(base, mat_rate, lab_rate, currency,
                        _rate_source(book, fx, rate.is_missing), basis)
    priced.line = replace(
        base, breakdown=list(base.breakdown) + _price_breakdown(
            base, rate, priced, book, currency, fx_lines, fx))
    return priced


def price(lines: list[BOQLine], book: PriceBook, *, fx: Optional[FxRate] = None,
          qty_basis: Optional[str] = None,
          on_missing: str = "raise") -> list[PricedLine]:
    """Price a whole BOQ against one book. Deterministic; input order preserved.

    The same ``lines`` priced against ``load_book("gulf")`` instead of
    ``load_book("levant")`` is the whole region-swap feature: the geometry never
    moves, only the money does.
    """
    out: list[PricedLine] = []
    for ln in lines:
        pl = price_one(ln, book, fx=fx, qty_basis=qty_basis, on_missing=on_missing)
        if pl is not None:
            out.append(pl)
    return out


# --------------------------------------------------------------------------- #
# Project-level totals                                                          #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuoteTerms:
    """The commercial cascade, as fractions (0.16 == 16%), like ``waste_pct``.

    Every percentage defaults to zero: the office states its margins explicitly or
    the quote carries none. ``bases`` overrides what a given step is applied to
    (see :data:`TOTAL_BASES`); ``round_to`` rounds only the grand total, and the
    rounding difference is printed rather than absorbed.
    """

    overhead_pct: float = 0.0
    profit_pct: float = 0.0
    contingency_pct: float = 0.0
    discount_pct: float = 0.0
    tax_pct: float = 0.0
    bases: dict = field(default_factory=dict)
    round_to: float = 0.0
    note_ar: str = ""

    def pct_of(self, code: str) -> float:
        spec = TOTAL_STEPS_BY_CODE.get(code)
        return _num(getattr(self, spec["field"], 0.0)) if spec else 0.0

    def base_of(self, code: str) -> str:
        spec = TOTAL_STEPS_BY_CODE.get(code) or {}
        chosen = str((self.bases or {}).get(code) or spec.get("base") or "running")
        if chosen not in TOTAL_BASES:
            raise PriceError(f"أساس احتساب غير معروف «{chosen}» للخطوة «{code}». المتاح: "
                             f"{', '.join(TOTAL_BASES)}. | unknown base for step")
        return chosen

    def to_dict(self) -> dict:
        return {"overhead_pct": _num(self.overhead_pct), "profit_pct": _num(self.profit_pct),
                "contingency_pct": _num(self.contingency_pct),
                "discount_pct": _num(self.discount_pct), "tax_pct": _num(self.tax_pct),
                "bases": dict(self.bases or {}), "round_to": _num(self.round_to),
                "note_ar": self.note_ar}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "QuoteTerms":
        d = d or {}
        return cls(overhead_pct=_num(d.get("overhead_pct")),
                   profit_pct=_num(d.get("profit_pct")),
                   contingency_pct=_num(d.get("contingency_pct")),
                   discount_pct=_num(d.get("discount_pct")),
                   tax_pct=_num(d.get("tax_pct")),
                   bases=dict(d.get("bases") or {}), round_to=_num(d.get("round_to")),
                   note_ar=str(d.get("note_ar") or ""))


@dataclass
class QuoteStep:
    """One line of the commercial cascade, with the arithmetic that produced it."""

    code: str
    label_ar: str
    pct: float
    base_code: str
    base_amount: float
    amount: float
    running_total: float

    def to_dict(self) -> dict:
        return {"code": self.code, "label_ar": self.label_ar, "pct": _num(self.pct),
                "base_code": self.base_code, "base_amount": _num(self.base_amount),
                "amount": _num(self.amount), "running_total": _num(self.running_total)}

    @classmethod
    def from_dict(cls, d: dict) -> "QuoteStep":
        return cls(code=str(d.get("code", "")), label_ar=str(d.get("label_ar", "")),
                   pct=_num(d.get("pct")), base_code=str(d.get("base_code", "running")),
                   base_amount=_num(d.get("base_amount")), amount=_num(d.get("amount")),
                   running_total=_num(d.get("running_total")))


def apply_terms(subtotal: float, terms: Optional[QuoteTerms] = None,
                currency: str = "") -> tuple[list[QuoteStep], float, list[str]]:
    """Run the commercial cascade over a subtotal.

    Returns ``(steps, grand_total, breakdown_ar)``. Pure arithmetic — the same
    audit discipline as a BOQ line, because a client argues about the overhead
    percentage far more often than about a square metre.
    """
    terms = terms or QuoteTerms()
    cur_ar = _cur_ar(currency)
    sub = _r(subtotal)
    running = sub
    steps: list[QuoteStep] = []
    bd = [f"مجموع البنود (المجموع الأولي) = {_money(sub)} {cur_ar}"]

    for spec in TOTAL_STEPS:
        code = spec["code"]
        pct = terms.pct_of(code)
        if pct == 0.0:
            continue
        base_code = terms.base_of(code)
        base_amount = _r(TOTAL_BASES[base_code](sub, running))
        amount = _r(base_amount * pct * _num(spec["sign"]))
        running = _r(running + amount)
        steps.append(QuoteStep(code=code, label_ar=spec["label_ar"], pct=pct,
                               base_code=base_code, base_amount=base_amount,
                               amount=amount, running_total=running))
        bd.append(
            f"{spec['label_ar']} {_pct(pct * 100)} على {TOTAL_BASES_AR[base_code]} "
            f"({_money(base_amount)}) = {_money(amount)} ⇒ المتجمِّع {_money(running)} {cur_ar}"
        )

    grand = running
    step = _num(terms.round_to)
    if step > 0:
        rounded = _r(round(grand / step) * step)
        bd.append(f"تقريب الإجمالي إلى أقرب {_fmt(step)} {cur_ar}: {_money(grand)} ⇒ "
                  f"{_money(rounded)} (فرق {_money(_r(rounded - grand))}).")
        grand = rounded

    bd.append(f"الإجمالي النهائي = {_money(grand)} {cur_ar}")
    if terms.note_ar:
        bd.append(f"ملاحظة تجارية: {terms.note_ar}")
    return steps, grand, bd


# --------------------------------------------------------------------------- #
# The quote                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    """A priced BOQ, plus everything needed to rebuild it byte-for-byte later.

    ``book_id`` + ``book_version`` + ``book_fingerprint`` + ``fx`` + ``terms`` +
    ``qty_basis`` is a complete recipe: :func:`verify_reproducible` reloads that
    exact book and recomputes, and reports in Arabic if a single fils has moved.
    """

    project_id: str = ""
    project_name: str = ""
    client_name: str = ""
    region: str = ""
    currency: str = ""
    book_id: str = ""
    book_version: str = ""
    book_fingerprint: str = ""
    book_effective_date: str = ""
    is_placeholder: bool = False
    fx: Optional[FxRate] = None
    terms: QuoteTerms = field(default_factory=QuoteTerms)
    qty_basis: Optional[str] = None
    on_missing: str = "raise"
    measured_lines: int = 0         # how many BOQ lines went IN (vs len(lines) out)
    lines: list[PricedLine] = field(default_factory=list)
    subtotal: float = 0.0
    steps: list[QuoteStep] = field(default_factory=list)
    grand_total: float = 0.0
    breakdown: list[str] = field(default_factory=list)
    warnings_ar: list[str] = field(default_factory=list)
    priced_at: str = ""

    @property
    def rate_source(self) -> str:
        return f"{self.book_id}@{self.book_version}"

    @property
    def currency_ar(self) -> str:
        return _cur_ar(self.currency)

    @property
    def dropped_lines(self) -> int:
        """Measured lines that never reached the quote (``on_missing="skip"``)."""
        return max(0, int(self.measured_lines or 0) - len(self.lines))

    @property
    def is_client_ready(self) -> bool:
        return not client_block_reason_ar(self)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id, "project_name": self.project_name,
            "client_name": self.client_name, "region": self.region,
            "currency": self.currency, "book_id": self.book_id,
            "book_version": self.book_version, "book_fingerprint": self.book_fingerprint,
            "book_effective_date": self.book_effective_date,
            "is_placeholder": bool(self.is_placeholder),
            "fx": (self.fx.to_dict() if self.fx else None),
            "terms": self.terms.to_dict(), "qty_basis": self.qty_basis,
            "on_missing": self.on_missing,
            "measured_lines": int(self.measured_lines or 0),
            "lines": [pl.to_dict() for pl in self.lines],
            "subtotal": _num(self.subtotal),
            "steps": [s.to_dict() for s in self.steps],
            "grand_total": _num(self.grand_total),
            "breakdown": list(self.breakdown),
            "warnings_ar": list(self.warnings_ar),
            "priced_at": self.priced_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Quote":
        return cls(
            project_id=str(d.get("project_id", "")), project_name=str(d.get("project_name", "")),
            client_name=str(d.get("client_name", "")), region=str(d.get("region", "")),
            currency=str(d.get("currency", "")), book_id=str(d.get("book_id", "")),
            book_version=str(d.get("book_version", "")),
            book_fingerprint=str(d.get("book_fingerprint", "")),
            book_effective_date=str(d.get("book_effective_date", "")),
            is_placeholder=bool(d.get("is_placeholder", False)),
            fx=FxRate.from_dict(d.get("fx")),
            terms=QuoteTerms.from_dict(d.get("terms")),
            qty_basis=(d.get("qty_basis") or None),
            on_missing=str(d.get("on_missing") or "raise"),
            measured_lines=int(_num(d.get("measured_lines"))),
            lines=[PricedLine.from_dict(x) for x in (d.get("lines") or [])],
            subtotal=_num(d.get("subtotal")),
            steps=[QuoteStep.from_dict(x) for x in (d.get("steps") or [])],
            grand_total=_num(d.get("grand_total")),
            breakdown=list(d.get("breakdown") or []),
            warnings_ar=list(d.get("warnings_ar") or []),
            priced_at=str(d.get("priced_at") or ""),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Quote":
        return cls.from_dict(json.loads(text))


def _quote_warnings(book: PriceBook, lines: list[BOQLine], priced: list[PricedLine],
                    fx: Optional[FxRate], priced_at: str, on_missing: str) -> list[str]:
    """Arabic warnings. Like ``Space.validate``: warns loudly, never crashes."""
    out: list[str] = []
    if book.is_placeholder:
        out.append(book.placeholder_warning_ar or
                   "جدول الأسعار المستعمل أولي (PLACEHOLDER) ولا يمثل أسعار السوق — "
                   "لا يجوز إرسال هذا العرض إلى العميل قبل إدخال الفئات الحقيقية.")
    gone = len(lines) - len(priced)
    if gone > 0:
        out.append(f"{gone} بند/بنود استُبعدت من العرض لعدم وجود سعر لها في الجدول "
                   f"(سياسة «{MISSING_RATE_POLICIES_AR.get(on_missing, on_missing)}») — "
                   f"البند المستبعَد يبقى واجباً على المقاول تنفيذه.")
    zeroed = [pl.line.name_ar for pl in priced if "no-rate" in (pl.rate_source or "")]
    if zeroed:
        out.append("بنود أُدرجت بقيمة صفر لعدم وجود فئة لها: " + "، ".join(zeroed) + ".")

    if fx is not None:
        age = _age_hours(fx.captured_at, priced_at)
        if age is not None and age > FX_STALE_HOURS:
            out.append(f"سعر الصرف المستعمل مثبَّت منذ {age / 24.0:.1f} يوم "
                       f"({fx.captured_at}) — في سوق متقلب يُنصح بتثبيت سعر اليوم "
                       f"قبل إصدار العرض.")
        if age is not None and age < 0:
            out.append("سعر الصرف مؤرَّخ بعد تاريخ التسعير — راجع الختم الزمني.")
    elif book.is_anchor:
        out.append(f"الفئات مخزَّنة بعملة المرساة {_cur_ar(book.currency)} ولم يُطلب تحويل — "
                   f"العرض صادر بعملة المرساة نفسها.")

    age_days = _age_hours(book.effective_date, priced_at)
    if age_days is not None:
        days = age_days / 24.0
        if days > BOOK_STALE_DAYS:
            out.append(f"جدول الأسعار «{book.id}» ساري منذ {days:.0f} يوم "
                       f"({book.effective_date}) — يُنصح بإصدار نسخة محدَّثة.")
        elif days < -1.0:
            out.append(f"تاريخ سريان جدول الأسعار ({book.effective_date}) في المستقبل — "
                       f"تأكد أنك تسعّر بالجدول الصحيح.")
    return out


def quote(project: Optional[Project], lines: list[BOQLine], book: PriceBook, *,
          terms: Optional[QuoteTerms] = None, fx: Optional[FxRate] = None,
          qty_basis: Optional[str] = None, on_missing: str = "raise",
          priced_at: Optional[str] = None) -> Quote:
    """Price a BOQ and run the project-level cascade — the whole deliverable.

    ``priced_at`` is injectable so a quote can be regenerated with the same stamp
    and compared byte-for-byte; only the default reads the clock.
    """
    terms = terms or QuoteTerms()
    stamp = priced_at or _now_iso()
    priced = price(lines, book, fx=fx, qty_basis=qty_basis, on_missing=on_missing)

    currency, _, _ = _resolve_fx(book, fx)
    subtotal = _r(sum(_num(pl.total) for pl in priced))
    steps, grand, totals_bd = apply_terms(subtotal, terms, currency)

    head = [
        f"عرض سعر — المشروع «{project.name if project else ''}»"
        + (f" — العميل: {project.client_name}" if project and project.client_name else ""),
        f"جدول الأسعار: «{book.id}» الإصدار {book.version} — منطقة {book.region} — "
        f"سريان {book.effective_date} — بصمة المحتوى {book.fingerprint}.",
        f"مصدر الفئات: {book.source_note_ar}" if book.source_note_ar else "",
        f"عدد البنود المسعّرة: {len(priced)} من أصل {len(lines)} بند مقيس.",
        f"عملة العرض: {_cur_ar(currency)}.",
        f"تاريخ التسعير: {stamp}.",
    ]
    if fx is not None and fx.quote.upper() != book.currency.upper():
        head.append(f"سعر الصرف المثبَّت: {fx.label_ar} بتاريخ {fx.captured_at}"
                    + (f" (المصدر: {fx.source_ar})" if fx.source_ar else "") + ".")
    head.append("كل بند يحمل بيانه الحسابي كاملاً: الكمية ثم الفئة ثم الحاصل.")

    q = Quote(
        project_id=(project.id if project else ""),
        project_name=(project.name if project else ""),
        client_name=(project.client_name if project else ""),
        region=book.region, currency=currency, book_id=book.id, book_version=book.version,
        book_fingerprint=book.fingerprint, book_effective_date=book.effective_date,
        is_placeholder=book.is_placeholder, fx=fx, terms=terms, qty_basis=qty_basis,
        on_missing=on_missing, measured_lines=len(lines), lines=priced,
        subtotal=subtotal, steps=steps, grand_total=grand,
        breakdown=[t for t in head if t] + totals_bd,
        warnings_ar=_quote_warnings(book, lines, priced, fx, stamp, on_missing),
        priced_at=stamp,
    )
    return q


# --------------------------------------------------------------------------- #
# Guards: what may and may not reach a client                                   #
# --------------------------------------------------------------------------- #
def client_block_reason_ar(q: Quote) -> str:
    """Why this quote must NOT be sent, in Arabic. Empty string == it may be sent."""
    if q.is_placeholder:
        return ("هذا العرض مبني على جدول أسعار أولي (PLACEHOLDER) لا يمثل أسعار السوق. "
                "أدخل فئات المكتب الحقيقية وأصدر نسخة جديدة من الجدول قبل الإرسال.")
    zeroed = [pl.line.name_ar for pl in q.lines if "no-rate" in (pl.rate_source or "")]
    if zeroed:
        return ("العرض يحتوي على بنود بلا فئة سعرية أُدرجت بصفر: "
                + "، ".join(zeroed) + ". سعّرها أو احذفها قبل الإرسال.")
    if q.dropped_lines:
        return (f"{q.dropped_lines} بند/بنود مقيسة أُسقطت من العرض لعدم وجود سعر لها. "
                f"البند المُسقَط يبقى واجب التنفيذ ويدفعه المقاول من جيبه — سعّره أو "
                f"استبعده من نطاق العمل صراحةً قبل الإرسال.")
    if not q.lines:
        return "العرض لا يحتوي على أي بند مسعّر."
    # A zero quantity the ENGINE flagged is «not measured», not «no work»: a room whose
    # height was entered as 0, a stair with no steps_count, a follower that never found
    # its parent. It costs nothing on paper and everything on site, so it blocks the quote
    # exactly like a missing rate. A zero the engine chose deliberately and explained
    # (a threshold charged to the adjoining space, a skirting replaced by cladding) is
    # printed but does not block — it is a measurement, not a hole.
    zero = [f"«{pl.line.name_ar}» ({pl.line.code}) في الفراغ «{pl.line.space_name}»"
            for pl in q.lines if _num(pl.line.net_qty) <= 0
            and any(WARN_PREFIX in b for b in (pl.line.breakdown or []))]
    if zero:
        return ("العرض يحتوي على بنود كميتها الصافية صفر: " + "، ".join(zero[:6])
                + (" وغيرها" if len(zero) > 6 else "")
                + ". الصفر هنا معناه «غير مقيس» لا «لا يوجد عمل» — أصلح الحصر أو استبعد "
                  "البند من نطاق العمل صراحةً قبل الإرسال.")
    return ""


def assert_client_ready(q: Quote) -> None:
    """Raise :class:`PlaceholderRatesError` if this quote may not leave the office."""
    reason = client_block_reason_ar(q)
    if reason:
        raise PlaceholderRatesError(
            reason + " | refusing to emit a client-facing quote: "
            f"placeholder={q.is_placeholder}, source={q.rate_source}"
        )


# --------------------------------------------------------------------------- #
# Reproducibility                                                               #
# --------------------------------------------------------------------------- #
def verify_reproducible(q: Quote, dir_path: Optional[str] = None) -> list[str]:
    """Recompute an old quote from its own recorded recipe and report differences.

    Returns Arabic problems; an empty list means the quote is reproducible to the
    fils from the files on disk. A fingerprint mismatch is the loud one: it means
    a published price book was edited in place instead of being re-versioned.
    """
    out: list[str] = []
    try:
        book = load_book(q.region, q.book_version, dir_path)
    except BookError as exc:
        return [f"تعذّر تحميل جدول الأسعار الأصلي ({q.rate_source}): {exc}"]

    if book.id != q.book_id:
        out.append(f"مُعرّف الجدول تغيّر: العرض صادر عن «{q.book_id}» والملف الآن "
                   f"«{book.id}».")
    if q.book_fingerprint and book.fingerprint != q.book_fingerprint:
        out.append(f"بصمة الجدول تغيّرت: العرض بُني على {q.book_fingerprint} والملف "
                   f"الحالي {book.fingerprint} — جدول أسعار منشور عُدِّل في مكانه بدل "
                   f"إصدار نسخة جديدة، وهذا يكسر إمكانية إعادة إنتاج العروض القديمة.")

    lines = [_strip_pricing(pl.line) for pl in q.lines]
    try:
        again = quote(None, lines, book, terms=q.terms, fx=q.fx, qty_basis=q.qty_basis,
                      on_missing=q.on_missing, priced_at=q.priced_at)
    except PriceError as exc:
        return out + [f"تعذّرت إعادة التسعير بنفس الوصفة: {exc}"]

    if _r(again.subtotal) != _r(q.subtotal):
        out.append(f"المجموع الأولي لا يتطابق: المسجَّل {_money(q.subtotal)} "
                   f"والمعاد حسابه {_money(again.subtotal)}.")
    if _r(again.grand_total) != _r(q.grand_total):
        out.append(f"الإجمالي النهائي لا يتطابق: المسجَّل {_money(q.grand_total)} "
                   f"والمعاد حسابه {_money(again.grand_total)}.")
    for old, new in zip(q.lines, again.lines):
        if _r(old.total) != _r(new.total):
            out.append(f"البند «{old.line.name_ar}» ({old.line.rule_key}): المسجَّل "
                       f"{_money(old.total)} والمعاد حسابه {_money(new.total)}.")
    if len(again.lines) != len(q.lines):
        out.append(f"عدد البنود تغيّر: {len(q.lines)} ⇒ {len(again.lines)}.")
    return out
