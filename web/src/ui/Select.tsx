import type { ReactNode, SelectHTMLAttributes } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '../lib/cn'
import { CONTROL_BASE, CONTROL_SIZES, type ControlSize } from './Input'

export interface SelectOption {
  value: string
  label: string
  disabled?: boolean
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
  size?: ControlSize
  invalid?: boolean
  /** Convenience list; ignored when `children` is supplied (use it for <optgroup>). */
  options?: SelectOption[]
  /** Shown as a disabled first entry when the value is "" — never a silent default. */
  placeholder?: string
  children?: ReactNode
}

/**
 * A native <select>, deliberately.
 *
 * A custom listbox would have to re-implement keyboard search, mobile pickers and
 * RTL popup placement. The native control gets all three right in Arabic, so only
 * its chrome is restyled: the browser arrow is removed and redrawn at the logical
 * `end` so it lands on the LEFT under RTL, where the native one already was.
 */
export function Select({
  size = 'md',
  invalid,
  options,
  placeholder,
  className,
  children,
  value,
  ...rest
}: SelectProps) {
  return (
    <div className="relative">
      <select
        value={value}
        aria-invalid={invalid || undefined}
        className={cn(
          CONTROL_BASE,
          CONTROL_SIZES[size],
          'appearance-none pe-9 bg-none',
          value === '' && placeholder ? 'text-subtle' : '',
          className,
        )}
        {...rest}
      >
        {placeholder !== undefined && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {children ??
          options?.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute inset-y-0 end-0 my-auto me-3 size-4 text-subtle"
      />
    </div>
  )
}
