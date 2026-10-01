import type {
  ArchiveCoverage,
  ArchiveSelection,
  ArchiveImportStatus,
  ArchiveListItem,
  BrowseResult,
  ChannelCreatePayload,
  ChannelHealth,
  ChannelListItem,
  ChannelOutputs,
  ChannelStatus,
  ManifestImportStatus,
  ManifestInspection,
  ManifestListItem,
  FrankenTsPlaylist,
  Job,
  MarkersNumberingPreview,
  MarkersPreview,
  PlaylistListItem,
  ProbeResult,
  ScheduleWindow,
} from './types'

async function handle(res: Response): Promise<Response> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return res
}

async function getJson<T>(url: string): Promise<T> {
  const res = await handle(await fetch(url))
  return res.json()
}

async function postJson<T>(url: string, body?: unknown): Promise<T> {
  const res = await handle(
    await fetch(url, {
      method: 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  )
  return res.json()
}

// ---------------------------------------------------------------------------
// franken-ts playlists
// ---------------------------------------------------------------------------

const PLAYLISTS_BASE = '/api/v1/playlists'

export function getPlaylistSchema(): Promise<Record<string, unknown>> {
  return getJson(`${PLAYLISTS_BASE}/schema`)
}

export function listPlaylists(): Promise<PlaylistListItem[]> {
  return getJson(`${PLAYLISTS_BASE}/`)
}

export function getPlaylist(name: string): Promise<FrankenTsPlaylist> {
  return getJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}`)
}

export async function savePlaylist(name: string, data: FrankenTsPlaylist): Promise<{ path: string }> {
  const res = await handle(
    await fetch(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
    }),
  )
  return res.json()
}

export async function deletePlaylist(name: string): Promise<void> {
  await handle(await fetch(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}`, { method: 'DELETE' }))
}

export function duplicatePlaylist(name: string, newName: string): Promise<{ path: string }> {
  return postJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/duplicate`, { new_name: newName })
}

export function buildPlaylist(name: string): Promise<Job> {
  return postJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/build`)
}

export function resolveMarkers(name: string, data?: FrankenTsPlaylist): Promise<MarkersPreview> {
  return postJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/resolve-markers`, data ? { data } : undefined)
}

/** Cheap numbering-only preview (no ffprobe/filesystem access) -- see
 * MarkersNumberingPreview. Doesn't need a playlist name: it never reads a
 * saved file, only validates `data`. */
export function validateMarkersNumbering(data: FrankenTsPlaylist): Promise<MarkersNumberingPreview> {
  return postJson(`${PLAYLISTS_BASE}/validate-markers`, { data })
}

/** URL for the quick 540p preview .mp4 franken-ts writes as the last step
 * of a successful build (see franken-ts's generate_preview_mp4) -- used
 * directly as a <video> src, not fetched via JSON. `cacheBust`, if given,
 * is appended as a query param so the <video> element re-fetches after a
 * rebuild instead of replaying a browser-cached copy of the old preview. */
export function playlistPreviewUrl(name: string, cacheBust?: string | number): string {
  const url = `${PLAYLISTS_BASE}/${encodeURIComponent(name)}/preview`
  return cacheBust === undefined ? url : `${url}?v=${encodeURIComponent(String(cacheBust))}`
}

/** Whether a preview .mp4 exists and whether it's stale (rendered from a
 * playlist version older than the current saved YAML) -- computed
 * server-side by comparing file mtimes, so it stays correct across page
 * reloads and other browser tabs/clients, not just this session's state. */
export function getPreviewStatus(name: string): Promise<{ exists: boolean; stale: boolean }> {
  return getJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/preview/status`)
}

export function getOutputStatus(name: string): Promise<{ exists: boolean; stale: boolean }> {
  return getJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/output/status`)
}

/** Spawns an independent scan of the assembled `.ts` for its actual
 * SCTE-35 markers (via inspector-krogh's `krogh`, not franken-ts --
 * see igor's `scte_verify.py`). */
export function buildScteVerify(name: string): Promise<Job> {
  return postJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/scte-verify`)
}

export function getScteVerifyStatus(name: string): Promise<{ exists: boolean; stale: boolean }> {
  return getJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/scte-verify/status`)
}

/** The raw JSON report, for a native (non-iframe) renderer. */
export function getScteVerifyReport(name: string): Promise<Record<string, unknown>> {
  return getJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/scte-verify`)
}

/** URL for the self-contained HTML rendering of the same report (iframe use). */
export function scteVerifyReportUrl(
  name: string,
  cacheBust?: string | number,
  view: 'filmstrip' | 'cards' = 'filmstrip',
): string {
  const url = `${PLAYLISTS_BASE}/${encodeURIComponent(name)}/scte-verify/report?view=${view}`
  return cacheBust === undefined ? url : `${url}&v=${encodeURIComponent(String(cacheBust))}`
}

// ---------------------------------------------------------------------------
// grave-robber archive imports (grave-robber/SCOPE.md §10)
// ---------------------------------------------------------------------------

const ARCHIVES_BASE = '/api/v1/archives'

export function listArchives(): Promise<ArchiveListItem[]> {
  return getJson(`${ARCHIVES_BASE}/`)
}

export async function uploadArchive(file: File): Promise<ArchiveListItem> {
  const query = new URLSearchParams({ filename: file.name })
  const res = await handle(
    await fetch(`${ARCHIVES_BASE}/upload?${query}`, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    }),
  )
  return res.json()
}

export async function deleteArchive(name: string): Promise<void> {
  await handle(await fetch(`${ARCHIVES_BASE}/${encodeURIComponent(name)}`, { method: 'DELETE' }))
}

export async function renameArchive(name: string, displayName: string): Promise<{ name: string; display_name: string }> {
  const res = await handle(
    await fetch(`${ARCHIVES_BASE}/${encodeURIComponent(name)}/name`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: displayName }),
    }),
  )
  return res.json()
}

/** Per-variant wall-clock coverage map for the import wizard's range
 * picker (SCOPE.md §8 steps 1-2). */
export function getArchiveCoverage(name: string): Promise<ArchiveCoverage> {
  return getJson(`${ARCHIVES_BASE}/${encodeURIComponent(name)}/coverage`)
}

/** Spawns the grave-robber `ingest` job for a human-confirmed reference
 * variant (SCOPE.md §8 step 5) -- `manifestUrl` is one of the URLs
 * returned by getArchiveCoverage. */
export function importArchive(
  name: string,
  manifestUrl: string,
  format?: 'HLS' | 'DASH',
  range?: { start: string; end: string },
): Promise<Job> {
  return postJson(`${ARCHIVES_BASE}/${encodeURIComponent(name)}/import`, {
    manifest_url: manifestUrl,
    format,
    start: range?.start,
    end: range?.end,
  })
}

export async function saveArchiveSelection(name: string, selection: ArchiveSelection): Promise<ArchiveSelection> {
  const res = await handle(
    await fetch(`${ARCHIVES_BASE}/${encodeURIComponent(name)}/selection`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selection),
    }),
  )
  return res.json()
}

export function getArchiveImportStatus(name: string): Promise<ArchiveImportStatus> {
  return getJson(`${ARCHIVES_BASE}/${encodeURIComponent(name)}/import/status`)
}

// ---------------------------------------------------------------------------
// VOD manifest-URL sources (grave-robber ingest-url)
// ---------------------------------------------------------------------------

const MANIFESTS_BASE = '/api/v1/manifests'

export function listManifests(): Promise<ManifestListItem[]> {
  return getJson(`${MANIFESTS_BASE}/`)
}

export function getManifestSource(name: string): Promise<ManifestListItem> {
  return getJson(`${MANIFESTS_BASE}/${encodeURIComponent(name)}`)
}

export function createManifestSource(manifestUrl: string, name?: string): Promise<ManifestListItem> {
  return postJson(`${MANIFESTS_BASE}/`, { manifest_url: manifestUrl, name: name || undefined })
}

export async function deleteManifestSource(name: string): Promise<void> {
  await handle(await fetch(`${MANIFESTS_BASE}/${encodeURIComponent(name)}`, { method: 'DELETE' }))
}

export async function renameManifestSource(name: string, displayName: string): Promise<{ name: string; display_name: string }> {
  const res = await handle(
    await fetch(`${MANIFESTS_BASE}/${encodeURIComponent(name)}/name`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: displayName }),
    }),
  )
  return res.json()
}

/** Fetches just the manifest (no segments) and describes its rendition ladder. */
export function inspectManifest(manifestUrl: string): Promise<ManifestInspection> {
  return postJson(`${MANIFESTS_BASE}/inspect`, { manifest_url: manifestUrl })
}

/** Spawns `grave-robber ingest-url`, downloading every segment of the chosen renditions
 * (`renditions`: 'all', 'best' or ranked positions like '#1,#3'). */
export function importManifest(
  name: string,
  options: { renditions?: string; audio?: boolean; allow_missing_segments?: boolean },
): Promise<Job> {
  return postJson(`${MANIFESTS_BASE}/${encodeURIComponent(name)}/import`, options)
}

export function getManifestImportStatus(name: string): Promise<ManifestImportStatus> {
  return getJson(`${MANIFESTS_BASE}/${encodeURIComponent(name)}/import/status`)
}

// ---------------------------------------------------------------------------
// its-a-live channels
// ---------------------------------------------------------------------------

const CHANNELS_BASE = '/api/v1/channels'

export function listChannels(): Promise<ChannelListItem[]> {
  return getJson(`${CHANNELS_BASE}/`)
}

/** Instant: names/backends/sources from the local configs only, no stack or
 * live state (that comes from `getChannelSummary`, one channel at a time). */
export function listChannelsQuick(): Promise<ChannelListItem[]> {
  return getJson(`${CHANNELS_BASE}/?live=false`)
}

export function getChannelSummary(name: string): Promise<ChannelListItem> {
  return getJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/summary`)
}

export function defineChannel(payload: ChannelCreatePayload): Promise<{ path: string }> {
  return postJson(`${CHANNELS_BASE}/`, payload)
}

export async function updateChannel(name: string, payload: ChannelCreatePayload): Promise<{ path: string }> {
  const res = await handle(
    await fetch(`${CHANNELS_BASE}/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  )
  return res.json()
}

export function getChannel(name: string): Promise<Record<string, unknown>> {
  return getJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}`)
}

export async function deleteChannel(name: string): Promise<void> {
  await handle(await fetch(`${CHANNELS_BASE}/${encodeURIComponent(name)}`, { method: 'DELETE' }))
}

export function getChannelStatus(name: string): Promise<ChannelStatus> {
  return getJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/status`)
}

export function getChannelOutputs(name: string): Promise<ChannelOutputs> {
  return getJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/outputs`)
}

export function getChannelHealth(name: string): Promise<ChannelHealth> {
  return getJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/health`)
}

export function createChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/create`)
}

export function sparkChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/spark`)
}

export function startChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/start`)
}

export function stopChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/stop`)
}

export function refreshChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/refresh`)
}

export function updateChannelContent(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/update`)
}

export function redeployChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/redeploy`)
}

export function terminateChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/terminate`)
}

export function listScheduleWindows(name: string): Promise<ScheduleWindow[]> {
  return getJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/schedule`)
}

export function addScheduleWindow(name: string, start: string | null, end: string | null): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/schedule`, { start, end })
}

export async function removeScheduleWindow(name: string, windowId: string): Promise<void> {
  await handle(
    await fetch(`${CHANNELS_BASE}/${encodeURIComponent(name)}/schedule/${encodeURIComponent(windowId)}`, {
      method: 'DELETE',
    }),
  )
}

// ---------------------------------------------------------------------------
// jobs
// ---------------------------------------------------------------------------

const JOBS_BASE = '/api/v1/jobs'

export function listJobs(channel?: string): Promise<Job[]> {
  const qs = channel ? `?channel=${encodeURIComponent(channel)}` : ''
  return getJson(`${JOBS_BASE}/${qs}`)
}

export function getJob(id: string, logOffset = 0): Promise<Job> {
  return getJson(`${JOBS_BASE}/${encodeURIComponent(id)}?log_offset=${logOffset}`)
}

// ---------------------------------------------------------------------------
// local files (browse + probe, for the asset file picker)
// ---------------------------------------------------------------------------

const FILES_BASE = '/api/v1/files'

export function browseFiles(path?: string): Promise<BrowseResult> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : ''
  return getJson(`${FILES_BASE}/browse${qs}`)
}

export function probeMedia(pathOrUrl: string): Promise<ProbeResult> {
  return getJson(`${FILES_BASE}/probe?path_or_url=${encodeURIComponent(pathOrUrl)}`)
}

/** URL for previewing a local source file through Igor's backend. */
export function localFilePreviewUrl(path: string): string {
  return `${FILES_BASE}/preview?path=${encodeURIComponent(path)}`
}

export async function uploadAsset(file: File): Promise<{ path: string; name: string }> {
  const res = await handle(
    await fetch(`${FILES_BASE}/upload?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    }),
  )
  return res.json()
}
