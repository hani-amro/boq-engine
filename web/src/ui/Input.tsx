import {
  useId,
  type InputHTMLAttributes,
  type LabelHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from 'react'
import { cn } from '../lib/cn'

/** Shared shell so Input, NumberInput and Select are visually one control family. */
export const CONTROL_BASE =
  'w-full rounded-lg border border-border bg-surface text-fg placeholder:text-subtle ' +
  'shadow-xs transition-[border-color,box-shadow] outline-none ' +
  'focus:border-primary focus:ring-2 focus:ring-primary/25 ' +
  'disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-subtle ' +
  'aria-[invalid=true]:border-danger aria-[invalid=true]:ring-2 aria-[invalid=true]:ring-danger/20'

export const CONTROL_SIZES = {
  sm: 'h-8 px-2.5 text-[13px]',
  md: 'h-9.5 px-3 text-sm',
  lg: 'h-11 px-3.5 text-[15px]',
} as const

export type ControlSize = keyof typeof CONTROL_SIZES

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  size?: ControlSize
  invalid?: boolean
}

export function Input({ size = 'md', invalid, className, ...rest }: InputProps) {
  return (
    <input
      aria-invalid={invalid || undefined}
      className={cn(CONTROL_BASE, CONTROL_SIZES[size], className)}
      {...rest}
    />
  )
}

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
}

export function Textarea({ invalid, className, rows = 3, ...rest }: TextareaProps) {
  return (
    <textarea
      rows={rows}
      aria-invalid={invalid || undefined}
      className={cn(CONTROL_BASE, 'px-3 py-2 text-sm leading-relaxed', className)}
      {...rest}
    />
  )
}

/**
 * Label + control + hint/error, wired together by id.
 *
 * `hint` is help text; `error` REPLACES it when present and is announced. Both are
 * Arabic sentences — an error that reads "invalid" in English in an Arabic form is
 * an error the user cannot act on.
 */
export interface FieldProps {
  label?: ReactNode
  hint?: ReactNode
  error?: string | null
  required?: boolean
  htmlFor?: string
  className?: string
  children: ReactNode | ((id: string) => ReactNode)
}

export function Field({
  label,
  hint,
  error,
  required,
  htmlFor,
  className,
  children,
}: FieldProps) {
  const auto = useId()
  const id = htmlFor ?? auto
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      {label !== undefined && (
        <label htmlFor={id} className="text-[13px] font-medium text-muted">
          {label}
          {required && (
            <span className="ms-1 text-danger" aria-hidden>
              *
            </span>
          )}
        </label>
      )}
      {typeof children === 'function' ? children(id) : children}
      {error ? (
        <p className="text-[12.5px] leading-snug text-danger" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="text-[12.5px] leading-snug text-subtle">{hint}</p>
      ) : null}
    </div>
  )
}

/** Standalone label, for controls that build their own layout. */
export function Label({
  className,
  children,
  ...rest
}: LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className={cn('text-[13px] font-medium text-muted', className)} {...rest}>
      {children}
    </label>
  )
}
