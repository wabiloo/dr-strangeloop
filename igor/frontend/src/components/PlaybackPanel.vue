<script setup lang="ts">
import Tag from 'primevue/tag'
import Message from 'primevue/message'
import Button from 'primevue/button'
import { onBeforeUnmount, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import type { ChannelHealth } from '../api/types'
import { bytesToHex, describeAllMarkers } from '../scte35Lite'

const props = defineProps<{
  hlsUrl?: string | null
  dashUrl?: string | null
  health?: ChannelHealth | null
  healthError?: string
}>()

const toast = useToast()

const HLS_CDN = 'https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js'
const DASH_CDN = 'https://cdn.dashjs.org/latest/dash.all.min.js'

// Both players target the SAME distance behind the live edge, explicitly,
// rather than each deriving its own default (hls.js: liveSyncDurationCount
// segments; dash.js: the MPD's suggestedPresentationDelay) -- those two
// defaults don't actually agree (15s vs 10s here) and drift apart further
// if segment duration or the manifest ever change independently.
const TARGET_LIVE_DELAY_SECONDS = 10

const hlsVideo = ref<HTMLVideoElement | null>(null)
const dashVideo = ref<HTMLVideoElement | null>(null)
const hlsError = ref('')
const dashError = ref('')
const hlsLoading = ref(false)
const dashLoading = ref(false)
const hlsPlaying = ref(false)
const dashPlaying = ref(false)
const hlsPlayheadTime = ref('')
const dashPlayheadTime = ref('')

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let hlsInstance: any = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let dashInstance: any = null

const scriptPromises: Record<string, Promise<void> | undefined> = {}

// --- SCTE-35 marker toast overlay -------------------------------------------
//
// hls.js: EXT-X-DATERANGE tags are exposed as real, natively-timed
// TextTrackCues -- hls.js's ID3TrackController creates a hidden `metadata`
// kind <track> on the video element and appends one VTTCue per DATERANGE
// attribute (id = the DATERANGE's own ID, value = {key: attrName, data:
// attrValue}, start/endTime = the actual media-timeline position). Since
// these are genuine TextTrackCues, the BROWSER's own native cue-timing
// fires `cuechange` at exactly the right playback position -- no need to
// reimplement "has playback reached this timestamp yet" ourselves (an
// earlier version of this did that by hand via LEVEL_UPDATED, which only
// reports when a DATERANGE enters the DVR window -- several segments
// before it's actually reached -- causing toasts unrelated to on-screen
// content).
//
// dash.js: surfaces MPD <Event>/emsg occurrences by registering a listener
// on the event's own schemeIdUri directly ("urn:scte:scte35:2014:xml+bin"
// here, see loop-dee-loop/scte35_signaling.py) -- dash.js already only
// invokes plain (mode-less) listeners at the event's actual presentation
// time (EVENT_MODE_ON_START), so no equivalent gating is needed there.
const SCTE35_DASH_SCHEME = 'urn:scte:scte35:2014:xml+bin'

interface MarkerToast {
  key: string
  label: string
}

const hlsMarkerToasts = ref<MarkerToast[]>([])
const dashMarkerToasts = ref<MarkerToast[]>([])
const hlsSeenActivations = new Set<string>()
const dashSeenEventKeys = new Set<string>()
/** Coincident dash.js SCTE-35 events (same batch of `EventController`
 * callback firings, see attachHls.../playDash's SCTE35_DASH_SCHEME
 * listener) pending their queueMicrotask flush, grouped by payload bytes. */
let dashPendingBatch: Map<string, { bytes: Uint8Array; ids: string[] }> | null = null

function pushMarkerToast(list: typeof hlsMarkerToasts, label: string) {
  const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
  list.value.push({ key, label })
  setTimeout(() => {
    list.value = list.value.filter((t) => t.key !== key)
  }, 5000)
}

/** Wire up hls.js's native "id3"/metadata TextTrack so SCTE35-OUT/IN/CMD
 * cues pop a toast exactly when the browser's own cue timing says they're
 * active -- attaches immediately if the track already exists, and again
 * for any track hls.js adds later (it's created lazily on first fragment
 * with PROGRAM-DATE-TIME, which may be after this runs). The cue's own
 * "SCTE35-OUT"/"SCTE35-IN"/"SCTE35-CMD" key is only used to know a marker
 * fired at all -- describeAllMarkers() decodes the real Start/End (or
 * no-suffix, instant) wording, and each descriptor's own event_id,
 * straight from the SCTE-35 bytes -- never from the DATERANGE `ID`
 * attribute's string shape, which is loop-dee-loop's own signaling
 * convention (see scte35Lite.ts's module docstring) and not something
 * this player needs to understand.
 *
 * By default, several coincident events' DATERANGE tags carry
 * byte-identical SCTE-35 payloads (one shared wire message -- see
 * bake.py's `narrow_scte35_descriptors`), so all not-yet-seen active cues
 * are grouped by (key, payload bytes) first -- each unique payload is
 * decoded, and its toasts pushed, exactly once per activation, no matter
 * how many DATERANGE tags deliver it. `cue.id` (the DATERANGE ID) is only
 * ever used as an opaque per-tag token here, to know whether a given tag
 * has already been processed -- loop-dee-loop scopes it to the loop
 * number, so the same marker's tags are treated as new again next loop. */
function attachHlsMetadataCueListener(video: HTMLVideoElement) {
  const wired = new WeakSet<TextTrack>()

  function wireTrack(track: TextTrack) {
    if (track.kind !== 'metadata' || wired.has(track)) return
    wired.add(track)
    track.addEventListener('cuechange', () => {
      const active = track.activeCues
      if (!active) return

      const groups = new Map<string, { key: string; bytes: Uint8Array; ids: string[] }>()
      for (let i = 0; i < active.length; i++) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const cue = active[i] as any
        const key = cue.value?.key
        if (key !== 'SCTE35-OUT' && key !== 'SCTE35-IN' && key !== 'SCTE35-CMD') continue
        const id = String(cue.id ?? 'unknown')
        if (hlsSeenActivations.has(`${id}:${key}`)) continue
        const bytes = new Uint8Array(cue.value.data as ArrayBuffer)
        const groupKey = `${key}:${bytesToHex(bytes)}`
        const group = groups.get(groupKey) ?? { key, bytes, ids: [] as string[] }
        group.ids.push(id)
        groups.set(groupKey, group)
      }

      for (const { key, bytes, ids } of groups.values()) {
        for (const id of ids) hlsSeenActivations.add(`${id}:${key}`)
        for (const marker of describeAllMarkers(bytes)) {
          pushMarkerToast(hlsMarkerToasts, `${marker.label} · ${marker.eventId ?? '?'}`)
        }
      }
    })
  }

  Array.from(video.textTracks).forEach(wireTrack)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  video.textTracks.addEventListener('addtrack', (e: any) => {
    if (e.track) wireTrack(e.track)
  })
}


function loadScript(src: string, globalName: string): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if ((window as any)[globalName]) return Promise.resolve()
  if (scriptPromises[src]) return scriptPromises[src]
  scriptPromises[src] = new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`)
    if (existing) {
      existing.addEventListener('load', () => resolve())
      existing.addEventListener('error', () => reject(new Error(`Failed to load ${src}`)))
      return
    }
    const el = document.createElement('script')
    el.src = src
    el.async = true
    el.onload = () => resolve()
    el.onerror = () => reject(new Error(`Failed to load ${src}`))
    document.head.appendChild(el)
  })
  return scriptPromises[src]
}

async function playHls() {
  const url = props.hlsUrl
  if (!url) return
  hlsError.value = ''
  hlsLoading.value = true
  hlsPlaying.value = true
  try {
    await loadScript(HLS_CDN, 'Hls')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const Hls = (window as any).Hls
    const video = hlsVideo.value
    if (!video) return
    if (Hls.isSupported()) {
      // hls.js's LatencyController targets liveSyncDuration seconds behind
      // the live edge -- an explicit absolute value (rather than
      // liveSyncDurationCount, a segment-count multiple that only matched
      // dash.js's target by coincidence and drifted apart with any
      // segment-duration change) keeps this aligned with DASH's own
      // liveDelay (see playDash()) at the SAME distance from live, not two
      // independently-derived, different-by-design values.
      //
      // maxLiveSyncPlaybackRate defaults to 1 -- i.e. hls.js computes the
      // target and then is capped from ever actually speeding up to reach
      // it, so any one-off delay (a stall, a slow segment fetch, the
      // initial join itself) is permanent at 1.0x. liveMaxLatencyDuration
      // is the second half of recovering from a BIG one-off stall
      // specifically: past this absolute latency, hls.js seeks forward
      // immediately instead of waiting for a slow multi-minute gradual
      // catch-up at a capped, mild speed-up.
      hlsInstance = new Hls({
        liveSyncDuration: TARGET_LIVE_DELAY_SECONDS,
        maxLiveSyncPlaybackRate: 1.5,
        liveMaxLatencyDuration: TARGET_LIVE_DELAY_SECONDS * 2,
      })
      hlsInstance.loadSource(url)
      hlsInstance.attachMedia(video)
      attachHlsMetadataCueListener(video)
      hlsInstance.on(Hls.Events.ERROR, (_evt: unknown, data: { fatal?: boolean; details?: string }) => {
        if (data?.fatal) hlsError.value = `hls.js fatal error: ${data.details ?? 'unknown'}`
      })
      hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
        hlsLoading.value = false
        video.play().catch(() => {})
      })
      // Playhead position (wall-clock): hls.js exposes the currently-active
      // fragment's own PROGRAM-DATE-TIME (ms epoch) + its start offset on
      // FRAG_CHANGED; map video.currentTime onto that to get the real
      // wall-clock instant currently being displayed -- video.currentTime
      // itself is on hls.js's own internal (non-epoch) timeline, not wall
      // clock, so it can't be shown directly (unlike DASH -- see playDash()).
      let hlsFragProgramDateTime: number | null = null
      let hlsFragStart = 0
      hlsInstance.on(Hls.Events.FRAG_CHANGED, (_evt: unknown, data: { frag?: { programDateTime?: number | null; start?: number } }) => {
        if (data.frag?.programDateTime != null) {
          hlsFragProgramDateTime = data.frag.programDateTime
          hlsFragStart = data.frag.start ?? video.currentTime
        }
      })
      video.addEventListener('timeupdate', () => {
        if (hlsFragProgramDateTime == null) return
        const wallMs = hlsFragProgramDateTime + (video.currentTime - hlsFragStart) * 1000
        hlsPlayheadTime.value = new Date(wallMs).toISOString().replace('T', ' ')
      })
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari: native HLS support, no hls.js needed.
      video.src = url
      video.addEventListener('loadedmetadata', () => {
        hlsLoading.value = false
        video.play().catch(() => {})
      })
    } else {
      hlsError.value = 'HLS is not supported in this browser (and hls.js failed to initialize).'
      hlsLoading.value = false
    }
  } catch (e) {
    hlsError.value = e instanceof Error ? e.message : String(e)
    hlsLoading.value = false
  }
}

async function playDash() {
  const url = props.dashUrl
  if (!url) return
  dashError.value = ''
  dashLoading.value = true
  dashPlaying.value = true
  try {
    await loadScript(DASH_CDN, 'dashjs')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dashjs = (window as any).dashjs
    const video = dashVideo.value
    if (!video) return
    dashInstance = dashjs.MediaPlayer().create()
    // Playhead position (wall-clock): our MPD's availabilityStartTime is
    // 1970-01-01 (unix epoch) by construction (see loop-dee-loop/serve.py),
    // so video.currentTime for a dash.js-driven <video> IS already real
    // Unix epoch seconds directly -- no fragment-metadata mapping needed
    // here, unlike hls.js (see playHls()), whose currentTime lives on its
    // own internal, non-epoch timeline.
    video.addEventListener('timeupdate', () => {
      if (!Number.isFinite(video.currentTime) || video.currentTime <= 0) return
      dashPlayheadTime.value = new Date(video.currentTime * 1000).toISOString().replace('T', ' ')
    })
    // dash.js's live-catchup (actively nudging playback rate to stay near
    // the live edge) defaults to enabled:null -- auto-on only for
    // low-latency (LL-DASH) manifests. Ours is a regular "dynamic" MPD, so
    // it stays off by default and any drift (a stall, a slow segment
    // fetch, ...) is permanent: dash.js just keeps playing at 1.0x forever
    // behind. hls.js has no such off-switch -- its LatencyController
    // continuously corrects for regular HLS too -- which is why the two
    // players visibly diverge over time without this.
    //
    // delay.liveDelay: explicit, matching hls.js's liveSyncDuration (see
    // playHls()) instead of implicitly trusting the MPD's own
    // suggestedPresentationDelay to happen to agree with it.
    //
    // liveCatchup.maxDrift + playbackRate: mild speed-up alone (the
    // default bounds) only closes a SMALL drift in reasonable time --
    // recovering from a big one-off stall (a rebuffer, a slow segment
    // fetch) at a capped ~1.05x can take minutes. Past maxDrift seconds
    // behind target, dash.js seeks forward immediately instead.
    dashInstance.updateSettings({
      streaming: {
        delay: { liveDelay: TARGET_LIVE_DELAY_SECONDS },
        liveCatchup: {
          enabled: true,
          maxDrift: TARGET_LIVE_DELAY_SECONDS * 2,
          playbackRate: { min: -0.5, max: 0.5 },
        },
      },
    })
    dashInstance.initialize(video, url, true)
    dashInstance.on(dashjs.MediaPlayer.events.ERROR, (e: { error?: { message?: string } }) => {
      dashError.value = `dash.js error: ${e?.error?.message ?? 'unknown'}`
    })
    dashInstance.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, () => {
      dashLoading.value = false
    })
    // `e.event.id` is serve.py's own DASH `<Event id>` -- the real, plain
    // segmentation/splice event_id (a decimal int; never parsed here, only
    // ever compared for equality). Start and End of the same break SHARE
    // that event_id by design, and can both be due in the same manifest
    // at once -- so `id` ALONE isn't a unique per-occurrence token here,
    // unlike HLS's DATERANGE `ID`. dash.js also exposes the enclosing
    // <EventStream>'s own `value` attribute on the event object
    // (`e.event.eventStream.value`), which is exactly what serve.py sets
    // to disambiguate Start/End/instant and loop iteration (see
    // serve.py's DASH <Event> authoring) -- combining the two, the same
    // way HLS combines its cue id with the SCTE35-OUT/IN/CMD key, gives a
    // genuinely unique-per-occurrence opaque token.
    //
    // Coincident events (see scte35Lite.ts's module docstring) fire as
    // separate dash.js callback invocations, but genuinely simultaneous
    // ones (same presentationTime) are dispatched synchronously
    // back-to-back within one JS turn (confirmed against dash.js's own
    // EventController source) -- queueMicrotask collects everything
    // dash.js fires in that turn into one batch, grouped by payload
    // bytes, so each unique underlying SCTE-35 message is decoded and
    // announced exactly once no matter how many <Event> elements deliver
    // it.
    dashInstance.on(
      SCTE35_DASH_SCHEME,
      (e: { event?: { id?: string; eventStream?: { value?: string }; messageData?: Uint8Array } }) => {
        const rawId = e.event?.id
        const streamValue = e.event?.eventStream?.value
        const bytes = e.event?.messageData
        if (rawId == null || !bytes) return
        const id = `${rawId}:${streamValue ?? ''}`
        if (dashSeenEventKeys.has(id)) return

        if (!dashPendingBatch) {
          dashPendingBatch = new Map()
          queueMicrotask(() => {
            const batch = dashPendingBatch
            dashPendingBatch = null
            if (!batch) return
            for (const { bytes: groupBytes, ids } of batch.values()) {
              for (const groupId of ids) dashSeenEventKeys.add(groupId)
              for (const marker of describeAllMarkers(groupBytes)) {
                pushMarkerToast(dashMarkerToasts, `${marker.label} · ${marker.eventId ?? '?'}`)
              }
            }
          })
        }
        const groupKey = bytesToHex(bytes)
        const group = dashPendingBatch.get(groupKey) ?? { bytes, ids: [] as string[] }
        group.ids.push(id)
        dashPendingBatch.set(groupKey, group)
      },
    )
  } catch (e) {
    dashError.value = e instanceof Error ? e.message : String(e)
    dashLoading.value = false
  }
}

function destroyHls() {
  if (hlsInstance) {
    hlsInstance.destroy()
    hlsInstance = null
  }
  if (hlsVideo.value) {
    hlsVideo.value.removeAttribute('src')
    hlsVideo.value.load()
  }
  hlsPlaying.value = false
  hlsLoading.value = false
  hlsError.value = ''
  hlsSeenActivations.clear()
  hlsMarkerToasts.value = []
  hlsPlayheadTime.value = ''
}

function destroyDash() {
  if (dashInstance) {
    dashInstance.reset()
    dashInstance = null
  }
  if (dashVideo.value) {
    dashVideo.value.removeAttribute('src')
    dashVideo.value.load()
  }
  dashPlaying.value = false
  dashLoading.value = false
  dashError.value = ''
  dashSeenEventKeys.clear()
  dashPendingBatch = null
  dashMarkerToasts.value = []
  dashPlayheadTime.value = ''
}

// If the channel gets redeployed/refreshed with new URLs, stop rather than
// silently keep playing a stale stream.
watch(() => props.hlsUrl, destroyHls)
watch(() => props.dashUrl, destroyDash)
onBeforeUnmount(() => {
  destroyHls()
  destroyDash()
})

// Autoplay both players as soon as a URL is available -- no need to click
// "Play HLS"/"Play DASH" manually every time.
watch(() => props.hlsUrl, (url) => { if (url) playHls() }, { immediate: true })
watch(() => props.dashUrl, (url) => { if (url) playDash() }, { immediate: true })

function openUrl(url?: string | null) {
  if (url) window.open(url, '_blank')
}

async function copyUrl(url?: string | null) {
  if (!url) return
  try {
    await navigator.clipboard.writeText(url)
    toast.add({ severity: 'success', summary: 'Copied to clipboard', life: 2000 })
  } catch {
    toast.add({ severity: 'error', summary: 'Copy failed', detail: url, life: 4000 })
  }
}
</script>

<template>
  <div class="playback-panel">
    <div class="playback-header">
      <div class="flex align-items-center gap-2">
        <i class="pi pi-circle-fill live-dot" />
        <h3 class="m-0">Live Playback</h3>
      </div>
      <div v-if="health" class="playback-stats">
        <Tag severity="info" :value="`loop #${health.loop_number}`" />
        <Tag
          severity="secondary"
          :value="`${health.position_in_loop_seconds.toFixed(1)}s / ${health.total_loop_duration_seconds.toFixed(1)}s`"
        />
        <Tag severity="secondary" :value="`uptime ${health.uptime_seconds.toFixed(0)}s`" />
        <Tag severity="secondary" :value="health.renditions.join(', ')" />
      </div>
    </div>

    <Message v-if="healthError" severity="warn" :closable="false">
      No health data yet ({{ healthError }}) -- the deployed task may predate the /health endpoint;
      redeploy/refresh to pick it up.
    </Message>

    <div class="players-grid">
      <div v-if="hlsUrl" class="player-card">
        <div class="player-card-header">
          <span class="font-semibold text-sm">HLS</span>
          <Button v-if="hlsPlaying" icon="pi pi-stop-circle" text size="small" severity="secondary" label="Stop" @click="destroyHls" />
        </div>
        <div class="video-wrapper">
          <video ref="hlsVideo" controls muted playsinline class="player-video" />
          <div v-if="hlsLoading" class="player-overlay"><i class="pi pi-spin pi-spinner" /></div>
          <button v-if="!hlsPlaying" class="play-overlay" @click="playHls">
            <i class="pi pi-play-circle" />
            <span>Play HLS</span>
          </button>
          <TransitionGroup name="marker-toast" tag="div" class="marker-toast-stack">
            <div v-for="t in hlsMarkerToasts" :key="t.key" class="marker-toast">
              <i class="pi pi-bell" />
              <span>{{ t.label }}</span>
            </div>
          </TransitionGroup>
        </div>
        <Message v-if="hlsError" severity="error" :closable="false" class="text-xs">{{ hlsError }}</Message>
        <div v-if="hlsPlayheadTime" class="playhead-row">
          <i class="pi pi-clock" />
          <span>Playhead: {{ hlsPlayheadTime }}</span>
        </div>
        <div class="url-row">
          <input class="url-input" type="text" readonly :value="hlsUrl" @focus="($event.target as HTMLInputElement).select()" />
          <Button icon="pi pi-copy" text size="small" title="Copy URL" @click="copyUrl(hlsUrl)" />
          <Button icon="pi pi-external-link" text size="small" title="Open in new tab" @click="openUrl(hlsUrl)" />
        </div>
      </div>

      <div v-if="dashUrl" class="player-card">
        <div class="player-card-header">
          <span class="font-semibold text-sm">DASH</span>
          <Button v-if="dashPlaying" icon="pi pi-stop-circle" text size="small" severity="secondary" label="Stop" @click="destroyDash" />
        </div>
        <div class="video-wrapper">
          <video ref="dashVideo" controls muted playsinline class="player-video" />
          <div v-if="dashLoading" class="player-overlay"><i class="pi pi-spin pi-spinner" /></div>
          <button v-if="!dashPlaying" class="play-overlay" @click="playDash">
            <i class="pi pi-play-circle" />
            <span>Play DASH</span>
          </button>
          <TransitionGroup name="marker-toast" tag="div" class="marker-toast-stack">
            <div v-for="t in dashMarkerToasts" :key="t.key" class="marker-toast">
              <i class="pi pi-bell" />
              <span>{{ t.label }}</span>
            </div>
          </TransitionGroup>
        </div>
        <Message v-if="dashError" severity="error" :closable="false" class="text-xs">{{ dashError }}</Message>
        <div v-if="dashPlayheadTime" class="playhead-row">
          <i class="pi pi-clock" />
          <span>Playhead: {{ dashPlayheadTime }}</span>
        </div>
        <div class="url-row">
          <input class="url-input" type="text" readonly :value="dashUrl" @focus="($event.target as HTMLInputElement).select()" />
          <Button icon="pi pi-copy" text size="small" title="Copy URL" @click="copyUrl(dashUrl)" />
          <Button icon="pi pi-external-link" text size="small" title="Open in new tab" @click="openUrl(dashUrl)" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.playback-panel {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1.25rem;
  border-radius: 12px;
  background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%);
  color: #f1f5f9;
}

.playback-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.playback-header h3 {
  color: #f8fafc;
}

.live-dot {
  color: #ef4444;
  font-size: 0.55rem;
  animation: pulse 1.6s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}

.playback-stats {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}

.players-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
}

.player-card {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  padding: 0.75rem;
}

.player-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #cbd5e1;
}

.video-wrapper {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #000;
  border-radius: 6px;
  overflow: hidden;
}

.player-video {
  width: 100%;
  height: 100%;
  display: block;
  background: #000;
}

.player-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #e2e8f0;
  font-size: 1.5rem;
  background: rgba(0, 0, 0, 0.35);
  pointer-events: none;
}

.play-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  background: rgba(15, 23, 42, 0.55);
  color: #f8fafc;
  border: none;
  cursor: pointer;
  font-size: 0.9rem;
}

.play-overlay i {
  font-size: 3rem;
}

.play-overlay:hover {
  background: rgba(15, 23, 42, 0.7);
}

.marker-toast-stack {
  position: absolute;
  top: 0.5rem;
  left: 0.5rem;
  right: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  pointer-events: none;
  z-index: 2;
}

.marker-toast {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  align-self: flex-start;
  max-width: 100%;
  padding: 0.35rem 0.65rem;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.85);
  border: 1px solid rgba(96, 165, 250, 0.5);
  color: #e0f2fe;
  font-size: 0.75rem;
  font-family: var(--font-mono, monospace);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
}

.marker-toast i {
  color: #60a5fa;
  font-size: 0.8rem;
}

.marker-toast-enter-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.marker-toast-leave-active {
  transition: opacity 0.4s ease, transform 0.4s ease;
}

.marker-toast-enter-from {
  opacity: 0;
  transform: translateY(-6px);
}

.marker-toast-leave-to {
  opacity: 0;
  transform: translateX(8px);
}

.playhead-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  color: #94a3b8;
  font-size: 0.75rem;
  font-family: var(--font-mono, monospace);
}

.url-row {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  padding: 0.15rem 0.15rem 0.15rem 0.6rem;
}

.url-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  color: #e2e8f0;
  font-size: 0.8rem;
  font-family: var(--font-mono, monospace);
}
</style>
