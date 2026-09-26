<script setup lang="ts">
import Button from 'primevue/button'
import Message from 'primevue/message'
import RadioButton from 'primevue/radiobutton'
import Slider from 'primevue/slider'
import Tag from 'primevue/tag'
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { getArchiveCoverage, importArchive, saveArchiveSelection } from '../api/client'
import JobPanel from '../components/JobPanel.vue'
import type { ArchiveCoverage, Job, RangeSuggestion, VariantCoverage } from '../api/types'

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
    // Skip variants with no coverage (e.g. a multivariant playlist, which has no segments of its own).
    const usable = coverage.value.variants.find((v) => v.covered_ranges.length > 0)
    if (usable) selectedVariantUrl.value = usable.manifest_url
    restoreSelection()
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
// A smart suggestion (or restored selection) pins the EXACT window here --
// the slider's 0.5% steps can't hit segment boundaries precisely. Moving the
// slider by hand clears it.
const exactRange = ref<[Date, Date] | null>(null)
const selectedSuggestionId = ref<string | null>(null)
const targetRange = computed<[Date, Date]>(
  () =>
    exactRange.value ?? [
      new Date(sessionStartMs.value + (rangePercent.value[0] / 100) * sessionDurationMs.value),
      new Date(sessionStartMs.value + (rangePercent.value[1] / 100) * sessionDurationMs.value),
    ],
)

function setRange(start: Date, end: Date) {
  exactRange.value = [start, end]
  rangePercent.value = [
    ((start.getTime() - sessionStartMs.value) / sessionDurationMs.value) * 100,
    ((end.getTime() - sessionStartMs.value) / sessionDurationMs.value) * 100,
  ]
}

function onSliderChange() {
  exactRange.value = null
  selectedSuggestionId.value = null
  persistSelection()
}

function applySuggestion(sug: RangeSuggestion) {
  setRange(new Date(sug.start), new Date(sug.end))
  selectedSuggestionId.value = sug.id
  selectedVariantUrl.value = sug.manifest_url
  persistSelection()
}

// ── Persistence: the choice is saved server-side (data/archives/<name>.selection.json)
// so reloading the page restores it. ────────────────────────────────────────
let saveTimer: ReturnType<typeof setTimeout> | undefined
function persistSelection() {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    const [start, end] = targetRange.value
    saveArchiveSelection(props.name, {
      manifest_url: selectedVariantUrl.value,
      start: start.toISOString(),
      end: end.toISOString(),
      suggestion_id: selectedSuggestionId.value,
    }).catch(() => {
      /* best-effort: a failed save must never block the wizard */
    })
  }, 300)
}

function restoreSelection() {
  const sel = coverage.value?.selection
  if (!sel || !sel.start || !sel.end) return
  setRange(new Date(sel.start), new Date(sel.end))
  selectedSuggestionId.value = sel.suggestion_id
  if (sel.manifest_url && coverage.value?.variants.some((v) => v.manifest_url === sel.manifest_url)) {
    selectedVariantUrl.value = sel.manifest_url
  }
}

// Media stats of the selected variant within the target range.
const rangeStats = computed(() => {
  const variant = coverage.value?.variants.find((v) => v.manifest_url === selectedVariantUrl.value)
  if (!variant || variant.segments.length === 0) return null
  const [start, end] = targetRange.value
  const tol = 50
  const inside = variant.segments.filter(
    (sg) => Date.parse(sg.start) >= start.getTime() - tol && Date.parse(sg.end) <= end.getTime() + tol,
  )
  return { total: inside.length, withMedia: inside.filter((sg) => sg.has_media).length }
})

function fmtDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  return m > 0 ? `${m}m ${Math.round(seconds % 60)}s` : `${Math.round(seconds)}s`
}

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
    if (rStart > cursor + 50) break
    if (rEnd > cursor) cursor = rEnd
    if (cursor >= targetEnd.getTime() - 50) return true
  }
  return cursor >= targetEnd.getTime() - 50
}

const survivors = computed<VariantCoverage[]>(() => {
  const [start, end] = targetRange.value
  return (coverage.value?.variants ?? []).filter((v) => coversFully(v.covered_ranges, start, end))
})
const survivorUrls = computed(() => new Set(survivors.value.map((v) => v.manifest_url)))
const dropped = computed<VariantCoverage[]>(() =>
  (coverage.value?.variants ?? []).filter((v) => !survivorUrls.value.has(v.manifest_url)),
)

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
    persistSelection()
    const [start, end] = targetRange.value
    const job = await importArchive(
      props.name,
      variant.manifest_url,
      variant.format,
      variant.format === 'HLS' ? { start: start.toISOString(), end: end.toISOString() } : undefined,
    )
    importJobId.value = job.id
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = false
  }
}

function onImportFinished(job: Job) {
  // On success, stay here (the JobPanel shows the result); no redirect.
  if (job.status !== 'succeeded') importError.value = 'Import failed -- see log above.'
  else importError.value = ''

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
        stretches a live player wasn't using this variant (SCOPE.md §8). Green segments have their media in the archive; orange ones are listed in the manifest but their
        media isn't in the archive.
      </p>
      <div class="flex flex-column gap-2">
        <div
          v-for="variant in coverage.variants"
          :key="variant.manifest_url"
          class="variant-lane flex flex-column gap-1"
          :class="{ dimmed: !survivorUrls.has(variant.manifest_url) }"
        >
          <div class="text-xs text-color-secondary" style="word-break: break-all">
            <Tag :value="variant.format" severity="secondary" class="mr-2" />{{ variant.manifest_url }}
          </div>
          <div class="coverage-lane">
            <template v-if="variant.segments.length > 0">
              <div
                v-for="(sg, i) in variant.segments"
                :key="i"
                class="coverage-range"
                :class="{ missing: !sg.has_media }"
                :style="rangeStyle(sg)"
              />
            </template>
            <template v-else>
              <div v-for="(range, i) in variant.covered_ranges" :key="i" class="coverage-range" :style="rangeStyle(range)" />
            </template>
            <div v-if="survivorUrls.has(variant.manifest_url)" class="coverage-range-picker" :style="{ left: `${rangePercent[0]}%`, width: `${rangePercent[1] - rangePercent[0]}%` }" />
          </div>
        </div>
      </div>

      <template v-if="coverage.multivariants.length > 0">
        <h4 class="mb-0 mt-2">Multivariant playlist renditions (info only)</h4>
        <div v-for="multivariant in coverage.multivariants" :key="multivariant.manifest_url" class="flex flex-column gap-1">
          <div class="text-xs text-color-secondary" style="word-break: break-all">{{ multivariant.manifest_url }}</div>
          <div v-for="r in multivariant.renditions" :key="r.manifest_url" class="text-xs" style="word-break: break-all">
            {{ r.resolution ?? 'audio/other' }} &middot; {{ r.bandwidth ? Math.round(r.bandwidth / 1000) + ' kbps' : '?' }}
            <span v-if="r.frame_rate"> &middot; {{ r.frame_rate }} fps</span>
            <span v-if="r.codecs"> &middot; {{ r.codecs }}</span>
            &middot; {{ r.manifest_url }}
          </div>
        </div>
      </template>

      <h4 class="mb-0 mt-2">2. Pick a target range</h4>
      <div v-if="coverage.suggestions.length > 0" class="flex flex-wrap gap-2">
        <button
          v-for="sug in coverage.suggestions"
          :key="sug.id"
          type="button"
          class="suggestion"
          :class="{ active: selectedSuggestionId === sug.id }"
          @click="applySuggestion(sug)"
        >
          <strong>{{ sug.title }}</strong>
          <span class="text-sm">{{ fmtDuration(sug.duration_seconds) }}</span>
          <span class="text-xs" :class="sug.segments_with_media === sug.segments_total ? 'text-green-600' : 'text-orange-600'">
            media {{ sug.segments_with_media }}/{{ sug.segments_total }} segments
          </span>
          <span class="text-xs text-color-secondary">{{ sug.survivor_count }}/{{ sug.variant_count }} variants cover it</span>
          <span class="text-xs text-color-secondary">{{ sug.description }}</span>
        </button>
      </div>
      <Slider v-model="rangePercent" range :min="0" :max="100" :step="0.5" @slideend="onSliderChange" />
      <div class="text-sm text-color-secondary">
        {{ targetRange[0].toISOString() }} &rarr; {{ targetRange[1].toISOString() }}
      </div>

      <h4 class="mb-0 mt-2">3. Variants covering this range</h4>
      <div class="flex flex-column gap-2">
        <div v-for="variant in survivors" :key="variant.manifest_url" class="flex align-items-center gap-2">
          <RadioButton v-model="selectedVariantUrl" :input-id="variant.manifest_url" :value="variant.manifest_url" name="reference-variant" @update:model-value="persistSelection" />
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
        The loop is trimmed to the segments lying fully inside the range above, taken from the selected variant
        (HLS; a DASH import uses the full span).
      </p>
      <Message v-if="rangeStats" :severity="rangeStats.withMedia === rangeStats.total ? 'success' : 'warn'" :closable="false">
        {{ rangeStats.total }} segments in range, {{ rangeStats.withMedia }} with media in the archive<template
          v-if="rangeStats.withMedia < rangeStats.total"
        >
          -- {{ rangeStats.total - rangeStats.withMedia }} missing (enable "allow missing segments" when defining the channel).</template
        ><template v-else> -- fully recoverable.</template>
      </Message>
      <Message v-if="importError" severity="error">{{ importError }}</Message>
      <div>
        <Button
          label="Import"
          icon="pi pi-download"
          :loading="importing"
          :disabled="!selectedVariantUrl || !survivors.some((v) => v.manifest_url === selectedVariantUrl)"
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
/* Segment colours: green = media in the archive, orange = listed but no media. */
.coverage-range {
  position: absolute;
  top: 0;
  bottom: 0;
  background: #4ade80;
}
.coverage-range.missing {
  background: #fb923c;
}
.suggestion {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  align-items: flex-start;
  text-align: left;
  padding: 0.6rem 0.8rem;
  min-width: 14rem;
  flex: 1;
  border: 1px solid var(--surface-300, #d4d4d8);
  border-radius: 6px;
  background: var(--surface-0, #fff);
  color: inherit;
  cursor: pointer;
}
.suggestion.active {
  border-color: var(--primary-color, #6366f1);
  box-shadow: 0 0 0 1px var(--primary-color, #6366f1);
}
/* Variants that don't fully cover the picked range: dimmed, base colours kept. */
.variant-lane.dimmed {
  opacity: 0.35;
}
/* Range selection: blue outline, distinct from the green/orange segments. */
.coverage-range-picker {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 1;
  border: 3px solid #2563eb;
  box-sizing: border-box;
  pointer-events: none;
}
</style>
