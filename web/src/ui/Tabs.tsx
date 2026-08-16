import { useId, type ReactNode } from 'react'
import { cn } from '../lib/cn'

export interface TabItem {
  value: string
  label: ReactNode
  icon?: ReactNode
  /** A count or status pill after the label. */
  badge?: ReactNode
  disabled?: boolean
}

export interface TabsProps {
  items: TabItem[]
  value: string
  onChange: (value: string) => void
  className?: string
  /** `underline` for page-level sections, `pill` for a compact in-card switch. */
  variant?: 'underline' | 'pill'
  'aria-label'?: string
}

/**
 * Roving-focus tab list.
 *
 * The arrow keys are swapped under RTL on purpose: ArrowLeft moves to the NEXT tab
 * because the next tab is physically to the left. Getting this backwards is the
 * single most common keyboard bug in an Arabic UI, and it is invisible to anyone
 * testing with a mouse.
 */
export function Tabs({
  items,
  value,
  onChange,
  className,
  variant = 'underline',
  'aria-label': ariaLabel,
}: TabsProps) {
  const uid = useId()

  const move = (delta: number) => {
    const enabled = items.filter((i) => !i.disabled)
    if (enabled.length === 0) return
    const at = enabled.findIndex((i) => i.value === value)
    const next = enabled[(at + delta + enabled.length) % enabled.length]
    onChange(next.value)
  }

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') {
          e.preventDefault()
          move(+1)
        } else if (e.key === 'ArrowRight') {
          e.preventDefault()
          move(-1)
        } else if (e.key === 'Home') {
          e.preventDefault()
          onChange(items.find((i) => !i.disabled)?.value ?? value)
        } else if (e.key === 'End') {
          e.preventDefault()
          const last = [...items].reverse().find((i) => !i.disabled)
          if (last) onChange(last.value)
        }
      }}
      className={cn(
        'flex items-center gap-1 overflow-x-auto',
        variant === 'underline' && 'border-b border-border',
        variant === 'pill' && 'rounded-lg bg-surface-2 p-1',
        className,
      )}
    >
      {items.map((item) => {
        const active = item.value === value
        return (
          <button
            key={item.value}
            type="button"
            role="tab"
            id={`${uid}-tab-${item.value}`}
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            disabled={item.disabled}
            onClick={() => onChange(item.value)}
            className={cn(
              'inline-flex shrink-0 items-center gap-2 whitespace-nowrap text-sm font-medium',
              'transition-colors disabled:cursor-not-allowed disabled:opacity-45',
              '[&_svg]:size-4 [&_svg]:shrink-0',
              variant === 'underline' &&
                cn(
                  'relative -mb-px border-b-2 px-3.5 py-2.5',
                  active
                    ? 'border-primary text-primary'
                    : 'border-transparent text-muted hover:border-border-strong hover:text-fg',
                ),
              variant === 'pill' &&
                cn(
                  'rounded-md px-3 py-1.5',
                  active
                    ? 'bg-surface text-fg shadow-xs'
                    : 'text-muted hover:text-fg',
                ),
            )}
          >
            {item.icon}
            {item.label}
            {item.badge}
          </button>
        )
      })}
    </div>
  )
}

export function TabPanel({
  value,
  active,
  children,
  className,
}: {
  value: string
  active: string
  children: ReactNode
  className?: string
}) {
  if (value !== active) return null
  return (
    <div role="tabpanel" tabIndex={0} className={cn('outline-none', className)}>
      {children}
    </div>
  )
}
