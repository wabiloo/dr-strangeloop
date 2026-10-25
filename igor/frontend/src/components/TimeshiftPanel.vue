<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import { useToast } from 'primevue/usetoast'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import type { ChannelHealth } from '../api/types'
import {
  buildTimeshiftUrl,
  classify,
  dateToInputValue,
  effectiveRange,
  effectiveTiming,
  floorToLoopMs,
  formatDuration,
  inputValueToDate,
  parseOffsetSeconds,
  formatIsoDuration,
  offsetSecondsForTarget,
  OFFSET_PARAM,
  paramName,
  validateRequest,
  type ChannelTiming,
  type TimelineChoice,
  type TimeFormat,
  type TimeshiftParams,
  type TimeshiftRequest,
  FULL_LOOPS_PARAM,
  TIMELINE_PARAM,
} from '../utils/timeshift'
import { QP_SPECS, overridePairs, type QpFormat, type QpSpec } from '../utils/queryParams'
import FieldHelp from './FieldHelp.vue'

export interface TimeshiftPreview {
  hlsUrl: string | null
  dashUrl: string | null
  /** Wall-clock instant (epoch seconds) the stream starts at, for the
   * player's playhead display; null for a non-time-shifted URL. */
  startEpochSeconds: number | null
  label: string
}

const props = defineProps<{
  /** The channel's normal (live) manifest URLs -- the base for generated ones. */
  hlsUrl?: string | null
  dashUrl?: string | null
  params: TimeshiftParams
  /** The channel's own value for each override, by parameter name (display text), shown
   * as "channel: X" so it is clear what leaving a field empty gives. */
  channelDefaults?: Record<string, string>
  health?: ChannelHealth | null
  previewing: boolean
}>()

const emit = defineEmits<{ preview: [value: TimeshiftPreview | null] }>()

const toast = useToast()

// --- collapsible ---------------------------------------------------------------
// Collapsed by default so live viewing isn't crowded by a big form; the choice
// is remembered per browser. The body is v-show (not v-if) so form state
// survives collapsing.
const STORAGE_KEY = 'igor.timeshiftPanel.open'
function readOpen(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false // storage blocked/unavailable: just stay collapsed
  }
}
const open = ref(readOpen())
function onHeaderClick(e: MouseEvent) {
  // The whole header row toggles; other buttons/links in it (help, docs, raw JSON...) keep their own behavior.
  const hit = (e.target as HTMLElement).closest('button, a, input')
  if (hit && !hit.classList.contains('ts-toggle')) return
  toggleOpen()
}
function toggleOpen() {
  open.value = !open.value
  try {
    localStorage.setItem(STORAGE_KEY, open.value ? '1' : '0')
  } catch {
    /* non-persistent is fine */
  }
}
// What is being previewed, for the header while collapsed. Captured at
// Preview time (the form may be edited afterwards).
const activeSummary = ref('')
watch(
  () => props.previewing,
  (now) => {
    if (!now) activeSummary.value = ''
  },
)

// --- request form ------------------------------------------------------------

const useUtc = ref(true)
const startText = ref('')
const endText = ref('')
const fullLoop = ref(false)
// The per-request overrides (queryParams.ts), raw text by parameter name; '' = not set
// (the channel's own setting applies). timeline_mode is one of them.
const overrides = reactive<Record<string, string>>(Object.fromEntries(QP_SPECS.map((spec) => [spec.name, ''])))
const timeline = computed<TimelineChoice>(() => {
  const v = overrides.timeline_mode
  return v === 'continuous' || v === 'periodic' ? v : 'default'
})
const overrideCount = computed(() => (props.params.overrides ? overridePairs(overrides, 'hls').length + overridePairs(overrides, 'dash').filter(([n]) => !overridePairs(overrides, 'hls').some(([m]) => m === n)).length : 0))
function clearOverrides() {
  for (const key of Object.keys(overrides)) overrides[key] = ''
  added.value = []
}
function overrideSummary(): string {
  const names = new Set<string>()
  for (const fmt of ['hls', 'dash'] as QpFormat[]) for (const [n, v] of overridePairs(overrides, fmt)) names.add(`${n}=${v}`)
  return [...names].join(', ')
}
// The overrides are opt-in so the form stays short: the section starts empty and an
// "Add override" picker (grouped like the rows: both formats, HLS only, DASH only) adds
// one. Each added override is a row with its own remove button; removing clears its value.
// Rows keep the same grouping under subheadings, only for groups that have a row.
const OVERRIDE_GROUPS: { id: string; title: string; note: string; match: (spec: QpSpec) => boolean }[] = [
  { id: 'common', title: 'HLS and DASH', note: 'Read by both manifests.', match: (spec) => spec.formats.length === 2 },
  { id: 'hls', title: 'HLS', note: 'Only on the HLS URL.', match: (spec) => spec.formats.length === 1 && spec.formats[0] === 'hls' },
  { id: 'dash', title: 'DASH', note: 'Only on the DASH URL.', match: (spec) => spec.formats.length === 1 && spec.formats[0] === 'dash' },
]
const added = ref<string[]>([])
const overrideGroups = computed(() =>
  OVERRIDE_GROUPS.map((g) => ({
    ...g,
    specs: QP_SPECS.filter((spec) => g.match(spec) && added.value.includes(spec.name)),
  })).filter((g) => g.specs.length),
)
// grouped options for the picker: only what is not added yet
const addOptions = computed(() =>
  OVERRIDE_GROUPS.map((g) => ({
    label: g.title,
    items: QP_SPECS.filter((spec) => g.match(spec) && !added.value.includes(spec.name)).map((spec) => ({
      label: spec.label,
      value: spec.name,
    })),
  })).filter((g) => g.items.length),
)
const pickerValue = ref<string | null>(null)
function addOverride(name: string | null) {
  if (name && !added.value.includes(name)) added.value.push(name)
  pickerValue.value = null // back to the "Add override…" placeholder
}
function removeOverride(name: string) {
  overrides[name] = ''
  added.value = added.value.filter((n) => n !== name)
}
function defaultOptionLabel(spec: QpSpec): string {
  const d = props.channelDefaults?.[spec.name]
  return d ? `default (channel: ${d})` : 'default (channel setting)'
}
function selectOptions(spec: QpSpec) {
  return [{ label: defaultOptionLabel(spec), value: '' }, ...(spec.options ?? [])]
}
// The offset can be typed as a duration (-PT1H / -3600) or given as the
// datetime "now" should pretend to be. A datetime is turned into an offset
// relative to the moment the stream is started (Preview/Copy), then frozen
// into the URL -- the stream advances in real time from there.
const offsetMode = ref<'duration' | 'datetime'>('duration')
const offsetDurationText = ref('')
const pretendText = ref('')
const offsetSnapshot = ref('')

function freezeOffset() {
  if (offsetMode.value !== 'datetime') return
  const target = inputValueToDate(pretendText.value, useUtc.value)
  offsetSnapshot.value = target ? String(offsetSecondsForTarget(target, Date.now())) : ''
}
watch([offsetMode, pretendText, useUtc], freezeOffset, { immediate: true })

// What the URL carries: the typed duration, or the datetime's frozen offset.
const offsetText = computed({
  get: () => (offsetMode.value === 'duration' ? offsetDurationText.value : offsetSnapshot.value),
  set: (v: string) => {
    offsetMode.value = 'duration'
    offsetDurationText.value = v
  },
})
const offsetSecondsNow = computed(() => parseOffsetSeconds(offsetText.value))
const offsetHint = computed(() => {
  const s = offsetSecondsNow.value
  if (s === null || s === 0) return ''
  const pretend = new Date(nowMs.value + s * 1000).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z')
  return `offset ${s} s = ${formatIsoDuration(s)}; “now” would be ${pretend}`
})
const format = ref<TimeFormat>('iso')

const nowMs = ref(Date.now())
let nowTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  nowTimer = setInterval(() => (nowMs.value = Date.now()), 5000)
})
onBeforeUnmount(() => {
  if (nowTimer) clearInterval(nowTimer)
})

const timing = computed<ChannelTiming | undefined>(() => {
  const h = props.health
  if (!h) return undefined
  // epoch_utc is newer serve.py; fall back to the tick count for older ones.
  const epochMs = h.epoch_utc ? Date.parse(h.epoch_utc) : (h.epoch_ticks / h.timescale) * 1000
  return { epochMs, loopMs: h.total_loop_duration_seconds * 1000 }
})
const fullTiming = computed(() => {
  const t = timing.value
  return t && Number.isFinite(t.epochMs) && Number.isFinite(t.loopMs) ? (t as Required<ChannelTiming>) : null
})

// What the server will treat as "now" for this request (real now + offset).
function pretendNow(): number {
  return Date.now() + (parseOffsetSeconds(offsetText.value) ?? 0) * 1000
}

const request = computed<TimeshiftRequest>(() => ({
  start: inputValueToDate(startText.value, useUtc.value),
  end: inputValueToDate(endText.value, useUtc.value),
  fullLoop: fullLoop.value,
  timeline: timeline.value,
  overrides: { ...overrides },
  offset: offsetText.value,
  format: format.value,
}))

const problems = computed(() => {
  const found = validateRequest(request.value, props.params, nowMs.value, timing.value)
  // A datetime that can't become an offset (beyond ±10 years, which also
  // covers anything before a long-ago epoch) deserves a clearer message.
  if (offsetMode.value === 'datetime' && pretendText.value && offsetSecondsNow.value === null) {
    return [
      ...found.filter((p) => !p.startsWith('Offset must be')),
      'That “now” is too far from the present (limit ±10 years) or before the channel epoch.',
    ]
  }
  return found
})
const kind = computed(() => classify(request.value, nowMs.value))
// an `epoch` override moves loop 0, so range maths use the effective timing
const range = computed(() => effectiveRange(request.value, props.params, effectiveTiming(request.value, props.params, timing.value)))

const kindLabel = computed(() => {
  switch (kind.value) {
    case 'catchup':
      return 'Catchup — finished VOD of a past range'
    case 'startover-bounded':
      return 'Startover — live-style, plays from the start and ends at the end time'
    case 'startover-open':
      return `Startover — live-style from the start point, capped at ${formatDuration(props.params.max_span_seconds)}`
    default: {
      const off = parseOffsetSeconds(offsetText.value) ?? 0
      return off
        ? `Live, as it ${off < 0 ? 'was' : 'will be'} ${formatDuration(Math.abs(off))} ${off < 0 ? 'ago' : 'from now'} — no start time set`
        : overrideCount.value
          ? `Live with ${overrideCount.value} override${overrideCount.value === 1 ? '' : 's'} — no start time set`
          : 'Live — no start time set (plain channel URL)'
    }
  }
})

const summary = computed(() => {
  const r = range.value
  if (!r) return ''
  const fmt = (ms: number) => new Date(ms).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z')
  const loops = fullTiming.value ? ` (${(((r.endMs - r.startMs) / fullTiming.value.loopMs)).toFixed(1)} loops)` : ''
  const snapped = fullLoop.value && !fullTiming.value ? ' Loop timing unknown until the channel is reachable, so full-loop widening is not shown.' : ''
  return `Serves ${fmt(r.startMs)} → ${fmt(r.endMs)}, ${formatDuration((r.endMs - r.startMs) / 1000)}${loops}. Start and end snap to segment boundaries.${snapped}`
})

// A timeline-only override (no start) is also a valid, shareable URL.
// (each URL carries only the overrides its format reads)
const hlsUrlOut = computed(() =>
  props.hlsUrl ? buildTimeshiftUrl(props.hlsUrl, props.params, request.value, 'hls') : '',
)
const dashUrlOut = computed(() =>
  props.dashUrl ? buildTimeshiftUrl(props.dashUrl, props.params, request.value, 'dash') : '',
)

const canPreview = computed(() => problems.value.length === 0 && (!!hlsUrlOut.value || !!dashUrlOut.value))

// --- presets --------------------------------------------------------------------

function setRange(start: Date | null, end: Date | null, loop = false) {
  startText.value = dateToInputValue(start, useUtc.value)
  endText.value = dateToInputValue(end, useUtc.value)
  fullLoop.value = loop
}

function presetLast(minutes: number) {
  const now = pretendNow()
  nowMs.value = Date.now()
  setRange(new Date(now - minutes * 60_000), new Date(now), false)
}

function presetStartoverFrom(minutesAgo: number) {
  const now = pretendNow()
  nowMs.value = Date.now()
  setRange(new Date(now - minutesAgo * 60_000), null, false)
}

function presetCurrentLoop() {
  const t = fullTiming.value
  if (!t) return
  const now = pretendNow()
  nowMs.value = Date.now()
  setRange(new Date(floorToLoopMs(now, t)), null, true)
}

function presetPreviousLoop() {
  const t = fullTiming.value
  if (!t) return
  const now = pretendNow()
  nowMs.value = Date.now()
  const thisLoop = floorToLoopMs(now, t)
  setRange(new Date(thisLoop - t.loopMs), new Date(thisLoop), true)
}

function setNow(which: 'start' | 'end') {
  const text = dateToInputValue(new Date(pretendNow()), useUtc.value)
  if (which === 'start') startText.value = text
  else endText.value = text
}

function onZoneChange(utc: boolean) {
  // Keep the same instants when the display zone flips.
  const { start, end } = request.value
  useUtc.value = utc
  const pretend = inputValueToDate(pretendText.value, !utc)
  startText.value = dateToInputValue(start, utc)
  endText.value = dateToInputValue(end, utc)
  if (pretend) pretendText.value = dateToInputValue(pretend, utc)
}

// --- actions --------------------------------------------------------------------

async function copy(which: 'hls' | 'dash') {
  freezeOffset() // a datetime-based offset is relative to the moment of copying
  const url = which === 'hls' ? hlsUrlOut.value : dashUrlOut.value
  try {
    await navigator.clipboard.writeText(url)
    toast.add({ severity: 'success', summary: 'Copied', detail: url, life: 2500 })
  } catch {
    toast.add({ severity: 'warn', summary: 'Copy failed', detail: 'Select the URL and copy it manually.', life: 4000 })
  }
}

function startEpochSeconds(): number | null {
  const r = range.value
  return r ? r.startMs / 1000 : null
}

function describeRequest(): string {
  const r = range.value
  const fmt = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
  const word = { live: 'Live', catchup: 'Catchup', 'startover-bounded': 'Startover', 'startover-open': 'Startover' }[kind.value]
  const off = offsetSecondsNow.value
  return [
    word,
    r ? `${fmt(r.startMs)} → ${kind.value === 'startover-open' ? 'open' : fmt(r.endMs)}` : '',
    off ? `as if ${off < 0 ? '' : 'in '}${formatDuration(Math.abs(off))}${off < 0 ? ' ago' : ''}` : '',
    overrideSummary(),
  ]
    .filter(Boolean)
    .join(' · ')
}

function preview(target: 'both' | 'hls' | 'dash') {
  freezeOffset() // a datetime-based offset is relative to the moment the stream starts
  if (!canPreview.value) return
  activeSummary.value = describeRequest()
  emit('preview', {
    hlsUrl: target === 'dash' ? null : hlsUrlOut.value || null,
    dashUrl: target === 'hls' ? null : dashUrlOut.value || null,
    startEpochSeconds: startEpochSeconds(),
    label: kindLabel.value,
  })
}

const offsetModeOptions = [
  { label: 'Duration', value: 'duration' },
  { label: 'Pretend “now” is (datetime)', value: 'datetime' },
]
const zoneOptions = [
  { label: 'UTC', value: true },
  { label: 'Local', value: false },
]
const formatOptions = [
  { label: 'ISO 8601', value: 'iso' },
  { label: 'Epoch seconds', value: 'epoch' },
  { label: 'Epoch milliseconds', value: 'epoch_ms' },
]
</script>

<template>
  <section class="timeshift-panel surface-card border-round p-3 flex flex-column gap-3">
    <div class="ts-toggle-row flex align-items-center gap-2 flex-wrap" @click="onHeaderClick">
      <button
        type="button"
        class="ts-toggle flex align-items-center gap-2"
        :aria-expanded="open"
        aria-controls="ts-body"
      >
        <i :class="['pi', open ? 'pi-chevron-down' : 'pi-chevron-right']" aria-hidden="true" />
        <h3 class="m-0 text-base">Startover, catchup &amp; overrides</h3>
      </button>
      <FieldHelp label="Startover, catchup and overrides">
        One form for every query parameter of the channel's manifests: the generated HLS and DASH URLs go to the
        players below when you press Preview (each URL carries only the parameters its format reads), and you can
        copy them. Overrides replace a channel setting for that request only; leave a field empty to use the
        channel's own. The same manifest URLs the channel serves live also accept a start (and optional end) time as query
        parameters. With a past end it is a finished VOD (catchup); with a future or missing end it is a live-style
        stream that starts at the start point (startover). Nothing is recorded: the past is re-derived from the loop
        and the channel epoch, so it stays correct only while the epoch and the baked content are unchanged.
      </FieldHelp>
      <span v-if="previewing" class="ts-active text-sm">
        Previewing{{ activeSummary ? ': ' + activeSummary : '' }}
      </span>
      <span v-else-if="!open" class="text-sm text-color-secondary">Play past ranges, “as if it were another time”, or re-render the manifests with other settings</span>
      <Button
        v-if="previewing"
        label="Back to live"
        icon="pi pi-replay"
        size="small"
        severity="secondary"
        class="ml-auto"
        @click="emit('preview', null)"
      />
    </div>

    <!-- v-show on a plain wrapper: primeflex's `.flex` is display:flex !important and would defeat it. -->
    <div v-show="open" id="ts-body">
      <div class="flex flex-column gap-3">
    <div class="grid">
      <div v-if="!params.timeshift" class="col-12">
        <Message severity="secondary" :closable="false">
          Startover and catchup are switched off for this channel (<code>[query] timeshift</code>): start, end,
          whole loops and offset are ignored, so it only plays live.
        </Message>
      </div>
      <template v-if="params.timeshift">
      <div class="col-12 md:col-6 lg:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-start">
          Start <code class="qp">{{ paramName(params, params.start_param) }}</code>
        </label>
        <div class="flex gap-2">
          <input id="ts-start" v-model="startText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button label="Now" size="small" severity="secondary" text @click="setNow('start')" />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear start" @click="startText = ''" />
        </div>
      </div>
      <div class="col-12 md:col-6 lg:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-end">
          End <code class="qp">{{ paramName(params, params.end_param) }}</code> <span>(optional)</span>
        </label>
        <div class="flex gap-2">
          <input id="ts-end" v-model="endText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button label="Now" size="small" severity="secondary" text @click="setNow('end')" />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear end" @click="endText = ''" />
        </div>
      </div>
      <div class="col-12 md:col-6 lg:col-4 flex flex-column justify-content-end">
        <SelectButton
          :model-value="useUtc"
          :options="zoneOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          @update:model-value="onZoneChange"
        />
      </div>
      <div class="col-12 flex gap-2 flex-wrap align-items-center">
        <span class="text-xs text-color-secondary uppercase">Presets</span>
        <Button label="Last 5 min" size="small" severity="secondary" outlined @click="presetLast(5)" />
        <Button label="Last 15 min" size="small" severity="secondary" outlined @click="presetLast(15)" />
        <Button label="Last hour" size="small" severity="secondary" outlined @click="presetLast(60)" />
        <Button label="Startover from 2 min ago" size="small" severity="secondary" outlined @click="presetStartoverFrom(2)" />
        <Button
          label="Previous loop"
          size="small"
          severity="secondary"
          outlined
          :disabled="!fullTiming"
          :title="fullTiming ? '' : 'Needs the channel running (loop timing from /health)'"
          @click="presetPreviousLoop"
        />
        <Button
          label="Startover from current loop"
          size="small"
          severity="secondary"
          outlined
          :disabled="!fullTiming"
          :title="fullTiming ? '' : 'Needs the channel running (loop timing from /health)'"
          @click="presetCurrentLoop"
        />
      </div>
      <div class="col-12 flex align-items-center gap-2">
        <Checkbox v-model="fullLoop" binary input-id="ts-full-loop" />
        <label for="ts-full-loop">Whole loops only <code class="qp">{{ paramName(params, FULL_LOOPS_PARAM) }}</code></label>
        <FieldHelp label="Whole loops only">
          Widens the range to complete loop iterations: the start moves back to the nearest loop start at or before
          it, and the end (if given) moves forward to the nearest loop end at or after it. With no end, the
          range runs for as many whole loops as fit within the maximum span. The maximum span applies to the
          widened range.
        </FieldHelp>
      </div>
      <div class="col-12 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-offset">
          Pretend “now” is… <code class="qp">{{ paramName(params, OFFSET_PARAM) }}</code>
          <FieldHelp label="Offset">
            Plays the stream as it was (negative) or will be (positive) that long from now: the live edge and the
            DVR window behind it sit at <em>now + offset</em>, every timestamp is the true content time, and
            “past”/“future” for start and end are judged against that moment. Signed seconds
            (<code>-3600</code>) or an ISO 8601 duration (<code>-PT1H</code>, <code>P1DT2H</code>). A negative
            offset can't reach back before the channel epoch. Instead of a duration you can pick the
            datetime “now” should pretend to be: Igor converts it to an offset when you press Preview or Copy
            (<em>that datetime minus the moment you start</em>) and fixes it in the URL, so the stream plays on in
            real time from that instant; copy again later and the offset will differ accordingly. DASH manifests carry a
            <code>UTCTiming</code> value so players use that time as “now”.
          </FieldHelp>
        </label>
        <SelectButton
          v-model="offsetMode"
          :options="offsetModeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
        />
        <div v-if="offsetMode === 'duration'" class="flex gap-2 flex-wrap align-items-center">
          <InputText id="ts-offset" v-model="offsetDurationText" placeholder="e.g. -PT1H or -3600" class="flex-1" style="min-width: 10rem" />
          <Button label="−1 h" size="small" severity="secondary" outlined @click="offsetText = '-PT1H'" />
          <Button label="−10 min" size="small" severity="secondary" outlined @click="offsetText = '-PT10M'" />
          <Button label="+1 h" size="small" severity="secondary" outlined @click="offsetText = 'PT1H'" />
          <Button
            label="−1 loop"
            size="small"
            severity="secondary"
            outlined
            :disabled="!fullTiming"
            @click="offsetText = String(-Math.round(fullTiming!.loopMs / 1000))"
          />
          <Button
            label="+1 loop"
            size="small"
            severity="secondary"
            outlined
            :disabled="!fullTiming"
            @click="offsetText = String(Math.round(fullTiming!.loopMs / 1000))"
          />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear offset" @click="offsetText = ''" />
        </div>
        <div v-else class="flex gap-2 align-items-center">
          <input id="ts-pretend" v-model="pretendText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear" @click="pretendText = ''" />
        </div>
        <div v-if="offsetMode === 'datetime'" class="text-xs text-color-secondary">
          Turned into an offset when you press Preview or Copy (the stream then plays on in real time from
          that instant), in the same zone as the other times above.
        </div>
        <div v-if="offsetHint" class="text-xs text-color-secondary">{{ offsetHint }}</div>
      </div>
      </template>
      <div v-if="params.overrides" class="col-12 flex flex-column gap-2">
        <div class="flex align-items-center gap-2">
          <h4 class="m-0 text-sm">Rendering overrides</h4>
          <FieldHelp label="Rendering overrides">
            Each one replaces a channel setting for the request that carries it; the manifests change, the segment
            bytes never do. Nothing is shown until you add an override; an added one with no value keeps the
            channel's own. Rows under the <em>HLS</em> or <em>DASH</em> subheading are only put on that format's URL. Anything the server cannot honour (for example
            <code>cue_tags=only</code> with markers that are not plain splice_insert) comes back as HTTP 400 in the
            player. Which parameters exist, and their names, is a channel setting: <code>[query]</code>.
          </FieldHelp>
          <Select
            v-if="addOptions.length"
            v-model="pickerValue"
            input-id="ts-add-override"
            :options="addOptions"
            option-group-label="label"
            option-group-children="items"
            option-label="label"
            option-value="value"
            placeholder="Add override…"
            size="small"
            data-testid="ts-add-override"
            @update:model-value="addOverride"
          />
          <Button
            v-if="added.length"
            label="Remove all"
            icon="pi pi-times"
            size="small"
            severity="secondary"
            text
            @click="clearOverrides"
          />
        </div>
        <div v-if="!added.length" class="text-xs text-color-secondary">
          None added: requests use the channel's own settings. Add one to change how the manifests are rendered.
        </div>
        <div v-for="group in overrideGroups" :key="group.id" class="flex flex-column gap-2" :data-testid="`ts-overrides-${group.id}`">
          <div class="flex align-items-baseline gap-2">
            <h5 class="m-0 text-sm">{{ group.title }}</h5>
            <span class="text-xs text-color-secondary">{{ group.note }}</span>
          </div>
          <div class="grid">
            <div v-for="spec in group.specs" :key="spec.name" class="col-12 md:col-6 lg:col-4 flex flex-column gap-1" :data-testid="`ts-override-${spec.name}`">
              <label class="text-xs text-color-secondary ts-label" :for="`ts-ov-${spec.name}`">
                {{ spec.label }} <code class="qp">{{ paramName(params, spec.name) }}</code>
                <FieldHelp :label="spec.label">{{ spec.help }}</FieldHelp>
                <Button
                  icon="pi pi-times"
                  size="small"
                  severity="secondary"
                  text
                  rounded
                  class="ml-auto"
                  :aria-label="`Remove ${spec.label}`"
                  title="Remove this override"
                  @click="removeOverride(spec.name)"
                />
              </label>
              <Select
                v-if="spec.kind === 'choice' || spec.kind === 'bool'"
                v-model="overrides[spec.name]"
                :input-id="`ts-ov-${spec.name}`"
                :options="selectOptions(spec)"
                :placeholder="defaultOptionLabel(spec)"
                option-label="label"
                option-value="value"
                fluid
              />
              <template v-else>
                <InputText
                  :id="`ts-ov-${spec.name}`"
                  v-model="overrides[spec.name]"
                  :placeholder="spec.placeholder"
                  fluid
                />
                <span v-if="channelDefaults?.[spec.name]" class="text-xs text-color-secondary">
                  channel: {{ channelDefaults[spec.name] }}
                </span>
              </template>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="col-12">
        <Message severity="secondary" :closable="false">
          Per-request overrides are switched off for this channel (<code>[query] overrides</code>): only the
          startover and catchup parameters are read, and the channel's own settings always apply.
        </Message>
      </div>
    </div>

    <div class="ts-output flex flex-column gap-3">
      <div class="text-sm">
        <strong>{{ kindLabel }}</strong>
        <div v-if="summary" class="text-color-secondary mt-1">{{ summary }}</div>
      </div>

      <Message v-for="p in problems" :key="p" severity="warn" :closable="false">{{ p }}</Message>

      <div class="flex align-items-center gap-2">
        <label class="text-xs text-color-secondary" for="ts-format">Value format in URL</label>
        <Select v-model="format" input-id="ts-format" :options="formatOptions" option-label="label" option-value="value" />
      </div>

      <div class="flex flex-column gap-2">
        <div v-if="hlsUrlOut" class="flex align-items-center gap-2">
          <span class="url-tag">HLS</span>
          <InputText :model-value="hlsUrlOut" readonly fluid class="font-mono text-xs" />
          <Button icon="pi pi-copy" severity="secondary" text size="small" title="Copy HLS URL" @click="copy('hls')" />
          <Button icon="pi pi-play" severity="secondary" text size="small" title="Preview in the HLS player" :disabled="!canPreview" @click="preview('hls')" />
        </div>
        <div v-if="dashUrlOut" class="flex align-items-center gap-2">
          <span class="url-tag">DASH</span>
          <InputText :model-value="dashUrlOut" readonly fluid class="font-mono text-xs" />
          <Button icon="pi pi-copy" severity="secondary" text size="small" title="Copy DASH URL" @click="copy('dash')" />
          <Button icon="pi pi-play" severity="secondary" text size="small" title="Preview in the DASH player" :disabled="!canPreview" @click="preview('dash')" />
        </div>
      </div>

      <div class="flex gap-2 flex-wrap">
        <Button label="Preview in both players" icon="pi pi-play" size="small" :disabled="!canPreview" @click="preview('both')" />
      </div>
    </div>

    <details class="text-sm">
      <summary class="cursor-pointer font-semibold">How it works</summary>
      <div class="flex flex-column gap-2 mt-2 text-color-secondary">
        <p class="m-0">
          These are ordinary channel manifest URLs with extra query parameters, so any player (or the CDN) can use
          them; nothing is stored per viewer. The start and end parameter names, and a prefix put in front of every
          parameter name, are set per channel in <code>[query]</code> (currently
          <code>{{ paramName(params, params.start_param) }}</code> and
          <code>{{ paramName(params, params.end_param) }}</code>); <code>{{ paramName(params, FULL_LOOPS_PARAM) }}</code>
          (boolean), <code>{{ paramName(params, TIMELINE_PARAM) }}</code> (<code>default</code>,
          <code>continuous</code> or <code>periodic</code>) and <code>{{ paramName(params, OFFSET_PARAM) }}</code>
          (seconds or ISO 8601 duration) otherwise have these fixed names. The overrides ({{ paramName(params, 'epoch') }},
          {{ paramName(params, 'dvr_window') }}, {{ paramName(params, 'cue_tags') }}, ...) work the same way, can be used
          without a start time, and are listed with their names in the form above; a channel can switch the overrides off
          (<code>[query] overrides</code>) or startover/catchup off (<code>[query] timeshift</code>) independently.
        </p>
        <ul class="m-0 pl-4">
          <li>
            <strong>Values</strong>: epoch seconds, epoch milliseconds (anything ≥ 1e11) or ISO 8601
            (<code>2026-09-30T08:00:00Z</code>, an offset, or no zone = UTC).
          </li>
          <li>
            <strong>Catchup</strong> (start and a past end): a VOD playlist (<code>ENDLIST</code> / static MPD) over
            the range, cacheable forever.
          </li>
          <li>
            <strong>Startover</strong> (start, end in the future or absent): plays from the start point at normal
            speed; the manifest grows to the live edge and becomes a VOD once the end passes. With no end it stops
            after the maximum span ({{ formatDuration(params.max_span_seconds) }}).
          </li>
          <li>
            <strong>Limits</strong>: start no earlier than the channel epoch and not in the future; range at most
            {{ formatDuration(params.max_span_seconds) }}. Violations return HTTP 400 with a message.
          </li>
          <li>
            <strong>Precision</strong>: start and end snap to segment boundaries (no re-muxing). SCTE-35 markers
            inside the range are carried through.
          </li>
          <li>
            <strong>Discontinuities</strong>: unless the timeline is continuous, each loop wrap inside the range is an
            #EXT-X-DISCONTINUITY / DASH Period, so a long range over a short loop has many.
          </li>
          <li>
            <strong>Caveat</strong>: history is re-derived from the loop and epoch, not recorded. Restarting the
            channel with a new epoch, or re-baking, silently changes what past times contain.
          </li>
          <li>
            The playhead time shown for DASH in a preview is approximate for time-shifted streams.
          </li>
        </ul>
      </div>
    </details>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ts-toggle-row {
  cursor: pointer;
}
.ts-toggle {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  color: inherit;
}
.ts-active {
  color: var(--p-primary-color, #c0392b);
  font-weight: 600;
}
/* Same height with or without an inline help button, so the controls below line up. */
.ts-label {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  min-height: 2rem;
}
.ts-output {
  padding: 1rem;
  border-radius: var(--p-border-radius, 0.5rem);
  background: var(--p-surface-0, #fff);
  border: 1px solid var(--p-surface-200, #e5e7eb);
}
.qp {
  font-size: 0.75rem;
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
  color: var(--p-text-color, #111827);
}
.url-tag {
  min-width: 3.25rem;
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-align: center;
  padding: 0.15rem 0.4rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
  color: var(--p-text-color, #111827);
}
</style>
