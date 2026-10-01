// One layout for channel configuration, shared by the New channel form, the
// read-only Configuration panel and the Configuration edit form on the
// channel detail page: same sections, same order, same field labels.
//
// NAMING RULE (keep it): a section here IS one its-a-live TOML table. Its id
// is the table name and its title is that name, capitalised ([timeline] ->
// "Timeline"; [infrastructure.aws] -> "Infrastructure · AWS"). A field is shown in the section of the table it is stored in,
// never anywhere else. To add a setting, put it in the table it belongs to
// (or add a new table + section together); to rename a table, rename the
// section id/title, the New/Edit templates and the docs in the same change.
// See igor/AGENTS.md "Channel config sections".

import { DEFAULT_EPOCH_UTC } from './epoch'
import { timeshiftParamsFromConfig } from './timeshift'

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
  timeshift: 'Timeshift',
  packaging: 'Packaging',
  markers: 'Markers',
} as const

export type ConfigSectionId = keyof typeof CONFIG_SECTION_TITLE

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
  period_on_segmentation: 'New Period on SCTE-35 segmentations',
  period_on_segmentation_apply: 'Apply to',
  timeshift_enabled: 'Enable startover & catchup',
  timeshift_start_param: 'Start parameter',
  timeshift_end_param: 'End parameter',
  timeshift_max_span_seconds: 'Maximum range (s)',
  segment_duration: 'Segment duration (s)',
  dvr_window_seconds: 'DVR window (s)',
  epoch_utc: 'Channel epoch (UTC)',
  hls_format: 'HLS segment format',
  hls_ts_mux_audio: 'Mux audio into each HLS TS video segment',
  port: 'Serve port',
  cpu: 'Express CPU units',
  memory: 'Express memory (MB)',
  cdn: 'CloudFront CDN',
  daterange_mode: 'HLS DATERANGE mode',
  cue_tags: 'HLS CUE-OUT/CUE-IN tags',
  dash_signal_format: 'DASH SCTE-35 signal format',
  dash_descriptor_mode: 'DASH coincident descriptor mode',
  increment_event_ids: 'Increment SCTE-35 event ids each loop (HLS + DASH)',
  daterange_id_format: 'HLS DATERANGE ID format',
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

/** `[infrastructure.<name>]` -- aws, s3, express or docker. */
export function infraTable(config: TomlConfig, name: 'aws' | 's3' | 'express' | 'docker'): Record<string, unknown> {
  return table(table(config, 'infrastructure'), name)
}

/** TOML `[markers] period_on_segmentation` (ints or strings) -> hex strings ("0x22"),
 * the form the channel form and API use. */
export function periodTypesFromConfig(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => (typeof item === 'number' ? item : Number(item)))
    .filter((n) => Number.isInteger(n) && n >= 0 && n <= 0xff)
    .map((n) => `0x${n.toString(16).toUpperCase().padStart(2, '0')}`)
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
  const timeline = table(config, 'timeline')
  const markers = table(config, 'markers')
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
      ['cpu', isEcsExpress ? portTable.cpu : undefined],
      ['memory', isEcsExpress ? portTable.memory : undefined],
      ['cdn', isEcsExpress ? (portTable.cdn ?? true) : undefined],
    ])
    const periodTypes = periodTypesFromConfig(markers.period_on_segmentation)
    add('timeline', [
      ['epoch_utc', timeline.epoch_utc ?? DEFAULT_EPOCH_UTC],
      ['continuous', timeline.continuous],
    ])
    add('packaging', [
      // Archive/manifest segment lists keep each segment's own duration.
      ['segment_duration', sourceKind === 'playlist' ? packaging.segment_duration : 'as source'],
      ['dvr_window_seconds', packaging.dvr_window_seconds],
      ['hls_format', packaging.hls_format],
      ['hls_ts_mux_audio', packaging.hls_format === 'ts' ? packaging.hls_ts_mux_audio : undefined],
    ])
    // A channel written before [timeshift] existed gets its-a-live's
    // defaults (enabled) -- show those rather than hiding the section.
    const ts = timeshiftParamsFromConfig(table(config, 'timeshift'))
    add('timeshift', [
      ['timeshift_enabled', ts.enabled],
      ['timeshift_start_param', ts.enabled ? ts.start_param : undefined],
      ['timeshift_end_param', ts.enabled ? ts.end_param : undefined],
      ['timeshift_max_span_seconds', ts.enabled ? ts.max_span_seconds : undefined],
    ])
    add('markers', [
      ['daterange_mode', markers.daterange_mode],
      ['cue_tags', markers.cue_tags],
      ['dash_signal_format', markers.dash_signal_format],
      ['dash_descriptor_mode', markers.dash_descriptor_mode],
      ['increment_event_ids', markers.increment_event_ids],
      ['daterange_id_format', markers.daterange_id_format],
      ['period_on_segmentation', periodTypes.length ? periodTypes : undefined],
      ['period_on_segmentation_apply', periodTypes.length ? (markers.period_on_segmentation_apply ?? 'both') : undefined],
    ])
  }
  return sections
}
