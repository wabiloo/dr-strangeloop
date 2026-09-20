<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import ConfirmPopup from 'primevue/confirmpopup'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { buildConfig, deleteConfig, listConfigs } from '../api/client'
import type { ConfigListItem } from '../api/types'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const configs = ref<ConfigListItem[]>([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    configs.value = await listConfigs()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function build(name: string) {
  try {
    const job = await buildConfig(name)
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

function confirmDelete(event: MouseEvent, name: string) {
  confirm.require({
    target: event.currentTarget as HTMLElement,
    message: `Delete config "${name}"?`,
    accept: async () => {
      await deleteConfig(name)
      await load()
    },
  })
}

onMounted(load)
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="flex justify-content-between align-items-center">
      <h2 class="m-0">Content configs (franken-ts)</h2>
      <div class="flex gap-2">
        <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined @click="load" :loading="loading" />
        <Button label="New config" icon="pi pi-plus" @click="router.push('/configs/new')" />
      </div>
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <DataTable :value="configs" :loading="loading" data-key="name">
      <Column field="name" header="Name" />
      <Column field="output_file" header="Output file" />
      <Column field="output_dir" header="Output dir (ladder)" />
      <Column field="asset_count" header="Assets" />
      <Column field="ad_break_count" header="Ad breaks" />
      <Column header="Actions">
        <template #body="{ data }">
          <div class="flex gap-2">
            <Button icon="pi pi-pencil" severity="secondary" text @click="router.push(`/configs/${data.name}`)" />
            <Button icon="pi pi-play" severity="success" text @click="build(data.name)" title="Build .ts" />
            <Button icon="pi pi-trash" severity="danger" text @click="confirmDelete($event, data.name)" />
          </div>
        </template>
      </Column>
      <template #empty>No franken-ts configs yet. Click "New config" to create one.</template>
    </DataTable>
    <ConfirmPopup />
  </div>
</template>
