# -*- coding: utf-8 -*-
"""The error envelope — every failure leaves this API as a complete Arabic sentence.

One rule governs this module: ``message_ar`` is shown to a surveyor VERBATIM, so it
must read like something a colleague wrote. A raw Python exception string, a stack
trace, or a bare ``KeyError('ceramic_tile_flor')`` reaching the screen is a bug, not
a diagnostic.

The core already writes bilingual exception messages in the shape
``"<Arabic sentence> | <English detail>"`` — the Arabic half is the user's, the
English half is the log's. :func:`split_bilingual` is what keeps that promise at the
HTTP boundary: the client gets the Arabic, the server console gets everything.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger("engeneer.api")

__all__ = [
    "ApiError", "error_body", "error_response", "split_bilingual",
    "install_handlers", "CODES",
]

# Stable machine codes. The UI switches on these; the human reads message_ar.
CODES = {
    "VALIDATION": 422,
    "NOT_FOUND": 404,
    # A save whose base version is no longer the version on disk. Refusing it is the
    # only way an edit made in another tab (or on another machine) is not silently
    # overwritten by a browser that had the project open since before it landed.
    "CONFLICT": 409,
    "GEOMETRY": 422,
    "RULE": 422,
    "FORMULA": 422,
    "PRICE_BOOK": 404,
    "PRICING": 422,
    "PROPOSAL": 422,
    "STORAGE": 500,
    "INTERNAL": 500,
}

# The last line of defence: shown only when nothing better could be derived. Still a
# complete sentence, still actionable, and never a traceback.
GENERIC_AR = ("حدث خطأ غير متوقع أثناء تنفيذ الطلب على الخادم. لم يُحفظ أي تغيير. "
              "أعد المحاولة، وإن تكرر الخطأ فأبلغ المطوّر مع ذكر وقت حدوثه.")


def split_bilingual(exc: BaseException) -> tuple[str, str]:
    """Split a core exception into (arabic_for_the_user, english_for_the_log).

    The core writes ``"جملة عربية كاملة. | english detail"``. Anything without the
    separator is treated as English-only and never reaches the client.
    """
    text = str(exc or "").strip()
    if not text:
        return "", exc.__class__.__name__
    if "|" in text:
        ar, _, en = text.partition("|")
        return ar.strip(), en.strip() or exc.__class__.__name__
    # Heuristic: a message with Arabic letters is already the user's sentence.
    if any("؀" <= ch <= "ۿ" for ch in text):
        return text, exc.__class__.__name__
    return "", text


def _sentence(text: str) -> str:
    """Guarantee a complete Arabic sentence: non-empty, terminated."""
    t = " ".join(str(text or "").split())
    if not t:
        return GENERIC_AR
    if t[-1] not in ".؟!:":
        t += "."
    return t


class ApiError(Exception):
    """A failure this API raises deliberately, already phrased for the user."""

    def __init__(self, code: str, message_ar: str, *, status: Optional[int] = None,
                 details: Any = None, log_detail: str = "") -> None:
        super().__init__(message_ar)
        self.code = code
        self.message_ar = _sentence(message_ar)
        self.status = int(status or CODES.get(code, 400))
        self.details = details
        self.log_detail = log_detail

    def body(self) -> dict:
        return error_body(self.code, self.message_ar, self.details)


def error_body(code: str, message_ar: str, details: Any = None) -> dict:
    body: dict = {"error": {"code": code, "message_ar": _sentence(message_ar)}}
    if details:
        body["error"]["details"] = details
    return body


def error_response(code: str, message_ar: str, status: Optional[int] = None,
                   details: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=int(status or CODES.get(code, 400)),
        content=error_body(code, message_ar, details),
    )


# --------------------------------------------------------------------------- #
# Core exception -> envelope                                                    #
# --------------------------------------------------------------------------- #
def _fallbacks() -> list[tuple[type, str, str]]:
    """(exception class, code, fallback Arabic) — most specific FIRST.

    Imported lazily so that importing this module never drags the whole core in,
    and so a core refactor cannot break error handling at import time.
    """
    from core.schema import GeometryError
    from core.rules_loader import FormulaError, RuleError
    from core.pricing import (BookError, PlaceholderRatesError, PriceError,
                              RateMissingError, UnitMismatchError)

    return [
        (GeometryError, "GEOMETRY",
         "تعذّر اشتقاق كمية من هندسة أحد الفراغات — راجع الأبعاد المُدخلة "
         "(المضلع أو مساحة الأرضية أو أطوال الجدران) ثم أعد الحصر."),
        (FormulaError, "FORMULA",
         "تعذّر تنفيذ معادلة الحصر لأحد بنود التشطيب — راجع بارامترات المواصفة "
         "المُدخلة لهذا البند."),
        (RuleError, "RULE",
         "قاعدة تشطيب غير معروفة أو ملف القواعد غير سليم — راجع نظام التشطيب "
         "المُسند للفراغ."),
        (BookError, "PRICE_BOOK",
         "جدول الأسعار المطلوب غير موجود — اختر منطقة وإصداراً متاحين."),
        (RateMissingError, "PRICING",
         "أحد البنود المقيسة لا يملك فئة سعرية في جدول الأسعار المختار — أضف الفئة "
         "أو استبعد البند قبل التسعير."),
        (UnitMismatchError, "PRICING",
         "وحدة أحد البنود لا تطابق وحدة الفئة في جدول الأسعار — لا يجوز ضرب كمية "
         "بوحدة مختلفة."),
        (PlaceholderRatesError, "PRICING",
         "لا يمكن إصدار هذا العرض للعميل بحالته الراهنة."),
        (PriceError, "PRICING",
         "تعذّر إتمام التسعير — راجع جدول الأسعار وشروط العرض التجارية."),
    ]


def from_exception(exc: BaseException) -> tuple[int, dict, str]:
    """Map ANY exception onto (status, envelope, english_log_line)."""
    if isinstance(exc, ApiError):
        return exc.status, exc.body(), exc.log_detail or exc.code

    try:
        table = _fallbacks()
    except Exception:                                   # pragma: no cover - import guard
        table = []

    ar, en = split_bilingual(exc)
    for cls, code, fallback_ar in table:
        if isinstance(exc, cls):
            return CODES[code], error_body(code, ar or fallback_ar), en

    # A KeyError on an unknown rule key is the classic 500 this API must not emit.
    if isinstance(exc, KeyError):
        key = str(exc.args[0]) if exc.args else ""
        return CODES["RULE"], error_body(
            "RULE",
            f"نظام التشطيب «{key}» غير معروف في ملف قواعد الحصر — اختر نظاماً من "
            f"القائمة المتاحة في /api/rules." if key else
            "عنصر مطلوب غير موجود في البيانات المُرسلة — راجع مدخلات المشروع."), en or repr(exc)

    if isinstance(exc, (ValueError, TypeError)) and ar:
        return CODES["VALIDATION"], error_body("VALIDATION", ar), en

    return CODES["INTERNAL"], error_body("INTERNAL", GENERIC_AR), en or repr(exc)


# --------------------------------------------------------------------------- #
# FastAPI wiring                                                                #
# --------------------------------------------------------------------------- #
def install_handlers(app) -> None:
    """Attach the handlers that make a traceback impossible to leak."""
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):        # noqa: ANN202
        log.warning("ApiError %s %s -> %s %s", request.method, request.url.path,
                    exc.code, exc.log_detail or exc.message_ar)
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _bad_body(request: Request, exc: RequestValidationError):   # noqa: ANN202
        # FastAPI's own validation text is English and shaped like a stack of loc
        # tuples. The user gets an Arabic sentence instead; the detail goes to the log.
        log.warning("bad body %s %s: %s", request.method, request.url.path, exc.errors())
        return error_response(
            "VALIDATION",
            "صيغة الطلب غير صحيحة — يجب إرسال بيانات JSON صالحة تحتوي على الحقول "
            "المطلوبة. راجع توثيق الواجهة في README.")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):       # noqa: ANN202
        if exc.status_code == 404:
            return error_response(
                "NOT_FOUND",
                f"المسار المطلوب «{request.url.path}» غير موجود في هذه الواجهة.", 404)
        if exc.status_code == 405:
            return error_response(
                "VALIDATION",
                f"طريقة الطلب «{request.method}» غير مسموحة على هذا المسار.", 405)
        ar, _ = split_bilingual(Exception(str(exc.detail)))
        return error_response("INTERNAL", ar or GENERIC_AR, exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):               # noqa: ANN202
        status, body, en = from_exception(exc)
        # The traceback belongs in the console, and ONLY in the console.
        log.error("unhandled %s %s: %s\n%s", request.method, request.url.path, en,
                  "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return JSONResponse(status_code=status, content=body)
