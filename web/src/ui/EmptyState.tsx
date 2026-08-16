import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { cn } from '../lib/cn'

export interface EmptyStateProps {
  icon?: ReactNode
  title: ReactNode
  /** One Arabic sentence saying what to do next — never just "لا توجد بيانات". */
  description?: ReactNode
  action?: ReactNode
  className?: string
  tone?: 'neutral' | 'danger'
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  tone = 'neutral',
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-3 px-6 py-14 text-center',
        className,
      )}
    >
      {icon && (
        <div
          aria-hidden
          className={cn(
            'flex size-12 items-center justify-center rounded-full [&_svg]:size-6',
            tone === 'danger'
              ? 'bg-danger-soft text-danger'
              : 'bg-surface-2 text-subtle',
          )}
        >
          {icon}
        </div>
      )}
      <div className="max-w-md space-y-1.5">
        <h3 className="text-[15px] font-semibold text-fg">{title}</h3>
        {description && (
          <p className="text-[13.5px] leading-relaxed text-muted">{description}</p>
        )}
      </div>
      {action && <div className="mt-1 flex items-center gap-2">{action}</div>}
    </div>
  )
}

/**
 * A failure with a retry. The message is the server's `message_ar`, shown verbatim
 * — it was written to be read by the person who has to fix the input.
 */
export function ErrorState({
  message_ar,
  action,
  className,
}: {
  message_ar: string
  action?: ReactNode
  className?: string
}) {
  return (
    <EmptyState
      tone="danger"
      icon={<AlertTriangle />}
      title="تعذّر إتمام العملية"
      description={message_ar}
      action={action}
      className={className}
    />
  )
}

/**
 * The engine's Arabic warnings.
 *
 * They are listed, never collapsed into a count and never hidden behind a
 * disclosure that defaults to closed: a warning the surveyor did not read is a
 * warning that did not happen, and these are the lines that say a quantity was
 * skipped or an assumption was substituted.
 */
export function WarningList({
  warnings,
  title = 'ملاحظات الحصر',
  className,
}: {
  warnings: string[]
  title?: ReactNode
  className?: string
}) {
  if (!warnings.length) return null
  return (
    <div
      className={cn(
        'rounded-lg border border-warn/30 bg-warn-soft/60 px-4 py-3',
        className,
      )}
    >
      <div className="mb-1.5 flex items-center gap-2 text-[13px] font-semibold text-warn">
        <AlertTriangle className="size-4 shrink-0" aria-hidden />
        {title}
        <span className="tabular-nums">({warnings.length})</span>
      </div>
      <ul className="space-y-1 ps-5 text-[13px] leading-relaxed text-fg/90 [&>li]:list-disc">
        {warnings.map((w, i) => (
          <li key={`${i}-${w.slice(0, 24)}`}>{w}</li>
        ))}
      </ul>
    </div>
  )
}
