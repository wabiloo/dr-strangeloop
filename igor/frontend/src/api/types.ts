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
  duration_seconds?: number | null
  duration_estimated?: boolean
  error?: string
}

/** GET /api/v1/archives/{name}/import/status -- output freshness, mirrors
 * PlaylistListItem's preview/output status shape. */
export interface ArchiveImportStatus {
  exists: boolean
  stale: boolean
  manifest_path: string | null
}

/** One row from GET /api/v1/archives/ (grave-robber/SCOPE.md §10's list view). */
export interface ArchiveListItem {
  name: string
  display_name: string
  path: string
  format: string
  entry_count?: number
  variant_count?: number
  session_duration_seconds?: number | null
  marker_count?: number | null
  error?: string
  import: ArchiveImportStatus | null
}

/** One [start, end) wall-clock interval a variant was actually captured for. */
export interface CoverageRange {
  start: string
  end: string
}

/** One variant's entry in GET /api/v1/archives/{name}/coverage. */
export interface VariantCoverage {
  manifest_url: string
  format: 'HLS' | 'DASH'
  covered_ranges: CoverageRange[]
  /** HLS only: each unique wall-clock-positioned segment, and whether its
   * media bytes exist in the archive. Empty for DASH. */
  segments: SegmentAvailability[]
}

export interface SegmentAvailability {
  start: string
  end: string
  has_media: boolean
}

/** One of the "smart" range suggestions (grave_robber.suggest). */
export interface RangeSuggestion {
  id: 'complete' | 'longest' | 'widest'
  title: string
  description: string
  start: string
  end: string
  duration_seconds: number
  manifest_url: string
  segments_total: number
  segments_with_media: number
  survivor_count: number
  variant_count: number
}

/** The wizard's persisted choice (data/archives/<name>.selection.json). */
export interface ArchiveSelection {
  manifest_url: string | null
  start: string | null
  end: string | null
  suggestion_id: string | null
}

/** One `#EXT-X-STREAM-INF` rendition advertised by an HLS multivariant playlist. */
export interface MultivariantRendition {
  manifest_url: string
  bandwidth: number | null
  average_bandwidth: number | null
  resolution: string | null
  frame_rate: number | null
  codecs: string | null
}

/** An HLS multivariant playlist: segment-less, so informational only (never importable). */
export interface MultivariantPlaylist {
  manifest_url: string
  renditions: MultivariantRendition[]
}

/** GET /api/v1/archives/{name}/coverage -- SCOPE.md §8 steps 1-2's
 * per-variant coverage map, for the import wizard's range picker. */
export interface ArchiveCoverage {
  name: string
  display_name: string
  variants: VariantCoverage[]
  multivariants: MultivariantPlaylist[]
  suggestions: RangeSuggestion[]
  selection: ArchiveSelection | null
}

/** franken-ts's franken_ts.config.Config, as raw parsed YAML -- kept as a
 * loose record because the form is schema-driven (see /api/v1/playlists/schema,
 * a live JSON Schema export of the Pydantic model) rather than hand-typed. */
export type FrankenTsPlaylist = Record<string, unknown>

/** One `markers` entry from the playlist YAML, as edited in the UI. */
export interface MarkerEntry {
  _ui_id: number
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
    sub_segment_num?: number | null
    sub_segments_expected?: number | null
  }
  /** `splice_insert` only, default true -- see franken_ts.config.SpliceConfig.auto_return. */
  auto_return?: boolean
}

/** One resolved entry from POST /playlists/{name}/resolve-markers -- real
 * start/end seconds (from ffprobe'd durations) and the auto-filled
 * segment_num/segments_expected, for rendering marker lanes on the timeline. */
export interface ResolvedMarker {
  marker_index: number
  event_id: number
  type: string
  assets: string[]
  start_seconds: number | null
  end_seconds: number | null
  segment_num?: number | null
  segments_expected?: number | null
  sub_segment_num?: number | null
  sub_segments_expected?: number | null
}

export interface MarkersPreview {
  markers: ResolvedMarker[]
  warnings: string[]
  /** Real (ffprobe'd) per-asset durations in seconds, keyed by asset id. */
  asset_durations: Record<string, number>
}

/** One marker's numbering fields, as returned by
 * POST /playlists/validate-markers -- see MarkersNumberingPreview. */
export interface MarkerNumbering {
  segment_num: number | null
  segments_expected: number | null
  sub_segment_num: number | null
  sub_segments_expected: number | null
}

/** Response from POST /playlists/validate-markers -- the numbering
 * franken-ts would compute for the posted (possibly unsaved) playlist,
 * keyed by event_id. Cheap (no ffprobe), so the editor calls this on a
 * debounce after every edit rather than only via an explicit "Resolve"
 * action -- see resolveMarkers/MarkersPreview above for the heavier,
 * real-durations variant. */
export interface MarkersNumberingPreview {
  numbering: Record<string, MarkerNumbering>
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
  hls_format: 'cmaf' | 'ts'
  hls_ts_mux_audio: boolean
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
  source_kind?: 'playlist' | 'archive'
  playlist_name?: string | null
  archive_name?: string | null
  archive_display_name?: string | null
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
  source_kind?: 'playlist' | 'archive'
  /** grave-robber/SCOPE.md §10: [input].allow_missing_segments -- only
   * meaningful when source_path is a grave-robber segment-list manifest,
   * but stays a plain bake.py-level flag regardless of source kind. */
  allow_missing_segments?: boolean
  segment_duration?: number
  dvr_window_seconds?: number
  hls_format?: 'cmaf' | 'ts'
  hls_ts_mux_audio?: boolean
  /** loop-dee-loop/SCOPE.md §12: default true -- no #EXT-X-DISCONTINUITY /
   * DASH Period restart at the loop wrap (serve.py rewrites each segment's
   * own timestamps per request instead). ecs-express/local-docker only. */
  continuous_timeline?: boolean
  // int to pin an explicit host port, "auto" (local-docker only) to let
  // it self-select a free one at start/refresh time.
  port?: number | 'auto'
  cpu?: number
  memory?: number
  daterange_mode?: 'grouped' | 'shared' | 'narrowed'
  cue_tags?: 'none' | 'alongside' | 'only'
  increment_event_ids?: boolean
  daterange_id_format?: string
  dash_signal_format?: 'binary' | 'xml'
  dash_descriptor_mode?: 'shared' | 'narrowed'
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
