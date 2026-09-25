/**
 * Client-side mirror of franken_ts.timeline.resolve_markers' containment
 * logic (asset-index based, not seconds) -- used to lay markers out into
 * stacked "lanes" in the editor timeline and to report Table 23
 * hierarchy/overlap violations for immediate UI feedback while editing
 * (including mid-drag/resize, so it has to run synchronously, client-side).
 *
 * This is a UI convenience only: the source of truth for validation
 * (contiguity, disjoint/nested-only overlap) and for real frame-accurate
 * seconds is still franken-ts itself, via `Config.model_validate` on save
 * and `POST /resolve-markers` for a ffprobe'd preview. If this ever
 * disagrees with the backend, the backend wins.
 *
 * The numbering *values* (segment_num/segments_expected/sub_segment_*)
 * are NOT computed here: unlike the hierarchy check, they're only ever
 * displayed (never used to gate a drag/resize gesture), so
 * PlaylistEditor.vue fetches them from `POST /playlists/validate-markers`
 * (debounced) and passes them into `layoutMarkers` as `numberingByEventId`
 * -- see that function's docstring. This used to be a second,
 * ~135-line hand-kept-in-sync reimplementation of franken-ts's three
 * SCTE-35 numbering profiles; see SCTE35_MARKER_RULES.md.
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

/** One marker's numbering fields, as computed server-side by
 * `POST /playlists/validate-markers` -- see `layoutMarkers`'s
 * `numberingByEventId` parameter. */
export interface NumberingValues {
  segmentNum: number
  segmentsExpected: number
  subSegmentNum: number | null
  subSegmentsExpected: number | null
}

/** Resolve each marker's asset-index span and containment depth (0 =
 * top-level, i.e. no other marker strictly contains it) -- same
 * "immediate parent = smallest strictly containing span" rule as the
 * Python implementation.
 *
 * `numberingByEventId`, when `strict` (enforcement on), supplies the
 * segment_num/segments_expected/sub_segment_* to report for each span --
 * fetched from `POST /playlists/validate-markers` (see
 * PlaylistEditor.vue's `scheduleNumberingRefresh`), not computed here.
 * A marker missing from the map (nothing fetched yet, or the current
 * state doesn't validate) reports 0/0/null/null, same as the loading-state
 * fallback this replaced. With `strict` false, authored values pass
 * through unchanged, as before. */
export function layoutMarkers<M extends MarkerLike>(
  markers: M[],
  assetIdToIndex: Map<string, number>,
  strict = true,
  numberingByEventId: ReadonlyMap<number, NumberingValues> = new Map(),
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

  return spans.map((span, i) => {
    const authoritative = strict ? numberingByEventId.get(span.marker.event_id) : undefined
    return {
      marker: span.marker,
      markerIndex: i,
      loIndex: span.loIndex,
      hiIndex: span.hiIndex,
      depth: depthOf[i],
      segmentNum: authoritative?.segmentNum ?? (strict ? 0 : span.marker.segmentation?.segment_num ?? 0),
      segmentsExpected: authoritative?.segmentsExpected ?? (strict ? 0 : span.marker.segmentation?.segments_expected ?? 0),
      subSegmentNum: authoritative ? authoritative.subSegmentNum : (strict ? null : span.marker.segmentation?.sub_segment_num ?? null),
      subSegmentsExpected: authoritative ? authoritative.subSegmentsExpected : (strict ? null : span.marker.segmentation?.sub_segments_expected ?? null),
      instant: isInstantMarker(span.marker),
    }
  })
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
