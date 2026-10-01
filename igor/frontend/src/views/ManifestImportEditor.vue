<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  getManifestImportStatus,
  getManifestSource,
  importManifest,
  inspectManifest,
  renameManifestSource,
} from '../api/client'
import ManifestPreviewDialog from '../components/ManifestPreviewDialog.vue'
import JobPanel from '../components/JobPanel.vue'
import type { Job, ManifestImportStatus, ManifestInspection, ManifestListItem } from '../api/types'

const props = defineProps<{ name: string }>()

const source = ref<ManifestListItem | null>(null)
const importStatus = ref<ManifestImportStatus | null>(null)
const inspection = ref<ManifestInspection | null>(null)
const previewUrl = ref<string | null>(null)
const loading = ref(true)
const inspecting = ref(false)
const error = ref('')
const inspectError = ref('')

const displayName = ref('')
const savingDisplayName = ref(false)
const displayNameError = ref('')

// Which renditions to keep (by ranked position), and the import options.
const selectedPositions = ref<number[]>([])
const includeAudio = ref(true)
const allowMissing = ref(false)

const importing = ref(false)
const importError = ref('')
const importJobId = ref<string | null>(null)

async function inspect() {
  if (!source.value) return
  inspecting.value = true
  inspectError.value = ''
  try {
    inspection.value = await inspectManifest(source.value.manifest_url)
    const previous = source.value.import_options?.renditions
    const all = inspection.value.renditions.map((r) => r.position)
    selectedPositions.value = restoreSelection(previous, all)
  } catch (e) {
    inspection.value = null
    inspectError.value = e instanceof Error ? e.message : String(e)
  } finally {
    inspecting.value = false
  }
}

/** Turn a previously used `renditions` option ('all' | 'best' | '#1,#3') back into positions. */
function restoreSelection(previous: string | undefined, all: number[]): number[] {
  if (!previous || previous === 'all') return all
  if (previous === 'best') return all.slice(0, 1)
  const wanted = previous.split(',').map((t) => Number(t.replace('#', '')))
  const kept = all.filter((p) => wanted.includes(p))
  return kept.length > 0 ? kept : all
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    source.value = await getManifestSource(props.name)
    displayName.value = source.value.display_name || props.name
    importStatus.value = source.value.import
    if (source.value.import_options) {
      includeAudio.value = source.value.import_options.audio
      allowMissing.value = source.value.import_options.allow_missing_segments
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    loading.value = false
    return
  }
  loading.value = false
  await inspect()
}
onMounted(load)

async function saveDisplayName() {
  if (!displayName.value.trim()) return
  savingDisplayName.value = true
  displayNameError.value = ''
  try {
    const result = await renameManifestSource(props.name, displayName.value.trim())
    displayName.value = result.display_name
  } catch (e) {
    displayNameError.value = e instanceof Error ? e.message : String(e)
  } finally {
    savingDisplayName.value = false
  }
}

function formatBandwidth(bps: number | null): string {
  if (!bps) return ''
  return bps >= 1_000_000 ? `${(bps / 1_000_000).toFixed(1)} Mbps` : `${Math.round(bps / 1000)} kbps`
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.round(seconds % 60)
  return h > 0 ? `${h}h ${m}m ${s}s` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

const canImport = computed(
  () => inspection.value?.is_vod === true && selectedPositions.value.length > 0 && !importing.value,
)

const renditionsOption = computed(() => {
  const all = inspection.value?.renditions.length ?? 0
  const picked = [...selectedPositions.value].sort((a, b) => a - b)
  return picked.length === all ? 'all' : picked.map((p) => `#${p}`).join(',')
})

async function startImport() {
  importing.value = true
  importError.value = ''
  try {
    const job: Job = await importManifest(props.name, {
      renditions: renditionsOption.value,
      audio: includeAudio.value,
      allow_missing_segments: allowMissing.value,
    })
    importJobId.value = job.id
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = false
  }
}

async function onImportFinished(job: Job) {
  if (job.status !== 'succeeded') return
  importStatus.value = await getManifestImportStatus(props.name)
}
</script>

<template>
  <div class="flex flex-column gap-3">
    <Message v-if="error" severity="error">{{ error }}</Message>

    <template v-if="source">
      <div class="flex flex-column gap-1">
        <label for="manifest-display-name" class="font-semibold">Name</label>
        <div class="flex gap-2 manifest-display-name-field">
          <InputText id="manifest-display-name" v-model="displayName" class="flex-1" @keyup.enter="saveDisplayName" />
          <Button
            label="Save"
            severity="secondary"
            :loading="savingDisplayName"
            :disabled="!displayName.trim() || displayName.trim() === source.display_name"
            @click="saveDisplayName"
          />
        </div>
        <small v-if="displayNameError" class="p-error">{{ displayNameError }}</small>
        <div class="flex align-items-center gap-2">
          <code class="text-sm text-color-secondary manifest-source-url">{{ source.manifest_url }}</code>
          <Button
            icon="pi pi-play-circle"
            severity="secondary"
            outlined
            title="Preview manifest"
            aria-label="Preview manifest"
            @click="previewUrl = source.manifest_url"
          />
        </div>
      </div>

      <h4 class="mb-0 mt-2">1. Renditions</h4>
      <div v-if="inspecting" class="text-color-secondary">Reading the manifest...</div>
      <Message v-if="inspectError" severity="error">{{ inspectError }}</Message>
      <div v-if="!inspecting && (inspectError || !inspection)">
        <Button label="Retry" icon="pi pi-refresh" severity="secondary" outlined @click="inspect" />
      </div>

      <template v-if="inspection">
        <div class="flex align-items-center gap-2">
          <Tag :value="inspection.format.toUpperCase()" severity="secondary" />
          <Tag v-if="inspection.is_vod" value="VOD" severity="success" />
          <span class="text-sm text-color-secondary">
            {{ inspection.renditions.length }} rendition{{ inspection.renditions.length === 1 ? '' : 's' }}
            <template v-if="inspection.audio">, separate audio track</template>
          </span>
        </div>
        <Message v-if="!inspection.is_vod" severity="warn" :closable="false">
          This is a live or event manifest, not a VOD, so it can't be downloaded as a loop. Capture it and use
          <RouterLink to="/archives">Archives</RouterLink> instead.
        </Message>

        <div class="flex flex-column gap-2">
          <label
            v-for="r in inspection.renditions"
            :key="r.position"
            :for="`rendition-${r.position}`"
            class="rendition-row"
            :class="{ 'rendition-row-selected': selectedPositions.includes(r.position) }"
          >
            <Checkbox
              v-model="selectedPositions"
              :value="r.position"
              :input-id="`rendition-${r.position}`"
              :disabled="!inspection.is_vod"
            />
            <strong>{{ r.resolution || r.name }}</strong>
            <span class="text-color-secondary">{{ formatBandwidth(r.bandwidth) }}</span>
            <span v-if="r.frame_rate" class="text-color-secondary">{{ r.frame_rate }} fps</span>
            <code v-if="r.codecs" class="text-xs text-color-secondary">{{ r.codecs }}</code>
          </label>
        </div>
        <small class="text-color-secondary">
          The highest-bandwidth selected rendition sets the loop's timing and ad markers. Renditions must
          share the same segment boundaries, or the import fails.
        </small>

        <h4 class="mb-0 mt-2">2. Options</h4>
        <div class="flex flex-column gap-2">
          <div v-if="inspection.audio" class="flex align-items-center gap-2">
            <Checkbox v-model="includeAudio" binary input-id="manifest-audio" />
            <label for="manifest-audio">Include the separate audio track</label>
          </div>
          <div class="flex align-items-center gap-2">
            <Checkbox v-model="allowMissing" binary input-id="manifest-allow-missing" />
            <label for="manifest-allow-missing">Skip segments that fail to download instead of failing the import</label>
          </div>
        </div>

        <h4 class="mb-0 mt-2">3. Import</h4>
        <p class="text-color-secondary text-sm m-0">
          Downloads every segment of the selected renditions (this can be large and take a while).
        </p>
        <Message v-if="importError" severity="error">{{ importError }}</Message>
        <div>
          <Button
            :label="importStatus?.exists ? 'Re-import' : 'Import'"
            icon="pi pi-download"
            :loading="importing"
            :disabled="!canImport"
            @click="startImport"
          />
        </div>
      </template>

      <JobPanel v-if="importJobId" :job-id="importJobId" @finished="onImportFinished" />

      <Message v-if="importStatus?.exists && importStatus.summary" severity="success" :closable="false">
        Imported: {{ importStatus.summary.segments }} segments ({{ formatDuration(importStatus.summary.duration_seconds) }}),
        {{ importStatus.summary.renditions.map((r) => r.resolution || r.name).join(', ') }},
        {{ importStatus.summary.markers }} marker{{ importStatus.summary.markers === 1 ? '' : 's' }}<template
          v-if="importStatus.summary.audio"
          >, separate audio</template
        >.
      </Message>
      <div v-if="importStatus?.exists" class="text-sm text-color-secondary">
        Next, <RouterLink to="/channels/new">define a new channel</RouterLink>, pick "Manifest import" as the
        source kind, and select "{{ name }}".
      </div>
    </template>
    <ManifestPreviewDialog
      :url="previewUrl"
      :format="inspection ? (inspection.format.toLowerCase() as 'hls' | 'dash') : null"
      @close="previewUrl = null"
    />
  </div>
</template>

<style scoped>
.manifest-display-name-field {
  width: min(100%, 32rem);
}
.manifest-source-url {
  word-break: break-all;
}
.rendition-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.6rem 0.8rem;
  border: 1px solid var(--p-surface-300);
  border-radius: var(--p-border-radius);
  cursor: pointer;
}
.rendition-row-selected {
  border-color: var(--p-primary-color);
}
</style>
