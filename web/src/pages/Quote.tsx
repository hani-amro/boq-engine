/**
 * The priced quote — `/p/:id/quote`.
 *
 * Four decisions shape this page, and three of them are product rules rather than
 * styling:
 *
 * * **A quote the engine blocked is never allowed to look sendable.** When
 *   `blocked_reason_ar` comes back non-null — placeholder rates, an uncaptured FX
 *   rate, a missing rate — the page leads with a red banner that says, in one
 *   Arabic sentence, that this must not go to a client, repeats the engine's reason
 *   verbatim, disables print/download/open-in-tab, prints the reason beside the
 *   disabled buttons, and stripes the document preview so a screenshot of it is
 *   also unmistakable. `is_placeholder` alone blocks too, even if the engine forgot
 *   to set a reason: a placeholder price book that reached a client is the single
 *   most expensive failure this product can have. The gate and the document are
 *   built from the SAME landed result (`data.book`), never from the region/version
 *   pickers, which run a round trip ahead of it — and nothing is exportable while a
 *   recompute is in flight, because the banner still describes the previous book.
 * * **The terms recompute live.** Overhead / profit / tax are debounced into the
 *   query key, and the previous result stays on screen while the new one lands, so
 *   the totals never flash empty under someone who is nudging a margin.
 * * **The engine's numbers are the numbers.** Only two figures on this page are
 *   computed in the browser — the pre-tax running total in the cascade and the
 *   per-chapter subtotals — and both are labelled as derived. If the steps do not
 *   add up to the engine's `grand_total`, that disagreement is shown rather than
 *   hidden, and the engine's total is the one to trust.
 * * **The quote describes the SAVED project.** The query flushes the autosave
 *   before asking, and refuses to price at all if that flush failed — pricing a
 *   version the server has not seen is how a number nobody can reproduce gets sent.
 *
 * Implemented locally because the shell has no equivalent: `use_debounced`
 * (value debounce for the live recompute) and `download_blob`.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  AlertTriangle,
  Download,
  ExternalLink,
  FileText,
  Printer,
  Receipt,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react'

import { ApiError, api, error_ar, qk, toast_error } from '../api/client'
import { useProjectStore } from '../store'
import {
  fmt_date,
  fmt_datetime,
  fmt_money,
  fmt_num,
  fmt_pct,
  fmt_qty,
  surface_ar,
  surface_chapter_ar,
} from '../lib/format'
import type { PricedLine, QuoteBody, QuoteTerms } from '../api/types'
import {
  Badge,
  Button,
  ButtonRow,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  CodeChip,
  EmptyState,
  ErrorState,
  Field,
  Num,
  NumberInput,
  Select,
  Skeleton,
  SkeletonRows,
  Stat,
  TBody,
  TD,
  TFoot,
  TGroupRow,
  TH,
  THead,
  TR,
  Table,
  TableWrap,
  Tabs,
  WarningList,
} from '../ui'

/* ==========================================================================
   Local helpers (nothing here belongs in the shared shell)
   ========================================================================== */

/** Value debounce. The terms feed a query key, so without this every digit typed
 *  would be its own POST to the pricing engine. */
function use_debounced<T>(value: T, delay: number): T {
  const [held, set_held] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => set_held(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return held
}

function download_blob(contents: string, filename: string, type: string): void {
  const url = URL.createObjectURL(new Blob([contents], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoked on the next tick: revoking synchronously can cancel the download in
  // Firefox before it has read the blob.
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** Filenames travel through email and Windows shares; keep them boring. */
function safe_filename(stem: string): string {
  const cleaned = stem.replace(/[\\/:*?"<>|]+/g, '-').replace(/\s+/g, '-').trim()
  return cleaned || 'quote'
}

const TERMS_DEBOUNCE_MS = 450

const DEFAULT_TERMS: QuoteTerms = { overhead_pct: 0.1, profit_pct: 0.12, tax_pct: 0 }

/** Percentages a Levant contractor actually types, as one-click chips. */
const OVERHEAD_PRESETS = [5, 8, 10, 12, 15]
const PROFIT_PRESETS = [8, 10, 12, 15, 20]
/** 0 (SY), 11 (LB), 14 (EG), 15 (SA), 16 (JO) — the VAT rates in the region. */
const TAX_PRESETS = [0, 11, 14, 15, 16]

/** The local sentence for a placeholder book that arrived without a reason. It is
 *  never preferred over the engine's own wording — only used when there is none. */
const PLACEHOLDER_REASON_AR =
  'دليل الأسعار المستخدم مبدئي (أسعار افتراضية غير مربوطة بالسوق)، فلا يصلح هذا العرض للإرسال إلى العميل. ' +
  'استورد دليل أسعار منشوراً للمنطقة ثم أعد الحساب.'

/* ==========================================================================
   Terms
   ========================================================================== */

function TermControl({
  label,
  hint,
  value,
  presets,
  onChange,
}: {
  label: string
  hint: string
  /** Fraction of one, as the contract carries it: 0.10 means 10%. */
  value: number
  presets: number[]
  onChange: (fraction: number) => void
}) {
  return (
    <Field label={label} hint={hint}>
      {(id) => (
        <div className="space-y-2">
          <NumberInput
            id={id}
            value={value * 100}
            digits={2}
            unit="%"
            min={0}
            max={100}
            clamp
            onValueChange={(v) => onChange((v ?? 0) / 100)}
          />
          <ButtonRow className="gap-1">
            {presets.map((p) => {
              const active = Math.abs(value * 100 - p) < 0.005
              return (
                <Button
                  key={p}
                  size="sm"
                  variant={active ? 'primary' : 'subtle'}
                  aria-pressed={active}
                  className="h-7 px-2 text-[12px]"
                  onClick={() => onChange(p / 100)}
                >
                  <Num>{p}%</Num>
                </Button>
              )
            })}
          </ButtonRow>
        </div>
      )}
    </Field>
  )
}

/* ==========================================================================
   Totals cascade
   ========================================================================== */

function CascadeRow({
  label,
  note,
  value,
  op,
  strong,
  tone = 'default',
}: {
  label: string
  note?: string
  value: string
  /** The arithmetic sign shown at the start of the row: "+" or "=". */
  op?: '+' | '='
  strong?: boolean
  tone?: 'default' | 'danger' | 'primary'
}) {
  const tones = {
    default: 'text-fg',
    danger: 'text-danger',
    primary: 'text-primary',
  } as const
  return (
    <TR>
      <TD className={strong ? 'font-semibold' : undefined}>
        <span className="inline-flex items-baseline gap-2">
          <span aria-hidden className="w-3 text-subtle tabular-nums">
            {op ?? ''}
          </span>
          <span>
            {label}
            {note && <span className="ms-2 text-[12px] font-normal text-subtle">{note}</span>}
          </span>
        </span>
      </TD>
      <TD numeric className={strong ? `text-[15px] font-bold ${tones[tone]}` : tones[tone]}>
        {value}
      </TD>
    </TR>
  )
}

/* ==========================================================================
   Page
   ========================================================================== */

export default function Quote() {
  const project_id = useProjectStore((s) => s.project_id)
  const project_name = useProjectStore((s) => s.project?.name ?? '')
  const project_region = useProjectStore((s) => s.project?.region ?? '')
  const project_currency = useProjectStore((s) => s.project?.currency ?? '')
  const flush = useProjectStore((s) => s.flush)

  const [terms, set_terms] = useState<QuoteTerms>(DEFAULT_TERMS)
  const [region, set_region] = useState('')
  const [version, set_version] = useState('')
  const [detailed, set_detailed] = useState(false)
  const [doc_loading, set_doc_loading] = useState(true)

  const debounced_terms = use_debounced(terms, TERMS_DEBOUNCE_MS)
  const iframe_ref = useRef<HTMLIFrameElement | null>(null)

  /* -- price books --------------------------------------------------------- */

  const books_q = useQuery({
    queryKey: qk.price_books,
    queryFn: () => api.price_books({ silent: true }),
    staleTime: Infinity,
  })
  const books = useMemo(() => books_q.data ?? [], [books_q.data])

  const regions = useMemo(
    () => Array.from(new Set(books.map((b) => b.region))).sort(),
    [books],
  )

  // The project's region is the default, and stays the default even before the
  // catalogue loads — a quote priced against another region by accident is a quote
  // priced against the wrong labour market.
  const active_region = region || project_region

  const region_books = useMemo(
    () =>
      books
        .filter((b) => b.region === active_region)
        .slice()
        .sort((a, b) => b.effective_date.localeCompare(a.effective_date)),
    [books, active_region],
  )

  /* -- the quote ----------------------------------------------------------- */

  const body: QuoteBody = useMemo(
    () => ({
      region: active_region,
      ...(version ? { version } : {}),
      terms: debounced_terms,
    }),
    [active_region, version, debounced_terms],
  )

  const quote_q = useQuery({
    queryKey: qk.quote(project_id ?? '', body),
    enabled: Boolean(project_id && active_region),
    queryFn: async () => {
      // Price the SAVED project, or do not price at all.
      const saved = await flush()
      if (!saved) {
        throw new ApiError(
          0,
          'unsaved_changes',
          'تعذّر حفظ آخر التعديلات على الخادم، ولن يُحسب السعر على نسخة قديمة. أصلح الاتصال ثم أعد المحاولة.',
        )
      }
      return api.quote(project_id as string, body, { silent: true })
    },
    // The screen carries the failure inline; the toast is raised once below.
    placeholderData: keepPreviousData,
    staleTime: 0,
    refetchOnMount: 'always',
    retry: 0,
  })

  // Exactly once per failed query — the inline ErrorState is the durable copy.
  useEffect(() => {
    if (quote_q.error) toast_error(quote_q.error)
  }, [quote_q.error])

  const data = quote_q.data
  const recomputing = quote_q.isFetching && Boolean(data)

  // The row that describes the book the PRICES came from — never a fallback to the
  // first book of the currently selected region. During a region change `data` still
  // describes the previous region, and that fallback printed the new region's
  // «تاريخ السريان» beside the old region's prices.
  const book_row = useMemo(
    () =>
      data
        ? books.find((b) => b.region === data.book.region && b.version === data.book.version)
        : undefined,
    [books, data],
  )

  const currency = data?.totals.currency || book_row?.currency || project_currency

  /* -- the blocking rule --------------------------------------------------- */

  const placeholder = Boolean(data?.book.is_placeholder)
  const blocked_reason_ar = data
    ? (data.blocked_reason_ar ?? (placeholder ? PLACEHOLDER_REASON_AR : null))
    : null
  const blocked = Boolean(blocked_reason_ar)

  /**
   * Whether the document may leave this screen.
   *
   * `blocked` alone is not enough. `keepPreviousData` means that while a new quote is
   * in flight — after a region change, a version change, a nudged margin — `data`,
   * and therefore `blocked`, still describes the PREVIOUS selection. A price book
   * whose rates are placeholders would be printed and emailed during that window
   * under a screen showing no banner at all, because the banner is computed from an
   * answer to a different question. Until the answer that describes the current
   * selection has landed, nothing is sendable.
   */
  const settled = Boolean(data) && !quote_q.isFetching
  const can_export = settled && !blocked

  /* -- derived figures (labelled as derived wherever they are shown) -------- */

  const chapters = useMemo(() => {
    if (!data) return []
    const order = new Map<string, { surface: string; lines: PricedLine[]; total: number }>()
    for (const pl of data.lines) {
      const key = String(pl.line.surface)
      let ch = order.get(key)
      if (!ch) {
        ch = { surface: key, lines: [], total: 0 }
        order.set(key, ch)
      }
      ch.lines.push(pl)
      ch.total += pl.total
    }
    return Array.from(order.values())
  }, [data])

  const material_sum = useMemo(
    () => (data ? data.lines.reduce((s, pl) => s + pl.material_total, 0) : 0),
    [data],
  )
  const labor_sum = useMemo(
    () => (data ? data.lines.reduce((s, pl) => s + pl.labor_total, 0) : 0),
    [data],
  )

  const before_tax = data ? data.totals.subtotal + data.totals.overhead + data.totals.profit : 0
  const steps_total = before_tax + (data?.totals.tax ?? 0)
  const mismatch =
    data !== undefined &&
    Math.abs(steps_total - data.totals.grand_total) >
      Math.max(0.01, Math.abs(data.totals.grand_total) * 1e-6)

  const rate_sources = useMemo(
    () => (data ? Array.from(new Set(data.lines.map((pl) => pl.rate_source))) : []),
    [data],
  )

  /* -- the Arabic document ------------------------------------------------- */

  /**
   * The document is built from the book the LANDED quote used — `data.book` — not
   * from the region/version pickers.
   *
   * Those are plain local state and change synchronously, so a URL derived from them
   * runs ahead of the result that decides whether the quote may be sent at all: the
   * preview would already be showing the new book while the banner, the stripes and
   * the disabled buttons still described the old one. Pinning `data.book.version`
   * also resolves «الأحدث» to the exact version that was priced, so the document and
   * the cascade above it are the same computation, reproducible later.
   */
  const doc_book = data?.book
  const doc_url =
    project_id && doc_book
      ? api.quote_html_url(project_id, {
          detailed,
          region: doc_book.region,
          version: doc_book.version,
          // Same cascade the screen is showing. Without it the document prints the
          // bare subtotal as «الإجمالي النهائي» and under-states the quote.
          terms: debounced_terms,
        })
      : ''

  useEffect(() => {
    set_doc_loading(true)
  }, [doc_url])

  const download = useMutation({
    mutationFn: async () => {
      if (blocked) throw new ApiError(0, 'blocked_quote', blocked_reason_ar ?? PLACEHOLDER_REASON_AR)
      if (!can_export || !doc_book) {
        throw new ApiError(
          0,
          'quote_not_settled',
          'لم يكتمل حساب هذا العرض بعد. انتظر انتهاء إعادة الحساب ثم أعد المحاولة — ' +
            'التنزيل الآن يُنزّل مستند الحساب السابق، لا الحساب المعروض.',
        )
      }
      return api.quote_html(project_id as string, {
        detailed,
        region: doc_book.region,
        version: doc_book.version,
        terms: debounced_terms,
      })
    },
    onSuccess: (html) => {
      download_blob(
        html,
        `${safe_filename(project_name || 'quote')}-عرض-سعر${detailed ? '-تفصيلي' : ''}.html`,
        'text/html;charset=utf-8',
      )
      toast.success('نُزّل المستند العربي.')
    },
    onError: (err) => toast_error(err),
  })

  function print_doc(): void {
    if (blocked) return
    if (!can_export) {
      toast.error('لم يكتمل حساب هذا العرض بعد. انتظر انتهاء إعادة الحساب ثم أعد المحاولة.')
      return
    }
    const win = iframe_ref.current?.contentWindow
    if (!win) {
      toast.error('لم يكتمل تحميل المستند بعد. انتظر ظهور المعاينة ثم أعد المحاولة.')
      return
    }
    try {
      win.focus()
      win.print()
    } catch {
      toast.error('تعذّرت الطباعة من داخل المعاينة. نزّل المستند ثم اطبعه من المتصفّح.')
    }
  }

  /* -- guards -------------------------------------------------------------- */

  if (!project_region && !books_q.isPending) {
    return (
      <EmptyState
        icon={<Receipt />}
        title="لا يمكن التسعير قبل تحديد المنطقة"
        description="حدّد منطقة المشروع في صفحة الفراغات أولاً — دليل الأسعار يُختار بالمنطقة، ولا يوجد سعر افتراضي صالح لكل المناطق."
      />
    )
  }

  /* ======================================================================= */

  return (
    <div className="space-y-5">
      {/* -- title + export actions ------------------------------------------ */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-bold">عرض السعر</h1>
          <p className="mt-0.5 text-[13.5px] text-muted">
            الأسعار تُقرأ من دليل أسعار منشور بنسخة محدّدة، ويُختم مصدرها على كل بند، فيمكن إعادة
            إنتاج هذا العرض حرفياً في أي وقت.
          </p>
        </div>
        <ButtonRow>
          <Button
            icon={<RefreshCw />}
            loading={quote_q.isFetching && !data}
            onClick={() => void quote_q.refetch()}
          >
            إعادة الحساب
          </Button>
          <Button icon={<Printer />} disabled={!can_export} onClick={print_doc}>
            طباعة
          </Button>
          <Button
            variant="primary"
            icon={<Download />}
            loading={download.isPending}
            disabled={!can_export}
            onClick={() => download.mutate()}
          >
            تنزيل المستند
          </Button>
        </ButtonRow>
      </div>

      {/* -- THE BLOCKING BANNER --------------------------------------------- */}
      {blocked && (
        <div
          role="alert"
          aria-live="assertive"
          className="rounded-xl border-2 border-danger/60 border-s-8 bg-danger-soft p-4 sm:p-5"
        >
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-0.5 size-7 shrink-0 text-danger" aria-hidden />
            <div className="min-w-0 space-y-2">
              <h2 className="text-[17px] font-bold text-danger">
                لا تُرسل هذا العرض إلى العميل
              </h2>
              <p className="text-[14px] leading-relaxed font-medium text-fg">
                {blocked_reason_ar}
              </p>
              <p className="text-[13px] leading-relaxed text-fg/85">
                الأرقام أدناه صالحة للمراجعة الداخلية فقط. الطباعة والتنزيل وفتح المستند معطّلة حتى
                يُستبدل دليل الأسعار بنسخة منشورة، ولن تُفعَّل بأي إجراء آخر في هذه الصفحة.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* -- failure --------------------------------------------------------- */}
      {quote_q.isError && !data && (
        <Card flush>
          <ErrorState
            message_ar={error_ar(quote_q.error)}
            action={
              <Button variant="primary" onClick={() => void quote_q.refetch()}>
                إعادة المحاولة
              </Button>
            }
          />
        </Card>
      )}

      {/* -- terms + price book ---------------------------------------------- */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card flush className="lg:col-span-2">
          <CardHeader
            title="شروط التسعير"
            subtitle="تُطبَّق بالترتيب: مصاريف إدارية على المجموع، ثم ربح، ثم ضريبة على ما سبق."
            actions={
              recomputing ? (
                <Badge tone="info" size="sm" dot>
                  جارٍ إعادة الحساب…
                </Badge>
              ) : null
            }
          />
          <CardBody className="grid gap-5 sm:grid-cols-3">
            <TermControl
              label="مصاريف إدارية"
              hint={`الحالي: ${fmt_pct(terms.overhead_pct)}`}
              value={terms.overhead_pct}
              presets={OVERHEAD_PRESETS}
              onChange={(v) => set_terms((t) => ({ ...t, overhead_pct: v }))}
            />
            <TermControl
              label="ربح المقاول"
              hint={`الحالي: ${fmt_pct(terms.profit_pct)}`}
              value={terms.profit_pct}
              presets={PROFIT_PRESETS}
              onChange={(v) => set_terms((t) => ({ ...t, profit_pct: v }))}
            />
            <TermControl
              label="ضريبة القيمة المضافة"
              hint={`الحالي: ${fmt_pct(terms.tax_pct)}`}
              value={terms.tax_pct}
              presets={TAX_PRESETS}
              onChange={(v) => set_terms((t) => ({ ...t, tax_pct: v }))}
            />
          </CardBody>
        </Card>

        <Card flush>
          <CardHeader
            title="دليل الأسعار"
            actions={
              placeholder ? (
                <Badge tone="danger" size="sm" icon={<AlertTriangle />}>
                  أسعار مبدئية
                </Badge>
              ) : data ? (
                <Badge tone="success" size="sm">
                  منشور
                </Badge>
              ) : null
            }
          />
          <CardBody className="space-y-4">
            {books_q.isPending ? (
              <div className="space-y-3">
                <Skeleton className="h-9.5 w-full" />
                <Skeleton className="h-9.5 w-full" />
              </div>
            ) : (
              <>
                <Field label="المنطقة">
                  {(id) => (
                    <Select
                      id={id}
                      value={active_region}
                      onChange={(e) => {
                        set_region(e.target.value)
                        set_version('')
                      }}
                      options={
                        regions.length
                          ? regions.map((r) => ({ value: r, label: r }))
                          : [{ value: active_region, label: active_region }]
                      }
                    />
                  )}
                </Field>
                <Field
                  label="النسخة"
                  hint="«الأحدث» يترك اختيار النسخة للخادم؛ ثبّت رقم نسخة إذا أردت عرضاً قابلاً لإعادة الإنتاج حرفياً."
                >
                  {(id) => (
                    <Select
                      id={id}
                      value={version}
                      onChange={(e) => set_version(e.target.value)}
                    >
                      <option value="">الأحدث</option>
                      {region_books.map((b) => (
                        <option key={b.version} value={b.version}>
                          {b.version}
                          {b.is_placeholder ? ' — مبدئي' : ''}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
              </>
            )}

            <dl className="space-y-1.5 border-t border-border pt-3 text-[13px]">
              <div className="flex items-baseline justify-between gap-3">
                <dt className="text-muted">المنطقة المعتمدة</dt>
                <dd className="font-medium">{data?.book.region || active_region || '—'}</dd>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <dt className="text-muted">النسخة المعتمدة</dt>
                <dd className="font-medium">
                  {data ? <CodeChip>{data.book.version}</CodeChip> : '—'}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <dt className="text-muted">تاريخ السريان</dt>
                <dd className="font-medium">
                  {book_row ? fmt_date(book_row.effective_date) : '—'}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <dt className="text-muted">العملة</dt>
                <dd className="font-medium">{currency || '—'}</dd>
              </div>
            </dl>
          </CardBody>
        </Card>
      </div>

      {/* -- engine warnings -------------------------------------------------- */}
      {data && <WarningList warnings={data.warnings} title="ملاحظات التسعير" />}

      {/* -- totals cascade --------------------------------------------------- */}
      {quote_q.isPending && !data ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Skeleton className="h-[86px] rounded-lg" />
          <Skeleton className="h-[86px] rounded-lg" />
          <Skeleton className="h-[86px] rounded-lg" />
        </div>
      ) : data ? (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card flush className="lg:col-span-2">
            <CardHeader
              title="سلسلة الاحتساب"
              subtitle="كل خطوة تُضاف إلى ما قبلها، بالترتيب الذي يُقرأ به العرض."
            />
            <TableWrap>
              <Table>
                <TBody>
                  <CascadeRow
                    label="المجموع الأولي للبنود"
                    note="مواد + عمالة"
                    value={fmt_money(data.totals.subtotal, currency)}
                  />
                  <CascadeRow
                    op="+"
                    label="مصاريف إدارية"
                    note={fmt_pct(debounced_terms.overhead_pct)}
                    value={fmt_money(data.totals.overhead, currency)}
                  />
                  <CascadeRow
                    op="+"
                    label="ربح المقاول"
                    note={fmt_pct(debounced_terms.profit_pct)}
                    value={fmt_money(data.totals.profit, currency)}
                  />
                  <CascadeRow
                    op="="
                    label="الصافي قبل الضريبة"
                    note="محسوب في المتصفح من الخطوات أعلاه"
                    value={fmt_money(before_tax, currency)}
                  />
                  <CascadeRow
                    op="+"
                    label="ضريبة القيمة المضافة"
                    note={fmt_pct(debounced_terms.tax_pct)}
                    value={fmt_money(data.totals.tax, currency)}
                  />
                </TBody>
                <TFoot>
                  <CascadeRow
                    op="="
                    label="الإجمالي النهائي"
                    value={fmt_money(data.totals.grand_total, currency)}
                    strong
                    tone={blocked ? 'danger' : 'primary'}
                  />
                </TFoot>
              </Table>
            </TableWrap>
            {mismatch && (
              <CardFooter className="text-[12.5px] leading-relaxed text-warn">
                <AlertTriangle className="size-4 shrink-0" aria-hidden />
                مجموع الخطوات ({fmt_money(steps_total, currency)}) لا يطابق الإجمالي القادم من
                المحرّك. الإجمالي القادم من المحرّك هو المعتمد؛ راجع تدوير الكسور قبل الإرسال.
              </CardFooter>
            )}
          </Card>

          <div className="grid content-start gap-3">
            <Stat
              label="الإجمالي النهائي"
              value={fmt_money(data.totals.grand_total, currency)}
              hint={blocked ? 'غير صالح للإرسال' : `${data.lines.length} بند مسعّر`}
              tone={blocked ? 'danger' : 'primary'}
            />
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
              <Stat
                label="إجمالي المواد"
                value={fmt_money(material_sum, currency)}
                hint="مجموع أعمدة المواد"
              />
              <Stat
                label="إجمالي العمالة"
                value={fmt_money(labor_sum, currency)}
                hint="مجموع أعمدة العمالة"
              />
            </div>
          </div>
        </div>
      ) : null}

      {/* -- priced lines ----------------------------------------------------- */}
      <Card flush>
        <CardHeader
          title="البنود المسعّرة"
          subtitle={
            data
              ? `${data.lines.length} بند — الأسعار من ${data.book.region} نسخة ${data.book.version}`
              : 'الكميات من الحصر، والأسعار من دليل الأسعار المحدّد أعلاه.'
          }
          actions={
            placeholder ? (
              <Badge tone="danger" size="sm">
                أسعار مبدئية
              </Badge>
            ) : null
          }
        />
        <TableWrap>
          <Table>
            <THead>
              <TR>
                <TH>الكود</TH>
                <TH>البند</TH>
                <TH>الفراغ</TH>
                <TH>السطح</TH>
                <TH numeric>الكمية المسعّرة</TH>
                <TH numeric>سعر المادة</TH>
                <TH numeric>سعر العمالة</TH>
                <TH numeric>قيمة المواد</TH>
                <TH numeric>قيمة العمالة</TH>
                <TH numeric>إجمالي البند</TH>
              </TR>
            </THead>
            {quote_q.isPending && !data ? (
              <SkeletonRows rows={6} cols={10} />
            ) : !data ? (
              <TBody>
                <TR>
                  <TD colSpan={10}>
                    <EmptyState
                      icon={<Receipt />}
                      title="لم تُحسب الأسعار بعد"
                      description="اضبط شروط التسعير ودليل الأسعار؛ يُعاد الحساب تلقائياً بعد كل تعديل."
                    />
                  </TD>
                </TR>
              </TBody>
            ) : data.lines.length === 0 ? (
              <TBody>
                <TR>
                  <TD colSpan={10}>
                    <EmptyState
                      title="لا توجد بنود مسعّرة"
                      description="لم يُنتج الحصر أي بند، أو لم يُعثر على سعر لأي بند في هذه النسخة من دليل الأسعار. راجع جدول الكميات والملاحظات أعلاه."
                    />
                  </TD>
                </TR>
              </TBody>
            ) : (
              <TBody className={recomputing ? 'opacity-60 transition-opacity' : undefined}>
                {chapters.map((ch) => (
                  <ChapterRows key={ch.surface} chapter={ch} currency={currency} />
                ))}
              </TBody>
            )}
            {data && data.lines.length > 0 && (
              <TFoot>
                <TR>
                  <TD colSpan={7} className="font-semibold">
                    المجموع الأولي للبنود
                  </TD>
                  <TD numeric>{fmt_money(material_sum, currency)}</TD>
                  <TD numeric>{fmt_money(labor_sum, currency)}</TD>
                  <TD numeric className="text-[14px] font-bold">
                    {fmt_money(data.totals.subtotal, currency)}
                  </TD>
                </TR>
              </TFoot>
            )}
          </Table>
        </TableWrap>
        {data && rate_sources.length > 0 && (
          <CardFooter className="text-[12.5px] text-muted">
            <span>مصدر الأسعار:</span>
            {rate_sources.map((rs) => (
              <CodeChip key={rs}>{rs}</CodeChip>
            ))}
            <span className="ms-auto">
              حُسب في {fmt_datetime(quote_q.dataUpdatedAt || Date.now())}
            </span>
          </CardFooter>
        )}
      </Card>

      {/* -- the Arabic document ---------------------------------------------- */}
      <Card flush>
        <CardHeader
          title="المستند العربي"
          subtitle="هذه هي الصفحة نفسها التي سيستلمها العميل، كما يُنتجها الخادم."
          actions={
            <div className="flex items-center gap-2">
              <Tabs
                variant="pill"
                aria-label="مستوى تفصيل المستند"
                value={detailed ? 'detailed' : 'summary'}
                onChange={(v) => set_detailed(v === 'detailed')}
                items={[
                  { value: 'summary', label: 'مختصر' },
                  { value: 'detailed', label: 'تفصيلي' },
                ]}
              />
              <Button
                size="sm"
                icon={<ExternalLink />}
                disabled={!can_export || !doc_url}
                onClick={() => window.open(doc_url, '_blank', 'noopener,noreferrer')}
              >
                فتح في تبويب
              </Button>
            </div>
          }
        />

        {blocked && (
          <div className="flex items-start gap-2 border-b border-danger/40 bg-danger-soft px-4 py-2.5 text-[13px] font-semibold text-danger sm:px-5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span className="font-normal text-fg">
              <span className="font-bold text-danger">معاينة داخلية فقط —</span>{' '}
              {blocked_reason_ar}
            </span>
          </div>
        )}

        {/* min-h matches the iframe's: the skeleton is absolutely positioned, so
            without it this box collapses to nothing before the first quote lands
            and the "loading" state renders as a silent 0px gap. */}
        <div className="relative min-h-[420px] bg-surface-2">
          {doc_loading && (
            <div className="absolute inset-0 z-10 space-y-3 bg-surface p-6" aria-busy="true">
              <Skeleton className="mx-auto h-5 w-64" />
              <Skeleton className="mx-auto h-3.5 w-40" />
              <div className="h-4" />
              <Skeleton className="h-3.5 w-full" />
              <Skeleton className="h-3.5 w-11/12" />
              <Skeleton className="h-3.5 w-4/5" />
              <div className="h-4" />
              <Skeleton className="h-40 w-full" />
            </div>
          )}
          {doc_url && (
            <iframe
              ref={iframe_ref}
              key={doc_url}
              src={doc_url}
              title="معاينة مستند عرض السعر"
              onLoad={() => set_doc_loading(false)}
              className="h-[70vh] max-h-[900px] min-h-[420px] w-full border-0 bg-white"
            />
          )}
          {blocked && !doc_loading && (
            <>
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0 z-20"
                style={{
                  backgroundImage:
                    'repeating-linear-gradient(45deg, color-mix(in oklab, var(--danger) 12%, transparent) 0 16px, transparent 16px 44px)',
                }}
              />
              <div
                aria-hidden
                className="pointer-events-none absolute inset-x-0 top-1/2 z-20 flex justify-center"
              >
                <span className="rounded-full border-2 border-danger bg-danger px-5 py-2 text-[15px] font-bold text-danger-fg shadow-lg">
                  مسودة — غير صالحة للإرسال
                </span>
              </div>
            </>
          )}
        </div>

        <CardFooter className="justify-between text-[12.5px] leading-relaxed text-muted">
          <p className="max-w-3xl">
            <FileText className="me-1.5 inline size-3.5 align-[-2px]" aria-hidden />
            المستند يُبنى على الخادم من المنطقة والنسخة المعتمدتين أعلاه، ومعه نسب المصاريف والربح
            والضريبة نفسها، فإجماليه هو «الإجمالي النهائي» في سلسلة الاحتساب حرفياً.
          </p>
          <ButtonRow>
            <Button size="sm" icon={<Printer />} disabled={!can_export} onClick={print_doc}>
              طباعة
            </Button>
            <Button
              size="sm"
              icon={<Download />}
              loading={download.isPending}
              disabled={!can_export}
              onClick={() => download.mutate()}
            >
              تنزيل
            </Button>
            {blocked ? (
              <span className="text-[12.5px] font-medium text-danger">
                معطّل: {blocked_reason_ar}
              </span>
            ) : !settled ? (
              <span className="text-[12.5px] font-medium text-muted">
                معطّل حتى ينتهي حساب العرض المعروض.
              </span>
            ) : null}
          </ButtonRow>
        </CardFooter>
      </Card>
    </div>
  )
}

/* ==========================================================================
   One chapter of the priced table (grouped by surface, the way a BOQ prints)
   ========================================================================== */

function ChapterRows({
  chapter,
  currency,
}: {
  chapter: { surface: string; lines: PricedLine[]; total: number }
  currency: string
}) {
  return (
    <>
      <TGroupRow colSpan={10}>
        <span className="flex flex-wrap items-baseline justify-between gap-2">
          <span>{surface_chapter_ar(chapter.surface)}</span>
          <span className="text-[12px] font-medium text-muted">
            مجموع الفصل (محسوب): {fmt_money(chapter.total, currency)}
          </span>
        </span>
      </TGroupRow>
      {chapter.lines.map((pl, i) => {
        const gross = pl.qty_basis === 'gross'
        const qty = gross ? pl.line.gross_qty : pl.line.net_qty
        return (
          <TR key={`${pl.line.rule_key}-${pl.line.space_id}-${i}`}>
            <TD>
              <CodeChip>{pl.line.code}</CodeChip>
            </TD>
            <TD truncate title={pl.line.name_ar}>
              {pl.line.name_ar}
            </TD>
            <TD>{pl.line.space_name || pl.line.space_id}</TD>
            <TD>{surface_ar(pl.line.surface)}</TD>
            <TD
              numeric
              title={`صافي: ${fmt_num(pl.line.net_qty)} · إجمالي بعد الهدر: ${fmt_num(
                pl.line.gross_qty,
              )}`}
            >
              {fmt_qty(qty, pl.line.unit)}
              <span className="ms-1.5 text-[11px] text-subtle">{gross ? 'إجمالي' : 'صافي'}</span>
            </TD>
            <TD numeric>{fmt_money(pl.material_rate, pl.currency)}</TD>
            <TD numeric>{fmt_money(pl.labor_rate, pl.currency)}</TD>
            <TD numeric>{fmt_money(pl.material_total, pl.currency)}</TD>
            <TD numeric>{fmt_money(pl.labor_total, pl.currency)}</TD>
            <TD numeric className="font-semibold">
              {fmt_money(pl.total, pl.currency)}
            </TD>
          </TR>
        )
      })}
    </>
  )
}
