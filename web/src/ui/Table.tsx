import type {
  HTMLAttributes,
  ReactNode,
  TdHTMLAttributes,
  ThHTMLAttributes,
} from 'react'
import { cn } from '../lib/cn'

/**
 * A bill of quantities is a wide table read on a laptop. Two rules:
 *
 * * The table scrolls inside its own box; the PAGE never scrolls sideways. A BOQ
 *   that pushes the whole layout off-screen is unusable on the site office's 13".
 * * Quantity and money columns align to the logical `end` with tabular figures, so
 *   the decimal points form a straight column and a hand-added total matches.
 */
export function TableWrap({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('w-full overflow-x-auto overscroll-x-contain', className)}
      {...rest}
    >
      {children}
    </div>
  )
}

export function Table({ className, children, ...rest }: HTMLAttributes<HTMLTableElement>) {
  return (
    <table
      className={cn('w-full border-collapse text-start text-[13.5px]', className)}
      {...rest}
    >
      {children}
    </table>
  )
}

export function THead({ className, children, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead
      className={cn(
        'bg-surface-2 text-[12.5px] font-semibold text-muted',
        '[&_th]:border-b [&_th]:border-border',
        className,
      )}
      {...rest}
    >
      {children}
    </thead>
  )
}

export function TBody({ className, children, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <tbody className={cn('divide-y divide-border', className)} {...rest}>
      {children}
    </tbody>
  )
}

export function TFoot({ className, children, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <tfoot
      className={cn(
        'border-t-2 border-border-strong bg-surface-2 font-semibold',
        className,
      )}
      {...rest}
    >
      {children}
    </tfoot>
  )
}

export interface TrProps extends HTMLAttributes<HTMLTableRowElement> {
  /** Highlights a row the engine flagged rather than measured. */
  tone?: 'default' | 'warn' | 'danger' | 'muted'
  interactive?: boolean
}

export function TR({ tone = 'default', interactive, className, children, ...rest }: TrProps) {
  const tones = {
    default: '',
    warn: 'bg-warn-soft/60',
    danger: 'bg-danger-soft/60',
    muted: 'text-muted',
  } as const
  return (
    <tr
      className={cn(
        tones[tone],
        interactive && 'cursor-pointer transition-colors hover:bg-surface-2',
        className,
      )}
      {...rest}
    >
      {children}
    </tr>
  )
}

type Align = 'start' | 'end' | 'center'

const ALIGN: Record<Align, string> = {
  start: 'text-start',
  end: 'text-end',
  center: 'text-center',
}

// `align` is overridden with logical values: the deprecated HTML attribute only
// knows left/right, which are exactly the two words this UI must never use.
export interface ThProps extends Omit<ThHTMLAttributes<HTMLTableCellElement>, 'align'> {
  align?: Align
  /** Right-hand alignment plus tabular figures; use for every quantity/money head. */
  numeric?: boolean
}

export function TH({ align, numeric, className, children, ...rest }: ThProps) {
  return (
    <th
      scope="col"
      className={cn(
        'px-3 py-2.5 font-semibold whitespace-nowrap',
        ALIGN[align ?? (numeric ? 'end' : 'start')],
        className,
      )}
      {...rest}
    >
      {children}
    </th>
  )
}

export interface TdProps extends Omit<TdHTMLAttributes<HTMLTableCellElement>, 'align'> {
  align?: Align
  numeric?: boolean
  /** Keeps a long Arabic description on one line with an ellipsis. */
  truncate?: boolean
}

export function TD({ align, numeric, truncate, className, children, ...rest }: TdProps) {
  return (
    <td
      className={cn(
        'px-3 py-2.5 align-middle',
        ALIGN[align ?? (numeric ? 'end' : 'start')],
        numeric && 'tabular-nums whitespace-nowrap',
        truncate && 'max-w-[22rem] truncate',
        className,
      )}
      {...rest}
    >
      {children}
    </td>
  )
}

/** A full-width group heading inside a table body (a BOQ chapter). */
export function TGroupRow({
  colSpan,
  children,
  className,
}: {
  colSpan: number
  children: ReactNode
  className?: string
}) {
  return (
    <tr>
      <th
        scope="colgroup"
        colSpan={colSpan}
        className={cn(
          'bg-surface-3 px-3 py-2 text-start text-[12.5px] font-bold text-fg',
          className,
        )}
      >
        {children}
      </th>
    </tr>
  )
}

/**
 * A bare number rendered inside Arabic prose.
 *
 * The bidi isolation is load-bearing: without it a value like "1,234.50" sitting
 * next to Arabic text can be reordered by the bidi algorithm — most visibly when a
 * sign, a slash or a bracket is adjacent — and the printed figure stops matching
 * the stored one. Anything already carrying its own Arabic unit ("55.20 م²") reads
 * correctly in RTL flow and does not need this.
 */
export function Num({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('num tnum', className)}>{children}</span>
}
