<script setup lang="ts">
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import JobPanel from '../components/JobPanel.vue'
import {
  createChannel,
  getChannelHealth,
  getChannelOutputs,
  getChannelStatus,
  redeployChannel,
  refreshChannel,
  sparkChannel,
  startChannel,
  stopChannel,
} from '../api/client'
import type { ChannelHealth, ChannelOutputs, ChannelStatus } from '../api/types'

const props = defineProps<{ name: string }>()

const status = ref<ChannelStatus | null>(null)
const outputs = ref<ChannelOutputs | null>(null)
const health = ref<ChannelHealth | null>(null)
const statusError = ref('')
const healthError = ref('')
const loading = ref(false)
const activeJobId = ref<string | null>(null)
const activeAction = ref('')

const toast = useToast()
let healthTimer: ReturnType<typeof setInterval> | null = null

async function loadStatus() {
  statusError.value = ''
  try {
    ;[status.value, outputs.value] = await Promise.all([
      getChannelStatus(props.name),
      getChannelOutputs(props.name),
    ])
  } catch (e) {
    statusError.value = e instanceof Error ? e.message : String(e)
  }
}

async function loadHealth() {
  if (status.value?.backend !== 'ecs-express') return
  try {
    health.value = await getChannelHealth(props.name)
    healthError.value = ''
  } catch (e) {
    healthError.value = e instanceof Error ? e.message : String(e)
  }
}

async function run(action: string, fn: () => Promise<{ id: string }>) {
  loading.value = true
  activeAction.value = action
  try {
    const job = await fn()
    activeJobId.value = job.id
  } catch (e) {
    toast.add({ severity: 'error', summary: `${action} failed`, detail: e instanceof Error ? e.message : String(e), life: 6000 })
  } finally {
    loading.value = false
  }
}

function onJobFinished() {
  toast.add({
    severity: 'info',
    summary: `${activeAction.value} finished`,
    detail: `See the job log above for details.`,
    life: 4000,
  })
  loadStatus()
}

onMounted(() => {
  loadStatus()
  healthTimer = setInterval(loadHealth, 5000)
  loadHealth()
})
onBeforeUnmount(() => {
  if (healthTimer) clearInterval(healthTimer)
})
</script>

<template>
  <div class="flex flex-column gap-4">
    <div class="flex align-items-center gap-2">
      <h2 class="m-0">{{ name }}</h2>
      <Tag v-if="status" :value="status.backend" />
      <Tag v-if="status" :value="status.status" severity="info" />
    </div>

    <Message v-if="statusError" severity="warn">
      Could not fetch live status (channel may not be deployed yet): {{ statusError }}
    </Message>

    <div class="flex gap-2 flex-wrap">
      <Button label="Create / deploy" icon="pi pi-cloud-upload" :loading="loading" @click="run('create', () => createChannel(name))" />
      <Button label="Spark" icon="pi pi-bolt" severity="secondary" :loading="loading" @click="run('spark', () => sparkChannel(name))" />
      <Button label="Start" icon="pi pi-play" severity="success" :loading="loading" @click="run('start', () => startChannel(name))" />
      <Button label="Stop" icon="pi pi-stop" severity="danger" :loading="loading" @click="run('stop', () => stopChannel(name))" />
      <Button label="Refresh content" icon="pi pi-refresh" severity="secondary" :loading="loading" @click="run('refresh', () => refreshChannel(name))" />
      <Button label="Redeploy" icon="pi pi-wrench" severity="warn" outlined :loading="loading" @click="run('redeploy', () => redeployChannel(name))" />
    </div>

    <JobPanel :job-id="activeJobId" @finished="onJobFinished" />

    <div v-if="outputs" class="flex flex-column gap-2">
      <h3 class="m-0">Playback</h3>
      <div v-if="outputs.HlsPlaybackUrl">
        HLS: <a :href="outputs.HlsPlaybackUrl" target="_blank">{{ outputs.HlsPlaybackUrl }}</a>
      </div>
      <div v-if="outputs.DashPlaybackUrl">
        DASH: <a :href="outputs.DashPlaybackUrl" target="_blank">{{ outputs.DashPlaybackUrl }}</a>
      </div>
    </div>

    <div v-if="status?.backend === 'ecs-express'" class="flex flex-column gap-2">
      <h3 class="m-0">Live loop position</h3>
      <Message v-if="healthError" severity="warn">
        No health data yet ({{ healthError }}) -- the deployed task may predate the /health
        endpoint; redeploy/refresh to pick it up.
      </Message>
      <div v-if="health" class="flex flex-column gap-1 text-sm">
        <div>Loop number: {{ health.loop_number }}</div>
        <div>Position in loop: {{ health.position_in_loop_seconds.toFixed(1) }}s / {{ health.total_loop_duration_seconds.toFixed(1) }}s</div>
        <div>Uptime: {{ health.uptime_seconds.toFixed(0) }}s</div>
        <div>Renditions: {{ health.renditions.join(', ') }}</div>
      </div>
    </div>

    <div v-if="outputs" class="flex flex-column gap-2">
      <h3 class="m-0">Stack outputs</h3>
      <pre class="job-log">{{ JSON.stringify(outputs, null, 2) }}</pre>
    </div>
  </div>
</template>
