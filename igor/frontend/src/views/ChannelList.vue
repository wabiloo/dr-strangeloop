<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { listChannels } from '../api/client'
import type { ChannelListItem } from '../api/types'
import { listItemPhase, phaseSeverity } from '../utils/channelPhase'

const router = useRouter()
const channels = ref<ChannelListItem[]>([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    channels.value = await listChannels()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// Badge color reflects the same running/stopped/failed/... phase across
// backends (see utils/channelPhase.ts); the tag text keeps the raw
// backend-specific status string, since that's still useful for debugging.
function statusSeverity(channel: ChannelListItem) {
  return phaseSeverity(listItemPhase(channel))
}

onMounted(load)
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="flex justify-content-between align-items-center">
      <h2 class="m-0">Channels</h2>
      <div class="flex gap-2">
        <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined @click="load" :loading="loading" />
        <Button label="New channel" icon="pi pi-plus" @click="router.push('/channels/new')" />
      </div>
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <DataTable :value="channels" :loading="loading" data-key="name" @row-click="(e) => router.push(`/channels/${e.data.name}`)" class="cursor-pointer">
      <Column field="name" header="Name" />
      <Column field="backend" header="Backend" />
      <Column header="Playlist">
        <template #body="{ data }">
          <RouterLink
            v-if="data.playlist_name"
            :to="`/playlists/${data.playlist_name}`"
            class="playlist-link"
            @click.stop
          >
            {{ data.playlist_name }}
          </RouterLink>
          <span v-else-if="data.source_path" class="text-color-secondary text-sm" :title="data.source_path">
            {{ data.source_path }}
          </span>
          <span v-else class="text-color-secondary text-sm">--</span>
        </template>
      </Column>
      <Column header="Stack status">
        <template #body="{ data }">
          <Tag :value="data.stack_status || 'not deployed'" :severity="statusSeverity(data)" />
        </template>
      </Column>
      <Column field="stack_name" header="CloudFormation stack" />
      <template #empty>
        No channels defined yet. Click "New channel" to define one from a franken-ts output.
      </template>
    </DataTable>
  </div>
</template>

<style scoped>
.playlist-link {
  color: var(--p-primary-color, #0e7490);
  text-decoration: none;
  font-weight: 600;
}

.playlist-link:hover {
  text-decoration: underline;
}
</style>
