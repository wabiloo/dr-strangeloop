/** Mirrors igor's FastAPI JSON responses (see
 * igor/src/igor/app/routes/*.py). Kept hand-in-sync with
 * the backend Pydantic models / dict shapes -- there is no shared codegen
 * yet, so double check both sides when changing a route's response shape.
 */

export interface ConfigListItem {
  name: string
  path: string
  output_file?: string | null
  output_dir?: string | null
  asset_count?: number
  ad_break_count?: number
  error?: string
}

/** franken-ts's franken_ts.config.Config, as raw parsed YAML -- kept as a
 * loose record because the form is schema-driven (see /api/v1/configs/schema,
 * a live JSON Schema export of the Pydantic model) rather than hand-typed. */
export type FrankenTsConfig = Record<string, unknown>

export interface FileEntry {
  name: string
  path: string
  is_dir: boolean
  is_video: boolean
}

export interface BrowseResult {
  path: string
  parent: string | null
  entries: FileEntry[]
}

export interface ProbeResult {
  duration_seconds: number | null
  width: number | null
  height: number | null
  video_codec: string | null
  frame_rate: number | null
  has_audio: boolean
  audio_codec: string | null
  format_name: string | null
  size_bytes: number | null
}

export interface ChannelListItem {
  config_path: string
  name: string
  backend: 'aws-media' | 'ecs-express'
  stack_name: string
  stack_status: string | null
}

export interface ChannelStatus {
  backend: 'aws-media' | 'ecs-express'
  status: string
  hls_url?: string | null
  dash_url?: string | null
  // ecs-express only:
  service_name?: string
  min_tasks?: number
  max_tasks?: number
  // aws-media only:
  channel_id?: string
}

export type ChannelOutputs = Record<string, string>

export interface ChannelHealth {
  status: string
  package_dir: string
  timescale: number
  epoch_ticks: number
  total_loop_duration_ticks: number
  total_loop_duration_seconds: number
  loop_number: number
  position_in_loop_ticks: number
  position_in_loop_seconds: number
  uptime_seconds: number
  renditions: string[]
  has_audio: boolean
  window_segments: number
}

export interface ChannelCreatePayload {
  name: string
  backend: 'aws-media' | 'ecs-express'
  region: string
  bucket_name: string
  content_folder: string
  source_path: string
  segment_duration?: number
  dvr_window_seconds?: number
  port?: number
  cpu?: number
  memory?: number
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface Job {
  id: string
  type: string
  channel_name: string | null
  command: string[]
  status: JobStatus
  log: string[]
  log_length: number
  return_code: number | null
  created_at: number
  started_at: number | null
  finished_at: number | null
}
