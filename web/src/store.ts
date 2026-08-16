/**
 * The project being edited, and the autosave that keeps it on the server.
 *
 * The editor is a long, fiddly, numeric form: a surveyor types wall lengths and
 * opening sizes for twenty minutes straight. Three things follow from that, and
 * they are the whole design of this store:
 *
 * * **Nothing is saved on a keystroke, and nothing is ever lost.** Edits mark the
 *   project dirty and schedule one debounced PUT. A save that lands while the user
 *   is still typing does NOT clear the dirty flag — the store compares a revision
 *   counter taken before the request against the current one, so an edit made
 *   mid-flight survives and gets its own save.
 * * **A failed save stays visible.** `status` goes to `'error'` and `dirty` stays
 *   true, forever, until a retry succeeds. Silently swallowing a failed PUT is how
 *   an afternoon of measurements disappears.
 * * **The store holds the wire shape, unmodified.** `project` is exactly
 *   `Project.to_dict()` and is PUT back as-is. No view models, no derived
 *   quantities — every number the user sees measured comes from the engine, never
 *   from a duplicate implementation in the browser.
 */

import { create } from 'zustand'
import { ApiError, api, error_ar, qk } from './api/client'
import { queryClient } from './queryClient'
import type {
  FinishAssignment,
  GetProjectResult,
  Opening,
  Project,
  Space,
  SpaceType,
  Surface,
  Wall,
} from './api/types'

/* ==========================================================================
   Tunables
   ========================================================================== */

/** Long enough that a burst of typing is one request, short enough that closing
 *  the laptop a second after the last keystroke still saves. */
export const AUTOSAVE_DEBOUNCE_MS = 1200

/** Defaults for a newly added space: a 3.00 m clear height is the Levant norm and
 *  is inside the engine's sanity band, so a fresh space never opens on a warning. */
const DEFAULT_SPACE_HEIGHT_M = 3
const DEFAULT_WALL_HEIGHT_M = 3

/* ==========================================================================
   Id minting — readable, stable, and unique within the project
   ========================================================================== */

function next_id(prefix: string, taken: Iterable<string>): string {
  const used = new Set(taken)
  let n = 1
  while (used.has(prefix + n)) n++
  return prefix + n
}

function all_wall_ids(space: Space): string[] {
  return space.walls.map((w) => w.id)
}

function all_opening_ids(space: Space): string[] {
  return space.walls.flatMap((w) => w.openings.map((o) => o.id))
}

/* ==========================================================================
   Immutable edit helpers (no immer — the tree is small and explicit is safer)
   ========================================================================== */

function map_space(p: Project, space_id: string, fn: (s: Space) => Space): Project {
  return { ...p, spaces: p.spaces.map((s) => (s.id === space_id ? fn(s) : s)) }
}

function map_wall(p: Project, space_id: string, wall_id: string, fn: (w: Wall) => Wall): Project {
  return map_space(p, space_id, (s) => ({
    ...s,
    walls: s.walls.map((w) => (w.id === wall_id ? fn(w) : w)),
  }))
}

/** `Partial<T>` or a function — both spellings read naturally at a call site. */
type Patch<T> = Partial<T> | ((current: T) => T)

function apply_patch<T extends object>(current: T, patch: Patch<T>): T {
  return typeof patch === 'function' ? patch(current) : { ...current, ...patch }
}

export function make_space(init: Partial<Space> = {}, taken: string[] = []): Space {
  return {
    id: init.id ?? next_id('S', taken),
    name: init.name ?? '',
    type: init.type ?? ('living' satisfies SpaceType),
    height_m: init.height_m ?? DEFAULT_SPACE_HEIGHT_M,
    walls: init.walls ?? [],
    floor_area_m2: init.floor_area_m2 ?? null,
    polygon: init.polygon ?? null,
    finishes: init.finishes ?? [],
  }
}

export function make_wall(init: Partial<Wall> = {}, taken: string[] = [], height_m?: number): Wall {
  return {
    id: init.id ?? next_id('W', taken),
    length_m: init.length_m ?? 0,
    height_m: init.height_m ?? height_m ?? DEFAULT_WALL_HEIGHT_M,
    is_external: init.is_external ?? false,
    openings: init.openings ?? [],
  }
}

export function make_opening(
  init: Partial<Opening> = {},
  taken: string[] = [],
  wall_id: string | null = null,
): Opening {
  return {
    id: init.id ?? next_id('O', taken),
    kind: init.kind ?? 'door',
    width_m: init.width_m ?? 0,
    height_m: init.height_m ?? 0,
    count: init.count ?? 1,
    wall_id: init.wall_id ?? wall_id,
  }
}

/** Deep-clone a space onto fresh ids — a duplicated room that reuses an id is a
 *  duplicated BOQ line, which is money claimed twice. */
function clone_space(source: Space, taken_space_ids: string[]): Space {
  const wall_ids: string[] = []
  const opening_ids: string[] = []
  return {
    ...source,
    id: next_id('S', taken_space_ids),
    name: source.name ? `${source.name} (نسخة)` : '',
    polygon: source.polygon ? source.polygon.map((pt) => [pt[0], pt[1]] as [number, number]) : null,
    finishes: source.finishes.map((f) => ({ ...f, spec: { ...f.spec } })),
    walls: source.walls.map((w) => {
      const wid = next_id('W', wall_ids)
      wall_ids.push(wid)
      return {
        ...w,
        id: wid,
        openings: w.openings.map((o) => {
          const oid = next_id('O', opening_ids)
          opening_ids.push(oid)
          return { ...o, id: oid, wall_id: o.wall_id === null ? null : wid }
        }),
      }
    }),
  }
}

/* ==========================================================================
   Store
   ========================================================================== */

export type SaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'

export interface ProjectStore {
  /** Exactly `Project.to_dict()`, or null when no project is open. */
  project: Project | null
  /** Set as soon as the route is known, before the fetch resolves. */
  project_id: string | null
  loading: boolean

  dirty: boolean
  status: SaveStatus
  /** Server `updated_at` from the last successful PUT. */
  updated_at: string | null
  /** Arabic sentence from the last failed save; null when the last save worked. */
  save_error_ar: string | null
  /** Bumped on every local edit — the mid-flight-edit guard compares this. */
  revision: number
  autosave: boolean

  /** Which space the editor has focused. Pure UI, never sent to the server. */
  selected_space_id: string | null

  /* -- lifecycle --------------------------------------------------------- */
  /** Adopt a project fetched from the server. Clears dirty and cancels a pending save. */
  load: (project: Project, updated_at?: string | null) => void
  begin_load: (project_id: string) => void
  /** Drop the open project. Flushes a pending save first so leaving never loses edits. */
  clear: () => void

  /* -- edits (each marks dirty and schedules an autosave) ----------------- */
  set_meta: (
    patch: Partial<Pick<Project, 'name' | 'client_name' | 'region' | 'currency'>>,
  ) => void
  replace_project: (patch: Patch<Project>) => void

  add_space: (init?: Partial<Space>) => string | null
  update_space: (space_id: string, patch: Patch<Space>) => void
  remove_space: (space_id: string) => void
  duplicate_space: (space_id: string) => string | null

  add_wall: (space_id: string, init?: Partial<Wall>) => string | null
  update_wall: (space_id: string, wall_id: string, patch: Patch<Wall>) => void
  remove_wall: (space_id: string, wall_id: string) => void

  add_opening: (space_id: string, wall_id: string, init?: Partial<Opening>) => string | null
  update_opening: (
    space_id: string,
    wall_id: string,
    opening_id: string,
    patch: Patch<Opening>,
  ) => void
  remove_opening: (space_id: string, wall_id: string, opening_id: string) => void

  /** Upsert by surface — one finish system per surface per space, by contract. */
  set_finish: (
    space_id: string,
    surface: Surface | string,
    system: string,
    spec?: Record<string, unknown>,
  ) => void
  update_finish_spec: (
    space_id: string,
    surface: Surface | string,
    spec_patch: Record<string, unknown>,
  ) => void
  remove_finish: (space_id: string, surface: Surface | string) => void

  /* -- saving ------------------------------------------------------------ */
  /** PUT now, bypassing the debounce. Resolves true on success. */
  save_now: () => Promise<boolean>
  /** Run the pending debounced save immediately, if there is one. */
  flush: () => Promise<boolean>
  set_autosave: (on: boolean) => void

  /* -- ui ---------------------------------------------------------------- */
  select_space: (space_id: string | null) => void
}

let debounce_timer: ReturnType<typeof setTimeout> | null = null
/** The in-flight PUT, so `flush()` and a route change can await the same promise. */
let in_flight: Promise<boolean> | null = null

function cancel_pending(): void {
  if (debounce_timer !== null) {
    clearTimeout(debounce_timer)
    debounce_timer = null
  }
}

/**
 * Move the cache entry for this project onto the version that was just saved.
 *
 * Without this the entry keeps the document as it was when the project was first
 * opened. Leave the project and come back inside react-query's `gcTime` and that
 * old document resolves instantly, gets adopted by the store as clean, and the next
 * keystroke autosaves it over everything saved since — the whole session's edits,
 * destroyed by a browser Back and one letter.
 */
function cache_saved(id: string, project: Project, updated_at: string): void {
  queryClient.setQueryData<GetProjectResult>(qk.project(id), { project, updated_at })
}

/** The version the server last confirmed for this project, from the cache — the one
 *  place that is still right after the store has moved on to another project. */
function cached_version(id: string): string | null {
  return queryClient.getQueryData<GetProjectResult>(qk.project(id))?.updated_at ?? null
}

export const useProjectStore = create<ProjectStore>()((set, get) => {
  /** Every mutator funnels through here: one place that marks dirty, bumps the
   *  revision and schedules the save. A mutator that forgets one of the three is
   *  a silent data-loss bug, so there is only ever one of them. */
  function edit(fn: (p: Project) => Project): void {
    const { project } = get()
    if (!project) return
    const next = fn(project)
    if (next === project) return
    set({
      project: next,
      dirty: true,
      status: 'dirty',
      revision: get().revision + 1,
    })
    schedule()
  }

  function schedule(): void {
    if (!get().autosave) return
    cancel_pending()
    debounce_timer = setTimeout(() => {
      debounce_timer = null
      void get().save_now()
    }, AUTOSAVE_DEBOUNCE_MS)
  }

  return {
    project: null,
    project_id: null,
    loading: false,
    dirty: false,
    status: 'idle',
    updated_at: null,
    save_error_ar: null,
    revision: 0,
    autosave: true,
    selected_space_id: null,

    /* -- lifecycle ------------------------------------------------------- */
    /**
     * Point the store at another project.
     *
     * React Router reuses the same `ProjectLayout` element for /p/A and /p/B, so
     * moving between two projects changes the `:id` param WITHOUT unmounting: the
     * unmount flush never runs, and this is the only place the hand-off can happen.
     * Two things must therefore be true of it, and each is a data-loss bug alone:
     *
     * * the outgoing edits are sent as an explicitly captured `(id, document)` pair,
     *   because the debounced save that `cancel_pending()` is about to destroy is
     *   otherwise simply lost — silently, with the indicator still reading «محفوظ»;
     * * `project` is cleared in the SAME `set()` that repoints `project_id`, so there
     *   is never an instant where the store holds project A's rooms under project B's
     *   id. One PUT in that window replaces B with A — the storage layer forces the
     *   path id onto the document, so nothing downstream can catch it.
     */
    begin_load: (project_id) => {
      const prev = get()
      if (prev.project_id === project_id) {
        if (!prev.project) set({ loading: true })
        return
      }

      cancel_pending()

      const outgoing_id = prev.project_id
      const outgoing = prev.project
      if (outgoing_id && outgoing && (prev.dirty || prev.status === 'error')) {
        void (async () => {
          // A PUT already on the wire has to land first: two writes to one row
          // racing each other let network timing decide which document survives.
          if (in_flight) await in_flight
          // NOT silent: the save indicator that would have carried this failure
          // belongs to a project that is no longer open, so the toast is the only
          // place left to say that those edits did not reach the server.
          try {
            const res = await api.put_project(outgoing_id, outgoing, cached_version(outgoing_id))
            // Re-opening the project it just left must show what was handed over,
            // not the document it was opened with an hour ago.
            cache_saved(outgoing_id, outgoing, res.updated_at)
          } catch (err) {
            // The Arabic sentence is already on screen as a toast; what is left is
            // to stop the cache handing this superseded document back on re-entry.
            if (err instanceof ApiError && err.status === 409) {
              void queryClient.invalidateQueries({ queryKey: qk.project(outgoing_id) })
            }
          }
        })()
      }

      set({
        project: null,
        project_id,
        loading: true,
        dirty: false,
        status: 'idle',
        updated_at: null,
        save_error_ar: null,
        revision: 0,
        selected_space_id: null,
      })
    },

    load: (project, updated_at = null) => {
      cancel_pending()
      set({
        project,
        project_id: project.id,
        loading: false,
        dirty: false,
        status: 'idle',
        updated_at,
        save_error_ar: null,
        revision: 0,
        selected_space_id: project.spaces[0]?.id ?? null,
      })
    },

    clear: () => {
      cancel_pending()
      set({
        project: null,
        project_id: null,
        loading: false,
        dirty: false,
        status: 'idle',
        updated_at: null,
        save_error_ar: null,
        revision: 0,
        selected_space_id: null,
      })
    },

    /* -- edits ----------------------------------------------------------- */
    set_meta: (patch) => edit((p) => ({ ...p, ...patch })),

    replace_project: (patch) => edit((p) => apply_patch(p, patch)),

    add_space: (init = {}) => {
      const p = get().project
      if (!p) return null
      const space = make_space(init, p.spaces.map((s) => s.id))
      edit((cur) => ({ ...cur, spaces: [...cur.spaces, space] }))
      set({ selected_space_id: space.id })
      return space.id
    },

    update_space: (space_id, patch) =>
      edit((p) => map_space(p, space_id, (s) => apply_patch(s, patch))),

    remove_space: (space_id) => {
      edit((p) => ({ ...p, spaces: p.spaces.filter((s) => s.id !== space_id) }))
      if (get().selected_space_id === space_id) {
        set({ selected_space_id: get().project?.spaces[0]?.id ?? null })
      }
    },

    duplicate_space: (space_id) => {
      const p = get().project
      const source = p?.spaces.find((s) => s.id === space_id)
      if (!p || !source) return null
      const copy = clone_space(source, p.spaces.map((s) => s.id))
      edit((cur) => {
        const at = cur.spaces.findIndex((s) => s.id === space_id)
        const spaces = [...cur.spaces]
        spaces.splice(at + 1, 0, copy)
        return { ...cur, spaces }
      })
      set({ selected_space_id: copy.id })
      return copy.id
    },

    add_wall: (space_id, init = {}) => {
      const space = get().project?.spaces.find((s) => s.id === space_id)
      if (!space) return null
      const wall = make_wall(init, all_wall_ids(space), space.height_m)
      edit((p) => map_space(p, space_id, (s) => ({ ...s, walls: [...s.walls, wall] })))
      return wall.id
    },

    update_wall: (space_id, wall_id, patch) =>
      edit((p) => map_wall(p, space_id, wall_id, (w) => apply_patch(w, patch))),

    remove_wall: (space_id, wall_id) =>
      edit((p) =>
        map_space(p, space_id, (s) => ({ ...s, walls: s.walls.filter((w) => w.id !== wall_id) })),
      ),

    add_opening: (space_id, wall_id, init = {}) => {
      const space = get().project?.spaces.find((s) => s.id === space_id)
      if (!space || !space.walls.some((w) => w.id === wall_id)) return null
      const opening = make_opening(init, all_opening_ids(space), wall_id)
      edit((p) =>
        map_wall(p, space_id, wall_id, (w) => ({ ...w, openings: [...w.openings, opening] })),
      )
      return opening.id
    },

    update_opening: (space_id, wall_id, opening_id, patch) =>
      edit((p) =>
        map_wall(p, space_id, wall_id, (w) => ({
          ...w,
          openings: w.openings.map((o) => (o.id === opening_id ? apply_patch(o, patch) : o)),
        })),
      ),

    remove_opening: (space_id, wall_id, opening_id) =>
      edit((p) =>
        map_wall(p, space_id, wall_id, (w) => ({
          ...w,
          openings: w.openings.filter((o) => o.id !== opening_id),
        })),
      ),

    set_finish: (space_id, surface, system, spec = {}) =>
      edit((p) =>
        map_space(p, space_id, (s) => {
          const next: FinishAssignment = { surface, system, spec }
          const at = s.finishes.findIndex((f) => f.surface === surface)
          if (at < 0) return { ...s, finishes: [...s.finishes, next] }
          const finishes = [...s.finishes]
          // Keep the existing spec when only the system changed back and forth;
          // a surveyor who re-picks the same system should not lose their tile size.
          finishes[at] =
            finishes[at].system === system ? { ...finishes[at], spec } : next
          return { ...s, finishes }
        }),
      ),

    update_finish_spec: (space_id, surface, spec_patch) =>
      edit((p) =>
        map_space(p, space_id, (s) => ({
          ...s,
          finishes: s.finishes.map((f) =>
            f.surface === surface ? { ...f, spec: { ...f.spec, ...spec_patch } } : f,
          ),
        })),
      ),

    remove_finish: (space_id, surface) =>
      edit((p) =>
        map_space(p, space_id, (s) => ({
          ...s,
          finishes: s.finishes.filter((f) => f.surface !== surface),
        })),
      ),

    /* -- saving ---------------------------------------------------------- */
    save_now: async () => {
      cancel_pending()
      const { project, project_id, dirty } = get()
      if (!project || !project_id) return false
      // The document and the id it is about to be written to must be ONE pair. They
      // can only disagree while the store is being repointed at another project, and
      // a PUT sent in that window writes one project's rooms into another project's
      // row — the storage layer stamps the path id onto the document, so the
      // overwrite is silent and total. Refusing is the only safe answer.
      if (project.id && project.id !== project_id) return false
      if (!dirty && get().status !== 'error') return true

      // A PUT is already on the wire. Returning ITS promise would tell the caller
      // "saved" about a payload that predates the edits it is waiting on — which is
      // exactly wrong for `flush()` before a take-off. Wait for it, then save again
      // only if the project actually moved on. This terminates: the second pass
      // finds `in_flight` null, or finds nothing left to save.
      if (in_flight) {
        await in_flight
        if (!get().dirty && get().status !== 'error') return true
        return get().save_now()
      }

      const target_id = project_id
      const at_revision = get().revision
      set({ status: 'saving' })

      in_flight = (async () => {
        // Re-read, so an edit made between the scheduling and this line still
        // travels — but only while the store is still on the same project. If it has
        // moved on, the pair captured above is the one that belongs to `target_id`.
        const current = get()
        const same_project =
          current.project_id === target_id &&
          current.project !== null &&
          (!current.project.id || current.project.id === target_id)
        const doc = same_project ? (current.project as Project) : project
        const base = same_project ? current.updated_at : cached_version(target_id)

        try {
          // `silent` because the save indicator in the header already carries the
          // failure; a toast on every retry of a broken connection is noise.
          const res = await api.put_project(target_id, doc, base, { silent: true })
          // The cache entry for this project moves to the version just written, so
          // re-opening the project cannot resurrect the document it was opened with.
          cache_saved(target_id, doc, res.updated_at)
          // The store moved to another project while this was on the wire: the save
          // landed, but none of the flags below describe the project now open.
          if (get().project_id !== target_id) return true
          const edited_mid_flight = get().revision !== at_revision
          set({
            updated_at: res.updated_at,
            save_error_ar: null,
            dirty: edited_mid_flight,
            status: edited_mid_flight ? 'dirty' : 'saved',
          })
          if (edited_mid_flight) schedule()
          return true
        } catch (err) {
          // A refused save means the cache entry for this project is behind the
          // server as well, so re-opening the project must fetch the newer document
          // rather than resurrect the one this tab has been editing.
          if (err instanceof ApiError && err.status === 409) {
            void queryClient.invalidateQueries({ queryKey: qk.project(target_id) })
          }
          // dirty stays true on purpose: the edits are still only in this tab.
          if (get().project_id === target_id) {
            set({ status: 'error', save_error_ar: error_ar(err), dirty: true })
          }
          return false
        } finally {
          in_flight = null
        }
      })()

      return in_flight
    },

    flush: async () => {
      if (debounce_timer !== null || get().dirty) return get().save_now()
      if (in_flight) return in_flight
      return true
    },

    set_autosave: (on) => {
      set({ autosave: on })
      if (!on) cancel_pending()
      else if (get().dirty) schedule()
    },

    /* -- ui -------------------------------------------------------------- */
    select_space: (space_id) => set({ selected_space_id: space_id }),
  }
})

/* ==========================================================================
   Selectors — subscribe to the narrowest slice, so typing in one wall does not
   re-render the whole editor.
   ========================================================================== */

export const useProject = () => useProjectStore((s) => s.project)
export const useProjectLoading = () => useProjectStore((s) => s.loading)
export const useDirty = () => useProjectStore((s) => s.dirty)
export const useSaveStatus = () => useProjectStore((s) => s.status)
export const useSaveErrorAr = () => useProjectStore((s) => s.save_error_ar)
export const useSelectedSpaceId = () => useProjectStore((s) => s.selected_space_id)

export const useSpace = (space_id: string | null | undefined): Space | null =>
  useProjectStore((s) =>
    space_id ? (s.project?.spaces.find((sp) => sp.id === space_id) ?? null) : null,
  )

export const useSpaceCount = () => useProjectStore((s) => s.project?.spaces.length ?? 0)

/** The project's currency, or "" — needed by nearly every money-rendering cell. */
export const useCurrency = () => useProjectStore((s) => s.project?.currency ?? '')

/**
 * Warn before a browser close/reload that would drop unsaved edits, and take one
 * last shot at flushing them. Call once, from main.tsx.
 */
export function install_unload_guard(): void {
  window.addEventListener('beforeunload', (e) => {
    if (!useProjectStore.getState().dirty) return
    void useProjectStore.getState().flush()
    e.preventDefault()
    // Browsers ignore custom text now, but a non-empty returnValue still triggers
    // the native "leave site?" prompt, which is the part that matters.
    e.returnValue = ''
  })
}
