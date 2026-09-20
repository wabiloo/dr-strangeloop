<script setup lang="ts">
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { getJob } from '../api/client'
import type { Job } from '../api/types'

const props = defineProps<{ jobId: string | null; etaHint?: string }>()
const emit = defineEmits<{ (e: 'finished', job: Job): void }>()

const job = ref<Job | null>(null)
const error = ref('')
const elapsedSeconds = ref(0)
let timer: ReturnType<typeof setInterval> | null = null
let elapsedTimer: ReturnType<typeof setInterval> | null = null
let startedAtMs = 0

const elapsedLabel = computed(() => {
  const m = Math.floor(elapsedSeconds.value / 60)
  const s = elapsedSeconds.value % 60
  return `${m}:${String(s).padStart(2, '0')}`
})

function statusSeverity(status?: string) {
  switch (status) {
    case 'succeeded':
      return 'success'
    case 'failed':
      return 'danger'
    case 'running':
      return 'info'
    default:
      return 'secondary'
  }
}

async function poll() {
  if (!props.jobId) return
  try {
    const offset = job.value?.log_length ?? 0
    const update = await getJob(props.jobId, offset)
    if (job.value) {
      job.value = { ...update, log: [...job.value.log, ...update.log] }
    } else {
      job.value = update
    }
    error.value = ''
    if (update.status === 'succeeded' || update.status === 'failed') {
      stop()
      emit('finished', job.value)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function start() {
  stop()
  job.value = null
  elapsedSeconds.value = 0
  if (!props.jobId) return
  startedAtMs = Date.now()
  poll()
  timer = setInterval(poll, 1500)
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value = Math.floor((Date.now() - startedAtMs) / 1000)
  }, 1000)
}

function stop() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (elapsedTimer) {
    clearInterval(elapsedTimer)
    elapsedTimer = null
  }
}

watch(() => props.jobId, start, { immediate: true })
onBeforeUnmount(stop)
</script>

<template>
  <div v-if="jobId" class="flex flex-column gap-2">
    <div class="flex align-items-center gap-2">
      <ProgressSpinner v-if="job?.status === 'running' || !job" style="width: 1.25rem; height: 1.25rem" stroke-width="6" />
      <Tag v-if="job" :value="job.status" :severity="statusSeverity(job.status)" />
      <span v-if="job?.status === 'running'" class="text-sm text-color-secondary">elapsed {{ elapsedLabel }}</span>
      <span class="text-sm text-color-secondary">job {{ jobId }}</span>
    </div>
    <Message v-if="job?.status === 'running' && etaHint" severity="info" :closable="false">
      <i class="pi pi-clock mr-1" />Still running -- this is expected: {{ etaHint }}
    </Message>
    <Message v-if="error" severity="error">{{ error }}</Message>
    <pre v-if="job" class="job-log">{{ job.log.join('\n') || '(no output yet)' }}</pre>
  </div>
</template>
