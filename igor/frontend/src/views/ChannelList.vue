<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import ConfirmPopup from 'primevue/confirmpopup'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { deleteChannel, listChannels } from '../api/client'
import type { ChannelListItem } from '../api/types'
import { PHASE_LABEL, listItemPhase, phaseSeverity } from '../utils/channelPhase'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()
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
// backends (see utils/channelPhase.ts).
function statusSeverity(channel: ChannelListItem) {
  return phaseSeverity(listItemPhase(channel))
}

// Running-status tag: the resolved phase (what you actually came here to
// know -- is it serving or not), independent of backend.
function runningStatusLabel(channel: ChannelListItem) {
  return PHASE_LABEL[listItemPhase(channel)]
}

// Stack status only makes sense for backends with a real CloudFormation
// stack (aws-media/ecs-express). local-docker repurposes `stack_status` to
// carry Docker's State.Status, which isn't a "stack" at all, so don't show
// it in that column.
function hasStack(channel: ChannelListItem) {
  return channel.backend !== 'local-docker'
}

function stackSeverity(channel: ChannelListItem) {
  if (!channel.stack_status) return 'secondary'
  if (channel.stack_status.includes('ROLLBACK') || channel.stack_status.includes('FAILED')) return 'danger'
  if (channel.stack_status.includes('IN_PROGRESS')) return 'info'
  if (channel.stack_status.includes('COMPLETE')) return 'success'
  return 'secondary'
}

// Deleting only removes igor's local TOML config, never touches AWS/Docker
// (see routes/channels.py's delete_channel) -- restrict it in the UI to
// channels with no deployed stack/container at all, so it can't be used to
// orphan a running or stopped-but-still-deployed resource. "not deployed"
// is the one Phase where there's nothing left to tear down.
function isDeletable(channel: ChannelListItem) {
  return listItemPhase(channel) === 'not-deployed'
}

function deleteDisabledReason(channel: ChannelListItem) {
  if (isDeletable(channel)) return undefined
  return 'Only channels with no deployed stack/container can be deleted here -- destroy the deployment first.'
}

async function confirmDelete(event: MouseEvent, channel: ChannelListItem) {
  confirm.require({
    target: event.currentTarget as HTMLElement,
    message: `Delete channel "${channel.name}"? This only removes its local config -- there is nothing deployed to tear down.`,
    accept: async () => {
      try {
        await deleteChannel(channel.name)
        await load()
      } catch (e) {
        toast.add({
          severity: 'error',
          summary: 'Delete failed',
          detail: e instanceof Error ? e.message : String(e),
          life: 6000,
        })
      }
    },
  })
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
      <Column header="Running status">
        <template #body="{ data }">
          <Tag :value="runningStatusLabel(data)" :severity="statusSeverity(data)" />
        </template>
      </Column>
      <Column header="Stack status">
        <template #body="{ data }">
          <Tag v-if="hasStack(data) && data.stack_status" :value="data.stack_status" :severity="stackSeverity(data)" />
          <span v-else class="text-color-secondary text-sm">--</span>
        </template>
      </Column>
      <Column field="stack_name" header="CloudFormation stack" />
      <Column header="Actions">
        <template #body="{ data }">
          <div class="flex gap-2" @click.stop>
            <Button
              icon="pi pi-trash"
              severity="danger"
              text
              :disabled="!isDeletable(data)"
              :title="deleteDisabledReason(data) ?? 'Delete channel'"
              @click="confirmDelete($event, data)"
            />
          </div>
        </template>
      </Column>
      <template #empty>
        No channels defined yet. Click "New channel" to define one from a franken-ts output.
      </template>
    </DataTable>
    <ConfirmPopup />
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
