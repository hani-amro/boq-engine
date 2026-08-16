/**
 * The bill of quantities — `/p/:id/boq`.
 *
 * Five decisions shape this page, and four of them are product rules rather than
 * styling:
 *
 * * **The breakdown is the product.** Every line carries the engine's Arabic
 *   arithmetic, one step per row, and that trail is rendered as a document — step
 *   number, the named quantity, the substituted expression, the result — not as a
 *   debug dump. A quantity nobody can retrace is a quantity nobody can defend in an
 *   interim certificate, so the trail is one click from every row and never
 *   truncated.
 * * **A zero is louder than a warning.** `net_qty <= 0` is the most dangerous state
 *   this product has: it looks like a measurement and it is an omission. Those lines
 *   get their own red banner with a count, a red row, a red badge, and a filter that
 *   isolates them — on top of whatever the engine said in `warnings`.
 * * **The take-off describes the SAVED project.** The query flushes the autosave
 *   first and refuses to measure at all if that flush failed. Measuring a version
 *   the server has not seen produces numbers nobody can reproduce.
 * * **The browser does not measure.** Every quantity printed here comes from the
 *   engine. The only figures computed locally are the per-group subtotals and the
 *   `net × (1 + waste)` check inside the breakdown — both are labelled as derived,
 *   and when the check disagrees with the engine the disagreement is SHOWN, with
 *   the engine's number kept as the one to trust.
 * * **An override is an edit to the reasoning, not a knob.** Changing a rule's waste
 *   percentage re-runs the take-off through the engine (never a local multiply) and
 *   is written into the breakdown as its own step, so the printed trail explains why
 *   the gross quantity moved.
 *
 * Implemented locally because the shell has no equivalent: `use_debounced`,
 * `download_blob` / `safe_filename` / the CSV writer, `ArabicMath` (digit isolation
 * inside an Arabic sentence) and `parse_step`.
 */

import { Fragment, useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Calculator,
  ChevronDown,
  Download,
  RefreshCw,
  RotateCcw,
  Ruler,
  Search,
  SlidersHorizontal,
} from 'lucide-react'

import { ApiError, api, error_ar, qk, toast_error } from '../api/client'
import { useProjectStore } from '../store'
import { cn } from '../lib/cn'
import {
  MISSING,
  UNIT_DIGITS,
  fmt_int,
  fmt_num,
  fmt_pct,
  fmt_qty,
  surface_ar,
  surface_chapter_ar,
  unit_ar,
} from '../lib/format'
import type { BOQLine, Overrides, QtyBucket, RuleInfo, TakeoffResult } from '../api/types'
import {
  Badge,
  Button,
  ButtonRow,
  Card,
  CardBody,
  CardHeader,
  CodeChip,
  EmptyState,
  ErrorState,
  Input,
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
  TabPanel,
  Tabs,
  WarningList,
} from '../ui'

/* ==========================================================================
   Local helpers — none of this belongs in the shared shell
   ========================================================================== */

/** Value debounce. The override map feeds the query key, so without this every
 *  digit typed into a waste field would be its own take-off request. */
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
  const cleaned = stem
    .replace(/[\\/:*?"<>|]+/g, '-')
    .replace(/\s+/g, '-')
    .trim()
  return cleaned || 'boq'
}

/** A DOM id built from a line key, which contains separators an id should not. */
function dom_id(key: string): string {
  return `boq-bd-${key.replace(/[^A-Za-z0-9_-]+/g, '-')}`
}

const OVERRIDE_DEBOUNCE_MS = 500
const WASTE_PRESETS = [0, 5, 7.5, 10, 15, 20]

/** Quantities agree to the last printed decimal, or they disagree visibly. */
const GROSS_TOLERANCE = 0.005

/* -- CSV ------------------------------------------------------------------- */

type CsvCell = string | number | null | undefined

/** Every field is quoted. An Arabic description containing a comma, a rule note
 *  containing a newline, or a code that starts with "=" must not be able to shift
 *  a column or execute as a formula. */
function csv_field(v: CsvCell): string {
  const s = v === null || v === undefined ? '' : String(v)
  // A leading =, +, - or @ is a formula in Excel. Prefix with a zero-width
  // apostrophe-free guard: a single quote makes Excel treat it as text.
  const guarded = /^[=+\-@]/.test(s) ? `'${s}` : s
  return `"${guarded.replace(/"/g, '""')}"`
}

function csv_rows(rows: CsvCell[][]): string {
  return rows.map((r) => r.map(csv_field).join(',')).join('\r\n')
}

/** A number for a spreadsheet cell: no thousands separators (Excel would read
 *  them as text) and a blank — never a zero — for a value we do not have. */
function csv_num(v: unknown, digits: number): string {
  const s = fmt_num(v, digits)
  return s === MISSING ? '' : s.replace(/,/g, '')
}

function unit_digits(unit: string): number {
  return UNIT_DIGITS[unit] ?? 2
}

/* -- warning attribution ---------------------------------------------------- */

/**
 * Wording the engine uses when it substituted an assumption for a measurement.
 * A line matched by this is reported as "تقديرية" even when its number looks fine.
 *
 * Deliberately narrow. A bare "افتراض" would also match "القيمة الافتراضية" — the
 * rule's own default, which is not an assumption about the building — and a badge
 * that appears on every row is a badge nobody reads.
 */
const ESTIMATE_RE = /تقدير|تقريب|مفترض|افتراضاً|بدون قياس|غير مقيس|لم يُقس|نمطية/

function escape_re(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** A short machine id ("S1") must match as a whole token, or "S1" flags "S12". */
function mentions_token(text: string, token: string): boolean {
  if (!token) return false
  return new RegExp(`(^|[^\\p{L}\\p{N}_])${escape_re(token)}([^\\p{L}\\p{N}_]|$)`, 'u').test(text)
}

/* ==========================================================================
   Derived view model
   ========================================================================== */

interface Row {
  key: string
  line: BOQLine
  /** Nothing was measured here. The single most dangerous state on the page. */
  zero: boolean
  /** The engine substituted an assumption for a measurement. */
  estimated: boolean
  /** Engine warnings that name this line. */
  warnings: string[]
  /** The waste percentage was overridden by hand for this take-off. */
  overridden: boolean
}

interface TradeGroup {
  surface: string
  title: string
  rows: Row[]
  /** Derived in the browser from the rows above — labelled as such wherever shown. */
  totals: Map<string, { net: number; gross: number }>
}

interface SpaceGroup {
  space_id: string
  space_name: string
  trades: TradeGroup[]
  row_count: number
  flagged: number
  /** Warnings that name the space but no particular line. */
  warnings: string[]
}

function is_zero(line: BOQLine): boolean {
  return !Number.isFinite(line.net_qty) || line.net_qty <= 0
}

function group_lines(
  lines: BOQLine[],
  warnings: string[],
  overrides: Record<string, number>,
): SpaceGroup[] {
  // Space identities, so a warning that names one space is not pinned onto the
  // identically-coded line in another one.
  const space_names = new Map<string, string>()
  for (const l of lines) space_names.set(l.space_id, l.space_name || l.space_id)

  const warn_spaces = warnings.map((w) => {
    const hit: string[] = []
    for (const [id, name] of space_names) {
      if (mentions_token(w, id) || (name.length >= 2 && w.includes(name))) hit.push(id)
    }
    return hit
  })

  const seen = new Map<string, number>()
  const rows: Row[] = lines.map((line) => {
    const base = `${line.space_id}|${line.rule_key}|${line.surface}|${line.code}`
    const n = (seen.get(base) ?? 0) + 1
    seen.set(base, n)

    const mine = warnings.filter((w, i) => {
      const names_line = mentions_token(w, line.code) || mentions_token(w, line.rule_key)
      if (!names_line) return false
      const spaces = warn_spaces[i]
      return spaces.length === 0 || spaces.includes(line.space_id)
    })

    return {
      key: n === 1 ? base : `${base}#${n}`,
      line,
      zero: is_zero(line),
      estimated: mine.some((w) => ESTIMATE_RE.test(w)) || line.breakdown.some((b) => ESTIMATE_RE.test(b)),
      warnings: mine,
      overridden: Object.hasOwn(overrides, line.rule_key),
    }
  })

  // A warning that names a space but no line belongs to the space header.
  const space_only = new Map<string, string[]>()
  warnings.forEach((w, i) => {
    const names_any_line = lines.some(
      (l) => mentions_token(w, l.code) || mentions_token(w, l.rule_key),
    )
    if (names_any_line) return
    for (const id of warn_spaces[i]) {
      space_only.set(id, [...(space_only.get(id) ?? []), w])
    }
  })

  const by_space = new Map<string, SpaceGroup>()
  for (const row of rows) {
    const { line } = row
    let g = by_space.get(line.space_id)
    if (!g) {
      g = {
        space_id: line.space_id,
        space_name: line.space_name || line.space_id,
        trades: [],
        row_count: 0,
        flagged: 0,
        warnings: space_only.get(line.space_id) ?? [],
      }
      by_space.set(line.space_id, g)
    }
    const surface = String(line.surface)
    let t = g.trades.find((x) => x.surface === surface)
    if (!t) {
      t = { surface, title: surface_chapter_ar(surface), rows: [], totals: new Map() }
      g.trades.push(t)
    }
    t.rows.push(row)
    g.row_count += 1
    if (row.zero || row.estimated || row.warnings.length) g.flagged += 1

    const unit = String(line.unit)
    const acc = t.totals.get(unit) ?? { net: 0, gross: 0 }
    if (Number.isFinite(line.net_qty)) acc.net += line.net_qty
    if (Number.isFinite(line.gross_qty)) acc.gross += line.gross_qty
    t.totals.set(unit, acc)
  }

  for (const g of by_space.values()) {
    for (const t of g.trades) t.rows.sort((a, b) => a.line.code.localeCompare(b.line.code))
  }
  return [...by_space.values()]
}

/* ==========================================================================
   The audit trail
   ========================================================================== */

/**
 * Arabic prose with Western digits in it.
 *
 * The digit runs are isolated (`.num` sets `direction: ltr; unicode-bidi: isolate`)
 * and set in the mono face so the columns of a multi-step calculation line up and
 * "18.40 م × 3.00 م" cannot be re-ordered by the bidi algorithm into something that
 * reads as a different measurement.
 */
const NUM_RE = /(\d[\d,]*(?:\.\d+)?\s*%?)/g

function ArabicMath({ text, className }: { text: string; className?: string }) {
  const parts = text.split(NUM_RE)
  return (
    <span className={className}>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <span key={i} className="num tnum font-mono font-semibold text-fg">
            {p.trim()}
          </span>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </span>
  )
}

interface Step {
  /** "مساحة الجدران الإجمالية" */
  label: string
  /** "المحيط 18.40 م × الارتفاع 3.00 م" */
  expr: string | null
  /** "55.20 م²" */
  result: string | null
}

/**
 * Split one engine sentence into label / expression / result.
 *
 * The engine writes `name = substituted expression = value`. Anything that does not
 * fit that shape is kept whole as a note rather than mangled — a step we cannot
 * parse is still a step the surveyor must read.
 */
function parse_step(raw: string): Step {
  const parts = raw.split('=')
  if (parts.length < 2) return { label: raw.trim(), expr: null, result: null }
  const label = parts[0].trim()
  const result = parts[parts.length - 1].trim()
  const expr = parts.slice(1, -1).join(' = ').trim()
  return { label, expr: expr || null, result: result || null }
}

function StepRow({ step, index, last, tone = 'default' }: {
  step: Step
  index: number
  last: boolean
  tone?: 'default' | 'accent'
}) {
  return (
    <li className="relative flex gap-3 ps-1">
      {/* The connector: a continuous rule down the start edge, cut after the last
          step, so the trail reads as one calculation instead of loose sentences. */}
      <span aria-hidden className="relative flex w-6 shrink-0 justify-center">
        <span
          className={cn(
            'z-10 mt-0.5 flex size-6 items-center justify-center rounded-full border text-[11px] font-semibold',
            tone === 'accent'
              ? 'border-accent/40 bg-accent-soft text-accent-fg'
              : 'border-border bg-surface-2 text-muted',
          )}
        >
          <span className="num tnum">{index}</span>
        </span>
        {!last && (
          <span className="absolute inset-x-0 top-6 -bottom-3 mx-auto w-px bg-border" />
        )}
      </span>

      <div className="min-w-0 flex-1 pb-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <ArabicMath
            text={step.label}
            className={cn(
              'text-[13.5px] leading-relaxed',
              tone === 'accent' ? 'font-semibold text-accent-fg' : 'font-semibold text-fg',
            )}
          />
          {step.result && (
            <span
              className={cn(
                'shrink-0 rounded-md border px-2 py-0.5 font-mono text-[13px] tabular-nums',
                last
                  ? 'border-primary/30 bg-primary-soft font-bold text-primary'
                  : tone === 'accent'
                    ? 'border-accent/30 bg-accent-soft text-accent-fg'
                    : 'border-border bg-surface-2 text-fg',
              )}
            >
              <ArabicMath text={step.result} />
            </span>
          )}
        </div>
        {step.expr && (
          <ArabicMath
            text={step.expr}
            className="mt-0.5 block text-[13px] leading-relaxed text-muted"
          />
        )}
      </div>
    </li>
  )
}

/* ==========================================================================
   Waste override control
   ========================================================================== */

function WasteOverride({
  rule_key,
  code,
  default_pct,
  value,
  onChange,
  onReset,
}: {
  rule_key: string
  code: string
  /** The rule's own waste from `GET /api/rules`, as a fraction of one. */
  default_pct: number | null
  /** The override, as a fraction of one, or null when the default is in force. */
  value: number | null
  onChange: (fraction: number) => void
  onReset: () => void
}) {
  const shown = value ?? default_pct ?? 0
  return (
    <div className="rounded-lg border border-border bg-surface-2/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[13px] font-semibold text-fg">
          <SlidersHorizontal className="size-4 text-subtle" aria-hidden />
          نسبة الهدر لهذه القاعدة
        </div>
        {value !== null && (
          <Button size="sm" variant="ghost" icon={<RotateCcw />} onClick={onReset}>
            استعادة الافتراضي
          </Button>
        )}
      </div>

      <p className="mt-1 text-[12.5px] leading-relaxed text-subtle">
        يُطبَّق على كل بنود القاعدة <CodeChip>{code || rule_key}</CodeChip> في المشروع، ويُعاد
        حساب الحصر على الخادم — لا تُضرب الكمية في المتصفح. الافتراضي في ملف القواعد:{' '}
        <Num>{default_pct === null ? MISSING : fmt_pct(default_pct)}</Num>.
      </p>

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <NumberInput
          size="sm"
          className="w-32"
          value={shown * 100}
          digits={2}
          unit="%"
          min={0}
          max={100}
          clamp
          aria-label="نسبة الهدر"
          onValueChange={(v) => onChange((v ?? 0) / 100)}
        />
        <ButtonRow className="gap-1">
          {WASTE_PRESETS.map((p) => {
            const active = Math.abs(shown * 100 - p) < 0.005
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
    </div>
  )
}

/* ==========================================================================
   The breakdown panel
   ========================================================================== */

function BreakdownPanel({
  row,
  default_pct,
  override,
  onOverride,
  onResetOverride,
}: {
  row: Row
  default_pct: number | null
  override: number | null
  onOverride: (fraction: number) => void
  onResetOverride: () => void
}) {
  const { line } = row
  const digits = unit_digits(String(line.unit))

  // Derived, and shown as derived: the engine's gross is the number of record.
  const expected_gross =
    Number.isFinite(line.net_qty) && Number.isFinite(line.waste_pct)
      ? line.net_qty * (1 + line.waste_pct)
      : null
  const gross_disagrees =
    expected_gross !== null &&
    Number.isFinite(line.gross_qty) &&
    Math.abs(expected_gross - line.gross_qty) > GROSS_TOLERANCE

  const steps = line.breakdown ?? []

  return (
    <div className="border-s-2 border-primary/30 bg-surface-2/40 px-4 py-4 sm:px-6">
      <div className="flex flex-wrap items-center gap-2 pb-3">
        <Ruler className="size-4 text-primary" aria-hidden />
        <h4 className="text-[13.5px] font-bold text-fg">خطوات الحساب</h4>
        <CodeChip>{line.code}</CodeChip>
        <span className="text-[12.5px] text-muted">{line.name_ar}</span>
        <span className="text-[12.5px] text-subtle">·</span>
        <span className="text-[12.5px] text-muted">{line.space_name || line.space_id}</span>
        <span className="text-[12.5px] text-subtle">·</span>
        <span className="text-[12.5px] text-muted">{surface_ar(line.surface)}</span>
        <CodeChip className="ms-auto">{line.rule_key}</CodeChip>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
        {/* -- the trail ------------------------------------------------------ */}
        <div className="rounded-lg border border-border bg-surface px-4 py-4">
          {steps.length === 0 ? (
            <p className="text-[13px] leading-relaxed text-danger">
              لم يُرفق الخادم خطوات حساب لهذا البند. الكمية غير قابلة للمراجعة كما هي، فلا
              تعتمدها في مستخلص قبل التحقق من القاعدة.
            </p>
          ) : (
            <ol className="space-y-0">
              {steps.map((raw, i) => (
                <StepRow
                  key={`${i}-${raw.slice(0, 24)}`}
                  step={parse_step(raw)}
                  index={i + 1}
                  last={i === steps.length - 1 && override === null}
                />
              ))}
              {override !== null && (
                <StepRow
                  tone="accent"
                  index={steps.length + 1}
                  last
                  step={{
                    label: 'تجاوز يدوي لنسبة الهدر',
                    expr:
                      `الافتراضي ${default_pct === null ? MISSING : fmt_pct(default_pct)}` +
                      ` ← المطبَّق ${fmt_pct(override)}` +
                      ` · الكمية الصافية ${fmt_qty(line.net_qty, line.unit)} × (1 + ${fmt_pct(line.waste_pct)})`,
                    result: fmt_qty(line.gross_qty, line.unit),
                  }}
                />
              )}
            </ol>
          )}

          {gross_disagrees && (
            <div className="mt-2 rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-[12.5px] leading-relaxed text-fg/90">
              <span className="font-semibold text-danger">تحقّق: </span>
              الكمية الإجمالية المعادة من المحرّك (<Num>{fmt_num(line.gross_qty, digits)}</Num>) لا
              تطابق حاصل ضرب الكمية الصافية في نسبة الهدر المحسوب في المتصفح (
              <Num>{fmt_num(expected_gross, digits)}</Num>). الرقم المعتمد هو رقم المحرّك؛ راجع
              القاعدة قبل الاعتماد.
            </div>
          )}
        </div>

        {/* -- the numbers, and the control that moves them -------------------- */}
        <div className="space-y-3">
          <dl className="rounded-lg border border-border bg-surface px-4 py-3 text-[13px]">
            <div className="flex items-baseline justify-between gap-3 py-1">
              <dt className="text-muted">الكمية الصافية (أساس الدفع)</dt>
              <dd className="font-mono font-semibold tabular-nums">
                <ArabicMath text={fmt_qty(line.net_qty, line.unit)} />
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-3 border-t border-border py-1">
              <dt className="text-muted">نسبة الهدر</dt>
              <dd className="flex items-center gap-2 font-mono font-semibold tabular-nums">
                {override !== null && (
                  <Badge tone="accent" size="sm">
                    مُعدّلة
                  </Badge>
                )}
                <Num>{fmt_pct(line.waste_pct)}</Num>
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-3 border-t border-border py-1">
              <dt className="text-muted">الكمية الإجمالية (أساس التوريد)</dt>
              <dd className="font-mono font-semibold tabular-nums">
                <ArabicMath text={fmt_qty(line.gross_qty, line.unit)} />
              </dd>
            </div>
          </dl>

          {row.warnings.length > 0 && <WarningList warnings={row.warnings} title="تنبيهات هذا البند" />}

          {row.zero && (
            <div className="rounded-lg border border-danger/30 bg-danger-soft px-4 py-3 text-[13px] leading-relaxed text-fg/90">
              <span className="font-semibold text-danger">كمية صفر. </span>
              لم يُقس هذا البند: راجع أبعاد الفراغ والفتحات في صفحة الفراغات قبل إرسال الجدول،
              فالصفر هنا يعني بنداً ساقطاً من العقد لا بنداً بلا عمل.
            </div>
          )}

          <WasteOverride
            rule_key={line.rule_key}
            code={line.code}
            default_pct={default_pct}
            value={override}
            onChange={onOverride}
            onReset={onResetOverride}
          />
        </div>
      </div>
    </div>
  )
}

/* ==========================================================================
   Summary
   ========================================================================== */

function bucket_list(by_unit: Record<string, QtyBucket>): QtyBucket[] {
  return Object.values(by_unit ?? {})
}

/** Per-unit chips — quantities are only ever summed WITHIN one unit. */
function UnitTotals({ buckets, gross }: { buckets: QtyBucket[]; gross?: boolean }) {
  if (!buckets.length) return <span className="text-subtle">{MISSING}</span>
  return (
    <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
      {buckets.map((b) => (
        <span key={b.unit} className="font-mono text-[12.5px] tabular-nums">
          <ArabicMath
            text={`${fmt_num(gross ? b.gross_qty : b.net_qty, unit_digits(String(b.unit)))} ${b.unit_ar || unit_ar(b.unit)}`}
          />
        </span>
      ))}
    </span>
  )
}

/** Lines in a bucket set — the engine's own count, summed across units. */
function bucket_lines(buckets: QtyBucket[]): number {
  return buckets.reduce((n, b) => n + (Number.isFinite(b.lines) ? b.lines : 0), 0)
}

function SummaryPanel({
  data,
  overrides,
}: {
  data: TakeoffResult
  overrides: Record<string, number>
}) {
  const [tab, set_tab] = useState('trade')
  const rules = useMemo(
    () =>
      Object.values(data.summary.by_rule ?? {}).sort(
        (a, b) => a.surface.localeCompare(b.surface) || a.code.localeCompare(b.code),
      ),
    [data],
  )
  const spaces = useMemo(() => Object.values(data.summary.by_space ?? {}), [data])
  const trades = useMemo(() => Object.values(data.summary.by_surface ?? {}), [data])
  const grand = bucket_list(data.summary.by_unit)

  return (
    <Card flush>
      <CardHeader
        title="الملخّص"
        subtitle="مجاميع المحرّك. تُجمع الكميات داخل الوحدة الواحدة فقط — م² لا تُجمع مع م.ط."
        actions={
          <Tabs
            variant="pill"
            aria-label="طريقة التجميع"
            value={tab}
            onChange={set_tab}
            items={[
              { value: 'trade', label: 'حسب القسم' },
              { value: 'rule', label: 'حسب البند' },
              { value: 'space', label: 'حسب الفراغ' },
            ]}
          />
        }
      />

      {/* -- per trade: the chapter roll-up a BOQ is signed off by --------- */}
      <TabPanel value="trade" active={tab}>
        {trades.length === 0 ? (
          <EmptyState
            title="لا مجاميع لكل قسم"
            description="لم يُرجع الخادم تجميعاً حسب السطح لهذا الحصر. المجاميع حسب البند وحسب الفراغ أدناه ما تزال كاملة."
          />
        ) : (
          <TableWrap>
            <Table>
              <THead>
                <TR>
                  <TH>القسم</TH>
                  <TH>السطح</TH>
                  <TH numeric>البنود</TH>
                  <TH numeric>الصافي</TH>
                  <TH numeric>الإجمالي</TH>
                </TR>
              </THead>
              <TBody>
                {trades.map((t) => {
                  const buckets = bucket_list(t.by_unit)
                  return (
                    <TR key={String(t.surface)}>
                      <TD>
                        <span className="font-medium">{surface_chapter_ar(t.surface)}</span>
                      </TD>
                      <TD>{t.surface_ar || surface_ar(t.surface)}</TD>
                      <TD numeric>{fmt_int(bucket_lines(buckets))}</TD>
                      <TD numeric>
                        <UnitTotals buckets={buckets} />
                      </TD>
                      <TD numeric>
                        <UnitTotals buckets={buckets} gross />
                      </TD>
                    </TR>
                  )
                })}
              </TBody>
              <TFoot>
                <TR>
                  <TD colSpan={3}>مجموع المشروع لكل وحدة</TD>
                  <TD numeric>
                    <UnitTotals buckets={grand} />
                  </TD>
                  <TD numeric>
                    <UnitTotals buckets={grand} gross />
                  </TD>
                </TR>
              </TFoot>
            </Table>
          </TableWrap>
        )}
      </TabPanel>

      <TabPanel value="rule" active={tab}>
        <TableWrap>
          <Table>
            <THead>
              <TR>
                <TH>الكود</TH>
                <TH>البند</TH>
                <TH>القسم</TH>
                <TH>الوحدة</TH>
                <TH numeric>الفراغات</TH>
                <TH numeric>البنود</TH>
                <TH numeric>الهدر</TH>
                <TH numeric>الصافي</TH>
                <TH numeric>الإجمالي</TH>
              </TR>
            </THead>
            <TBody>
              {rules.map((r) => (
                <TR key={r.rule_key}>
                  <TD>
                    <CodeChip>{r.code}</CodeChip>
                  </TD>
                  <TD truncate title={r.name_ar}>
                    {r.name_ar}
                  </TD>
                  <TD>{r.surface_ar || surface_ar(r.surface)}</TD>
                  <TD>{r.unit_ar || unit_ar(r.unit)}</TD>
                  <TD numeric>{fmt_int(r.spaces?.length ?? 0)}</TD>
                  <TD numeric>{fmt_int(r.lines)}</TD>
                  <TD numeric>
                    <span className="inline-flex items-center gap-1.5">
                      {Object.hasOwn(overrides, r.rule_key) && (
                        <Badge tone="accent" size="sm">
                          مُعدّلة
                        </Badge>
                      )}
                      <Num>{fmt_pct(r.waste_pct)}</Num>
                    </span>
                  </TD>
                  <TD numeric>{fmt_num(r.net_qty, unit_digits(String(r.unit)))}</TD>
                  <TD numeric>{fmt_num(r.gross_qty, unit_digits(String(r.unit)))}</TD>
                </TR>
              ))}
            </TBody>
            <TFoot>
              <TR>
                <TD colSpan={7}>مجموع المشروع لكل وحدة</TD>
                <TD numeric>
                  <UnitTotals buckets={grand} />
                </TD>
                <TD numeric>
                  <UnitTotals buckets={grand} gross />
                </TD>
              </TR>
            </TFoot>
          </Table>
        </TableWrap>
      </TabPanel>

      <TabPanel value="space" active={tab}>
        <TableWrap>
          <Table>
            <THead>
              <TR>
                <TH>الفراغ</TH>
                <TH numeric>البنود</TH>
                <TH numeric>التنبيهات</TH>
                <TH numeric>الصافي</TH>
                <TH numeric>الإجمالي</TH>
              </TR>
            </THead>
            <TBody>
              {spaces.map((s) => (
                <TR key={s.space_id} tone={s.warnings ? 'warn' : 'default'}>
                  <TD>
                    <span className="font-medium">{s.space_name || s.space_id}</span>
                    <CodeChip className="ms-2">{s.space_id}</CodeChip>
                  </TD>
                  <TD numeric>{fmt_int(s.lines)}</TD>
                  <TD numeric>
                    {s.warnings ? (
                      <Badge tone="warn" size="sm" icon={<AlertTriangle />}>
                        <Num>{fmt_int(s.warnings)}</Num>
                      </Badge>
                    ) : (
                      <span className="text-subtle">{MISSING}</span>
                    )}
                  </TD>
                  <TD numeric>
                    <UnitTotals buckets={bucket_list(s.by_unit)} />
                  </TD>
                  <TD numeric>
                    <UnitTotals buckets={bucket_list(s.by_unit)} gross />
                  </TD>
                </TR>
              ))}
            </TBody>
            <TFoot>
              <TR>
                <TD colSpan={3}>مجموع المشروع لكل وحدة</TD>
                <TD numeric>
                  <UnitTotals buckets={grand} />
                </TD>
                <TD numeric>
                  <UnitTotals buckets={grand} gross />
                </TD>
              </TR>
            </TFoot>
          </Table>
        </TableWrap>
      </TabPanel>
    </Card>
  )
}

/* ==========================================================================
   Page
   ========================================================================== */

const COLS = 7

export default function Boq() {
  const project_id = useProjectStore((s) => s.project_id)
  const project_name = useProjectStore((s) => s.project?.name ?? '')
  const flush = useProjectStore((s) => s.flush)

  const [overrides, set_overrides] = useState<Record<string, number>>({})
  const [expanded, set_expanded] = useState<Set<string>>(() => new Set())
  const [text, set_text] = useState('')
  const [space_filter, set_space_filter] = useState('')
  const [only_flagged, set_only_flagged] = useState(false)

  const debounced_overrides = use_debounced(overrides, OVERRIDE_DEBOUNCE_MS)

  /* -- the rules catalogue, for the DEFAULT waste of each rule ------------- */

  const rules_q = useQuery({
    queryKey: qk.rules,
    queryFn: () => api.rules({ silent: true }),
    staleTime: Infinity,
  })

  const rule_by_key = useMemo(() => {
    const m = new Map<string, RuleInfo>()
    for (const r of rules_q.data ?? []) m.set(r.rule_key, r)
    return m
  }, [rules_q.data])

  /* -- the take-off --------------------------------------------------------- */

  const wire_overrides: Overrides | undefined = useMemo(() => {
    const keys = Object.keys(debounced_overrides)
    if (keys.length === 0) return undefined
    // ASSUMED SHAPE — `Overrides` is deliberately open in the contract. One entry
    // per rule_key, carrying the spec fields to replace for this take-off only.
    const out: Record<string, unknown> = {}
    for (const k of keys) out[k] = { waste_pct: debounced_overrides[k] }
    return out
  }, [debounced_overrides])

  const takeoff_q = useQuery({
    queryKey: qk.takeoff(project_id ?? '', wire_overrides),
    enabled: Boolean(project_id),
    queryFn: async () => {
      // Measure the SAVED project, or do not measure at all.
      const saved = await flush()
      if (!saved) {
        throw new ApiError(
          0,
          'unsaved_changes',
          'تعذّر حفظ آخر التعديلات على الخادم، ولن يُحسب الحصر على نسخة قديمة. أصلح الاتصال ثم أعد المحاولة.',
        )
      }
      return api.takeoff(project_id as string, wire_overrides, { silent: true })
    },
    placeholderData: keepPreviousData,
    staleTime: 0,
    refetchOnMount: 'always',
    retry: 0,
  })

  // Exactly once per failure — the inline ErrorState is the durable copy.
  useEffect(() => {
    if (takeoff_q.error) toast_error(takeoff_q.error)
  }, [takeoff_q.error])

  const data = takeoff_q.data
  const recomputing = takeoff_q.isFetching && Boolean(data)

  /* -- view model ----------------------------------------------------------- */

  const groups = useMemo(
    () => (data ? group_lines(data.lines, data.warnings ?? [], debounced_overrides) : []),
    [data, debounced_overrides],
  )

  const all_rows = useMemo(() => groups.flatMap((g) => g.trades.flatMap((t) => t.rows)), [groups])
  const zero_rows = useMemo(() => all_rows.filter((r) => r.zero), [all_rows])
  const flagged_count = useMemo(
    () => all_rows.filter((r) => r.zero || r.estimated || r.warnings.length).length,
    [all_rows],
  )

  const space_options = useMemo(
    () => [
      { value: '', label: 'كل الفراغات' },
      ...groups.map((g) => ({ value: g.space_id, label: g.space_name })),
    ],
    [groups],
  )

  const needle = text.trim().toLowerCase()
  const visible = useMemo(() => {
    const keep = (r: Row) => {
      if (only_flagged && !(r.zero || r.estimated || r.warnings.length)) return false
      if (!needle) return true
      const l = r.line
      return (
        l.code.toLowerCase().includes(needle) ||
        l.name_ar.toLowerCase().includes(needle) ||
        l.rule_key.toLowerCase().includes(needle) ||
        (l.space_name ?? '').toLowerCase().includes(needle)
      )
    }
    return groups
      .filter((g) => !space_filter || g.space_id === space_filter)
      .map((g) => ({
        ...g,
        trades: g.trades
          .map((t) => ({ ...t, rows: t.rows.filter(keep) }))
          .filter((t) => t.rows.length > 0),
      }))
      .filter((g) => g.trades.length > 0)
  }, [groups, space_filter, only_flagged, needle])

  const visible_rows = useMemo(
    () => visible.flatMap((g) => g.trades.flatMap((t) => t.rows)),
    [visible],
  )

  /* -- interactions --------------------------------------------------------- */

  function toggle(key: string) {
    set_expanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function set_override(rule_key: string, fraction: number) {
    set_overrides((prev) => ({ ...prev, [rule_key]: fraction }))
  }

  function clear_override(rule_key: string) {
    set_overrides((prev) => {
      const next = { ...prev }
      delete next[rule_key]
      return next
    })
  }

  /* -- CSV ------------------------------------------------------------------ */

  function export_csv() {
    if (!data) return
    const rows: CsvCell[][] = []
    rows.push([
      'الفراغ',
      'رمز الفراغ',
      'القسم',
      'السطح',
      'الكود',
      'اسم البند',
      'مفتاح القاعدة',
      'الوحدة',
      'الكمية الصافية',
      'نسبة الهدر %',
      'الكمية الإجمالية',
      'الحالة',
      'خطوات الحساب',
    ])

    for (const g of groups) {
      for (const t of g.trades) {
        for (const r of t.rows) {
          const l = r.line
          const d = unit_digits(String(l.unit))
          const flags: string[] = []
          if (r.zero) flags.push('كمية صفر')
          if (r.estimated) flags.push('تقديرية')
          if (r.overridden) flags.push('هدر مُعدّل')
          if (r.warnings.length) flags.push(`تنبيهات: ${r.warnings.length}`)
          rows.push([
            g.space_name,
            g.space_id,
            t.title,
            surface_ar(l.surface),
            l.code,
            l.name_ar,
            l.rule_key,
            unit_ar(l.unit),
            csv_num(l.net_qty, d),
            csv_num(l.waste_pct * 100, 2),
            csv_num(l.gross_qty, d),
            flags.join(' · '),
            l.breakdown.join('  |  '),
          ])
        }
      }
    }

    rows.push([])
    rows.push(['الملخّص حسب القسم'])
    rows.push(['القسم', 'السطح', 'الوحدة', 'البنود', 'الصافي', 'الإجمالي'])
    for (const t of Object.values(data.summary.by_surface ?? {})) {
      for (const b of bucket_list(t.by_unit)) {
        const d = unit_digits(String(b.unit))
        rows.push([
          surface_chapter_ar(t.surface),
          t.surface_ar || surface_ar(t.surface),
          b.unit_ar || unit_ar(b.unit),
          fmt_int(b.lines),
          csv_num(b.net_qty, d),
          csv_num(b.gross_qty, d),
        ])
      }
    }

    rows.push([])
    rows.push(['الملخّص حسب البند'])
    rows.push(['الكود', 'البند', 'القسم', 'الوحدة', 'البنود', 'نسبة الهدر %', 'الصافي', 'الإجمالي'])
    for (const r of Object.values(data.summary.by_rule ?? {})) {
      const d = unit_digits(String(r.unit))
      rows.push([
        r.code,
        r.name_ar,
        r.surface_ar || surface_ar(r.surface),
        r.unit_ar || unit_ar(r.unit),
        fmt_int(r.lines),
        csv_num(r.waste_pct * 100, 2),
        csv_num(r.net_qty, d),
        csv_num(r.gross_qty, d),
      ])
    }

    rows.push([])
    rows.push(['الملخّص حسب الفراغ'])
    rows.push(['الفراغ', 'رمز الفراغ', 'الوحدة', 'البنود', 'التنبيهات', 'الصافي', 'الإجمالي'])
    for (const s of Object.values(data.summary.by_space ?? {})) {
      for (const b of bucket_list(s.by_unit)) {
        const d = unit_digits(String(b.unit))
        rows.push([
          s.space_name,
          s.space_id,
          b.unit_ar || unit_ar(b.unit),
          fmt_int(b.lines),
          fmt_int(s.warnings),
          csv_num(b.net_qty, d),
          csv_num(b.gross_qty, d),
        ])
      }
    }

    if ((data.warnings ?? []).length) {
      rows.push([])
      rows.push(['التنبيهات'])
      for (const w of data.warnings) rows.push([w])
    }

    if (Object.keys(debounced_overrides).length) {
      rows.push([])
      rows.push(['تجاوزات الهدر المطبَّقة'])
      rows.push(['مفتاح القاعدة', 'الكود', 'الافتراضي %', 'المطبَّق %'])
      for (const [k, v] of Object.entries(debounced_overrides)) {
        const info = rule_by_key.get(k)
        rows.push([k, info?.code ?? '', csv_num((info?.waste_pct ?? 0) * 100, 2), csv_num(v * 100, 2)])
      }
    }

    // `sep=,` tells Excel which separator to use whatever the machine's locale list
    // separator is, and the BOM is what makes it open the Arabic as UTF-8 instead of
    // the ANSI code page — without both, this file opens as one column of mojibake.
    const body = `﻿sep=,\r\n${csv_rows(rows)}\r\n`
    const stamp = new Date().toISOString().slice(0, 10)
    download_blob(
      body,
      `${safe_filename(`boq-${project_name || project_id || 'project'}-${stamp}`)}.csv`,
      'text/csv;charset=utf-8',
    )
  }

  /* -- render --------------------------------------------------------------- */

  const override_entries = Object.entries(overrides)

  return (
    <div className="space-y-5">
      {/* -- heading ---------------------------------------------------------- */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-bold">جدول الكميات</h1>
          <p className="mt-0.5 max-w-2xl text-[13.5px] leading-relaxed text-muted">
            الكميات الصافية هي أساس الدفع؛ الكميات الإجمالية (بعد الهدر) أساس التوريد فقط. افتح
            أي بند لقراءة خطوات حسابه كما أنتجها المحرّك.
          </p>
        </div>
        <ButtonRow>
          {recomputing && (
            <Badge tone="info" size="sm" icon={<RefreshCw className="animate-spin" />}>
              جارٍ إعادة الحساب…
            </Badge>
          )}
          <Button
            variant="secondary"
            icon={<Download />}
            disabled={!data || data.lines.length === 0}
            onClick={export_csv}
          >
            تصدير CSV
          </Button>
          <Button
            variant="primary"
            icon={<Calculator />}
            loading={takeoff_q.isFetching}
            onClick={() => void takeoff_q.refetch()}
          >
            إعادة الحساب
          </Button>
        </ButtonRow>
      </div>

      {/* -- tiles ------------------------------------------------------------ */}
      {takeoff_q.isPending ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-surface px-4 py-3">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="mt-2.5 h-6 w-16" />
            </div>
          ))}
        </div>
      ) : data ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="عدد البنود"
            value={<Num>{fmt_int(data.summary.totals.lines)}</Num>}
            hint={
              <>
                <Num>{fmt_int(data.summary.totals.rules)}</Num> قاعدة
              </>
            }
          />
          <Stat label="عدد الفراغات" value={<Num>{fmt_int(data.summary.totals.spaces)}</Num>} />
          <Stat
            label="تنبيهات"
            value={<Num>{fmt_int(data.warnings?.length ?? data.summary.totals.warnings)}</Num>}
            tone={(data.warnings?.length ?? 0) > 0 ? 'warn' : 'default'}
            hint={
              flagged_count ? (
                <>
                  <Num>{fmt_int(flagged_count)}</Num> بنداً متأثراً
                </>
              ) : (
                'لا بنود متأثرة'
              )
            }
          />
          <Stat
            label="بنود بكمية صفر"
            value={<Num>{fmt_int(zero_rows.length)}</Num>}
            tone={zero_rows.length ? 'danger' : 'success'}
            hint={zero_rows.length ? 'راجعها قبل الإرسال' : 'كل البنود مقيسة'}
          />
        </div>
      ) : null}

      {/* -- the two alarms --------------------------------------------------- */}
      {zero_rows.length > 0 && (
        <div className="flex flex-wrap items-start gap-3 rounded-lg border border-danger/40 bg-danger-soft px-4 py-3.5">
          <AlertTriangle className="mt-0.5 size-5 shrink-0 text-danger" aria-hidden />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-danger">
              ⚠ تنبيه: <Num>{fmt_int(zero_rows.length)}</Num> بنداً بكمية صفر
            </div>
            <p className="mt-1 text-[13.5px] leading-relaxed text-fg/90">
              بند بكمية صفر يبدو في الجدول كأنه مقيس، وهو في الحقيقة بند لم يُقس: أبعاد ناقصة، أو
              فتحات تلتهم كامل المساحة، أو تشطيب مُسند إلى سطح غير معرَّف. راجعها في صفحة الفراغات
              قبل أن يخرج هذا الجدول من مكتبك.
            </p>
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {zero_rows.slice(0, 8).map((r) => (
                <li key={r.key}>
                  <button
                    type="button"
                    onClick={() => {
                      set_only_flagged(true)
                      set_expanded((prev) => new Set(prev).add(r.key))
                    }}
                    className="inline-flex items-center gap-1.5 rounded-full border border-danger/30 bg-surface px-2 py-0.5 text-[12px] text-fg hover:bg-surface-2"
                  >
                    <CodeChip>{r.line.code}</CodeChip>
                    <span className="text-muted">{r.line.space_name || r.line.space_id}</span>
                  </button>
                </li>
              ))}
              {zero_rows.length > 8 && (
                <li className="self-center text-[12px] text-muted">
                  و<Num>{fmt_int(zero_rows.length - 8)}</Num> غيرها…
                </li>
              )}
            </ul>
          </div>
        </div>
      )}

      {data && <WarningList warnings={data.warnings ?? []} title="⚠ تنبيهات الحصر" />}

      {/* -- active overrides -------------------------------------------------- */}
      {override_entries.length > 0 && (
        <Card flush>
          <CardHeader
            title="تجاوزات الهدر المطبَّقة"
            subtitle="تُرسل مع طلب الحصر ولا تُحفظ في المشروع. تختفي بمجرد إعادة تحميل الصفحة."
            actions={
              <Button size="sm" variant="ghost" icon={<RotateCcw />} onClick={() => set_overrides({})}>
                استعادة كل الافتراضيات
              </Button>
            }
          />
          <CardBody className="flex flex-wrap gap-2">
            {override_entries.map(([k, v]) => {
              const info = rule_by_key.get(k)
              return (
                <span
                  key={k}
                  className="inline-flex items-center gap-2 rounded-lg border border-accent/30 bg-accent-soft px-2.5 py-1.5 text-[12.5px]"
                >
                  <CodeChip>{info?.code ?? k}</CodeChip>
                  <span className="text-accent-fg">
                    <Num>{info ? fmt_pct(info.waste_pct) : MISSING}</Num>
                    <span className="mx-1">←</span>
                    <Num>{fmt_pct(v)}</Num>
                  </span>
                  <button
                    type="button"
                    onClick={() => clear_override(k)}
                    className="text-accent-fg/70 hover:text-accent-fg"
                    aria-label={`استعادة الافتراضي للقاعدة ${info?.code ?? k}`}
                  >
                    <RotateCcw className="size-3.5" aria-hidden />
                  </button>
                </span>
              )
            })}
          </CardBody>
        </Card>
      )}

      {/* -- the table --------------------------------------------------------- */}
      <Card flush>
        <CardHeader
          title="البنود"
          subtitle={data?.summary.basis_ar}
          actions={
            data ? (
              <Badge tone="neutral">
                <Num>{fmt_int(visible_rows.length)}</Num>
                {visible_rows.length === all_rows.length ? ' بند' : ` من ${fmt_int(all_rows.length)}`}
              </Badge>
            ) : null
          }
        />

        {data && data.lines.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface-2/40 px-4 py-3 sm:px-5">
            <div className="relative min-w-[14rem] flex-1">
              <Search
                aria-hidden
                className="pointer-events-none absolute inset-y-0 start-0 my-auto ms-3 size-4 text-subtle"
              />
              <Input
                size="sm"
                className="ps-9"
                value={text}
                placeholder="ابحث بالكود أو اسم البند أو الفراغ…"
                aria-label="بحث في البنود"
                onChange={(e) => set_text(e.target.value)}
              />
            </div>
            <Select
              size="sm"
              className="w-auto min-w-[10rem]"
              value={space_filter}
              aria-label="تصفية حسب الفراغ"
              options={space_options}
              onChange={(e) => set_space_filter(e.target.value)}
            />
            <Button
              size="sm"
              variant={only_flagged ? 'primary' : 'subtle'}
              aria-pressed={only_flagged}
              icon={<AlertTriangle />}
              onClick={() => set_only_flagged((v) => !v)}
            >
              المتأثرة فقط
              <Num>({fmt_int(flagged_count)})</Num>
            </Button>
            <Button
              size="sm"
              variant="subtle"
              className="ms-auto"
              onClick={() =>
                set_expanded((prev) =>
                  prev.size >= visible_rows.length && visible_rows.length > 0
                    ? new Set()
                    : new Set(visible_rows.map((r) => r.key)),
                )
              }
            >
              {expanded.size >= visible_rows.length && visible_rows.length > 0
                ? 'طيّ كل الخطوات'
                : 'فتح كل الخطوات'}
            </Button>
          </div>
        )}

        {takeoff_q.isPending ? (
          <TableWrap>
            <Table>
              <THead>
                <TR>
                  <TH>الكود</TH>
                  <TH>البند</TH>
                  <TH>الوحدة</TH>
                  <TH numeric>الصافية</TH>
                  <TH numeric>الهدر</TH>
                  <TH numeric>الإجمالية</TH>
                </TR>
              </THead>
              <SkeletonRows rows={8} cols={6} />
            </Table>
          </TableWrap>
        ) : takeoff_q.isError ? (
          <ErrorState
            message_ar={error_ar(takeoff_q.error)}
            action={
              <Button variant="primary" icon={<RefreshCw />} onClick={() => void takeoff_q.refetch()}>
                إعادة المحاولة
              </Button>
            }
          />
        ) : !data || data.lines.length === 0 ? (
          <EmptyState
            icon={<Calculator />}
            title="لم يُنتج المحرّك أي بند"
            description="لا توجد تشطيبات مُسندة إلى الفراغات، أو أن الأبعاد غير مكتملة. أضف التشطيبات في صفحة الفراغات، وراجع التنبيهات أعلاه إن وُجدت."
          />
        ) : visible.length === 0 ? (
          <EmptyState
            title="لا بنود تطابق التصفية"
            description="غيّر نص البحث أو الفراغ المحدَّد، أو ألغِ «المتأثرة فقط»."
            action={
              <Button
                variant="secondary"
                onClick={() => {
                  set_text('')
                  set_space_filter('')
                  set_only_flagged(false)
                }}
              >
                إلغاء التصفية
              </Button>
            }
          />
        ) : (
          <TableWrap className="max-h-[75vh] overflow-y-auto">
            <Table>
              <THead className="[&>tr>th]:sticky [&>tr>th]:top-0 [&>tr>th]:z-20 [&>tr>th]:bg-surface-2">
                <TR>
                  <TH className="w-10" aria-label="الخطوات" />
                  <TH>الكود</TH>
                  <TH>البند</TH>
                  <TH>الوحدة</TH>
                  <TH numeric>الكمية الصافية</TH>
                  <TH numeric>الهدر</TH>
                  <TH numeric>الكمية الإجمالية</TH>
                </TR>
              </THead>

              {visible.map((g) => (
                <TBody key={g.space_id}>
                  <TGroupRow colSpan={COLS} className="bg-surface-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[13px] font-bold">{g.space_name}</span>
                      <CodeChip>{g.space_id}</CodeChip>
                      <span className="text-[12px] font-normal text-muted">
                        <Num>{fmt_int(g.row_count)}</Num> بند
                      </span>
                      {g.flagged > 0 && (
                        <Badge tone="warn" size="sm" icon={<AlertTriangle />}>
                          <Num>{fmt_int(g.flagged)}</Num> متأثر
                        </Badge>
                      )}
                      {g.warnings.length > 0 && (
                        <span className="w-full text-[12px] font-normal leading-relaxed text-warn">
                          {g.warnings.join(' — ')}
                        </span>
                      )}
                    </div>
                  </TGroupRow>

                  {g.trades.map((t) => (
                    <TradeRows
                      key={`${g.space_id}-${t.surface}`}
                      group={t}
                      expanded={expanded}
                      onToggle={toggle}
                      rule_by_key={rule_by_key}
                      overrides={overrides}
                      onOverride={set_override}
                      onResetOverride={clear_override}
                    />
                  ))}
                </TBody>
              ))}
            </Table>
          </TableWrap>
        )}
      </Card>

      {/* -- summary ------------------------------------------------------------ */}
      {data && data.lines.length > 0 && <SummaryPanel data={data} overrides={debounced_overrides} />}
    </div>
  )
}

/* ==========================================================================
   Rows for one trade inside one space
   ========================================================================== */

function TradeRows({
  group,
  expanded,
  onToggle,
  rule_by_key,
  overrides,
  onOverride,
  onResetOverride,
}: {
  group: TradeGroup
  expanded: Set<string>
  onToggle: (key: string) => void
  rule_by_key: Map<string, RuleInfo>
  overrides: Record<string, number>
  onOverride: (rule_key: string, fraction: number) => void
  onResetOverride: (rule_key: string) => void
}) {
  const totals = [...group.totals.entries()]
  return (
    <>
      <TGroupRow colSpan={COLS} className="bg-surface-2 ps-8 font-semibold text-muted">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[12.5px]">{group.title}</span>
          <span
            className="text-[12px] font-normal text-subtle"
            title="مجموع محسوب في المتصفح من بنود هذه المجموعة — المرجع هو ملخّص المحرّك أسفل الصفحة"
          >
            {totals.map(([unit, v]) => (
              <span key={unit} className="ms-3 font-mono tabular-nums">
                <ArabicMath text={`${fmt_num(v.net, unit_digits(unit))} ${unit_ar(unit)}`} />
              </span>
            ))}
          </span>
        </div>
      </TGroupRow>

      {group.rows.map((r) => {
        const l = r.line
        const open = expanded.has(r.key)
        const panel_id = dom_id(r.key)
        const flagged = r.zero || r.estimated || r.warnings.length > 0
        const info = rule_by_key.get(l.rule_key)
        const override = Object.hasOwn(overrides, l.rule_key) ? overrides[l.rule_key] : null

        return (
          <Fragment key={r.key}>
          <TR
            interactive
            tone={r.zero ? 'danger' : flagged ? 'warn' : 'default'}
            onClick={() => onToggle(r.key)}
          >
            {/* The expander is a real button so the trail is reachable from the
                keyboard; the row click is a convenience on top of it. */}
            <TD className="w-10">
              <button
                type="button"
                aria-expanded={open}
                aria-controls={panel_id}
                aria-label={open ? `إخفاء خطوات حساب ${l.code}` : `عرض خطوات حساب ${l.code}`}
                onClick={(e) => {
                  e.stopPropagation()
                  onToggle(r.key)
                }}
                className="flex size-6 items-center justify-center rounded-md text-subtle hover:bg-surface-3 hover:text-fg"
              >
                <ChevronDown
                  aria-hidden
                  className={cn('size-4 transition-transform', open && 'rotate-180')}
                />
              </button>
            </TD>
            <TD>
              <CodeChip>{l.code}</CodeChip>
            </TD>
            <TD>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{l.name_ar}</span>
                {r.zero && (
                  <Badge tone="danger" size="sm" icon={<AlertTriangle />}>
                    كمية صفر
                  </Badge>
                )}
                {r.estimated && (
                  <Badge tone="warn" size="sm">
                    تقديرية
                  </Badge>
                )}
                {r.warnings.length > 0 && !r.zero && (
                  <Badge tone="warn" size="sm" icon={<AlertTriangle />}>
                    <Num>{fmt_int(r.warnings.length)}</Num> تنبيه
                  </Badge>
                )}
                {r.overridden && (
                  <Badge tone="accent" size="sm">
                    هدر مُعدّل
                  </Badge>
                )}
              </div>
            </TD>
            <TD>{unit_ar(l.unit)}</TD>
            <TD numeric className={r.zero ? 'font-semibold text-danger' : undefined}>
              {fmt_num(l.net_qty, unit_digits(String(l.unit)))}
            </TD>
            <TD numeric className={r.overridden ? 'text-accent-fg' : undefined}>
              {fmt_pct(l.waste_pct)}
            </TD>
            <TD numeric className="font-medium">
              {fmt_num(l.gross_qty, unit_digits(String(l.unit)))}
            </TD>

          </TR>

          {/* The trail is its OWN row, never nested inside the line's row: that
              keeps the table structure valid and lets the panel use a normal block
              layout instead of fighting table cell sizing. */}
          {open && (
            <tr id={panel_id}>
              <td colSpan={COLS} className="p-0">
                <BreakdownPanel
                  row={r}
                  default_pct={info ? info.waste_pct : null}
                  override={override}
                  onOverride={(f) => onOverride(l.rule_key, f)}
                  onResetOverride={() => onResetOverride(l.rule_key)}
                />
              </td>
            </tr>
          )}
          </Fragment>
        )
      })}
    </>
  )
}
