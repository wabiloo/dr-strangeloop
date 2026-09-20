<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { listChannels } from '../api/client'
import type { ChannelListItem } from '../api/types'

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

function statusSeverity(status: string | null) {
  if (!status) return 'secondary'
  if (status.includes('COMPLETE') && !status.includes('ROLLBACK')) return 'success'
  if (status.includes('FAILED') || status.includes('ROLLBACK')) return 'danger'
  return 'info'
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
      <Column header="Stack status">
        <template #body="{ data }">
          <Tag :value="data.stack_status || 'not deployed'" :severity="statusSeverity(data.stack_status)" />
        </template>
      </Column>
      <Column field="stack_name" header="CloudFormation stack" />
      <template #empty>
        No channels defined yet. Click "New channel" to define one from a franken-ts output.
      </template>
    </DataTable>
  </div>
</template>
