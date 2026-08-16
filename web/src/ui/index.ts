/**
 * The design system, in one import.
 *
 *   import { Button, Card, NumberInput, TableWrap, Table, TH, TD } from '../ui'
 *
 * Everything below is RTL-native: not one component uses a physical direction
 * (left/right, ml/mr, pl/pr). Layout is expressed with logical properties
 * (ps/pe, ms/me, start/end, text-start/text-end) so the whole UI mirrors from the
 * single `dir="rtl"` on <html>, and colours come from tokens that already carry a
 * dark variant — no component needs a `dark:` prefix.
 */

export { Button, ButtonRow, type ButtonProps, type ButtonSize, type ButtonVariant } from './Button'
export {
  CONTROL_BASE,
  CONTROL_SIZES,
  Field,
  Input,
  Label,
  Textarea,
  type ControlSize,
  type FieldProps,
  type InputProps,
  type TextareaProps,
} from './Input'
export { NumberInput, type NumberInputProps } from './NumberInput'
export { Select, type SelectOption, type SelectProps } from './Select'
export { Card, CardBody, CardFooter, CardHeader, Stat, type CardProps } from './Card'
export {
  Num,
  TBody,
  TD,
  TFoot,
  TGroupRow,
  TH,
  THead,
  TR,
  Table,
  TableWrap,
  type TdProps,
  type ThProps,
  type TrProps,
} from './Table'
export { Badge, CodeChip, type BadgeProps, type BadgeTone } from './Badge'
export { ConfirmDialog, Dialog, type DialogProps } from './Dialog'
export { TabPanel, Tabs, type TabItem, type TabsProps } from './Tabs'
export { EmptyState, ErrorState, WarningList, type EmptyStateProps } from './EmptyState'
export { LoadingLine, Skeleton, SkeletonCard, SkeletonRows, SkeletonText } from './Skeleton'
