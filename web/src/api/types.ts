/**
 * TypeScript mirrors of the pinned data contract.
 *
 * Every field name here is copied verbatim from `core/schema.py`. The Python side
 * calls those names frozen — renaming one is a breaking change to the whole
 * product, not a refactor — so this file is a transcription, never an adaptation.
 * If a name reads oddly in TypeScript (`floor_area_m2`, `name_ar`), that is the
 * point: the wire format is the contract, and a camelCase translation layer is one
 * more place a quantity can silently change shape.
 *
 * Arabic is the deliverable language; identifiers and comments stay English.
 */

/* ==========================================================================
   Closed vocabularies (core/schema.py registries)
   ========================================================================== */

export const OPENING_KINDS = ['door', 'window', 'arch', 'niche'] as const
export type OpeningKind = (typeof OPENING_KINDS)[number]

/** The kinds that are a genuine hole in a wall. A niche is a recess: it is added
 *  (back + four sides), never deducted. */
export const HOLE_KINDS = ['door', 'window', 'arch'] as const

export const SPACE_TYPES = [
  'living',
  'bedroom',
  'kitchen',
  'bath',
  'corridor',
  'balcony',
  'stair',
] as const
export type SpaceType = (typeof SPACE_TYPES)[number]

export const SURFACES = ['floor', 'wall', 'ceiling', 'skirting', 'facade'] as const
export type Surface = (typeof SURFACES)[number]

export const UNITS = ['m2', 'm', 'no', 'kg', 'L'] as const
export type Unit = (typeof UNITS)[number]

/* ==========================================================================
   Project tree — Project > Space > Wall > Opening, plus FinishAssignment
   ========================================================================== */

/** `[x, y]` in metres. Serialised as a list of 2-element lists by Space.to_dict(). */
export type Point = [number, number]

/** A hole (or recess) in a wall. `count` means identical repeats. */
export interface Opening {
  id: string
  kind: OpeningKind | string
  width_m: number
  height_m: number
  count: number
  wall_id: string | null
}

export interface Wall {
  id: string
  length_m: number
  height_m: number
  is_external: boolean
  openings: Opening[]
}

/**
 * Which finish system is applied to which surface of a space.
 * `system` is a rule_key from core/rules/finishes.yaml; `spec` carries the
 * per-project parameters (tile size, coats, deduction threshold overrides, ...).
 */
export interface FinishAssignment {
  surface: Surface | string
  system: string
  spec: Record<string, unknown>
}

/**
 * One room / zone.
 *
 * `floor_area_m2 === null` means "derive it from the polygon". When both are
 * present the polygon wins — it is the measured drawing, the number is the typed
 * assumption. When neither is present the engine raises rather than reporting 0.
 */
export interface Space {
  id: string
  name: string
  type: SpaceType | string
  height_m: number
  walls: Wall[]
  floor_area_m2: number | null
  polygon: Point[] | null
  finishes: FinishAssignment[]
}

/** Project.to_dict() — the exact body of PUT /api/projects/{id}. */
export interface Project {
  id: string
  name: string
  client_name: string
  region: string
  currency: string
  created_at: string
  spaces: Space[]
}

/* ==========================================================================
   Engine output
   ========================================================================== */

/**
 * One measured line of the bill of quantities.
 *
 * `breakdown` is a mandatory Arabic audit trail — one entry per arithmetic step,
 * with the substituted values. A quantity nobody can trace is a quantity nobody
 * can defend in an interim certificate, so never render a line without a path to
 * its breakdown.
 */
export interface BOQLine {
  code: string
  name_ar: string
  space_id: string
  space_name: string
  surface: Surface | string
  unit: Unit | string
  net_qty: number
  waste_pct: number
  gross_qty: number
  rule_key: string
  breakdown: string[]
}

/** A BOQ line with money on it, and the price-book id+version that put it there. */
export interface PricedLine {
  line: BOQLine
  material_rate: number
  labor_rate: number
  material_total: number
  labor_total: number
  total: number
  currency: string
  /** price-book id + version, e.g. "levant-base@1.2.0" */
  rate_source: string
  /** "net" (payment) | "gross" (procurement) */
  qty_basis: 'net' | 'gross' | string
}

/* ==========================================================================
   Take-off summary (core/boq_engine.summarize)
   ========================================================================== */

/** The shape every roll-up bucket shares. Quantities are only ever summed WITHIN
 *  one unit — م² and م.ط are never added. */
export interface QtyBucket {
  unit: Unit | string
  unit_ar: string
  net_qty: number
  gross_qty: number
  lines: number
}

export interface RuleBucket extends QtyBucket {
  rule_key: string
  code: string
  name_ar: string
  surface: Surface | string
  surface_ar: string
  waste_pct: number
  /** space_ids this rule was measured in */
  spaces: string[]
}

/** Per-space bucket; same as RuleBucket minus the cross-space fields. */
export interface SpaceRuleBucket extends QtyBucket {
  rule_key: string
  code: string
  name_ar: string
  surface: Surface | string
  waste_pct: number
}

export interface SpaceSummary {
  space_id: string
  space_name: string
  lines: number
  warnings: number
  by_rule: Record<string, SpaceRuleBucket>
  by_unit: Record<string, QtyBucket>
  warnings_ar: string[]
}

export interface SurfaceSummary {
  surface: Surface | string
  surface_ar: string
  by_unit: Record<string, QtyBucket>
}

export interface SummaryTotals {
  lines: number
  spaces: number
  rules: number
  warnings: number
}

export interface TakeoffSummary {
  by_rule: Record<string, RuleBucket>
  by_space: Record<string, SpaceSummary>
  by_surface: Record<string, SurfaceSummary>
  by_unit: Record<string, QtyBucket>
  totals: SummaryTotals
  warnings: string[]
  /** The measurement-basis paragraph printed at the head of the BOQ. */
  basis_ar: string
}

/* ==========================================================================
   HTTP contract — response envelopes
   ========================================================================== */

export interface Health {
  ok: boolean
  core_version: string
  rules_count: number
}

/** GET /api/rules */
export interface RuleInfo {
  rule_key: string
  code: string
  name_ar: string
  surface: Surface | string
  unit: Unit | string
  waste_pct: number
  /** parameter name -> default value, the editable half of FinishAssignment.spec */
  spec_params: Record<string, unknown>
  notes_ar: string
}

/** GET /api/space-types */
export interface SpaceTypeInfo {
  key: SpaceType | string
  name_ar: string
}

/** GET /api/price-books */
export interface PriceBookInfo {
  region: string
  currency: string
  version: string
  is_placeholder: boolean
  effective_date: string
}

/** GET /api/projects — the list row, NOT the full project. */
export interface ProjectListItem {
  id: string
  name: string
  client_name: string
  region: string
  currency: string
  updated_at: string
  space_count: number
}

/** POST /api/projects body */
export interface CreateProjectBody {
  name: string
  client_name: string
  region: string
  currency: string
}

/** GET /api/projects/{id} — the document plus the version it was read at. */
export interface GetProjectResult {
  project: Project
  /** The server version this copy was taken from; sent back on PUT as the base of
   *  the edit. Null only when talking to a server that predates the field. */
  updated_at: string | null
}

export interface PutProjectResult {
  ok: boolean
  updated_at: string
  /** What was saved despite being incomplete — a room with no dimensions is kept,
   *  not refused, and says so here. Absent on older servers. */
  warnings?: string[]
}

/** Per-space, per-rule spec overrides applied for one take-off / quote only.
 *  Shape is intentionally open: it is forwarded to the engine untouched. */
export type Overrides = Record<string, unknown>

/** POST /api/projects/{id}/takeoff */
export interface TakeoffResult {
  lines: BOQLine[]
  summary: TakeoffSummary
  warnings: string[]
}

export interface QuoteTerms {
  overhead_pct: number
  profit_pct: number
  tax_pct: number
}

/** POST /api/projects/{id}/quote body */
export interface QuoteBody {
  region: string
  version?: string
  terms: QuoteTerms
  overrides?: Overrides
}

export interface QuoteTotals {
  subtotal: number
  overhead: number
  profit: number
  tax: number
  grand_total: number
  currency: string
}

export interface QuoteBookRef {
  region: string
  version: string
  is_placeholder: boolean
}

/**
 * POST /api/projects/{id}/quote
 *
 * `blocked_reason_ar` non-null means the quote MUST NOT be shown to a client —
 * placeholder rates, a missing FX capture, a missing rate. Render the reason and
 * disable export; never fall back to displaying the numbers anyway.
 */
export interface QuoteResult {
  lines: PricedLine[]
  totals: QuoteTotals
  blocked_reason_ar: string | null
  book: QuoteBookRef
  warnings: string[]
}

/* ==========================================================================
   Errors
   ========================================================================== */

/** `{error: {code, message_ar}}` — message_ar is shown to the user verbatim. */
export interface ApiErrorBody {
  error: {
    code: string
    message_ar: string
  }
}
