<script setup lang="ts">
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import { computed, ref } from 'vue'
import { browseFiles, probeMedia } from '../api/client'
import type { FileEntry, ProbeResult } from '../api/types'

const model = defineModel<string>({ required: true })
const props = defineProps<{ placeholder?: string }>()

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
  <div class="flex flex-column gap-1">
    <div class="flex gap-1">
      <InputText v-model="model" class="flex-1" :placeholder="props.placeholder ?? '/path/to/file.mp4 or https://...'" />
      <Button icon="pi pi-folder-open" severity="secondary" outlined title="Browse local files" @click="openBrowse" />
      <Button icon="pi pi-search" severity="secondary" outlined title="Probe (resolution/duration)" :loading="probing" @click="probe" />
    </div>

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
  </div>
</template>
