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

const props = defineProps<{ assets: TimelineAsset[] }>()

const FALLBACK_SECONDS = 15 // weight used for assets with an unparseable/empty duration

const segments = computed(() => {
  const durations = props.assets.map((a) => parseApproxSeconds(a.duration) ?? FALLBACK_SECONDS)
  const total = durations.reduce((sum, d) => sum + d, 0) || 1
  return props.assets.map((a, i) => ({
    asset: a,
    seconds: durations[i],
    percent: (durations[i] / total) * 100,
    approx: parseApproxSeconds(a.duration) === null,
  }))
})

function fileLabel(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}
</script>

<template>
  <div class="timeline">
    <div class="timeline-track">
      <div
        v-for="(seg, i) in segments"
        :key="i"
        class="timeline-segment"
        :class="{ 'timeline-segment-ad': seg.asset.ad_break.enabled }"
        :style="{ width: seg.percent + '%' }"
        :title="`${fileLabel(seg.asset.file)} (${seg.asset.duration || 'unknown duration'})`"
      >
        <span class="timeline-segment-label">{{ fileLabel(seg.asset.file) }}</span>
        <span v-if="seg.approx" class="timeline-segment-approx">~</span>
      </div>
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

    <div class="timeline-legend">
      <span class="timeline-legend-item"><span class="timeline-swatch timeline-swatch-content" /> Content</span>
      <span class="timeline-legend-item"><span class="timeline-swatch timeline-swatch-ad" /> Ad break (SCTE-35)</span>
      <span class="timeline-legend-item text-color-secondary">~ = duration not set / not parseable, shown at equal weight</span>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.timeline-track {
  display: flex;
  height: 2.5rem;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #cbd5e1;
}

.timeline-segment {
  background: #38bdf8;
  border-right: 1px solid rgba(255, 255, 255, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 2px;
  overflow: hidden;
  position: relative;
}

.timeline-segment:last-child {
  border-right: none;
}

.timeline-segment-ad {
  background: #fb923c;
}

.timeline-segment-label {
  font-size: 0.7rem;
  color: #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 0.35rem;
}

.timeline-segment-approx {
  position: absolute;
  top: 1px;
  right: 2px;
  font-size: 0.65rem;
  color: #0f172a99;
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
