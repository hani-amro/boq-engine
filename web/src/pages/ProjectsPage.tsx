/**
 * SHELL PAGE — the projects list.
 *
 * Deliberately thin: it shows the app working end to end
 * (fetch → render → create → navigate).
 */
import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderPlus, Layers, Plus } from 'lucide-react'
import { toast } from 'sonner'

import { api, qk } from '../api/client'
import { fmt_int, fmt_relative } from '../lib/format'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  Select,
  SkeletonCard,
} from '../ui'

export default function ProjectsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({
    name: '',
    client_name: '',
    region: 'levant',
    currency: 'USD',
  })

  const projects = useQuery({ queryKey: qk.projects, queryFn: () => api.list_projects() })
  const books = useQuery({ queryKey: qk.price_books, queryFn: () => api.price_books() })

  const create = useMutation({
    mutationFn: () => api.create_project(form),
    onSuccess: (res) => {
      void qc.invalidateQueries({ queryKey: qk.projects })
      setOpen(false)
      toast.success('أُنشئ المشروع.')
      navigate(`/p/${res.id}`)
    },
  })

  const regions = Array.from(new Set((books.data ?? []).map((b) => b.region)))

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold">المشاريع</h1>
          <p className="mt-0.5 text-[13.5px] text-muted">
            كل مشروع يحمل فراغاته وتشطيباتها، ومنه يُشتقّ جدول الكميات وعرض السعر.
          </p>
        </div>
        <Button variant="primary" icon={<Plus />} onClick={() => setOpen(true)}>
          مشروع جديد
        </Button>
      </div>

      {projects.isPending ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : (projects.data?.length ?? 0) === 0 ? (
        <Card flush>
          <EmptyState
            icon={<FolderPlus />}
            title="لا توجد مشاريع بعد"
            description="أنشئ مشروعاً، ثم أضف فراغاته وأبعادها؛ الحصر والتسعير يُبنيان منها تلقائياً."
            action={
              <Button variant="primary" icon={<Plus />} onClick={() => setOpen(true)}>
                مشروع جديد
              </Button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.data?.map((p) => (
            <Link key={p.id} to={`/p/${p.id}`} className="group block">
              <Card className="h-full transition-shadow group-hover:shadow-md">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="truncate font-semibold group-hover:text-primary">
                    {p.name || 'مشروع بلا اسم'}
                  </h2>
                  <Badge tone="neutral" size="sm">
                    {p.currency}
                  </Badge>
                </div>
                <p className="mt-1 truncate text-[13px] text-muted">
                  {p.client_name || 'بدون اسم عميل'}
                </p>
                <div className="mt-4 flex items-center gap-3 text-[12.5px] text-subtle">
                  <span className="inline-flex items-center gap-1.5">
                    <Layers className="size-3.5" aria-hidden />
                    {fmt_int(p.space_count)} فراغ
                  </span>
                  <span>·</span>
                  <span>آخر تعديل {fmt_relative(p.updated_at)}</span>
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}

      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="مشروع جديد"
        description="المنطقة والعملة تحددان دليل الأسعار المستعمل في عرض السعر."
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              إلغاء
            </Button>
            <Button
              variant="primary"
              loading={create.isPending}
              disabled={!form.name.trim()}
              onClick={() => create.mutate()}
            >
              إنشاء
            </Button>
          </>
        }
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="اسم المشروع" required className="sm:col-span-2">
            {(id) => (
              <Input
                id={id}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="شقة السيد أحمد — الطابق الثالث"
              />
            )}
          </Field>
          <Field label="اسم العميل" className="sm:col-span-2">
            {(id) => (
              <Input
                id={id}
                value={form.client_name}
                onChange={(e) => setForm({ ...form, client_name: e.target.value })}
              />
            )}
          </Field>
          <Field label="المنطقة">
            {(id) => (
              <Select
                id={id}
                value={form.region}
                onChange={(e) => setForm({ ...form, region: e.target.value })}
                options={
                  regions.length
                    ? regions.map((r) => ({ value: r, label: r }))
                    : [{ value: 'levant', label: 'levant' }]
                }
              />
            )}
          </Field>
          <Field label="العملة">
            {(id) => (
              <Select
                id={id}
                value={form.currency}
                onChange={(e) => setForm({ ...form, currency: e.target.value })}
                options={['USD', 'SYP', 'JOD', 'SAR', 'AED', 'EGP'].map((c) => ({
                  value: c,
                  label: c,
                }))}
              />
            )}
          </Field>
        </div>
      </Dialog>
    </div>
  )
}
