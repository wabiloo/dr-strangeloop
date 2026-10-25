<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Select from 'primevue/select'
import { computed, ref, watch } from 'vue'
import type { ManifestCapture } from '../composables/useManifestCapture'
import { changedLines, lineKind, summarizeManifest, type ManifestEntry } from '../utils/manifestCapture'
import FieldHelp from './FieldHelp.vue'

// The manifests the players above actually polled: HLS master / media playlists and the DASH
// MPD, newest versions kept per playlist. The capture itself lives on the page
// (useManifestCapture) so it also sees what was polled while this panel was collapsed.
const props = defineProps<{ capture: ManifestCapture }>()
const { store, entries, paused } = props.capture

const STORAGE_KEY = 'igor.manifestsPanel.open'
function readOpen(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
const open = ref(readOpen())
function toggle() {
  open.value = !open.value
  try {
    localStorage.setItem(STORAGE_KEY, open.value ? '1' : '0')
  } catch {
    /* non-persistent is fine */
  }
}

const PINNED = '__pinned__'
const tab = ref<string>('')
const selectedId = ref<number | null>(null)
// "Follow" = auto-refresh: the viewer shows the newest version of the tab as polls arrive.
const follow = ref(true)
const showChanges = ref(true)
const wrap = ref(false)
const keepOptions = [3, 5, 10, 20, 50].map((n) => ({ label: `Keep last ${n}`, value: n }))
const keep = ref(10)
watch(keep, (n) => props.capture.setMaxPerSlot(n), { immediate: true })
const skipUnchanged = ref(true)
watch(skipUnchanged, (v) => props.capture.setSkipUnchanged(v), { immediate: true })

const slots = computed(() => {
  void entries.value
  return store.slots()
})
const pinned = computed(() => entries.value.filter((e) => e.pinned))
const tabs = computed(() => [
  ...slots.value.map((s) => ({ key: s.slot, label: `${s.kind === 'mpd' ? 'DASH' : 'HLS'} · ${s.label}` })),
  ...(pinned.value.length ? [{ key: PINNED, label: 'Pinned' }] : []),
])
const versions = computed<ManifestEntry[]>(() => {
  const list = tab.value === PINNED ? pinned.value : entries.value.filter((e) => e.slot === tab.value)
  return [...list].reverse() // newest first
})
const selected = computed(() => entries.value.find((e) => e.id === selectedId.value) ?? null)

// Keep a valid tab; follow the newest version of it.
watch(
  [tabs, versions],
  () => {
    if (!tabs.value.some((t) => t.key === tab.value)) tab.value = tabs.value[0]?.key ?? ''
    if (follow.value || !selected.value) selectedId.value = versions.value[0]?.id ?? null
  },
  { immediate: true },
)
function pickTab(key: string) {
  tab.value = key
  selectedId.value = versions.value[0]?.id ?? null
}
function pick(id: number) {
  selectedId.value = id
  follow.value = id === versions.value[0]?.id // choosing the newest is following it
}
watch(follow, (on) => {
  if (on) selectedId.value = versions.value[0]?.id ?? null
})

const MAX_LINES = 5000
const format = computed(() => (selected.value?.kind === 'mpd' ? 'dash' : 'hls'))
const lines = computed(() => {
  const e = selected.value
  if (!e) return []
  const all = e.body.split(/\r?\n/)
  const changed = showChanges.value ? changedLines(e.body, store.previous(e)?.body) : new Set<number>()
  return all.slice(0, MAX_LINES).map((text, i) => ({ n: i + 1, text, kind: lineKind(text, format.value), changed: changed.has(i) }))
})
const lineCount = computed(() => (selected.value ? selected.value.body.split(/\r?\n/).length : 0))
const hasPrevious = computed(() => (selected.value ? !!store.previous(selected.value) : false))
const unseen = computed(() => (follow.value ? 0 : (versions.value[0]?.id ?? 0) !== selectedId.value ? versions.value.findIndex((e) => e.id === selectedId.value) : 0))

const summary = computed(() => (selected.value ? summarizeManifest(selected.value.body, selected.value.kind) : null))
const fmtStart = (iso: string) => iso.replace('T', ' ').replace(/(\.\d{1,3})\d*Z$/, '$1').replace(/Z$/, '') + ' UTC'
function fmtDuration(s: number): string {
  if (s < 120) return `${Number(s.toFixed(2))} s`
  const m = Math.floor(s / 60)
  return `${m} min ${Math.round(s - m * 60)} s`
}

const time = (iso: string) => iso.slice(11, 23)
const copied = ref('')
async function copy() {
  if (!selected.value) return
  try {
    await navigator.clipboard.writeText(selected.value.body)
    copied.value = 'Copied'
  } catch {
    copied.value = 'Copy failed'
  }
  setTimeout(() => (copied.value = ''), 1500)
}
</script>

<template>
  <section class="manifests-panel surface-card border-round p-3 flex flex-column gap-3" data-testid="manifests-panel">
    <div class="mp-toggle-row flex align-items-center gap-2 flex-wrap" @click="toggle">
      <button type="button" class="mp-toggle flex align-items-center gap-2" :aria-expanded="open" @click.stop="toggle">
        <i :class="['pi', open ? 'pi-chevron-down' : 'pi-chevron-right']" aria-hidden="true" />
        <h3 class="m-0 text-base">Manifests</h3>
      </button>
      <span @click.stop>
        <FieldHelp label="Manifests">
          The HLS playlists and DASH MPD the players above polled, as they received them (nothing is fetched
          again). The newest versions of each are kept; a poll that returns the same text only counts as another
          hit. <em>Follow</em> keeps the viewer on the newest version as polls arrive; click an older version to
          stop following and read it. Pin a version to keep it (pins are never dropped, and are listed together
          under <em>Pinned</em>). Lines new since the previous version are highlighted, and ad / SCTE-35
          signalling (DATERANGE, CUE, EventStream) and discontinuities / Periods stand out.
        </FieldHelp>
      </span>
      <span class="text-sm text-color-secondary">
        {{ entries.length ? `${entries.length} kept${pinned.length ? ` · ${pinned.length} pinned` : ''}` : 'nothing polled yet' }}
      </span>
    </div>

    <div v-show="open" class="flex flex-column gap-3">
      <div class="flex align-items-center gap-3 flex-wrap">
        <div class="flex align-items-center gap-2">
          <Checkbox v-model="follow" binary input-id="mp-follow" />
          <label for="mp-follow">Follow latest</label>
        </div>
        <Select v-model="keep" :options="keepOptions" option-label="label" option-value="value" size="small" aria-label="Versions kept per playlist" />
        <div class="flex align-items-center gap-2">
          <Checkbox v-model="skipUnchanged" binary input-id="mp-skip" />
          <label for="mp-skip">Fold unchanged polls</label>
        </div>
        <div class="flex align-items-center gap-2">
          <Checkbox v-model="showChanges" binary input-id="mp-changes" />
          <label for="mp-changes">Highlight changes</label>
        </div>
        <div class="flex align-items-center gap-2">
          <Checkbox v-model="wrap" binary input-id="mp-wrap" />
          <label for="mp-wrap">Wrap lines</label>
        </div>
        <Button
          :label="paused ? 'Resume capture' : 'Pause capture'"
          :icon="paused ? 'pi pi-play' : 'pi pi-pause'"
          size="small"
          severity="secondary"
          outlined
          @click="paused = !paused"
        />
        <Button label="Clear unpinned" icon="pi pi-trash" size="small" severity="secondary" text :disabled="entries.length === pinned.length" @click="capture.clearUnpinned()" />
      </div>

      <div v-if="!tabs.length" class="text-sm text-color-secondary">
        No manifest seen yet: they appear here as soon as a player loads one.
      </div>

      <template v-else>
        <div class="mp-tabs" role="tablist" aria-label="Playlists">
          <button
            v-for="t in tabs"
            :key="t.key"
            type="button"
            role="tab"
            class="mp-tab"
            :aria-selected="t.key === tab"
            @click="pickTab(t.key)"
          >
            <i v-if="t.key === PINNED" class="pi pi-bookmark-fill text-xs" />
            {{ t.label }}
          </button>
        </div>

        <div class="mp-body">

          <div class="mp-viewer flex flex-column gap-2">
            <template v-if="selected">
              <div class="flex align-items-center gap-2 flex-wrap text-xs text-color-secondary">
                <span class="font-mono mp-url" :title="selected.url">{{ selected.url }}</span>
                <span>polled {{ time(selected.at) }} UTC · HTTP {{ selected.status }} · {{ selected.bytes.toLocaleString() }} bytes · {{ lineCount }} lines<template v-if="selected.truncated"> · truncated</template></span>
                <span v-if="!follow && unseen" class="mp-behind">{{ unseen }} newer — <a href="#" @click.prevent="follow = true">follow latest</a></span>
                <Button :label="copied || 'Copy'" icon="pi pi-copy" size="small" severity="secondary" text class="ml-auto" @click="copy" />
                <Button
                  :label="selected.pinned ? 'Unpin' : 'Pin'"
                  :icon="selected.pinned ? 'pi pi-bookmark-fill' : 'pi pi-bookmark'"
                  size="small"
                  severity="secondary"
                  text
                  @click="capture.togglePin(selected.id)"
                />
              </div>
              <div v-if="summary" class="mp-summary" data-testid="mp-summary">
                <span><strong>{{ summary.segments ?? '?' }}</strong> segments</span>
                <span><strong>{{ summary.durationS != null ? fmtDuration(summary.durationS) : '?' }}</strong> total</span>
                <span>
                  first segment starts
                  <strong v-if="summary.firstStart">{{ fmtStart(summary.firstStart) }}</strong>
                  <span v-else class="text-color-secondary">(unknown: {{ summary.note }})</span>
                </span>
              </div>
              <div class="mp-code" :data-wrap="wrap" role="tabpanel" aria-label="Manifest text">
                <div v-for="l in lines" :key="l.n" class="mp-line" :class="[`mp-${l.kind}`, { 'mp-changed': l.changed }]">
                  <span class="mp-n">{{ l.n }}</span><span class="mp-t">{{ l.text }}</span>
                </div>
                <div v-if="lineCount > lines.length" class="text-xs text-color-secondary p-2">… {{ lineCount - lines.length }} more lines (use Copy for the whole text)</div>
              </div>
              <div v-if="showChanges && !hasPrevious" class="text-xs text-color-secondary">First version kept: nothing to compare with.</div>
            </template>
          </div>
          <ul class="mp-versions" aria-label="Versions">
            <li
              v-for="(e, i) in versions"
              :key="e.id"
              class="mp-version"
              :class="{ 'mp-version-on': e.id === selectedId }"
              @click="pick(e.id)"
            >
              <span class="font-mono text-xs">{{ time(e.at) }}</span>
              <span v-if="i === 0" class="mp-badge">latest</span>
              <span v-if="tab === PINNED" class="text-xs text-color-secondary">{{ e.label }}</span>
              <span v-if="e.hits > 1" class="text-xs text-color-secondary" :title="`Returned the same text ${e.hits} times (last ${time(e.lastAt)})`">×{{ e.hits }}</span>
              <button
                type="button"
                class="mp-pin ml-auto"
                :class="{ 'mp-pin-on': e.pinned }"
                :title="e.pinned ? 'Unpin' : 'Pin this version (kept until unpinned)'"
                :aria-pressed="e.pinned"
                @click.stop="capture.togglePin(e.id)"
              >
                <i :class="e.pinned ? 'pi pi-bookmark-fill' : 'pi pi-bookmark'" />
              </button>
            </li>
          </ul>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.mp-toggle-row {
  cursor: pointer;
}
.mp-toggle {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  color: inherit;
}
.mp-tabs {
  display: flex;
  gap: 0.25rem;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--p-surface-200, #e5e7eb);
}
.mp-tab {
  background: none;
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 0.4rem 0.75rem;
  cursor: pointer;
  color: inherit;
  font: inherit;
}
.mp-tab[aria-selected='true'] {
  border-bottom-color: var(--p-primary-color, #c0392b);
  font-weight: 600;
}
.mp-body {
  display: grid;
  grid-template-columns: 1fr minmax(12rem, 18rem);
  gap: 1rem;
  align-items: start;
}
@media (max-width: 800px) {
  .mp-body {
    grid-template-columns: 1fr;
  }
}
.mp-versions {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 32rem;
  overflow: auto;
  border: 1px solid var(--p-surface-200, #e5e7eb);
  border-radius: 0.375rem;
}
.mp-version {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0.5rem;
  cursor: pointer;
  border-bottom: 1px solid var(--p-surface-100, #f3f4f6);
}
.mp-version:hover {
  background: var(--p-surface-100, #f3f4f6);
}
.mp-version-on {
  background: var(--p-highlight-background, #fde8e8);
}
.mp-badge {
  font-size: 0.65rem;
  padding: 0 0.3rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
}
.mp-pin {
  background: none;
  border: 0;
  cursor: pointer;
  color: var(--p-text-muted-color, #6b7280);
  padding: 0.1rem 0.25rem;
}
.mp-pin-on {
  color: var(--p-primary-color, #c0392b);
}
.mp-viewer {
  min-width: 0;
}
.mp-url {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mp-behind {
  color: var(--p-primary-color, #c0392b);
}
.mp-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 1.5rem;
  font-size: 0.875rem;
}
.mp-code {
  max-height: 32rem;
  overflow: auto;
  border: 1px solid var(--p-surface-200, #e5e7eb);
  border-radius: 0.375rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.75rem;
  line-height: 1.5;
}
.mp-line {
  display: flex;
  min-width: max-content;
}
.mp-code[data-wrap='true'] .mp-line {
  min-width: 0;
}
.mp-code[data-wrap='true'] .mp-t {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.mp-n {
  flex: none;
  width: 3.5rem;
  padding-right: 0.5rem;
  text-align: right;
  color: var(--p-text-muted-color, #9ca3af);
  user-select: none;
}
.mp-t {
  white-space: pre;
}
.mp-tag .mp-t {
  color: var(--p-text-muted-color, #4b5563);
}
.mp-uri .mp-t {
  color: var(--p-text-color, #111827);
}
.mp-marker {
  background: rgba(234, 179, 8, 0.18);
}
.mp-discontinuity {
  background: rgba(99, 102, 241, 0.14);
}
.mp-changed .mp-n {
  border-right: 3px solid #16a34a;
  margin-right: 0;
}
.mp-changed .mp-t {
  background: rgba(22, 163, 74, 0.15);
}
</style>
