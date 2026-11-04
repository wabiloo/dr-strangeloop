// The per-request override query parameters of a channel's manifests
// (loop-dee-loop/SCOPE.md §13.8) -- ONE registry, used by
//   * the playback panel (TimeshiftPanel) to offer every override and build URLs, and
//   * the config forms (QpBadge) to mark the settings a request can override.
//
// Names, values and per-format applicability mirror loop-dee-loop/timeshift.py and
// serve.py's `resolve_request_channel`; `paramName` (timeshift.ts) puts the
// channel's prefix in front. The two switches ([query] timeshift / overrides) are
// TOML only and deliberately not parameters.
//
// Pure functions only (no Vue, no DOM) so they can be tested directly.

export type QpFormat = 'hls' | 'dash'
export type QpKind = 'choice' | 'bool' | 'duration' | 'lead' | 'instant' | 'int' | 'text' | 'types'

export interface QpOption {
  label: string
  value: string
}

export interface QpSpec {
  /** The query parameter name (before the channel prefix). */
  name: string
  label: string
  kind: QpKind
  /** The manifest formats that read it (the other ignores it). */
  formats: QpFormat[]
  options?: QpOption[]
  placeholder?: string
  /** One-line explanation (the long one lives in the docs). */
  help: string
}

const BOOL: QpOption[] = [
  { label: 'on', value: 'true' },
  { label: 'off', value: 'false' },
]

/** Every per-request override, in documentation order (matches timeshift.py's
 * OVERRIDE_PARAMS). Startover/catchup parameters (start, end, full_loops, offset)
 * are not here: they have their own controls and their own switch. */
export const QP_SPECS: QpSpec[] = [
  {
    name: 'timeline_mode',
    label: 'Timeline mode',
    kind: 'choice',
    formats: ['hls', 'dash'],
    options: [
      { label: 'continuous (no discontinuity / Period at the loop wrap)', value: 'continuous' },
      { label: 'periodic (a discontinuity / Period at each wrap)', value: 'periodic' },
    ],
    help: 'continuous rewrites timestamps so the loop wrap is seamless; periodic signals one discontinuity (HLS) / Period (DASH) per wrap. continuous is refused if the package cannot support it.',
  },
  {
    name: 'epoch',
    label: 'Channel epoch',
    kind: 'instant',
    formats: ['hls', 'dash'],
    placeholder: 'ISO 8601 or epoch seconds',
    help: 'Replace the epoch (loop 0’s start) for this request: loop numbers, PROGRAM-DATE-TIME, Period starts and availabilityStartTime all follow it. Keep it fixed for a player session. Not in the future.',
  },
  {
    name: 'dvr_window',
    label: 'DVR window',
    kind: 'duration',
    formats: ['hls', 'dash'],
    placeholder: 'e.g. 60 or PT1M',
    help: 'Size of the sliding window: seconds or an ISO 8601 duration, rounded up to whole segments, at most the channel’s maximum span.',
  },
  {
    name: 'timeline_style',
    label: 'DASH SegmentTimeline style',
    kind: 'choice',
    formats: ['dash'],
    options: [
      { label: 'full (@t and @d on every S, a SegmentTemplate per representation)', value: 'full' },
      { label: 'compact (implicit @t, @r runs, one shared SegmentTemplate)', value: 'compact' },
    ],
    help: 'How the DASH SegmentTimeline is written.',
  },
  {
    name: 'addressing',
    label: 'DASH segment addressing',
    kind: 'choice',
    formats: ['dash'],
    options: [
      { label: 'number ($Number$)', value: 'number' },
      { label: 'time ($Time$)', value: 'time' },
    ],
    help: 'DASH SegmentTemplate addressing.',
  },
  {
    name: 'cue_tags',
    label: 'HLS CUE-OUT/CUE-IN tags',
    kind: 'choice',
    formats: ['hls'],
    options: [
      { label: 'none (DATERANGE only)', value: 'none' },
      { label: 'alongside (CUE tags and DATERANGE)', value: 'alongside' },
      { label: 'only (CUE tags instead of DATERANGE)', value: 'only' },
    ],
    help: 'only needs every marker to be a bare splice_insert, otherwise the server answers 400.',
  },
  {
    name: 'descriptor_grouping',
    label: 'Descriptor grouping',
    kind: 'choice',
    formats: ['hls', 'dash'],
    options: [
      { label: 'shared (whole message in each tag)', value: 'shared' },
      { label: 'narrowed (each marker’s own descriptor)', value: 'narrowed' },
      { label: 'grouped (HLS only: one tag per coincident group)', value: 'grouped' },
    ],
    help: 'How SCTE-35 descriptors map to signalling. grouped is HLS only: the DASH URL then keeps the channel setting. A package baked before 2.0 may refuse to switch the payload (400: re-bake).',
  },
  {
    name: 'increment_event_ids',
    label: 'Increment event ids each loop',
    kind: 'bool',
    formats: ['hls', 'dash'],
    options: BOOL,
    help: 'Give every loop iteration its own SCTE-35 event ids.',
  },
  {
    name: 'signal_format',
    label: 'DASH SCTE-35 signal format',
    kind: 'choice',
    formats: ['dash'],
    options: [
      { label: 'binary (<scte35:Binary>)', value: 'binary' },
      { label: 'xml (decoded SpliceInfoSection)', value: 'xml' },
    ],
    help: 'EventStream encoding of the SCTE-35 message.',
  },
  {
    name: 'id_template',
    label: 'HLS DATERANGE ID template',
    kind: 'text',
    formats: ['hls'],
    placeholder: '{segcode}-{eventid}-{loop}',
    help: 'Placeholders: {loop} {eventid} {segid} {seghex} {segcode} {segname} {epoch} {pd}.',
  },
  {
    name: 'split_on',
    label: 'New Period / discontinuity on SCTE-35 types',
    kind: 'types',
    formats: ['hls', 'dash'],
    placeholder: 'e.g. 0x22,0x30 (none to clear)',
    help: 'Comma-separated segmentation_type_ids; a Start implies its End. Replaces the channel setting; none disables it.',
  },
  {
    name: 'announce_ahead',
    label: 'Announce SCTE-35 markers ahead',
    kind: 'lead',
    formats: ['hls', 'dash'],
    placeholder: 'seconds or PT10S (0 = off)',
    help: 'Announce markers at least this long before the segment they fall in (rounded to whole segments, across the loop wrap): the DATERANGE / Event shows up earlier and stays until every segment it covers has left the window. The marker’s own time and CUE-OUT/CUE-IN do not move. 0 turns it off; at most 3600 seconds; not applied to a closed catchup range.',
  },
  {
    name: 'announce_on',
    label: 'Announce only these SCTE-35 markers',
    kind: 'types',
    formats: ['hls', 'dash'],
    placeholder: 'e.g. 0x22,0x30,splice_insert (all = every marker)',
    help: 'Which markers announce_ahead applies to: comma-separated segmentation_type_ids and/or splice_insert. Replaces the channel setting; empty or all = every marker.',
  },
  {
    name: 'max_height',
    label: 'Maximum height',
    kind: 'int',
    formats: ['hls', 'dash'],
    placeholder: 'pixels, e.g. 720',
    help: 'Keep only renditions up to this height. 400 if none is left.',
  },
  {
    name: 'max_bitrate',
    label: 'Maximum bitrate',
    kind: 'int',
    formats: ['hls', 'dash'],
    placeholder: 'bits per second, e.g. 2000000',
    help: 'Keep only renditions up to this bandwidth. 400 if none is left.',
  },
  {
    name: 'low_latency',
    label: 'Low latency',
    kind: 'bool',
    formats: ['hls', 'dash'],
    options: BOOL,
    help: 'Whether this manifest advertises low latency: the MPD (availabilityTimeOffset, a latency target, a clock source) or an HLS media playlist (parts, preload hint, blocking reload). 400 on true if the channel does not deliver low latency (not enabled, no chunks, or MPEG-TS for HLS).',
  },
  {
    name: 'll_target_latency',
    label: 'Low-latency target',
    kind: 'duration',
    formats: ['dash'],
    placeholder: 'seconds, e.g. 2',
    help: 'The latency the low-latency MPD asks players to hold (ServiceDescription / Latency). At most 120 seconds; has no effect unless the MPD advertises low latency.',
  },
  {
    name: 'll_utc_timing',
    label: 'Low-latency clock sync',
    kind: 'choice',
    formats: ['dash'],
    options: [
      { label: 'direct (the time in the MPD, served no-store)', value: 'direct' },
      { label: 'http (a polled /time endpoint)', value: 'http' },
      { label: 'none (the player’s own clock)', value: 'none' },
    ],
    help: 'How a low-latency MPD lets players sync their clock. The UTCTiming URL itself is configuration only, not a parameter.',
  },
]

export const QP_NAMES = QP_SPECS.map((s) => s.name)

export function qpSpec(name: string): QpSpec {
  const spec = QP_SPECS.find((s) => s.name === name)
  if (!spec) throw new Error(`unknown query parameter ${name}`)
  return spec
}

/** The channel-form settings a request can override, and the parameter each maps to.
 * Keys are the form/API field names (see channelConfigLayout.ts for their TOML names). */
export const FIELD_OVERRIDES: Record<string, { name: string; formats: QpFormat[] }> = {
  continuous: { name: 'timeline_mode', formats: ['hls', 'dash'] },
  dvr_window_seconds: { name: 'dvr_window', formats: ['hls', 'dash'] },
  epoch_utc: { name: 'epoch', formats: ['hls', 'dash'] },
  period_on_segmentation: { name: 'split_on', formats: ['hls', 'dash'] },
  announce_ahead: { name: 'announce_ahead', formats: ['hls', 'dash'] },
  announce_on: { name: 'announce_on', formats: ['hls', 'dash'] },
  dash_addressing: { name: 'addressing', formats: ['dash'] },
  dash_timeline: { name: 'timeline_style', formats: ['dash'] },
  dash_low_latency: { name: 'low_latency', formats: ['dash'] },
  hls_low_latency: { name: 'low_latency', formats: ['hls'] },
  target_latency: { name: 'll_target_latency', formats: ['dash'] },
  utc_timing: { name: 'll_utc_timing', formats: ['dash'] },
  daterange_mode: { name: 'descriptor_grouping', formats: ['hls'] },
  dash_descriptor_mode: { name: 'descriptor_grouping', formats: ['dash'] },
  cue_tags: { name: 'cue_tags', formats: ['hls'] },
  increment_event_ids: { name: 'increment_event_ids', formats: ['hls', 'dash'] },
  daterange_id_format: { name: 'id_template', formats: ['hls'] },
  dash_signal_format: { name: 'signal_format', formats: ['dash'] },
}

/** The override a form field maps to, or undefined when a request cannot change it. */
export function overrideForField(field: string): { name: string; formats: QpFormat[] } | undefined {
  return FIELD_OVERRIDES[field]
}

/** Raw text typed per override ('' / missing = not set). */
export type OverrideValues = Record<string, string | undefined>

/** Whether `spec` applies to `fmt` -- and, for descriptor_grouping, whether this
 * VALUE does (`grouped` has no DASH form: the server answers 400, so it is left
 * off the DASH URL and the channel setting applies). */
export function appliesTo(spec: QpSpec, fmt: QpFormat, value?: string): boolean {
  if (!spec.formats.includes(fmt)) return false
  return !(spec.name === 'descriptor_grouping' && fmt === 'dash' && value === 'grouped')
}

/** `[name, value]` pairs of the overrides set in `values` that apply to `fmt`,
 * in registry order (the order they appear in a URL). */
export function overridePairs(values: OverrideValues | undefined, fmt: QpFormat): [string, string][] {
  const out: [string, string][] = []
  for (const spec of QP_SPECS) {
    const raw = values?.[spec.name]?.trim()
    if (raw === undefined || raw === '') continue
    if (appliesTo(spec, fmt, raw)) out.push([spec.name, raw])
  }
  return out
}

// ── validation (mirrors the server so the UI explains instead of showing a bare 400) ──

const MS_THRESHOLD = 1e11
const ID_FIELDS = new Set(['loop', 'eventid', 'segid', 'seghex', 'segcode', 'segname', 'epoch', 'pd'])

/** A positive length of time in seconds (`30`, `7.5`, `PT30S`, `PT1H30M`, `P1D`; the grammar serve.py accepts), or null. */
export function parseDurationSeconds(text: string): number | null {
  const raw = text.trim()
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const n = Number(raw)
    return n > 0 ? n : null
  }
  const m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/i.exec(raw)
  if (!m || !m.slice(1).some((g) => g !== undefined)) return null
  const [d, h, mi, s] = [m[1], m[2], m[3], m[4]].map((g) => (g ? Number(g) : 0))
  const seconds = d * 86400 + h * 3600 + mi * 60 + s
  return seconds > 0 ? seconds : null
}

/** An instant (epoch seconds, epoch ms >= 1e11, or ISO 8601; no zone = UTC) in ms, or null. */
export function parseInstantMs(text: string): number | null {
  const raw = text.trim()
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const n = Number(raw)
    return (n >= MS_THRESHOLD ? n : n * 1000)
  }
  if (!/^\d{4}-\d{2}-\d{2}/.test(raw)) return null
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(raw)
  const ms = Date.parse(hasZone ? raw : `${raw}Z`)
  return Number.isNaN(ms) ? null : ms
}

/** The segmentation_type_ids in a `split_on` value, or null when it is malformed.
 * Empty / `none` give []. */
export function parseSplitOn(text: string): number[] | null {
  const ids: number[] = []
  for (const token of text.split(',')) {
    const t = token.trim()
    if (t === '' || t.toLowerCase() === 'none') continue
    if (!/^(0x[0-9a-f]+|\d+)$/i.test(t)) return null
    const n = Number(t)
    if (!Number.isInteger(n) || n < 0 || n > 0xff) return null
    ids.push(n)
  }
  return ids
}

/** The announced marker kinds in an `announce_on` value -- segmentation_type_ids and/or `splice_insert` -- or null
 * when malformed. Empty / `all` give [] (every marker). */
export function parseAnnounceOn(text: string): (number | 'splice_insert')[] | null {
  const out: (number | 'splice_insert')[] = []
  for (const token of text.split(',')) {
    const t = token.trim().toLowerCase()
    if (t === '' || t === 'all') continue
    if (t === 'splice_insert') {
      out.push('splice_insert')
      continue
    }
    if (t === 'none') return null
    const ids = parseSplitOn(t)
    if (ids === null || ids.length !== 1) return null
    out.push(ids[0])
  }
  return out
}

export const MAX_ANNOUNCE_AHEAD = 3600

/** Seconds for an `announce_ahead` value (`0` = off, `10`, `PT10S`), or null when malformed or above 3600. */
export function parseAnnounceAhead(text: string): number | null {
  const raw = text.trim()
  if (/^0+(\.0*)?$/.test(raw)) return 0
  const s = parseDurationSeconds(raw)
  return s === null || s > MAX_ANNOUNCE_AHEAD ? null : s
}

export interface ValidateContext {
  /** The channel's max span (also caps dvr_window). */
  maxSpanSeconds: number
  /** What the server will treat as "now" (real now + offset), in ms. */
  nowMs: number
}

/** Problems the server would answer with HTTP 400, found up front. Empty = OK.
 * Only the values that are set are checked. */
export function validateOverrides(values: OverrideValues | undefined, ctx: ValidateContext): string[] {
  const problems: string[] = []
  for (const spec of QP_SPECS) {
    const raw = values?.[spec.name]?.trim()
    if (raw === undefined || raw === '') continue
    switch (spec.kind) {
      case 'choice':
      case 'bool':
        if (!spec.options!.some((o) => o.value === raw.toLowerCase())) {
          problems.push(`${spec.label}: “${raw}” is not one of ${spec.options!.map((o) => o.value).join(', ')}.`)
        }
        break
      case 'duration': {
        const s = parseDurationSeconds(raw)
        if (s === null) problems.push(`${spec.label} must be a number of seconds (60) or an ISO 8601 duration (PT1M).`)
        else if (s > ctx.maxSpanSeconds) problems.push(`${spec.label} is limited to ${ctx.maxSpanSeconds} seconds on this channel.`)
        break
      }
      case 'instant': {
        const ms = parseInstantMs(raw)
        if (ms === null) problems.push(`${spec.label} must be epoch seconds/milliseconds or an ISO 8601 time.`)
        else if (ms > ctx.nowMs) problems.push(`${spec.label} is in the future (nothing existed yet).`)
        break
      }
      case 'int':
        if (!/^\d+$/.test(raw) || Number(raw) < 1) problems.push(`${spec.label} must be a positive whole number.`)
        break
      case 'types':
        if (spec.name === 'announce_on') {
          if (parseAnnounceOn(raw) === null) {
            problems.push(`${spec.label} must be comma-separated segmentation_type_ids (0x22,0x30), splice_insert, or all.`)
          }
        } else if (parseSplitOn(raw) === null) {
          problems.push(`${spec.label} must be comma-separated segmentation_type_ids (0x22,0x30) or none.`)
        }
        break
      case 'lead':
        if (parseAnnounceAhead(raw) === null) {
          problems.push(`${spec.label} must be 0 (off) or a number of seconds (10) / ISO 8601 duration (PT10S), at most ${MAX_ANNOUNCE_AHEAD} seconds.`)
        }
        break
      case 'text':
        if (spec.name === 'id_template') {
          const unknown = [...raw.matchAll(/\{([^{}]*)\}/g)].map((m) => m[1]).filter((f) => !ID_FIELDS.has(f))
          const unbalanced = (raw.match(/\{/g)?.length ?? 0) !== (raw.match(/\}/g)?.length ?? 0)
          if (unknown.length) problems.push(`${spec.label}: unknown placeholder {${unknown[0]}}.`)
          else if (unbalanced) problems.push(`${spec.label}: unbalanced braces.`)
        }
        break
    }
  }
  return problems
}
