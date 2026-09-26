<script setup lang="ts">
import Button from 'primevue/button'
import Message from 'primevue/message'
import RadioButton from 'primevue/radiobutton'
import Slider from 'primevue/slider'
import Tag from 'primevue/tag'
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { getArchiveCoverage, importArchive } from '../api/client'
import JobPanel from '../components/JobPanel.vue'
import type { ArchiveCoverage, Job, VariantCoverage } from '../api/types'

const props = defineProps<{ name: string }>()
const router = useRouter()

const coverage = ref<ArchiveCoverage | null>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    coverage.value = await getArchiveCoverage(props.name)
    if (coverage.value.variants.length > 0) selectedVariantUrl.value = coverage.value.variants[0].manifest_url
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

// ── Overall session span (union of every variant's own covered ranges) ────
// grave-robber/SCOPE.md §7's own loop-boundary rule (full captured span,
// no auto-detection) applies per selected variant at import time, not
// here -- this is purely the wizard's own display window for the range
// picker below.
const sessionStartMs = computed(() => {
  const starts = (coverage.value?.variants ?? []).flatMap((v) => v.covered_ranges.map((r) => Date.parse(r.start)))
  return starts.length ? Math.min(...starts) : 0
})
const sessionEndMs = computed(() => {
  const ends = (coverage.value?.variants ?? []).flatMap((v) => v.covered_ranges.map((r) => Date.parse(r.end)))
  return ends.length ? Math.max(...ends) : 0
})
const sessionDurationMs = computed(() => Math.max(1, sessionEndMs.value - sessionStartMs.value))

function rangeStyle(range: { start: string; end: string }) {
  const startPct = ((Date.parse(range.start) - sessionStartMs.value) / sessionDurationMs.value) * 100
  const widthPct = ((Date.parse(range.end) - Date.parse(range.start)) / sessionDurationMs.value) * 100
  return { left: `${startPct}%`, width: `${widthPct}%` }
}

// ── SCOPE.md §8 step 3: human-picked target range, as a % of the session
// span (a lightweight purpose-built lane picker -- AssetTimeline.vue's own
// lane rendering is deeply coupled to franken-ts's asset/marker model
// (TimelineAsset/MarkerLike) and isn't a practical fit for wall-clock
// variant-coverage ranges without a large, fragile adapter; this
// visualization mirrors its spirit (proportional horizontal lanes) with
// its own small implementation instead). ──────────────────────────────────
const rangePercent = ref<[number, number]>([0, 100])
const targetRange = computed<[Date, Date]>(() => [
  new Date(sessionStartMs.value + (rangePercent.value[0] / 100) * sessionDurationMs.value),
  new Date(sessionStartMs.value + (rangePercent.value[1] / 100) * sessionDurationMs.value),
])

// SCOPE.md §8 steps 3-4: keep only variants whose coverage is a *superset*
// of the picked range (zero gaps); everything else is dropped, not
// trimmed/reconciled. Same algorithm as grave-robber's own
// coverage.filter_variants_covering_range, reimplemented here in TS for
// instant client-side feedback as the slider moves.
function coversFully(ranges: { start: string; end: string }[], targetStart: Date, targetEnd: Date): boolean {
  const sorted = [...ranges].sort((a, b) => Date.parse(a.start) - Date.parse(b.start))
  let cursor = targetStart.getTime()
  for (const r of sorted) {
    const rStart = Date.parse(r.start)
    const rEnd = Date.parse(r.end)
    if (rStart > cursor) break
    if (rEnd > cursor) cursor = rEnd
    if (cursor >= targetEnd.getTime()) return true
  }
  return cursor >= targetEnd.getTime()
}

const survivors = computed<VariantCoverage[]>(() => {
  const [start, end] = targetRange.value
  return (coverage.value?.variants ?? []).filter((v) => coversFully(v.covered_ranges, start, end))
})
const dropped = computed<VariantCoverage[]>(() => {
  const survivorUrls = new Set(survivors.value.map((v) => v.manifest_url))
  return (coverage.value?.variants ?? []).filter((v) => !survivorUrls.has(v.manifest_url))
})

// SCOPE.md §8 step 5: residual reference pick -- arbitrary among survivors
// once they all share the same window, but the human can still pick.
const selectedVariantUrl = ref<string | null>(null)

// ── Import ──────────────────────────────────────────────────────────────
const importing = ref(false)
const importJobId = ref<string | null>(null)
const importError = ref('')

async function startImport() {
  if (!selectedVariantUrl.value) return
  const variant = coverage.value?.variants.find((v) => v.manifest_url === selectedVariantUrl.value)
  if (!variant) return
  importing.value = true
  importError.value = ''
  try {
    const job = await importArchive(props.name, variant.manifest_url, variant.format)
    importJobId.value = job.id
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = false
  }
}

function onImportFinished(job: Job) {
  if (job.status === 'succeeded') {
    router.push('/channels/new')
  } else {
    importError.value = 'Import failed -- see log above.'
  }
}
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="flex align-items-center gap-2">
      <Button icon="pi pi-arrow-left" text severity="secondary" @click="router.push('/archives')" />
      <h2 class="m-0">Import: {{ name }}</h2>
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>
    <Message v-if="!loading && coverage && coverage.variants.length === 0" severity="warn">
      No HLS/DASH manifest URLs were detected in this archive.
    </Message>

    <template v-if="coverage && coverage.variants.length > 0">
      <h4 class="mb-0">1. Per-variant coverage</h4>
      <p class="text-color-secondary text-sm m-0">
        Each lane shows the wall-clock stretches this variant was actually captured for -- gaps are
        stretches a live player wasn't using this variant (SCOPE.md §8).
      </p>
      <div class="flex flex-column gap-2">
        <div v-for="variant in coverage.variants" :key="variant.manifest_url" class="flex flex-column gap-1">
          <div class="text-xs text-color-secondary" style="word-break: break-all">
            <Tag :value="variant.format" severity="secondary" class="mr-2" />{{ variant.manifest_url }}
          </div>
          <div class="coverage-lane">
            <div v-for="(range, i) in variant.covered_ranges" :key="i" class="coverage-range" :style="rangeStyle(range)" />
            <div class="coverage-range-picker" :style="{ left: `${rangePercent[0]}%`, width: `${rangePercent[1] - rangePercent[0]}%` }" />
          </div>
        </div>
      </div>

      <h4 class="mb-0 mt-2">2. Pick a target range</h4>
      <Slider v-model="rangePercent" range :min="0" :max="100" :step="0.5" />
      <div class="text-sm text-color-secondary">
        {{ targetRange[0].toISOString() }} &rarr; {{ targetRange[1].toISOString() }}
      </div>

      <h4 class="mb-0 mt-2">3. Variants covering this range</h4>
      <div class="flex flex-column gap-2">
        <div v-for="variant in survivors" :key="variant.manifest_url" class="flex align-items-center gap-2">
          <RadioButton v-model="selectedVariantUrl" :input-id="variant.manifest_url" :value="variant.manifest_url" name="reference-variant" />
          <label :for="variant.manifest_url" class="text-sm" style="word-break: break-all">{{ variant.manifest_url }}</label>
        </div>
        <Message v-if="survivors.length === 0" severity="warn" :closable="false">
          No variant fully covers the selected range -- widen the range or pick a different one.
        </Message>
      </div>
      <div v-if="dropped.length > 0" class="text-xs text-color-secondary">
        Dropped (don't fully cover the range): {{ dropped.length }} variant(s).
      </div>

      <h4 class="mb-0 mt-2">4. Import</h4>
      <p class="text-color-secondary text-sm m-0">
        The selected variant's own <strong>full captured span</strong> is used as the loop
        (SCOPE.md §7 -- the range above only filters which variant is eligible, it doesn't trim
        the imported timeline).
      </p>
      <Message v-if="importError" severity="error">{{ importError }}</Message>
      <div>
        <Button
          label="Import"
          icon="pi pi-download"
          :loading="importing"
          :disabled="!selectedVariantUrl"
          @click="startImport"
        />
      </div>
      <JobPanel v-if="importJobId" :job-id="importJobId" @finished="onImportFinished" />
      <div v-if="importJobId" class="text-sm text-color-secondary">
        Once this succeeds, go to <RouterLink to="/channels/new">Define a new channel</RouterLink>,
        pick "Archive import" as the source kind, and select "{{ name }}".
      </div>
    </template>
  </div>
</template>

<style scoped>
.coverage-lane {
  position: relative;
  height: 1.5rem;
  background: var(--surface-100, #f4f4f5);
  border-radius: 4px;
  overflow: hidden;
}
.coverage-range {
  position: absolute;
  top: 0;
  bottom: 0;
  background: var(--primary-color, #6366f1);
  opacity: 0.55;
}
.coverage-range-picker {
  position: absolute;
  top: 0;
  bottom: 0;
  border: 2px dashed var(--red-500, #ef4444);
  box-sizing: border-box;
  pointer-events: none;
}
</style>
