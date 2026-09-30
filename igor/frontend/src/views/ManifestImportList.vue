<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import ConfirmPopup from 'primevue/confirmpopup'
import DataTable from 'primevue/datatable'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { createManifestSource, deleteManifestSource, listManifests } from '../api/client'
import type { ManifestListItem } from '../api/types'
import { alignConfirmPopup } from '../utils/confirmPopup'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const manifests = ref<ManifestListItem[]>([])
const loading = ref(true)
const error = ref('')
const newUrl = ref('')
const adding = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    manifests.value = await listManifests()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function add() {
  if (!newUrl.value.trim()) return
  adding.value = true
  error.value = ''
  try {
    const created = await createManifestSource(newUrl.value.trim())
    newUrl.value = ''
    await router.push(`/manifests/${created.name}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    adding.value = false
  }
}

function confirmDelete(event: MouseEvent, manifest: ManifestListItem) {
  const target = (event.target as HTMLElement).closest('button') ?? (event.target as HTMLElement)
  confirm.require({
    target,
    message: `Remove manifest "${manifest.name}"? Downloaded output used by existing channels is kept.`,
    accept: async () => {
      try {
        await deleteManifestSource(manifest.name)
        await load()
        toast.add({ severity: 'success', summary: 'Manifest removed', detail: manifest.name, life: 4000 })
      } catch (e) {
        toast.add({ severity: 'error', summary: 'Remove failed', detail: e instanceof Error ? e.message : String(e), life: 6000 })
      }
    },
  })
  alignConfirmPopup(target)
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
      <h2 class="m-0">Manifests (VOD import)</h2>
      <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined @click="load" :loading="loading" />
    </div>

    <Message severity="info" :closable="false">
      Turn a VOD HLS or DASH manifest URL into a looping channel source. Every segment of the renditions
      you pick is downloaded, so the loop keeps the source's ABR ladder. For a live stream, capture it and
      use <RouterLink to="/archives">Archives</RouterLink> instead.
    </Message>

    <form class="flex gap-2 align-items-center" @submit.prevent="add">
      <InputText
        v-model="newUrl"
        type="url"
        class="flex-1"
        placeholder="https://cdn.example.com/vod/master.m3u8 or .../manifest.mpd"
        aria-label="Manifest URL"
      />
      <Button type="submit" label="Add manifest" icon="pi pi-plus" :disabled="!newUrl.trim()" :loading="adding" />
    </form>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <DataTable
      :value="manifests"
      :loading="loading"
      data-key="name"
      row-hover
      class="cursor-pointer"
      @row-click="({ data }) => router.push(`/manifests/${data.name}`)"
    >
      <Column header="Name">
        <template #body="{ data }">{{ data.display_name || data.name }}</template>
      </Column>
      <Column header="URL">
        <template #body="{ data }">
          <span class="manifest-url" :title="data.manifest_url">{{ data.manifest_url }}</span>
        </template>
      </Column>
      <Column header="Renditions">
        <template #body="{ data }">{{ data.import?.summary?.renditions.length ?? '' }}</template>
      </Column>
      <Column header="Length">
        <template #body="{ data }">{{ formatDuration(data.import?.summary?.duration_seconds) }}</template>
      </Column>
      <Column header="Import">
        <template #body="{ data }">
          <Tag v-if="!data.import" severity="secondary" value="Not imported" />
          <Tag v-else severity="success" value="Imported" />
        </template>
      </Column>
      <Column header="Actions">
        <template #body="{ data }">
          <div class="flex gap-2" @click.stop>
            <Button
              :label="data.import ? 'Re-import' : 'Inspect + import'"
              icon="pi pi-download"
              size="small"
              @click="router.push(`/manifests/${data.name}`)"
            />
            <Button
              icon="pi pi-trash"
              severity="danger"
              text
              rounded
              title="Remove manifest"
              @click="confirmDelete($event, data)"
            />
          </div>
        </template>
      </Column>
      <template #empty>No manifests yet. Paste a VOD manifest URL above.</template>
    </DataTable>
    <ConfirmPopup />
  </div>
</template>

<style scoped>
.manifest-url {
  display: inline-block;
  max-width: 34rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
</style>
