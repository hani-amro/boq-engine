/**
 * The editor's geometry model — the bridge between what an architect types and the
 * `Space` shape the engine reads.
 *
 * The wire format offers two ways to describe a room (`floor_area_m2` or `polygon`)
 * and one way to describe its walls (`walls[]`, which is also the only place an
 * opening can live). This module keeps those three in agreement, because every way
 * they can disagree is a quantity that silently changes:
 *
 * * **A rectangle is four walls, not one number.** Typing 4.20 × 3.10 writes
 *   `floor_area_m2 = 13.02` AND walls of 4.20, 3.10, 4.20, 3.10. Storing only the
 *   area would leave the wall run undefined, and wall area — the biggest number in a
 *   finishing BOQ — would come out as zero without a single warning.
 * * **A polygon is its own edges.** Switching to «غرفة غير منتظمة» rebuilds the wall
 *   list from the polygon edges and sets `floor_area_m2` to null, because the
 *   contract says the polygon wins when both are present. Leaving a stale typed area
 *   behind would mean the BOQ and the drawing disagree with no way to tell which
 *   was measured.
 * * **Rebuilding walls never drops an opening.** `sync_walls` reuses wall ids by
 *   position and moves the openings of any wall it removes onto the last surviving
 *   wall. A door that vanishes because a polygon lost a vertex is a deduction that
 *   silently stops happening.
 *
 * Every exported edit is `Space -> Space` and pure, so it can be handed straight to
 * `update_space(id, fn)` and inherit the store's single dirty/autosave path.
 */

import { make_opening, make_wall } from '../../store'
import { HOLE_KINDS, type Opening, type Point, type Space, type Wall } from '../../api/types'

/* ==========================================================================
   Numbers
   ========================================================================== */

/**
 * Six decimals, applied to every derived dimension before it is stored.
 *
 * `4.2 * 3.1` is `13.020000000000001` in binary floating point. That value is
 * correct enough to measure with and completely wrong to save: it travels to the
 * server, comes back, and prints as a number no one typed. Rounding at the moment
 * of derivation keeps the saved document readable without touching precision that
 * matters at millimetre scale.
 */
function r6(n: number): number {
  return Number.isFinite(n) ? Math.round(n * 1e6) / 1e6 : 0
}

/** A dimension the user has actually given us, or null. 0 means «لم يُقس بعد». */
function positive(n: number | null | undefined): number | null {
  return typeof n === 'number' && Number.isFinite(n) && n > 0 ? n : null
}

/* ==========================================================================
   Geometry mode
   ========================================================================== */

export type GeometryMode = 'rect' | 'polygon'

export const GEOMETRY_MODE_AR: Record<GeometryMode, string> = {
  rect: 'مستطيل (طول × عرض)',
  polygon: 'غرفة غير منتظمة',
}

/** A polygon on the document is the statement "this room is not a rectangle". */
export function geometry_mode(space: Space): GeometryMode {
  return space.polygon !== null && space.polygon.length > 0 ? 'polygon' : 'rect'
}

/* ==========================================================================
   Polygon arithmetic
   ========================================================================== */

/** Shoelace area. Absolute, so the point order (CW or CCW) cannot flip the sign. */
export function polygon_area_m2(points: Point[]): number {
  if (points.length < 3) return 0
  let twice = 0
  for (let i = 0; i < points.length; i++) {
    const a = points[i]
    const b = points[(i + 1) % points.length]
    twice += a[0] * b[1] - b[0] * a[1]
  }
  return r6(Math.abs(twice) / 2)
}

/** Edge lengths of the CLOSED polygon — edge `i` runs from point `i` to `i + 1`,
 *  and the last one closes back to the first. These become the wall lengths. */
export function polygon_edges_m(points: Point[]): number[] {
  if (points.length < 2) return []
  const out: number[] = []
  for (let i = 0; i < points.length; i++) {
    const a = points[i]
    const b = points[(i + 1) % points.length]
    out.push(r6(Math.hypot(b[0] - a[0], b[1] - a[1])))
  }
  return out
}

export function polygon_perimeter_m(points: Point[]): number {
  return r6(polygon_edges_m(points).reduce((n, e) => n + e, 0))
}

/** The rectangle a length/width pair describes, for the plan preview. */
export function rect_points(length_m: number, width_m: number): Point[] {
  return [
    [0, 0],
    [length_m, 0],
    [length_m, width_m],
    [0, width_m],
  ]
}

/* ==========================================================================
   Reading a space
   ========================================================================== */

/**
 * The length/width of a rectangular room, read back out of its walls.
 *
 * Wall 0 is the length and wall 1 the width by construction ({@link set_rect}
 * writes them in that order), so the pair survives a round trip through the server
 * without a second field on the document to hold it.
 */
export function rect_dims(space: Space): { length_m: number | null; width_m: number | null } {
  return {
    length_m: positive(space.walls[0]?.length_m),
    width_m: positive(space.walls[1]?.length_m),
  }
}

/** Total wall run, from the walls themselves. Null when nothing is measured. */
export function wall_run_m(space: Space): number | null {
  if (space.walls.length === 0) return null
  return positive(r6(space.walls.reduce((n, w) => n + (w.length_m || 0), 0)))
}

export interface Geometry {
  mode: GeometryMode
  /** Floor area in m², or null when the room still has no usable dimension. */
  area_m2: number | null
  /** Wall run in metres — the perimeter the finishes are measured along. */
  perimeter_m: number | null
  /** Gross wall area BEFORE any opening is deducted: perimeter × clear height. */
  wall_area_m2: number | null
  /**
   * The area came from a number typed straight onto the document, not from
   * dimensions — so there is no perimeter to go with it and nothing to check it
   * against. Worth saying out loud in the UI rather than showing it as measured.
   */
  typed_area: boolean
}

/** Everything the editor shows live while the user types. Nothing here is sent to
 *  the server as a quantity: the engine measures, this only lets a human check. */
export function geometry_of(space: Space): Geometry {
  const mode = geometry_mode(space)
  const height = positive(space.height_m)

  const finish = (area_m2: number | null, perimeter_m: number | null, typed_area: boolean): Geometry => ({
    mode,
    area_m2,
    perimeter_m,
    wall_area_m2: perimeter_m !== null && height !== null ? r6(perimeter_m * height) : null,
    typed_area,
  })

  if (mode === 'polygon') {
    const points = space.polygon ?? []
    const area = points.length >= 3 ? polygon_area_m2(points) : 0
    const perimeter = points.length >= 2 ? polygon_perimeter_m(points) : null
    return finish(positive(area), positive(perimeter), false)
  }

  const { length_m, width_m } = rect_dims(space)
  if (length_m !== null && width_m !== null) {
    return finish(r6(length_m * width_m), wall_run_m(space), false)
  }

  // No dimensions, but the document may still carry an area typed by an earlier
  // version of this editor. It is a real measurement; it is just unaccompanied.
  const stored = positive(space.floor_area_m2)
  return finish(stored, wall_run_m(space), stored !== null)
}

/* ==========================================================================
   Walls
   ========================================================================== */

/**
 * Resize a space's wall list to exactly `lengths`, in order.
 *
 * Walls are matched by POSITION so ids stay stable while a dimension is being
 * typed — otherwise every keystroke would mint a new wall id and detach the
 * openings on it. Openings belonging to walls that fall off the end are moved onto
 * the last surviving wall rather than deleted.
 */
export function sync_walls(space: Space, lengths: number[]): Wall[] {
  // Nothing to build: keep the walls (and their openings) exactly as they are.
  if (lengths.length === 0) return space.walls

  const taken = space.walls.map((w) => w.id)
  const out: Wall[] = []
  for (let i = 0; i < lengths.length; i++) {
    const length_m = r6(Math.max(0, lengths[i]))
    const current = space.walls[i] as Wall | undefined
    if (current) {
      out.push(
        current.length_m === length_m && current.height_m === space.height_m
          ? current
          : { ...current, length_m, height_m: space.height_m },
      )
    } else {
      const wall = make_wall({ length_m }, taken, space.height_m)
      taken.push(wall.id)
      out.push(wall)
    }
  }

  const orphans = space.walls.slice(lengths.length).flatMap((w) => w.openings)
  if (orphans.length > 0) {
    const last = out[out.length - 1]
    out[out.length - 1] = {
      ...last,
      openings: [...last.openings, ...orphans.map((o) => ({ ...o, wall_id: last.id }))],
    }
  }
  return out
}

/* ==========================================================================
   Edits — Space -> Space, for `update_space(id, fn)`
   ========================================================================== */

/**
 * Set the rectangle.
 *
 * Walls carry the clear height too, so a room whose height changes must push that
 * height down into every wall — a 3.20 m room whose walls still say 3.00 m is a
 * wall area that is 6% short on every surface in it, and nothing on screen says so.
 * This editor never exposes a per-wall height, so mirroring is always correct here.
 */
export function set_rect(space: Space, length_m: number | null, width_m: number | null): Space {
  const l = positive(length_m) ?? 0
  const w = positive(width_m) ?? 0
  return {
    ...space,
    polygon: null,
    walls: sync_walls(space, [l, w, l, w]),
    floor_area_m2: l > 0 && w > 0 ? r6(l * w) : null,
  }
}

/** Set the polygon, rebuild the walls from its edges, and hand the area over to it. */
export function set_polygon(space: Space, points: Point[]): Space {
  return {
    ...space,
    polygon: points,
    walls: sync_walls(space, polygon_edges_m(points)),
    // Null, not the shoelace figure: the contract says the polygon wins, and the
    // engine derives the area from it. Writing a browser-computed number here would
    // put a second, competing area on the document.
    floor_area_m2: null,
  }
}

/** Switch to «غرفة غير منتظمة», seeding the polygon from the rectangle when there
 *  is one, so an already-measured room keeps its measurement. */
export function to_polygon(space: Space): Space {
  const { length_m, width_m } = rect_dims(space)
  const seed: Point[] =
    length_m !== null && width_m !== null
      ? rect_points(length_m, width_m)
      : [
          [0, 0],
          [0, 0],
          [0, 0],
          [0, 0],
        ]
  return set_polygon(space, seed)
}

/** Switch back to a rectangle, keeping the first two edge lengths as length/width.
 *  The polygon is discarded — the caller must confirm that with the user first. */
export function to_rect(space: Space): Space {
  const first = space.walls[0]?.length_m ?? 0
  const second = space.walls[1]?.length_m ?? 0
  return set_rect({ ...space, polygon: null }, first, second)
}

/** The clear height, mirrored into every wall. See {@link set_rect}. */
export function set_height(space: Space, height_m: number | null): Space {
  const h = positive(height_m) ?? 0
  if (h === space.height_m) return space
  return {
    ...space,
    height_m: h,
    walls: space.walls.map((w) => (w.height_m === h ? w : { ...w, height_m: h })),
  }
}

/* ==========================================================================
   Openings
   ========================================================================== */

export interface OpeningRef {
  opening: Opening
  wall: Wall
  /** 1-based position of the wall in the space, for the «الجدار ٢» label. */
  wall_no: number
}

/** Every opening in the space, flattened, in wall order — the way the editor lists
 *  them. Which wall a door is on rarely matters to a finishing take-off; that it
 *  exists at all always does. */
export function list_openings(space: Space): OpeningRef[] {
  const out: OpeningRef[] = []
  space.walls.forEach((wall, i) => {
    wall.openings.forEach((opening) => out.push({ opening, wall, wall_no: i + 1 }))
  })
  return out
}

/** A door, a window and an arch are holes and get deducted. A niche is a recess and
 *  gets ADDED — the single most misunderstood thing in this whole form. */
export function is_hole(kind: string): boolean {
  return (HOLE_KINDS as readonly string[]).includes(kind)
}

/** Face area of an opening, all repeats included. What gets deducted (or added) is
 *  decided by the engine against the rule's threshold; this is only the raw face. */
export function opening_face_m2(o: Opening): number {
  const w = Math.max(0, o.width_m || 0)
  const h = Math.max(0, o.height_m || 0)
  const n = Math.max(0, Math.trunc(o.count || 0))
  return r6(w * h * n)
}

/**
 * Add an opening.
 *
 * A room with no dimensions yet has no walls, and an opening can only exist inside
 * one. Rather than refusing — "measure the room before you can record the door you
 * are standing in front of" is exactly backwards on site — a wall is materialised
 * to hold it, and picks up its length when the room is measured.
 */
export function add_opening_to(space: Space, init: Partial<Opening> = {}): Space {
  const walls = space.walls.length > 0 ? space.walls : [make_wall({ length_m: 0 }, [], space.height_m)]
  const taken = walls.flatMap((w) => w.openings.map((o) => o.id))
  const target = walls[0]
  const opening = make_opening({ kind: 'door', count: 1, ...init }, taken, target.id)
  return {
    ...space,
    walls: walls.map((w, i) => (i === 0 ? { ...w, openings: [...w.openings, opening] } : w)),
  }
}

export function update_opening_in(
  space: Space,
  opening_id: string,
  patch: Partial<Opening>,
): Space {
  return {
    ...space,
    walls: space.walls.map((w) =>
      w.openings.some((o) => o.id === opening_id)
        ? {
            ...w,
            openings: w.openings.map((o) => (o.id === opening_id ? { ...o, ...patch } : o)),
          }
        : w,
    ),
  }
}

export function remove_opening_from(space: Space, opening_id: string): Space {
  return {
    ...space,
    walls: space.walls.map((w) =>
      w.openings.some((o) => o.id === opening_id)
        ? { ...w, openings: w.openings.filter((o) => o.id !== opening_id) }
        : w,
    ),
  }
}

/** Move an opening to another wall, in one edit — detaching and re-attaching as two
 *  separate edits would leave a moment where the opening exists nowhere. */
export function move_opening_to_wall(space: Space, opening_id: string, wall_id: string): Space {
  const found = space.walls.flatMap((w) => w.openings).find((o) => o.id === opening_id)
  if (!found || !space.walls.some((w) => w.id === wall_id)) return space
  if (found.wall_id === wall_id) return space
  return {
    ...space,
    walls: space.walls.map((w) => {
      const openings = w.openings.filter((o) => o.id !== opening_id)
      return w.id === wall_id
        ? { ...w, openings: [...openings, { ...found, wall_id }] }
        : openings.length === w.openings.length
          ? w
          : { ...w, openings }
    }),
  }
}

/* ==========================================================================
   Finish spec parameters
   ========================================================================== */

/**
 * Arabic labels for the spec parameters the rules file exposes.
 *
 * `spec_params` is an open map — a rule can introduce a parameter this table has
 * never heard of. An unknown key is therefore never hidden and never guessed at: it
 * is shown as the raw identifier ({@link SpecMeta.raw}), because a parameter the
 * user cannot name is still a parameter that changes the quantity.
 */
const SPEC_LABELS_AR: Record<string, string> = {
  tile: 'مقاس البلاطة',
  tile_size: 'مقاس البلاطة',
  tile_l: 'طول البلاطة',
  tile_w: 'عرض البلاطة',
  tile_length: 'طول البلاطة',
  tile_width: 'عرض البلاطة',
  joint: 'عرض الفاصل (الروبة)',
  grout: 'الروبة',
  coats: 'عدد الطبقات',
  layers: 'عدد الطبقات',
  primer: 'وجه أساس (برايمر)',
  thickness: 'السماكة',
  mortar_thickness: 'سماكة المونة',
  screed_thickness: 'سماكة الفرشة',
  plaster_thickness: 'سماكة اللياسة',
  mix_ratio: 'نسبة الخلط',
  waste: 'نسبة الهدر',
  waste_pct: 'نسبة الهدر',
  deduct_threshold: 'حدّ خصم الفتحات',
  opening_deduct_threshold: 'حدّ خصم الفتحات',
  min_deduct: 'أصغر فتحة تُخصم',
  skirting_h: 'ارتفاع الوزرة',
  skirting_height: 'ارتفاع الوزرة',
  height: 'الارتفاع',
  width: 'العرض',
  length: 'الطول',
  pattern: 'نمط التركيب',
  finish: 'نوع الإنهاء',
  color: 'اللون',
  brand: 'الصنف',
  substrate: 'الأرضية القاعدية',
  reinforcement: 'التسليح',
  sealer: 'مادة الحماية',
  count: 'العدد',
  price_note: 'ملاحظة سعرية',
}

const UNIT_SUFFIX: Record<string, { unit: string; digits: number }> = {
  m2: { unit: 'm2', digits: 2 },
  m: { unit: 'm', digits: 2 },
  mm: { unit: 'مم', digits: 0 },
  cm: { unit: 'سم', digits: 1 },
  kg: { unit: 'kg', digits: 2 },
  l: { unit: 'L', digits: 2 },
  pct: { unit: '٪', digits: 4 },
}

export interface SpecMeta {
  /** Arabic label, or the raw identifier when the parameter is not in the table. */
  label: string
  /** True when `label` is a raw identifier and must be rendered as a code chip. */
  raw: boolean
  kind: 'number' | 'text' | 'bool'
  unit?: string
  digits: number
  hint_ar?: string
}

/** Best-effort presentation for one `spec_params` entry. */
export function spec_meta(name: string, def: unknown): SpecMeta {
  const suffix = /_([a-z0-9]+)$/i.exec(name)?.[1]?.toLowerCase()
  const unit_rec = suffix ? UNIT_SUFFIX[suffix] : undefined
  const stem = unit_rec ? name.slice(0, name.length - suffix!.length - 1) : name

  const known = SPEC_LABELS_AR[name] ?? SPEC_LABELS_AR[stem]
  const kind: SpecMeta['kind'] =
    typeof def === 'boolean' ? 'bool' : typeof def === 'number' ? 'number' : 'text'

  const counting = /(^|_)(count|coats|layers|no|n)$/i.test(name)
  const digits = counting ? 0 : (unit_rec?.digits ?? 2)

  let hint_ar: string | undefined
  if (suffix === 'pct' && typeof def === 'number' && def > 0 && def <= 1) {
    hint_ar = 'تُكتب ككسر من واحد: 0.10 تعني 10٪.'
  }

  return {
    label: known ?? name,
    raw: known === undefined,
    kind,
    unit: counting ? 'no' : unit_rec?.unit,
    digits,
    hint_ar,
  }
}
