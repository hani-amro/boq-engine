import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '../lib/cn'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'subtle' | 'link'
export type ButtonSize = 'sm' | 'md' | 'lg' | 'icon'

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-primary text-primary-fg shadow-xs hover:bg-primary-hover active:translate-y-px disabled:hover:bg-primary',
  secondary:
    'bg-surface text-fg border border-border shadow-xs hover:bg-surface-2 active:translate-y-px disabled:hover:bg-surface',
  subtle: 'bg-surface-2 text-fg hover:bg-surface-3 active:translate-y-px disabled:hover:bg-surface-2',
  ghost: 'text-muted hover:bg-surface-2 hover:text-fg active:translate-y-px',
  danger:
    'bg-danger text-danger-fg shadow-xs hover:brightness-110 active:translate-y-px disabled:hover:brightness-100',
  link: 'text-primary underline-offset-4 hover:underline p-0 h-auto',
}

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-[13px] gap-1.5 rounded-md',
  md: 'h-9.5 px-4 text-sm gap-2 rounded-lg',
  lg: 'h-11 px-5 text-[15px] gap-2 rounded-lg',
  icon: 'size-9 p-0 rounded-lg',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Swaps the leading icon for a spinner and disables the button. */
  loading?: boolean
  /** Rendered before the label in reading order (i.e. to its RIGHT under RTL). */
  icon?: ReactNode
  /** Rendered after the label. */
  iconEnd?: ReactNode
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  icon,
  iconEnd,
  className,
  children,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex select-none items-center justify-center whitespace-nowrap font-medium',
        'transition-[background-color,color,box-shadow,transform] duration-100',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
        'disabled:pointer-events-none disabled:opacity-50',
        '[&_svg]:size-4 [&_svg]:shrink-0',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Loader2 className="animate-spin" aria-hidden /> : icon}
      {children}
      {!loading && iconEnd}
    </button>
  )
}

/** A row of buttons with consistent spacing; `justify` follows reading order. */
export function ButtonRow({
  className,
  children,
}: {
  className?: string
  children: ReactNode
}) {
  return <div className={cn('flex flex-wrap items-center gap-2', className)}>{children}</div>
}
