<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { parseApproxSeconds } from '../utils/duration'
import { layoutMarkers, type MarkerLike, type MarkerSpan } from '../markerLayout'
import { colorForLaneKey, laneKeyForMarker, laneLabelForMarker } from '../segmentationPresets'

interface TimelineAsset {
  id: string
  file: string
  duration: string
}

const props = defineProps<{
  assets: TimelineAsset[]
  selectedIndex?: number | null
  markers?: MarkerLike[]
  /** Real per-asset durations (seconds) from a resolve-markers preview
   * (ffprobe'd), keyed by asset id -- overrides the parsed/fallback
   * duration used for layout so assets with no explicit `duration:` in
   * the YAML (very common for ads -- "use the whole file") still render
   * at their true width instead of the placeholder weight. */
  resolvedDurations?: Record<string, number>
  /** event_id of the marker currently selected (click-driven, persistent)
   * in the parent's marker list -- highlighted with a solid outline. */
  selectedMarkerEventId?: number | null
  /** event_id of the marker currently hovered (transient) in the parent's
   * marker list -- highlighted with a lighter dashed outline, visually
   * distinct from "selected" so both can be told apart if they differ. */
  hoveredMarkerEventId?: number | null
  /** Set (by the parent) whenever the marker list's hover/selection should
   * bring the corresponding span into view here -- e.g. when hovering a
   * list row for a marker currently scrolled out of sight under zoom. */
  scrollToMarkerEventId?: number | null
}>()
const emit = defineEmits<{
  select: [index: number]
  add: []
  tagRange: [range: { startIndex: number; endIndex: number }]
  editMarker: [eventId: number]
  hoverMarker: [eventId: number | null]
}>()

const FALLBACK_SECONDS = 15 // weight used for assets with no known/parseable duration

const segments = computed(() => {
  const durations = props.assets.map((a) => {
    const resolved = props.resolvedDurations?.[a.id]
    if (resolved !== undefined) return resolved
    return parseApproxSeconds(a.duration) ?? FALLBACK_SECONDS
  })
  const total = durations.reduce((sum, d) => sum + d, 0) || 1
  let offset = 0
  return props.assets.map((a, i) => {
    const seconds = durations[i]
    const resolved = props.resolvedDurations?.[a.id] !== undefined
    const seg = {
      asset: a,
      seconds,
      offsetSeconds: offset,
      percent: (seconds / total) * 100,
      approx: !resolved && parseApproxSeconds(a.duration) === null,
    }
    offset += seconds
    return seg
  })
})

const totalSeconds = computed(() => segments.value.reduce((sum, s) => sum + s.seconds, 0) || 1)
const hasApproxSegments = computed(() => segments.value.some((s) => s.approx))

/** "Nice" round numbers to space ruler ticks at, in seconds. */
const NICE_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]

const tickStepSeconds = computed(() => {
  const total = totalSeconds.value
  // Aim for roughly 5-10 ticks across the timeline.
  const target = total / 7
  return NICE_STEPS.find((step) => step >= target) ?? NICE_STEPS[NICE_STEPS.length - 1]
})

const ticks = computed(() => {
  const step = tickStepSeconds.value
  const total = totalSeconds.value
  const result: { seconds: number; percent: number }[] = []
  for (let t = 0; t <= total + 0.001; t += step) {
    result.push({ seconds: t, percent: (t / total) * 100 })
  }
  return result
})

function formatDuration(totalSecondsValue: number): string {
  const s = Math.round(totalSecondsValue)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  if (m > 0) return `${m}:${String(sec).padStart(2, '0')}`
  return `${sec}s`
}

function fileLabel(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

// ── Horizontal zoom + pan ───────────────────────────────────────────────────
// Ads are typically a small fraction of the total duration, so at 1x they're
// slivers a few pixels wide. Zoom widens the "scale" beyond 100% inside a
// horizontally-scrolling viewport; the add-asset button stays outside the
// viewport (fixed, never scrolls away).
const ZOOM_STEPS = [1, 1.5, 2, 3, 4, 6, 8, 12, 16]
const zoomIndex = ref(0)
const zoom = computed(() => ZOOM_STEPS[zoomIndex.value])

function zoomIn() {
  zoomIndex.value = Math.min(zoomIndex.value + 1, ZOOM_STEPS.length - 1)
}
function zoomOut() {
  zoomIndex.value = Math.max(zoomIndex.value - 1, 0)
}
function zoomReset() {
  zoomIndex.value = 0
}

// ── Contiguous-range selection (shift-click) for tagging markers ──────────
// Independent of `selectedIndex` (single-asset edit focus, owned by the
// parent). A range anchor is set on plain click, extended on shift-click.
const rangeAnchor = ref<number | null>(null)
const rangeEnd = ref<number | null>(null)

const selectedRange = computed<[number, number] | null>(() => {
  if (rangeAnchor.value === null || rangeEnd.value === null) return null
  return [Math.min(rangeAnchor.value, rangeEnd.value), Math.max(rangeAnchor.value, rangeEnd.value)]
})

function handleSegmentClick(i: number, shiftKey: boolean) {
  if (shiftKey && rangeAnchor.value !== null) {
    rangeEnd.value = i
  } else {
    rangeAnchor.value = i
    rangeEnd.value = i
  }
  emit('select', i)
}

function clearRange() {
  rangeAnchor.value = null
  rangeEnd.value = null
}

function tagRange() {
  if (!selectedRange.value) return
  emit('tagRange', { startIndex: selectedRange.value[0], endIndex: selectedRange.value[1] })
  clearRange()
}

function isInRange(i: number): boolean {
  const r = selectedRange.value
  return r !== null && i >= r[0] && i <= r[1]
}

// ── Marker lanes (one row PER TYPE, e.g. all "ad" markers share a row so
// same-kind markers always align, regardless of nesting depth) ────────────
const assetIdToIndex = computed(() => {
  const m = new Map<string, number>()
  props.assets.forEach((a, i) => m.set(a.id, i))
  return m
})

const markerLanes = computed<{ key: string; label: string; spans: MarkerSpan[] }[]>(() => {
  if (!props.markers || props.markers.length === 0) return []
  const spans = layoutMarkers(props.markers, assetIdToIndex.value)
  const keys = Array.from(new Set(spans.map((s) => laneKeyForMarker(s.marker))))
  // Sort by the underlying type_id (numerically) so related lanes (Break,
  // then Placement Opportunities, then Advertisements, ...) land in Table
  // 22 order; splice_insert (no type_id) sorts last.
  keys.sort((a, b) => {
    const na = a === 'splice_insert' ? Infinity : parseInt(a.split(':')[1], 16)
    const nb = b === 'splice_insert' ? Infinity : parseInt(b.split(':')[1], 16)
    return na - nb
  })
  return keys.map((key) => {
    const laneSpans = spans.filter((s) => laneKeyForMarker(s.marker) === key)
    return { key, label: laneLabelForMarker(laneSpans[0].marker), spans: laneSpans }
  })
})

function spanStyle(span: MarkerSpan) {
  const segs = segments.value
  if (span.loIndex < 0 || span.hiIndex < 0 || !segs[span.loIndex] || !segs[span.hiIndex]) {
    return { display: 'none' }
  }
  const startPercent = (segs[span.loIndex].offsetSeconds / totalSeconds.value) * 100
  if (span.instant) {
    // Standalone/instant signal (no defined End partner) -- a single point
    // at the span's start, not a bar spanning start->end.
    return { left: `calc(${startPercent}% - 0.45rem)`, width: '0.9rem' }
  }
  const endSeg = segs[span.hiIndex]
  const endPercent = ((endSeg.offsetSeconds + endSeg.seconds) / totalSeconds.value) * 100
  return { left: startPercent + '%', width: Math.max(endPercent - startPercent, 0.5) + '%' }
}

/** Vertical guide lines at every marker boundary (start + end of every
 * span, deduped), spanning the full height of the scale, so it's easy to
 * visually confirm a marker lines up with the asset boundary it should. */
const boundaryPercents = computed(() => {
  if (!props.markers || props.markers.length === 0) return []
  const segs = segments.value
  const idToIndex = assetIdToIndex.value
  const spans = layoutMarkers(props.markers, idToIndex)
  const percents = new Set<number>()
  for (const span of spans) {
    if (span.loIndex < 0 || span.hiIndex < 0 || !segs[span.loIndex] || !segs[span.hiIndex]) continue
    percents.add((segs[span.loIndex].offsetSeconds / totalSeconds.value) * 100)
    const endSeg = segs[span.hiIndex]
    percents.add(((endSeg.offsetSeconds + endSeg.seconds) / totalSeconds.value) * 100)
  }
  return Array.from(percents)
})

// ── Add-asset button vertical alignment ────────────────────────────────────
// The button lives OUTSIDE .timeline-scale (a flex sibling of
// .timeline-viewport, see template) so it (a) never eats into the
// percentage budget the marker lanes/track/ruler assume is 100% of the
// scale -- adding it as a real row member there desyncs segment widths from
// the marker lanes/ruler above/below, since they'd stop agreeing on what
// 100% means -- and (b) stays put while the scale scrolls under zoom.
// Because it's outside, it can't line up with .timeline-track (whose
// vertical offset varies with the marker lane count) via percentage/flex
// math either -- so its position is measured directly off the real DOM
// node instead of duplicated as CSS constants that would drift out of sync.
// A ResizeObserver (not a markerLanes watch) drives the remeasure: this
// component can mount while its tab is inactive (PrimeVue keeps inactive
// TabPanels in the DOM, just display:none'd) -- offsetTop reads 0 in that
// state, and only a genuine size change (e.g. the tab becoming visible,
// not just a marker-lanes count change) is guaranteed to catch that.
const scaleRowEl = ref<HTMLElement | null>(null)
const trackEl = ref<HTMLElement | null>(null)
const trackOffsetTop = ref(0)

function measureTrackOffset() {
  if (trackEl.value) trackOffsetTop.value = trackEl.value.offsetTop
}

let trackResizeObserver: ResizeObserver | null = null
onMounted(() => {
  measureTrackOffset()
  trackResizeObserver = new ResizeObserver(measureTrackOffset)
  if (scaleRowEl.value) trackResizeObserver.observe(scaleRowEl.value)
})
onBeforeUnmount(() => trackResizeObserver?.disconnect())

// ── Cross-highlight with the parent's marker list (hover + click) ─────────
// The parent owns the actual hover/selection state (it also needs to drive
// the list side); this component only renders the highlight classes and,
// when told to via `scrollToMarkerEventId`, scrolls the matching span into
// view -- needed once zoom hides most of the timeline off-screen.
const markerSpanEls = new Map<number, HTMLElement>()
function setMarkerSpanRef(eventId: number, el: unknown) {
  if (el instanceof HTMLElement) markerSpanEls.set(eventId, el)
  else markerSpanEls.delete(eventId)
}

watch(
  () => props.scrollToMarkerEventId,
  (eventId) => {
    if (eventId === null || eventId === undefined) return
    markerSpanEls.get(eventId)?.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
  },
)
</script>


<template>
  <div class="timeline">
    <div class="timeline-header">
      <span class="font-semibold">Total duration: {{ formatDuration(totalSeconds) }}</span>
      <span v-if="hasApproxSegments" class="text-color-secondary text-sm">(approximate -- see ~ below)</span>
      <slot name="header-actions" />
      <span class="flex-1" />
      <div class="zoom-controls">
        <button type="button" class="zoom-btn" title="Zoom out" :disabled="zoomIndex === 0" @click="zoomOut">
          <i class="pi pi-minus" />
        </button>
        <span class="zoom-label">{{ zoom }}x</span>
        <button
          type="button"
          class="zoom-btn"
          title="Zoom in"
          :disabled="zoomIndex === ZOOM_STEPS.length - 1"
          @click="zoomIn"
        >
          <i class="pi pi-plus" />
        </button>
        <button v-if="zoomIndex !== 0" type="button" class="zoom-btn zoom-reset-btn" title="Reset zoom" @click="zoomReset">
          <i class="pi pi-refresh" />
        </button>
      </div>
    </div>

    <div class="range-toolbar">
      <span class="text-sm text-color-secondary">
        <template v-if="selectedRange">
          {{ selectedRange[1] - selectedRange[0] + 1 }} asset(s) selected (shift-click another asset to extend the range)
        </template>
        <template v-else>Click an asset, then shift-click another to select a range, and add it as a marker.</template>
      </span>
      <span class="flex-1" />
      <button type="button" class="range-tag-btn" :disabled="!selectedRange" @click="tagRange">
        <i class="pi pi-tag" /> Add marker
      </button>
      <button v-if="selectedRange" type="button" class="range-clear-btn" title="Unselect all" @click="clearRange">
        <i class="pi pi-times" /> Unselect all
      </button>
    </div>

    <!-- Everything inside .timeline-scale shares the exact same rendered
         width (the "scale"), so percentage-based positioning stays
         consistent across rows. The add-asset button lives OUTSIDE the
         scrolling viewport (see script comment on trackOffsetTop for why),
         so it never eats into that percentage budget and stays put while
         the scale scrolls under zoom. -->
    <div ref="scaleRowEl" class="timeline-scale-row">
      <div class="timeline-viewport">
        <div class="timeline-scale" :style="{ width: zoom * 100 + '%' }">
          <!-- Vertical guide lines at every marker boundary, spanning the
               full height of the scale -- lets you visually confirm a
               marker's edge lines up with the asset boundary it's supposed
               to sit on. -->
          <div
            v-for="(p, i) in boundaryPercents"
            :key="'guide' + i"
            class="timeline-boundary-guide"
            :style="{ left: p + '%' }"
          />

          <!-- Marker lanes ABOVE the asset track: these are what you're
               usually scanning for, so they get top billing. One lane per
               (splice_type, segmentation.type_id) -- e.g. every "Break"
               marker shares a lane, every "Provider Placement Opportunity"
               marker shares a different one. -->
          <div v-if="markerLanes.length" class="timeline-marker-lanes">
            <div v-for="lane in markerLanes" :key="lane.key" class="timeline-marker-lane">
              <span class="timeline-marker-lane-label">{{ lane.label }}</span>
              <div class="timeline-marker-lane-track">
                <div
                  v-for="span in lane.spans"
                  :key="span.marker.event_id"
                  :ref="(el) => setMarkerSpanRef(span.marker.event_id, el)"
                  class="timeline-marker-span"
                  :class="{
                    'timeline-marker-span-instant': span.instant,
                    'timeline-marker-span-selected': span.marker.event_id === selectedMarkerEventId,
                    'timeline-marker-span-hovered': span.marker.event_id === hoveredMarkerEventId,
                  }"
                  :style="{ ...spanStyle(span), background: colorForLaneKey(lane.key) }"
                  :title="`${lane.label} #${span.marker.event_id}${span.instant ? ' (instant)' : ` (segment ${span.segmentNum + 1} of ${span.segmentsExpected})`} -- depth ${span.depth}`"
                  role="button"
                  tabindex="0"
                  @click="emit('editMarker', span.marker.event_id)"
                  @mouseenter="emit('hoverMarker', span.marker.event_id)"
                  @mouseleave="emit('hoverMarker', null)"
                >
                  <span v-if="!span.instant" class="timeline-marker-span-label">#{{ span.marker.event_id }}</span>
                </div>
              </div>
            </div>
          </div>

          <div ref="trackEl" class="timeline-track">
            <div
              v-for="(seg, i) in segments"
              :key="i"
              class="timeline-segment"
              :class="{
                'timeline-segment-selected': i === selectedIndex,
                'timeline-segment-in-range': isInRange(i),
              }"
              :style="{ width: seg.percent + '%' }"
              :title="`${seg.asset.id} (${fileLabel(seg.asset.file)}) -- ${formatDuration(seg.seconds)}${seg.approx ? ' (approximate)' : ''} -- click to edit, shift-click to select a range`"
              role="button"
              tabindex="0"
              @click="handleSegmentClick(i, $event.shiftKey)"
              @keydown.enter="handleSegmentClick(i, false)"
            >
              <span class="timeline-segment-label">{{ seg.asset.id || fileLabel(seg.asset.file) || `Asset ${i + 1}` }}</span>
              <span class="timeline-segment-duration">{{ formatDuration(seg.seconds) }}<span v-if="seg.approx">~</span></span>
            </div>
          </div>

          <div class="timeline-ruler">
            <div
              v-for="(tick, i) in ticks"
              :key="i"
              class="timeline-tick"
              :style="{ left: tick.percent + '%' }"
            >
              <span class="timeline-tick-line" />
              <span class="timeline-tick-label">{{ formatDuration(tick.seconds) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- margin-top mirrors .timeline-track's measured offset (see
           trackOffsetTop in the script) so this lines up with the asset
           row itself, not the top of the marker lanes above it. -->
      <button
        type="button"
        class="timeline-add-segment"
        title="Add asset"
        :style="{ marginTop: trackOffsetTop + 'px' }"
        @click="emit('add')"
      >
        <i class="pi pi-plus" />
      </button>
    </div>

    <div class="timeline-legend">
      <span class="timeline-legend-item text-color-secondary">~ = duration not resolved -- placeholder weight ({{ formatDuration(FALLBACK_SECONDS) }}); click "Resolve" to ffprobe real durations</span>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.timeline-header {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.timeline-scale-row {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
}

.timeline-viewport {
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: auto;
  overflow-y: hidden;
  /* Leaves room so segment/marker labels aren't clipped by the scrollbar. */
  padding-bottom: 0.15rem;
}

.timeline-scale {
  min-width: 100%;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  position: relative;
}

.zoom-controls {
  display: flex;
  align-items: center;
  gap: 0.25rem;
}

.zoom-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 5px;
  border: 1px solid #94a3b8;
  background: #fff;
  color: #0f172a;
  cursor: pointer;
  font-size: 0.7rem;
}

.zoom-btn:hover:not(:disabled) {
  background: #e2e8f0;
}

.zoom-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.zoom-reset-btn {
  color: #7c3aed;
  border-color: #c4b5fd;
}

.zoom-label {
  font-size: 0.7rem;
  color: #475569;
  min-width: 2rem;
  text-align: center;
}

/* Spans the full height of the scale at a marker boundary's x position, so
 * alignment with the asset track above/ruler below is visually checkable. */
.timeline-boundary-guide {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: rgba(124, 58, 237, 0.35);
  pointer-events: none;
  z-index: 1;
}

.timeline-track {
  display: flex;
  height: 2.75rem;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #cbd5e1;
}

.timeline-segment {
  background: #38bdf8;
  border-right: 1px solid rgba(255, 255, 255, 0.6);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-width: 2px;
  overflow: hidden;
  position: relative;
  cursor: pointer;
  transition: filter 0.1s ease;
}

.timeline-segment:hover {
  filter: brightness(0.92);
}

.timeline-segment:last-child {
  border-right: none;
}

.timeline-segment-selected {
  outline: 3px solid #0f172a;
  outline-offset: -3px;
  z-index: 1;
}

/* Its own row, outside .timeline-scale -- see trackOffsetTop in the script
 * for why (must not eat into the percentage budget segments/markers share)
 * and how its margin-top keeps it level with .timeline-track despite that. */
.timeline-add-segment {
  flex: 0 0 2.75rem;
  width: 2.75rem;
  height: 2.75rem;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f1f5f9;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  color: #475569;
  cursor: pointer;
}

.timeline-add-segment:hover {
  background: #e2e8f0;
  color: #0f172a;
}

.timeline-segment-label {
  font-size: 0.7rem;
  color: #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 0.35rem;
  max-width: 100%;
}

.timeline-segment-duration {
  font-size: 0.65rem;
  color: #0f172acc;
  white-space: nowrap;
}

.timeline-ruler {
  position: relative;
  height: 1.5rem;
  margin-top: 0.15rem;
}

.timeline-tick {
  position: absolute;
  top: 0;
  transform: translateX(-1px);
  display: flex;
  flex-direction: column;
  align-items: center;
}

/* Last tick's label would overflow the container to the right -- anchor
 * it to the right edge of the tick mark instead of centering. */
.timeline-tick:last-child {
  transform: translateX(-100%);
}

.timeline-tick-line {
  width: 1px;
  height: 0.4rem;
  background: #94a3b8;
}

.timeline-tick-label {
  font-size: 0.65rem;
  color: #64748b;
  white-space: nowrap;
  margin-top: 0.1rem;
}

.timeline-legend {
  display: flex;
  gap: 1.25rem;
  font-size: 0.75rem;
  flex-wrap: wrap;
}

.timeline-legend-item {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.range-toolbar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.range-tag-btn,
.range-clear-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  border-radius: 5px;
  border: 1px solid #94a3b8;
  background: #fff;
  color: #0f172a;
  padding: 0.2rem 0.5rem;
  cursor: pointer;
}

.range-tag-btn:hover:not(:disabled) {
  background: #e2e8f0;
}

.range-tag-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.range-clear-btn {
  padding: 0.2rem 0.4rem;
}

.timeline-segment.timeline-segment-in-range {
  background: #1e3a8a;
}

.timeline-segment-in-range .timeline-segment-label,
.timeline-segment-in-range .timeline-segment-duration {
  color: #fff;
}

.timeline-marker-lanes {
  display: flex;
  flex-direction: column;
}

.timeline-marker-lane {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  padding: 0.3rem 0 0.15rem;
  border-top: 1px solid #eef0f3;
}

.timeline-marker-lane:first-child {
  border-top: none;
  padding-top: 0;
}

.timeline-marker-lane-label {
  font-size: 0.55rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.02em;
  color: #94a3b8;
}

.timeline-marker-lane-track {
  position: relative;
  width: 100%;
  height: 1.1rem;
}

.timeline-marker-span {
  position: absolute;
  top: 0;
  height: 1.1rem;
  border-radius: 4px;
  display: flex;
  align-items: center;
  overflow: hidden;
  cursor: pointer;
  border: 1px solid rgba(15, 23, 42, 0.25);
}

.timeline-marker-span-label {
  font-size: 0.6rem;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
  padding: 0 0.3rem;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Standalone/instant signal (no End partner): a small diamond "pin" at the
 * span's start rather than a bar spanning start->end. */
.timeline-marker-span-instant {
  border-radius: 3px;
  transform: rotate(45deg);
  top: 0.1rem;
  height: 0.9rem;
}

/* Selected (persistent, click-driven from the marker list) -- solid dark
 * outline, same visual language as .timeline-segment-selected for assets. */
.timeline-marker-span-selected {
  outline: 2px solid #0f172a;
  outline-offset: 1px;
  z-index: 2;
}

/* Hovered (transient, from either the graph or the marker list) -- a
 * lighter dashed outline + glow, deliberately different from "selected"
 * so the two states are never confused when they don't coincide. */
.timeline-marker-span-hovered {
  outline: 2px dashed #ffffff;
  outline-offset: 1px;
  box-shadow: 0 0 0 3px rgba(15, 23, 42, 0.35);
  z-index: 3;
}
</style>
