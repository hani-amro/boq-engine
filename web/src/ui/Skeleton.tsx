import { cn } from '../lib/cn'

/**
 * A loading placeholder.
 *
 * `aria-hidden` and `role="presentation"` are deliberate: the container that owns
 * the skeleton carries `aria-busy`, so a screen reader hears "جارٍ التحميل" once
 * instead of a dozen empty boxes.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      role="presentation"
      aria-hidden
      className={cn('animate-pulse rounded-md bg-surface-3', className)}
    />
  )
}

/** Text lines with a short last line, so the block reads as a paragraph. */
export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn('space-y-2', className)} aria-hidden>
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton
          key={i}
          className={cn('h-3.5', i === lines - 1 ? 'w-2/5' : i % 2 ? 'w-4/5' : 'w-full')}
        />
      ))}
    </div>
  )
}

export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div
      aria-busy="true"
      aria-live="polite"
      className={cn('rounded-xl border border-border bg-surface p-5', className)}
    >
      <Skeleton className="h-4 w-40" />
      <div className="mt-4">
        <SkeletonText lines={3} />
      </div>
    </div>
  )
}

/** Placeholder rows that keep a table's column widths while it loads. */
export function SkeletonRows({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <tbody aria-busy="true" className="divide-y divide-border">
      {Array.from({ length: rows }, (_, r) => (
        <tr key={r}>
          {Array.from({ length: cols }, (_, c) => (
            <td key={c} className="px-3 py-3">
              <Skeleton className={cn('h-3.5', c === 0 ? 'w-40' : 'w-16 ms-auto')} />
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  )
}

/** Centred inline spinner text, for a panel that is refetching in place. */
export function LoadingLine({ label = 'جارٍ التحميل…' }: { label?: string }) {
  return (
    <div
      className="flex items-center justify-center gap-2 py-10 text-[13.5px] text-muted"
      aria-live="polite"
    >
      <span className="size-4 animate-spin rounded-full border-2 border-border border-t-primary" />
      {label}
    </div>
  )
}
