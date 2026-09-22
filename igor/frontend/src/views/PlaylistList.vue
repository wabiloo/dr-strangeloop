<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import ConfirmPopup from 'primevue/confirmpopup'
import DataTable from 'primevue/datatable'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { buildPlaylist, deletePlaylist, duplicatePlaylist, listPlaylists } from '../api/client'
import type { PlaylistListItem } from '../api/types'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const playlists = ref<PlaylistListItem[]>([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    playlists.value = await listPlaylists()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function build(name: string) {
  try {
    const job = await buildPlaylist(name)
    toast.add({
      severity: 'info',
      summary: 'Build started',
      detail: `job ${job.id} -- see channel detail pages for progress, or GET /api/v1/jobs/${job.id}`,
      life: 6000,
    })
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Build failed to start', detail: e instanceof Error ? e.message : String(e), life: 6000 })
  }
}

function outputBasename(path: string) {
  return path.replace(/[/\\]+$/, '').split(/[/\\]/).pop() || path
}

function confirmDelete(event: MouseEvent, name: string) {
  confirm.require({
    target: event.currentTarget as HTMLElement,
    message: `Delete playlist "${name}"?`,
    accept: async () => {
      await deletePlaylist(name)
      await load()
    },
  })
}

// -- Duplicate ---------------------------------------------------------
// No existing modal-name-prompt pattern elsewhere in this view (delete
// uses an inline ConfirmPopup, new-playlist just navigates to a full
// editor route) -- a small Dialog with a single name field is the
// simplest fit here, since duplicate needs a *new* name up front rather
// than a full form.
const duplicateSource = ref<string | null>(null)
const duplicateName = ref('')
const duplicating = ref(false)

const existingNames = computed(() => new Set(playlists.value.map((p) => p.name)))

function nextAvailableCopyName(source: string): string {
  const base = `${source}-copy`
  if (!existingNames.value.has(base)) return base
  for (let n = 2; ; n++) {
    const candidate = `${base}-${n}`
    if (!existingNames.value.has(candidate)) return candidate
  }
}

function openDuplicate(name: string) {
  duplicateSource.value = name
  duplicateName.value = nextAvailableCopyName(name)
}

function closeDuplicate() {
  duplicateSource.value = null
  duplicateName.value = ''
}

async function confirmDuplicate() {
  if (!duplicateSource.value || !duplicateName.value.trim()) return
  duplicating.value = true
  try {
    await duplicatePlaylist(duplicateSource.value, duplicateName.value.trim())
    closeDuplicate()
    await load()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Duplicate failed', detail: e instanceof Error ? e.message : String(e), life: 6000 })
  } finally {
    duplicating.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="flex justify-content-between align-items-center">
      <h2 class="m-0">Playlists (franken-ts)</h2>
      <div class="flex gap-2">
        <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined @click="load" :loading="loading" />
        <Button label="New playlist" icon="pi pi-plus" @click="router.push('/playlists/new')" />
      </div>
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <DataTable
      :value="playlists"
      :loading="loading"
      data-key="name"
      row-hover
      class="cursor-pointer"
      @row-click="({ data }) => router.push(`/playlists/${data.name}`)"
    >
      <Column field="name" header="Name" />
      <Column header="Output">
        <template #body="{ data }">
          <div v-if="data.output_file || data.output_dir" class="flex align-items-center gap-2" :title="data.output_file || data.output_dir">
            <i
              :class="data.output_dir ? 'pi pi-folder' : 'pi pi-file'"
              :title="data.output_dir ? 'Rendition ladder' : 'Single rendition'"
            />
            <span>{{ outputBasename(data.output_file || data.output_dir) }}</span>
          </div>
        </template>
      </Column>
      <Column field="rendition_count" header="Renditions" />
      <Column field="asset_count" header="Assets" />
      <Column field="marker_count" header="Markers" />
      <Column header="Actions">
        <template #body="{ data }">
          <div class="flex gap-2" @click.stop>
            <Button icon="pi pi-pencil" severity="secondary" text @click="router.push(`/playlists/${data.name}`)" />
            <Button icon="pi pi-play" severity="success" text @click="build(data.name)" title="Build .ts" />
            <Button icon="pi pi-clone" severity="secondary" text @click="openDuplicate(data.name)" title="Duplicate" />
            <Button icon="pi pi-trash" severity="danger" text @click="confirmDelete($event, data.name)" />
          </div>
        </template>
      </Column>
      <template #empty>No franken-ts playlists yet. Click "New playlist" to create one.</template>
    </DataTable>
    <ConfirmPopup />

    <Dialog
      :visible="duplicateSource !== null"
      modal
      header="Duplicate playlist"
      :style="{ width: '28rem' }"
      @update:visible="(v) => { if (!v) closeDuplicate() }"
    >
      <div class="flex flex-column gap-2">
        <label for="duplicate-name">New playlist name</label>
        <InputText
          id="duplicate-name"
          v-model="duplicateName"
          autofocus
          @keyup.enter="confirmDuplicate"
        />
        <small v-if="duplicateName && existingNames.has(duplicateName.trim())" class="text-red-500">
          A playlist named "{{ duplicateName.trim() }}" already exists.
        </small>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" text @click="closeDuplicate" />
        <Button
          label="Duplicate"
          icon="pi pi-clone"
          :loading="duplicating"
          :disabled="!duplicateName.trim() || existingNames.has(duplicateName.trim())"
          @click="confirmDuplicate"
        />
      </template>
    </Dialog>
  </div>
</template>
