<script setup lang="ts">
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

// Plays a source manifest in hls.js (HLS) or dash.js (DASH), straight from the
// browser. The format comes from the caller (an inspection) when known, else
// from the URL's extension.
const props = defineProps<{ url: string | null; format?: 'hls' | 'dash' | null }>()
const emit = defineEmits<{ close: [] }>()

const videoEl = ref<HTMLVideoElement | null>(null)
const error = ref(false)
let player: { destroy?: () => void; reset?: () => void } | null = null

const open = computed(() => props.url !== null)
const kind = computed<'hls' | 'dash'>(() => {
  if (props.format) return props.format
  let path = props.url ?? ''
  try {
    path = new URL(path).pathname
  } catch {
    // keep the raw string
  }
  return path.toLowerCase().endsWith('.mpd') ? 'dash' : 'hls'
})

function destroyPlayer() {
  if (!player) return
  if (player.destroy) player.destroy()
  else player.reset?.()
  player = null
}

async function attach() {
  error.value = false
  await nextTick()
  const video = videoEl.value
  const url = props.url
  if (!video || !url) return
  try {
    if (kind.value === 'hls') {
      const { default: Hls } = await import('hls.js')
      if (props.url !== url) return
      if (Hls.isSupported()) {
        const hls = new Hls()
        player = hls
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (data.fatal) error.value = true
        })
        hls.loadSource(url)
        hls.attachMedia(video)
        void video.play().catch(() => {})
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url
        void video.play().catch(() => {})
      } else {
        error.value = true
      }
    } else {
      const dashjs = await import('dashjs')
      if (props.url !== url) return
      const dash = dashjs.MediaPlayer().create()
      player = dash
      dash.on('error', () => {
        error.value = true
      })
      dash.initialize(video, url, true)
    }
  } catch {
    error.value = true
  }
}

watch(open, (isOpen) => {
  if (isOpen) void attach()
  else destroyPlayer()
})
onBeforeUnmount(destroyPlayer)
</script>

<template>
  <Dialog
    :visible="open"
    modal
    :style="{ width: '64rem', maxWidth: '95vw' }"
    @update:visible="(v) => { if (!v) emit('close') }"
  >
    <template #header>
      <div class="flex align-items-center gap-2">
        <span class="font-semibold">Manifest preview</span>
        <Tag :value="kind === 'hls' ? 'HLS · hls.js' : 'DASH · dash.js'" severity="secondary" />
      </div>
    </template>
    <div class="flex flex-column gap-2">
      <code class="text-xs text-color-secondary preview-url">{{ url }}</code>
      <video v-if="open" ref="videoEl" controls autoplay playsinline class="preview-video" />
      <Message v-if="error" severity="error" :closable="false">
        This manifest could not be played. Check that it is reachable from your browser (the server must allow
        cross-origin requests, CORS) and that it uses a supported format.
      </Message>
    </div>
  </Dialog>
</template>

<style scoped>
.preview-url {
  word-break: break-all;
}
.preview-video {
  width: 100%;
  max-height: 70vh;
  background: #000;
}
</style>
