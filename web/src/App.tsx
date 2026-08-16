import { useEffect, useRef, type ReactNode } from 'react'
import {
  Link,
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useParams,
} from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronLeft, CloudOff, Loader2, Ruler } from 'lucide-react'

import { api, error_ar, qk } from './api/client'
import { useProjectStore, useSaveStatus, useSaveErrorAr } from './store'
import { cn } from './lib/cn'
import { Badge, Button, EmptyState, ErrorState, LoadingLine } from './ui'

import ProjectsPage from './pages/ProjectsPage'
import EditorPage from './pages/EditorPage'
import BoqPage from './pages/Boq'
import QuotePage from './pages/QuotePage'

/* ==========================================================================
   Chrome
   ========================================================================== */

function AppHeader({ children }: { children?: ReactNode }) {
  return (
    <header className="sticky top-0 z-30 border-b border-border bg-surface/85 backdrop-blur">
      <div className="mx-auto flex h-14 w-full max-w-7xl items-center gap-3 px-4 sm:px-6">
        <Link
          to="/"
          className="flex shrink-0 items-center gap-2 text-[15px] font-bold text-fg hover:text-primary"
        >
          <Ruler className="size-5 text-primary" aria-hidden />
          <span className="hidden sm:inline">حصر وتسعير التشطيبات</span>
        </Link>
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </header>
  )
}

/**
 * The autosave state, always on screen.
 *
 * It never disappears into a silent "saved" nothing: an editor that shows no
 * status is indistinguishable from an editor whose saves have been failing for
 * ten minutes, and the second one costs an afternoon of measurements.
 */
export function SaveIndicator() {
  const status = useSaveStatus()
  const error_ar = useSaveErrorAr()
  const save_now = useProjectStore((s) => s.save_now)

  if (status === 'error') {
    return (
      <button
        type="button"
        onClick={() => void save_now()}
        title={error_ar ?? undefined}
        className="inline-flex items-center gap-1.5 rounded-full border border-danger/30 bg-danger-soft px-2.5 py-1 text-[12.5px] font-medium text-danger"
      >
        <CloudOff className="size-3.5" aria-hidden />
        لم يُحفظ — إعادة المحاولة
      </button>
    )
  }
  if (status === 'saving') {
    return (
      <Badge tone="info" size="sm" icon={<Loader2 className="animate-spin" />}>
        جارٍ الحفظ…
      </Badge>
    )
  }
  if (status === 'dirty') {
    return (
      <Badge tone="warn" size="sm" dot>
        تغييرات غير محفوظة
      </Badge>
    )
  }
  if (status === 'saved') {
    return (
      <Badge tone="success" size="sm" icon={<Check />}>
        محفوظ
      </Badge>
    )
  }
  return null
}

/** A <Link> that reads as a secondary button without nesting <a> inside <button>. */
const BACK_LINK_CLS =
  'inline-flex h-9.5 items-center gap-2 rounded-lg border border-border bg-surface px-4 ' +
  'text-sm font-medium text-fg shadow-xs transition-colors hover:bg-surface-2'

const PROJECT_TABS = [
  { to: '.', end: true, label: 'الفراغات' },
  { to: 'boq', end: false, label: 'جدول الكميات' },
  { to: 'quote', end: false, label: 'عرض السعر' },
]

/* ==========================================================================
   Project shell — owns fetching the project into the store
   ========================================================================== */

function ProjectLayout() {
  const { id = '' } = useParams()
  const begin_load = useProjectStore((s) => s.begin_load)
  const load = useProjectStore((s) => s.load)
  const clear = useProjectStore((s) => s.clear)
  const project = useProjectStore((s) => s.project)
  const loaded_id = useRef<string | null>(null)

  const query = useQuery({
    queryKey: qk.project(id),
    queryFn: () => api.get_project(id, { silent: true }),
    enabled: Boolean(id),
    // The store — not the cache — owns the editing copy while a project is open: a
    // background refetch that pushed a server version back into the store would
    // discard whatever the surveyor typed in the last few seconds.
    //
    // That is only safe because the cache no longer goes stale behind the store:
    // every successful save writes the saved document and its new `updated_at` back
    // into THIS key (store.ts `cache_saved`). Re-entering a project therefore
    // re-loads the version that was last saved, not the version it was opened with.
    // A version written by another tab is caught by the `updated_at` precondition on
    // PUT, which comes back 409 instead of overwriting.
    staleTime: Infinity,
    refetchOnMount: false,
    retry: 1,
  })

  useEffect(() => {
    if (id) begin_load(id)
  }, [id, begin_load])

  useEffect(() => {
    if (!query.data) return
    const st = useProjectStore.getState()
    // Adopt the fetched document when this project is not the one the store is
    // holding. The ref alone is not enough: the store is also emptied by leaving the
    // project and by `begin_load` handing the previous one over, and a store with no
    // document renders as «تعذّر فتح المشروع» however well the fetch went.
    if (loaded_id.current === id && st.project_id === id && st.project) return
    loaded_id.current = id
    load(query.data.project, query.data.updated_at)
  }, [query.data, id, load])

  // Leaving the project flushes any pending edit before the store is emptied.
  useEffect(
    () => () => {
      void useProjectStore.getState().flush()
      clear()
    },
    [clear],
  )

  if (query.isPending) {
    return (
      <>
        <AppHeader />
        <LoadingLine label="جارٍ فتح المشروع…" />
      </>
    )
  }

  if (query.isError || !project) {
    return (
      <>
        <AppHeader />
        <ErrorState
          message_ar={
            query.error
              ? error_ar(query.error)
              : 'تعذّر فتح المشروع. قد يكون محذوفاً أو أن الخادم لا يستجيب.'
          }
          action={
            <>
              <Button variant="primary" onClick={() => void query.refetch()}>
                إعادة المحاولة
              </Button>
              <Link to="/" className={BACK_LINK_CLS}>
                العودة إلى المشاريع
              </Link>
            </>
          }
        />
      </>
    )
  }

  return (
    <>
      <AppHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <ChevronLeft className="size-4 shrink-0 text-subtle rtl:rotate-180" aria-hidden />
            <span className="truncate text-sm font-semibold">
              {project.name || 'مشروع بلا اسم'}
            </span>
            {project.client_name && (
              <span className="hidden truncate text-[13px] text-muted sm:inline">
                · {project.client_name}
              </span>
            )}
          </div>
          <SaveIndicator />
        </div>
      </AppHeader>

      <nav className="border-b border-border bg-surface">
        <div className="mx-auto flex w-full max-w-7xl gap-1 px-4 sm:px-6">
          {PROJECT_TABS.map((t) => (
            <NavLink
              key={t.to}
              to={t.to}
              end={t.end}
              className={({ isActive }) =>
                cn(
                  '-mb-px border-b-2 px-3.5 py-2.5 text-sm font-medium transition-colors',
                  isActive
                    ? 'border-primary text-primary'
                    : 'border-transparent text-muted hover:border-border-strong hover:text-fg',
                )
              }
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      </nav>

      <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6">
        <Outlet />
      </main>
    </>
  )
}

function NotFound() {
  return (
    <>
      <AppHeader />
      <EmptyState
        title="الصفحة غير موجودة"
        description="الرابط الذي فتحته لا يقابل أي صفحة في التطبيق."
        action={
          <Link to="/" className={BACK_LINK_CLS}>
            العودة إلى المشاريع
          </Link>
        }
      />
    </>
  )
}

/* ==========================================================================
   Routes
   ========================================================================== */

export default function App() {
  return (
    <div className="min-h-dvh bg-bg text-fg">
      <Routes>
        <Route
          path="/"
          element={
            <>
              <AppHeader />
              <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6">
                <ProjectsPage />
              </main>
            </>
          }
        />
        <Route path="/p/:id" element={<ProjectLayout />}>
          <Route index element={<EditorPage />} />
          <Route path="boq" element={<BoqPage />} />
          <Route path="quote" element={<QuotePage />} />
          <Route path="*" element={<Navigate to="." replace />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Routes>
    </div>
  )
}
