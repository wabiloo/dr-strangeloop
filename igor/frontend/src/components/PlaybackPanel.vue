<script setup lang="ts">
import Message from 'primevue/message'
import Button from 'primevue/button'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import type { ChannelHealth } from '../api/types'
import { bytesToHex, describeAllMarkers, markerToastLabel, type Scte35MarkerKind } from '../scte35Lite'

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
const dashIsPlaying = ref(false)
const dashMuted = ref(true)
const dashVolume = ref(1)
const dashFullscreen = ref(false)
const hlsIsPlaying = ref(false)
const hlsMuted = ref(true)
const hlsVolume = ref(1)
const hlsFullscreen = ref(false)
const hlsPlayheadTime = ref('')
const dashPlayheadTime = ref('')
const hlsDvrWindowSeconds = computed(() => Math.max(0, hlsSeekMax.value - hlsSeekMin.value))
const dashDvrWindowSeconds = computed(() => Math.max(0, dashSeekMax.value - dashSeekMin.value))

function formatDvrWindow(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds - minutes * 60
  return remainder >= 0.5 ? `${minutes}m ${remainder.toFixed(0)}s` : `${minutes}m`
}

// --- DASH custom DVR seekbar -------------------------------------------
//
// Why not the native <video controls> scrubber: our MPD's
// availabilityStartTime is pinned to the Unix epoch (see playDash()'s own
// comment above, and loop-dee-loop/serve.py), so a long-running channel's
// nominal MPD duration is "years since 1970" -- dash.js sets that as
// `video.duration`, and the BROWSER draws its native seekbar's full track
// across that entire nominal span. The actual DVR window
// (timeShiftBufferDepth, tens of seconds) is then a fraction of a single
// pixel of that track, so dragging the native scrubber anywhere visually
// "back" lands outside `video.seekable` and just snaps back to the live
// edge -- not a bug in dash.js, just the wrong UI for this kind of stream.
//
// `video.seekable` itself is NOT affected by that -- it's a TimeRanges
// object dash.js keeps accurate to the real buffered/available DVR window
// (same units as `video.currentTime`) regardless of how large `duration`
// is, so building our own slider directly off `seekable`/`currentTime` --
// standard HTMLMediaElement, no dash.js-version-specific API needed --
// sidesteps the whole problem.
const dashSeekMin = ref(0)
const dashSeekMax = ref(0)
const dashSeekSliderMax = computed(() => Math.max(dashSeekMin.value, dashSeekMax.value - 0.1))
const dashSeekValue = ref(0)
const dashSeekDragging = ref(false)
const dashSeekPending = ref(false)
const dashAtLiveEdge = ref(true)
let dashDvrPollTimer: ReturnType<typeof setInterval> | null = null
let dashSeekCommitTimer: ReturnType<typeof setTimeout> | null = null
const hlsSeekMin = ref(0)
const hlsSeekMax = ref(0)
const hlsSeekSliderMax = computed(() => Math.max(hlsSeekMin.value, hlsSeekMax.value - 0.1))
const hlsSeekValue = ref(0)
const hlsSeekDragging = ref(false)
const hlsSeekPending = ref(false)
const hlsAtLiveEdge = ref(true)
let hlsDvrPollTimer: ReturnType<typeof setInterval> | null = null
let hlsSeekCommitTimer: ReturnType<typeof setTimeout> | null = null
let hlsOriginalMaxLatency: number | undefined
let hlsDvrMode = false
let hlsGeneration = 0
let hlsFragProgramDateTime: number | null = null
let hlsFragStart = 0
let hlsNativePlayback = false

function onHlsTimeUpdate() {
  const video = hlsVideo.value
  if (!video) return
  updateHlsDvrRange()
  if (hlsFragProgramDateTime == null) return
  const wallMs = hlsFragProgramDateTime + (video.currentTime - hlsFragStart) * 1000
  hlsPlayheadTime.value = new Date(wallMs).toISOString().replace('T', ' ')
}

function onHlsLoadedMetadata() {
  const video = hlsVideo.value
  if (!video) return
  syncHlsVideoState()
  if (!hlsNativePlayback) return
  hlsLoading.value = false
  video.play().then(syncHlsVideoState).catch(() => {
    hlsError.value = 'Playback was blocked. Use the play button to start the stream.'
    hlsLoading.value = false
    hlsPlaying.value = false
  })
}

function updateHlsDvrRange() {
  const video = hlsVideo.value
  if (!video || video.seekable.length === 0) {
    hlsSeekMin.value = 0
    hlsSeekMax.value = 0
    hlsSeekValue.value = 0
    return
  }
  const idx = video.seekable.length - 1
  let seekableStart: number
  let seekableEnd: number
  try {
    seekableStart = video.seekable.start(idx)
    seekableEnd = video.seekable.end(idx)
  } catch {
    return
  }

  // MediaSource's seekable range can retain buffered HLS history beyond
  // the segments still advertised by the current live playlist. Bound the
  // DVR slider to that playlist window, then intersect with media seekable
  // so slider targets remain positions the browser can actually seek to.
  const details = hlsInstance?.latestLevelDetails
  if (details?.live && Number.isFinite(details.fragmentStart) && Number.isFinite(details.edge)) {
    hlsSeekMin.value = Math.max(details.fragmentStart, seekableStart)
    hlsSeekMax.value = Math.min(details.edge, seekableEnd)
  } else {
    // Native HLS (e.g. Safari) does not expose hls.js playlist details.
    hlsSeekMin.value = seekableStart
    hlsSeekMax.value = seekableEnd
  }
  if (hlsSeekMax.value <= hlsSeekMin.value) {
    hlsSeekMin.value = 0
    hlsSeekMax.value = 0
    hlsSeekValue.value = 0
    return
  }
  if (!hlsSeekDragging.value && !hlsSeekPending.value) {
    hlsSeekValue.value = Math.min(
      Math.max(hlsSeekMin.value, video.currentTime),
      Math.max(hlsSeekMin.value, hlsSeekMax.value - 0.1),
    )
  }
  if (hlsDvrMode && hlsInstance) {
    hlsInstance.config.liveMaxLatencyDuration =
      hlsSeekMax.value - hlsSeekMin.value + TARGET_LIVE_DELAY_SECONDS * 2
  }
  hlsAtLiveEdge.value = hlsSeekMax.value - video.currentTime <= TARGET_LIVE_DELAY_SECONDS + 1
}

function hlsBehindLiveLabel(value: number): string {
  const behind = Math.max(0, hlsSeekMax.value - value)
  return behind < 1 ? 'LIVE' : `-${behind.toFixed(1)}s`
}

function onHlsSeekStart() {
  hlsSeekDragging.value = true
}

function onHlsSeekInput(value: number) {
  hlsSeekValue.value = value
}

function onHlsSeekKeyDown(event: KeyboardEvent) {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
    onHlsSeekStart()
  }
}

function onHlsSeekCommit(value: number) {
  const video = hlsVideo.value
  if (video && hlsSeekMax.value > hlsSeekMin.value) {
    const target = Math.min(hlsSeekSliderMax.value, Math.max(hlsSeekMin.value, value))
    if (hlsInstance) {
      // HLS latency control normally jumps playback to liveSyncPosition
      // once latency exceeds liveMaxLatencyDuration. Let the player use
      // the full available DVR range until LIVE is explicitly selected.
      if (hlsOriginalMaxLatency === undefined) {
        hlsOriginalMaxLatency = hlsInstance.config.liveMaxLatencyDuration
      }
      hlsInstance.config.liveMaxLatencyDuration =
        hlsSeekMax.value - hlsSeekMin.value + TARGET_LIVE_DELAY_SECONDS * 2
    }
    hlsDvrMode = true
    hlsSeekValue.value = target
    if (Math.abs(video.currentTime - target) < 0.05) {
      hlsSeekDragging.value = false
    } else {
      hlsSeekPending.value = true
      if (hlsSeekCommitTimer != null) clearTimeout(hlsSeekCommitTimer)
      hlsSeekCommitTimer = setTimeout(() => {
        hlsSeekCommitTimer = null
        hlsSeekPending.value = false
        hlsSeekDragging.value = false
        syncHlsVideoState()
      }, 5000)
      try {
        video.currentTime = target
      } catch {
        clearTimeout(hlsSeekCommitTimer)
        hlsSeekCommitTimer = null
        hlsSeekPending.value = false
        hlsSeekDragging.value = false
        syncHlsVideoState()
      }
    }
  }
  if (!hlsSeekPending.value) hlsSeekDragging.value = false
}

async function goHlsLive() {
  const video = hlsVideo.value
  if (!video) return
  updateHlsDvrRange()
  const target = Math.max(hlsSeekMin.value, Math.min(
    hlsSeekSliderMax.value,
    hlsSeekMax.value - TARGET_LIVE_DELAY_SECONDS,
  ))
  if (hlsInstance && hlsOriginalMaxLatency !== undefined) {
    hlsInstance.config.liveMaxLatencyDuration = hlsOriginalMaxLatency
  }
  hlsDvrMode = false
  hlsOriginalMaxLatency = undefined
  hlsDvrMode = false
  video.currentTime = target
  hlsSeekValue.value = target
  hlsSeekDragging.value = false
  hlsSeekPending.value = false
  if (hlsSeekCommitTimer != null) {
    clearTimeout(hlsSeekCommitTimer)
    hlsSeekCommitTimer = null
  }
  if (video.paused) {
    try {
      await video.play()
    } catch (e) {
      hlsError.value = e instanceof Error ? e.message : String(e)
    }
  }
}

function syncHlsVideoState() {
  const video = hlsVideo.value
  if (!video) return
  hlsIsPlaying.value = !video.paused && !video.ended
  hlsMuted.value = video.muted
  hlsVolume.value = video.volume
  updateHlsDvrRange()
}

async function toggleHlsPlayback() {
  const video = hlsVideo.value
  if (!video) return
  if (video.paused) {
    try {
      await video.play()
    } catch (e) {
      hlsError.value = e instanceof Error ? e.message : String(e)
    }
  } else {
    video.pause()
  }
}

function toggleHlsMute() {
  const video = hlsVideo.value
  if (!video) return
  video.muted = !video.muted
  syncHlsVideoState()
}

function onHlsVolumeInput(value: number) {
  const video = hlsVideo.value
  if (!video) return
  video.volume = Math.min(1, Math.max(0, value))
  if (video.volume > 0 && video.muted) video.muted = false
  syncHlsVideoState()
}

async function toggleHlsFullscreen() {
  const wrapper = hlsVideo.value?.closest('.hls-player-shell')
  if (!wrapper) return
  try {
    if (document.fullscreenElement === wrapper) await document.exitFullscreen()
    else await wrapper.requestFullscreen()
  } catch {
    hlsError.value = 'Fullscreen is not available in this browser.'
  }
}

function onHlsFullscreenChange() {
  hlsFullscreen.value = document.fullscreenElement === hlsVideo.value?.closest('.hls-player-shell')
}

function onHlsSeekPointerCancel(event: PointerEvent) {
  const input = event.currentTarget as HTMLInputElement
  hlsSeekDragging.value = false
  hlsSeekPending.value = false
  if (hlsSeekCommitTimer != null) {
    clearTimeout(hlsSeekCommitTimer)
    hlsSeekCommitTimer = null
  }
  input.value = String(hlsSeekValue.value)
  syncHlsVideoState()
}

function onHlsSeeked() {
  hlsSeekPending.value = false
  hlsSeekDragging.value = false
  if (hlsSeekCommitTimer != null) {
    clearTimeout(hlsSeekCommitTimer)
    hlsSeekCommitTimer = null
  }
  syncHlsVideoState()
}

/** Refresh the DVR window + slider position from `video.seekable`. Skips
 * overwriting `dashSeekValue` while the user has the thumb grabbed, so
 * their drag isn't fought by playback continuing to advance underneath
 * it. Called from `timeupdate`/`progress` (the window only grows/slides
 * while segments actually arrive) and from a cheap poll timer as a
 * fallback -- dash.js can keep the buffer near live topped up via
 * background fetches even while paused, which doesn't reliably fire
 * either of those events on the <video> element itself. */
function updateDashDvrRange() {
  const video = dashVideo.value
  if (!video || video.seekable.length === 0) {
    dashSeekMin.value = 0
    dashSeekMax.value = 0
    dashSeekValue.value = 0
    return
  }
  const idx = video.seekable.length - 1
  try {
    dashSeekMin.value = video.seekable.start(idx)
    dashSeekMax.value = video.seekable.end(idx)
  } catch {
    // The browser can invalidate a TimeRanges object while the MPD's DVR
    // window is being replaced. The next poll/event will retry.
    return
  }
  if (!dashSeekDragging.value && !dashSeekPending.value) {
    dashSeekValue.value = Math.min(
      Math.max(dashSeekMin.value, video.currentTime),
      Math.max(dashSeekMin.value, dashSeekMax.value - 0.1),
    )
  }
  dashAtLiveEdge.value = dashSeekMax.value - video.currentTime <= TARGET_LIVE_DELAY_SECONDS + 1
}

/** Seconds behind the live edge for a given DVR-slider position -- 0 (or
 * negative, clamped) right at the live edge, growing as you scrub back.
 * Shown instead of an absolute time: "how far back am I" is what matters
 * for DVR scrubbing, not the underlying epoch-anchored absolute number. */
function dashBehindLiveLabel(value: number): string {
  const behind = Math.max(0, dashSeekMax.value - value)
  return behind < 1 ? 'LIVE' : `-${behind.toFixed(1)}s`
}

function onDashSeekStart() {
  dashSeekDragging.value = true
}

function onDashSeekInput(value: number) {
  dashSeekValue.value = value
}

function onDashSeekKeyDown(event: KeyboardEvent) {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
    onDashSeekStart()
  }
}

function onDashSeekCommit(value: number) {
  const video = dashVideo.value
  if (video && dashSeekMax.value > dashSeekMin.value) {
    const target = Math.min(dashSeekSliderMax.value, Math.max(dashSeekMin.value, value))
    // A DVR seek is an explicit choice to watch time-shifted content. Turn
    // off dash.js's live catch-up, or it will undo the seek to meet its
    // target live delay.
    dashInstance?.updateSettings({ streaming: { liveCatchup: { enabled: false } } })
    dashSeekValue.value = target
    if (Math.abs(video.currentTime - target) < 0.05) {
      dashSeekDragging.value = false
    } else {
      dashSeekPending.value = true
      if (dashSeekCommitTimer != null) clearTimeout(dashSeekCommitTimer)
      dashSeekCommitTimer = setTimeout(() => {
        dashSeekCommitTimer = null
        dashSeekPending.value = false
        dashSeekDragging.value = false
        syncDashVideoState()
      }, 5000)
      try {
        video.currentTime = target
      } catch {
        clearTimeout(dashSeekCommitTimer)
        dashSeekCommitTimer = null
        dashSeekPending.value = false
        dashSeekDragging.value = false
        syncDashVideoState()
      }
    }
  }
  if (!dashSeekPending.value) dashSeekDragging.value = false
}

async function goDashLive() {
  const video = dashVideo.value
  if (!video) return
  updateDashDvrRange()
  const target = Math.max(dashSeekMin.value, Math.min(
    dashSeekSliderMax.value,
    dashSeekMax.value - TARGET_LIVE_DELAY_SECONDS,
  ))
  // Restore normal live catch-up only when the viewer asks to rejoin live.
  dashInstance?.updateSettings({ streaming: { liveCatchup: { enabled: true } } })
  video.currentTime = target
  dashSeekValue.value = target
  dashSeekDragging.value = false
  dashSeekPending.value = false
  if (dashSeekCommitTimer != null) {
    clearTimeout(dashSeekCommitTimer)
    dashSeekCommitTimer = null
  }
  if (video.paused) {
    try {
      await video.play()
    } catch (e) {
      dashError.value = e instanceof Error ? e.message : String(e)
    }
  }
}

function syncDashVideoState() {
  const video = dashVideo.value
  if (!video) return
  dashIsPlaying.value = !video.paused && !video.ended
  dashMuted.value = video.muted
  dashVolume.value = video.volume
  if (Number.isFinite(video.currentTime) && video.currentTime > 0) {
    dashPlayheadTime.value = new Date(video.currentTime * 1000).toISOString().replace('T', ' ')
  }
  updateDashDvrRange()
}

async function toggleDashPlayback() {
  const video = dashVideo.value
  if (!video) return
  if (video.paused) {
    try {
      await video.play()
    } catch (e) {
      dashError.value = e instanceof Error ? e.message : String(e)
    }
  } else {
    video.pause()
  }
}

function toggleDashMute() {
  const video = dashVideo.value
  if (!video) return
  video.muted = !video.muted
  syncDashVideoState()
}

function onDashVolumeInput(value: number) {
  const video = dashVideo.value
  if (!video) return
  video.volume = Math.min(1, Math.max(0, value))
  if (video.volume > 0 && video.muted) video.muted = false
  syncDashVideoState()
}

async function toggleDashFullscreen() {
  const wrapper = dashVideo.value?.closest('.dash-player-shell')
  if (!wrapper) return
  try {
    if (document.fullscreenElement === wrapper) {
      await document.exitFullscreen()
    } else {
      await wrapper.requestFullscreen()
    }
  } catch {
    dashError.value = 'Fullscreen is not available in this browser.'
  }
}

function onDashFullscreenChange() {
  dashFullscreen.value = document.fullscreenElement === dashVideo.value?.closest('.dash-player-shell')
}

function onDashSeekPointerCancel(event: PointerEvent) {
  const input = event.currentTarget as HTMLInputElement
  dashSeekDragging.value = false
  dashSeekPending.value = false
  if (dashSeekCommitTimer != null) {
    clearTimeout(dashSeekCommitTimer)
    dashSeekCommitTimer = null
  }
  input.value = String(dashSeekValue.value)
  syncDashVideoState()
}

function onDashTimeUpdate() {
  syncDashVideoState()
}

function onDashSeeked() {
  dashSeekPending.value = false
  dashSeekDragging.value = false
  if (dashSeekCommitTimer != null) {
    clearTimeout(dashSeekCommitTimer)
    dashSeekCommitTimer = null
  }
  syncDashVideoState()
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let hlsInstance: any = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let dashInstance: any = null
let dashGeneration = 0

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
// here, see loop-dee-loop/scte35_signaling.py).
const SCTE35_DASH_SCHEME = 'urn:scte:scte35:2014:xml+bin'

// Both players, on first join, immediately surface every marker already
// within their initial window as "active" -- hls.js because ALL DATERANGE
// tags in the freshly-loaded DVR window (not just ones ahead of where we
// joined) become active TextTrackCues at once; dash.js because it catches
// up through every <Event> whose presentationTime already precedes the
// current position as soon as the MPD's initial Period(s) are parsed.
// Neither is "wrong" -- those markers genuinely already happened within
// the window -- but announcing that whole backlog as if it just occurred
// is misleading (and was the literal complaint: opening the page showed
// toasts for markers from loops ago).
//
// Rather than guess how long that initial catch-up takes (a fixed
// "suppress everything for N seconds after join" window -- tried first,
// but that either cuts a slow join's backlog short or wrongly swallows a
// genuinely new marker landing early), compare the marker's OWN
// presentation time against the player's actual current position, on
// BOTH players' native timelines:
//   - hls.js: `cue.startTime` is already the real media-timeline position
//     hls.js computed for this DATERANGE (that's what drives the
//     browser's own native cue-timing in the first place) -- directly
//     comparable to `video.currentTime`.
//   - dash.js: `event.calculatedPresentationTime` is what dash.js's own
//     EventController compares against its internal "current video time"
//     before ever dispatching a listener (confirmed against its source:
//     `event.calculatedPresentationTime <= currentVideoTime`) -- equally
//     directly comparable to `video.currentTime` here.
// A marker whose own position is more than this far behind the current
// position is backlog (it already happened before we started watching,
// however recently); anything closer is a genuine just-now activation.
// One shared constant since both comparisons land on the same kind of
// timeline -- no need for the two guessed, player-specific numbers a
// join-time window required.
const MARKER_STALE_THRESHOLD_SECONDS = 2

// Single source of truth for how long a toast stays mounted -- also drives
// its CSS fade-out (see the `--marker-toast-life` custom property below and
// .marker-toast's `animation` rule), so the two can never drift apart the
// way two independently-chosen numbers could.
const MARKER_TOAST_LIFE_MS = 4000

interface MarkerToast {
  key: string
  label: string
  kind: Scte35MarkerKind
}

const hlsMarkerToasts = ref<MarkerToast[]>([])
const dashMarkerToasts = ref<MarkerToast[]>([])
const hlsSeenActivations = new Set<string>()
const dashSeenEventKeys = new Set<string>()
/** Coincident dash.js SCTE-35 events (same batch of `EventController`
 * callback firings, see attachHls.../playDash's SCTE35_DASH_SCHEME
 * listener) pending their queueMicrotask flush, grouped by payload bytes. */
let dashPendingBatch: Map<string, { bytes: Uint8Array; presentationTime: number; ids: string[] }> | null = null

/** Icon per Scte35MarkerKind -- Start/End/Other get visually distinct
 * treatment (not just wording) so a run of toasts is scannable at a
 * glance: sign-out (leaving the main program, e.g. an ad break starting)
 * for Start, sign-in (returning to it) for End -- matching SCTE-35/HLS's
 * own CUE-OUT/CUE-IN vocabulary for the same two halves -- and bell (the
 * original, kind-less icon) for everything without a Start/End pairing
 * (instant signals, bare splice/Time Signal commands, ...). Color lives
 * in CSS (.marker-toast-start/-end/-other below), keyed off the same
 * `kind` value via a class binding -- this table is only the icon. */
const MARKER_KIND_ICON: Record<Scte35MarkerKind, string> = {
  start: 'pi pi-sign-out',
  end: 'pi pi-sign-in',
  other: 'pi pi-bell',
}

function pushMarkerToast(list: typeof hlsMarkerToasts, label: string, kind: Scte35MarkerKind) {
  const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
  list.value.push({ key, label, kind })
  setTimeout(() => {
    list.value = list.value.filter((t) => t.key !== key)
  }, MARKER_TOAST_LIFE_MS)
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
 * bake.py's `--daterange-mode` / `[markers] daterange_mode`), so all
 * not-yet-seen active cues
 * are grouped by payload bytes ALONE first (never combined with `key` --
 * see the comment at the grouping loop below for why that matters) --
 * each unique payload is decoded, and its toasts pushed, exactly once per
 * activation, no matter how many DATERANGE tags (even across different
 * OUT/IN/CMD keys) deliver it. `cue.id` (the DATERANGE ID) is only ever
 * used as an opaque per-tag token here, to know whether a given tag has
 * already been processed -- loop-dee-loop scopes it to the loop number,
 * so the same marker's tags are treated as new again next loop. */
function attachHlsMetadataCueListener(video: HTMLVideoElement) {
  const wired = new WeakSet<TextTrack>()

  function wireTrack(track: TextTrack) {
    if (track.kind !== 'metadata' || wired.has(track)) return
    wired.add(track)
    track.addEventListener('cuechange', () => {
      const active = track.activeCues
      if (!active) return

      // Grouped by payload bytes ALONE, never combined with `key`: the
      // default (daterange_mode="shared") multi-descriptor message
      // routinely backs cues with DIFFERENT keys at once (e.g. a
      // coincident Break Start[OUT] + Ad End[IN] + Call Ad Server[CMD]
      // all merged into one wire message -- see bake.py's
      // `--daterange-mode` / `[markers] daterange_mode`).
      // describeAllMarkers() decodes the WHOLE message regardless of
      // which key led us to it, so grouping by (key, bytes) -- as an
      // earlier version of this did -- created one group PER DISTINCT KEY
      // sharing that payload, and each group independently re-announced
      // every event in it: a payload shared across OUT+IN+CMD got
      // announced 3 times over, not once.
      const groups = new Map<
        string,
        { bytes: Uint8Array; startTime: number; idKeys: { id: string; key: string }[] }
      >()
      for (let i = 0; i < active.length; i++) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const cue = active[i] as any
        const key = cue.value?.key
        if (key !== 'SCTE35-OUT' && key !== 'SCTE35-IN' && key !== 'SCTE35-CMD') continue
        const id = String(cue.id ?? 'unknown')
        if (hlsSeenActivations.has(`${id}:${key}`)) continue
        const bytes = new Uint8Array(cue.value.data as ArrayBuffer)
        const groupKey = bytesToHex(bytes)
        const group = groups.get(groupKey) ?? {
          bytes,
          startTime: cue.startTime as number,
          idKeys: [] as { id: string; key: string }[],
        }
        group.idKeys.push({ id, key })
        groups.set(groupKey, group)
      }

      for (const { bytes, startTime, idKeys } of groups.values()) {
        for (const { id, key } of idKeys) hlsSeenActivations.add(`${id}:${key}`)
        // Backlog, not a fresh activation (see MARKER_STALE_THRESHOLD_SECONDS)
        // -- still marked seen above, so it never re-announces later.
        if (video.currentTime - startTime > MARKER_STALE_THRESHOLD_SECONDS) continue
        for (const marker of describeAllMarkers(bytes)) {
          pushMarkerToast(hlsMarkerToasts, markerToastLabel(marker), marker.kind)
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
  const existingVideo = hlsVideo.value
  if (existingVideo && (hlsInstance || hlsNativePlayback) && existingVideo.currentSrc) {
    hlsError.value = ''
    hlsPlaying.value = true
    try {
      await existingVideo.play()
      syncHlsVideoState()
    } catch (e) {
      hlsPlaying.value = false
      hlsError.value = e instanceof Error ? e.message : String(e)
    }
    return
  }
  const generation = ++hlsGeneration
  hlsError.value = ''
  hlsLoading.value = true
  hlsPlaying.value = true
  try {
    await loadScript(HLS_CDN, 'Hls')
    if (generation !== hlsGeneration) return
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const Hls = (window as any).Hls
    const video = hlsVideo.value
    if (!video) return
    video.addEventListener('timeupdate', onHlsTimeUpdate)
    video.addEventListener('progress', updateHlsDvrRange)
    video.addEventListener('play', syncHlsVideoState)
    video.addEventListener('playing', syncHlsVideoState)
    video.addEventListener('pause', syncHlsVideoState)
    video.addEventListener('ended', syncHlsVideoState)
    video.addEventListener('volumechange', syncHlsVideoState)
    video.addEventListener('seeked', onHlsSeeked)
    video.addEventListener('loadedmetadata', onHlsLoadedMetadata)
    document.addEventListener('fullscreenchange', onHlsFullscreenChange)
    hlsDvrPollTimer = setInterval(updateHlsDvrRange, 2000)
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
      hlsOriginalMaxLatency = hlsInstance.config.liveMaxLatencyDuration
      hlsInstance.loadSource(url)
      hlsInstance.attachMedia(video)
      attachHlsMetadataCueListener(video)
      hlsInstance.on(Hls.Events.ERROR, (_evt: unknown, data: { fatal?: boolean; details?: string }) => {
        if (data?.fatal) hlsError.value = `hls.js fatal error: ${data.details ?? 'unknown'}`
      })
      hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
        hlsLoading.value = false
        video.play().then(syncHlsVideoState).catch(() => {
          hlsError.value = 'Playback was blocked. Use the play button to start the stream.'
          hlsLoading.value = false
          hlsPlaying.value = false
        })
      })
      // Playhead position (wall-clock): hls.js exposes the currently-active
      // fragment's own PROGRAM-DATE-TIME (ms epoch) + its start offset on
      // FRAG_CHANGED; map video.currentTime onto that to get the real
      // wall-clock instant currently being displayed -- video.currentTime
      // itself is on hls.js's own internal (non-epoch) timeline, not wall
      // clock, so it can't be shown directly (unlike DASH -- see playDash()).
      hlsInstance.on(Hls.Events.FRAG_CHANGED, (_evt: unknown, data: { frag?: { programDateTime?: number | null; start?: number } }) => {
        if (data.frag?.programDateTime != null) {
          hlsFragProgramDateTime = data.frag.programDateTime
          hlsFragStart = data.frag.start ?? video.currentTime
          onHlsTimeUpdate()
        }
      })
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari: native HLS support, no hls.js needed.
      hlsNativePlayback = true
      video.src = url
    } else {
      hlsError.value = 'HLS is not supported in this browser (and hls.js failed to initialize).'
      hlsLoading.value = false
      hlsPlaying.value = false
    }
  } catch (e) {
    if (generation !== hlsGeneration) return
    hlsError.value = e instanceof Error ? e.message : String(e)
    hlsLoading.value = false
    hlsPlaying.value = false
  }
}

async function playDash() {
  const url = props.dashUrl
  if (!url) return
  const generation = ++dashGeneration
  dashError.value = ''
  dashLoading.value = true
  dashPlaying.value = true
  try {
    await loadScript(DASH_CDN, 'dashjs')
    if (generation !== dashGeneration) return
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
    video.addEventListener('timeupdate', onDashTimeUpdate)
    video.addEventListener('play', syncDashVideoState)
    video.addEventListener('playing', syncDashVideoState)
    video.addEventListener('pause', syncDashVideoState)
    video.addEventListener('ended', syncDashVideoState)
    video.addEventListener('volumechange', syncDashVideoState)
    video.addEventListener('seeked', onDashSeeked)
    video.addEventListener('loadedmetadata', syncDashVideoState)
    document.addEventListener('fullscreenchange', onDashFullscreenChange)
    // Custom DVR seekbar (see the block above `dashSeekMin` for why the
    // native <video controls> scrubber can't be used here) -- `progress`
    // catches the DVR window sliding forward as new segments append even
    // between `timeupdate` ticks; the poll timer is a fallback for when
    // dash.js keeps topping up the live-edge buffer with no DOM event at
    // all (observed while paused).
    video.addEventListener('progress', updateDashDvrRange)
    dashDvrPollTimer = setInterval(updateDashDvrRange, 2000)
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
      (e: {
        event?: {
          id?: string
          eventStream?: { value?: string }
          messageData?: Uint8Array
          calculatedPresentationTime?: number
        }
      }) => {
        const rawId = e.event?.id
        const streamValue = e.event?.eventStream?.value
        const bytes = e.event?.messageData
        const presentationTime = e.event?.calculatedPresentationTime
        if (rawId == null || !bytes || presentationTime == null) return
        const id = `${rawId}:${streamValue ?? ''}`
        if (dashSeenEventKeys.has(id)) return

        if (!dashPendingBatch) {
          dashPendingBatch = new Map()
          queueMicrotask(() => {
            const batch = dashPendingBatch
            dashPendingBatch = null
            if (!batch) return
            for (const { bytes: groupBytes, presentationTime: groupPresentationTime, ids } of batch.values()) {
              for (const groupId of ids) dashSeenEventKeys.add(groupId)
              // dash.js's own internal position -- what
              // `calculatedPresentationTime` is actually compared against
              // -- can already be at the live edge before the DOM <video>
              // element's `currentTime` has caught up to it (a brief
              // startup race: dash.js starts dispatching its initial
              // catch-up burst before the element has been seeked/started
              // yet). Until `currentTime` is a real, populated value (same
              // readiness check as the playhead-time updater below), we
              // have nothing trustworthy to compare against, so default to
              // treating it as backlog rather than risk mislabeling a
              // whole startup burst as "just now" -- which is exactly what
              // was happening: currentTime near 0 minus a real epoch-scale
              // presentationTime is hugely negative, i.e. never "stale".
              if (!Number.isFinite(video.currentTime) || video.currentTime <= 0) continue
              // Backlog, not a fresh activation (see
              // MARKER_STALE_THRESHOLD_SECONDS) -- still marked seen
              // above, so it never re-announces later.
              if (video.currentTime - groupPresentationTime > MARKER_STALE_THRESHOLD_SECONDS) continue
              for (const marker of describeAllMarkers(groupBytes)) {
                pushMarkerToast(dashMarkerToasts, markerToastLabel(marker), marker.kind)
              }
            }
          })
        }
        const groupKey = bytesToHex(bytes)
        const group = dashPendingBatch.get(groupKey) ?? { bytes, presentationTime, ids: [] as string[] }
        group.ids.push(id)
        dashPendingBatch.set(groupKey, group)
      },
    )
  } catch (e) {
    if (generation !== dashGeneration) return
    const message = e instanceof Error ? e.message : String(e)
    destroyDash()
    dashError.value = message
  }
}

function destroyHls() {
  hlsGeneration++
  const video = hlsVideo.value
  if (video) {
    video.removeEventListener('timeupdate', onHlsTimeUpdate)
    video.removeEventListener('progress', updateHlsDvrRange)
    video.removeEventListener('play', syncHlsVideoState)
    video.removeEventListener('playing', syncHlsVideoState)
    video.removeEventListener('pause', syncHlsVideoState)
    video.removeEventListener('ended', syncHlsVideoState)
    video.removeEventListener('volumechange', syncHlsVideoState)
    video.removeEventListener('seeked', onHlsSeeked)
    video.removeEventListener('loadedmetadata', onHlsLoadedMetadata)
  }
  document.removeEventListener('fullscreenchange', onHlsFullscreenChange)
  if (hlsSeekCommitTimer != null) {
    clearTimeout(hlsSeekCommitTimer)
    hlsSeekCommitTimer = null
  }
  if (hlsInstance) {
    hlsInstance.destroy()
    hlsInstance = null
  }
  if (hlsVideo.value) {
    hlsVideo.value.removeAttribute('src')
    hlsVideo.value.load()
  }
  if (hlsDvrPollTimer != null) {
    clearInterval(hlsDvrPollTimer)
    hlsDvrPollTimer = null
  }
  hlsOriginalMaxLatency = undefined
  hlsNativePlayback = false
  hlsFragProgramDateTime = null
  hlsFragStart = 0
  hlsPlaying.value = false
  hlsIsPlaying.value = false
  hlsFullscreen.value = false
  hlsLoading.value = false
  hlsError.value = ''
  hlsSeenActivations.clear()
  hlsMarkerToasts.value = []
  hlsPlayheadTime.value = ''
  hlsSeekMin.value = 0
  hlsSeekMax.value = 0
  hlsSeekValue.value = 0
  hlsSeekDragging.value = false
  hlsSeekPending.value = false
  hlsAtLiveEdge.value = true
}

function destroyDash() {
  dashGeneration++
  const video = dashVideo.value
  if (video) {
    video.removeEventListener('timeupdate', onDashTimeUpdate)
    video.removeEventListener('play', syncDashVideoState)
    video.removeEventListener('playing', syncDashVideoState)
    video.removeEventListener('pause', syncDashVideoState)
    video.removeEventListener('ended', syncDashVideoState)
    video.removeEventListener('volumechange', syncDashVideoState)
    video.removeEventListener('seeked', onDashSeeked)
    video.removeEventListener('loadedmetadata', syncDashVideoState)
  }
  document.removeEventListener('fullscreenchange', onDashFullscreenChange)
  if (dashSeekCommitTimer != null) {
    clearTimeout(dashSeekCommitTimer)
    dashSeekCommitTimer = null
  }
  if (dashInstance) {
    dashInstance.reset()
    dashInstance = null
  }
  if (dashVideo.value) {
    dashVideo.value.removeAttribute('src')
    dashVideo.value.load()
  }
  if (dashDvrPollTimer != null) {
    clearInterval(dashDvrPollTimer)
    dashDvrPollTimer = null
  }
  dashPlaying.value = false
  dashIsPlaying.value = false
  dashFullscreen.value = false
  dashLoading.value = false
  dashError.value = ''
  dashSeenEventKeys.clear()
  dashPendingBatch = null
  dashMarkerToasts.value = []
  dashPlayheadTime.value = ''
  dashSeekMin.value = 0
  dashSeekMax.value = 0
  dashSeekValue.value = 0
  dashSeekDragging.value = false
  dashSeekPending.value = false
  dashAtLiveEdge.value = true
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

// Force both players to tear down and restart from the live edge. Used by
// the parent when the channel transitions into the "It's Alive!" phase
// without the playback URLs themselves changing (e.g. MediaLive recovering,
// or ECS tasks scaling back up) -- otherwise a player left attached across
// a stop/start cycle can keep showing the tail of a stale DVR window from
// the previous run instead of jumping to the new live content.
function reloadPlayers() {
  destroyHls()
  destroyDash()
  if (props.hlsUrl) playHls()
  if (props.dashUrl) playDash()
}

defineExpose({ reloadPlayers })

// Rough estimate of how many wrapped lines a URL needs so the readonly
// textarea below can sit tall enough to show it in full without
// scrolling/cropping, without needing to measure actual rendered text
// width (which varies with the panel's responsive column width). Errs on
// the generous side -- an extra blank-ish line is far less annoying than
// a cropped URL.
function urlRows(url?: string | null): number {
  if (!url) return 1
  return Math.max(1, Math.ceil(url.length / 40))
}

/** Fixed-width HH:MM:SS -- unlike a bare seconds count, its length never
 * changes as uptime ticks up, so the uptime pill doesn't visibly resize. */
function formatUptime(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
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
        <div class="stat-pill">
          <span class="stat-pill-label stat-pill-label-loop">Loop</span>
          <span class="stat-pill-value stat-pill-value-loop">#{{ health.loop_number }}</span>
        </div>
        <div class="stat-pill">
          <span class="stat-pill-label stat-pill-label-position">Position</span>
          <span class="stat-pill-value stat-pill-value-position">
            {{ health.position_in_loop_seconds.toFixed(1) }}s / {{ health.total_loop_duration_seconds.toFixed(1) }}s
          </span>
        </div>
        <div class="stat-pill">
          <span class="stat-pill-label stat-pill-label-uptime">Uptime</span>
          <span class="stat-pill-value stat-pill-value-uptime">{{ formatUptime(health.uptime_seconds) }}</span>
        </div>
        <div class="stat-pill">
          <span class="stat-pill-label stat-pill-label-playlist">Playlist</span>
          <span class="stat-pill-value stat-pill-value-playlist">{{ health.renditions.join(', ') }}</span>
        </div>
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
        <div v-if="hlsError" class="player-stack-error">
          <Message severity="error" :closable="false" class="text-xs">{{ hlsError }}</Message>
        </div>
        <div class="player-stack">
          <div class="hls-player-shell dash-player-shell">
            <div class="video-wrapper">
              <video ref="hlsVideo" muted playsinline class="player-video" />
              <div v-if="hlsLoading" class="player-overlay"><i class="pi pi-spin pi-spinner" /></div>
              <button v-if="!hlsPlaying" class="play-overlay" @click="playHls">
                <i class="pi pi-play-circle" />
                <span>Play HLS</span>
              </button>
              <TransitionGroup
                name="marker-toast"
                tag="div"
                class="marker-toast-stack"
                :style="{ '--marker-toast-life': `${MARKER_TOAST_LIFE_MS}ms` }"
              >
                <div v-for="t in hlsMarkerToasts" :key="t.key" :class="['marker-toast', `marker-toast-${t.kind}`]">
                  <i :class="MARKER_KIND_ICON[t.kind]" />
                  <span>{{ t.label }}</span>
                </div>
              </TransitionGroup>
            </div>
            <div v-if="hlsPlaying" class="dash-controls" aria-label="HLS playback controls">
            <button
              class="dash-control-btn"
              :aria-label="hlsIsPlaying ? 'Pause' : 'Play'"
              :title="hlsIsPlaying ? 'Pause' : 'Play'"
              @click="toggleHlsPlayback"
            >
              <i :class="hlsIsPlaying ? 'pi pi-pause' : 'pi pi-play'" />
            </button>
            <button
              class="dash-control-btn"
              :aria-label="hlsMuted || hlsVolume === 0 ? 'Unmute' : 'Mute'"
              :title="hlsMuted || hlsVolume === 0 ? 'Unmute' : 'Mute'"
              @click="toggleHlsMute"
            >
              <i :class="hlsMuted || hlsVolume === 0 ? 'pi pi-volume-off' : 'pi pi-volume-up'" />
            </button>
            <input
              type="range"
              class="dash-volume"
              min="0"
              max="1"
              step="0.05"
              :value="hlsMuted ? 0 : hlsVolume"
              aria-label="Volume"
              title="Volume"
              @input="onHlsVolumeInput(($event.target as HTMLInputElement).valueAsNumber)"
            />
            <div v-if="hlsSeekMax > hlsSeekMin" class="dvr-seekbar">
              <span class="dvr-label" :class="{ 'dvr-label-live': hlsAtLiveEdge && !hlsSeekDragging }">
                {{ hlsBehindLiveLabel(hlsSeekValue) }}
              </span>
              <input
                type="range"
                class="dvr-range"
                :min="hlsSeekMin"
                :max="hlsSeekSliderMax"
                step="0.1"
                :value="hlsSeekValue"
                aria-label="DVR position, seconds behind live"
                :aria-valuetext="hlsBehindLiveLabel(hlsSeekValue)"
                title="Scrub within the DVR window"
                @pointerdown="onHlsSeekStart"
                @pointercancel="onHlsSeekPointerCancel"
                @keydown="onHlsSeekKeyDown"
                @input="onHlsSeekInput(($event.target as HTMLInputElement).valueAsNumber)"
                @change="onHlsSeekCommit(($event.target as HTMLInputElement).valueAsNumber)"
              />
              <button
                class="dvr-live-btn"
                :class="{ 'dvr-live-btn-active': hlsAtLiveEdge && !hlsSeekDragging }"
                title="Return to live"
                @click="goHlsLive"
              >
                <i class="pi pi-circle-fill" /> LIVE
              </button>
            </div>
            <span v-else class="dvr-label dvr-label-waiting">Waiting for DVR window…</span>
            <button
              class="dash-control-btn"
              :aria-label="hlsFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'"
              :title="hlsFullscreen ? 'Exit fullscreen' : 'Fullscreen'"
              @click="toggleHlsFullscreen"
            >
              <i :class="hlsFullscreen ? 'pi pi-compress' : 'pi pi-expand'" />
            </button>
            </div>
          </div>
          <div v-if="hlsPlayheadTime || hlsDvrWindowSeconds > 0" class="playback-meta-panel stacked-player-panel">
          <div v-if="hlsDvrWindowSeconds > 0" class="playhead-chip dvr-window-chip">
            <i class="pi pi-history" />
            <span class="playback-meta-label">DVR window</span>
            <span class="playback-meta-value">{{ formatDvrWindow(hlsDvrWindowSeconds) }}</span>
          </div>
          <div v-if="hlsPlayheadTime" class="playhead-chip playhead-chip-right">
            <i class="pi pi-clock" />
            <span class="playback-meta-label">Playhead</span>
            <time class="playback-meta-value">{{ hlsPlayheadTime }}</time>
          </div>
          </div>
          <div class="url-row stacked-player-panel url-panel">
          <textarea
            class="url-input"
            readonly
            :rows="urlRows(hlsUrl)"
            :value="hlsUrl"
            @focus="($event.target as HTMLTextAreaElement).select()"
          />
          <Button icon="pi pi-copy" text size="small" title="Copy URL" @click="copyUrl(hlsUrl)" />
          </div>
        </div>
      </div>

      <div v-if="dashUrl" class="player-card">
        <div class="player-card-header">
          <span class="font-semibold text-sm">DASH</span>
          <Button v-if="dashPlaying" icon="pi pi-stop-circle" text size="small" severity="secondary" label="Stop" @click="destroyDash" />
        </div>
        <div v-if="dashError" class="player-stack-error">
          <Message severity="error" :closable="false" class="text-xs">{{ dashError }}</Message>
        </div>
        <div class="player-stack">
          <div class="dash-player-shell">
            <div class="video-wrapper">
              <video ref="dashVideo" muted playsinline class="player-video" />
              <div v-if="dashLoading" class="player-overlay"><i class="pi pi-spin pi-spinner" /></div>
              <button v-if="!dashPlaying" class="play-overlay" @click="playDash">
                <i class="pi pi-play-circle" />
                <span>Play DASH</span>
              </button>
              <TransitionGroup
                name="marker-toast"
                tag="div"
                class="marker-toast-stack"
                :style="{ '--marker-toast-life': `${MARKER_TOAST_LIFE_MS}ms` }"
              >
                <div v-for="t in dashMarkerToasts" :key="t.key" :class="['marker-toast', `marker-toast-${t.kind}`]">
                  <i :class="MARKER_KIND_ICON[t.kind]" />
                  <span>{{ t.label }}</span>
                </div>
              </TransitionGroup>
            </div>
            <div v-if="dashPlaying" class="dash-controls" aria-label="DASH playback controls">
            <button
              class="dash-control-btn"
              :aria-label="dashIsPlaying ? 'Pause' : 'Play'"
              :title="dashIsPlaying ? 'Pause' : 'Play'"
              @click="toggleDashPlayback"
            >
              <i :class="dashIsPlaying ? 'pi pi-pause' : 'pi pi-play'" />
            </button>
            <button
              class="dash-control-btn"
              :aria-label="dashMuted || dashVolume === 0 ? 'Unmute' : 'Mute'"
              :title="dashMuted || dashVolume === 0 ? 'Unmute' : 'Mute'"
              @click="toggleDashMute"
            >
              <i :class="dashMuted || dashVolume === 0 ? 'pi pi-volume-off' : 'pi pi-volume-up'" />
            </button>
            <input
              type="range"
              class="dash-volume"
              min="0"
              max="1"
              step="0.05"
              :value="dashMuted ? 0 : dashVolume"
              aria-label="Volume"
              title="Volume"
              @input="onDashVolumeInput(($event.target as HTMLInputElement).valueAsNumber)"
            />
            <div v-if="dashSeekMax > dashSeekMin" class="dvr-seekbar">
              <span class="dvr-label" :class="{ 'dvr-label-live': dashAtLiveEdge && !dashSeekDragging }">
                {{ dashBehindLiveLabel(dashSeekValue) }}
              </span>
              <input
                type="range"
                class="dvr-range"
                :min="dashSeekMin"
                :max="dashSeekSliderMax"
                step="0.1"
                :value="dashSeekValue"
                aria-label="DVR position, seconds behind live"
                :aria-valuetext="dashBehindLiveLabel(dashSeekValue)"
                title="Scrub within the DVR window"
                @pointerdown="onDashSeekStart"
                @pointercancel="onDashSeekPointerCancel"
                @keydown="onDashSeekKeyDown"
                @input="onDashSeekInput(($event.target as HTMLInputElement).valueAsNumber)"
                @change="onDashSeekCommit(($event.target as HTMLInputElement).valueAsNumber)"
              />
              <button
                class="dvr-live-btn"
                :class="{ 'dvr-live-btn-active': dashAtLiveEdge && !dashSeekDragging }"
                title="Return to live"
                @click="goDashLive"
              >
                <i class="pi pi-circle-fill" /> LIVE
              </button>
            </div>
            <span v-else class="dvr-label dvr-label-waiting">Waiting for DVR window…</span>
            <button
              class="dash-control-btn"
              :aria-label="dashFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'"
              :title="dashFullscreen ? 'Exit fullscreen' : 'Fullscreen'"
              @click="toggleDashFullscreen"
            >
              <i :class="dashFullscreen ? 'pi pi-compress' : 'pi pi-expand'" />
            </button>
            </div>
          </div>
          <div v-if="dashPlayheadTime || dashDvrWindowSeconds > 0" class="playback-meta-panel stacked-player-panel">
          <div v-if="dashDvrWindowSeconds > 0" class="playhead-chip dvr-window-chip">
            <i class="pi pi-history" />
            <span class="playback-meta-label">DVR window</span>
            <span class="playback-meta-value">{{ formatDvrWindow(dashDvrWindowSeconds) }}</span>
          </div>
          <div v-if="dashPlayheadTime" class="playhead-chip playhead-chip-right">
            <i class="pi pi-clock" />
            <span class="playback-meta-label">Playhead</span>
            <time class="playback-meta-value">{{ dashPlayheadTime }}</time>
          </div>
          </div>
          <div class="url-row stacked-player-panel url-panel">
          <textarea
            class="url-input"
            readonly
            :rows="urlRows(dashUrl)"
            :value="dashUrl"
            @focus="($event.target as HTMLTextAreaElement).select()"
          />
          <Button icon="pi pi-copy" text size="small" title="Copy URL" @click="copyUrl(dashUrl)" />
          </div>
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

/* One pill per stat, sized to fit its own worst case so it doesn't visibly
 * resize as the numbers inside tick over (loop count climbing, position
 * cycling each loop) -- tabular-nums keeps digit widths uniform, and each
 * value gets a min-width generous enough that a longer number doesn't push
 * the pill wider mid-poll. */
.stat-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  background: #f1f5f9;
  border-radius: 999px;
  padding: 0.3rem 0.75rem;
  font-size: 0.75rem;
  white-space: nowrap;
}

.stat-pill-label {
  font-weight: 700;
  text-transform: uppercase;
  font-size: 0.62rem;
  letter-spacing: 0.03em;
}

.stat-pill-label-loop {
  color: #3b6ea5;
}

.stat-pill-label-position {
  color: #3f8f7f;
}

.stat-pill-label-uptime {
  color: #b07a2e;
}

.stat-pill-label-playlist {
  color: #8467a8;
}

.stat-pill-value {
  color: #0f172a;
  font-weight: 700;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.stat-pill-value-loop {
  display: inline-block;
  min-width: 5.5rem;
}

.stat-pill-value-position {
  display: inline-block;
  min-width: 7.5rem;
}

.stat-pill-value-uptime {
  display: inline-block;
  min-width: 4rem;
}

.stat-pill-value-playlist {
  display: inline-block;
  min-width: 8rem;
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
  /* Gradual, ACCELERATING fade-out across the toast's whole lifetime
   * (ease-in: slow at first, rapid near the end) -- not just a quick fade
   * at removal. `--marker-toast-life` (set on .marker-toast-stack, see
   * MARKER_TOAST_LIFE_MS) is the single source of truth for how long the
   * toast stays mounted, so this animation's duration always matches the
   * JS setTimeout that actually removes it. Held off for the first 2s
   * (longer than the enter transition's own 0.2s below needs, so that
   * quick fade-IN isn't fought by this fade-OUT animation starting at the
   * same instant -- a CSS animation on a property always wins over a
   * transition on that same property -- and so the toast reads clearly
   * before it starts dimming at all). */
  animation: marker-toast-fade calc(var(--marker-toast-life, 4s) - 2s) ease-in 2s forwards;
}

@keyframes marker-toast-fade {
  from {
    opacity: 1;
  }
  to {
    opacity: 0;
  }
}

.marker-toast i {
  color: #60a5fa;
  font-size: 0.8rem;
}

/* Start/End/Other get their own border + icon color on top of the shared
 * .marker-toast shape above, so a run of toasts reads at a glance --
 * green/sign-out = Start, amber/sign-in = End (deliberately NOT red --
 * an End marker is expected, routine signaling, not an error/alert), the
 * original blue/bell = Other (no Start/End pairing: instant signals,
 * bare splice commands, ...). */
.marker-toast-start {
  border-color: rgba(74, 222, 128, 0.55);
}

.marker-toast-start i {
  color: #4ade80;
}

.marker-toast-end {
  border-color: rgba(251, 191, 36, 0.55);
}

.marker-toast-end i {
  color: #fbbf24;
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

.dvr-seekbar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex: 1;
  min-width: 0;
}

.dash-player-shell {
  position: relative;
  z-index: 3;
  width: 100%;
  background: #000;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 8px;
  overflow: hidden;
  box-sizing: border-box;
  box-shadow: 0 3px 6px rgba(0, 0, 0, 0.25);
}

.player-stack {
  display: flex;
  flex-direction: column;
  gap: 0;
  width: 100%;
  overflow: visible;
}

.player-stack-error {
  margin-bottom: 0.5rem;
}

.dash-player-shell:fullscreen {
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 1rem;
  background: #020617;
  border: 0;
  border-radius: 0;
}

.dash-player-shell:fullscreen .video-wrapper {
  flex: 1;
  min-height: 0;
  aspect-ratio: auto;
}

.dash-player-shell:fullscreen .player-video {
  object-fit: contain;
}

.dash-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-height: 42px;
  padding: 0.35rem 0.6rem;
  background: #0b1220;
  color: #e2e8f0;
}

.dash-control-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  color: #e2e8f0;
  background: transparent;
  border: 0;
  border-radius: 5px;
  cursor: pointer;
}

.dash-control-btn:hover,
.dash-control-btn:focus-visible {
  color: #fff;
  background: rgba(255, 255, 255, 0.12);
  outline: none;
}

.dash-volume {
  flex: none;
  width: 72px;
  accent-color: #60a5fa;
}

.dvr-label-waiting {
  flex: 1;
  text-align: left;
  color: #94a3b8;
}

.dvr-label {
  flex: none;
  min-width: 3.4rem;
  text-align: right;
  color: #fbbf24;
  font-size: 0.75rem;
  font-family: var(--font-mono, monospace);
  font-variant-numeric: tabular-nums;
}

.dvr-label-live {
  color: #4ade80;
}

/* Custom track/thumb (not the browser default) so this reads as a
 * distinct, purpose-built DVR control -- not a second, confusingly
 * redundant copy of the native <video controls> scrubber above it. */
.dvr-range {
  flex: 1;
  min-width: 0;
  appearance: none;
  height: 4px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.15);
  outline: none;
  cursor: pointer;
}

.dvr-range::-webkit-slider-thumb {
  appearance: none;
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: #60a5fa;
  border: 2px solid #0f172a;
  cursor: pointer;
}

.dvr-range::-moz-range-thumb {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: #60a5fa;
  border: 2px solid #0f172a;
  cursor: pointer;
}

.dvr-range:focus-visible,
.dash-volume:focus-visible {
  outline: 2px solid #93c5fd;
  outline-offset: 4px;
}

.dvr-live-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 999px;
  padding: 0.2rem 0.6rem;
  color: #cbd5e1;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  cursor: pointer;
}

.dvr-live-btn i {
  font-size: 0.5rem;
  color: #94a3b8;
}

.dvr-live-btn-active {
  color: #4ade80;
  border-color: rgba(74, 222, 128, 0.4);
}

.dvr-live-btn-active i {
  color: #ef4444;
  animation: pulse 1.6s ease-in-out infinite;
}

@media (max-width: 520px) {
  .dash-controls {
    flex-wrap: wrap;
  }

  .dvr-seekbar {
    flex-basis: 100%;
    order: 1;
  }

  .dvr-label-waiting {
    flex: 1;
  }
}

.playback-meta-panel {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  min-height: 52px;
  width: 100%;
  padding: calc(0.3rem + 6px) 0.8rem 0.3rem;
  box-sizing: border-box;
  background: #17243a;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-top: 0;
  border-radius: 0 0 8px 8px;
  color: #cbd5e1;
  box-shadow: 0 3px 6px rgba(0, 0, 0, 0.18);
}

.playhead-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  color: #94a3b8;
  font-size: 0.7rem;
}

.playhead-chip > i {
  color: #60a5fa;
  font-size: 0.75rem;
}

.playback-meta-label {
  color: #94a3b8;
}

.playback-meta-value {
  color: #e2e8f0;
  font-family: var(--font-mono, monospace);
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
}

.dvr-window-chip > i {
  color: #4ade80;
}

.playhead-chip-right {
  margin-left: auto;
}

@media (max-width: 520px) {
  .playback-meta-panel {
    gap: 0.5rem;
    padding: calc(0.3rem + 6px) 0.6rem 0.3rem;
  }

  .playhead-chip {
    gap: 0.3rem;
  }

  .playback-meta-value {
    font-size: 0.66rem;
  }
}

.url-row {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  min-width: 0;
  background: #263750;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-top: 0;
  border-radius: 0 0 8px 8px;
  padding: calc(0.25rem + 6px) 0.3rem 0.25rem 0.75rem;
  box-sizing: border-box;
}

/* Each lower panel begins behind the rounded bottom edge above it. Their
 * light side/bottom borders follow the shared width; no top border shows. */
.stacked-player-panel {
  position: relative;
  width: 100%;
  margin-top: -6px;
}

.playback-meta-panel.stacked-player-panel {
  z-index: 2;
}

.url-panel {
  width: calc(100% - 2px);
  margin-left: auto;
  margin-right: auto;
  background: #263750;
  z-index: 1;
}

.url-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  overflow: hidden;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.3;
  color: #e2e8f0;
  font-size: 0.7rem;
  font-family: var(--font-mono, monospace);
}
</style>
