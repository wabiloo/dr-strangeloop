<script setup lang="ts">
import Tag from 'primevue/tag'
import Message from 'primevue/message'
import Button from 'primevue/button'
import { onBeforeUnmount, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import type { ChannelHealth } from '../api/types'

const props = defineProps<{
  hlsUrl?: string | null
  dashUrl?: string | null
  health?: ChannelHealth | null
  healthError?: string
}>()

const toast = useToast()

const HLS_CDN = 'https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js'
const DASH_CDN = 'https://cdn.dashjs.org/latest/dash.all.min.js'

const hlsVideo = ref<HTMLVideoElement | null>(null)
const dashVideo = ref<HTMLVideoElement | null>(null)
const hlsError = ref('')
const dashError = ref('')
const hlsLoading = ref(false)
const dashLoading = ref(false)
const hlsPlaying = ref(false)
const dashPlaying = ref(false)

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let hlsInstance: any = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let dashInstance: any = null

const scriptPromises: Record<string, Promise<void> | undefined> = {}

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
      hlsInstance = new Hls({ liveSyncDurationCount: 3 })
      hlsInstance.loadSource(url)
      hlsInstance.attachMedia(video)
      hlsInstance.on(Hls.Events.ERROR, (_evt: unknown, data: { fatal?: boolean; details?: string }) => {
        if (data?.fatal) hlsError.value = `hls.js fatal error: ${data.details ?? 'unknown'}`
      })
      hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
        hlsLoading.value = false
        video.play().catch(() => {})
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
    dashInstance.initialize(video, url, true)
    dashInstance.on(dashjs.MediaPlayer.events.ERROR, (e: { error?: { message?: string } }) => {
      dashError.value = `dash.js error: ${e?.error?.message ?? 'unknown'}`
    })
    dashInstance.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, () => {
      dashLoading.value = false
    })
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
}

// If the channel gets redeployed/refreshed with new URLs, stop rather than
// silently keep playing a stale stream.
watch(() => props.hlsUrl, destroyHls)
watch(() => props.dashUrl, destroyDash)
onBeforeUnmount(() => {
  destroyHls()
  destroyDash()
})

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
        </div>
        <Message v-if="hlsError" severity="error" :closable="false" class="text-xs">{{ hlsError }}</Message>
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
        </div>
        <Message v-if="dashError" severity="error" :closable="false" class="text-xs">{{ dashError }}</Message>
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
