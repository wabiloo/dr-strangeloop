/**
 * Client-side mirror of franken_ts.timeline.resolve_markers' containment
 * logic (asset-index based, not seconds) -- used to lay markers out into
 * stacked "lanes" in the editor timeline and to auto-fill segment_num/
 * segments_expected for immediate UI feedback while editing.
 *
 * This is a UI convenience only: the source of truth for validation
 * (contiguity, disjoint/nested-only overlap) and for real
 * frame-accurate seconds is still franken-ts itself, via
 * `Config.model_validate` on save and `POST /resolve-markers` for a
 * ffprobe'd preview. If this ever disagrees with the backend, the
 * backend wins.
 */

import { isInstantTypeId } from './segmentationPresets'

export interface MarkerLike {
  _ui_id?: number
  event_id: number
  assets: string[]
  splice_type?: 'splice_insert' | 'time_signal'
  segmentation?: { type_id: string; segment_num?: number | null; segments_expected?: number | null; sub_segment_num?: number | null; sub_segments_expected?: number | null }
  break_interval?: number
}

export type NumberingScheme = 'SCTE35_2019A' | 'SCTE35_2023R1' | 'AF2M_SNPTV'
export type AssetRole = 'advert' | 'jingle'

type SemanticRole = { category: 'content' | 'advertising' | 'alternate'; level: number } | null

function semanticRole(marker: MarkerLike): SemanticRole {
  if (marker.splice_type !== 'time_signal' || !marker.segmentation?.type_id) return null
  const id = Number.parseInt(marker.segmentation.type_id, 16)
  if ([0x10, 0x17, 0x19].includes(id)) return { category: 'content', level: 1 }
  if (id === 0x20) return { category: 'content', level: 2 }
  if (id === 0x50) return { category: 'content', level: 0 }
  if (id === 0x22) return { category: 'advertising', level: 0 }
  if ([0x34, 0x36, 0x38, 0x3a].includes(id)) return { category: 'advertising', level: 1 }
  if ([0x44, 0x46].includes(id)) return { category: 'advertising', level: 2 }
  if ([0x30, 0x32, 0x3c, 0x3e].includes(id)) return { category: 'advertising', level: 3 }
  if (id === 0x42) return { category: 'alternate', level: 0 }
  return null
}

/** Returns semantic violations for type-aware interval containment. */
export function semanticMarkerIssues<M extends MarkerLike>(
  markers: M[],
  assetIdToIndex: Map<string, number>,
): string[] {
  const spans = markers.map((marker) => {
    const indices = marker.assets.map((id) => assetIdToIndex.get(id)).filter((n): n is number => n !== undefined)
    return { marker, lo: indices.length ? Math.min(...indices) : -1, hi: indices.length ? Math.max(...indices) : -1 }
  })
  const issues: string[] = []
  for (let i = 0; i < spans.length; i++) for (let j = i + 1; j < spans.length; j++) {
    const a = spans[i], b = spans[j]
    if (a.lo < 0 || b.lo < 0 || a.hi < b.lo || b.hi < a.lo) continue
    const ra = semanticRole(a.marker), rb = semanticRole(b.marker)
    if (!ra || !rb) continue
    const acb = a.lo <= b.lo && b.hi <= a.hi
    const bca = b.lo <= a.lo && a.hi <= b.hi
    const same = a.lo === b.lo && a.hi === b.hi
    const ta = Number.parseInt(a.marker.segmentation?.type_id ?? '-1', 16)
    const tb = Number.parseInt(b.marker.segmentation?.type_id ?? '-1', 16)
    if (same && ra.category === rb.category && ra.level === rb.level && ta === tb) {
      issues.push(`Markers #${a.marker.event_id} and #${b.marker.event_id} duplicate the same signal over the same span.`)
      continue
    }
    if (ra.category === 'content' && rb.category === 'content' && ra.level === 2 && rb.level === 2) continue
    if (ra.category === 'content' && rb.category === 'content' && ra.level === 1 && rb.level === 1 && (ta === 0x17 || tb === 0x17)) continue
    if (ra.category === 'advertising' && rb.category === 'advertising' && ra.level === 1 && rb.level === 1) {
      if (!acb && !bca) issues.push(`Placement Opportunities #${a.marker.event_id} and #${b.marker.event_id} partially overlap; they must be nested or disjoint.`)
      continue
    }
    if (ra.category === rb.category) {
      if (ra.level === rb.level) {
        issues.push(`Markers #${a.marker.event_id} and #${b.marker.event_id} overlap at the same ${ra.category} level.`)
      } else if (!(ra.level < rb.level ? acb : bca)) {
        issues.push(`Markers #${a.marker.event_id} and #${b.marker.event_id} violate the ${ra.category} hierarchy.`)
      }
      continue
    }
    if (ra.category === 'alternate' || rb.category === 'alternate') {
      if (!(ra.category === 'alternate' ? bca : acb)) {
        issues.push(`Alternate Content Opportunity must be contained within Content or Advertisement.`)
      }
      continue
    }
    if (!(ra.category === 'content' ? acb : bca)) {
      issues.push(`Advertising marker #${ra.category === 'advertising' ? a.marker.event_id : b.marker.event_id} crosses a Content span boundary.`)
    }
  }
  return [...new Set(issues)]
}

export function semanticNumberingIssues<M extends MarkerLike>(
  markers: M[],
  assetIdToIndex: Map<string, number>,
): string[] {
  const spans = markers.map((marker) => {
    const indices = marker.assets.map((id) => assetIdToIndex.get(id)).filter((n): n is number => n !== undefined)
    return { marker, lo: indices.length ? Math.min(...indices) : -1, hi: indices.length ? Math.max(...indices) : -1 }
  })
  const type = (m: M) => Number.parseInt(m.segmentation?.type_id ?? '-1', 16)
  const programs = spans.filter(({ marker }) => [0x10,0x17,0x19].includes(type(marker)))
  const breaks = spans.filter(({ marker }) => type(marker) === 0x22)
  const error: string[] = []
  const check = (marker: M, expected: [number, number]) => {
    const got: [number, number] = [marker.segmentation?.segment_num ?? 0, marker.segmentation?.segments_expected ?? 0]
    if (got[0] !== expected[0] || got[1] !== expected[1]) error.push(`Marker #${marker.event_id} numbering is ${got.join('/')}; expected ${expected.join('/')}.`)
  }
  for (const p of programs) if ([0x10,0x17,0x19].includes(type(p.marker))) check(p.marker, [1,1])
  const byParent = new Map<number | null, typeof breaks>()
  for (const item of breaks) {
    const parent = programs.filter((p) => p.lo <= item.lo && item.hi <= p.hi && !(p.lo === item.lo && p.hi === item.hi))
      .sort((a, b) => (a.hi - a.lo) - (b.hi - b.lo))[0]
    const key = parent ? parent.marker._ui_id ?? parent.marker.event_id : null
    byParent.set(key, [...(byParent.get(key) ?? []), item])
  }
  const breakNums = new Map<number, [number, number]>()
  for (const [key, group] of byParent) {
    group.sort((a, b) => a.lo - b.lo)
    const numbered = key !== null
    group.forEach((item, pos) => {
      const expected: [number, number] = numbered ? [pos + 1, group.length] : [0,0]
      check(item.marker, expected)
      breakNums.set(item.marker._ui_id ?? item.marker.event_id, expected)
    })
  }
  for (const item of spans) {
    if (![0x34,0x36,0x38,0x3a,0x44,0x46,0x30,0x32,0x3c,0x3e].includes(type(item.marker))) continue
    const parent = breaks.filter((b) => b.lo <= item.lo && item.hi <= b.hi && !(b.lo === item.lo && b.hi === item.hi))
      .sort((a,b) => (a.hi-a.lo) - (b.hi-b.lo))[0]
    check(item.marker, parent ? (breakNums.get(parent.marker._ui_id ?? parent.marker.event_id) ?? [0,0]) : [0,0])
  }
  return [...new Set(error)]
}

export interface MarkerSpan<M extends MarkerLike = MarkerLike> {
  marker: M
  markerIndex: number
  loIndex: number
  hiIndex: number
  depth: number
  segmentNum: number
  segmentsExpected: number
  subSegmentNum: number | null
  subSegmentsExpected: number | null
  /** True for standalone/instant segmentation types (no defined End
   * partner, e.g. 0x13 Program Breakaway) -- renders as a single point at
   * the span's start rather than a bar spanning start->end. */
  instant: boolean
}

/** True if `marker` is a standalone/instant signal (see
 * segmentationPresets.isInstantTypeId) rather than a Start/End pair.
 * `splice_insert` markers are always a pair (out_of_network true/false). */
export function isInstantMarker(marker: MarkerLike): boolean {
  if (marker.splice_type !== 'time_signal') return false
  const typeId = marker.segmentation?.type_id
  return typeId ? isInstantTypeId(typeId) : false
}

/** Resolve each marker's asset-index span, containment depth (0 = top-level,
 * i.e. no other marker strictly contains it), and sibling segment_num/
 * segments_expected -- same "immediate parent = smallest strictly
 * containing span" rule as the Python implementation. */
export function layoutMarkers<M extends MarkerLike>(
  markers: M[],
  assetIdToIndex: Map<string, number>,
  strict = true,
  scheme: NumberingScheme = 'SCTE35_2023R1',
  breakNumberingSupported = false,
  assetRoles: ReadonlyMap<string, AssetRole | null> = new Map(),
): MarkerSpan<M>[] {
  const spans = markers.map((marker) => {
    const indices = marker.assets.map((id) => assetIdToIndex.get(id)).filter((i): i is number => i !== undefined)
    const loIndex = indices.length ? Math.min(...indices) : -1
    const hiIndex = indices.length ? Math.max(...indices) : -1
    return { marker, loIndex, hiIndex }
  })

  function contains(outer: { loIndex: number; hiIndex: number }, inner: { loIndex: number; hiIndex: number }): boolean {
    return (
      outer.loIndex <= inner.loIndex &&
      inner.hiIndex <= outer.hiIndex &&
      !(outer.loIndex === inner.loIndex && outer.hiIndex === inner.hiIndex)
    )
  }

  const parentOf = spans.map((span, i) => {
    let best: number | null = null
    let bestSize = Infinity
    spans.forEach((other, j) => {
      if (i === j) return
      if (contains(other, span)) {
        const size = other.hiIndex - other.loIndex
        if (size < bestSize) {
          best = j
          bestSize = size
        }
      }
    })
    return best
  })

  const depthOf = spans.map(() => -1)
  function depthOfIndex(i: number): number {
    if (depthOf[i] !== -1) return depthOf[i]
    const p = parentOf[i]
    const d = p === null ? 0 : depthOfIndex(p) + 1
    depthOf[i] = d
    return d
  }
  spans.forEach((_, i) => depthOfIndex(i))

  const semanticNumbers = strict ? getSemanticNumbers(spans, markers, scheme, breakNumberingSupported, assetRoles) : new Map<number, { outer: [number, number]; inner: [number, number] | null }>()

  return spans.map((span, i) => ({
    marker: span.marker,
    markerIndex: i,
    loIndex: span.loIndex,
    hiIndex: span.hiIndex,
    depth: depthOf[i],
    segmentNum: semanticNumbers.get(i)?.outer[0] ?? (strict ? 0 : span.marker.segmentation?.segment_num ?? 0),
    segmentsExpected: semanticNumbers.get(i)?.outer[1] ?? (strict ? 0 : span.marker.segmentation?.segments_expected ?? 0),
    subSegmentNum: semanticNumbers.get(i)?.inner?.[0] ?? (strict ? null : span.marker.segmentation?.sub_segment_num ?? null),
    subSegmentsExpected: semanticNumbers.get(i)?.inner?.[1] ?? (strict ? null : span.marker.segmentation?.sub_segments_expected ?? null),
    instant: isInstantMarker(span.marker),
  }))
}

function getSemanticNumbers<M extends MarkerLike>(
  spans: { loIndex: number; hiIndex: number }[],
  markers: M[],
  scheme: NumberingScheme,
  breakNumberingSupported: boolean,
  assetRoles: ReadonlyMap<string, AssetRole | null>,
): Map<number, { outer: [number, number]; inner: [number, number] | null }> {
  const result = new Map<number, { outer: [number, number]; inner: [number, number] | null }>()
  const typeId = (index: number) => {
    const raw = markers[index]?.segmentation?.type_id
    return raw ? Number.parseInt(raw, 16) : -1
  }
  const ranges = (ids: number[]) => ids.map((i) => ({ i, lo: spans[i].loIndex, hi: spans[i].hiIndex }))
  type Range = ReturnType<typeof ranges>[number]
  const all = ranges(markers.map((_, i) => i).filter((i) => markers[i].splice_type === 'time_signal'))
  const programs = ranges(markers.map((_, i) => i).filter((i) => [0x10, 0x17, 0x19].includes(typeId(i))))
  const breaks = ranges(markers.map((_, i) => i).filter((i) => typeId(i) === 0x22))
  const pos = (items: Range[], target: Range): [number, number] => [items.findIndex((s) => s.i === target.i) + 1, items.length]
  const ordered = (items: Range[]) => [...items].sort((a, b) => a.lo - b.lo || a.hi - b.hi || markers[a.i].event_id - markers[b.i].event_id)
  const parent = (item: Range, parents: Range[]): Range | undefined => parents.filter((p) => p.i !== item.i && p.lo <= item.lo && item.hi <= p.hi)
    .sort((a, b) => (a.hi - a.lo) - (b.hi - b.lo) || a.lo - b.lo || markers[a.i].event_id - markers[b.i].event_id)[0]
  const set = (item: Range, outer: [number, number], inner: [number, number] | null = null) => result.set(item.i, { outer, inner })
  const po = (t: number) => [0x34, 0x36, 0x38, 0x3a].includes(t)
  const ad = (t: number) => [0x30, 0x32, 0x3c, 0x3e].includes(t)
  const block = (t: number) => [0x44, 0x46].includes(t)
  const isJingle = (item: Range) => assetRoles.get(markers[item.i].assets[0]) === 'jingle'

  for (const item of all) {
    const t = typeId(item.i)
    set(item, [0x10,0x13,0x14,0x15,0x16,0x17,0x19,0x24,0x26].includes(t) ? [1, 1] : [0, 0])
  }
  for (const item of ordered(all.filter((s) => typeId(s.i) === 0x20))) {
    const p = parent(item, programs)?.i
    const group = ordered(all.filter((s) => typeId(s.i) === 0x20 && parent(s, programs)?.i === p))
    set(item, pos(group, item))
  }
  for (const item of ordered(breaks)) {
    const p = parent(item, programs)?.i
    const group = ordered(breaks.filter((s) => parent(s, programs)?.i === p && (p !== undefined || (markers[s.i].break_interval ?? 1) === (markers[item.i].break_interval ?? 1))))
    set(item, scheme === 'AF2M_SNPTV' ? [1, 1] : breakNumberingSupported ? pos(group, item) : [0, 0])
  }
  for (const item of ordered(all.filter((s) => po(typeId(s.i)) || ad(typeId(s.i)) || block(typeId(s.i))))) {
    const br = parent(item, breaks)
    const group = ordered(all.filter((s) => parent(s, breaks)?.i === br?.i))
    const t = typeId(item.i)
    const outer = br ? result.get(br.i)?.outer ?? [0, 0] as [number, number] : [0, 0] as [number, number]
    if (scheme === 'AF2M_SNPTV') {
      if (po(t)) set(item, [1, 1])
      else {
        const spots = group.filter((s) => typeId(s.i) === 0x30 && !isJingle(s))
        if (!isJingle(item)) set(item, pos(spots, item))
        else {
          const ads = group.filter((s) => typeId(s.i) === 0x30)
          set(item, ads[0]?.i === item.i && item.lo === br?.lo ? [0, spots.length] : [0, 0])
        }
      }
    } else if (scheme === 'SCTE35_2019A') {
      if (po(t)) set(item, outer, br ? pos(group.filter((s) => po(typeId(s.i))), item) : null)
      else set(item, br ? pos(group.filter((s) => ad(typeId(s.i))), item) : [0, 0])
    } else if (block(t)) {
      const ads = group.filter((s) => ad(typeId(s.i)))
      const first = ads.findIndex((s) => item.lo <= s.lo && s.hi <= item.hi) + 1
      set(item, outer, first ? [first, group.filter((s) => block(typeId(s.i))).length] : null)
    } else {
      set(item, outer, br ? pos(group.filter((s) => po(t) ? po(typeId(s.i)) : ad(typeId(s.i))), item) : null)
    }
  }
  return result
}

/** Next unused event_id across the flat `markers` list -- max(used ids) + 1,
 * starting at 1. Mirrors the uniqueness namespace franken_ts.config.Config
 * enforces server-side. */
export function nextEventId(usedIds: (number | null | undefined)[]): number {
  const used = usedIds.filter((id): id is number => typeof id === 'number')
  return used.length ? Math.max(...used) + 1 : 1
}

/** Reorders `layoutMarkers`' output into a depth-first tree traversal --
 * every marker's children immediately follow it, recursively -- so a flat
 * list with only indentation (no explicit parent lines) still reads
 * unambiguously as a tree. Plain array/authoring order does NOT guarantee
 * this: e.g. [Break, PPO, Ad(jingle), Ad, Ad, Ad] has the jingle-Ad sit
 * between PPO and PPO's own children in authoring order, which would make
 * indentation alone look like the jingle-Ad is PPO's parent -- or the
 * children's parent -- depending on how you read it. */
export function orderForDisplay<M extends MarkerLike>(spans: MarkerSpan<M>[]): MarkerSpan<M>[] {
  function contains(outer: MarkerSpan<M>, inner: MarkerSpan<M>): boolean {
    return (
      outer.loIndex <= inner.loIndex &&
      inner.hiIndex <= outer.hiIndex &&
      !(outer.loIndex === inner.loIndex && outer.hiIndex === inner.hiIndex)
    )
  }

  const parentOf = spans.map((span, i) => {
    let best: number | null = null
    let bestSize = Infinity
    spans.forEach((other, j) => {
      if (i === j) return
      if (contains(other, span)) {
        const size = other.hiIndex - other.loIndex
        if (size < bestSize) {
          best = j
          bestSize = size
        }
      }
    })
    return best
  })

  const childrenOf = new Map<number | null, number[]>()
  parentOf.forEach((p, i) => {
    const list = childrenOf.get(p) ?? []
    list.push(i)
    childrenOf.set(p, list)
  })
  childrenOf.forEach((list) => list.sort((a, b) => spans[a].loIndex - spans[b].loIndex))

  const ordered: MarkerSpan<M>[] = []
  function visit(i: number) {
    ordered.push(spans[i])
    for (const child of childrenOf.get(i) ?? []) visit(child)
  }
  for (const root of childrenOf.get(null) ?? []) visit(root)
  return ordered
}
