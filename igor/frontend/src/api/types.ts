/** Mirrors igor's FastAPI JSON responses (see
 * igor/src/igor/app/routes/*.py). Kept hand-in-sync with
 * the backend Pydantic models / dict shapes -- there is no shared codegen
 * yet, so double check both sides when changing a route's response shape.
 */

export interface PlaylistListItem {
  name: string
  path: string
  output_file?: string | null
  output_dir?: string | null
  asset_count?: number
  marker_count?: number
  rendition_count?: number
  error?: string
}

/** franken-ts's franken_ts.config.Config, as raw parsed YAML -- kept as a
 * loose record because the form is schema-driven (see /api/v1/playlists/schema,
 * a live JSON Schema export of the Pydantic model) rather than hand-typed. */
export type FrankenTsPlaylist = Record<string, unknown>

/** One `markers` entry from the playlist YAML, as edited in the UI. */
export interface MarkerEntry {
  event_id: number
  type: string
  splice_type: 'splice_insert' | 'time_signal'
  assets: string[]
  segmentation?: {
    type_id: string
    upid_type?: string
    upid_hex?: string
    web_delivery_allowed?: boolean
    no_regional_blackout?: boolean
    archive_allowed?: boolean
    device_restrictions?: number
    segment_num?: number | null
    segments_expected?: number | null
  }
  /** `splice_insert` only, default true -- see franken_ts.config.SpliceConfig.auto_return. */
  auto_return?: boolean
}

/** One resolved entry from POST /playlists/{name}/resolve-markers -- real
 * start/end seconds (from ffprobe'd durations) and the auto-filled
 * segment_num/segments_expected, for rendering marker lanes on the timeline. */
export interface ResolvedMarker {
  event_id: number
  type: string
  assets: string[]
  start_seconds: number | null
  end_seconds: number | null
  segment_num?: number | null
  segments_expected?: number | null
}

export interface MarkersPreview {
  markers: ResolvedMarker[]
  warnings: string[]
  /** Real (ffprobe'd) per-asset durations in seconds, keyed by asset id. */
  asset_durations: Record<string, number>
}

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
  backend: 'aws-media' | 'ecs-express' | 'local-docker'
  stack_name: string | null
  // local-docker only (null for aws-media/ecs-express) -- there's no
  // CloudFormation stack for that backend, so this is the nearest
  // equivalent identifier: the deterministic `its-a-live-<name>` Docker
  // container name.
  container_name?: string | null
  stack_status: string | null
  source_path?: string
  playlist_name?: string | null
  // Live running/stopped signal, fetched by `channel.py list` once the
  // CloudFormation stack has settled -- stack_status alone can't tell
  // "deployed" apart from "deployed but scaled to 0 / IDLE". Absent
  // while the stack is mid-transition/broken/not deployed, or if the
  // live fetch itself failed.
  live_status?: string | null
  min_tasks?: number | null
  max_tasks?: number | null
  // Whether HlsPlaybackUrl/DashPlaybackUrl actually returned a manifest
  // (not just whether the backend's own infra claims to be serving) --
  // see its-a-live's _reachability.py. null/absent when not checked
  // (channel not settled/running, or the check itself couldn't run).
  reachable?: boolean | null
}

export interface ChannelStatus {
  backend: 'aws-media' | 'ecs-express' | 'local-docker'
  status: string
  // CloudFormation StackStatus for aws-media/ecs-express (absent for
  // local-docker, which has no stack). Set even while `status` itself is
  // a CFN-status fallback (stack mid create/update/delete, or gone) rather
  // than the backend's own live status -- see its-a-live's cmd_status,
  // which skips querying MediaLive/ECS entirely until the stack is
  // "settled", to avoid e.g. MediaLive's DescribeChannel 404ing mid
  // DELETE_IN_PROGRESS once the channel resource itself is already gone
  // but other stack resources are still being torn down.
  stack_status?: string | null
  hls_url?: string | null
  dash_url?: string | null
  // ecs-express only:
  service_name?: string
  min_tasks?: number
  max_tasks?: number
  // aws-media only:
  channel_id?: string
  // local-docker only:
  container_name?: string
  // local-docker only -- the resolved port, even when docker.port is
  // "auto" in the config; null if no container has ever been created yet.
  port?: number | null
  // Whether HlsPlaybackUrl/DashPlaybackUrl actually returned a manifest --
  // see its-a-live's _reachability.py. null/absent when not checked.
  reachable?: boolean | null
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
  backend: 'aws-media' | 'ecs-express' | 'local-docker'
  region: string
  bucket_name: string
  content_folder: string
  source_path: string
  segment_duration?: number
  dvr_window_seconds?: number
  // int to pin an explicit host port, "auto" (local-docker only) to let
  // it self-select a free one at start/refresh time.
  port?: number | 'auto'
  cpu?: number
  memory?: number
  daterange_mode?: 'grouped' | 'shared' | 'narrowed'
  cue_tags?: 'none' | 'alongside' | 'only'
  increment_event_ids?: boolean
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
