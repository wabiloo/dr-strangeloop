// Startover / catchup URL generation (loop-dee-loop/SCOPE.md §13).
//
// A channel serves time-shifted playback from its NORMAL manifest URLs via
// query parameters whose names are configurable per channel ([timeshift] in
// the channel TOML). This module is the single place that turns a
// user-facing request (start, optional end, full-loop, timeline override)
// into those URLs, and pre-validates it the same way serve.py will, so the
// UI can explain a problem instead of showing a bare HTTP 400.
//
// Pure functions only (no Vue, no DOM) so they can be exercised directly.

export interface TimeshiftParams {
  enabled: boolean
  start_param: string
  end_param: string
  max_span_seconds: number
}

export const DEFAULT_TIMESHIFT_PARAMS: TimeshiftParams = {
  enabled: true,
  start_param: 'start',
  end_param: 'end',
  max_span_seconds: 21600,
}

export type TimeFormat = 'iso' | 'epoch' | 'epoch_ms'
/** Fixed (not configurable) name of the per-request timeline-mode override,
 * `timeline=default|continuous|periodic`; absent == `default`, i.e. the
 * channel's own continuous-timeline setting. Mirrors loop-dee-loop's
 * timeshift.TIMELINE_PARAM. */
export const TIMELINE_PARAM = 'timeline'
/** Also fixed: `full-loops=true` widens the range to whole loops. */
export const FULL_LOOPS_PARAM = 'full-loops'
/** Also fixed: `offset=` pretends "now" is earlier (negative) or later
 * (positive): the live edge, and what "past"/"future" mean for start/end,
 * move with it. Signed seconds or an ISO 8601 duration. */
export const OFFSET_PARAM = 'offset'
export type TimelineChoice = 'default' | 'continuous' | 'periodic'

export interface TimeshiftRequest {
  start: Date | null
  end: Date | null
  fullLoop: boolean
  timeline: TimelineChoice
  /** Raw text typed for `offset` ('' = none): signed seconds or ISO 8601 duration. */
  offset: string
  format: TimeFormat
}

/** What the channel reports about itself (serve.py /health), all optional:
 * without it the loop-aware helpers and some validation are skipped. */
export interface ChannelTiming {
  epochMs?: number
  loopMs?: number
}

export type TimeshiftKind = 'live' | 'catchup' | 'startover-bounded' | 'startover-open'

/** Merge a parsed [timeshift] TOML table over the defaults. A missing table
 * means "defaults, enabled" -- same as its-a-live's own defaulting. */
export function timeshiftParamsFromConfig(table: Record<string, unknown> | undefined): TimeshiftParams {
  const t = table ?? {}
  const str = (key: keyof TimeshiftParams) =>
    typeof t[key] === 'string' && t[key] !== '' ? (t[key] as string) : String(DEFAULT_TIMESHIFT_PARAMS[key])
  return {
    enabled: typeof t.enabled === 'boolean' ? t.enabled : DEFAULT_TIMESHIFT_PARAMS.enabled,
    start_param: str('start_param'),
    end_param: str('end_param'),
    max_span_seconds:
      typeof t.max_span_seconds === 'number' && t.max_span_seconds >= 1
        ? t.max_span_seconds
        : DEFAULT_TIMESHIFT_PARAMS.max_span_seconds,
  }
}

/** One instant in the requested wire format. */
export function formatInstant(d: Date, format: TimeFormat): string {
  switch (format) {
    case 'epoch':
      return String(Math.floor(d.getTime() / 1000))
    case 'epoch_ms':
      return String(d.getTime())
    case 'iso':
      // Drop a zero millisecond part: 2026-01-01T00:00:00Z reads better
      // than ...00.000Z and serve.py accepts both.
      return d.toISOString().replace(/\.000Z$/, 'Z')
  }
}

// encodeURIComponent, but keep ':' readable in ISO8601 timestamps.
function encodeValue(v: string): string {
  return encodeURIComponent(v).replace(/%3A/g, ':')
}

/** The query string (no leading '?') for a request; '' for plain live. */
export function buildTimeshiftQuery(params: TimeshiftParams, req: TimeshiftRequest): string {
  const parts: string[] = []
  if (req.start) {
    parts.push(`${encodeURIComponent(params.start_param)}=${encodeValue(formatInstant(req.start, req.format))}`)
    if (req.end) {
      parts.push(`${encodeURIComponent(params.end_param)}=${encodeValue(formatInstant(req.end, req.format))}`)
    }
    if (req.fullLoop) parts.push(`${FULL_LOOPS_PARAM}=true`)
  }
  if (req.timeline !== 'default') parts.push(`${TIMELINE_PARAM}=${req.timeline}`)
  const offsetSeconds = parseOffsetSeconds(req.offset)
  // Normalised to plain signed seconds: no '+' to get mangled in a URL.
  if (typeof offsetSeconds === 'number' && offsetSeconds !== 0) parts.push(`${OFFSET_PARAM}=${offsetSeconds}`)
  return parts.join('&')
}

/** `baseUrl` (a channel's normal HLS or DASH URL) plus the request. Any
 * existing query on baseUrl is preserved. */
export function buildTimeshiftUrl(baseUrl: string, params: TimeshiftParams, req: TimeshiftRequest): string {
  const query = buildTimeshiftQuery(params, req)
  if (!query) return baseUrl
  return baseUrl + (baseUrl.includes('?') ? '&' : '?') + query
}

const MAX_ABS_OFFSET_SECONDS = 10 * 365 * 86400 // same sanity bound as serve.py

const ISO_DURATION = /^([+-])?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(\d+(?:\.\d+)?S)?)?$/i

/** Same grammar as serve.py: signed seconds (`-3600`) or an ISO 8601
 * duration with optional sign (`-PT1H`, `P1DT2H`; days and below).
 * '' -> 0; unparseable -> null. */
export function parseOffsetSeconds(text: string): number | null {
  const raw = text.trim()
  if (raw === '') return 0
  let seconds: number
  if (/^[+-]?\d+(\.\d+)?$/.test(raw)) {
    seconds = Number(raw)
  } else {
    const m = ISO_DURATION.exec(raw)
    if (!m || !(m[2] || m[3] || m[4] || m[5])) return null
    const sign = m[1] === '-' ? -1 : 1
    seconds = sign * (Number(m[2] ?? 0) * 86400 + Number(m[3] ?? 0) * 3600 + Number(m[4] ?? 0) * 60 + parseFloat(m[5] ?? '0'))
  }
  return Number.isFinite(seconds) && Math.abs(seconds) <= MAX_ABS_OFFSET_SECONDS ? seconds : null
}

/** Signed seconds -> an ISO 8601 duration `parseOffsetSeconds` reads back
 * (`-PT1H2M3S`, `P1DT2H`, `PT0S`). Whole seconds only. */
export function formatIsoDuration(seconds: number): string {
  const total = Math.round(Math.abs(seconds))
  const d = Math.floor(total / 86400)
  const h = Math.floor((total % 86400) / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const time = `${h ? `${h}H` : ''}${m ? `${m}M` : ''}${s ? `${s}S` : ''}`
  const body = `${d ? `${d}D` : ''}${time ? `T${time}` : ''}`
  return body ? `${seconds < 0 ? '-' : ''}P${body}` : 'PT0S'
}

/** Whole-second offset that makes "now" equal `target` when the stream is
 * started at `startedAtMs`. Used when the user picks a pretend-"now"
 * datetime instead of a duration: the offset is fixed at start time and the
 * stream then advances in real time from there. */
export function offsetSecondsForTarget(target: Date, startedAtMs: number): number {
  return Math.round((target.getTime() - startedAtMs) / 1000) || 0 // `|| 0`: no -0
}

/** The time the server will treat as "now" for this request. */
export function pretendNowMs(req: TimeshiftRequest, nowMs: number): number {
  return nowMs + (parseOffsetSeconds(req.offset) ?? 0) * 1000
}

export function classify(req: TimeshiftRequest, nowMs: number): TimeshiftKind {
  if (!req.start) return 'live'
  if (!req.end) return 'startover-open'
  return req.end.getTime() <= pretendNowMs(req, nowMs) ? 'catchup' : 'startover-bounded'
}

export function floorToLoopMs(t: number, timing: Required<ChannelTiming>): number {
  return timing.epochMs + Math.floor((t - timing.epochMs) / timing.loopMs) * timing.loopMs
}

export function ceilToLoopMs(t: number, timing: Required<ChannelTiming>): number {
  return timing.epochMs + Math.ceil((t - timing.epochMs) / timing.loopMs) * timing.loopMs
}

function hasTiming(t: ChannelTiming | undefined): t is Required<ChannelTiming> {
  return !!t && Number.isFinite(t.epochMs) && Number.isFinite(t.loopMs) && (t.loopMs as number) > 0
}

/** The range serve.py will actually serve, to within segment snapping: the
 * requested range, widened to whole loops when `fullLoop` and the channel's
 * loop timing is known; an open-ended start is capped at start+max span.
 * null for plain live. */
export function effectiveRange(
  req: TimeshiftRequest,
  params: TimeshiftParams,
  timing?: ChannelTiming,
): { startMs: number; endMs: number; capped: boolean } | null {
  if (!req.start) return null
  let startMs = req.start.getTime()
  let endMs = req.end ? req.end.getTime() : null
  const capped = endMs === null // open-ended: serve.py applies the max-span cap
  const maxMs = params.max_span_seconds * 1000
  if (req.fullLoop && hasTiming(timing)) {
    startMs = floorToLoopMs(startMs, timing)
    if (endMs !== null) endMs = ceilToLoopMs(endMs, timing)
    else endMs = startMs + Math.floor(maxMs / timing.loopMs) * timing.loopMs
  }
  if (endMs === null) endMs = startMs + maxMs
  return { startMs, endMs, capped }
}

/** Problems serve.py would reject (HTTP 400), found up front. Empty = OK. */
export function validateRequest(
  req: TimeshiftRequest,
  params: TimeshiftParams,
  nowMs: number,
  timing?: ChannelTiming,
): string[] {
  const problems: string[] = []
  if (parseOffsetSeconds(req.offset) === null) {
    problems.push('Offset must be signed seconds (-3600) or an ISO 8601 duration (-PT1H), within ±10 years.')
  }
  nowMs = pretendNowMs(req, nowMs)
  if (timing && Number.isFinite(timing.epochMs) && nowMs < (timing.epochMs as number)) {
    problems.push('That offset puts “now” before the channel epoch (nothing existed yet).')
  }
  if (req.end && !req.start) problems.push(`An end time needs a start time (“${params.end_param}” requires “${params.start_param}”).`)
  if (!req.start) return problems
  if (Number.isNaN(req.start.getTime())) problems.push('Start is not a valid date.')
  if (req.end && Number.isNaN(req.end.getTime())) problems.push('End is not a valid date.')
  if (problems.length) return problems
  if (req.start.getTime() > nowMs) problems.push('Start is in the future.')
  if (req.end && req.end.getTime() <= req.start.getTime()) problems.push('End must be after start.')
  if (timing && Number.isFinite(timing.epochMs) && req.start.getTime() < (timing.epochMs as number)) {
    problems.push('Start is before the channel epoch (nothing existed yet).')
  }
  const range = effectiveRange(req, params, timing)
  if (range && req.end) {
    const spanS = (range.endMs - range.startMs) / 1000
    if (spanS > params.max_span_seconds) {
      const widened = req.fullLoop && hasTiming(timing) ? ' (after widening to whole loops)' : ''
      problems.push(`Range is ${formatDuration(spanS)}${widened}; this channel allows at most ${formatDuration(params.max_span_seconds)}.`)
    }
  }
  if (req.fullLoop && hasTiming(timing) && !req.end) {
    if (Math.floor((params.max_span_seconds * 1000) / timing.loopMs) < 1) {
      problems.push('One loop is longer than this channel’s maximum span.')
    }
  }
  return problems
}

export function formatDuration(seconds: number): string {
  const s = Math.round(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const parts: string[] = []
  if (h) parts.push(`${h}h`)
  if (m) parts.push(`${m}m`)
  if (sec || !parts.length) parts.push(`${sec}s`)
  return parts.join(' ')
}

/** `<input type="datetime-local">` value <-> Date, in local time or UTC. */
export function dateToInputValue(d: Date | null, utc: boolean): string {
  if (!d || Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const y = utc ? d.getUTCFullYear() : d.getFullYear()
  const mo = (utc ? d.getUTCMonth() : d.getMonth()) + 1
  const da = utc ? d.getUTCDate() : d.getDate()
  const h = utc ? d.getUTCHours() : d.getHours()
  const mi = utc ? d.getUTCMinutes() : d.getMinutes()
  const s = utc ? d.getUTCSeconds() : d.getSeconds()
  return `${y}-${pad(mo)}-${pad(da)}T${pad(h)}:${pad(mi)}:${pad(s)}`
}

export function inputValueToDate(value: string, utc: boolean): Date | null {
  if (!value) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value)
  if (!m) return null
  const [y, mo, da, h, mi, s] = [m[1], m[2], m[3], m[4], m[5], m[6] ?? '0'].map(Number)
  const d = utc ? new Date(Date.UTC(y, mo - 1, da, h, mi, s)) : new Date(y, mo - 1, da, h, mi, s)
  return Number.isNaN(d.getTime()) ? null : d
}
