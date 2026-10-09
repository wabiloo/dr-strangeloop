<script setup lang="ts">
import Button from 'primevue/button'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { channelDocsUrl, getChannelTimeline } from '../api/client'
import type { ChannelTimeline } from '../api/types'
import FieldHelp from './FieldHelp.vue'
import JsonViewer from './JsonViewer.vue'
import { colorForLaneKey, laneKeyForMarker, laneLabelForMarker } from '../segmentationPresets'

const props = defineProps<{
  name: string
  /** Raw timeshift query string ("start=...&end=...") when a startover/catchup
   * preview is active; empty for the live window. */
  query?: string
}>()

const STORAGE_KEY = 'igor.windowPanel.open'
const POLL_MS = 3000

function loadOpen(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
const open = ref(loadOpen())
function toggle() {
  open.value = !open.value
  try {
    localStorage.setItem(STORAGE_KEY, open.value ? '1' : '0')
  } catch {
    // per-browser convenience only
  }
}

function openDocs() {
  window.open(channelDocsUrl(props.name), '_blank', 'noopener')
}

function toggleRaw() {
  if (!open.value) {
    toggle()
    showRaw.value = true
  } else {
    showRaw.value = !showRaw.value
  }
}

const doc = ref<ChannelTimeline | null>(null)
const error = ref('')
const showRaw = ref(false)
type Selection = { kind: string; label: string; data: unknown }
const selected = ref<Selection | null>(null)

// Animated view state (see "geometry" below); declared before the immediate watch uses it.
type View = { t0: number; t1: number }
const TWEEN_MS = 450
const view = ref<View | null>(null)
const ghost = ref<ChannelTimeline | null>(null)
const liveNow = ref(0)
let tween: { from: View; to: View; start: number } | null = null
let fetchedAt = 0
let raf = 0
const altCache = new Map<string, boolean>()
// User zoom/pan: an absolute sub-range of the (animated) full range; null = whole range.
const vp = ref<View | null>(null)
const overlayEl = ref<HTMLElement | null>(null)

let timer: ReturnType<typeof setInterval> | null = null
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  try {
    const next = await getChannelTimeline(props.name, props.query ?? '')
    if (seq !== requestSeq) return // a newer request (e.g. query changed) superseded this one
    applyDoc(next)
    error.value = ''
  } catch (e) {
    if (seq !== requestSeq) return
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function restartPolling() {
  if (timer) clearInterval(timer)
  timer = null
  if (!open.value) return
  load()
  // An ended timeshift range is immutable; only the live window moves.
  if (!props.query) timer = setInterval(load, POLL_MS)
}
watch([open, () => props.name, () => props.query], () => {
  doc.value = null
  view.value = null
  ghost.value = null
  tween = null
  vp.value = null
  altCache.clear()
  selected.value = null
  restartPolling()
}, { immediate: true })
onBeforeUnmount(() => {
  requestSeq++
  cancelAnimationFrame(raf)
  if (timer) clearInterval(timer)
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
})

// ── geometry ────────────────────────────────────────────────────────────────
// Positions are percentages of an animated `view`. When the range moves (a loop
// boundary was crossed), the view slides from the old range to the new one while
// the previous document's content stays rendered (`ghost`), so nothing pops in
// or out mid-slide.

const rangeOf = (d: ChannelTimeline): View => ({ t0: Date.parse(d.range.start_utc), t1: Date.parse(d.range.end_utc) })

function applyDoc(next: ChannelTimeline) {
  const prev = doc.value
  const to = rangeOf(next)
  doc.value = next
  fetchedAt = performance.now()
  if (!prev || !view.value) {
    view.value = to
    ghost.value = null
    tween = null
  } else if (prev.range.start_utc !== next.range.start_utc || prev.range.end_utc !== next.range.end_utc) {
    ghost.value = prev
    tween = { from: { ...view.value }, to, start: performance.now() }
  } else if (!tween) {
    view.value = to
  }
}

function frame() {
  raf = requestAnimationFrame(frame)
  const now = performance.now()
  if (tween) {
    const p = Math.min(1, (now - tween.start) / TWEEN_MS)
    const e = 1 - (1 - p) ** 3
    view.value = {
      t0: tween.from.t0 + (tween.to.t0 - tween.from.t0) * e,
      t1: tween.from.t1 + (tween.to.t1 - tween.from.t1) * e,
    }
    if (p >= 1) {
      tween = null
      ghost.value = null
    }
  }
  const d = doc.value
  if (d && d.mode === 'live') liveNow.value = Date.parse(d.generated_at) + (now - fetchedAt)
}
onMounted(() => {
  raf = requestAnimationFrame(frame)
})

const fullT0 = computed(() => view.value?.t0 ?? (doc.value ? rangeOf(doc.value).t0 : 0))
const fullT1 = computed(() => view.value?.t1 ?? (doc.value ? rangeOf(doc.value).t1 : 1))
// What is actually drawn: the whole range, or the zoomed sub-range kept inside it.
const viewport = computed<View>(() => {
  const a = fullT0.value
  const b = fullT1.value
  const v = vp.value
  if (!v) return { t0: a, t1: b }
  const s = Math.min(v.t1 - v.t0, b - a)
  const s0 = Math.min(Math.max(v.t0, a), b - s)
  return { t0: s0, t1: s0 + s }
})
const t0 = computed(() => viewport.value.t0)
const t1 = computed(() => viewport.value.t1)
const span = computed(() => Math.max(1, t1.value - t0.value))

// ── zoom / pan ──────────────────────────────────────────────────────────────
const MIN_SPAN_MS = 2000
const zoomed = computed(() => vp.value !== null)
const zoomFactor = computed(() => (fullT1.value - fullT0.value) / Math.max(1, t1.value - t0.value))

function setViewport(a: number, b: number) {
  const fa = fullT0.value
  const fb = fullT1.value
  const s = Math.min(Math.max(b - a, MIN_SPAN_MS), fb - fa)
  const s0 = Math.min(Math.max(a, fa), fb - s)
  vp.value = s >= fb - fa - 1 ? null : { t0: s0, t1: s0 + s }
}
function zoomAt(factor: number, frac = 0.5) {
  const { t0: a, t1: b } = viewport.value
  const s = b - a
  const anchor = a + s * frac
  const ns = s * factor
  setViewport(anchor - ns * frac, anchor + ns * (1 - frac))
}
function panBy(frac: number) {
  const { t0: a, t1: b } = viewport.value
  const d = (b - a) * frac
  setViewport(a + d, b + d)
}
function fracOf(clientX: number): { frac: number; width: number } {
  const r = overlayEl.value?.getBoundingClientRect()
  if (!r || !r.width) return { frac: 0.5, width: 1 }
  return { frac: Math.min(1, Math.max(0, (clientX - r.left) / r.width)), width: r.width }
}
function onWheel(e: WheelEvent) {
  const { frac, width } = fracOf(e.clientX)
  if (e.ctrlKey || e.metaKey) {
    e.preventDefault()
    zoomAt(Math.exp(Math.max(-100, Math.min(100, e.deltaY)) * 0.005), frac)
  } else if (zoomed.value && (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY))) {
    e.preventDefault()
    panBy((e.deltaX || e.deltaY) / width)
  }
}
let drag: { x: number; a: number; b: number; width: number; moved: boolean } | null = null
let suppressClick = false
function onDown(e: PointerEvent) {
  if (!zoomed.value || e.button !== 0) return
  drag = { x: e.clientX, a: viewport.value.t0, b: viewport.value.t1, width: fracOf(e.clientX).width, moved: false }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}
function onMove(e: PointerEvent) {
  if (!drag) return
  const dx = e.clientX - drag.x
  if (Math.abs(dx) > 3) drag.moved = true
  if (drag.moved) {
    const d = -(dx / drag.width) * (drag.b - drag.a)
    setViewport(drag.a + d, drag.b + d)
  }
}
function onUp() {
  suppressClick = drag?.moved ?? false
  drag = null
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  setTimeout(() => (suppressClick = false), 0)
}
function onClickCapture(e: MouseEvent) {
  if (suppressClick) {
    e.stopPropagation()
    e.preventDefault()
  }
}
// Scrollbar position (0..1000) of the viewport inside the full range.
const scrollPos = computed(() => {
  const room = fullT1.value - fullT0.value - (t1.value - t0.value)
  return room > 0 ? Math.round(((t0.value - fullT0.value) / room) * 1000) : 0
})
function onScroll(e: Event) {
  const room = fullT1.value - fullT0.value - (t1.value - t0.value)
  const a = fullT0.value + (Number((e.target as HTMLInputElement).value) / 1000) * room
  setViewport(a, a + (t1.value - t0.value))
}

function pctMs(ms: number): number {
  return ((ms - t0.value) / span.value) * 100
}
function pct(iso: string): number {
  return pctMs(Date.parse(iso))
}
function box(startIso: string, endIso: string | null): { left: string; width: string } {
  const a = Math.min(100, Math.max(0, pct(startIso)))
  const b = endIso ? Math.min(100, Math.max(0, pct(endIso))) : a
  return { left: `${a}%`, width: `${Math.max(b - a, 0)}%` }
}
function at(iso: string): { left: string } {
  return { left: `${Math.min(100, Math.max(0, pct(iso)))}%` }
}
// Whether any part of [startIso, endIso] (or the instant startIso) is in view.
function inView(startIso: string, endIso: string | null): boolean {
  const a = pct(startIso)
  if (!endIso) return a >= 0 && a <= 100
  return pct(endIso) > 0 && a < 100
}

// Current document plus (during a slide) the previous one, de-duplicated by key;
// the newer document wins.
function merged<T>(pick: (d: ChannelTimeline) => T[], key: (x: T) => string, startOf: (x: T) => string): T[] {
  const m = new Map<string, T>()
  for (const d of [ghost.value, doc.value]) {
    if (d) for (const x of pick(d)) m.set(key(x), x)
  }
  return [...m.values()].sort((x, y) => startOf(x).localeCompare(startOf(y)))
}

// Alternating shades are fixed the first time an item is seen, so they do not
// flip when items leave the list and shift the indices.
function withAlt<T>(ns: string, items: T[], key: (x: T) => string): { item: T; alt: boolean }[] {
  const live = new Set<string>()
  let prev = true
  const out = items.map((item) => {
    const k = `${ns}:${key(item)}`
    live.add(k)
    let alt = altCache.get(k)
    if (alt === undefined) {
      alt = !prev
      altCache.set(k, alt)
    }
    prev = alt
    return { item, alt }
  })
  for (const k of [...altCache.keys()]) if (k.startsWith(`${ns}:`) && !live.has(k)) altCache.delete(k)
  return out
}

const NICE_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400]
const ticks = computed(() => {
  if (!doc.value) return []
  const totalS = span.value / 1000
  const step = NICE_STEPS.find((s) => s >= totalS / 6) ?? NICE_STEPS[NICE_STEPS.length - 1]
  const out: { left: string; label: string }[] = []
  for (let t = Math.ceil(t0.value / 1000 / step) * step; t * 1000 <= t1.value; t += step) {
    out.push({
      left: `${((t * 1000 - t0.value) / span.value) * 100}%`,
      label: new Date(t * 1000).toISOString().slice(step >= 86400 ? 5 : 11, step >= 60 ? 16 : 19),
    })
  }
  return out
})

const periodRows = computed(() =>
  withAlt('p', merged((d) => d.periods, (p) => p.id + p.start_utc, (p) => p.start_utc), (p) => p.id + p.start_utc)
    .filter(({ item: p }) => inView(p.start_utc, p.end_utc ?? new Date(t1.value).toISOString()))
    .map(({ item: p, alt }) => ({ p, style: box(p.start_utc, p.end_utc ?? new Date(t1.value).toISOString()), alt })),
)
const assetRows = computed(() =>
  withAlt('a', merged((d) => d.assets, (a) => a.asset_id + a.start_utc, (a) => a.start_utc), (a) => a.asset_id + a.start_utc)
    .filter(({ item: a }) => inView(a.start_utc, a.end_utc))
    .map(({ item: a, alt }) => ({ a, style: box(a.start_utc, a.end_utc), alt })),
)
const discontinuityRows = computed(() =>
  merged((d) => d.discontinuities, (x) => String(x.segment), (x) => x.utc)
    .filter((x) => inView(x.utc, null))
    .map((d) => ({ d, style: at(d.utc) })),
)


const eventIdDec = (hex: string) => {
  const n = parseInt(hex, 16)
  return Number.isNaN(n) ? hex : String(n)
}

const loopRows = computed(() =>
  merged((d) => d.loops, (l) => String(l.number), (l) => l.start_utc)
    .filter((l) => inView(l.start_utc, l.end_utc))
    .map((l) => ({ l, style: box(l.start_utc, l.end_utc), alt: l.number % 2 === 1 })),
)

// The manifest window (what players see) as a band, and "now" (the live edge) as a line.
const windowBand = computed(() =>
  doc.value && inView(doc.value.window.start_utc, doc.value.window.end_utc)
    ? box(doc.value.window.start_utc, doc.value.window.end_utc)
    : null,
)
// Both labels share one line: "manifest window" sits inside the band's left edge and
// "live edge" just right of the line; when the line is near the right edge its label goes
// left of it instead, and a narrow band then pushes its label outside, left of the band.
const windowLabelOutside = computed(() => {
  const w = doc.value?.window
  return !!w && !!liveEdge.value?.flip && ((Date.parse(w.end_utc) - Date.parse(w.start_utc)) / span.value) * 100 < 30
})
const liveEdge = computed(() => {
  const d = doc.value
  if (!d || d.window.ended) return null
  // The only wall-clock element: everything else is drawn straight from the JSON.
  const ms = d.mode === 'live' && liveNow.value ? liveNow.value : Date.parse(d.generated_at)
  const p = pctMs(ms)
  if (zoomed.value && (p < 0 || p > 100)) return null
  return { style: { left: `${Math.min(100, Math.max(0, p))}%` }, flip: p > 85 }
})

type Marker = ChannelTimeline['markers'][number]
const markerKey = (m: Marker) => m.event_id + m.start_utc + m.is_out
const markerVisible = (m: Marker) => inView(m.start_utc, m.is_out && !m.is_instant ? m.end_utc : null)

const lanes = computed(() => {
  const byKey = new Map<string, { key: string; label: string; color: string; markers: ChannelTimeline['markers'] }>()
  const all = merged((d) => d.markers, markerKey, (m) => m.start_utc)
  // A cue-out's span already ends at its cue-in; only draw a cue-in on its own
  // when its cue-out is not part of the document.
  const outIds = new Set(all.filter((m) => m.is_out && !m.is_instant).map((m) => m.event_id))
  for (const m of all) {
    if (!m.is_out && !m.is_instant && outIds.has(m.event_id)) continue
    const key = laneKeyForMarker({
      splice_type: m.splice_type ?? undefined,
      segmentation: m.segmentation_type_id ? { type_id: m.segmentation_type_id } : undefined,
    })
    let lane = byKey.get(key)
    if (!lane) {
      lane = {
        key,
        label: laneLabelForMarker({
          splice_type: m.splice_type ?? undefined,
          segmentation: m.segmentation_type_id ? { type_id: m.segmentation_type_id } : undefined,
        }),
        color: colorForLaneKey(key),
        markers: [],
      }
      byKey.set(key, lane)
    }
    lane.markers.push(m)
  }
  return [...byKey.values()]
})

// ── formatting ──────────────────────────────────────────────────────────────
function hms(iso: string): string {
  return iso.slice(11, 23).replace(/\.000$/, '')
}
function secs(v: number | null | undefined): string {
  if (v == null) return '—'
  return v >= 60 ? `${Math.floor(v / 60)}m${String(Math.round(v % 60)).padStart(2, '0')}s` : `${Math.round(v * 10) / 10}s`
}
const summary = computed(() => {
  const d = doc.value
  if (!d) return ''
  const w = d.window
  const r = d.range
  return (
    `${d.mode === 'live' ? 'Live' : 'Timeshift'} · ${r.scope === 'loops' ? 'previous/current/next loop' : 'range'} ${hms(r.start_utc)} → ${hms(r.end_utc)} UTC · ` +
    `manifest window ${hms(w.start_utc)} → ${hms(w.end_utc)} (${secs(w.duration_s)}) · ` +
    `segments ${w.segments.first}–${w.segments.last} (${w.segments.count}) · ${d.timeline} timeline · loop #${d.loop.number}, ${secs(d.loop.position_s)} of ${secs(d.loop.duration_s)}`
  )
})

function select(kind: string, label: string, data: unknown) {
  selected.value = selected.value?.data === data ? null : { kind, label, data }
}
function isSelected(data: unknown): boolean {
  return selected.value?.data === data
}
</script>

<template>
  <div class="window-panel surface-card border-round p-3 flex flex-column gap-3">
    <div class="flex align-items-center gap-2 wp-header">
      <button
        type="button"
        class="wp-toggle flex align-items-center gap-2"
        :aria-expanded="open"
        @click="toggle"
      >
        <i :class="['pi', open ? 'pi-chevron-down' : 'pi-chevron-right']" aria-hidden="true" />
        <h3 class="m-0 text-base">Timeline</h3>
      </button>
      <FieldHelp label="Timeline">
        The content of the loop around now, from the channel's /timeline.json: the previous, current and next loop, the
        window advertised in the manifests, and the live edge. HLS discontinuities and DASH periods are the two
        manifest views of the same loop boundaries and breaks; assets and markers (ad breaks, PPOs, ...) are placed on
        the same time axis. Click an item for its details.
      </FieldHelp>
      <span v-if="open && doc" class="text-sm text-color-secondary window-summary">{{ summary }}</span>
      <span v-else-if="!open" class="text-sm text-color-secondary">Loops, breaks and markers with the live edge and manifest window</span>
      <Button
        class="ml-auto flex-shrink-0"
        label="API docs"
        icon="pi pi-external-link"
        size="small"
        severity="secondary"
        text
        title="Open the /timeline.json API documentation (OpenAPI) in a new tab"
        @click="openDocs"
      />
      <Button
        class="flex-shrink-0"
        :label="open && showRaw ? 'Hide raw JSON' : 'Raw JSON'"
        :title="doc ? `${doc.renditions.length} rendition(s) · ${doc.assets.length} asset(s) · ${doc.markers.length} marker(s) · ${doc.discontinuities.length} discontinuit${doc.discontinuities.length === 1 ? 'y' : 'ies'}` : 'Show the raw /timeline.json'"
        size="small"
        severity="secondary"
        text
        @click="toggleRaw"
      />
    </div>

    <template v-if="open">
      <div v-if="error && !doc" class="text-sm text-color-secondary">Not available yet: {{ error }}</div>
      <div v-else-if="!doc" class="text-sm text-color-secondary">Loading…</div>

      <template v-else>
        <div v-if="error" class="text-xs text-yellow-600">Last refresh failed: {{ error }}</div>

        <div class="flex align-items-center justify-content-end gap-1 flex-wrap wp-toolbar">
          <span class="text-xs text-color-secondary text-right">
            <template v-if="zoomed">×{{ zoomFactor < 10 ? zoomFactor.toFixed(1) : Math.round(zoomFactor) }} · showing {{ secs(span / 1000) }} of {{ secs((fullT1 - fullT0) / 1000) }} · </template>
            Ctrl/⌘ + scroll or pinch to zoom, drag or Shift + scroll to pan
          </span>
          <Button label="Fit" size="small" severity="secondary" text :disabled="!zoomed" @click="vp = null" />
          <Button icon="pi pi-search-minus" size="small" severity="secondary" text rounded aria-label="Zoom out" :disabled="!zoomed" @click="zoomAt(2)" />
          <Button icon="pi pi-search-plus" size="small" severity="secondary" text rounded aria-label="Zoom in" @click="zoomAt(0.5)" />
        </div>

        <div
          class="wp-grid"
          :class="{ 'wp-zoomed': zoomed }"
          @wheel="onWheel"
          @pointerdown="onDown"
          @click.capture="onClickCapture"
        >
          <div ref="overlayEl" class="wp-overlay">
            <div
              v-for="r in loopRows"
              :key="r.l.number"
              class="wp-loop-bar"
              :style="r.style"
            />
            <div v-if="windowBand" class="wp-window" :style="windowBand" title="Window advertised in the HLS manifest">
              <span class="wp-window-label" :class="{ 'wp-window-outside': windowLabelOutside }">manifest window</span>
            </div>
            <div v-if="liveEdge" class="wp-live" :style="liveEdge.style" title="Live edge (now)">
              <span class="wp-live-label" :class="{ 'wp-live-flip': liveEdge.flip }">live edge</span>
            </div>
          </div>
          <div class="wp-label" />
          <div class="wp-track wp-ruler">
            <span v-for="t in ticks" :key="t.left" class="wp-tick" :style="{ left: t.left }">{{ t.label }}</span>
          </div>

          <div class="wp-label" />
          <div class="wp-flags" />

          <div class="wp-label">Loops</div>
          <div class="wp-track">
            <div
              v-for="r in loopRows"
              :key="r.l.number"
              class="wp-block wp-loop"
              :class="{ 'wp-current': r.l.current, 'wp-selected': isSelected(r.l) }"
              :style="r.style"
              :title="`loop ${r.l.number}${r.l.current ? ' (current)' : ''}\n${hms(r.l.start_utc)} → ${hms(r.l.end_utc)}\nsegments ${r.l.segments.first}–${r.l.segments.last} (${r.l.segments.count})`"
              @click="select('Loop', `#${r.l.number}`, r.l)"
            >
              <span class="wp-text">loop #{{ r.l.number }}<template v-if="r.l.current"> (current)</template></span>
            </div>
          </div>

          <div class="wp-label">Assets</div>
          <div class="wp-track">
            <div
              v-for="r in assetRows"
              :key="r.a.asset_id + r.a.start_utc"
              class="wp-block wp-asset"
              :class="{
                'wp-alt': r.alt,
                'wp-open-left': r.a.starts_before_range,
                'wp-open-right': r.a.ends_after_range,
                'wp-selected': isSelected(r.a),
              }"
              :style="r.style"
              :title="`${r.a.asset_id}\n${hms(r.a.start_utc)} → ${hms(r.a.end_utc)} (${secs(r.a.duration_s)})\nsegments ${r.a.segments.first}–${r.a.segments.last} (${r.a.segments.count})`"
              @click="select('Asset', r.a.asset_id, r.a)"
            >
              <span class="wp-text">{{ r.a.asset_id }}</span>
            </div>
          </div>

          <div class="wp-sep" />

          <div class="wp-label" title="Discontinuities in the HLS media playlists">HLS discontinuities</div>
          <div class="wp-track">
            <div
              v-for="r in discontinuityRows"
              :key="r.d.segment"
              class="wp-disc"
              :class="{ 'wp-selected': isSelected(r.d) }"
              :style="r.style"
              :title="`segment ${r.d.segment}, sequence ${r.d.sequence}\n${hms(r.d.utc)}`"
              @click="select('Discontinuity', r.d.reason, r.d)"
            >
            </div>
          </div>

          <div class="wp-label" title="Periods in the DASH manifest">DASH periods</div>
          <div class="wp-track">
            <div
              v-for="r in periodRows"
              :key="r.p.id + r.p.start_utc"
              class="wp-block wp-period"
              :class="{ 'wp-alt': r.alt, 'wp-selected': isSelected(r.p) }"
              :style="r.style"
              :title="`${r.p.id}\n${hms(r.p.start_utc)} → ${r.p.end_utc ? hms(r.p.end_utc) : 'open'}\nsegments ${r.p.segments.first}–${r.p.segments.last} (${r.p.segments.count})`"
              @click="select('Period', r.p.id, r.p)"
            >
              <span class="wp-text">{{ r.p.id }}</span>
            </div>
          </div>

          <div class="wp-sep" />

          <template v-for="lane in lanes" :key="lane.key">
            <div class="wp-label" :title="lane.label">{{ lane.label }}</div>
            <div class="wp-track">
              <div
                v-for="m in lane.markers.filter(markerVisible)"
                :key="markerKey(m)"
                class="wp-block wp-marker"
                :class="{
                  'wp-point': m.is_instant || !m.is_out || !m.end_utc,
                  'wp-in': !m.is_out && !m.is_instant,
                  'wp-open-left': m.starts_before_range,
                  'wp-open-right': m.ends_after_range,
                  'wp-selected': isSelected(m),
                }"
                :style="{
                  ...box(m.start_utc, m.is_out && !m.is_instant ? m.end_utc : null),
                  '--lane': lane.color,
                }"
                :title="`event ${eventIdDec(m.event_id)} — ${m.is_instant ? 'instant' : m.is_out ? 'out' : 'in'}${m.duration_s != null ? ' ' + secs(m.duration_s) : ''}\n${hms(m.start_utc)}${m.end_utc ? ' → ' + hms(m.end_utc) : ''}\nsegments ${m.segments.first}–${m.segments.last} (${m.segments.count})`"
                @click="select('Marker', `event ${eventIdDec(m.event_id)}`, m)"
              >
                <span v-if="m.is_out && !m.is_instant && m.end_utc" class="wp-text">{{ eventIdDec(m.event_id) }}</span>
              </div>
            </div>
          </template>
        </div>

        <div v-if="zoomed" class="wp-scroll">
          <input type="range" min="0" max="1000" :value="scrollPos" aria-label="Scroll the timeline" @input="onScroll" />
        </div>

        <div v-if="selected" class="wp-detail surface-100 border-round p-2">
          <div class="text-xs text-color-secondary mb-1">{{ selected.kind }} · {{ selected.label }}</div>
          <JsonViewer :value="selected.data" />
        </div>
        <div v-else class="text-xs text-color-secondary text-right">Click an item for its details.</div>

        <JsonViewer v-if="showRaw" :value="doc" max-height="24rem" />
      </template>
    </template>
  </div>
</template>

<style scoped>
.wp-toggle {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  color: inherit;
}
.wp-header > * {
  flex-shrink: 0;
}
.wp-header > .window-summary {
  flex-shrink: 1;
}
.window-summary {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wp-toolbar {
  margin-bottom: -0.5rem;
}
.wp-zoomed {
  cursor: grab;
  user-select: none;
}
.wp-scroll {
  margin-left: 9rem;
}
.wp-scroll input {
  width: 100%;
}
.wp-grid {
  position: relative;
  isolation: isolate;
  display: grid;
  grid-template-columns: 9rem 1fr;
  row-gap: 4px;
  align-items: stretch;
  padding-bottom: 20px;
}
.wp-overlay {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 9rem;
  right: 0;
  pointer-events: none;
  z-index: 1;
}
.wp-loop-bar {
  position: absolute;
  top: 22px;
  bottom: 0;
  border-left: 2px dashed rgba(100, 116, 139, 0.75);
}
.wp-loop {
  background: transparent;
  border-radius: 0;
}
.wp-loop .wp-text {
  color: var(--text-color-secondary);
  padding-left: 0.4rem;
}
.wp-loop.wp-current .wp-text {
  color: var(--text-color);
  font-weight: 700;
}
.wp-sep {
  grid-column: 1 / -1;
  border-top: 1px solid var(--p-surface-400, #94a3b8);
  margin: 2px 0;
}
.wp-flags {
  height: 18px;
}
.wp-window {
  position: absolute;
  top: 22px;
  bottom: 0;
  background: rgba(99, 102, 241, 0.14);
  border-left: 1px dashed rgba(99, 102, 241, 0.7);
  border-right: 1px dashed rgba(99, 102, 241, 0.7);
}
.wp-window-label {
  position: absolute;
  top: 2px;
  left: 4px;
  font-size: 0.65rem;
  white-space: nowrap;
  color: var(--text-color-secondary);
}
.wp-live {
  position: absolute;
  top: 22px;
  bottom: 0;
  width: 2px;
  margin-left: -1px;
  background: #ef4444;
}
.wp-live-flip {
  left: auto !important;
  right: 5px;
}
.wp-window-outside {
  left: auto !important;
  right: 100%;
  margin-right: 4px;
}
.wp-live-label {
  position: absolute;
  bottom: 2px;
  left: 5px;
  font-size: 0.65rem;
  font-weight: 600;
  white-space: nowrap;
  color: #ef4444;
  background: var(--surface-card);
  padding: 0 3px;
  border-radius: 2px;
}
.wp-label {
  font-size: 0.75rem;
  color: var(--text-color-secondary);
  align-self: center;
  padding-right: 0.5rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* Layers inside the grid: track backdrop (0) < overlay lines/bands (1) < track content (2). */
.wp-track {
  position: relative;
  height: 26px;
  border-radius: 4px;
  overflow: hidden;
}
.wp-track::before {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--surface-100);
  z-index: 0;
}
.wp-track > * {
  z-index: 2;
}
.wp-ruler {
  height: 18px;
  overflow: visible;
}
.wp-ruler::before {
  display: none;
}
.wp-tick {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  font-size: 0.7rem;
  color: var(--text-color-secondary);
  white-space: nowrap;
}
.wp-block {
  position: absolute;
  top: 2px;
  bottom: 2px;
  min-width: 3px;
  border-radius: 3px;
  overflow: hidden;
  cursor: pointer;
  box-sizing: border-box;
  display: flex;
  align-items: center;
}
.wp-text {
  font-size: 0.7rem;
  padding: 0 0.35rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #fff;
}
.wp-period {
  background: #3b82f6;
}
.wp-period.wp-alt {
  filter: brightness(1.2);
}
/* Alternating slate shades, like franken-ts's loop-bar OSD (lighter dark shade here). */
.wp-asset {
  background: #64748b;
}
.wp-asset.wp-alt {
  background: #94a3b8;
}
.wp-asset.wp-alt .wp-text {
  color: #0f172a;
}
.wp-marker {
  background: var(--lane);
}
.wp-marker.wp-point {
  width: 3px;
  min-width: 3px;
}
.wp-marker.wp-in {
  opacity: 0.6;
}
.wp-open-left {
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
  border-left: 3px dashed rgba(255, 255, 255, 0.85);
}
.wp-open-right {
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
  border-right: 3px dashed rgba(255, 255, 255, 0.85);
}
.wp-selected {
  outline: 2px solid var(--text-color);
  outline-offset: -1px;
}
.wp-disc {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  background: #d97706;
  cursor: pointer;
  overflow: visible;
}
</style>
