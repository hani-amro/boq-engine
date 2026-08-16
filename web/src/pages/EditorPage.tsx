/**
 * SHELL PAGE — the space editor.
 *
 * Thin on purpose. It shows the store's edit + autosave loop working (add a space,
 * type a height, watch the header indicator go dirty → saving → saved) and is
 * meant to be replaced by the real editor. Keep the store calls; they are the
 * whole contract this page has with the rest of the app.
 *
 * Two things here are NOT stylistic and should survive that rewrite:
 *
 * * **A room can be given a dimension.** Floor area is the one measurement that
 *   makes a room measurable at all without a wall editor or a polygon tool. A room
 *   with no dimension is still saved — the server keeps it and says so in a warning
 *   rather than refusing the whole document — but nothing can be measured in it, so
 *   the field is on screen from the moment the room exists.
 * * **Deleting a room is confirmed, by name and by what is inside it.** The delete
 *   is instant, un-undoable and persisted 1.2 s later by the autosave; the only copy
 *   of ten minutes of measurements is the one being removed.
 */
import { useState } from 'react'
import { Plus, Ruler, Trash2 } from 'lucide-react'

import { useProjectStore, useProject } from '../store'
import { SPACE_TYPES, type Space } from '../api/types'
import { fmt_int, space_type_ar } from '../lib/format'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  EmptyState,
  Field,
  Input,
  NumberInput,
  Select,
} from '../ui'

/** What the confirm prompt has to name, because it is what disappears. */
function contents_ar(s: Space): string {
  const openings = s.walls.reduce((n, w) => n + w.openings.length, 0)
  const parts = [
    `${fmt_int(s.walls.length)} جدار`,
    `${fmt_int(openings)} فتحة`,
    `${fmt_int(s.finishes.length)} تشطيب`,
  ]
  return parts.join(' و')
}

export default function EditorPage() {
  const project = useProject()
  const add_space = useProjectStore((s) => s.add_space)
  const update_space = useProjectStore((s) => s.update_space)
  const remove_space = useProjectStore((s) => s.remove_space)
  const set_meta = useProjectStore((s) => s.set_meta)

  const [pending_delete, set_pending_delete] = useState<string | null>(null)

  if (!project) return null

  const doomed = pending_delete
    ? (project.spaces.find((s) => s.id === pending_delete) ?? null)
    : null

  return (
    <div className="space-y-5">
      <Card flush>
        <CardHeader title="بيانات المشروع" />
        <CardBody className="grid gap-4 sm:grid-cols-2">
          <Field label="اسم المشروع">
            {(id) => (
              <Input
                id={id}
                value={project.name}
                onChange={(e) => set_meta({ name: e.target.value })}
              />
            )}
          </Field>
          <Field label="اسم العميل">
            {(id) => (
              <Input
                id={id}
                value={project.client_name}
                onChange={(e) => set_meta({ client_name: e.target.value })}
              />
            )}
          </Field>
        </CardBody>
      </Card>

      <Card flush>
        <CardHeader
          title="الفراغات"
          subtitle={`${fmt_int(project.spaces.length)} فراغ في هذا المشروع`}
          actions={
            <Button variant="primary" size="sm" icon={<Plus />} onClick={() => add_space()}>
              إضافة فراغ
            </Button>
          }
        />
        {project.spaces.length === 0 ? (
          <EmptyState
            icon={<Ruler />}
            title="لا توجد فراغات"
            description="أضف فراغاً وأدخل أبعاده؛ بدون فراغ واحد على الأقل لا يوجد ما يُحصر."
            action={
              <Button variant="primary" icon={<Plus />} onClick={() => add_space()}>
                إضافة فراغ
              </Button>
            }
          />
        ) : (
          <ul className="divide-y divide-border">
            {project.spaces.map((s) => {
              const measurable =
                (s.floor_area_m2 !== null && s.floor_area_m2 > 0) ||
                (s.polygon !== null && s.polygon.length >= 3) ||
                s.walls.some((w) => w.length_m > 0)
              return (
                <li
                  key={s.id}
                  className="grid items-end gap-3 p-4 sm:grid-cols-[1fr_11rem_8rem_9rem_auto]"
                >
                  <Field label="اسم الفراغ">
                    {(id) => (
                      <Input
                        id={id}
                        value={s.name}
                        placeholder="صالون"
                        onChange={(e) => update_space(s.id, { name: e.target.value })}
                      />
                    )}
                  </Field>
                  <Field label="النوع">
                    {(id) => (
                      <Select
                        id={id}
                        value={s.type}
                        onChange={(e) => update_space(s.id, { type: e.target.value })}
                        options={SPACE_TYPES.map((t) => ({ value: t, label: space_type_ar(t) }))}
                      />
                    )}
                  </Field>
                  <Field label="الارتفاع الصافي">
                    {(id) => (
                      <NumberInput
                        id={id}
                        // A plain metre, not "م.ط": a clear height is a dimension,
                        // and "م.ط" is the LINE-ITEM unit for a running metre.
                        unit="م"
                        value={s.height_m}
                        min={2}
                        max={6}
                        onValueChange={(v) => update_space(s.id, { height_m: v ?? 0 })}
                      />
                    )}
                  </Field>
                  <Field label="مساحة الأرضية">
                    {(id) => (
                      <NumberInput
                        id={id}
                        unit="m2"
                        // Empty is null — "لم تُقس بعد", never a measured zero. The
                        // engine reads null as "اشتقها من المضلع"، and reports the
                        // room as unmeasurable when there is no polygon either.
                        value={s.floor_area_m2}
                        min={0}
                        placeholder="—"
                        onValueChange={(v) => update_space(s.id, { floor_area_m2: v })}
                      />
                    )}
                  </Field>
                  <div className="flex items-center gap-2 pb-1">
                    {measurable ? (
                      <Badge size="sm" tone="neutral">
                        {fmt_int(s.walls.length)} جدار
                      </Badge>
                    ) : (
                      <Badge size="sm" tone="warn" dot>
                        بلا أبعاد
                      </Badge>
                    )}
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`حذف الفراغ ${s.name || s.id}`}
                      onClick={() => set_pending_delete(s.id)}
                    >
                      <Trash2 />
                    </Button>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Card>

      {/* The delete is persisted by the autosave 1.2 s later, and there is no undo
          and no second copy — so it is answered explicitly, never by a misclick. */}
      <ConfirmDialog
        open={doomed !== null}
        onClose={() => set_pending_delete(null)}
        onConfirm={() => {
          if (doomed) remove_space(doomed.id)
          set_pending_delete(null)
        }}
        danger
        title={`حذف الفراغ «${doomed?.name || doomed?.id || ''}»؟`}
        description={
          doomed
            ? `سيُحذف الفراغ وكل ما فيه: ${contents_ar(doomed)}. الحذف يُحفظ على الخادم ` +
              `خلال ثوانٍ ولا يمكن التراجع عنه — لا توجد نسخة أخرى من هذه القياسات.`
            : undefined
        }
        confirmLabel="احذف الفراغ"
        cancelLabel="إبقاء الفراغ"
      />
    </div>
  )
}
