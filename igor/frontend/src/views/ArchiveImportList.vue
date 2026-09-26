<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { listArchives } from '../api/client'
import type { ArchiveListItem } from '../api/types'

const router = useRouter()

const archives = ref<ArchiveListItem[]>([])
const loading = ref(true)
const error = ref('')

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
      Drop a captured HAR (Chrome DevTools/Fiddler) or Proxyman log into <code>data/archives/</code>,
      then click one below to review its variants and import a loop timeline from it -- an
      alternative to authoring a franken-ts playlist, for re-serving an already-observed real
      HLS/DASH session.
    </Message>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <DataTable
      :value="archives"
      :loading="loading"
      data-key="name"
      row-hover
      class="cursor-pointer"
      @row-click="({ data }) => router.push(`/archives/${data.name}`)"
    >
      <Column field="name" header="Name" />
      <Column header="Format">
        <template #body="{ data }">
          <span class="uppercase">{{ data.format }}</span>
        </template>
      </Column>
      <Column header="Session length">
        <template #body="{ data }">{{ formatDuration(data.session_duration_seconds) }}</template>
      </Column>
      <Column field="variant_count" header="Variants" />
      <Column field="marker_count" header="Markers (approx.)" />
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
          </div>
        </template>
      </Column>
      <template #empty>
        No archives found in <code>data/archives/</code>. Drop a HAR/Proxyman log there and click Refresh.
      </template>
    </DataTable>
  </div>
</template>
