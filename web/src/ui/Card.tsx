import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '../lib/cn'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Removes the inner padding, for a card whose body is a full-bleed table. */
  flush?: boolean
}

export function Card({ flush, className, children, ...rest }: CardProps) {
  return (
    <div
      className={cn(
        'rounded-xl border border-border bg-surface shadow-sm',
        'overflow-hidden',
        className,
      )}
      {...rest}
    >
      {flush ? children : <div className="p-4 sm:p-5">{children}</div>}
    </div>
  )
}

export function CardHeader({
  title,
  subtitle,
  actions,
  className,
}: {
  title: ReactNode
  subtitle?: ReactNode
  /** Buttons; they sit at the logical end of the row (the LEFT under RTL). */
  actions?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex flex-wrap items-start justify-between gap-3 border-b border-border',
        'bg-surface-2/50 px-4 py-3 sm:px-5',
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="truncate text-[15px] font-semibold text-fg">{title}</h2>
        {subtitle && <p className="mt-0.5 text-[13px] leading-snug text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export function CardBody({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn('p-4 sm:p-5', className)} {...rest}>
      {children}
    </div>
  )
}

export function CardFooter({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-2 border-t border-border bg-surface-2/50 px-4 py-3 sm:px-5',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  )
}

/** A labelled figure — the summary tiles above a BOQ or a quote. */
export function Stat({
  label,
  value,
  hint,
  tone = 'default',
  className,
}: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: 'default' | 'primary' | 'success' | 'warn' | 'danger'
  className?: string
}) {
  const tones = {
    default: 'text-fg',
    primary: 'text-primary',
    success: 'text-success',
    warn: 'text-warn',
    danger: 'text-danger',
  } as const
  return (
    <div className={cn('rounded-lg border border-border bg-surface px-4 py-3', className)}>
      <div className="text-[12.5px] font-medium text-muted">{label}</div>
      <div className={cn('mt-1 text-xl font-semibold tabular-nums', tones[tone])}>{value}</div>
      {hint && <div className="mt-0.5 text-[12px] text-subtle">{hint}</div>}
    </div>
  )
}
