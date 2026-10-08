<script setup lang="ts">
import Button from 'primevue/button'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { getChannelTimeline } from '../api/client'
import type { ChannelTimeline } from '../api/types'
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

const doc = ref<ChannelTimeline | null>(null)
const error = ref('')
const showRaw = ref(false)
type Selection = { kind: string; label: string; data: unknown }
const selected = ref<Selection | null>(null)

let timer: ReturnType<typeof setInterval> | null = null
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  try {
    const next = await getChannelTimeline(props.name, props.query ?? '')
    if (seq !== requestSeq) return // a newer request (e.g. query changed) superseded this one
    doc.value = next
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
  selected.value = null
  restartPolling()
}, { immediate: true })
onBeforeUnmount(() => {
  requestSeq++
  if (timer) clearInterval(timer)
})

// ── geometry ────────────────────────────────────────────────────────────────
const t0 = computed(() => (doc.value ? Date.parse(doc.value.window.start_utc) : 0))
const t1 = computed(() => (doc.value ? Date.parse(doc.value.window.end_utc) : 1))
const span = computed(() => Math.max(1, t1.value - t0.value))

function pct(iso: string): number {
  return ((Date.parse(iso) - t0.value) / span.value) * 100
}
function box(startIso: string, endIso: string | null): { left: string; width: string } {
  const a = Math.min(100, Math.max(0, pct(startIso)))
  const b = endIso ? Math.min(100, Math.max(0, pct(endIso))) : a
  return { left: `${a}%`, width: `${Math.max(b - a, 0)}%` }
}
function at(iso: string): { left: string } {
  return { left: `${Math.min(100, Math.max(0, pct(iso)))}%` }
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
  (doc.value?.periods ?? []).map((p, i) => ({ p, style: box(p.start_utc, p.end_utc ?? doc.value!.window.end_utc), alt: i % 2 === 1 })),
)
const assetRows = computed(() =>
  (doc.value?.assets ?? []).map((a, i) => ({ a, style: box(a.start_utc, a.end_utc), alt: i % 2 === 1 })),
)
const discontinuityRows = computed(() => (doc.value?.discontinuities ?? []).map((d) => ({ d, style: at(d.utc) })))

const lanes = computed(() => {
  const byKey = new Map<string, { key: string; label: string; color: string; markers: ChannelTimeline['markers'] }>()
  for (const m of doc.value?.markers ?? []) {
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
  return (
    `${d.mode === 'live' ? 'Live window' : 'Timeshift range'} · ${hms(w.start_utc)} → ${hms(w.end_utc)} UTC · ${secs(w.duration_s)} · ` +
    `segments ${w.first_segment}–${w.last_segment} · ${d.timeline} timeline · loop #${d.loop.number}, ${secs(d.loop.position_s)} of ${secs(d.loop.duration_s)}`
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
    <div class="flex align-items-center gap-2 cursor-pointer" @click="toggle">
      <span :class="open ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" class="text-sm" />
      <h3 class="m-0 text-sm text-color-secondary uppercase">Window</h3>
      <span v-if="open && doc" class="text-xs text-color-secondary ml-2 window-summary">{{ summary }}</span>
      <span v-if="!open" class="text-xs text-color-secondary ml-2">
        Markers, periods, assets and discontinuities in the current window (from /timeline.json)
      </span>
    </div>

    <template v-if="open">
      <div v-if="error && !doc" class="text-sm text-color-secondary">Not available yet: {{ error }}</div>
      <div v-else-if="!doc" class="text-sm text-color-secondary">Loading…</div>

      <template v-else>
        <div v-if="error" class="text-xs text-yellow-600">Last refresh failed: {{ error }}</div>

        <div class="wp-grid">
          <div class="wp-label" />
          <div class="wp-track wp-ruler">
            <span v-for="t in ticks" :key="t.left" class="wp-tick" :style="{ left: t.left }">{{ t.label }}</span>
          </div>

          <div class="wp-label">Periods</div>
          <div class="wp-track">
            <div
              v-for="r in periodRows"
              :key="r.p.id + r.p.start_utc"
              class="wp-block wp-period"
              :class="{ 'wp-alt': r.alt, 'wp-selected': isSelected(r.p) }"
              :style="r.style"
              :title="`${r.p.id}\n${hms(r.p.start_utc)} → ${r.p.end_utc ? hms(r.p.end_utc) : 'open'}\nsegments ${r.p.first_segment}–${r.p.last_segment}`"
              @click="select('Period', r.p.id, r.p)"
            >
              <span class="wp-text">{{ r.p.id }}</span>
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
                'wp-open-left': r.a.starts_before_window,
                'wp-open-right': r.a.ends_after_window,
                'wp-selected': isSelected(r.a),
              }"
              :style="r.style"
              :title="`${r.a.asset_id}\n${hms(r.a.start_utc)} → ${hms(r.a.end_utc)} (${secs(r.a.duration_s)})\nsegments ${r.a.first_segment}–${r.a.last_segment}`"
              @click="select('Asset', r.a.asset_id, r.a)"
            >
              <span class="wp-text">{{ r.a.asset_id }}</span>
            </div>
          </div>

          <template v-for="lane in lanes" :key="lane.key">
            <div class="wp-label" :title="lane.label">{{ lane.label }}</div>
            <div class="wp-track">
              <div
                v-for="m in lane.markers"
                :key="m.event_id + m.start_utc + m.is_out"
                class="wp-block wp-marker"
                :class="{
                  'wp-point': m.is_instant || !m.is_out || !m.end_utc,
                  'wp-in': !m.is_out && !m.is_instant,
                  'wp-open-left': m.starts_before_window,
                  'wp-open-right': m.ends_after_window,
                  'wp-selected': isSelected(m),
                }"
                :style="{
                  ...box(m.start_utc, m.is_out && !m.is_instant ? m.end_utc : null),
                  '--lane': lane.color,
                }"
                :title="`event ${m.event_id} — ${m.is_instant ? 'instant' : m.is_out ? 'out' : 'in'}${m.duration_s != null ? ' ' + secs(m.duration_s) : ''}\n${hms(m.start_utc)}${m.end_utc ? ' → ' + hms(m.end_utc) : ''}\nsegments ${m.first_segment}–${m.last_segment}`"
                @click="select('Marker', `event ${m.event_id}`, m)"
              >
                <span v-if="m.is_out && !m.is_instant && m.end_utc" class="wp-text">{{ m.event_id }}</span>
              </div>
            </div>
          </template>

          <div class="wp-label">Discont.</div>
          <div class="wp-track">
            <div
              v-for="r in discontinuityRows"
              :key="r.d.segment"
              class="wp-disc"
              :class="{ 'wp-selected': isSelected(r.d) }"
              :style="r.style"
              :title="`${r.d.reason}\nsegment ${r.d.segment}, sequence ${r.d.sequence}\n${hms(r.d.utc)}`"
              @click="select('Discontinuity', r.d.reason, r.d)"
            >
              <span class="wp-disc-label">{{ r.d.reason.replace('_', ' ') }}</span>
            </div>
          </div>
        </div>

        <div v-if="selected" class="wp-detail surface-100 border-round p-2">
          <div class="text-xs text-color-secondary mb-1">{{ selected.kind }} · {{ selected.label }}</div>
          <pre class="m-0 text-xs wp-pre">{{ JSON.stringify(selected.data, null, 2) }}</pre>
        </div>
        <div v-else class="text-xs text-color-secondary">Click an item for its details.</div>

        <div class="flex align-items-center gap-2">
          <Button
            :label="showRaw ? 'Hide raw JSON' : 'Raw JSON'"
            size="small"
            severity="secondary"
            text
            @click="showRaw = !showRaw"
          />
          <span class="text-xs text-color-secondary">
            {{ doc.renditions.length }} rendition(s) · {{ doc.assets.length }} asset(s) · {{ doc.markers.length }} marker(s) ·
            {{ doc.discontinuities.length }} discontinuit{{ doc.discontinuities.length === 1 ? 'y' : 'ies' }}
          </span>
        </div>
        <pre v-if="showRaw" class="wp-detail surface-100 border-round p-2 m-0 text-xs wp-pre">{{ JSON.stringify(doc, null, 2) }}</pre>
      </template>
    </template>
  </div>
</template>

<style scoped>
.window-summary {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wp-grid {
  display: grid;
  grid-template-columns: 9rem 1fr;
  row-gap: 4px;
  align-items: stretch;
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
.wp-track {
  position: relative;
  height: 26px;
  background: var(--surface-100);
  border-radius: 4px;
  overflow: hidden;
}
.wp-ruler {
  height: 18px;
  background: transparent;
  overflow: visible;
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
  background: var(--primary-color);
  opacity: 0.85;
}
.wp-asset {
  background: var(--teal-600, #0d9488);
}
.wp-period.wp-alt,
.wp-asset.wp-alt {
  filter: brightness(1.2);
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
  background: var(--red-500, #ef4444);
  cursor: pointer;
  overflow: visible;
}
.wp-disc-label {
  position: absolute;
  top: 3px;
  left: 4px;
  font-size: 0.65rem;
  white-space: nowrap;
  color: var(--red-500, #ef4444);
}
.wp-detail {
  max-height: 16rem;
  overflow: auto;
}
.wp-pre {
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
