<script setup lang="ts">
import { computed } from 'vue'
import { parseApproxSeconds } from '../utils/duration'

interface TimelineAsset {
  file: string
  duration: string
  ad_break: {
    enabled: boolean
    event_id: number | null
    splice_type: 'splice_insert' | 'time_signal'
  }
}

const props = defineProps<{ assets: TimelineAsset[]; selectedIndex?: number | null }>()
const emit = defineEmits<{ select: [index: number]; add: [] }>()

const FALLBACK_SECONDS = 15 // weight used for assets with an unparseable/empty duration

const segments = computed(() => {
  const durations = props.assets.map((a) => parseApproxSeconds(a.duration) ?? FALLBACK_SECONDS)
  const total = durations.reduce((sum, d) => sum + d, 0) || 1
  let offset = 0
  return props.assets.map((a, i) => {
    const seconds = durations[i]
    const seg = {
      asset: a,
      seconds,
      offsetSeconds: offset,
      percent: (seconds / total) * 100,
      approx: parseApproxSeconds(a.duration) === null,
    }
    offset += seconds
    return seg
  })
})

const totalSeconds = computed(() => segments.value.reduce((sum, s) => sum + s.seconds, 0) || 1)
const hasApproxSegments = computed(() => segments.value.some((s) => s.approx))

/** "Nice" round numbers to space ruler ticks at, in seconds. */
const NICE_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]

const tickStepSeconds = computed(() => {
  const total = totalSeconds.value
  // Aim for roughly 5-10 ticks across the timeline.
  const target = total / 7
  return NICE_STEPS.find((step) => step >= target) ?? NICE_STEPS[NICE_STEPS.length - 1]
})

const ticks = computed(() => {
  const step = tickStepSeconds.value
  const total = totalSeconds.value
  const result: { seconds: number; percent: number }[] = []
  for (let t = 0; t <= total + 0.001; t += step) {
    result.push({ seconds: t, percent: (t / total) * 100 })
  }
  return result
})

function formatDuration(totalSecondsValue: number): string {
  const s = Math.round(totalSecondsValue)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  if (m > 0) return `${m}:${String(sec).padStart(2, '0')}`
  return `${sec}s`
}

function fileLabel(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}
</script>

<template>
  <div class="timeline">
    <div class="timeline-header">
      <span class="font-semibold">Total duration: {{ formatDuration(totalSeconds) }}</span>
      <span v-if="hasApproxSegments" class="text-color-secondary text-sm">(approximate -- see ~ below)</span>
    </div>

    <div class="timeline-track">
      <div
        v-for="(seg, i) in segments"
        :key="i"
        class="timeline-segment"
        :class="{ 'timeline-segment-ad': seg.asset.ad_break.enabled, 'timeline-segment-selected': i === selectedIndex }"
        :style="{ width: seg.percent + '%' }"
        :title="`${fileLabel(seg.asset.file)} -- ${formatDuration(seg.seconds)}${seg.approx ? ' (approximate)' : ''} -- click to edit`"
        role="button"
        tabindex="0"
        @click="emit('select', i)"
        @keydown.enter="emit('select', i)"
      >
        <span class="timeline-segment-label">{{ fileLabel(seg.asset.file) || `Asset ${i + 1}` }}</span>
        <span class="timeline-segment-duration">{{ formatDuration(seg.seconds) }}<span v-if="seg.approx">~</span></span>
      </div>
      <button type="button" class="timeline-add-segment" title="Add asset" @click="emit('add')">
        <i class="pi pi-plus" />
      </button>
    </div>

    <div class="timeline-markers">
      <template v-for="(seg, i) in segments" :key="'m' + i">
        <div v-if="seg.asset.ad_break.enabled" class="timeline-marker-pair" :style="{ width: seg.percent + '%' }">
          <div class="timeline-marker timeline-marker-out">
            <i class="pi pi-flag-fill" />
            <span>OUT{{ seg.asset.ad_break.event_id !== null ? ` #${seg.asset.ad_break.event_id}` : '' }}</span>
          </div>
          <div class="timeline-marker timeline-marker-in">
            <i class="pi pi-flag" />
            <span>IN</span>
          </div>
        </div>
        <div v-else :style="{ width: seg.percent + '%' }" />
      </template>
    </div>

    <div class="timeline-ruler">
      <div
        v-for="(tick, i) in ticks"
        :key="i"
        class="timeline-tick"
        :style="{ left: tick.percent + '%' }"
      >
        <span class="timeline-tick-line" />
        <span class="timeline-tick-label">{{ formatDuration(tick.seconds) }}</span>
      </div>
    </div>

    <div class="timeline-legend">
      <span class="timeline-legend-item"><span class="timeline-swatch timeline-swatch-content" /> Content</span>
      <span class="timeline-legend-item"><span class="timeline-swatch timeline-swatch-ad" /> Ad break (SCTE-35)</span>
      <span class="timeline-legend-item text-color-secondary">~ = duration not set / not parseable, shown at {{ formatDuration(FALLBACK_SECONDS) }} placeholder weight</span>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.timeline-header {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.timeline-track {
  display: flex;
  height: 2.75rem;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #cbd5e1;
}

.timeline-segment {
  background: #38bdf8;
  border-right: 1px solid rgba(255, 255, 255, 0.6);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-width: 2px;
  overflow: hidden;
  position: relative;
  cursor: pointer;
  transition: filter 0.1s ease;
}

.timeline-segment:hover {
  filter: brightness(0.92);
}

.timeline-segment:last-child {
  border-right: none;
}

.timeline-segment-ad {
  background: #fb923c;
}

.timeline-segment-selected {
  outline: 3px solid #0f172a;
  outline-offset: -3px;
  z-index: 1;
}

.timeline-add-segment {
  flex: 0 0 2.75rem;
  width: 2.75rem;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f1f5f9;
  border: none;
  border-left: 1px dashed #cbd5e1;
  color: #475569;
  cursor: pointer;
}

.timeline-add-segment:hover {
  background: #e2e8f0;
  color: #0f172a;
}

.timeline-segment-label {
  font-size: 0.7rem;
  color: #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 0.35rem;
  max-width: 100%;
}

.timeline-segment-duration {
  font-size: 0.65rem;
  color: #0f172acc;
  white-space: nowrap;
}

.timeline-markers {
  display: flex;
  height: 1.1rem;
}

.timeline-marker-pair {
  display: flex;
  justify-content: space-between;
  position: relative;
}

.timeline-marker {
  display: flex;
  align-items: center;
  gap: 0.2rem;
  font-size: 0.65rem;
  color: #ea580c;
  white-space: nowrap;
}

.timeline-ruler {
  position: relative;
  height: 1.5rem;
  margin-top: 0.15rem;
}

.timeline-tick {
  position: absolute;
  top: 0;
  transform: translateX(-1px);
  display: flex;
  flex-direction: column;
  align-items: center;
}

/* Last tick's label would overflow the container to the right -- anchor
 * it to the right edge of the tick mark instead of centering. */
.timeline-tick:last-child {
  transform: translateX(-100%);
}

.timeline-tick-line {
  width: 1px;
  height: 0.4rem;
  background: #94a3b8;
}

.timeline-tick-label {
  font-size: 0.65rem;
  color: #64748b;
  white-space: nowrap;
  margin-top: 0.1rem;
}

.timeline-legend {
  display: flex;
  gap: 1.25rem;
  font-size: 0.75rem;
  flex-wrap: wrap;
}

.timeline-legend-item {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.timeline-swatch {
  display: inline-block;
  width: 0.8rem;
  height: 0.8rem;
  border-radius: 3px;
}

.timeline-swatch-content {
  background: #38bdf8;
}

.timeline-swatch-ad {
  background: #fb923c;
}
</style>
