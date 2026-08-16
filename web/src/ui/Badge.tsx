import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '../lib/cn'

export type BadgeTone =
  | 'neutral'
  | 'primary'
  | 'accent'
  | 'success'
  | 'warn'
  | 'danger'
  | 'info'

const TONES: Record<BadgeTone, string> = {
  neutral: 'bg-surface-2 text-muted border-border',
  primary: 'bg-primary-soft text-primary border-primary/20',
  accent: 'bg-accent-soft text-accent-fg border-accent/25',
  success: 'bg-success-soft text-success border-success/25',
  warn: 'bg-warn-soft text-warn border-warn/30',
  danger: 'bg-danger-soft text-danger border-danger/25',
  info: 'bg-info-soft text-info border-info/25',
}

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone
  size?: 'sm' | 'md'
  icon?: ReactNode
  /** A small filled circle before the label, for status without an icon. */
  dot?: boolean
}

export function Badge({
  tone = 'neutral',
  size = 'md',
  icon,
  dot,
  className,
  children,
  ...rest
}: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border font-medium whitespace-nowrap',
        size === 'sm' ? 'px-2 py-0.5 text-[11.5px]' : 'px-2.5 py-1 text-[12.5px]',
        '[&_svg]:size-3.5 [&_svg]:shrink-0',
        TONES[tone],
        className,
      )}
      {...rest}
    >
      {dot && <span className="size-1.5 rounded-full bg-current" aria-hidden />}
      {icon}
      {children}
    </span>
  )
}

/**
 * A machine identifier inside Arabic prose — rule_key, item code, space id.
 * Forced LTR and isolated: "TIL-CER-FLR" reordered by the bidi algorithm is a
 * code nobody can look up.
 */
export function CodeChip({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        'ltr inline-block rounded border border-border bg-surface-2 px-1.5 py-px',
        'font-mono text-[11.5px] text-muted',
        className,
      )}
    >
      {children}
    </span>
  )
}
