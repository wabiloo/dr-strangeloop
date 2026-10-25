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

export type ConfigBackend = 'aws-media' | 'ecs-express' | 'local-docker'
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
  port: 'Serve port',
  bundle_content: 'Bundle content into the Docker image',
  cpu: 'Express CPU units',
  memory: 'Express memory (MB)',
  cdn: 'CloudFront CDN',
  daterange_mode: 'HLS descriptor grouping',
  cue_tags: 'HLS CUE-OUT/CUE-IN tags',
  dash_signal_format: 'DASH SCTE-35 signal format',
  dash_descriptor_mode: 'DASH descriptor grouping',
  increment_event_ids: 'Increment SCTE-35 event ids each loop (HLS + DASH)',
  daterange_id_format: 'HLS DATERANGE ID template',
} as const

export type ConfigFieldKey = keyof typeof CONFIG_FIELD_LABEL

// aws-media has no packaging/serving/SCTE-35 settings -- MediaLive/
// MediaPackage own those; only ecs-express and local-docker bake+serve.
export function usesChannelSection(backend: string | undefined): boolean {
  return backend === 'ecs-express' || backend === 'local-docker'
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

/** `[infrastructure.<name>]` -- aws, s3, express or docker. */
export function infraTable(config: TomlConfig, name: 'aws' | 's3' | 'express' | 'docker'): Record<string, unknown> {
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
    | 'dash_descriptor_mode'
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
    add(isLocalDocker ? 'infrastructure.docker' : 'infrastructure.express', [
      ['port', portTable.port],
      ['bundle_content', isLocalDocker ? Boolean(portTable.bundle_content ?? false) : undefined],
      ['cpu', isEcsExpress ? portTable.cpu : undefined],
      ['memory', isEcsExpress ? portTable.memory : undefined],
      ['cdn', isEcsExpress ? (portTable.cdn ?? true) : undefined],
    ])
    const f = formFieldsFromConfig(config)
    add('timeline', [
      ['epoch_utc', f.epoch_utc],
      ['continuous', timeline.continuous],
    ])
    add('packaging', [
      // Archive/manifest segment lists keep each segment's own duration.
      ['segment_duration', sourceKind === 'playlist' ? packaging.segment_duration : 'as source'],
      ['dvr_window_seconds', packaging.dvr_window],
    ])
    add('packaging.hls', [
      ['hls_format', packagingHls.container],
      ['hls_ts_mux_audio', packagingHls.container === 'ts' ? packagingHls.mux_audio : undefined],
    ])
    add('packaging.dash', [
      ['dash_addressing', f.dash_addressing],
      ['dash_timeline', f.dash_timeline],
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
