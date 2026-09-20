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
  event_id: number
  assets: string[]
  splice_type?: 'splice_insert' | 'time_signal'
  segmentation?: { type_id: string }
}

export interface MarkerSpan<M extends MarkerLike = MarkerLike> {
  marker: M
  loIndex: number
  hiIndex: number
  depth: number
  segmentNum: number
  segmentsExpected: number
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

  const siblingsByParent = new Map<number | null, number[]>()
  parentOf.forEach((p, i) => {
    const list = siblingsByParent.get(p) ?? []
    list.push(i)
    siblingsByParent.set(p, list)
  })
  siblingsByParent.forEach((list) => list.sort((a, b) => spans[a].loIndex - spans[b].loIndex))

  const segmentNum = spans.map(() => 0)
  const segmentsExpected = spans.map(() => 1)
  siblingsByParent.forEach((list) => {
    list.forEach((i, position) => {
      segmentNum[i] = position
      segmentsExpected[i] = list.length
    })
  })

  return spans.map((span, i) => ({
    marker: span.marker,
    loIndex: span.loIndex,
    hiIndex: span.hiIndex,
    depth: depthOf[i],
    segmentNum: segmentNum[i],
    segmentsExpected: segmentsExpected[i],
    instant: isInstantMarker(span.marker),
  }))
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
