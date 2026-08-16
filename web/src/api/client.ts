/**
 * Typed fetch wrapper over the pinned HTTP contract.
 *
 * Two decisions shape this file:
 *
 * * **The server's Arabic sentence is the error message.** The contract promises
 *   `{error: {code, message_ar}}` where `message_ar` is a complete sentence written
 *   to be shown verbatim. This layer never rewrites it, never appends a status code
 *   to it, and never falls back to a raw exception string. When the server could not
 *   be reached at all — the one case where there is no `message_ar` — a fixed Arabic
 *   sentence is used, chosen so the user still learns what to do next.
 * * **A failure is visible exactly once.** `api_fetch` raises {@link ApiError} *and*
 *   shows the toast, so no caller has to remember to. Pass `silent: true` where the
 *   caller renders the failure itself (an inline field error, a blocked-quote
 *   banner) — a message shown twice trains people to dismiss it without reading.
 */

import { toast } from 'sonner'
import type {
  CreateProjectBody,
  GetProjectResult,
  Health,
  Overrides,
  PriceBookInfo,
  Project,
  ProjectListItem,
  PutProjectResult,
  QuoteBody,
  QuoteResult,
  QuoteTerms,
  RuleInfo,
  SpaceTypeInfo,
  TakeoffResult,
} from './types'

/** Same-origin by default; the dev server proxies /api to the Python engine. */
const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

/* ==========================================================================
   Errors
   ========================================================================== */

/** Arabic fallbacks for the failures that produce no `message_ar` of their own. */
const FALLBACK_AR: Record<number, string> = {
  0: 'تعذّر الاتصال بالخادم. تأكّد من أن الخدمة تعمل ثم أعد المحاولة.',
  400: 'الطلب غير صالح. راجع المُدخلات ثم أعد المحاولة.',
  401: 'الجلسة غير مصرّح بها. أعد تسجيل الدخول ثم أعد المحاولة.',
  403: 'لا تملك صلاحية تنفيذ هذا الإجراء.',
  404: 'العنصر المطلوب غير موجود — ربما حُذف من جهاز آخر.',
  409: 'تعارض في الحفظ: النسخة على الخادم أحدث من نسختك. أعد تحميل الصفحة قبل المتابعة.',
  413: 'حجم البيانات المُرسلة أكبر من المسموح.',
  422: 'البيانات المُرسلة غير مكتملة أو غير صالحة.',
  500: 'حدث خطأ داخلي في الخادم أثناء تنفيذ الطلب. لم يُحفظ أي تغيير.',
  502: 'الخادم لا يستجيب حالياً. أعد المحاولة بعد قليل.',
  503: 'الخدمة غير متاحة مؤقتاً. أعد المحاولة بعد قليل.',
}

const GENERIC_AR = 'تعذّر تنفيذ الطلب. أعد المحاولة، وإذا تكرّر الخطأ راجع سجل الخادم.'

/** A failed request, carrying the server's Arabic sentence untouched. */
export class ApiError extends Error {
  /** Stable machine code from the server, or a client-side code. */
  code: string
  /** The complete Arabic sentence to show the user. Never a Python exception. */
  message_ar: string
  /** HTTP status, or 0 when the request never reached the server. */
  status: number

  constructor(status: number, code: string, message_ar: string) {
    super(`${code} (${status}): ${message_ar}`)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.message_ar = message_ar
  }
}

/** The Arabic sentence for any thrown value — safe on non-ApiError rejections. */
export function error_ar(err: unknown): string {
  if (err instanceof ApiError) return err.message_ar
  if (err instanceof DOMException && err.name === 'AbortError') return 'أُلغي الطلب.'
  return GENERIC_AR
}

/** Show a failure as an Arabic toast. Exported so a caller that opted out of the
 *  automatic toast can still raise one at the moment that suits it. */
export function toast_error(err: unknown): void {
  toast.error(error_ar(err))
}

/* ==========================================================================
   Core
   ========================================================================== */

function pick_message_ar(body: unknown, status: number): { code: string; message_ar: string } {
  if (body && typeof body === 'object' && 'error' in body) {
    const e = (body as { error: unknown }).error
    if (e && typeof e === 'object') {
      const rec = e as { code?: unknown; message_ar?: unknown }
      const msg = typeof rec.message_ar === 'string' ? rec.message_ar.trim() : ''
      if (msg) {
        return { code: String(rec.code ?? `http_${status}`), message_ar: msg }
      }
    }
  }
  return {
    code: `http_${status}`,
    message_ar: FALLBACK_AR[status] ?? GENERIC_AR,
  }
}

export interface RequestOptions {
  /** Suppress the automatic toast; the caller renders the failure itself. */
  silent?: boolean
  signal?: AbortSignal
  /** Abort after this many ms. 0 disables. Take-offs on a large flat are slow. */
  timeout_ms?: number
  headers?: Record<string, string>
}

const DEFAULT_TIMEOUT_MS = 45_000

async function api_fetch<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
): Promise<T> {
  const url = BASE + path
  const timeout = opts.timeout_ms ?? DEFAULT_TIMEOUT_MS
  const controller = new AbortController()
  const timer =
    timeout > 0 ? setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeout) : null
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort(opts.signal.reason)
    else opts.signal.addEventListener('abort', () => controller.abort(opts.signal?.reason), { once: true })
  }

  let res: Response
  try {
    res = await fetch(url, {
      method,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...opts.headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (cause) {
    const aborted = cause instanceof DOMException && cause.name === 'AbortError'
    const err = new ApiError(
      0,
      aborted ? 'timeout' : 'network_unreachable',
      aborted
        ? 'استغرق الطلب وقتاً أطول من المتوقّع وأُلغي. أعد المحاولة، وإذا تكرّر فقد يكون المشروع كبيراً جداً على مهلة الاتصال الحالية.'
        : FALLBACK_AR[0],
    )
    if (!opts.silent) toast_error(err)
    throw err
  } finally {
    if (timer) clearTimeout(timer)
  }

  // 204 and empty bodies are legitimate; JSON.parse('') is not.
  const text = await res.text()
  let parsed: unknown = undefined
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = undefined
    }
  }

  if (!res.ok) {
    const { code, message_ar } = pick_message_ar(parsed, res.status)
    const err = new ApiError(res.status, code, message_ar)
    if (!opts.silent) toast_error(err)
    throw err
  }

  return parsed as T
}

/* ==========================================================================
   The contract, one function per endpoint
   ========================================================================== */

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  /* -- catalogue (static for the session; safe to cache aggressively) ----- */
  health: (o?: RequestOptions) => api_fetch<Health>('GET', '/api/health', undefined, o),

  rules: (o?: RequestOptions) => api_fetch<RuleInfo[]>('GET', '/api/rules', undefined, o),

  space_types: (o?: RequestOptions) =>
    api_fetch<SpaceTypeInfo[]>('GET', '/api/space-types', undefined, o),

  price_books: (o?: RequestOptions) =>
    api_fetch<PriceBookInfo[]>('GET', '/api/price-books', undefined, o),

  /* -- projects ----------------------------------------------------------- */
  list_projects: (o?: RequestOptions) =>
    api_fetch<ProjectListItem[]>('GET', '/api/projects', undefined, o),

  create_project: (body: CreateProjectBody, o?: RequestOptions) =>
    api_fetch<{ id: string }>('POST', '/api/projects', body, o),

  /**
   * The document AND the version it was read at.
   *
   * `updated_at` is not decoration: it is what {@link put_project} sends back as the
   * base of the edit, and it is the only thing that lets the server tell an edit
   * built on the current version from one built on a version that has since been
   * replaced. Dropping it here would make every save a blind overwrite.
   */
  get_project: async (id: string, o?: RequestOptions): Promise<GetProjectResult> => {
    const res = await api_fetch<{ project: Project; updated_at?: string | null }>(
      'GET',
      `/api/projects/${encodeURIComponent(id)}`,
      undefined,
      o,
    )
    return { project: res.project, updated_at: res.updated_at ?? null }
  },

  /** `base_updated_at` is the version this document was edited from. When it no
   *  longer matches the server's, the save comes back 409 instead of overwriting. */
  put_project: (
    id: string,
    project: Project,
    base_updated_at?: string | null,
    o?: RequestOptions,
  ) =>
    api_fetch<PutProjectResult>(
      'PUT',
      `/api/projects/${encodeURIComponent(id)}`,
      base_updated_at ? { project, updated_at: base_updated_at } : { project },
      o,
    ),

  delete_project: (id: string, o?: RequestOptions) =>
    api_fetch<{ ok: boolean }>('DELETE', `/api/projects/${encodeURIComponent(id)}`, undefined, o),

  /* -- engine -------------------------------------------------------------- */
  takeoff: (id: string, overrides?: Overrides, o?: RequestOptions) =>
    api_fetch<TakeoffResult>(
      'POST',
      `/api/projects/${encodeURIComponent(id)}/takeoff`,
      overrides ? { overrides } : {},
      o,
    ),

  quote: (id: string, body: QuoteBody, o?: RequestOptions) =>
    api_fetch<QuoteResult>('POST', `/api/projects/${encodeURIComponent(id)}/quote`, body, o),

  /**
   * URL of the rendered Arabic document. It is `text/html`, not JSON, so it is a
   * URL for an <iframe>/`window.open` rather than a fetch — the browser renders
   * the same bytes the client will receive.
   *
   * The commercial cascade travels with it. Omitting it does NOT mean "use the
   * defaults": the endpoint reads an absent percentage as zero, so a document
   * requested without terms prints the bare subtotal under the heading
   * «الإجمالي النهائي» while the screen shows the marked-up total. Two different
   * final totals for one quote — and the one the client receives is the lower —
   * is the silent under-quote this product exists to prevent.
   */
  quote_html_url: (
    id: string,
    opts: {
      detailed?: boolean
      region?: string
      version?: string
      terms?: QuoteTerms
    } = {},
  ): string =>
    `${BASE}/api/projects/${encodeURIComponent(id)}/quote.html` +
    qs({
      detailed: opts.detailed ? 1 : 0,
      region: opts.region,
      version: opts.version,
      overhead_pct: opts.terms?.overhead_pct,
      profit_pct: opts.terms?.profit_pct,
      tax_pct: opts.terms?.tax_pct,
    }),

  /** Raw HTML of the proposal, for a client-side print/download path. */
  quote_html: async (
    id: string,
    opts: {
      detailed?: boolean
      region?: string
      version?: string
      terms?: QuoteTerms
    } = {},
    o: RequestOptions = {},
  ): Promise<string> => {
    const res = await fetch(api.quote_html_url(id, opts), {
      headers: { Accept: 'text/html' },
      signal: o.signal,
    })
    if (!res.ok) {
      const err = new ApiError(res.status, `http_${res.status}`, FALLBACK_AR[res.status] ?? GENERIC_AR)
      if (!o.silent) toast_error(err)
      throw err
    }
    return res.text()
  },
}

/** Query keys, centralised so an invalidation can never miss a cache entry by
 *  spelling the key differently at the call site. */
export const qk = {
  health: ['health'] as const,
  rules: ['rules'] as const,
  space_types: ['space-types'] as const,
  price_books: ['price-books'] as const,
  projects: ['projects'] as const,
  project: (id: string) => ['project', id] as const,
  takeoff: (id: string, overrides?: Overrides) => ['takeoff', id, overrides ?? null] as const,
  quote: (id: string, body?: QuoteBody) => ['quote', id, body ?? null] as const,
}

export { api_fetch }
