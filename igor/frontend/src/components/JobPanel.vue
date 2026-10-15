<script setup lang="ts">
import { computed, watch } from 'vue'
import type { Job } from '../api/types'
import { selectJob, setOpen, trackJob, useJobDrawer } from '../jobDrawer'

// Output no longer renders inline: the job is registered with the bottom Jobs
// drawer (components/JobDrawer.vue), and this is just a one-line status.
const props = defineProps<{ jobId: string | null; etaHint?: string }>()
const emit = defineEmits<{ (e: 'finished', job: Job): void }>()

const { tracked } = useJobDrawer()
const job = computed(() => (props.jobId ? tracked[props.jobId]?.job ?? null : null))

let emittedFor: string | null = null

watch(
  () => props.jobId,
  (id) => {
    emittedFor = null
    if (id) trackJob({ id }, { etaHint: props.etaHint })
  },
  { immediate: true },
)
watch(
  () => props.etaHint,
  (hint) => {
    if (props.jobId && tracked[props.jobId]) tracked[props.jobId].etaHint = hint
  },
)
watch(job, (j) => {
  if (j && (j.status === 'succeeded' || j.status === 'failed') && emittedFor !== j.id) {
    emittedFor = j.id
    emit('finished', j)
  }
})

const icon = computed(() => {
  const st = job.value?.status
  return st === 'succeeded' ? 'pi pi-check-circle jp-ok' : st === 'failed' ? 'pi pi-times-circle jp-bad' : 'pi pi-spin pi-spinner jp-run'
})

function show() {
  if (!props.jobId) return
  selectJob(props.jobId)
  setOpen(true)
}
</script>

<template>
  <button v-if="jobId" type="button" class="jp-pill" title="Show output in the Jobs drawer" @click="show">
    <i :class="icon" />
    <span class="jp-status">{{ job?.cancelled ? 'cancelled' : (job?.status ?? 'queued') }}</span>
    <span class="jp-id">job {{ jobId }}</span>
    <i class="pi pi-window-maximize jp-open" />
  </button>
</template>

<style scoped>
/* Same look as a tab in JobDrawer.vue. */
.jp-pill {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  gap: 0.6rem;
  padding: 0.55rem 1rem;
  border: 0;
  border-radius: 6px;
  background: #0f172a;
  color: #e2e8f0;
  font: inherit;
  font-size: 0.85rem;
  cursor: pointer;
  white-space: nowrap;
}
.jp-pill:hover { background: #1e293b; }
.jp-status { font-weight: 600; }
.jp-id { color: #94a3b8; }
.jp-open { color: #94a3b8; font-size: 0.75rem; }
.jp-ok { color: #4ade80; }
.jp-bad { color: #f87171; }
.jp-run { color: #38bdf8; }
</style>
