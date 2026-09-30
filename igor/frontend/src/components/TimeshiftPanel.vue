<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import { useToast } from 'primevue/usetoast'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ChannelHealth } from '../api/types'
import {
  buildTimeshiftUrl,
  classify,
  dateToInputValue,
  effectiveRange,
  floorToLoopMs,
  formatDuration,
  inputValueToDate,
  parseOffsetSeconds,
  formatIsoDuration,
  offsetSecondsForTarget,
  OFFSET_PARAM,
  validateRequest,
  type ChannelTiming,
  type TimelineChoice,
  type TimeFormat,
  type TimeshiftParams,
  type TimeshiftRequest,
  FULL_LOOPS_PARAM,
  TIMELINE_PARAM,
} from '../utils/timeshift'
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
  health?: ChannelHealth | null
  previewing: boolean
}>()

const emit = defineEmits<{ preview: [value: TimeshiftPreview | null] }>()

const toast = useToast()

// --- request form ------------------------------------------------------------

const useUtc = ref(true)
const startText = ref('')
const endText = ref('')
const fullLoop = ref(false)
const timeline = ref<TimelineChoice>('default')
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
const range = computed(() => effectiveRange(request.value, props.params, timing.value))

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
const hlsUrlOut = computed(() =>
  props.hlsUrl ? buildTimeshiftUrl(props.hlsUrl, props.params, request.value) : '',
)
const dashUrlOut = computed(() =>
  props.dashUrl ? buildTimeshiftUrl(props.dashUrl, props.params, request.value) : '',
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

function preview(target: 'both' | 'hls' | 'dash') {
  freezeOffset() // a datetime-based offset is relative to the moment the stream starts
  if (!canPreview.value) return
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
const timelineOptions = [
  { label: 'default (channel setting)', value: 'default' },
  { label: 'continuous (no discontinuities)', value: 'continuous' },
  { label: 'periodic (signaled at each wrap)', value: 'periodic' },
]
const formatOptions = [
  { label: 'ISO 8601', value: 'iso' },
  { label: 'Epoch seconds', value: 'epoch' },
  { label: 'Epoch milliseconds', value: 'epoch_ms' },
]
</script>

<template>
  <section class="timeshift-panel surface-card border-round p-3 flex flex-column gap-3">
    <div class="flex align-items-center gap-2 flex-wrap">
      <h3 class="m-0 text-base">Startover &amp; catchup</h3>
      <FieldHelp label="Startover and catchup">
        The same manifest URLs the channel serves live also accept a start (and optional end) time as query
        parameters. With a past end it is a finished VOD (catchup); with a future or missing end it is a live-style
        stream that starts at the start point (startover). Nothing is recorded: the past is re-derived from the loop
        and the channel epoch, so it stays correct only while the epoch and the baked content are unchanged.
      </FieldHelp>
      <span v-if="previewing" class="ml-auto text-sm text-color-secondary">Previewing in the players above</span>
    </div>

    <div class="flex gap-2 flex-wrap align-items-center">
      <span class="text-xs text-color-secondary uppercase">Presets</span>
      <Button label="Last 5 min" size="small" outlined @click="presetLast(5)" />
      <Button label="Last 15 min" size="small" outlined @click="presetLast(15)" />
      <Button label="Last hour" size="small" outlined @click="presetLast(60)" />
      <Button label="Startover from 2 min ago" size="small" outlined @click="presetStartoverFrom(2)" />
      <Button
        label="Previous loop"
        size="small"
        outlined
        :disabled="!fullTiming"
        :title="fullTiming ? '' : 'Needs the channel running (loop timing from /health)'"
        @click="presetPreviousLoop"
      />
      <Button
        label="Startover from current loop"
        size="small"
        outlined
        :disabled="!fullTiming"
        :title="fullTiming ? '' : 'Needs the channel running (loop timing from /health)'"
        @click="presetCurrentLoop"
      />
    </div>

    <div class="grid">
      <div class="col-12 md:col-6 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-start">
          Start ({{ params.start_param }})
        </label>
        <div class="flex gap-2">
          <input id="ts-start" v-model="startText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button label="Now" size="small" text @click="setNow('start')" />
          <Button icon="pi pi-times" size="small" text aria-label="Clear start" @click="startText = ''" />
        </div>
      </div>
      <div class="col-12 md:col-6 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-end">
          End ({{ params.end_param }}, optional)
        </label>
        <div class="flex gap-2">
          <input id="ts-end" v-model="endText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button label="Now" size="small" text @click="setNow('end')" />
          <Button icon="pi pi-times" size="small" text aria-label="Clear end" @click="endText = ''" />
        </div>
      </div>
      <div class="col-12 md:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary ts-label">Times entered in</label>
        <SelectButton
          :model-value="useUtc"
          :options="zoneOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          @update:model-value="onZoneChange"
        />
      </div>
      <div class="col-12 md:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary ts-label">Value format in URL</label>
        <Select v-model="format" :options="formatOptions" option-label="label" option-value="value" fluid />
      </div>
      <div class="col-12 md:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary ts-label">
          Timeline ({{ TIMELINE_PARAM }})
          <FieldHelp label="Timeline override">
            <code>timeline=default|continuous|periodic</code> — works on live URLs too, not just ranges.
            <em>default</em> (or leaving it out) follows the channel's continuous-timeline setting.
            <em>continuous</em> rewrites timestamps so there is no discontinuity at each loop wrap;
            <em>periodic</em> signals one #EXT-X-DISCONTINUITY (HLS) / Period (DASH) per wrap, which for a
            long range is many. <em>continuous</em> is refused (HTTP 400) if the baked package cannot support it.
          </FieldHelp>
        </label>
        <Select v-model="timeline" :options="timelineOptions" option-label="label" option-value="value" fluid />
      </div>
      <div class="col-12 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-offset">
          Pretend “now” is… ({{ OFFSET_PARAM }})
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
          <Button label="−1 h" size="small" outlined @click="offsetText = '-PT1H'" />
          <Button label="−10 min" size="small" outlined @click="offsetText = '-PT10M'" />
          <Button label="+1 h" size="small" outlined @click="offsetText = 'PT1H'" />
          <Button
            label="−1 loop"
            size="small"
            outlined
            :disabled="!fullTiming"
            @click="offsetText = String(-Math.round(fullTiming!.loopMs / 1000))"
          />
          <Button
            label="+1 loop"
            size="small"
            outlined
            :disabled="!fullTiming"
            @click="offsetText = String(Math.round(fullTiming!.loopMs / 1000))"
          />
          <Button icon="pi pi-times" size="small" text aria-label="Clear offset" @click="offsetText = ''" />
        </div>
        <div v-else class="flex gap-2 align-items-center">
          <input id="ts-pretend" v-model="pretendText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button icon="pi pi-times" size="small" text aria-label="Clear" @click="pretendText = ''" />
        </div>
        <div v-if="offsetMode === 'datetime'" class="text-xs text-color-secondary">
          Turned into an offset when you press Preview or Copy (the stream then plays on in real time from
          that instant), in the same zone as the other times above.
        </div>
        <div v-if="offsetHint" class="text-xs text-color-secondary">{{ offsetHint }}</div>
      </div>
      <div class="col-12 flex align-items-center gap-2">
        <Checkbox v-model="fullLoop" binary input-id="ts-full-loop" />
        <label for="ts-full-loop">Whole loops only ({{ FULL_LOOPS_PARAM }})</label>
        <FieldHelp label="Whole loops only">
          Widens the range to complete loop iterations: the start moves back to the nearest loop start at or before
          it, and the end (if given) moves forward to the nearest loop end at or after it. With no end, the
          range runs for as many whole loops as fit within the maximum span. The maximum span applies to the
          widened range.
        </FieldHelp>
      </div>
    </div>

    <div class="text-sm">
      <strong>{{ kindLabel }}</strong>
      <div v-if="summary" class="text-color-secondary mt-1">{{ summary }}</div>
    </div>

    <Message v-for="p in problems" :key="p" severity="warn" :closable="false">{{ p }}</Message>

    <div class="flex flex-column gap-2">
      <div v-if="hlsUrlOut" class="flex align-items-center gap-2">
        <span class="url-tag">HLS</span>
        <InputText :model-value="hlsUrlOut" readonly fluid class="font-mono text-xs" />
        <Button icon="pi pi-copy" text size="small" title="Copy HLS URL" @click="copy('hls')" />
        <Button icon="pi pi-play" text size="small" title="Preview in the HLS player" :disabled="!canPreview" @click="preview('hls')" />
      </div>
      <div v-if="dashUrlOut" class="flex align-items-center gap-2">
        <span class="url-tag">DASH</span>
        <InputText :model-value="dashUrlOut" readonly fluid class="font-mono text-xs" />
        <Button icon="pi pi-copy" text size="small" title="Copy DASH URL" @click="copy('dash')" />
        <Button icon="pi pi-play" text size="small" title="Preview in the DASH player" :disabled="!canPreview" @click="preview('dash')" />
      </div>
    </div>

    <div class="flex gap-2 flex-wrap">
      <Button label="Preview in both players" icon="pi pi-play" size="small" :disabled="!canPreview" @click="preview('both')" />
      <Button label="Back to live" icon="pi pi-replay" size="small" severity="secondary" :disabled="!previewing" @click="emit('preview', null)" />
    </div>

    <details class="text-sm">
      <summary class="cursor-pointer font-semibold">How it works</summary>
      <div class="flex flex-column gap-2 mt-2 text-color-secondary">
        <p class="m-0">
          These are ordinary channel manifest URLs with extra query parameters, so any player (or the CDN) can use
          them; nothing is stored per viewer. The start and end parameter names are set per channel in
          <code>[timeshift]</code> (currently <code>{{ params.start_param }}</code> and
          <code>{{ params.end_param }}</code>); <code>{{ FULL_LOOPS_PARAM }}</code> (boolean) and
          <code>{{ TIMELINE_PARAM }}</code> (<code>default</code>, <code>continuous</code> or
          <code>periodic</code>) and <code>{{ OFFSET_PARAM }}</code> (seconds or ISO 8601 duration) always have
          these fixed names.
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
  </section>
</template>

<style scoped>
/* Same height with or without an inline help button, so the controls below line up. */
.ts-label {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  min-height: 2rem;
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
