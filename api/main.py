# -*- coding: utf-8 -*-
"""HTTP surface over the deterministic BOQ core.

This layer computes NOTHING. Every quantity comes from ``core.boq_engine``, every
rate from ``core.pricing``, every printed document from ``core.proposal``. What it
adds is three things the core deliberately does not do:

* **Persistence** — via ``core.schema.Project.to_dict()`` only (see ``storage.py``).
* **Arabic-first failure** — a malformed payload is refused with a sentence a
  surveyor can act on, BEFORE it reaches the core, so nobody ever sees a 500.
* **Visibility of doubt** — every take-off and every quote carries a ``warnings``
  array, and a quote priced from a placeholder book comes back ``200`` with
  ``blocked_reason_ar`` set. The numbers are still shown; what is refused is the
  pretence that they are sendable. Hiding a doubtful number is how an office signs
  a contract it cannot deliver.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

# Allow `uvicorn api.main:app` from anywhere, not just the repo root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from contextlib import asynccontextmanager                    # noqa: E402

from fastapi import Body, FastAPI, Query                      # noqa: E402
from fastapi.middleware.cors import CORSMiddleware            # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse      # noqa: E402

import core                                                    # noqa: E402
from core import boq_engine, pricing, proposal, rules_loader   # noqa: E402
from core.schema import (OPENING_KINDS, OPENING_KINDS_AR, SPACE_TYPES,  # noqa: E402
                         SPACE_TYPES_AR, SURFACES, SURFACES_AR, UNITS_AR, Project)

from api import storage                                        # noqa: E402
from api.errors import ApiError, install_handlers              # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("engeneer.api")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Fail loudly at boot, not on the first request: the database file must exist
    and the rules file must parse before anyone can ask for a quantity."""
    path = storage.init_db()
    count = len(rules_loader.load_rules())
    log.info("قاعدة البيانات جاهزة: %s", path)
    log.info("قواعد الحصر المحمّلة: %d", count)
    yield


app = FastAPI(
    title="engeneer — واجهة حصر وتسعير أعمال التشطيبات",
    description="واجهة HTTP فوق نواة الحصر الحتمية. كل كمية قابلة للتتبع، "
                "وكل رقم مشكوك فيه ظاهر للمستخدم.",
    version=core.__version__,
    lifespan=lifespan,
)

# The dev frontend runs on whatever port Vite/Next happened to grab.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_handlers(app)


# --------------------------------------------------------------------------- #
# Payload validation — Arabic sentences, raised BEFORE the core sees anything   #
# --------------------------------------------------------------------------- #
def _bad(message_ar: str, details: Any = None) -> "ApiError":
    return ApiError("VALIDATION", message_ar, details=details)


def _as_dict(v: Any, what: str) -> dict:
    if not isinstance(v, dict):
        raise _bad(f"{what} يجب أن يكون كائن JSON (مفاتيح وقيم)، والمُرسَل من نوع آخر.")
    return v


def _num(v: Any) -> Optional[float]:
    """A real, finite number — or None. A numeric STRING is accepted (forms send text)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if f == f and abs(f) != float("inf") else None


def _text(v: Any) -> str:
    return str(v).strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _space_label(space: dict, index: int) -> str:
    name = _text(space.get("name")) or _text(space.get("id"))
    return f"«{name}»" if name else f"رقم {index + 1}"


def _validate_polygon(poly: Any, label: str) -> None:
    if poly is None:
        return
    if not isinstance(poly, (list, tuple)):
        raise _bad(f"الغرفة {label}: المضلع يجب أن يكون قائمة نقاط [[x, y], ...].")
    for i, pt in enumerate(poly):
        if not isinstance(pt, (list, tuple)) or len(pt) < 2 \
                or _num(pt[0]) is None or _num(pt[1]) is None:
            raise _bad(f"الغرفة {label}: النقطة رقم {i + 1} في المضلع ليست إحداثيات "
                       f"صالحة [x, y] بأرقام.")
    if 0 < len(poly) < 3:
        raise _bad(f"الغرفة {label}: المضلع يحتوي على {len(poly)} نقطة فقط — "
                   f"يلزم ثلاث نقاط على الأقل لحساب مساحة.")


def _validate_walls(space: dict, label: str) -> float:
    walls = space.get("walls") or []
    if not isinstance(walls, (list, tuple)):
        raise _bad(f"الغرفة {label}: قائمة الجدران يجب أن تكون مصفوفة.")
    positive_length = 0.0
    for wi, w in enumerate(walls):
        if not isinstance(w, dict):
            raise _bad(f"الغرفة {label}: الجدار رقم {wi + 1} ليس كائن JSON.")
        wid = _text(w.get("id")) or f"رقم {wi + 1}"
        for field, ar in (("length_m", "الطول"), ("height_m", "الارتفاع")):
            if field in w and _num(w.get(field)) is None:
                raise _bad(f"الغرفة {label} الجدار «{wid}»: {ar} المُدخل ليس رقماً صالحاً.")
        positive_length += max(0.0, _num(w.get("length_m")) or 0.0)

        openings = w.get("openings") or []
        if not isinstance(openings, (list, tuple)):
            raise _bad(f"الغرفة {label} الجدار «{wid}»: قائمة الفتحات يجب أن تكون مصفوفة.")
        for oi, o in enumerate(openings):
            if not isinstance(o, dict):
                raise _bad(f"الغرفة {label} الجدار «{wid}»: الفتحة رقم {oi + 1} "
                           f"ليست كائن JSON.")
            oid = _text(o.get("id")) or f"رقم {oi + 1}"
            kind = _text(o.get("kind"))
            if kind not in OPENING_KINDS:
                known = "، ".join(f"{k} ({OPENING_KINDS_AR[k]})" for k in OPENING_KINDS)
                raise _bad(f"الغرفة {label} الفتحة «{oid}»: نوع الفتحة «{kind}» غير "
                           f"معروف. الأنواع المتاحة: {known}.")
            for field, ar in (("width_m", "العرض"), ("height_m", "الارتفاع")):
                if _num(o.get(field)) is None:
                    raise _bad(f"الغرفة {label} الفتحة «{oid}»: {ar} المُدخل ليس رقماً "
                               f"صالحاً — الفتحة بلا أبعاد لا يمكن خصمها ولا حصر كشفاتها.")
            if "count" in o and _num(o.get("count")) is None:
                raise _bad(f"الغرفة {label} الفتحة «{oid}»: العدد المُدخل ليس رقماً صالحاً.")
    return positive_length


def _validate_finishes(space: dict, label: str, rules: dict) -> None:
    finishes = space.get("finishes") or []
    if not isinstance(finishes, (list, tuple)):
        raise _bad(f"الغرفة {label}: قائمة التشطيبات يجب أن تكون مصفوفة.")
    for fi, f in enumerate(finishes):
        if not isinstance(f, dict):
            raise _bad(f"الغرفة {label}: التشطيب رقم {fi + 1} ليس كائن JSON.")
        surface = _text(f.get("surface"))
        system = _text(f.get("system"))
        if surface not in SURFACES:
            known = "، ".join(f"{s} ({SURFACES_AR[s]})" for s in SURFACES)
            raise _bad(f"الغرفة {label}: التشطيب «{system}» مُسنَد إلى سطح «{surface}» "
                       f"غير معروف. الأسطح المتاحة: {known}.")
        if not system:
            raise _bad(f"الغرفة {label}: أحد التشطيبات بلا نظام تشطيب محدَّد "
                       f"(الحقل system) على سطح «{SURFACES_AR[surface]}».")
        if system not in rules:
            raise _bad(f"الغرفة {label}: نظام التشطيب «{system}» غير موجود في ملف قواعد "
                       f"الحصر — اختر نظاماً من القائمة المتاحة في /api/rules.")
        if f.get("spec") is not None and not isinstance(f.get("spec"), dict):
            raise _bad(f"الغرفة {label}: بارامترات المواصفة للتشطيب «{system}» يجب أن "
                       f"تكون كائن JSON (مفتاح: قيمة).")


def validate_project_doc(doc: Any, notes: Optional[list] = None) -> Project:
    """Refuse a MALFORMED document with an Arabic sentence, then hand the core a
    document it can read.

    "Malformed" is deliberately narrower than "incomplete". A room whose dimensions
    have not been typed yet is not malformed — it is the normal intermediate state of
    a twenty-minute survey, and it is the state the project is in for the whole
    minute between «إضافة فراغ» and the first measurement. Refusing to SAVE it would
    make every autosave fail from the moment a room is added, and both the take-off
    and the quote refuse to run on a project whose last save failed. So a room with
    no dimensions is saved, and the doubt travels back in ``notes`` instead: the
    take-off repeats it, and the engine never invents a quantity from it.
    """
    doc = _as_dict(doc, "مستند المشروع")
    rules = rules_loader.load_rules()

    if not _text(doc.get("name")):
        raise _bad("اسم المشروع مطلوب — لا يمكن حفظ مشروع بلا اسم.")

    spaces = doc.get("spaces")
    if spaces is None:
        spaces = []
    if not isinstance(spaces, (list, tuple)):
        raise _bad("قائمة الفراغات يجب أن تكون مصفوفة — راجع بنية المستند المُرسَل.")

    for i, space in enumerate(spaces):
        if not isinstance(space, dict):
            raise _bad(f"الفراغ رقم {i + 1} ليس كائن JSON — راجع بنية المستند المُرسَل.")
        label = _space_label(space, i)

        if not _text(space.get("id")):
            raise _bad(f"الغرفة {label} بلا مُعرّف (id) — المُعرّف هو ما تُنسَب إليه "
                       f"الكميات في جدول الحصر.")
        if not _text(space.get("name")):
            raise _bad(f"الفراغ «{_text(space.get('id'))}» بلا اسم — الاسم هو ما يقرؤه "
                       f"العميل في جدول الكميات.")

        stype = _text(space.get("type"))
        if stype not in SPACE_TYPES:
            known = "، ".join(f"{k} ({SPACE_TYPES_AR[k]})" for k in SPACE_TYPES)
            raise _bad(f"الغرفة {label}: نوع الفراغ «{stype}» غير معروف. "
                       f"الأنواع المتاحة: {known}.")

        height = _num(space.get("height_m"))
        if height is None:
            raise _bad(f"الغرفة {label}: الارتفاع المُدخل ليس رقماً صالحاً.")
        if height <= 0:
            raise _bad(f"الغرفة {label}: الارتفاع {height:.2f} م غير صالح — كل بنود "
                       f"الجدران والأسقف ستكون صفراً، والصفر هنا خسارة مباشرة.")

        area = _num(space.get("floor_area_m2")) if space.get("floor_area_m2") is not None else None
        if space.get("floor_area_m2") is not None and area is None:
            raise _bad(f"الغرفة {label}: مساحة الأرضية المُدخلة ليست رقماً صالحاً.")

        _validate_polygon(space.get("polygon"), label)
        wall_length = _validate_walls(space, label)
        _validate_finishes(space, label, rules)

        has_polygon = isinstance(space.get("polygon"), (list, tuple)) and len(space["polygon"]) >= 3
        if not has_polygon and not (area and area > 0) and wall_length <= 0:
            # Saved, not refused — see the docstring. The sentence is the same one the
            # take-off will repeat, so the surveyor reads one wording, not two.
            if notes is not None:
                notes.append(f"الغرفة {label} بلا أبعاد: لا يوجد مضلع، ولا مساحة أرضية، "
                             f"ولا جدران بأطوال موجبة — حُفظت كما هي، ولن يُحصر فيها أي "
                             f"بند حتى تُدخل أبعادها.")

    return Project.from_dict(doc)


def _validate_overrides(raw: Any) -> dict:
    if raw is None:
        return {}
    overrides = _as_dict(raw, "التجاوزات (overrides)")
    for scope, value in overrides.items():
        if not isinstance(value, dict):
            raise _bad(f"التجاوز على النطاق «{scope}» يجب أن يكون كائناً مثل "
                       f'{{"waste_pct": 0.12}} — والمُرسَل من نوع آخر.')
        for field, val in value.items():
            if _num(val) is None:
                raise _bad(f"التجاوز على النطاق «{scope}»: قيمة «{field}» ليست رقماً "
                           f"صالحاً — تغيير الهدر أو حدّ الخصم يغيّر مالاً حقيقياً.")
    return overrides


# --------------------------------------------------------------------------- #
# Loading a project the same way everywhere                                     #
# --------------------------------------------------------------------------- #
def _warn_key(text: str) -> str:
    """The identity of a warning, for de-duplication only.

    The SAME sentence reaches this layer by three routes: ``warnings_of()`` (carrying
    ``WARN_PREFIX``), ``Quote.warnings_ar``, and ``Project.validate()`` (bare). Some
    also carry the English half of a core exception after a ``|``. Showing the same
    problem three times trains the surveyor to skim the warnings list, which is
    exactly how the one warning that mattered gets missed — so identity is compared
    on the Arabic sentence alone.
    """
    body = str(text or "").replace(rules_loader.WARN_PREFIX, "")
    body = body.partition("|")[0]
    return " ".join(body.split()).strip(" –-—:")


def _merge_warnings(*groups) -> list[str]:
    """Concatenate warning lists in order, keeping the first, fullest wording."""
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for w in group or []:
            text = str(w or "").strip()
            key = _warn_key(text)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


def _load(project_id: str) -> Project:
    found = storage.get_project(project_id)
    if found is None:
        raise ApiError("NOT_FOUND",
                       f"لا يوجد مشروع بالمُعرّف «{project_id}» — ربما حُذف، أو أن "
                       f"المُعرّف غير صحيح.")
    return found[0]


def _run_takeoff(project: Project, overrides: dict) -> list:
    return boq_engine.takeoff(project, overrides=overrides)


# --------------------------------------------------------------------------- #
# Reference data                                                                #
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {"ok": True,
            "core_version": core.__version__,
            "rules_count": len(rules_loader.load_rules())}


@app.get("/api/rules")
def get_rules() -> list[dict]:
    """Every finish system the engine can measure, with its editable defaults."""
    rules = rules_loader.load_rules()
    out = []
    for key, rule in rules.items():
        out.append({
            "rule_key": key,
            "code": rule.get("code", ""),
            "name_ar": rule.get("name_ar", ""),
            "surface": rule.get("surface", ""),
            "surface_ar": SURFACES_AR.get(rule.get("surface", ""), rule.get("surface", "")),
            "unit": rule.get("unit", ""),
            "unit_ar": UNITS_AR.get(rule.get("unit", ""), rule.get("unit", "")),
            "waste_pct": float(rule.get("waste_pct") or 0.0),
            "spec_params": dict(rule.get("spec_params") or {}),
            "notes_ar": rule.get("notes_ar", "") or "",
            # The deduction convention is an office policy, not a law — it belongs on
            # screen next to the parameter the user is about to change.
            "deduct_openings": bool(rule.get("deduct_openings")),
            "deduct_threshold_m2": float(rule.get("deduct_threshold_m2") or 0.0),
            "deduction_rule_ar": rule.get("deduction_rule_ar", "") or "",
        })
    return out


@app.get("/api/space-types")
def get_space_types() -> list[dict]:
    return [{"key": k, "name_ar": v} for k, v in SPACE_TYPES_AR.items()]


@app.get("/api/price-books")
def get_price_books() -> list[dict]:
    return [{
        "region": b.region,
        "currency": b.currency,
        "version": b.version,
        "is_placeholder": bool(b.is_placeholder),
        "effective_date": b.effective_date,
        "id": b.id,
        "source_note_ar": b.source_note_ar,
        "rates_count": len(b.rates),
    } for b in pricing.list_books()]


# --------------------------------------------------------------------------- #
# Projects                                                                      #
# --------------------------------------------------------------------------- #
@app.get("/api/projects")
def list_projects() -> list[dict]:
    return storage.list_projects()


@app.post("/api/projects", status_code=201)
def create_project(body: Any = Body(default=None)) -> dict:
    body = _as_dict(body, "بيانات المشروع الجديد")
    name = _text(body.get("name"))
    if not name:
        raise _bad("اسم المشروع مطلوب — لا يمكن إنشاء مشروع بلا اسم.")
    pid = storage.create_project(
        name=name,
        client_name=_text(body.get("client_name")),
        region=_text(body.get("region")),
        currency=_text(body.get("currency")),
    )
    return {"id": pid}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    """``{project, updated_at}``.

    ``updated_at`` is the version the client is about to edit. Handing it out here is
    what makes the optimistic-concurrency check on PUT possible at all: a browser that
    never learned which version it opened cannot tell the server what it based its
    edits on, and every save becomes a blind overwrite.
    """
    found = storage.get_project(project_id)
    if found is None:
        raise ApiError("NOT_FOUND",
                       f"لا يوجد مشروع بالمُعرّف «{project_id}» — ربما حُذف، أو أن "
                       f"المُعرّف غير صحيح.")
    project, row = found
    return {"project": project.to_dict(), "updated_at": row["updated_at"]}


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, body: Any = Body(default=None)) -> dict:
    """Overwrite a project. ``{project, updated_at?}`` -> ``{ok, updated_at, warnings}``.

    ``updated_at`` in the body is OPTIONAL and is the version the client believes it
    edited. When it is sent and no longer matches the row, the save is refused with
    ``409`` rather than applied: the alternative is that a tab holding a document from
    ten minutes ago silently destroys everything saved since. When it is absent the
    save proceeds — an older client, or a first save after a load that predates this
    field, must still be able to work.
    """
    body = _as_dict(body, "جسم الطلب")
    if "project" not in body:
        raise _bad('جسم الطلب يجب أن يحتوي على الحقل «project» بمستند المشروع كاملاً، '
                   'هكذا: {"project": { ... }}.')
    row = storage.get_row(project_id)
    if row is None:
        raise ApiError("NOT_FOUND",
                       f"لا يوجد مشروع بالمُعرّف «{project_id}» ليُحفظ فوقه.")

    known = _text(body.get("updated_at"))
    current = str(row.get("updated_at") or "")
    if known and current and known != current:
        raise ApiError(
            "CONFLICT",
            f"النسخة المحفوظة على الخادم ({current}) أحدث من النسخة التي تُحرّرها "
            f"({known}) — عُدّل هذا المشروع من نافذة أو جهاز آخر. أعد تحميل الصفحة "
            f"لتفتح النسخة الأحدث قبل المتابعة؛ لم يُحفظ أي تغيير الآن.")

    notes: list[str] = []
    project = validate_project_doc(body.get("project"), notes)
    updated_at = storage.save_project(project_id, project)
    if updated_at is None:
        raise ApiError("NOT_FOUND", f"لا يوجد مشروع بالمُعرّف «{project_id}» ليُحفظ فوقه.")
    return {"ok": True, "updated_at": updated_at, "warnings": notes}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    if not storage.delete_project(project_id):
        raise ApiError("NOT_FOUND",
                       f"لا يوجد مشروع بالمُعرّف «{project_id}» ليُحذف.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Take-off                                                                      #
# --------------------------------------------------------------------------- #
@app.post("/api/projects/{project_id}/takeoff")
def takeoff(project_id: str, body: Any = Body(default=None)) -> dict:
    body = _as_dict(body or {}, "جسم الطلب")
    overrides = _validate_overrides(body.get("overrides"))

    project = _load(project_id)
    lines = _run_takeoff(project, overrides)
    summary = boq_engine.summarize(lines)

    # Project-level geometry warnings never reach a line, so they are merged in here:
    # a room with no finishes assigned produces no line at all, and its problem would
    # otherwise be invisible.
    warnings = _merge_warnings(boq_engine.warnings_of(lines), project.validate())

    return {
        "lines": [ln.to_dict() for ln in lines],
        "summary": summary,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Quote                                                                         #
# --------------------------------------------------------------------------- #
_TERM_FIELDS = ("overhead_pct", "profit_pct", "tax_pct", "contingency_pct", "discount_pct")


def _terms_from(raw: Any, notes: list[str]) -> "pricing.QuoteTerms":
    """Read the commercial cascade, as FRACTIONS (0.16 == 16%), like the core.

    A value greater than 1 is almost certainly a percentage typed as a whole number,
    and charging 1600% overhead would be catastrophic. It is converted — and the
    conversion is announced in ``warnings``, never applied silently.
    """
    terms = _as_dict(raw or {}, "الشروط التجارية (terms)")
    clean: dict = {}
    for field in _TERM_FIELDS:
        if field not in terms or terms.get(field) is None:
            continue
        value = _num(terms.get(field))
        if value is None:
            raise _bad(f"قيمة «{field}» في الشروط التجارية ليست رقماً صالحاً — "
                       f"النسب تُكتب ككسر عشري (0.16 تعني 16%).")
        if value < 0:
            raise _bad(f"قيمة «{field}» في الشروط التجارية سالبة ({value}) — "
                       f"النسب التجارية لا تكون سالبة.")
        if value > 1.0:
            notes.append(f"النسبة «{field}» أُرسلت بالقيمة {value:g}؛ قُرئت على أنها "
                         f"{value:g}% أي {value / 100.0:g} كسراً. النسب في هذه الواجهة "
                         f"تُكتب ككسر عشري (0.16 تعني 16%).")
            value = value / 100.0
        clean[field] = value
    if "round_to" in terms and terms.get("round_to") is not None:
        rt = _num(terms.get("round_to"))
        if rt is None or rt < 0:
            raise _bad("قيمة «round_to» في الشروط التجارية غير صالحة — "
                       "يجب أن تكون رقماً موجباً أو صفراً.")
        clean["round_to"] = rt
    if _text(terms.get("note_ar")):
        clean["note_ar"] = _text(terms.get("note_ar"))
    return pricing.QuoteTerms.from_dict(clean)


def _load_book(region: str, version: Optional[str]) -> "pricing.PriceBook":
    if not region:
        known = "، ".join(pricing.regions()) or "لا يوجد"
        raise _bad(f"منطقة التسعير مطلوبة — حدّد «region» في الطلب أو في بيانات "
                   f"المشروع. المناطق المتاحة: {known}.")
    try:
        return pricing.load_book(region, version or "latest")
    except pricing.BookError as exc:
        ar = str(exc).partition("|")[0].strip()
        raise ApiError("PRICE_BOOK", ar or f"لا يوجد جدول أسعار للمنطقة «{region}».") from exc


def _build_quote(project: Project, body: dict) -> dict:
    notes: list[str] = []
    overrides = _validate_overrides(body.get("overrides"))
    region = _text(body.get("region")) or _text(project.region)
    version = _text(body.get("version")) or None
    terms = _terms_from(body.get("terms"), notes)

    book = _load_book(region, version)
    lines = _run_takeoff(project, overrides)

    # A warning line is a HOLE in the bill, not a zero-priced item: it carries no
    # quantity and must never appear as a priced row. Its warning still travels, below.
    measured = [ln for ln in lines if not boq_engine.is_warning(ln)]

    # on_missing="zero" instead of "raise": a rule with no rate in this book must stay
    # VISIBLE at zero (and block the quote via client_block_reason_ar), not abort the
    # whole request. A dropped line is a line the contractor pays for out of pocket.
    q = pricing.quote(project, measured, book, terms=terms, on_missing="zero")

    blocked = pricing.client_block_reason_ar(q) or None

    steps = {s.code: s for s in q.steps}

    def _step(code: str) -> float:
        s = steps.get(code)
        return round(float(s.amount), 2) if s is not None else 0.0

    totals = {
        "subtotal": round(float(q.subtotal), 2),
        "overhead": _step("overhead"),
        "profit": _step("profit"),
        "contingency": _step("contingency"),
        "discount": _step("discount"),
        "tax": _step("tax"),
        "grand_total": round(float(q.grand_total), 2),
        "currency": q.currency,
        "currency_ar": q.currency_ar,
        "steps": [s.to_dict() for s in q.steps],
        "terms": q.terms.to_dict(),
        "breakdown": list(q.breakdown),
    }

    warnings = _merge_warnings(boq_engine.warnings_of(lines), q.warnings_ar, notes,
                               project.validate())

    return {
        "quote": q,
        "book": book,
        "payload": {
            "lines": [pl.to_dict() for pl in q.lines],
            "totals": totals,
            "blocked_reason_ar": blocked,
            "book": {
                "region": book.region,
                "version": book.version,
                "is_placeholder": bool(book.is_placeholder),
                "id": book.id,
                "currency": book.currency,
                "effective_date": book.effective_date,
                "fingerprint": book.fingerprint,
            },
            "warnings": warnings,
        },
    }


@app.post("/api/projects/{project_id}/quote")
def quote(project_id: str, body: Any = Body(default=None)) -> dict:
    body = _as_dict(body or {}, "جسم الطلب")
    project = _load(project_id)
    return _build_quote(project, body)["payload"]


@app.get("/api/projects/{project_id}/quote.html", response_class=HTMLResponse)
def quote_html(project_id: str,
               detailed: int = Query(default=0),
               region: str = Query(default=""),
               version: str = Query(default=""),
               overhead_pct: Optional[float] = Query(default=None),
               profit_pct: Optional[float] = Query(default=None),
               tax_pct: Optional[float] = Query(default=None)) -> HTMLResponse:
    """The Arabic client document, print-ready A4, as one self-contained file."""
    project = _load(project_id)
    terms: dict = {}
    for field, value in (("overhead_pct", overhead_pct), ("profit_pct", profit_pct),
                         ("tax_pct", tax_pct)):
        if value is not None:
            terms[field] = value

    built = _build_quote(project, {"region": region, "version": version, "terms": terms})
    q = built["quote"]
    book = built["book"]
    blocked = built["payload"]["blocked_reason_ar"]

    options = proposal.ProposalOptions(
        detailed=bool(int(detailed or 0)),
        quote_no=project.id,
        price_book={"id": book.id, "version": book.version,
                    "is_placeholder": bool(book.is_placeholder),
                    "effective_date": book.effective_date},
        # The document stamps itself DRAFT for exactly the reason the API refuses to
        # call the quote sendable. One decision, printed in both places.
        draft=bool(blocked),
    )
    html = proposal.render_html(project, q.lines, q, options=options)
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


# --------------------------------------------------------------------------- #
# Root                                                                          #
# --------------------------------------------------------------------------- #
@app.get("/")
def root() -> JSONResponse:
    return JSONResponse({
        "name": "engeneer api",
        "core_version": core.__version__,
        "docs": "/docs",
        "health": "/api/health",
    })
