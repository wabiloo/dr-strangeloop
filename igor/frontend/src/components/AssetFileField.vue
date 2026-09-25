<script setup lang="ts">
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import { computed, ref } from 'vue'
import { browseFiles, localFilePreviewUrl, probeMedia } from '../api/client'
import type { FileEntry, ProbeResult } from '../api/types'
import { filesFromDrop, isDragInside, isFileDrag, sourceFromDroppedFile } from '../utils/fileDrop'

const model = defineModel<string>({ required: true })
const props = defineProps<{ placeholder?: string }>()

const dropActive = ref(false)
const dropBusy = ref(false)
const dropError = ref('')

function handleDragOver(event: DragEvent) {
  if (!isFileDrag(event)) return
  event.preventDefault()
  dropActive.value = true
}

function handleDragLeave(event: DragEvent) {
  if (isDragInside(event)) return
  dropActive.value = false
}

async function handleDrop(event: DragEvent) {
  if (!isFileDrag(event)) return
  event.preventDefault()
  dropActive.value = false
  const file = filesFromDrop(event)[0]
  if (!file) return

  dropBusy.value = true
  dropError.value = ''
  try {
    model.value = await sourceFromDroppedFile(file)
  } catch (e) {
    dropError.value = e instanceof Error ? e.message : String(e)
  } finally {
    dropBusy.value = false
  }
}

// --- Browse dialog --------------------------------------------------------
const browseOpen = ref(false)
const browsePath = ref<string | null>(null)
const browseParent = ref<string | null>(null)
const browseEntries = ref<FileEntry[]>([])
const browseError = ref('')
const browseLoading = ref(false)

async function loadDir(path?: string) {
  browseLoading.value = true
  browseError.value = ''
  try {
    const result = await browseFiles(path)
    browsePath.value = result.path
    browseParent.value = result.parent
    browseEntries.value = result.entries
  } catch (e) {
    browseError.value = e instanceof Error ? e.message : String(e)
  } finally {
    browseLoading.value = false
  }
}

function openBrowse() {
  browseOpen.value = true
  // Start from the current value's directory if it looks like a local path.
  const start = model.value && !model.value.startsWith('http') ? model.value : undefined
  loadDir(start)
}

function selectEntry(entry: FileEntry) {
  if (entry.is_dir) {
    loadDir(entry.path)
  } else {
    model.value = entry.path
    browseOpen.value = false
  }
}

// --- Probe -----------------------------------------------------------------
const probing = ref(false)
const probeResult = ref<ProbeResult | null>(null)
const probeError = ref('')

// This field is also used for slate images. Only offer the video player for
// paths/URLs that look like video assets.
const VIDEO_EXTENSIONS = new Set(['.mp4', '.m4v', '.mov', '.mkv', '.ts', '.avi', '.webm', '.m3u8', '.mpd'])
const canPreview = computed(() => {
  const source = model.value.trim()
  if (!source) return false
  let pathname = source
  try {
    pathname = new URL(source).pathname
  } catch {
    // A local filesystem path, rather than an absolute URL.
  }
  const extension = pathname.slice(pathname.lastIndexOf('.')).toLowerCase()
  return VIDEO_EXTENSIONS.has(extension)
})

const previewOpen = ref(false)
const previewSource = ref('')
const previewError = ref(false)

function openPreview() {
  const source = model.value.trim()
  if (!source || !canPreview.value) return
  previewError.value = false
  previewSource.value = /^https?:\/\//i.test(source) ? source : localFilePreviewUrl(source)
  previewOpen.value = true
}

async function probe() {
  if (!model.value.trim()) return
  probing.value = true
  probeError.value = ''
  probeResult.value = null
  try {
    probeResult.value = await probeMedia(model.value.trim())
  } catch (e) {
    probeError.value = e instanceof Error ? e.message : String(e)
  } finally {
    probing.value = false
  }
}

const probeSummary = computed(() => {
  if (!probeResult.value) return ''
  const r = probeResult.value
  const parts: string[] = []
  if (r.width && r.height) parts.push(`${r.width}x${r.height}`)
  if (r.frame_rate) parts.push(`${r.frame_rate}fps`)
  if (r.video_codec) parts.push(r.video_codec)
  if (r.duration_seconds !== null) parts.push(formatDuration(r.duration_seconds))
  if (r.has_audio) parts.push(`audio: ${r.audio_codec ?? 'yes'}`)
  return parts.join(' -- ')
})

function formatDuration(seconds: number): string {
  const s = Math.round(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  if (m > 0) return `${m}:${String(sec).padStart(2, '0')}`
  return `${sec}s`
}
</script>

<template>
  <div
    class="asset-file-field-drop-zone flex flex-column gap-1"
    :class="{ 'asset-file-field-drop-zone-active': dropActive }"
    @dragover="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <div class="flex gap-1">
      <InputText v-model="model" class="flex-1" :placeholder="props.placeholder ?? '/path/to/file.mp4 or https://...'" />
      <Button icon="pi pi-folder-open" severity="secondary" outlined title="Browse local files" @click="openBrowse" />
      <Button v-if="canPreview" icon="pi pi-play-circle" severity="secondary" outlined title="Preview video" aria-label="Preview video" @click="openPreview" />
      <Button icon="pi pi-search" severity="secondary" outlined title="Probe (resolution/duration)" :loading="probing" @click="probe" />
    </div>

    <div v-if="dropBusy" class="text-sm text-color-secondary flex align-items-center gap-1">
      <i class="pi pi-spin pi-spinner" /> Uploading dropped file...
    </div>
    <Message v-if="dropError" severity="error" class="text-sm">{{ dropError }}</Message>
    <Message v-if="probeError" severity="error" class="text-sm">{{ probeError }}</Message>
    <div v-if="probeSummary" class="text-sm text-color-secondary flex align-items-center gap-1">
      <i class="pi pi-info-circle" />
      <span>{{ probeSummary }}</span>
    </div>

    <Dialog v-model:visible="browseOpen" modal header="Browse files" style="width: 40rem">
      <div class="flex flex-column gap-2">
        <div class="flex align-items-center gap-2">
          <Button icon="pi pi-arrow-up" text :disabled="!browseParent" @click="loadDir(browseParent ?? undefined)" />
          <span class="text-sm text-color-secondary" style="word-break: break-all">{{ browsePath }}</span>
        </div>

        <Message v-if="browseError" severity="error">{{ browseError }}</Message>

        <div v-if="browseLoading" class="flex justify-content-center p-4">
          <ProgressSpinner style="width: 2rem; height: 2rem" stroke-width="6" />
        </div>

        <ul v-else class="list-none m-0 p-0 flex flex-column" style="max-height: 22rem; overflow-y: auto">
          <li
            v-for="entry in browseEntries"
            :key="entry.path"
            class="flex align-items-center gap-2 p-2 border-round cursor-pointer hover:surface-100"
            @click="selectEntry(entry)"
          >
            <i :class="entry.is_dir ? 'pi pi-folder' : entry.is_video ? 'pi pi-video' : 'pi pi-file'" />
            <span>{{ entry.name }}</span>
          </li>
          <li v-if="!browseEntries.length" class="text-color-secondary p-2">(empty directory)</li>
        </ul>
      </div>
    </Dialog>

    <Dialog v-model:visible="previewOpen" modal header="Video preview" :style="{ width: '64rem', maxWidth: '95vw' }">
      <video
        v-if="previewOpen"
        :src="previewSource"
        controls
        autoplay
        playsinline
        class="video-preview"
        @error="previewError = true"
      />
      <Message v-if="previewError" severity="error" :closable="false" class="mt-2">
        This video could not be played. Check that the file exists and uses a browser-supported format.
      </Message>
    </Dialog>
  </div>
</template>

<style scoped>
.asset-file-field-drop-zone {
  border: 2px dashed transparent;
  border-radius: 6px;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.asset-file-field-drop-zone-active {
  border-color: var(--p-primary-color, #b91c1c);
  background: var(--p-primary-50, #ecfeff);
}

.video-preview {
  display: block;
  width: 100%;
  max-height: 70vh;
  background: #000;
}
</style>
