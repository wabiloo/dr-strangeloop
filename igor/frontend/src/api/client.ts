import type {
  BrowseResult,
  ChannelCreatePayload,
  ChannelHealth,
  ChannelListItem,
  ChannelOutputs,
  ChannelStatus,
  FrankenTsPlaylist,
  Job,
  PlaylistListItem,
  ProbeResult,
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

export function buildPlaylist(name: string): Promise<Job> {
  return postJson(`${PLAYLISTS_BASE}/${encodeURIComponent(name)}/build`)
}

// ---------------------------------------------------------------------------
// its-a-live channels
// ---------------------------------------------------------------------------

const CHANNELS_BASE = '/api/v1/channels'

export function listChannels(): Promise<ChannelListItem[]> {
  return getJson(`${CHANNELS_BASE}/`)
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

export function redeployChannel(name: string): Promise<Job> {
  return postJson(`${CHANNELS_BASE}/${encodeURIComponent(name)}/redeploy`)
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
