<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'
import { onBeforeUnmount, ref, watch } from 'vue'
import { useUrlRequestForm } from '../composables/useUrlRequestForm'
import type { ChannelHealth } from '../api/types'
import { formatDuration, type TimeshiftParams } from '../utils/timeshift'
import TimeshiftPanel from './TimeshiftPanel.vue'
import UrlOverridesPanel from './UrlOverridesPanel.vue'

export interface TimeshiftPreview {
  hlsUrl: string | null
  dashUrl: string | null
  /** Wall-clock instant (epoch seconds) the stream starts at, for the
   * player's playhead display; null for a non-time-shifted URL. */
  startEpochSeconds: number | null
  label: string
}

// The Timeshift and URL overrides panels are separate forms but build ONE set of manifest
// URLs (shown, copied, previewed and tested below them), so their state lives in one place.
const props = defineProps<{
  /** The channel's normal (live) manifest URLs -- the base for generated ones. */
  hlsUrl?: string | null
  dashUrl?: string | null
  params: TimeshiftParams
  channelDefaults?: Record<string, string>
  health?: ChannelHealth | null
  previewing: boolean
}>()

const emit = defineEmits<{
  preview: [value: TimeshiftPreview | null]
  /** The URLs the forms currently build (null while they have problems), whether or not
   * previewed: the Playback test runs against these. */
  built: [value: { hlsUrl: string | null; dashUrl: string | null } | null]
}>()

const toast = useToast()
const form = useUrlRequestForm({
  hlsUrl: () => props.hlsUrl,
  dashUrl: () => props.dashUrl,
  params: () => props.params,
  health: () => props.health,
})
const { problems, kind, kindLabel, summary, range, overrideSummary, offsetSecondsNow, hlsUrlOut, dashUrlOut, canPreview, freezeOffset } = form

// What is being previewed, shown next to "Back to live". Captured at Preview time (the forms
// may be edited afterwards).
const activeSummary = ref('')
watch(
  () => props.previewing,
  (now) => {
    if (!now) activeSummary.value = ''
  },
)

watch(
  [hlsUrlOut, dashUrlOut, problems],
  () => emit('built', problems.value.length ? null : { hlsUrl: hlsUrlOut.value || null, dashUrl: dashUrlOut.value || null }),
  { immediate: true },
)
onBeforeUnmount(() => emit('built', null))

async function copy(which: 'hls' | 'dash') {
  freezeOffset() // a datetime-based offset is relative to the moment of copying
  const url = which === 'hls' ? hlsUrlOut.value : dashUrlOut.value
  try {
    await navigator.clipboard.writeText(url)
    toast.add({ severity: 'success', summary: 'Copied', detail: url, life: 2500 })
  } catch {
    toast.add({ severity: 'warn', summary: 'Copy failed', detail: 'Select the URL and copy it manually.', life: 4000 })
  }
}

function describeRequest(): string {
  const r = range.value
  const fmt = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
  const word = { live: 'Live', catchup: 'Catchup', 'startover-bounded': 'Startover', 'startover-open': 'Startover' }[kind.value]
  const off = offsetSecondsNow.value
  return [
    word,
    r ? `${fmt(r.startMs)} → ${kind.value === 'startover-open' ? 'open' : fmt(r.endMs)}` : '',
    off ? `as if ${off < 0 ? '' : 'in '}${formatDuration(Math.abs(off))}${off < 0 ? ' ago' : ''}` : '',
    overrideSummary(),
  ]
    .filter(Boolean)
    .join(' · ')
}

function startEpochSeconds(): number | null {
  const r = range.value
  return r ? r.startMs / 1000 : null
}

function preview(target: 'both' | 'hls' | 'dash') {
  freezeOffset() // a datetime-based offset is relative to the moment the stream starts
  if (!canPreview.value) return
  activeSummary.value = describeRequest()
  emit('preview', {
    hlsUrl: target === 'dash' ? null : hlsUrlOut.value || null,
    dashUrl: target === 'hls' ? null : dashUrlOut.value || null,
    startEpochSeconds: startEpochSeconds(),
    label: kindLabel.value,
  })
}
</script>

<template>
  <div class="flex flex-column gap-3">
    <TimeshiftPanel v-if="params.timeshift" :params="params" :form="form" />
    <UrlOverridesPanel v-if="params.overrides" :params="params" :form="form" :channel-defaults="channelDefaults" />

    <section class="ts-output surface-card flex flex-column gap-3" data-testid="ts-output">
      <div class="flex align-items-center gap-2 flex-wrap">
        <div class="text-sm">
          <strong>{{ kindLabel }}</strong>
          <span v-if="previewing" class="ts-active ml-2">Previewing{{ activeSummary ? ': ' + activeSummary : '' }}</span>
        </div>
        <Button
          v-if="previewing"
          label="Back to live"
          icon="pi pi-replay"
          size="small"
          severity="secondary"
          class="ml-auto"
          @click="emit('preview', null)"
        />
      </div>
      <div v-if="summary" class="text-sm text-color-secondary">{{ summary }}</div>

      <Message v-for="p in problems" :key="p" severity="warn" :closable="false">{{ p }}</Message>

      <div class="flex flex-column gap-2">
        <div v-if="hlsUrlOut" class="flex align-items-center gap-2">
          <span class="url-tag">HLS</span>
          <InputText :model-value="hlsUrlOut" readonly fluid class="font-mono text-xs" />
          <Button icon="pi pi-copy" severity="secondary" text size="small" title="Copy HLS URL" @click="copy('hls')" />
          <Button icon="pi pi-play" severity="secondary" text size="small" title="Preview in the HLS player" :disabled="!canPreview" @click="preview('hls')" />
        </div>
        <div v-if="dashUrlOut" class="flex align-items-center gap-2">
          <span class="url-tag">DASH</span>
          <InputText :model-value="dashUrlOut" readonly fluid class="font-mono text-xs" />
          <Button icon="pi pi-copy" severity="secondary" text size="small" title="Copy DASH URL" @click="copy('dash')" />
          <Button icon="pi pi-play" severity="secondary" text size="small" title="Preview in the DASH player" :disabled="!canPreview" @click="preview('dash')" />
        </div>
      </div>

      <div class="flex gap-2 flex-wrap">
        <Button label="Preview in both players" icon="pi pi-play" size="small" :disabled="!canPreview" @click="preview('both')" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.ts-active {
  color: var(--p-primary-color, #c0392b);
  font-weight: 600;
}
.ts-output {
  padding: 1rem;
  border-radius: var(--p-border-radius, 0.5rem);
  border: 1px solid var(--p-surface-200, #e5e7eb);
}
.url-tag {
  min-width: 3.25rem;
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-align: center;
  padding: 0.15rem 0.4rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
  color: var(--p-text-color, #111827);
}
</style>
