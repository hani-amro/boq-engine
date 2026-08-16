/**
 * Arabic terminology and number formatting — the browser half of `core/i18n.py`.
 *
 * The same three rules apply here as they do server-side:
 *
 * * **A number is never printed as a bare float.** `0.1 + 0.2` is
 *   `0.30000000000000004` and `12.1 * 1` can print as `12.100000000000001`. A
 *   quantity that prints like that in front of a client destroys the credibility of
 *   every other number on the page. Everything goes through {@link fmt_num}, which
 *   quantises on the DECIMAL STRING (never on the binary float) using commercial
 *   rounding — half away from zero, not banker's, and not `toFixed`, which rounds
 *   the binary value and so disagrees with the Python side on ties like 1.005.
 * * **Formatting never computes.** `fmt_money` picks its digit count per currency
 *   because Syrian pounds have no meaningful fraction and Jordanian dinars have
 *   three. That is a display decision; the stored number is untouched.
 * * **Western (ASCII) digits.** Levant contracts and interim certificates print
 *   Western digits inside Arabic text, and a number that has to be re-typed into
 *   Excel must survive the copy. {@link to_arabic_digits} exists for the rare
 *   surface that really wants Arabic-Indic numerals — never for a BOQ.
 *
 * Function names are snake_case on purpose: they are the same names as in
 * core/i18n.py, so a discrepancy between the two is a one-line diff to find.
 */

/* ==========================================================================
   Registries
   ========================================================================== */

/**
 * Unit codes exactly as a Levant BOQ writes them. "م.ط" is المتر الطولي — writing a
 * linear item as "م" alone is the single most common way a running metre gets read
 * (and paid) as a square metre.
 */
export const UNITS_AR: Record<string, string> = {
  m2: 'م²',
  m: 'م.ط',
  no: 'عدد',
  kg: 'كغ',
  L: 'لتر',
}

/** How many decimals each unit prints with. A count with decimals is nonsense and
 *  invites an argument; an area without two decimals hides up to 5 m² over a flat. */
export const UNIT_DIGITS: Record<string, number> = { m2: 2, m: 2, no: 0, kg: 2, L: 2 }

export const SPACE_TYPES_AR: Record<string, string> = {
  living: 'صالون/معيشة',
  bedroom: 'غرفة نوم',
  kitchen: 'مطبخ',
  bath: 'حمام',
  corridor: 'ممر',
  balcony: 'شرفة',
  stair: 'درج',
}

/** Line-level surface labels (singular — they sit in a "السطح" column next to one
 *  measured line). Chapter headings are plural and live below. */
export const SURFACES_AR: Record<string, string> = {
  floor: 'أرضية',
  wall: 'جدران',
  ceiling: 'سقف',
  skirting: 'وزرة',
  facade: 'واجهة',
}

/** Chapter titles for the BOQ table of contents / group headers. */
export const SURFACE_CHAPTERS_AR: Record<string, string> = {
  floor: 'أعمال الأرضيات',
  wall: 'أعمال الجدران',
  ceiling: 'أعمال الأسقف',
  skirting: 'أعمال الوزرة (النعلة)',
  facade: 'أعمال الواجهات',
}

export const OPENING_KINDS_AR: Record<string, string> = {
  door: 'باب',
  window: 'شباك',
  arch: 'قنطرة',
  niche: 'كوّة',
}

interface CurrencyRec {
  symbol_ar: string
  name_ar: string
  /** decimals SHOWN — a regional convention, not a rounding rule */
  digits: number
  place: 'prefix' | 'suffix'
}

/**
 * Currency registry: ISO code -> display behaviour. Display only; the stored
 * amount is never rounded by this table.
 */
export const CURRENCIES: Record<string, CurrencyRec> = {
  SYP: { symbol_ar: 'ل.س', name_ar: 'ليرة سورية', digits: 0, place: 'suffix' },
  USD: { symbol_ar: '$', name_ar: 'دولار أمريكي', digits: 2, place: 'prefix' },
  SAR: { symbol_ar: 'ر.س', name_ar: 'ريال سعودي', digits: 2, place: 'suffix' },
  AED: { symbol_ar: 'د.إ', name_ar: 'درهم إماراتي', digits: 2, place: 'suffix' },
  EGP: { symbol_ar: 'ج.م', name_ar: 'جنيه مصري', digits: 2, place: 'suffix' },
  JOD: { symbol_ar: 'د.أ', name_ar: 'دينار أردني', digits: 3, place: 'suffix' },
  LBP: { symbol_ar: 'ل.ل', name_ar: 'ليرة لبنانية', digits: 0, place: 'suffix' },
  ILS: { symbol_ar: '₪', name_ar: 'شيكل', digits: 2, place: 'suffix' },
  IQD: { symbol_ar: 'د.ع', name_ar: 'دينار عراقي', digits: 0, place: 'suffix' },
  QAR: { symbol_ar: 'ر.ق', name_ar: 'ريال قطري', digits: 2, place: 'suffix' },
  KWD: { symbol_ar: 'د.ك', name_ar: 'دينار كويتي', digits: 3, place: 'suffix' },
  OMR: { symbol_ar: 'ر.ع', name_ar: 'ريال عماني', digits: 3, place: 'suffix' },
  BHD: { symbol_ar: 'د.ب', name_ar: 'دينار بحريني', digits: 3, place: 'suffix' },
  EUR: { symbol_ar: '€', name_ar: 'يورو', digits: 2, place: 'prefix' },
}

/** What a missing / non-numeric value prints as. Never "0" — a silent zero in a BOQ
 *  is a silent under-quote — and never blank, which reads as "nothing here". */
export const MISSING = '—'

export const ARABIC_INDIC_DIGITS = '٠١٢٣٤٥٦٧٨٩'

const MAX_DIGITS = 6

/* ==========================================================================
   Decimal-string arithmetic — the only way to agree with Python's Decimal
   ========================================================================== */

/** Expand exponent notation to a plain decimal string ("1e-7" -> "0.0000001"). */
function to_plain(s: string): string {
  if (!/[eE]/.test(s)) return s
  const m = /^([+-]?)(\d+)(?:\.(\d+))?[eE]([+-]?\d+)$/.exec(s)
  if (!m) return s
  const sign = m[1] === '-' ? '-' : ''
  const digits = m[2] + (m[3] ?? '')
  const point = m[2].length + Number.parseInt(m[4], 10)
  if (point <= 0) return `${sign}0.${'0'.repeat(-point)}${digits}`
  if (point >= digits.length) return sign + digits + '0'.repeat(point - digits.length)
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`
}

/** Add 1 to a bare digit string, growing it if it carries past the front. */
function inc_digits(d: string): string {
  const out = d.split('')
  let i = out.length - 1
  for (; i >= 0; i--) {
    if (out[i] === '9') {
      out[i] = '0'
    } else {
      out[i] = String(Number(out[i]) + 1)
      return out.join('')
    }
  }
  return `1${out.join('')}`
}

/** Move the decimal point `places` to the right (negative moves left). */
function shift_point(plain: string, places: number): string {
  if (places === 0) return plain
  const neg = plain.startsWith('-')
  const body = neg ? plain.slice(1) : plain
  const [ip, fp = ''] = body.split('.')
  const digits = ip + fp
  const point = ip.length + places
  let out: string
  if (point <= 0) out = `0.${'0'.repeat(-point)}${digits}`
  else if (point >= digits.length) out = digits + '0'.repeat(point - digits.length)
  else out = `${digits.slice(0, point)}.${digits.slice(point)}`
  return (neg ? '-' : '') + out
}

/**
 * Quantise a plain decimal string to exactly `digits` decimals, rounding half
 * AWAY FROM ZERO. That is commercial rounding, which is what a bill of quantities
 * and an interim certificate use — banker's rounding would quietly shave a
 * fraction off every second tie and the column would stop matching the invoice.
 */
function quantize(plain: string, digits: number): string {
  let sign = ''
  let s = plain
  if (s.startsWith('-')) {
    sign = '-'
    s = s.slice(1)
  } else if (s.startsWith('+')) {
    s = s.slice(1)
  }
  const [ipRaw, fp = ''] = s.split('.')
  const ip = ipRaw === '' ? '0' : ipRaw

  let scaled: string
  if (fp.length <= digits) {
    scaled = ip + fp.padEnd(digits, '0')
  } else {
    scaled = ip + fp.slice(0, digits)
    if (fp.charCodeAt(digits) - 48 >= 5) scaled = inc_digits(scaled)
  }

  scaled = scaled.padStart(digits + 1, '0')
  const cut = scaled.length - digits
  let intPart = scaled.slice(0, cut).replace(/^0+(?=\d)/, '')
  const fracPart = digits > 0 ? scaled.slice(cut) : ''
  if (intPart === '') intPart = '0'

  // -0.00 is noise, not a measurement; normalise it away.
  if (sign === '-' && !/[1-9]/.test(intPart + fracPart)) sign = ''
  return sign + intPart + (digits > 0 ? `.${fracPart}` : '')
}

function group_thousands(intPart: string): string {
  const neg = intPart.startsWith('-')
  const body = neg ? intPart.slice(1) : intPart
  return (neg ? '-' : '') + body.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/**
 * Best-effort finite decimal string, or null. Goes through the number's shortest
 * round-trip representation on purpose: it is the same string Python's `str(float)`
 * produces, so `Decimal(str(v))` and this agree digit for digit before quantising.
 */
function dec(v: unknown): string | null {
  if (v === null || v === undefined || typeof v === 'boolean') return null
  let s: string
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return null
    s = String(v)
  } else if (typeof v === 'string') {
    s = v.trim()
    if (s === '' || !/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(s)) return null
  } else {
    return null
  }
  return to_plain(s)
}

function clamp_digits(d: unknown): number {
  const n = typeof d === 'number' && Number.isFinite(d) ? Math.trunc(d) : 2
  return Math.max(0, Math.min(MAX_DIGITS, n))
}

function cur(currency: unknown): CurrencyRec {
  const code = String(currency ?? '')
    .trim()
    .toUpperCase()
  return (
    CURRENCIES[code] ?? { symbol_ar: code, name_ar: code, digits: 2, place: 'suffix' }
  )
}

/* ==========================================================================
   Labels
   ========================================================================== */

/** Arabic unit label; an unknown code is echoed, never swallowed. */
export function unit_ar(unit: unknown): string {
  const k = String(unit ?? '')
  return UNITS_AR[k] ?? k
}

export function space_type_ar(spaceType: unknown): string {
  const k = String(spaceType ?? '')
  return SPACE_TYPES_AR[k] ?? k
}

export function surface_ar(surface: unknown): string {
  const k = String(surface ?? '')
  return SURFACES_AR[k] ?? k
}

export function surface_chapter_ar(surface: unknown): string {
  const k = String(surface ?? '')
  return SURFACE_CHAPTERS_AR[k] ?? surface_ar(k)
}

export function opening_kind_ar(kind: unknown): string {
  const k = String(kind ?? '')
  return OPENING_KINDS_AR[k] ?? k
}

export function currency_symbol_ar(currency: unknown): string {
  return cur(currency).symbol_ar
}

export function currency_name_ar(currency: unknown): string {
  return cur(currency).name_ar
}

/* ==========================================================================
   Numbers
   ========================================================================== */

/**
 * Format one number the way a BOQ prints it: `1,234.50` — never `1234.5000000001`.
 * `null`, NaN and infinity render as {@link MISSING}: a value we do not have is
 * never allowed to look like a zero we measured.
 */
export function fmt_num(v: unknown, digits = 2): string {
  const d = dec(v)
  if (d === null) return MISSING
  const n = clamp_digits(digits)
  const q = quantize(d, n)
  const [ip, fp] = q.split('.')
  return group_thousands(ip) + (fp ? `.${fp}` : '')
}

/** Counts: rounded to a whole number and grouped ("3", "1,240"). */
export function fmt_int(v: unknown): string {
  return fmt_num(v, 0)
}

/**
 * Percentage without trailing zeros: `0.10 -> "10%"`, `0.125 -> "12.5%"`.
 * `of_one` matches the rules file, where `waste_pct: 0.10` means 10%. Pass
 * `of_one: false` for a value already expressed in percent.
 */
export function fmt_pct(v: unknown, digits = 2, of_one = true): string {
  const d = dec(v)
  if (d === null) return MISSING
  let s = fmt_num(of_one ? shift_point(d, 2) : d, digits)
  if (s.includes('.')) s = s.replace(/\.?0+$/, '')
  return `${s || '0'}%`
}

/**
 * A measured quantity with its unit: `fmt_qty(55.2, 'm2') -> "55.20 م²"`.
 *
 * Decimals follow the unit ({@link UNIT_DIGITS}) unless overridden. The unit is
 * printed even when the value is missing — "— م²" is a visible gap, a bare "—" is
 * an invitation to assume zero.
 */
export function fmt_qty(v: unknown, unit: unknown, digits?: number): string {
  const code = String(unit ?? '')
  const n = digits === undefined ? (UNIT_DIGITS[code] ?? 2) : digits
  return `${fmt_num(v, n)} ${unit_ar(code)}`.trim()
}

/**
 * An amount with the right symbol on the right side for its currency.
 *
 * `fmt_money(1250, 'USD') -> "$1,250.00"` · `fmt_money(1250, 'SYP') -> "1,250 ل.س"`
 * `fmt_money(1250, 'JOD') -> "1,250.000 د.أ"`
 *
 * An unknown currency code is printed as-is beside the amount rather than dropped:
 * a total with no currency on it is a total nobody can honour.
 */
export function fmt_money(v: unknown, currency: unknown, digits?: number): string {
  const rec = cur(currency)
  const n = digits === undefined ? rec.digits : digits
  const amount = fmt_num(v, n)
  if (amount === MISSING) return `${MISSING} ${rec.symbol_ar}`.trim()
  return rec.place === 'prefix' ? `${rec.symbol_ar}${amount}` : `${amount} ${rec.symbol_ar}`
}

/**
 * A unit rate: `fmt_rate(45, 'USD', 'm2') -> "$45.00 / م²"`. The unit belongs to the
 * rate as much as the currency does — "45 دولار" without "/ م²" is the second most
 * expensive ambiguity in a price book, after a missing currency.
 */
export function fmt_rate(
  v: unknown,
  currency: unknown,
  unit: unknown,
  digits?: number,
): string {
  return `${fmt_money(v, currency, digits)} / ${unit_ar(unit)}`
}

/** Dimensions as the trade writes them: `fmt_dims([1.7, 0.7]) -> "1.70×0.70"`. */
export function fmt_dims(values: unknown[], digits = 2, sep = '×'): string {
  return values.map((v) => fmt_num(v, digits)).join(sep)
}

/** Western digits -> Arabic-Indic. Presentation only; never apply to anything that
 *  will be parsed, exported to a spreadsheet or compared. */
export function to_arabic_digits(text: unknown): string {
  return String(text).replace(/\d/g, (d) => ARABIC_INDIC_DIGITS[Number(d)])
}

/**
 * Parse what a surveyor actually types into a dimension field: Arabic-Indic
 * digits, an Arabic decimal separator (٫), a stray comma, surrounding spaces.
 * Returns null for anything that is not a finite number — the caller decides
 * whether that means "leave the old value" or "show a warning", never "use 0".
 */
export function parse_num(text: unknown): number | null {
  if (typeof text === 'number') return Number.isFinite(text) ? text : null
  let s = String(text ?? '').trim()
  if (s === '') return null
  s = s
    .replace(/[٠-٩]/g, (d) => String(ARABIC_INDIC_DIGITS.indexOf(d)))
    .replace(/[٫،]/g, '.')
    .replace(/,/g, '')
    .replace(/\s/g, '')
  if (!/^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(s)) return null
  const n = Number(s)
  return Number.isFinite(n) ? n : null
}

/* ==========================================================================
   Dates — Gregorian, Western digits, Arabic month names
   ========================================================================== */

// `-u-nu-latn` is load-bearing: the ar locale defaults to Arabic-Indic numerals,
// which contradicts every other number on the page.
const DATE_FMT = new Intl.DateTimeFormat('ar-u-ca-gregory-nu-latn', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
})
const DATETIME_FMT = new Intl.DateTimeFormat('ar-u-ca-gregory-nu-latn', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function as_date(v: unknown): Date | null {
  if (v === null || v === undefined || v === '') return null
  // A number is an epoch millisecond count, not a date string: `new Date("1753...")`
  // is Invalid Date, which would print "—" for a timestamp we actually have.
  const d = v instanceof Date ? v : typeof v === 'number' ? new Date(v) : new Date(String(v))
  return Number.isNaN(d.getTime()) ? null : d
}

export function fmt_date(v: unknown): string {
  const d = as_date(v)
  return d ? DATE_FMT.format(d) : MISSING
}

export function fmt_datetime(v: unknown): string {
  const d = as_date(v)
  return d ? DATETIME_FMT.format(d) : MISSING
}

/** "قبل ٣ دقائق" style relative time, for the autosave indicator and list rows. */
export function fmt_relative(v: unknown): string {
  const d = as_date(v)
  if (!d) return MISSING
  const secs = Math.round((Date.now() - d.getTime()) / 1000)
  if (secs < 45) return 'الآن'
  const rtf = new Intl.RelativeTimeFormat('ar-u-nu-latn', { numeric: 'auto' })
  const steps: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, 'second'],
    [3600, 'minute'],
    [86400, 'hour'],
    [604800, 'day'],
    [2629800, 'week'],
    [31557600, 'month'],
  ]
  let prev = 1
  for (const [limit, unit] of steps) {
    if (Math.abs(secs) < limit) return rtf.format(-Math.round(secs / prev), unit)
    prev = limit
  }
  return rtf.format(-Math.round(secs / 31557600), 'year')
}
