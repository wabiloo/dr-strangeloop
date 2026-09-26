<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import ConfirmPopup from 'primevue/confirmpopup'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { deleteArchive, listArchives, uploadArchive } from '../api/client'
import type { ArchiveListItem } from '../api/types'
import { alignConfirmPopup } from '../utils/confirmPopup'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const archives = ref<ArchiveListItem[]>([])
const loading = ref(true)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null)
const uploading = ref(false)
const isDraggingFile = ref(false)
function fileExtensionSupported(file: File): boolean {
  return /\.(har|proxymanlogv2|log|barc|zip)$/i.test(file.name)
}

function setSelectedFile(file: File | null) {
  selectedFile.value = file
}

function chooseFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  if (file && !fileExtensionSupported(file)) {
    error.value = 'Choose a HAR, Proxyman log, BARC, or ZIP archive.'
    setSelectedFile(null)
    return
  }
  error.value = ''
  setSelectedFile(file)
}

function onDragOver(event: DragEvent) {
  event.preventDefault()
  isDraggingFile.value = true
}

function onDragLeave(event: DragEvent) {
  if (!event.currentTarget || !(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) {
    isDraggingFile.value = false
  }
}

function onDrop(event: DragEvent) {
  event.preventDefault()
  isDraggingFile.value = false
  const file = event.dataTransfer?.files[0] ?? null
  if (!file) return
  if (!fileExtensionSupported(file)) {
    error.value = 'Choose a HAR, Proxyman log, BARC, or ZIP archive.'
    setSelectedFile(null)
    return
  }
  error.value = ''
  setSelectedFile(file)
}

async function upload() {
  if (!selectedFile.value) return
  uploading.value = true
  error.value = ''
  try {
    const archive = await uploadArchive(selectedFile.value)
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    await load()
    await router.push(`/archives/${archive.name}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    uploading.value = false
  }
}

function confirmDelete(event: MouseEvent, archive: ArchiveListItem) {
  const target = (event.target as HTMLElement).closest('button') ?? (event.target as HTMLElement)
  confirm.require({
    target,
    message: `Delete archive "${archive.name}"? This removes the original capture and saved selection. Existing imported output used by channels is kept.`,
    accept: async () => {
      try {
        await deleteArchive(archive.name)
        await load()
        toast.add({ severity: 'success', summary: 'Archive deleted', detail: archive.name, life: 4000 })
      } catch (e) {
        toast.add({ severity: 'error', summary: 'Delete failed', detail: e instanceof Error ? e.message : String(e), life: 6000 })
      }
    },
  })
  alignConfirmPopup(target)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    archives.value = await listArchives()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return ''
  const minutes = Math.floor(seconds / 60)
  const secs = Math.round(seconds % 60)
  return minutes > 0 ? `${minutes}m ${secs}s` : `${secs}s`
}

onMounted(load)
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="flex justify-content-between align-items-center">
      <h2 class="m-0">Archives (grave-robber import)</h2>
      <div class="flex gap-2">
        <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined @click="load" :loading="loading" />
      </div>
    </div>

    <Message severity="info" :closable="false">
      Upload a captured HAR (Chrome DevTools/Fiddler) or Proxyman log below, or copy it into
      <code>data/archives/</code>. Review its variants and import a loop timeline from it -- an
      alternative to authoring a franken-ts playlist, for re-serving an already-observed real
      HLS/DASH session.
    </Message>

    <form
      class="archive-dropzone"
      :class="{ 'archive-dropzone-active': isDraggingFile }"
      @submit.prevent="upload"
      @dragover="onDragOver"
      @dragleave="onDragLeave"
      @drop="onDrop"
    >
      <input
        id="archive-file"
        ref="fileInput"
        class="archive-file-input"
        type="file"
        accept=".har,.proxymanlogv2,.log,.barc,.zip"
        @change="chooseFile"
      />
      <i class="pi pi-cloud-upload archive-dropzone-icon" aria-hidden="true" />
      <div class="flex flex-column gap-1">
        <strong>{{ selectedFile ? selectedFile.name : 'Drop an archive file here' }}</strong>
        <span class="text-sm text-color-secondary">or <label for="archive-file" class="archive-browse-link">browse files</label></span>
      </div>
      <Button
        class="archive-upload-button"
        label="Upload archive"
        icon="pi pi-upload"
        type="submit"
        :disabled="!selectedFile"
        :loading="uploading"
      />
    </form>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <DataTable
      :value="archives"
      :loading="loading"
      data-key="name"
      row-hover
      class="cursor-pointer"
      @row-click="({ data }) => router.push(`/archives/${data.name}`)"
    >
      <Column header="Name">
        <template #body="{ data }">{{ data.display_name || data.name }}</template>
      </Column>
      <Column header="Format">
        <template #body="{ data }">
          <span class="uppercase">{{ data.format }}</span>
        </template>
      </Column>
      <Column header="Session length">
        <template #body="{ data }">{{ formatDuration(data.session_duration_seconds) }}</template>
      </Column>
      <Column field="variant_count" header="Variants" />
      <Column field="marker_count">
        <template #header>
          <span title="Approximate marker count, based on the latest snapshot of the first detected variant.">Markers</span>
        </template>
      </Column>
      <Column header="Import">
        <template #body="{ data }">
          <Tag v-if="!data.import" severity="secondary" value="Not imported" />
          <Tag v-else-if="data.import.stale" severity="warn" value="Stale" />
          <Tag v-else severity="success" value="Up to date" />
        </template>
      </Column>
      <Column header="Actions">
        <template #body="{ data }">
          <div class="flex gap-2" @click.stop>
            <Button
              :label="data.import ? 'Re-run import' : 'Review + import'"
              icon="pi pi-sitemap"
              size="small"
              @click="router.push(`/archives/${data.name}`)"
            />
            <Button
              icon="pi pi-trash"
              severity="danger"
              text
              rounded
              title="Delete archive"
              @click="confirmDelete($event, data)"
            />
          </div>
        </template>
      </Column>
      <template #empty>
        No archives found in <code>data/archives/</code>. Upload a capture above or copy one there and click Refresh.
      </template>
    </DataTable>
    <ConfirmPopup />
  </div>
</template>

<style scoped>
.archive-dropzone {
  min-height: 8rem;
  padding: 1rem 1.25rem;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 1rem;
  border: 2px dashed var(--p-surface-300);
  border-radius: var(--p-border-radius);
  background: var(--p-surface-0);
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.archive-dropzone-active {
  border-color: var(--p-primary-color);
  background: var(--p-primary-50);
}

.archive-dropzone-icon {
  color: var(--p-primary-color);
  font-size: 1.5rem;
}

.archive-upload-button {
  flex-shrink: 0;
  margin-left: auto;
}

.archive-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.archive-browse-link {
  color: var(--p-primary-color);
  text-decoration: underline;
  cursor: pointer;
}

@media (max-width: 42rem) {
  .archive-upload-button { margin-left: auto; }
}
</style>
