// The state behind the two request forms under the players: the Startover & catchup
// panel (start, end, whole loops, offset) and the URL overrides panel (every other
// per-request parameter). Both feed ONE set of manifest URLs, so the state, the
// validation and the URL building live here and the panels are views over it.

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import type { ChannelHealth } from '../api/types'
import { QP_SPECS, overridePairs } from '../utils/queryParams'
import {
  buildTimeshiftUrl,
  classify,
  dateToInputValue,
  effectiveRange,
  effectiveTiming,
  formatDuration,
  formatIsoDuration,
  inputValueToDate,
  offsetSecondsForTarget,
  parseOffsetSeconds,
  validateRequest,
  type ChannelTiming,
  type TimeFormat,
  type TimelineChoice,
  type TimeshiftParams,
  type TimeshiftRequest,
} from '../utils/timeshift'

export interface UrlRequestInputs {
  /** The channel's normal (live) manifest URLs -- the base for generated ones. */
  hlsUrl: () => string | null | undefined
  dashUrl: () => string | null | undefined
  params: () => TimeshiftParams
  health: () => ChannelHealth | null | undefined
}

export function useUrlRequestForm(inputs: UrlRequestInputs) {
  // --- time form (Startover & catchup panel) ----------------------------------
  const time = reactive({
    useUtc: true,
    startText: '',
    endText: '',
    fullLoop: false,
    offsetMode: 'duration' as 'duration' | 'datetime',
    offsetDurationText: '',
    pretendText: '',
    offsetSnapshot: '',
    format: 'iso' as TimeFormat,
  })

  // --- URL overrides (raw text by parameter name; '' = not set) ----------------
  const overrides = reactive<Record<string, string>>(Object.fromEntries(QP_SPECS.map((spec) => [spec.name, ''])))
  // Opt-in rows: the section starts empty and an "Add override" picker adds one.
  const added = ref<string[]>([])

  const timeline = computed<TimelineChoice>(() => {
    const v = overrides.timeline_mode
    return v === 'continuous' || v === 'periodic' ? v : 'default'
  })
  const overrideCount = computed(() => {
    if (!inputs.params().overrides) return 0
    const hls = overridePairs(overrides, 'hls')
    return hls.length + overridePairs(overrides, 'dash').filter(([n]) => !hls.some(([m]) => m === n)).length
  })
  function clearOverrides() {
    for (const key of Object.keys(overrides)) overrides[key] = ''
    added.value = []
  }
  function overrideSummary(): string {
    const names = new Set<string>()
    for (const fmt of ['hls', 'dash'] as const) for (const [n, v] of overridePairs(overrides, fmt)) names.add(`${n}=${v}`)
    return [...names].join(', ')
  }

  // The offset can be typed as a duration (-PT1H / -3600) or given as the datetime "now"
  // should pretend to be. A datetime is turned into an offset relative to the moment the
  // stream is started (Preview/Copy), then frozen into the URL.
  function freezeOffset() {
    if (time.offsetMode !== 'datetime') return
    const target = inputValueToDate(time.pretendText, time.useUtc)
    time.offsetSnapshot = target ? String(offsetSecondsForTarget(target, Date.now())) : ''
  }
  watch(() => [time.offsetMode, time.pretendText, time.useUtc], freezeOffset, { immediate: true })

  // What the URL carries: the typed duration, or the datetime's frozen offset.
  const offsetText = computed({
    get: () => (time.offsetMode === 'duration' ? time.offsetDurationText : time.offsetSnapshot),
    set: (v: string) => {
      time.offsetMode = 'duration'
      time.offsetDurationText = v
    },
  })
  const offsetSecondsNow = computed(() => parseOffsetSeconds(offsetText.value))

  const nowMs = ref(Date.now())
  let nowTimer: ReturnType<typeof setInterval> | null = null
  onMounted(() => {
    nowTimer = setInterval(() => (nowMs.value = Date.now()), 5000)
  })
  onBeforeUnmount(() => {
    if (nowTimer) clearInterval(nowTimer)
  })

  const offsetHint = computed(() => {
    const s = offsetSecondsNow.value
    if (s === null || s === 0) return ''
    const pretend = new Date(nowMs.value + s * 1000).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z')
    return `offset ${s} s = ${formatIsoDuration(s)}; “now” would be ${pretend}`
  })

  const timing = computed<ChannelTiming | undefined>(() => {
    const h = inputs.health()
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
    start: inputValueToDate(time.startText, time.useUtc),
    end: inputValueToDate(time.endText, time.useUtc),
    fullLoop: time.fullLoop,
    timeline: timeline.value,
    overrides: { ...overrides },
    offset: offsetText.value,
    format: time.format,
  }))

  const problems = computed(() => {
    const found = validateRequest(request.value, inputs.params(), nowMs.value, timing.value)
    // A datetime that can't become an offset (beyond ±10 years, which also covers anything
    // before a long-ago epoch) deserves a clearer message.
    if (time.offsetMode === 'datetime' && time.pretendText && offsetSecondsNow.value === null) {
      return [
        ...found.filter((p) => !p.startsWith('Offset must be')),
        'That “now” is too far from the present (limit ±10 years) or before the channel epoch.',
      ]
    }
    return found
  })
  const kind = computed(() => classify(request.value, nowMs.value))
  // an `epoch` override moves loop 0, so range maths use the effective timing
  const range = computed(() =>
    effectiveRange(request.value, inputs.params(), effectiveTiming(request.value, inputs.params(), timing.value)),
  )

  const kindLabel = computed(() => {
    const params = inputs.params()
    switch (kind.value) {
      case 'catchup':
        return 'Catchup — finished VOD of a past range'
      case 'startover-bounded':
        return 'Startover — live-style, plays from the start and ends at the end time'
      case 'startover-open':
        return `Startover — live-style from the start point, capped at ${formatDuration(params.max_span_seconds)}`
      default: {
        const off = parseOffsetSeconds(offsetText.value) ?? 0
        return off
          ? `Live, as it ${off < 0 ? 'was' : 'will be'} ${formatDuration(Math.abs(off))} ${off < 0 ? 'ago' : 'from now'}${overrideCount.value ? ` with ${overrideCount.value} override${overrideCount.value === 1 ? '' : 's'}` : ''}`
          : overrideCount.value
            ? `Live with ${overrideCount.value} override${overrideCount.value === 1 ? '' : 's'}`
            : 'Live — plain channel URL'
      }
    }
  })

  const summary = computed(() => {
    const r = range.value
    if (!r) return ''
    const fmt = (ms: number) => new Date(ms).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z')
    const loops = fullTiming.value ? ` (${((r.endMs - r.startMs) / fullTiming.value.loopMs).toFixed(1)} loops)` : ''
    const snapped = time.fullLoop && !fullTiming.value ? ' Loop timing unknown until the channel is reachable, so full-loop widening is not shown.' : ''
    return `Serves ${fmt(r.startMs)} → ${fmt(r.endMs)}, ${formatDuration((r.endMs - r.startMs) / 1000)}${loops}. Start and end snap to segment boundaries.${snapped}`
  })

  // each URL carries only the overrides its format reads
  const hlsUrlOut = computed(() => {
    const url = inputs.hlsUrl()
    return url ? buildTimeshiftUrl(url, inputs.params(), request.value, 'hls') : ''
  })
  const dashUrlOut = computed(() => {
    const url = inputs.dashUrl()
    return url ? buildTimeshiftUrl(url, inputs.params(), request.value, 'dash') : ''
  })
  const canPreview = computed(() => problems.value.length === 0 && (!!hlsUrlOut.value || !!dashUrlOut.value))

  // --- time presets -------------------------------------------------------------
  function setRange(start: Date | null, end: Date | null, loop = false) {
    time.startText = dateToInputValue(start, time.useUtc)
    time.endText = dateToInputValue(end, time.useUtc)
    time.fullLoop = loop
  }
  function setNow(which: 'start' | 'end') {
    const text = dateToInputValue(new Date(pretendNow()), time.useUtc)
    if (which === 'start') time.startText = text
    else time.endText = text
  }
  function onZoneChange(utc: boolean) {
    // Keep the same instants when the display zone flips.
    const { start, end } = request.value
    time.useUtc = utc
    const pretend = inputValueToDate(time.pretendText, !utc)
    time.startText = dateToInputValue(start, utc)
    time.endText = dateToInputValue(end, utc)
    if (pretend) time.pretendText = dateToInputValue(pretend, utc)
  }

  return {
    time, overrides, added, request, problems, kind, kindLabel, summary, range,
    overrideCount, clearOverrides, overrideSummary,
    offsetText, offsetSecondsNow, offsetHint, freezeOffset, pretendNow, nowMs, fullTiming,
    hlsUrlOut, dashUrlOut, canPreview,
    setRange, setNow, onZoneChange,
  }
}

export type UrlRequestForm = ReturnType<typeof useUrlRequestForm>
