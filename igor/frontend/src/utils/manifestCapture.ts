// Manifests the players on the preview page actually fetched (HLS master / media playlists,
// DASH MPD), kept as a short history so they can be read, compared with the previous poll and
// pinned. Pure logic (no Vue, no DOM): the fetch/XHR hook is composables/useManifestCapture.ts.

export type ManifestKind = 'master' | 'media' | 'mpd'

export interface ManifestEntry {
  id: number
  slot: string
  /** Position of the slot's tab: the MPD first, then the master, video, audio... */
  order: number
  label: string
  kind: ManifestKind
  url: string
  /** ISO time of the poll that produced this version. */
  at: string
  /** How many polls returned this exact body (the first one is `at`). */
  hits: number
  lastAt: string
  status: number
  bytes: number
  truncated: boolean
  body: string
  pinned: boolean
}

export const MAX_BODY_BYTES = 4 * 1024 * 1024
export const MAX_TOTAL_BYTES = 32 * 1024 * 1024
export const MAX_SLOTS = 24

export function looksLikeManifestUrl(value: string, contentType = ''): boolean {
  try {
    const url = new URL(value, 'http://x.invalid/')
    return /\.(?:m3u8|mpd)$/i.test(url.pathname) || /mpegurl|dash\+xml/i.test(contentType)
  } catch {
    return false
  }
}

export function classifyManifest(text: string): ManifestKind | null {
  const head = text.trimStart()
  if (head.startsWith('#EXTM3U')) return /#EXT-X-STREAM-INF/.test(text) ? 'master' : 'media'
  if (head.startsWith('<') && /<(?:[\w-]+:)?MPD[\s>]/.test(head.slice(0, 4096))) return 'mpd'
  return null
}

function pathKey(value: string, base: string): string | null {
  try {
    const url = new URL(value, base)
    return url.origin + url.pathname
  } catch {
    return null
  }
}

function attributes(line: string): Record<string, string> {
  return Object.fromEntries(
    [...line.slice(line.indexOf(':') + 1).matchAll(/([A-Z0-9-]+)=("[^"]*"|[^,]*)/g)].map(([, key, value]) => [
      key,
      value.replace(/^"|"$/g, ''),
    ]),
  )
}

/** Which playlist URLs a master playlist points at, with a human label for each. */
export function parseVariants(text: string, base: string): Map<string, string> {
  const out = new Map<string, string>()
  const lines = text.split(/\r?\n/)
  lines.forEach((line, index) => {
    if (line.startsWith('#EXT-X-STREAM-INF')) {
      const info = attributes(line)
      const uri = lines.slice(index + 1).find((next) => next.trim() && !next.startsWith('#'))
      const key = uri && pathKey(uri.trim(), base)
      if (!key) return
      const detail = info.RESOLUTION || (info.BANDWIDTH ? `${Math.round(Number(info.BANDWIDTH) / 1000)} kbps` : '')
      out.set(key, detail ? `Video · ${detail}` : 'Video')
    } else if (line.startsWith('#EXT-X-MEDIA:') || line.startsWith('#EXT-X-I-FRAME-STREAM-INF:')) {
      const info = attributes(line)
      const key = info.URI && pathKey(info.URI, base)
      if (!key || out.has(key)) return
      const iframe = line.startsWith('#EXT-X-I-FRAME')
      const type = iframe
        ? 'I-frame'
        : ({ AUDIO: 'Audio', SUBTITLES: 'Subtitles', VIDEO: 'Video', 'CLOSED-CAPTIONS': 'Captions' } as Record<string, string>)[
            info.TYPE
          ] || info.TYPE || 'Media'
      const name = iframe ? info.RESOLUTION : [info.NAME || info.LANGUAGE, info['GROUP-ID']].filter(Boolean).join(' · ')
      out.set(key, name ? `${type} · ${name}` : type)
    }
  })
  return out
}

function baseName(value: string): string {
  try {
    return new URL(value, 'http://x.invalid/').pathname.split('/').filter(Boolean).slice(-2).join('/')
  } catch {
    return value
  }
}

export class ManifestStore {
  entries: ManifestEntry[] = []
  maxPerSlot: number
  /** A poll returning the same body as the slot's latest version only counts a hit. */
  skipUnchanged = true
  private nextId = 1
  private variants = new Map<string, string>()
  private primaryMaster: string | null = null
  private primaryMpd: string | null = null
  // The video variants are all alike: only the first one a player loads is kept.
  private primaryVideo: string | null = null
  private slotOrder = new Map<string, number>()

  constructor(maxPerSlot = 10) {
    this.maxPerSlot = maxPerSlot
  }

  /** Record a manifest the player consumed; returns the entry it landed in, or null if ignored. */
  add(url: string, status: number, contentType: string, text: string, at = new Date().toISOString()): ManifestEntry | null {
    if (!looksLikeManifestUrl(url, contentType) || !text.length || status < 200 || status >= 300) return null
    const kind = classifyManifest(text)
    if (!kind) return null
    const key = pathKey(url, 'http://x.invalid/')
    if (!key) return null

    let slot: string, label: string, order: number
    if (kind === 'master') {
      // Only the first master (and its refetches) is the stream; others (e.g. ad assets) are not.
      if (this.primaryMaster && this.primaryMaster !== key) return null
      this.primaryMaster = key
      this.variants = parseVariants(text, url)
      if (this.primaryVideo && !this.variants.has(this.primaryVideo)) this.primaryVideo = null
      slot = 'master'
      label = 'Master playlist'
      order = 0
    } else if (kind === 'mpd') {
      if (this.primaryMpd && this.primaryMpd !== key) return null
      this.primaryMpd = key
      slot = 'mpd'
      label = 'MPD'
      order = -1
    } else {
      slot = 'm:' + key
      label = this.variants.get(key) ?? baseName(url)
      if (label.startsWith('Video')) {
        if (this.primaryVideo && this.primaryVideo !== key) return null
        this.primaryVideo = key
      }
      order = this.slotOrder.get(slot) ?? 1 + this.slotOrder.size
      if (!this.slotOrder.has(slot)) {
        if (this.slotOrder.size >= MAX_SLOTS) return null
        this.slotOrder.set(slot, order)
      }
    }

    const encoded = new TextEncoder().encode(text)
    const truncated = encoded.byteLength > MAX_BODY_BYTES
    const body = truncated ? new TextDecoder().decode(encoded.subarray(0, MAX_BODY_BYTES)) : text
    const latest = this.latest(slot)
    if (this.skipUnchanged && latest && latest.body === body) {
      latest.hits += 1
      latest.lastAt = at
      return latest
    }
    const entry: ManifestEntry = {
      id: this.nextId++, slot, order, label, kind, url, at, hits: 1, lastAt: at, status,
      bytes: encoded.byteLength, truncated, body, pinned: false,
    }
    this.entries.push(entry)
    this.evict()
    return entry
  }

  latest(slot: string): ManifestEntry | undefined {
    for (let i = this.entries.length - 1; i >= 0; i--) if (this.entries[i].slot === slot) return this.entries[i]
    return undefined
  }

  /** The version of the same slot before `entry` (for "what changed"). */
  previous(entry: ManifestEntry): ManifestEntry | undefined {
    const same = this.entries.filter((e) => e.slot === entry.slot)
    const i = same.indexOf(entry)
    return i > 0 ? same[i - 1] : undefined
  }

  slots(): { slot: string; label: string; kind: ManifestKind; order: number; count: number }[] {
    const seen = new Map<string, { slot: string; label: string; kind: ManifestKind; order: number; count: number }>()
    for (const e of this.entries) {
      const s = seen.get(e.slot)
      if (s) s.count += 1
      else seen.set(e.slot, { slot: e.slot, label: e.label, kind: e.kind, order: e.order, count: 1 })
    }
    return [...seen.values()].sort((a, b) => a.order - b.order)
  }

  togglePin(id: number): void {
    const e = this.entries.find((x) => x.id === id)
    if (e) {
      e.pinned = !e.pinned
      this.evict()
    }
  }

  /** Drop every unpinned version (pins stay). */
  clearUnpinned(): void {
    this.entries = this.entries.filter((e) => e.pinned)
  }

  /** Forget everything, including which master playlist is the stream (another channel). */
  reset(): void {
    this.entries = []
    this.variants = new Map()
    this.primaryMaster = null
    this.primaryMpd = null
    this.primaryVideo = null
    this.slotOrder = new Map()
  }

  setMaxPerSlot(n: number): void {
    this.maxPerSlot = n
    this.evict()
  }

  // Pinned versions are never evicted; each slot keeps its newest `maxPerSlot` unpinned ones,
  // and a total size cap drops the oldest unpinned ones beyond that.
  private evict(): void {
    const bySlot = new Map<string, ManifestEntry[]>()
    for (const e of this.entries) if (!e.pinned) bySlot.set(e.slot, [...(bySlot.get(e.slot) ?? []), e])
    const drop = new Set<number>()
    for (const list of bySlot.values()) for (const e of list.slice(0, Math.max(0, list.length - this.maxPerSlot))) drop.add(e.id)
    let total = 0
    for (const e of this.entries) if (!drop.has(e.id)) total += e.body.length
    for (const e of this.entries) {
      if (total <= MAX_TOTAL_BYTES) break
      if (!e.pinned && !drop.has(e.id)) {
        drop.add(e.id)
        total -= e.body.length
      }
    }
    if (drop.size) this.entries = this.entries.filter((e) => !drop.has(e.id))
  }
}

/** Indexes of `current`'s lines that do not occur in `previous` (new segments, new tags...). */
export function changedLines(current: string, previous: string | undefined): Set<number> {
  const out = new Set<number>()
  if (previous === undefined) return out
  const before = new Set(previous.split(/\r?\n/))
  current.split(/\r?\n/).forEach((line, i) => {
    if (line.trim() && !before.has(line)) out.add(i)
  })
  return out
}

export type LineKind = 'marker' | 'discontinuity' | 'tag' | 'uri' | 'plain'

/** Rough colouring: ad/SCTE-35 signalling and discontinuities stand out. */
export function lineKind(line: string, format: 'hls' | 'dash'): LineKind {
  if (format === 'hls') {
    if (/^#EXT-X-(?:DATERANGE|CUE-OUT|CUE-IN|CUE-OUT-CONT)|^#EXT-OATCLS|^#EXT-X-SCTE35|^#EXT-X-SPLICEPOINT/.test(line)) return 'marker'
    if (line.startsWith('#EXT-X-DISCONTINUITY')) return 'discontinuity'
    if (line.startsWith('#EXT')) return 'tag'
    return line.trim() && !line.startsWith('#') ? 'uri' : 'plain'
  }
  if (/scte35|<(?:[\w-]+:)?(?:EventStream|Event|Signal)\b/i.test(line)) return 'marker'
  if (/<(?:[\w-]+:)?Period\b/.test(line)) return 'discontinuity'
  return 'plain'
}

export interface ManifestSummary {
  /** Media segments listed (HLS: EXTINF entries; DASH: S entries incl. @r repeats of the video set). */
  segments: number | null
  durationS: number | null
  /** Wall-clock start of the first segment (ISO), when the manifest says it. */
  firstStart: string | null
  /** Why firstStart is missing, or other context. */
  note?: string
}

const attr = (tag: string, name: string): string | undefined => new RegExp(`\\b${name}="([^"]*)"`).exec(tag)?.[1]

function isoDurationSeconds(v: string | undefined): number {
  const m = v ? /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/.exec(v) : null
  return m ? Number(m[1] ?? 0) * 86400 + Number(m[2] ?? 0) * 3600 + Number(m[3] ?? 0) * 60 + Number(m[4] ?? 0) : 0
}

/** Segment count, total duration and first segment start, read from the manifest text. */
export function summarizeManifest(body: string, kind: ManifestKind): ManifestSummary | null {
  if (kind === 'master') return null
  if (kind === 'media') {
    const lines = body.split(/\r?\n/)
    let segments = 0, durationS = 0, pdt: string | null = null, seenFirst = false
    for (const line of lines) {
      if (line.startsWith('#EXT-X-PROGRAM-DATE-TIME:') && !seenFirst && pdt === null) pdt = line.slice(line.indexOf(':') + 1).trim()
      else if (line.startsWith('#EXTINF:')) {
        segments += 1
        durationS += parseFloat(line.slice(8)) || 0
        seenFirst = true
      }
    }
    return { segments, durationS, firstStart: pdt, note: pdt ? undefined : 'no PROGRAM-DATE-TIME' }
  }
  // MPD: the first video AdaptationSet of every Period.
  const ast = attr(/<(?:[\w-]+:)?MPD\b[^>]*>/.exec(body)?.[0] ?? '', 'availabilityStartTime')
  const astMs = ast ? Date.parse(ast) : NaN
  const periods = body.split(/<(?:[\w-]+:)?Period\b/).slice(1)
  let segments = 0, durationS = 0, first: number | null = null, unknown = false
  for (const chunk of periods) {
    const periodStart = isoDurationSeconds(attr(chunk.split('>')[0], 'start'))
    const sets = chunk.split(/<(?:[\w-]+:)?AdaptationSet\b/).slice(1)
    const set = sets.find((s) => /contentType="video"|mimeType="video\//.test(s.split('>')[0])) ?? sets[0]
    if (!set) continue
    const template = /<(?:[\w-]+:)?SegmentTemplate\b[^>]*>/.exec(set)?.[0]
    const timeline = /<(?:[\w-]+:)?SegmentTimeline\b[^>]*>([\s\S]*?)<\/(?:[\w-]+:)?SegmentTimeline>/.exec(set)?.[1]
    if (!template || timeline === undefined) {
      unknown = true
      continue
    }
    const timescale = Number(attr(template, 'timescale') ?? 1) || 1
    const pto = Number(attr(template, 'presentationTimeOffset') ?? 0) || 0
    let t: number | null = null
    for (const s of timeline.matchAll(/<(?:[\w-]+:)?S\b[^>]*>/g)) {
      const d = Number(attr(s[0], 'd') ?? 0)
      const r = Number(attr(s[0], 'r') ?? 0)
      const st = attr(s[0], 't')
      if (st !== undefined && t === null) t = Number(st)
      segments += 1 + Math.max(0, r)
      durationS += (d * (1 + Math.max(0, r))) / timescale
    }
    if (first === null && t !== null) first = periodStart + (t - pto) / timescale
  }
  if (!periods.length) return null
  const firstStart = first !== null && Number.isFinite(astMs) ? new Date(astMs + first * 1000).toISOString() : null
  return {
    segments: unknown && !segments ? null : segments,
    durationS: unknown && !segments ? null : durationS,
    firstStart,
    note: firstStart ? undefined : unknown ? 'no SegmentTimeline' : 'no availabilityStartTime',
  }
}
