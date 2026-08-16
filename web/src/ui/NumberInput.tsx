import { useEffect, useRef, useState, type InputHTMLAttributes } from 'react'
import { cn } from '../lib/cn'
import { fmt_num, parse_num, unit_ar } from '../lib/format'
import { CONTROL_BASE, CONTROL_SIZES, type ControlSize } from './Input'

/**
 * The measured-dimension control.
 *
 * Four behaviours here exist because this is where a wrong number enters the
 * product:
 *
 * * **An empty field is `null`, never 0.** "لم يُقس بعد" and "قياسه صفر" are
 *   different facts, and collapsing them is exactly how a silent under-quote
 *   starts. `onValueChange` receives `null` for an empty field.
 * * **Typing is not parsed away.** While the field has focus the raw text is kept,
 *   so "3." and "0.0" survive long enough to become "3.05". The value is
 *   normalised on blur, not on every keystroke.
 * * **Arabic input is accepted.** ٣٫٥ and 3,5 both parse — the surveyor's keyboard
 *   layout is not the surveyor's problem. See `parse_num`.
 * * **The unit is inside the control.** A dimension without its unit beside it is
 *   the ambiguity that turns a running metre into a square metre.
 */
export interface NumberInputProps
  extends Omit<
    InputHTMLAttributes<HTMLInputElement>,
    'size' | 'value' | 'onChange' | 'type' | 'defaultValue'
  > {
  value: number | null | undefined
  onValueChange: (value: number | null) => void
  /** Unit code ("m", "m2", "no") or a ready Arabic label ("م", "م²"). */
  unit?: string
  size?: ControlSize
  invalid?: boolean
  /** Decimals shown once the field loses focus. Dimensions want 2, counts 0. */
  digits?: number
  min?: number
  max?: number
  /** Clamp to [min, max] on blur instead of only flagging it. */
  clamp?: boolean
}

function to_text(v: number | null | undefined, digits: number): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return ''
  // fmt_num groups thousands, which is wrong inside an editable field: the group
  // separator is not re-typeable and confuses the decimal comma habit.
  return fmt_num(v, digits).replace(/,/g, '')
}

export function NumberInput({
  value,
  onValueChange,
  unit,
  size = 'md',
  invalid,
  digits = 2,
  min,
  max,
  clamp = false,
  className,
  disabled,
  ...rest
}: NumberInputProps) {
  const [text, setText] = useState(() => to_text(value, digits))
  const focused = useRef(false)

  // Re-sync only while unfocused: overwriting the box under a typing user is the
  // classic controlled-input bug, and here it would overwrite a measurement.
  useEffect(() => {
    if (!focused.current) setText(to_text(value, digits))
  }, [value, digits])

  const commit = (raw: string) => {
    const parsed = parse_num(raw)
    if (parsed === null) {
      onValueChange(null)
      setText('')
      return
    }
    let n = parsed
    if (clamp) {
      if (min !== undefined) n = Math.max(min, n)
      if (max !== undefined) n = Math.min(max, n)
    }
    onValueChange(n)
    setText(to_text(n, digits))
  }

  const out_of_range =
    value !== null &&
    value !== undefined &&
    ((min !== undefined && value < min) || (max !== undefined && value > max))

  const suffix = unit ? unit_ar(unit) : null

  return (
    <div className={cn('relative', className)}>
      <input
        {...rest}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        // dir=ltr on the INPUT only: digits, the decimal point and a leading minus
        // then order like a calculator, and the caret moves the way a numeric
        // keypad expects. Logical padding/alignment below resolve against THIS
        // element's ltr direction, while the suffix outside resolves against the
        // page's rtl — which is how the number ends up against the right edge with
        // its unit to the left of it, the way an Arabic reader expects.
        dir="ltr"
        value={text}
        disabled={disabled}
        aria-invalid={invalid || out_of_range || undefined}
        onFocus={(e) => {
          focused.current = true
          rest.onFocus?.(e)
        }}
        onChange={(e) => setText(e.target.value)}
        onBlur={(e) => {
          focused.current = false
          commit(e.target.value)
          rest.onBlur?.(e)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit((e.target as HTMLInputElement).value)
          rest.onKeyDown?.(e)
        }}
        className={cn(
          CONTROL_BASE,
          CONTROL_SIZES[size],
          'text-end tabular-nums',
          suffix && (size === 'sm' ? 'ps-9' : 'ps-11'),
        )}
      />
      {suffix && (
        <span
          // aria-hidden: the unit is announced through the field's label, and a
          // screen reader reading "م ٢" mid-number is worse than silence.
          aria-hidden
          className={cn(
            'pointer-events-none absolute inset-y-0 end-0 flex items-center px-3',
            'text-[12px] font-medium text-subtle',
          )}
        >
          {suffix}
        </span>
      )}
    </div>
  )
}
