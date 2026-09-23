<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import ConfirmPopup from 'primevue/confirmpopup'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { deleteChannel, getJob, listChannels, startChannel, stopChannel } from '../api/client'
import type { ChannelListItem, Job } from '../api/types'
import { PHASE_LABEL, isUpButMaybeUnreachable, listItemPhase, phaseSeverity } from '../utils/channelPhase'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()
const channels = ref<ChannelListItem[]>([])
const loading = ref(true)
const error = ref('')

// Channel names with a start/stop job currently in flight -- disables both
// buttons on that row (never both backends' worth of jobs at once for the
// same channel) and shows a spinner instead of the icon. Keyed by name,
// not a single global flag, so acting on one channel doesn't freeze the
// whole table.
const pendingActions = reactive<Set<string>>(new Set())
const jobTimers = new Map<string, ReturnType<typeof setInterval>>()

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

// aws-media/ecs-express: the CloudFormation stack name. local-docker has
// no stack -- its nearest equivalent identifier is the deterministic
// local Docker container name (see channel.py's cmd_list).
function stackOrContainerName(channel: ChannelListItem) {
  return channel.stack_name ?? channel.container_name ?? null
}

// local-docker repurposes `stack_status` to carry Docker's raw
// State.Status ("running", "exited", "not created", ...) instead of a
// CloudFormation StackStatus -- color it the same way the Running status
// column already does (via listItemPhase) rather than the CFN-specific
// substring matching below, which wouldn't recognize those strings.
function stackSeverity(channel: ChannelListItem) {
  if (channel.backend === 'local-docker') return phaseSeverity(listItemPhase(channel))
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

// Start/Stop mirror ChannelDetail.vue's stream actions -- see there for
// why the disabled conditions differ per backend (local-docker's `start`
// creates the container fresh so it works from 'not-deployed' too;
// aws-media/ecs-express's `start` reads stack outputs so it genuinely
// needs the stack deployed first).
//
// "Relevant" (shown vs. hidden-but-space-reserved) is purely phase/backend
// based -- a job in flight (pendingActions) never hides a button, it only
// disables it, so mid-action the row doesn't visually shift.
function isStartRelevant(channel: ChannelListItem) {
  const phase = listItemPhase(channel)
  return channel.backend === 'local-docker' ? !isUpButMaybeUnreachable(phase) : phase === 'stopped'
}

function isStartDisabled(channel: ChannelListItem) {
  return pendingActions.has(channel.name)
}

function isStopRelevant(channel: ChannelListItem) {
  return isUpButMaybeUnreachable(listItemPhase(channel))
}

function isStopDisabled(channel: ChannelListItem) {
  return pendingActions.has(channel.name)
}

// Fires the job, then polls it to completion (same polling cadence as
// JobPanel.vue, without the log/UI panel -- the list is a quick-actions
// surface, not where you'd want to read a job log) and reloads the list
// once it settles so the Running-status/Stack-status columns pick up the
// new phase.
async function runAction(channel: ChannelListItem, action: 'start' | 'stop') {
  if (pendingActions.has(channel.name)) return
  pendingActions.add(channel.name)
  try {
    const job = await (action === 'start' ? startChannel(channel.name) : stopChannel(channel.name))
    pollJob(channel.name, action, job.id)
  } catch (e) {
    pendingActions.delete(channel.name)
    toast.add({
      severity: 'error',
      summary: `${action === 'start' ? 'Start' : 'Stop'} failed`,
      detail: e instanceof Error ? e.message : String(e),
      life: 6000,
    })
  }
}

function pollJob(channelName: string, action: 'start' | 'stop', jobId: string) {
  let lastOffset = 0
  const label = action === 'start' ? 'Start' : 'Stop'
  const finish = async (job: Job) => {
    const timer = jobTimers.get(channelName)
    if (timer) {
      clearInterval(timer)
      jobTimers.delete(channelName)
    }
    pendingActions.delete(channelName)
    if (job.status === 'failed') {
      toast.add({
        severity: 'error',
        summary: `${label} failed`,
        detail: job.log.slice(-1)[0] ?? 'See channel detail page for the job log.',
        life: 8000,
      })
    } else {
      toast.add({ severity: 'success', summary: `${label} succeeded`, detail: channelName, life: 4000 })
    }
    await load()
  }
  const tick = async () => {
    try {
      const job = await getJob(jobId, lastOffset)
      lastOffset = job.log_length
      if (job.status === 'succeeded' || job.status === 'failed') {
        await finish(job)
      }
    } catch (e) {
      const timer = jobTimers.get(channelName)
      if (timer) {
        clearInterval(timer)
        jobTimers.delete(channelName)
      }
      pendingActions.delete(channelName)
      toast.add({
        severity: 'error',
        summary: `${label}: lost track of job`,
        detail: e instanceof Error ? e.message : String(e),
        life: 6000,
      })
    }
  }
  jobTimers.set(channelName, setInterval(tick, 1500))
  tick()
}

onMounted(load)
onBeforeUnmount(() => {
  for (const timer of jobTimers.values()) clearInterval(timer)
  jobTimers.clear()
})
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
      <Column header="State">
        <template #body="{ data }">
          <Tag :value="runningStatusLabel(data)" :severity="statusSeverity(data)" />
        </template>
      </Column>
      <Column header="Stack">
        <template #body="{ data }">
          <span v-if="stackOrContainerName(data)">{{ stackOrContainerName(data) }}</span>
          <span v-else class="text-color-secondary text-sm">--</span>
        </template>
      </Column>
      <Column header="Stack status">
        <template #body="{ data }">
          <Tag v-if="data.stack_status" :value="data.stack_status" :severity="stackSeverity(data)" />
          <span v-else class="text-color-secondary text-sm">--</span>
        </template>
      </Column>
      <Column header="Actions">
        <template #body="{ data }">
          <div class="flex gap-2" @click.stop>
            <Button
              icon="pi pi-play"
              severity="success"
              text
              :style="{ visibility: isStartRelevant(data) ? 'visible' : 'hidden' }"
              :disabled="isStartDisabled(data)"
              :loading="pendingActions.has(data.name)"
              title="Start"
              @click="runAction(data, 'start')"
            />
            <Button
              icon="pi pi-stop"
              severity="danger"
              text
              :style="{ visibility: isStopRelevant(data) ? 'visible' : 'hidden' }"
              :disabled="isStopDisabled(data)"
              :loading="pendingActions.has(data.name)"
              title="Stop"
              @click="runAction(data, 'stop')"
            />
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
  color: var(--p-primary-color, #b91c1c);
  text-decoration: none;
  font-weight: 600;
}

.playlist-link:hover {
  text-decoration: underline;
}
</style>
