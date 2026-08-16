/**
 * Live, inline, non-blocking validation — in Arabic, and never a blocked field.
 *
 * Nothing here refuses an edit. A surveyor types a room in the order the tape comes
 * off the wall, and a form that will not accept "4.20" until a height exists is a
 * form that gets abandoned for a paper pad. So every finding below is a sentence
 * shown beside the thing it is about, and the document saves either way — exactly
 * as the server behaves, which keeps an incomplete room and reports it.
 *
 * Two rules for the text itself:
 *
 * * **Say what will happen to the money, not what the field wants.** «لن يُحصر منه
 *   أي بند» is actionable; «قيمة مطلوبة» is not.
 * * **Never invent a limit the engine does not have.** The sanity bands here
 *   (a 2–6 m clear height, 1–200 m² for one room) are stated as "check this",
 *   because a 7 m atrium and a 0.9 m² niche cupboard are both real.
 */

import type { Point, PriceBookInfo, Project, RuleInfo, Space } from '../../api/types'
import { fmt_int, fmt_num, opening_kind_ar, surface_ar } from '../../lib/format'
import { geometry_of, is_hole, list_openings, opening_face_m2 } from './model'

/** A room with no measurable dimension produces nothing at all — the single most
 *  expensive silent state in the editor, so it leads every list. */
export function space_warnings(space: Space, rules_by_key: Map<string, RuleInfo>): string[] {
  const out: string[] = []
  const g = geometry_of(space)

  /* -- identity ---------------------------------------------------------- */
  if (!space.name.trim()) {
    out.push('هذا الفراغ بلا اسم، وسيظهر في جدول الكميات بمعرّفه فقط. اكتب الاسم الذي يعرفه صاحب العمل.')
  }

  /* -- floor ------------------------------------------------------------- */
  if (g.area_m2 === null) {
    out.push('لم تُدخل أبعاد هذا الفراغ بعد، فلن يُحصر منه أي بند. أدخل الطول والعرض، أو حوّله إلى غرفة غير منتظمة وأدخل نقاطه.')
  } else if (g.area_m2 < 1) {
    out.push(
      `المساحة المحسوبة ${fmt_num(g.area_m2)} م² أصغر من متر مربّع واحد. تأكّد من أن الأبعاد بالمتر وليست بالسنتيمتر.`,
    )
  } else if (g.area_m2 > 200) {
    out.push(
      `المساحة المحسوبة ${fmt_num(g.area_m2)} م² كبيرة على فراغ واحد. راجع الأبعاد، أو قسّم المساحة إلى فراغات لتسهيل المراجعة.`,
    )
  }

  if (g.typed_area) {
    out.push(
      'مساحة هذا الفراغ مُدخلة كرقم مباشر دون طول وعرض، فلا يوجد محيط تُحسب منه مساحة الجدران والوزرة. أدخل الطول والعرض لاستكمالها.',
    )
  }

  /* -- height ------------------------------------------------------------ */
  if (space.height_m <= 0) {
    out.push('الارتفاع الصافي غير مُدخل، فلن تُحسب مساحة الجدران ولا الوزرة في هذا الفراغ.')
  } else if (space.height_m < 2 || space.height_m > 6) {
    out.push(
      `الارتفاع الصافي ${fmt_num(space.height_m)} م خارج المدى المعتاد (2 – 6 م). تأكّد من القياس قبل اعتماد الحصر.`,
    )
  }

  /* -- polygon ----------------------------------------------------------- */
  if (g.mode === 'polygon') {
    const points = space.polygon ?? []
    if (points.length < 3) {
      out.push('المضلّع يحتاج ثلاث نقاط على الأقل ليُحدّد مساحة. أضف النقاط الناقصة أو ارجع إلى إدخال طول × عرض.')
    } else if (g.area_m2 === null) {
      out.push('مساحة المضلّع تساوي صفراً. تأكّد من الإحداثيات ومن أن النقاط مرتّبة على محيط الغرفة بالتتابع.')
    }
    const zero_edges = points.length >= 3 ? count_zero_edges(points) : 0
    if (zero_edges > 0) {
      out.push(
        `${fmt_int(zero_edges)} من أضلاع المضلّع طولها صفر (نقطتان متطابقتان). احذف النقاط المكرّرة حتى لا يظهر جدار بلا طول.`,
      )
    }
  }

  /* -- openings ---------------------------------------------------------- */
  const openings = list_openings(space)
  let holes_m2 = 0
  for (const { opening: o } of openings) {
    const label = opening_kind_ar(o.kind)
    if (o.width_m <= 0 || o.height_m <= 0) {
      out.push(`الفتحة «${label}» بلا أبعاد كاملة، فلن تُخصم ولن تُحتسب. أدخل العرض والارتفاع أو احذفها.`)
    }
    if (o.count < 1) {
      out.push(`عدد الفتحات من نوع «${label}» أقلّ من واحد، فلن تدخل الحصر إطلاقاً.`)
    }
    if (space.height_m > 0 && o.height_m > space.height_m) {
      out.push(
        `ارتفاع الفتحة «${label}» ${fmt_num(o.height_m)} م أكبر من الارتفاع الصافي للفراغ ${fmt_num(space.height_m)} م.`,
      )
    }
    if (is_hole(o.kind)) holes_m2 += opening_face_m2(o)
  }

  if (g.wall_area_m2 !== null && g.wall_area_m2 > 0 && holes_m2 > g.wall_area_m2 * 0.6) {
    out.push(
      `مساحة الفتحات المخصومة ${fmt_num(holes_m2)} م² تتجاوز 60٪ من مساحة الجدران ${fmt_num(g.wall_area_m2)} م². راجع الأعداد والأبعاد.`,
    )
  }

  /* -- finishes ---------------------------------------------------------- */
  if (space.finishes.length === 0) {
    out.push('لا يوجد نظام تشطيب مُسنَد إلى أي سطح في هذا الفراغ، فلن يظهر له بند واحد في جدول الكميات.')
  }
  for (const f of space.finishes) {
    const rule = rules_by_key.get(f.system)
    if (!rule) {
      out.push(
        `نظام التشطيب المُسنَد إلى سطح «${surface_ar(f.surface)}» غير موجود في دليل القواعد الحالي، ولن يُحصر. أعد اختياره.`,
      )
    } else if (rule.surface !== f.surface) {
      out.push(
        `نظام «${rule.name_ar}» معرّف لسطح «${surface_ar(rule.surface)}» وهو مُسنَد هنا إلى «${surface_ar(f.surface)}». أعد اختياره حتى لا يُحصر على السطح الخطأ.`,
      )
    }
  }

  return out
}

function count_zero_edges(points: Point[]): number {
  let n = 0
  for (let i = 0; i < points.length; i++) {
    const a = points[i]
    const b = points[(i + 1) % points.length]
    if (a[0] === b[0] && a[1] === b[1]) n++
  }
  return n
}

/**
 * Project-level findings — the ones that are about the document as a whole and so
 * have no single field to sit under.
 */
export function project_warnings(project: Project, books: PriceBookInfo[]): string[] {
  const out: string[] = []

  if (!project.name.trim()) {
    out.push('المشروع بلا اسم؛ سيُطبع عرض السعر بترويسة فارغة.')
  }
  if (!project.client_name.trim()) {
    out.push('لم يُدخل اسم العميل، وهو ما يظهر في ترويسة عرض السعر والمخاطبة.')
  }
  if (project.spaces.length === 0) {
    out.push('لا يوجد أي فراغ في هذا المشروع بعد. أضف فراغاً واحداً على الأقل ليصبح هناك ما يُحصر.')
  }

  const seen = new Map<string, number>()
  for (const s of project.spaces) {
    const key = s.name.trim()
    if (key) seen.set(key, (seen.get(key) ?? 0) + 1)
  }
  const repeated = [...seen.entries()].filter(([, n]) => n > 1)
  if (repeated.length > 0) {
    out.push(
      `تكرّرت أسماء فراغات: ${repeated.map(([name, n]) => `«${name}» ×${fmt_int(n)}`).join('، ')}. ` +
        'الأسماء المتطابقة تجعل مراجعة جدول الكميات وتوزيع الكميات على الغرف أصعب — ميّزها برقم أو موقع.',
    )
  }

  // Only speak about the price book once the catalogue has actually arrived; an
  // empty list mid-fetch is not the same fact as "no book for this region".
  if (books.length > 0) {
    const for_region = books.filter((b) => b.region === project.region)
    if (for_region.length === 0) {
      out.push(
        `لا يوجد دليل أسعار للمنطقة «${project.region || '—'}»، ولن يمكن إصدار عرض سعر منها. اختر منطقة من القائمة.`,
      )
    } else {
      if (for_region.every((b) => b.is_placeholder)) {
        out.push(
          'دليل أسعار هذه المنطقة مبدئي (أسعار افتراضية غير معتمدة). الكميات صحيحة، أما الأسعار فلا تُرسل إلى العميل قبل اعتماد دليل حقيقي.',
        )
      }
      const currencies = new Set(for_region.map((b) => b.currency))
      if (!currencies.has(project.currency)) {
        out.push(
          `عملة المشروع (${project.currency || '—'}) لا تطابق عملة دليل أسعار المنطقة (${[...currencies].join('، ')}). ` +
            'اضبط العملة قبل التسعير حتى لا تُجمع مبالغ بعملتين.',
        )
      }
    }
  }

  return out
}
