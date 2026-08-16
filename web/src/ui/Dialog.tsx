import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '../lib/cn'
import { Button } from './Button'

export interface DialogProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  children?: ReactNode
  /** Buttons for the footer; they sit at the logical end (the LEFT under RTL). */
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  /** Set false for a destructive confirm that must be answered explicitly. */
  dismissable?: boolean
}

const SIZES = { sm: 'max-w-md', md: 'max-w-xl', lg: 'max-w-3xl' } as const

/**
 * Built on the native <dialog>.
 *
 * `showModal()` gives the focus trap, the inert background, Escape-to-close and
 * the top layer for free — all of which a hand-rolled overlay gets subtly wrong,
 * and all of which are harder to get right under RTL. Only the chrome is styled.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  dismissable = true,
}: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (open && !el.open) el.showModal()
    else if (!open && el.open) el.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      dir="rtl"
      onCancel={(e) => {
        e.preventDefault()
        if (dismissable) onClose()
      }}
      onClick={(e) => {
        // A click that lands on the <dialog> itself is a click on the backdrop:
        // the panel below stops propagation of its own clicks.
        if (dismissable && e.target === ref.current) onClose()
      }}
      className={cn(
        'm-auto w-[calc(100%-2rem)] rounded-xl border border-border bg-surface p-0 text-fg shadow-lg',
        'backdrop:bg-black/45 backdrop:backdrop-blur-[1px]',
        'open:animate-[dialog-in_120ms_ease-out]',
        SIZES[size],
      )}
    >
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">{title}</h2>
          {description && (
            <p className="mt-1 text-[13px] leading-relaxed text-muted">{description}</p>
          )}
        </div>
        {dismissable && (
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            aria-label="إغلاق"
            className="-me-2 -mt-1 shrink-0"
          >
            <X aria-hidden />
          </Button>
        )}
      </div>

      {children !== undefined && (
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
      )}

      {footer && (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-surface-2/60 px-5 py-3.5">
          {footer}
        </div>
      )}
    </dialog>
  )
}

/** The two-button confirm every destructive action in the editor routes through. */
export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel = 'تأكيد',
  cancelLabel = 'إلغاء',
  danger = false,
  loading = false,
}: {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: ReactNode
  description?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  loading?: boolean
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            onClick={onConfirm}
            loading={loading}
            autoFocus
          >
            {confirmLabel}
          </Button>
        </>
      }
    />
  )
}
