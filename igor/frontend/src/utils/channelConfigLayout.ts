// One layout for channel configuration, shared by the New channel form, the
// read-only Configuration panel and the Configuration edit form on the
// channel detail page: same sections, same order, same field labels.

import { timeshiftParamsFromConfig } from './timeshift'

export type ConfigBackend = 'aws-media' | 'ecs-express' | 'local-docker'
export type ConfigSourceKind = 'playlist' | 'archive' | 'manifest'

// Display order: channel, then infrastructure (AWS / S3, Serving), then
// what gets baked and signaled.
export const CONFIG_SECTION_TITLE = {
  channel: 'Channel & source',
  aws: 'AWS / S3',
  serving: 'Serving',
  packaging: 'Packaging',
  timeshift: 'Startover & catchup',
  hls: 'HLS packaging',
  scte35: 'SCTE-35 signaling',
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
  continuous_timeline: 'Continuous timeline across the loop wrap',
  timeshift_enabled: 'Enable startover & catchup',
  timeshift_start_param: 'Start parameter',
  timeshift_end_param: 'End parameter',
  timeshift_full_loop_param: 'Whole-loops parameter',
  timeshift_max_span_seconds: 'Maximum range (s)',
  segment_duration: 'Segment duration (s)',
  dvr_window_seconds: 'DVR window (s)',
  hls_format: 'HLS segment format',
  hls_ts_mux_audio: 'Mux audio into each HLS TS video segment',
  port: 'Serve port',
  cpu: 'Express CPU units',
  memory: 'Express memory (MB)',
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

function display(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

// Read-only view of a channel's TOML in the shared layout. `port` lives in
// [docker] for local-docker and [express] for ecs-express.
export function buildConfigSections(config: TomlConfig): ConfigSection[] {
  const deploy = table(config, 'deploy')
  const aws = table(config, 'aws')
  const s3 = table(config, 's3')
  const input = table(config, 'input')
  const packaging = table(config, 'packaging')
  const markers = table(config, 'markers')
  const backend = String(deploy.backend ?? '')
  const isLocalDocker = backend === 'local-docker'
  const isEcsExpress = backend === 'ecs-express'
  const sourceKind = deriveSourceKind(input)
  const portTable = isLocalDocker ? table(config, 'docker') : table(config, 'express')

  const sections: ConfigSection[] = []
  const add = (id: ConfigSectionId, fields: [ConfigFieldKey, unknown][]) => {
    const rows = fields
      .filter(([, value]) => value !== undefined)
      .map(([key, value]) => ({ key, label: CONFIG_FIELD_LABEL[key], value: display(value) }))
    if (rows.length) sections.push({ id, title: CONFIG_SECTION_TITLE[id], rows })
  }

  add('channel', [
    ['backend', backend],
    ['source_kind', sourceKind],
    ['source_path', input.source_path],
    ['allow_missing_segments', input.allow_missing_segments],
  ])
  if (!isLocalDocker) {
    add('aws', [
      ['region', aws.region],
      ['bucket_name', s3.bucket_name],
      ['content_folder', s3.content_folder],
    ])
  }
  if (usesChannelSection(backend)) {
    add('serving', [
      ['port', portTable.port],
      ['cpu', isEcsExpress ? portTable.cpu : undefined],
      ['memory', isEcsExpress ? portTable.memory : undefined],
    ])
    add('packaging', [['continuous_timeline', packaging.continuous_timeline]])
    // A channel written before [timeshift] existed gets its-a-live's
    // defaults (enabled) -- show those rather than hiding the section.
    const ts = timeshiftParamsFromConfig(table(config, 'timeshift'))
    add('timeshift', [
      ['timeshift_enabled', ts.enabled],
      ['timeshift_start_param', ts.enabled ? ts.start_param : undefined],
      ['timeshift_end_param', ts.enabled ? ts.end_param : undefined],
      ['timeshift_full_loop_param', ts.enabled ? ts.full_loop_param : undefined],
      ['timeshift_max_span_seconds', ts.enabled ? ts.max_span_seconds : undefined],
    ])
    add('hls', [
      // Archive/manifest segment lists keep each segment's own duration.
      ['segment_duration', sourceKind === 'playlist' ? packaging.segment_duration : 'as source'],
      ['dvr_window_seconds', packaging.dvr_window_seconds],
      ['hls_format', packaging.hls_format],
      ['hls_ts_mux_audio', packaging.hls_format === 'ts' ? packaging.hls_ts_mux_audio : undefined],
    ])
    add('scte35', [
      ['daterange_mode', markers.daterange_mode],
      ['cue_tags', markers.cue_tags],
      ['dash_signal_format', markers.dash_signal_format],
      ['dash_descriptor_mode', markers.dash_descriptor_mode],
      ['increment_event_ids', markers.increment_event_ids],
      ['daterange_id_format', markers.daterange_id_format],
    ])
  }
  return sections
}
