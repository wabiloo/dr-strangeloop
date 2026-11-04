// One layout for channel configuration, shared by the New channel form, the
// read-only Configuration panel and the Configuration edit form on the
// channel detail page: same sections, same order, same field labels.
//
// NAMING RULE (keep it): a section here IS one its-a-live TOML table. Its id
// is the table name and its title is that name, capitalised ([timeline] ->
// "Timeline"; [infrastructure.aws] -> "Infrastructure · AWS"; [markers.hls]
// -> "Markers · HLS"). A field is shown in the section of the table it is stored in,
// never anywhere else. To add a setting, put it in the table it belongs to
// (or add a new table + section together); to rename a table, rename the
// section id/title, the New/Edit templates and the docs in the same change.
// See igor/AGENTS.md "Channel config sections".

import type { ChannelCreatePayload } from '../api/types'
import { DEFAULT_EPOCH_UTC } from './epoch.ts'
import { timeshiftParamsFromConfig } from './timeshift.ts'

/** The channel TOML layout this Igor reads and writes (`config_version`, absent = 1).
 * Mirrors its-a-live/_legacy_cfg.py CONFIG_VERSION (a test keeps them equal). */
export const CONFIG_VERSION = 2

/** A channel TOML's `config_version` (1 when the key is absent or not an integer). */
export function configVersionOf(config: Record<string, unknown> | null | undefined): number {
  const v = config?.config_version
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 ? v : 1
}

export type ConfigBackend = 'aws-media' | 'ecs-express' | 'aws-lambda' | 'local-docker'
export type ConfigSourceKind = 'playlist' | 'archive' | 'manifest'

// Display order: identity ([deploy], [input]), infrastructure
// ([infrastructure.aws], .s3, .express or .docker), then what gets baked and
// signaled.
export const CONFIG_SECTION_TITLE = {
  deploy: 'Deploy',
  input: 'Input',
  'infrastructure.aws': 'Infrastructure · AWS',
  'infrastructure.s3': 'Infrastructure · S3',
  'infrastructure.express': 'Infrastructure · Express',
  'infrastructure.lambda': 'Infrastructure · Lambda',
  'infrastructure.docker': 'Infrastructure · Docker',
  timeline: 'Timeline',
  packaging: 'Packaging',
  'packaging.hls': 'Packaging · HLS',
  'packaging.dash': 'Packaging · DASH',
  query: 'Query',
  timeshift: 'Timeshift',
  markers: 'Markers',
  'markers.hls': 'Markers · HLS',
  'markers.dash': 'Markers · DASH',
} as const

export type ConfigSectionId = keyof typeof CONFIG_SECTION_TITLE

// Keys are the channel form's field names, which still differ from the TOML
// option names in places (e.g. `hls_format` is [packaging.hls] `container`):
// `formFieldsFromConfig` below is the one mapping between the two.
export const CONFIG_FIELD_LABEL = {
  name: 'Channel name',
  backend: 'Backend',
  source_kind: 'Source kind',
  source_path: 'Source path',
  allow_missing_segments: 'Allow missing segments',
  region: 'AWS region',
  bucket_name: 'S3 bucket name',
  content_folder: 'S3 content folder',
  continuous: 'Continuous timeline across the loop wrap',
  period_on_segmentation: 'New Period / discontinuity on SCTE-35 segmentations',
  timeshift_enabled: 'Enable startover & catchup (timeshift query parameters)',
  query_overrides: 'Allow per-request overrides of the channel settings',
  query_prefix: 'Prefix for every query parameter',
  timeshift_start_param: 'Start parameter',
  timeshift_end_param: 'End parameter',
  timeshift_max_span_seconds: 'Maximum range (s)',
  segment_duration: 'Segment duration (s)',
  dvr_window_seconds: 'DVR window (s)',
  epoch_utc: 'Channel epoch (UTC)',
  hls_format: 'HLS container',
  hls_ts_mux_audio: 'Mux audio into each HLS TS video segment',
  dash_addressing: 'DASH segment addressing',
  dash_timeline: 'DASH SegmentTimeline style',
  dash_low_latency: 'Low-latency DASH',
  hls_low_latency: 'Low-latency HLS',
  chunk_duration: 'Chunk duration (s)',
  target_latency: 'Target latency (s)',
  utc_timing: 'Clock sync (UTCTiming)',
  utc_timing_url: 'UTCTiming URL',
  port: 'Serve port',
  bundle_content: 'Bundle content into the Docker image',
  cpu: 'Express CPU units',
  memory: 'Express memory (MB)',
  cdn: 'CloudFront CDN',
  lambda_memory: 'Lambda memory (MB)',
  lambda_timeout: 'Lambda timeout (s)',
  lambda_reserved_concurrency: 'Lambda reserved concurrency (-1 = none)',
  lambda_cache_mb: 'Lambda segment cache (MB)',
  lambda_architecture: 'Lambda architecture',
  daterange_mode: 'HLS descriptor grouping',
  cue_tags: 'HLS CUE-OUT/CUE-IN tags',
  dash_signal_format: 'DASH SCTE-35 signal format',
  dash_descriptor_mode: 'DASH descriptor grouping',
  increment_event_ids: 'Increment SCTE-35 event ids each loop (HLS + DASH)',
  daterange_id_format: 'HLS DATERANGE ID template',
} as const

export type ConfigFieldKey = keyof typeof CONFIG_FIELD_LABEL

// aws-media has no packaging/serving/SCTE-35 settings -- MediaLive/
// MediaPackage own those; ecs-express, aws-lambda and local-docker bake+serve.
export const LOW_LATENCY_BACKENDS: string[] = ['ecs-express', 'local-docker']
export const DEFAULT_CHUNK_DURATION = 0.5
export const MAX_TARGET_LATENCY = 120

/** A `low_latency` setting as the form's choice: the TOML booleans are accepted too (true = on). */
export function lowLatencyFromConfig(value: unknown): NonNullable<ChannelCreatePayload['dash_low_latency']> {
  if (value === true) return 'on'
  if (value === false || value === undefined || value === null) return 'off'
  return value === 'on' || value === 'optin' ? value : 'off'
}

type LowLatencyForm = Pick<
  ChannelCreatePayload,
  | 'backend' | 'dash_low_latency' | 'hls_low_latency' | 'hls_format' | 'segment_duration' | 'chunk_duration'
  | 'target_latency' | 'utc_timing' | 'utc_timing_url' | 'query_overrides'
>

/** Which form group a low-latency problem belongs to (where the component shows it). */
export type LowLatencyPart = 'packaging' | 'hls' | 'dash'

/** Why a low-latency combination would be refused (empty = fine), and in which group of the form. Mirrors the
 * server's validate_low_latency (igor/src/igor/integrations/its_a_live.py, itself mirroring
 * its-a-live/_packaging_cfg.py) so the form can say so before saving. The utc_timing / target rules apply whether or
 * not low latency is on, like the server's. */
export function lowLatencyIssues(f: LowLatencyForm): { part: LowLatencyPart; message: string }[] {
  const out: { part: LowLatencyPart; message: string }[] = []
  const add = (part: LowLatencyPart, message: string) => out.push({ part, message })
  const url = (f.utc_timing_url ?? '').trim()
  if (f.target_latency != null && !(f.target_latency > 0 && f.target_latency <= MAX_TARGET_LATENCY)) {
    add('dash', `Target latency must be above 0 and at most ${MAX_TARGET_LATENCY} seconds.`)
  }
  if (url) {
    if (f.utc_timing !== 'http') add('dash', 'A UTCTiming URL only applies to clock sync "http".')
    else if (!/^https?:\/\//.test(url)) add('dash', 'The UTCTiming URL must be an absolute http(s) URL (DASH does not allow a relative one).')
  }
  const dash = f.dash_low_latency ?? 'off'
  const hls = f.hls_low_latency ?? 'off'
  if (dash === 'off' && hls === 'off') return out
  if (!LOW_LATENCY_BACKENDS.includes(f.backend)) {
    add(dash !== 'off' ? 'dash' : 'hls', `Low latency is not supported on the ${f.backend} backend (only ${LOW_LATENCY_BACKENDS.join(', ')}).`)
  }
  const seg = f.segment_duration ?? 4
  const chunk = f.chunk_duration ?? DEFAULT_CHUNK_DURATION
  if (!(chunk > 0 && chunk < seg)) add('packaging', `Chunk duration must be above 0 and shorter than the segment duration (${seg} s).`)
  for (const [part, mode] of [['dash', dash], ['hls', hls]] as const) {
    if (mode === 'optin' && f.query_overrides === false) {
      add(part, 'Opt-in needs per-request overrides enabled (the viewer asks with ?low_latency=true).')
    }
  }
  if (hls !== 'off' && f.hls_format !== 'cmaf') add('hls', 'Low-latency HLS needs the CMAF container (MPEG-TS segments cannot carry parts).')
  return out
}

export function lowLatencyProblems(f: LowLatencyForm): string[] {
  return lowLatencyIssues(f).map((i) => i.message)
}

export function usesChannelSection(backend: string | undefined): boolean {
  return backend === 'ecs-express' || backend === 'aws-lambda' || backend === 'local-docker'
}

/** The aws-lambda form's settings read from `[infrastructure.lambda]`, with
 * the same defaults its-a-live applies for a missing key:
 *
 *   [infrastructure.lambda] memory, timeout, reserved_concurrency, cache_mb, architecture
 *                              -> lambda_memory, lambda_timeout, lambda_reserved_concurrency,
 *                                 lambda_cache_mb, lambda_architecture
 */
export function lambdaFieldsFromConfig(config: TomlConfig): Required<
  Pick<
    ChannelCreatePayload,
    'lambda_memory' | 'lambda_timeout' | 'lambda_reserved_concurrency' | 'lambda_cache_mb' | 'lambda_architecture'
  >
> {
  const lambda = infraTable(config, 'lambda')
  return {
    lambda_memory: Number(lambda.memory ?? 1024),
    lambda_timeout: Number(lambda.timeout ?? 30),
    lambda_reserved_concurrency: Number(lambda.reserved_concurrency ?? 10),
    lambda_cache_mb: Number(lambda.cache_mb ?? 512),
    lambda_architecture: (lambda.architecture as ChannelCreatePayload['lambda_architecture']) ?? 'arm64',
  }
}

// input.source_kind is missing in configs created before it existed; a
// manifest.json source path means an archive segment-list.
export function deriveSourceKind(input: Record<string, unknown> | undefined): ConfigSourceKind {
  if (input?.source_kind === 'manifest') return 'manifest'
  const path = String(input?.source_path ?? '')
  if (input?.source_kind === 'archive' || /(?:^|[\\/])manifest\.json$/i.test(path)) return 'archive'
  return 'playlist'
}

// `key` doubles as the TOML option name (form field names match the TOML keys).
export interface ConfigRow {
  key: ConfigFieldKey
  label: string
  value: string
}

export interface ConfigSection {
  id: ConfigSectionId
  title: string
  rows: ConfigRow[]
}

type TomlConfig = Record<string, unknown>

function table(config: TomlConfig, name: string): Record<string, unknown> {
  const t = config[name]
  return t && typeof t === 'object' ? (t as Record<string, unknown>) : {}
}

function nested(config: TomlConfig, ...path: string[]): Record<string, unknown> {
  return path.reduce<TomlConfig>((node, key) => table(node, key), config)
}

/** `[infrastructure.<name>]` -- aws, s3, express, lambda or docker. */
export function infraTable(config: TomlConfig, name: 'aws' | 's3' | 'express' | 'lambda' | 'docker'): Record<string, unknown> {
  return table(table(config, 'infrastructure'), name)
}

/** TOML `[markers] split_on` (ints or strings) -> hex strings ("0x22"),
 * the form the channel form and API use. */
export function periodTypesFromConfig(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => (typeof item === 'number' ? item : Number(item)))
    .filter((n) => Number.isInteger(n) && n >= 0 && n <= 0xff)
    .map((n) => `0x${n.toString(16).toUpperCase().padStart(2, '0')}`)
}

/** The channel form's settings read from a channel's parsed TOML, with the
 * same defaults its-a-live applies for a missing key. The ONE mapping between
 * the TOML layout and the form/API field names (used by the Edit form and the
 * "duplicate to ecs-express" prefill):
 *
 *   [packaging] dvr_window                     -> dvr_window_seconds
 *   [packaging.hls] container, mux_audio       -> hls_format, hls_ts_mux_audio
 *   [packaging.dash] timeline_style, addressing -> dash_timeline, dash_addressing
 *   [packaging] chunk_duration                  -> chunk_duration
 *   [packaging.hls] low_latency                 -> hls_low_latency
 *   [packaging.dash] low_latency                -> dash_low_latency
 *   [packaging.dash] target_latency, utc_timing, utc_timing_url
 *                                              -> the same names (no older flat name to keep)
 *   [timeline] epoch                           -> epoch_utc
 *   [markers] split_on                         -> period_on_segmentation
 *   [markers.hls] descriptor_grouping, cue_tags, id_template
 *                                              -> daterange_mode, cue_tags, daterange_id_format
 *   [markers.dash] descriptor_grouping, signal_format
 *                                              -> dash_descriptor_mode, dash_signal_format
 *   [query] timeshift, overrides, prefix, start_param, end_param
 *                                              -> timeshift_enabled, query_overrides, query_prefix, timeshift_*_param
 */
export function formFieldsFromConfig(config: TomlConfig): Required<
  Pick<
    ChannelCreatePayload,
    | 'segment_duration' | 'dvr_window_seconds' | 'hls_format' | 'hls_ts_mux_audio' | 'dash_addressing'
    | 'dash_timeline' | 'epoch_utc' | 'continuous' | 'period_on_segmentation' | 'timeshift_enabled'
    | 'query_overrides' | 'query_prefix' | 'timeshift_start_param' | 'timeshift_end_param' | 'timeshift_max_span_seconds'
    | 'daterange_mode' | 'cue_tags' | 'increment_event_ids' | 'daterange_id_format' | 'dash_signal_format'
    | 'dash_descriptor_mode' | 'dash_low_latency' | 'hls_low_latency' | 'chunk_duration' | 'target_latency'
    | 'utc_timing' | 'utc_timing_url'
  >
> {
  const packaging = table(config, 'packaging')
  const hls = nested(config, 'packaging', 'hls')
  const dash = nested(config, 'packaging', 'dash')
  const timeline = table(config, 'timeline')
  const markers = table(config, 'markers')
  const markersHls = nested(config, 'markers', 'hls')
  const markersDash = nested(config, 'markers', 'dash')
  const query = timeshiftParamsFromConfig(config)
  return {
    segment_duration: Number(packaging.segment_duration ?? 4.0),
    dvr_window_seconds: Number(packaging.dvr_window ?? 30),
    hls_format: (hls.container as ChannelCreatePayload['hls_format']) ?? 'cmaf',
    hls_ts_mux_audio: Boolean(hls.mux_audio ?? true),
    dash_addressing: (dash.addressing as ChannelCreatePayload['dash_addressing']) ?? 'number',
    dash_timeline: (dash.timeline_style as ChannelCreatePayload['dash_timeline']) ?? 'full',
    dash_low_latency: lowLatencyFromConfig(dash.low_latency),
    hls_low_latency: lowLatencyFromConfig(hls.low_latency),
    chunk_duration: Number(packaging.chunk_duration ?? DEFAULT_CHUNK_DURATION),
    target_latency: dash.target_latency === undefined || dash.target_latency === null ? null : Number(dash.target_latency),
    utc_timing: (dash.utc_timing as ChannelCreatePayload['utc_timing']) ?? 'direct',
    utc_timing_url: String(dash.utc_timing_url ?? ''),
    epoch_utc: String(timeline.epoch ?? DEFAULT_EPOCH_UTC),
    continuous: Boolean(timeline.continuous ?? true),
    period_on_segmentation: periodTypesFromConfig(markers.split_on),
    timeshift_enabled: query.timeshift,
    query_overrides: query.overrides,
    query_prefix: query.prefix,
    timeshift_start_param: query.start_param,
    timeshift_end_param: query.end_param,
    timeshift_max_span_seconds: query.max_span_seconds,
    daterange_mode: (markersHls.descriptor_grouping as ChannelCreatePayload['daterange_mode']) ?? 'shared',
    cue_tags: (markersHls.cue_tags as ChannelCreatePayload['cue_tags']) ?? 'none',
    increment_event_ids: Boolean(markers.increment_event_ids ?? false),
    daterange_id_format: String(markersHls.id_template ?? '{segcode}-{eventid}-{loop}'),
    dash_signal_format: (markersDash.signal_format as ChannelCreatePayload['dash_signal_format']) ?? 'binary',
    dash_descriptor_mode: (markersDash.descriptor_grouping as ChannelCreatePayload['dash_descriptor_mode']) ?? 'shared',
  }
}

function lowLatencyDefault(mode: string | undefined): string {
  return mode === 'on' ? 'on' : mode === 'optin' ? 'off (opt-in)' : 'off (not enabled)'
}

/** The channel's own value of each overridable setting, by query parameter name, as
 * display text -- what a request gets when it leaves that override out (shown as
 * "channel: X" in the playback panel). `descriptor_grouping` differs per format. */
export function channelDefaultsByParam(f: ReturnType<typeof formFieldsFromConfig>): Record<string, string> {
  return {
    timeline_mode: f.continuous ? 'continuous' : 'periodic',
    epoch: f.epoch_utc,
    dvr_window: String(f.dvr_window_seconds),
    timeline_style: f.dash_timeline,
    addressing: f.dash_addressing,
    // What a request gets when it leaves the parameter out, per format. "opt-in" channels deliver low latency but
    // only advertise it on ?low_latency=true.
    low_latency: `HLS ${lowLatencyDefault(f.hls_low_latency)}, DASH ${lowLatencyDefault(f.dash_low_latency)}`,
    ll_target_latency: f.target_latency === null ? 'automatic' : String(f.target_latency),
    ll_utc_timing: f.utc_timing,
    cue_tags: f.cue_tags,
    descriptor_grouping: `HLS ${f.daterange_mode}, DASH ${f.dash_descriptor_mode}`,
    increment_event_ids: f.increment_event_ids ? 'on' : 'off',
    signal_format: f.dash_signal_format,
    id_template: f.daterange_id_format,
    split_on: f.period_on_segmentation.length ? f.period_on_segmentation.join(',') : 'none',
  }
}

function display(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'none'

  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

// Read-only view of a channel's TOML in the shared layout. `port` lives in
// [infrastructure.docker] for local-docker and [infrastructure.express] for
// ecs-express.
export function buildConfigSections(config: TomlConfig): ConfigSection[] {
  const deploy = table(config, 'deploy')
  const aws = infraTable(config, 'aws')
  const s3 = infraTable(config, 's3')
  const input = table(config, 'input')
  const packaging = table(config, 'packaging')
  const packagingHls = nested(config, 'packaging', 'hls')
  const timeline = table(config, 'timeline')
  const markers = table(config, 'markers')
  const markersHls = nested(config, 'markers', 'hls')
  const markersDash = nested(config, 'markers', 'dash')
  const backend = String(deploy.backend ?? '')
  const isLocalDocker = backend === 'local-docker'
  const isEcsExpress = backend === 'ecs-express'
  const isAwsLambda = backend === 'aws-lambda'
  const sourceKind = deriveSourceKind(input)
  const portTable = isLocalDocker ? infraTable(config, 'docker') : infraTable(config, 'express')

  const sections: ConfigSection[] = []
  const add = (id: ConfigSectionId, fields: [ConfigFieldKey, unknown][]) => {
    const rows = fields
      .filter(([, value]) => value !== undefined)
      .map(([key, value]) => ({ key, label: CONFIG_FIELD_LABEL[key], value: display(value) }))
    if (rows.length) sections.push({ id, title: CONFIG_SECTION_TITLE[id], rows })
  }

  add('input', [
    ['source_kind', sourceKind],
    ['source_path', input.source_path],
    ['allow_missing_segments', input.allow_missing_segments],
  ])
  if (!isLocalDocker) {
    add('infrastructure.aws', [['region', aws.region]])
    add('infrastructure.s3', [
      ['bucket_name', s3.bucket_name],
      ['content_folder', s3.content_folder],
    ])
  }
  if (usesChannelSection(backend)) {
    if (isAwsLambda) {
      const l = lambdaFieldsFromConfig(config)
      add('infrastructure.lambda', [
        ['lambda_memory', l.lambda_memory],
        ['lambda_timeout', l.lambda_timeout],
        ['lambda_reserved_concurrency', l.lambda_reserved_concurrency],
        ['lambda_cache_mb', l.lambda_cache_mb],
        ['lambda_architecture', l.lambda_architecture],
      ])
    } else {
      add(isLocalDocker ? 'infrastructure.docker' : 'infrastructure.express', [
        ['port', portTable.port],
        ['bundle_content', isLocalDocker ? Boolean(portTable.bundle_content ?? false) : undefined],
        ['cpu', isEcsExpress ? portTable.cpu : undefined],
        ['memory', isEcsExpress ? portTable.memory : undefined],
        ['cdn', isEcsExpress ? (portTable.cdn ?? true) : undefined],
      ])
    }
    const f = formFieldsFromConfig(config)
    // Low latency needs a long-lived server: shown for ecs-express and local-docker only, and its detail rows
    // only when it is on (an "off" channel has just the one row per format).
    const llBackend = LOW_LATENCY_BACKENDS.includes(backend)
    const dashLlOn = llBackend && f.dash_low_latency !== 'off'
    const hlsLlOn = llBackend && f.hls_format === 'cmaf' && f.hls_low_latency !== 'off'
    const anyLlOn = dashLlOn || hlsLlOn
    add('timeline', [
      ['epoch_utc', f.epoch_utc],
      ['continuous', timeline.continuous],
    ])
    add('packaging', [
      // Archive/manifest segment lists keep each segment's own duration.
      ['segment_duration', sourceKind === 'playlist' ? packaging.segment_duration : 'as source'],
      ['dvr_window_seconds', packaging.dvr_window],
      ['chunk_duration', anyLlOn ? f.chunk_duration : undefined],
    ])
    add('packaging.hls', [
      ['hls_format', packagingHls.container],
      ['hls_ts_mux_audio', packagingHls.container === 'ts' ? packagingHls.mux_audio : undefined],
      // LL-HLS needs CMAF segments, so the row is offered only for the CMAF container
      ['hls_low_latency', llBackend && f.hls_format === 'cmaf' ? f.hls_low_latency : undefined],
    ])
    add('packaging.dash', [
      ['dash_addressing', f.dash_addressing],
      ['dash_timeline', f.dash_timeline],
      ['dash_low_latency', llBackend ? f.dash_low_latency : undefined],
      ['target_latency', dashLlOn && f.target_latency !== null ? f.target_latency : undefined],
      ['utc_timing', dashLlOn ? f.utc_timing : undefined],
      ['utc_timing_url', dashLlOn && f.utc_timing === 'http' ? f.utc_timing_url || 'automatic (set at deploy)' : undefined],
    ])
    // A channel written before [query] existed gets its-a-live's defaults
    // (enabled) -- show those rather than hiding the section.
    const ts = timeshiftParamsFromConfig(config)
    add('query', [
      ['timeshift_enabled', ts.timeshift],
      ['query_overrides', ts.overrides],
      ['query_prefix', ts.timeshift || ts.overrides ? ts.prefix || 'none' : undefined],
      ['timeshift_start_param', ts.timeshift ? ts.start_param : undefined],
      ['timeshift_end_param', ts.timeshift ? ts.end_param : undefined],
    ])
    add('timeshift', [['timeshift_max_span_seconds', ts.timeshift || ts.overrides ? ts.max_span_seconds : undefined]])
    add('markers', [
      ['increment_event_ids', markers.increment_event_ids],
      ['period_on_segmentation', f.period_on_segmentation.length ? f.period_on_segmentation : undefined],
    ])
    add('markers.hls', [
      ['daterange_mode', markersHls.descriptor_grouping],
      ['cue_tags', markersHls.cue_tags],
      ['daterange_id_format', markersHls.id_template],
    ])
    add('markers.dash', [
      ['dash_descriptor_mode', markersDash.descriptor_grouping],
      ['dash_signal_format', markersDash.signal_format],
    ])
  }
  return sections
}
