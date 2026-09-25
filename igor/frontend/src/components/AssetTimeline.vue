<script setup lang="ts">
import Menu from 'primevue/menu'
import type { MenuItem } from 'primevue/menuitem'
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
  /** Whether each one-asset edge adjustment is valid for the selected span. */
  markerResizeAvailability?: {
    extendStart: boolean
    shortenStart: boolean
    shortenEnd: boolean
    extendEnd: boolean
  } | null
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
  add: [index?: number]
  bootstrap: [index?: number]
  split: [payload: { index: number; target: HTMLElement }]
  tagRange: [range: { startIndex: number; endIndex: number }]
  editMarker: [eventId: number]
  resizeMarker: [payload: { eventId: number; edge: 'start' | 'end'; direction: -1 | 1 }]
  hoverMarker: [eventId: number | null]
}>()

// ── Add-asset "+" button: single asset vs bootstrap-from-files popup menu ──
// Two instances of the same two-item menu: `addMenuRef` for the fixed
// end-of-timeline button (always appends, no index), `insertMenuRef` shared
// by every inter-asset "+" joint button (see insertJoints below) -- each
// joint click records its own target index in `pendingInsertIndex` right
// before opening the popup, which the (fixed, non-reactive) menu commands
// read at click time.
const addMenuRef = ref<InstanceType<typeof Menu> | null>(null)
const addMenuItems: MenuItem[] = [
  { label: 'Add single asset', icon: 'pi pi-plus', command: () => emit('add') },
  { label: 'Add multiple assets...', icon: 'pi pi-list', command: () => emit('bootstrap') },
]
function toggleAddMenu(event: Event) {
  addMenuRef.value?.toggle(event)
}

const insertMenuRef = ref<InstanceType<typeof Menu> | null>(null)
const pendingInsertIndex = ref<number | null>(null)
const insertMenuItems: MenuItem[] = [
  { label: 'Insert single asset', icon: 'pi pi-plus', command: () => emit('add', pendingInsertIndex.value ?? undefined) },
  { label: 'Insert multiple assets...', icon: 'pi pi-list', command: () => emit('bootstrap', pendingInsertIndex.value ?? undefined) },
]
function toggleInsertMenu(event: Event, index: number) {
  pendingInsertIndex.value = index
  insertMenuRef.value?.toggle(event)
}

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

// ── Asset insertion "+" joints and per-asset split buttons ─────────────────
// One joint per gap BETWEEN two existing assets; the leading joint is
// rendered separately at position zero so a new asset can be inserted before
// the first existing one. The end-of-timeline button still appends assets.
// `index` is the position the new asset(s) will occupy once inserted.
const insertJoints = computed(() => {
  const segs = segments.value
  const total = totalSeconds.value
  const joints: { index: number; percent: number }[] = []
  for (let i = 0; i < segs.length - 1; i++) {
    joints.push({ index: i + 1, percent: (segs[i + 1].offsetSeconds / total) * 100 })
  }
  return joints
})

const splitPoints = computed(() => {
  const segs = segments.value
  const total = totalSeconds.value
  return segs.map((s, i) => ({ index: i, percent: ((s.offsetSeconds + s.seconds / 2) / total) * 100 }))
})

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
  // Keep Table 22's numeric order except for Provider/Distributor
  // Advertisement: visually place those below all Placement Opportunity
  // lanes, even though their type_ids (0x30/0x32) are numerically lower.
  // Other lane types retain their existing relative order; splice_insert
  // (no type_id) sorts last.
  function visualOrder(key: string): number {
    if (key === 'splice_insert') return Infinity
    const typeId = parseInt(key.split(':')[1], 16)
    if (typeId === 0x30) return 0x3A + 0.25
    if (typeId === 0x32) return 0x3A + 0.5
    return typeId
  }
  keys.sort((a, b) => {
    return visualOrder(a) - visualOrder(b)
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
// The append button lives OUTSIDE .timeline-scale (as a flex sibling of
// .timeline-viewport, see template) so it never eats into the percentage
// budget shared by the marker lanes/track/ruler and stays fixed while the
// scale scrolls under zoom. Insert buttons, including the leading button,
// belong to the scale's action row below the asset track.
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
                  <span
                    v-if="!span.instant"
                    class="timeline-marker-span-label"
                    :class="{ 'timeline-marker-span-label-selected': span.marker.event_id === selectedMarkerEventId }"
                  >#{{ span.marker.event_id }}</span>
                  <div
                    v-if="span.marker.event_id === selectedMarkerEventId && !span.instant && markerResizeAvailability"
                    class="timeline-marker-resize-controls"
                    @click.stop
                    @mouseenter="emit('hoverMarker', span.marker.event_id)"
                    @mouseleave="emit('hoverMarker', null)"
                  >
                    <div class="timeline-marker-resize-edge timeline-marker-resize-start">
                      <button
                        type="button"
                        class="timeline-marker-resize-btn"
                        title="Extend marker to previous asset"
                        :disabled="!markerResizeAvailability.extendStart"
                        @click.stop="emit('resizeMarker', { eventId: span.marker.event_id, edge: 'start', direction: -1 })"
                      ><i class="pi pi-angle-left" /></button>
                      <button
                        type="button"
                        class="timeline-marker-resize-btn"
                        title="Shorten marker from start"
                        :disabled="!markerResizeAvailability.shortenStart"
                        @click.stop="emit('resizeMarker', { eventId: span.marker.event_id, edge: 'start', direction: 1 })"
                      ><i class="pi pi-angle-right" /></button>
                    </div>
                    <div class="timeline-marker-resize-edge timeline-marker-resize-end">
                      <button
                        type="button"
                        class="timeline-marker-resize-btn"
                        title="Shorten marker from end"
                        :disabled="!markerResizeAvailability.shortenEnd"
                        @click.stop="emit('resizeMarker', { eventId: span.marker.event_id, edge: 'end', direction: -1 })"
                      ><i class="pi pi-angle-left" /></button>
                      <button
                        type="button"
                        class="timeline-marker-resize-btn"
                        title="Extend marker to next asset"
                        :disabled="!markerResizeAvailability.extendEnd"
                        @click.stop="emit('resizeMarker', { eventId: span.marker.event_id, edge: 'end', direction: 1 })"
                      ><i class="pi pi-angle-right" /></button>
                    </div>
                  </div>
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

          <!-- Insert-joint "+" buttons (between two existing assets) and
               per-asset split buttons -- both positioned by the same
               percent-of-total-duration math as the segments/ruler above,
               so they always land exactly on the boundary/midpoint they
               represent regardless of zoom. -->
          <div class="timeline-actions-row">
            <div class="timeline-insert-sub-row">
              <button
                v-if="segments.length"
                type="button"
                class="timeline-insert-btn timeline-leading-insert-btn"
                :style="{ left: '0%' }"
                title="Insert asset(s) before the first asset"
                @click="toggleInsertMenu($event, 0)"
              >
                <i class="pi pi-plus" />
              </button>
              <button
                v-for="joint in insertJoints"
                :key="'joint-' + joint.index"
                type="button"
                class="timeline-insert-btn"
                :style="{ left: joint.percent + '%' }"
                title="Insert asset(s) here"
                @click="toggleInsertMenu($event, joint.index)"
              >
                <i class="pi pi-plus" />
              </button>
            </div>
            <div class="timeline-split-sub-row">
              <button
                v-for="pt in splitPoints"
                :key="'split-' + pt.index"
                type="button"
                class="timeline-split-btn"
                :style="{ left: pt.percent + '%' }"
                title="Split this asset in two"
                @click="emit('split', { index: pt.index, target: $event.currentTarget as HTMLElement })"
              >
                <i class="pi pi-arrows-h" />
              </button>
            </div>
          </div>
          <Menu ref="insertMenuRef" :model="insertMenuItems" :popup="true" />

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
        @click="toggleAddMenu"
      >
        <i class="pi pi-plus" />
      </button>
      <Menu ref="addMenuRef" :model="addMenuItems" :popup="true" />
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
  box-sizing: border-box;
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

.timeline-actions-row {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.timeline-insert-sub-row,
.timeline-split-sub-row {
  position: relative;
  height: 1.2rem;
}

/* Same square/rounded/gray treatment as the end-of-timeline
   .timeline-add-segment button, just scaled down to fit between
   segments. */
.timeline-insert-btn,
.timeline-split-btn {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  width: 1.2rem;
  height: 1.2rem;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f1f5f9;
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  color: #475569;
  cursor: pointer;
  z-index: 2;
}

/* The first insertion point is at the viewport edge. Unlike centered
   * boundary buttons, it must not place half of itself outside the clipped
   * timeline viewport. */
.timeline-leading-insert-btn {
  transform: none;
}

.timeline-insert-btn:hover,
.timeline-split-btn:hover {
  background: #e2e8f0;
  color: #0f172a;
}

/* PrimeVue's base styles set `.pi { font-size: dt('icon.size') }` directly
 * on the icon itself (not inherited from an ancestor's font-size), so the
 * only way to size these glyphs smaller than that global default is to
 * override .pi here with higher selector specificity. */
.timeline-insert-btn .pi,
.timeline-split-btn .pi {
  font-size: 0.75rem;
}

.timeline-ruler {
  position: relative;
  height: 1.5rem;
  margin-top: 0.15rem;
}

/* width: 0 (not the default auto-sized flex column) is load-bearing: the
 * div's `left: X%` is the one true anchor for the tick mark, and an
 * auto-sized box would stretch to fit the label text, pulling the
 * *line* off its intended position (align-items:center would center the
 * 1px line inside a box as wide as e.g. "1:00", visibly offsetting it
 * from the asset/marker boundary it's meant to mark). Keeping this box
 * zero-width means the line renders exactly at the anchor regardless of
 * label length; the label is positioned independently below so it can
 * shift near the edges without dragging the line with it. */
.timeline-tick {
  position: absolute;
  top: 0;
  width: 0;
}

.timeline-tick-line {
  display: block;
  width: 1px;
  height: 0.4rem;
  background: #94a3b8;
  transform: translateX(-0.5px);
}

.timeline-tick-label {
  position: absolute;
  top: 0.5rem;
  left: 0;
  transform: translateX(-50%);
  font-size: 0.65rem;
  color: #64748b;
  white-space: nowrap;
}

/* First/last labels would overflow the ruler's edge if centered under
 * their (edge-anchored) tick line -- align to that edge instead. */
.timeline-tick:first-child .timeline-tick-label {
  left: 0;
  transform: translateX(0);
}

.timeline-tick:last-child .timeline-tick-label {
  left: auto;
  right: 0;
  transform: translateX(0.5px);
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

.timeline-marker-span-label-selected {
  padding-left: 1.6rem;
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
  overflow: visible;
}

.timeline-marker-resize-controls {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.timeline-marker-resize-edge {
  position: absolute;
  top: 50%;
  display: flex;
  transform: translateY(-50%);
  pointer-events: auto;
}

.timeline-marker-resize-start {
  left: -0.75rem;
}

.timeline-marker-resize-end {
  right: -0.75rem;
}

.timeline-marker-resize-btn {
  width: 0.8rem;
  height: 0.9rem;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f1f5f9;
  border: 1px solid #94a3b8;
  color: #334155;
  cursor: pointer;
}

.timeline-marker-resize-btn:first-child {
  border-radius: 4px 0 0 4px;
}

.timeline-marker-resize-btn:last-child {
  border-radius: 0 4px 4px 0;
}

.timeline-marker-resize-btn:hover:not(:disabled) {
  background: #e2e8f0;
  color: #0f172a;
}

.timeline-marker-resize-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.timeline-marker-resize-btn .pi {
  font-size: 0.65rem;
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
