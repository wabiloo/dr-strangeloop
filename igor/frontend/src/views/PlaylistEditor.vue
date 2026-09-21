<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Divider from 'primevue/divider'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Popover from 'primevue/popover'
import Select from 'primevue/select'
import Tab from 'primevue/tab'
import TabList from 'primevue/tablist'
import TabPanel from 'primevue/tabpanel'
import TabPanels from 'primevue/tabpanels'
import Tabs from 'primevue/tabs'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { buildPlaylist, getPlaylist, resolveMarkers, savePlaylist } from '../api/client'
import type { Job, ResolvedMarker } from '../api/types'
import AssetTimeline from '../components/AssetTimeline.vue'
import AssetFileField from '../components/AssetFileField.vue'
import JobPanel from '../components/JobPanel.vue'
import {
  SEGMENTATION_PAIR_OPTIONS,
  UPID_TYPE_OPTIONS,
  colorForLaneKey,
  hexToText,
  laneKeyForMarker,
  laneLabelForMarker,
  textToHex,
} from '../segmentationPresets'
import { isInstantMarker, layoutMarkers, nextEventId, orderForDisplay } from '../markerLayout'

const props = defineProps<{ name: string | null }>()
const router = useRouter()
const toast = useToast()

const nameInput = ref(props.name ?? '')
const outputMode = ref<'single' | 'ladder'>('single')
const loading = ref(false)
const saving = ref(false)
const error = ref('')

interface RenditionForm {
  name: string
  resolution: string
  bitrate_kbps: number
}

interface SegmentationForm {
  type_id: string
  upid_type: string
  upid_hex: string
  web_delivery_allowed: boolean
  no_regional_blackout: boolean
  archive_allowed: boolean
  device_restrictions: number
}

interface AssetForm {
  id: string
  file: string
  start: string
  duration: string
  countdown: string
  fade_in: string
  fade_out: string
  slate_image: string
}

interface MarkerForm {
  event_id: number
  splice_type: 'splice_insert' | 'time_signal'
  assets: string[]
  segmentation: SegmentationForm
  /** `splice_insert` only: true (default) emits a single self-contained
   * message with no cue-in (SCTE-35 auto_return); false emits an explicit
   * cue-out/cue-in pair. Ignored for `time_signal`. */
  auto_return: boolean
}

/** A marker being created or edited, not yet committed to `form.markers`.
 * `editingEventId` is the event_id of the existing committed marker being
 * replaced on commit, or `null` for a brand-new marker (from "Add marker"). */
interface MarkerDraft extends MarkerForm {
  editingEventId: number | null
}

function newSegmentation(): SegmentationForm {
  return {
    type_id: '', // deliberately unset -- see MarkerDraft/tagRange: no default
                 // segmentation type is pre-selected, so nothing looks
                 // already-chosen before the user actually picks one.
    upid_type: '0x09',
    upid_hex: '',
    web_delivery_allowed: true,
    no_regional_blackout: false,
    archive_allowed: false,
    device_restrictions: 1,
  }
}

let assetIdCounter = 1
function generateAssetId(): string {
  return `asset-${assetIdCounter++}`
}

function newAsset(): AssetForm {
  return {
    id: generateAssetId(),
    file: '',
    start: '',
    duration: '',
    countdown: '',
    fade_in: '',
    fade_out: '',
    slate_image: '',
  }
}

function defaultOutput() {
  return {
    file: '../outputs/output.ts',
    dir: '../outputs/mychannel',
    resolution: '1920x1080',
    framerate: 25,
    bitrate_kbps: 10000,
    gop: null as number | null,
    service_provider: 'broadpeak',
    service_name: 'broadpeak.io',
  }
}

const form = reactive({
  output: defaultOutput(),
  renditions: [] as RenditionForm[],
  normalize: false,
  slate_image: '',
  assets: [] as AssetForm[],
  markers: [] as MarkerForm[],
})

/** Resets the form to its blank-new-playlist state -- needed because vue-router
 * reuses this component instance when navigating between /playlists/new and
 * /playlists/:name, so mount-time initializers alone aren't enough. */
function resetForm() {
  outputMode.value = 'single'
  Object.assign(form.output, defaultOutput())
  form.renditions = []
  form.normalize = false
  form.slate_image = ''
  form.assets = []
  form.markers = []
  assetIdCounter = 1
  markerDraft.value = null
  selectedMarkerEventId.value = null
}

const isNew = computed(() => props.name === null)
const activeTab = ref('settings')
const selectedAssetIndex = ref<number | null>(null)
const selectedMarkerEventId = ref<number | null>(null)

// ── Hover cross-highlight between the marker list and the timeline graph ──
// `hoverSource` records which side the CURRENT hover came from, so hovering
// a list row scrolls the graph to it (the graph can be zoomed/scrolled out
// of view), while hovering a graph span only highlights the list row
// without scrolling the list (scrolling the list on hover was annoying).
const hoveredMarkerEventId = ref<number | null>(null)
const hoverSource = ref<'list' | 'graph' | null>(null)

function hoverMarkerFromList(eventId: number | null) {
  hoveredMarkerEventId.value = eventId
  hoverSource.value = eventId === null ? null : 'list'
}
function hoverMarkerFromGraph(eventId: number | null) {
  hoveredMarkerEventId.value = eventId
  hoverSource.value = eventId === null ? null : 'graph'
}

/** What the graph should scroll to: the list-originated hover if there is
 * one, else whatever's currently selected (click, from either side). */
const graphScrollTarget = computed(() =>
  hoverSource.value === 'list' ? hoveredMarkerEventId.value : selectedMarkerEventId.value,
)

function selectAsset(i: number) {
  selectedAssetIndex.value = i
  selectedMarkerEventId.value = null
  markerDraft.value = null
}

function addAsset() {
  form.assets.push(newAsset())
  selectAsset(form.assets.length - 1)
}

function removeAsset(i: number) {
  const removedId = form.assets[i]?.id
  form.assets.splice(i, 1)
  // Any marker referencing the removed asset would fail server-side
  // validation anyway (dangling asset id) -- drop the reference so the
  // form doesn't silently keep an invalid marker.
  form.markers.forEach((m) => {
    m.assets = m.assets.filter((id) => id !== removedId)
  })
  form.markers = form.markers.filter((m) => m.assets.length > 0)
  if (markerDraft.value?.assets.includes(removedId)) {
    markerDraft.value = null
    selectedMarkerEventId.value = null
  }
  if (selectedAssetIndex.value === null) return
  if (form.assets.length === 0) selectedAssetIndex.value = null
  else if (selectedAssetIndex.value >= form.assets.length) selectedAssetIndex.value = form.assets.length - 1
}
function addRendition() {
  form.renditions.push({ name: '', resolution: '1280x720', bitrate_kbps: 4500 })
}
function removeRendition(i: number) {
  form.renditions.splice(i, 1)
}

// ── Markers (nested, multi-asset SCTE-35 spans) ────────────────────────────

const usedEventIds = computed(() => form.markers.map((m) => m.event_id))

const resolvedMarkers = ref<ResolvedMarker[] | null>(null)
const resolvedAssetDurations = ref<Record<string, number> | null>(null)
const resolvingMarkers = ref(false)

function tagRange({ startIndex, endIndex }: { startIndex: number; endIndex: number }) {
  const assetIds = form.assets.slice(startIndex, endIndex + 1).map((a) => a.id)
  markerDraft.value = {
    editingEventId: null,
    event_id: nextEventId(usedEventIds.value),
    splice_type: 'time_signal',
    assets: assetIds,
    segmentation: newSegmentation(),
    auto_return: true,
  }
  selectedMarkerEventId.value = markerDraft.value.event_id
  selectedAssetIndex.value = null
}

function editMarker(eventId: number) {
  const existing = form.markers.find((m) => m.event_id === eventId)
  if (!existing) return
  markerDraft.value = {
    editingEventId: existing.event_id,
    event_id: existing.event_id,
    splice_type: existing.splice_type,
    assets: [...existing.assets],
    segmentation: { ...existing.segmentation },
    auto_return: existing.auto_return,
  }
  selectedMarkerEventId.value = eventId
  selectedAssetIndex.value = null
}

function removeMarker(eventId: number) {
  form.markers = form.markers.filter((m) => m.event_id !== eventId)
  if (markerDraft.value?.editingEventId === eventId) markerDraft.value = null
  if (selectedMarkerEventId.value === eventId) selectedMarkerEventId.value = null
}

const markerDraft = ref<MarkerDraft | null>(null)

/** Whether the draft's chosen segmentation type has no defined End partner
 * (see markerLayout.isInstantMarker) -- drives the "multiply into one
 * marker per asset" behavior on commit, since an instant signal is a
 * single point in time, not a range. */
const isDraftInstant = computed(() => {
  if (!markerDraft.value) return false
  return isInstantMarker(markerDraft.value)
})

/** How many markers committing the current draft will actually produce:
 * 1 for a Start/End pair (spans the whole selected range as one marker),
 * or one per selected asset for an instant signal. */
const draftPendingCount = computed(() => {
  if (!markerDraft.value) return 0
  return isDraftInstant.value ? markerDraft.value.assets.length : 1
})

const canCommitDraft = computed(() => {
  if (!markerDraft.value) return false
  if (markerDraft.value.splice_type === 'time_signal' && !markerDraft.value.segmentation.type_id) return false
  return true
})

const commitButtonLabel = computed(() => {
  if (!markerDraft.value) return ''
  if (markerDraft.value.editingEventId !== null) return 'Update'
  const n = draftPendingCount.value
  return n > 1 ? `Add ${n} markers` : 'Add marker'
})

function cancelDraft() {
  markerDraft.value = null
  selectedMarkerEventId.value = null
}

function commitDraft() {
  const d = markerDraft.value
  if (!d || !canCommitDraft.value) return

  if (d.editingEventId !== null) {
    form.markers = form.markers.filter((m) => m.event_id !== d.editingEventId)
  }

  if (isDraftInstant.value && d.assets.length > 1) {
    // Instant signal spanning multiple assets: one marker per asset --
    // the first reuses the draft's own event_id, the rest get freshly
    // allocated ones (tracking allocations made within this same commit,
    // since usedEventIds only reflects form.markers as of before this call).
    const usedSoFar = [...usedEventIds.value]
    const newMarkers: MarkerForm[] = d.assets.map((assetId, idx) => {
      const eventId = idx === 0 ? d.event_id : nextEventId(usedSoFar)
      usedSoFar.push(eventId)
      return {
        event_id: eventId,
        splice_type: d.splice_type,
        assets: [assetId],
        segmentation: { ...d.segmentation },
        auto_return: d.auto_return,
      }
    })
    form.markers.push(...newMarkers)
  } else {
    form.markers.push({
      event_id: d.event_id,
      splice_type: d.splice_type,
      assets: [...d.assets],
      segmentation: { ...d.segmentation },
      auto_return: d.auto_return,
    })
  }

  markerDraft.value = null
  selectedMarkerEventId.value = null
}

function laneColorForBadge(marker: MarkerForm | null): string {
  if (!marker) return colorForLaneKey('')
  return colorForLaneKey(laneKeyForMarker(marker))
}

/** Client-side containment layout (depths/segment_num), same algorithm as
 * the backend, just for display -- not authoritative (see markerLayout.ts). */
const markerLayout = computed(() => {
  const idToIndex = new Map(form.assets.map((a, i) => [a.id, i] as const))
  return layoutMarkers(form.markers, idToIndex)
})

/** The "Markers" list below the timeline shows nesting via indentation
 * only (no explicit parent lines), so it needs a real depth-first order --
 * plain authoring order can put an unrelated sibling between a parent and
 * its children (see orderForDisplay's docstring). */
const markerListOrder = computed(() => orderForDisplay(markerLayout.value))

async function previewMarkerResolution() {
  if (!nameInput.value.trim()) {
    toast.add({ severity: 'warn', summary: 'Name the playlist first', life: 3000 })
    return
  }
  resolvingMarkers.value = true
  try {
    const result = await resolveMarkers(nameInput.value.trim(), toYamlPlaylist())
    resolvedMarkers.value = result.markers
    resolvedAssetDurations.value = result.asset_durations

    // Persist real durations back into the form (and thus into the YAML on
    // save) for any asset that doesn't already specify one -- e.g. ads with
    // no `duration:` set (meaning "use the whole file") -- so the timeline
    // renders at true widths on next load without needing to re-resolve,
    // and only ever fills in a blank, never overrides a deliberate trim.
    let filledCount = 0
    for (const asset of form.assets) {
      if (asset.duration.trim()) continue
      const seconds = result.asset_durations[asset.id]
      if (seconds === undefined) continue
      asset.duration = `${seconds.toFixed(2)}s`
      filledCount++
    }

    result.warnings.forEach((w) => toast.add({ severity: 'warn', summary: w, life: 5000 }))
    toast.add({
      severity: 'success',
      summary: filledCount
        ? `Resolved -- filled in duration for ${filledCount} asset(s)`
        : 'Markers + durations resolved',
      life: 3000,
    })
  } catch (e) {
    toast.add({ severity: 'error', summary: e instanceof Error ? e.message : String(e), life: 6000 })
  } finally {
    resolvingMarkers.value = false
  }
}

function resolvedSpanFor(eventId: number): ResolvedMarker | undefined {
  return resolvedMarkers.value?.find((m) => m.event_id === eventId)
}

/** Live preview of the draft's containment (segment/depth), computed
 * against form.markers with the draft substituted in for whatever it's
 * replacing (or added fresh) -- lets the "Auto-computed" info in the
 * detail panel update as you edit, even before committing. Not used to
 * feed the graph/list (those only ever show committed markers). */
const draftPreviewSpan = computed(() => {
  const d = markerDraft.value
  if (!d) return null
  const withoutEditing = form.markers.filter((m) => m.event_id !== d.editingEventId)
  const draftAsMarker: MarkerForm = {
    event_id: d.event_id,
    splice_type: d.splice_type,
    assets: isDraftInstant.value ? d.assets.slice(0, 1) : [...d.assets],
    segmentation: { ...d.segmentation },
    auto_return: d.auto_return,
  }
  const idToIndex = new Map(form.assets.map((a, i) => [a.id, i] as const))
  const preview = layoutMarkers([...withoutEditing, draftAsMarker], idToIndex)
  return preview.find((s) => s.marker.event_id === d.event_id) ?? null
})

const draftResolved = computed(() => {
  if (!markerDraft.value || markerDraft.value.editingEventId === null) return undefined
  return resolvedSpanFor(markerDraft.value.editingEventId)
})

function timeOrUndefined(v: string): string | number | undefined {
  if (!v.trim()) return undefined
  const n = Number(v)
  return Number.isFinite(n) && v.trim() === String(n) ? n : v
}

/** Converts the form's local shape into the franken-ts YAML shape
 * (franken_ts.config.Config) -- server re-validates against the real
 * Pydantic model on save regardless. */
function toYamlPlaylist(): Record<string, unknown> {
  const output: Record<string, unknown> = {
    framerate: form.output.framerate,
    bitrate_kbps: form.output.bitrate_kbps,
    service_provider: form.output.service_provider,
    service_name: form.output.service_name,
  }
  if (form.output.gop) output.gop = form.output.gop
  if (outputMode.value === 'single') {
    output.file = form.output.file
    output.resolution = form.output.resolution
  } else {
    output.dir = form.output.dir
    output.renditions = form.renditions.map((r) => ({ ...r }))
  }

  const assets = form.assets
    .filter((a) => a.file.trim())
    .map((a) => {
      const asset: Record<string, unknown> = { file: a.file }
      if (a.id.trim()) asset.id = a.id
      if (a.start.trim()) asset.start = timeOrUndefined(a.start)
      if (a.duration.trim()) asset.duration = timeOrUndefined(a.duration)
      if (a.countdown.trim()) asset.countdown = timeOrUndefined(a.countdown)
      if (a.fade_in.trim()) asset.fade_in = timeOrUndefined(a.fade_in)
      if (a.fade_out.trim()) asset.fade_out = timeOrUndefined(a.fade_out)
      if (a.slate_image.trim()) asset.slate_image = a.slate_image
      return asset
    })

  const cfg: Record<string, unknown> = { output, assets }
  if (form.normalize) cfg.normalize = true
  if (form.slate_image.trim()) cfg.slate_image = form.slate_image
  if (form.markers.length) {
    cfg.markers = form.markers.map((m) => {
      const marker: Record<string, unknown> = {
        event_id: m.event_id,
        splice_type: m.splice_type,
        assets: [...m.assets],
      }
      if (m.splice_type === 'time_signal') {
        marker.segmentation = { ...m.segmentation }
      } else if (!m.auto_return) {
        // Default (true) is left implicit -- only write the field when it
        // diverges from franken-ts's own default, same convention as the
        // other optional marker/asset fields above.
        marker.auto_return = false
      }
      return marker
    })
  }
  return cfg
}

/** Best-effort inverse of toYamlPlaylist(), for loading an existing playlist
 * back into the form. */
function fromYamlPlaylist(data: Record<string, unknown>) {
  const output = (data.output as Record<string, unknown>) ?? {}
  outputMode.value = output.dir ? 'ladder' : 'single'
  form.output.file = (output.file as string) ?? form.output.file
  form.output.dir = (output.dir as string) ?? form.output.dir
  form.output.resolution = (output.resolution as string) ?? form.output.resolution
  form.output.framerate = (output.framerate as number) ?? form.output.framerate
  form.output.bitrate_kbps = (output.bitrate_kbps as number) ?? form.output.bitrate_kbps
  form.output.gop = (output.gop as number) ?? null
  form.output.service_provider = (output.service_provider as string) ?? form.output.service_provider
  form.output.service_name = (output.service_name as string) ?? form.output.service_name
  form.renditions = ((output.renditions as RenditionForm[]) ?? []).map((r) => ({ ...r }))
  form.normalize = Boolean(data.normalize)
  form.slate_image = (data.slate_image as string) ?? ''

  const rawAssets = (data.assets as Record<string, unknown>[]) ?? []
  form.assets = rawAssets.map((a) => ({
    // Playlists saved before this field existed won't have an id --
    // generate one so markers can still be tagged against these assets;
    // it gets written back on next save.
    id: a.id ? String(a.id) : generateAssetId(),
    file: String(a.file ?? ''),
    start: a.start !== undefined ? String(a.start) : '',
    duration: a.duration !== undefined ? String(a.duration) : '',
    countdown: a.countdown !== undefined ? String(a.countdown) : '',
    fade_in: a.fade_in !== undefined ? String(a.fade_in) : '',
    fade_out: a.fade_out !== undefined ? String(a.fade_out) : '',
    slate_image: String(a.slate_image ?? ''),
  }))

  const rawMarkers = (data.markers as Record<string, unknown>[]) ?? []
  form.markers = rawMarkers.map((m) => {
    const seg = (m.segmentation as Record<string, unknown>) ?? {}
    return {
      event_id: Number(m.event_id),
      splice_type: (m.splice_type as 'splice_insert' | 'time_signal') ?? 'time_signal',
      assets: [...((m.assets as string[]) ?? [])],
      segmentation: {
        type_id: String(seg.type_id ?? '0x34'),
        upid_type: String(seg.upid_type ?? '0x09'),
        upid_hex: String(seg.upid_hex ?? ''),
        web_delivery_allowed: seg.web_delivery_allowed !== false,
        no_regional_blackout: Boolean(seg.no_regional_blackout),
        archive_allowed: Boolean(seg.archive_allowed),
        device_restrictions: Number(seg.device_restrictions ?? 1),
      },
      auto_return: m.auto_return !== false,
    }
  })
  resolvedMarkers.value = null
  resolvedAssetDurations.value = null

  selectedAssetIndex.value = form.assets.length > 0 ? 0 : null
  selectedMarkerEventId.value = null
  markerDraft.value = null
}

async function load() {
  nameInput.value = props.name ?? ''
  selectedAssetIndex.value = null
  if (!props.name) {
    resetForm()
    return
  }
  loading.value = true
  error.value = ''
  try {
    const data = await getPlaylist(props.name)
    fromYamlPlaylist(data)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!nameInput.value.trim()) {
    error.value = 'Playlist name is required.'
    return
  }
  saving.value = true
  error.value = ''
  try {
    const savedName = nameInput.value.trim()
    await savePlaylist(savedName, toYamlPlaylist())
    toast.add({ severity: 'success', summary: 'Saved', life: 3000 })
    if (isNew.value) {
      // Was creating a new playlist -- move to its edit route (in place,
      // no list navigation) so subsequent saves update it instead of
      // re-creating, and reloads/refreshes work as expected.
      router.replace(`/playlists/${encodeURIComponent(savedName)}`)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.name, load)

// --- Build (franken-ts, runs as a background job -- builds whatever's ----
// currently saved on disk, NOT unsaved in-progress edits) ------------------
const building = ref(false)
const buildJobId = ref<string | null>(null)

async function build() {
  if (!props.name) return
  building.value = true
  error.value = ''
  try {
    const job = await buildPlaylist(props.name)
    buildJobId.value = job.id
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    building.value = false
  }
}

function onBuildFinished(job: Job) {
  if (job.status !== 'succeeded') error.value = 'Build failed -- see log below.'
}

// --- ASCII <-> hex helper popover for the selected marker's upid_hex ------
const hexPopover = ref<InstanceType<typeof Popover> | null>(null)
const hexPopoverText = ref('')

function openHexPopover(event: Event) {
  if (!markerDraft.value) return
  hexPopoverText.value = hexToText(markerDraft.value.segmentation.upid_hex)
  hexPopover.value?.toggle(event)
}

function applyHexPopover() {
  if (!markerDraft.value) return
  markerDraft.value.segmentation.upid_hex = textToHex(hexPopoverText.value)
  hexPopover.value?.hide()
}
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="editor-header">
      <div class="flex flex-column gap-1">
        <RouterLink to="/playlists" class="editor-back"><i class="pi pi-arrow-left" /> Playlists</RouterLink>
        <h2 class="m-0">{{ isNew ? 'New playlist' : `Edit: ${name}` }}</h2>
      </div>
      <Button label="Save playlist" icon="pi pi-check" :loading="saving" @click="save" />
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <Tabs v-model:value="activeTab">
      <TabList>
        <Tab value="settings">Settings</Tab>
        <Tab value="assets">
          Timeline
          <span class="asset-count-badge">{{ form.assets.length }}</span>
        </Tab>
        <Tab value="build">Build</Tab>
      </TabList>

      <TabPanels>
        <TabPanel value="settings">
          <div class="flex flex-column gap-4" style="max-width: 56rem">
            <div class="flex flex-column gap-1">
              <label for="playlist-name">Playlist name</label>
              <InputText id="playlist-name" v-model="nameInput" :disabled="!isNew" placeholder="my-stream" />
            </div>

            <Divider align="left"><span class="font-bold">Output</span></Divider>

            <div class="grid">
              <div class="col-4 flex flex-column gap-1">
                <label>Framerate</label>
                <InputNumber v-model="form.output.framerate" :use-grouping="false" />
              </div>
              <div class="col-4 flex flex-column gap-1">
                <label>GOP (default = framerate x2)</label>
                <InputNumber v-model="form.output.gop" :use-grouping="false" placeholder="auto" />
              </div>
              <div class="col-4" />
              <div class="col-6 flex flex-column gap-1">
                <label>Service provider</label>
                <InputText v-model="form.output.service_provider" />
              </div>
              <div class="col-6 flex flex-column gap-1">
                <label>Service name</label>
                <InputText v-model="form.output.service_name" />
              </div>
            </div>

            <Divider />

            <div class="flex gap-3 align-items-center">
              <label><input type="radio" value="single" v-model="outputMode" /> Single file (one bitrate)</label>
              <label><input type="radio" value="ladder" v-model="outputMode" /> ABR ladder (multi-rendition, for ecs-express)</label>
            </div>

            <div class="grid">
              <div v-if="outputMode === 'single'" class="col-6 flex flex-column gap-1">
                <label>Output file</label>
                <InputText v-model="form.output.file" />
              </div>
              <div v-if="outputMode === 'single'" class="col-3 flex flex-column gap-1">
                <label>Resolution</label>
                <InputText v-model="form.output.resolution" />
              </div>
              <div v-if="outputMode === 'single'" class="col-3 flex flex-column gap-1">
                <label>Bitrate (kbps)</label>
                <InputNumber v-model="form.output.bitrate_kbps" :use-grouping="false" />
              </div>
              <div v-if="outputMode === 'ladder'" class="col-12 flex flex-column gap-1">
                <label>Output directory</label>
                <InputText v-model="form.output.dir" />
              </div>
            </div>

            <template v-if="outputMode === 'ladder'">
              <div class="flex justify-content-between align-items-center">
                <span class="font-bold">Renditions</span>
                <Button label="Add rendition" icon="pi pi-plus" size="small" text @click="addRendition" />
              </div>
              <div v-for="(r, i) in form.renditions" :key="i" class="grid align-items-end">
                <div class="col-4"><label>Name</label><InputText v-model="r.name" /></div>
                <div class="col-4"><label>Resolution</label><InputText v-model="r.resolution" /></div>
                <div class="col-3"><label>Bitrate (kbps)</label><InputNumber v-model="r.bitrate_kbps" :use-grouping="false" /></div>
                <div class="col-1"><Button icon="pi pi-trash" severity="danger" text @click="removeRendition(i)" /></div>
              </div>
            </template>

            <Divider align="left"><span class="font-bold">Options</span></Divider>
            <div class="flex align-items-center gap-2">
              <Checkbox v-model="form.normalize" binary input-id="normalize" />
              <label for="normalize">Normalize (auto-fix mismatched inputs)</label>
            </div>
            <div class="flex flex-column gap-1">
              <label>Global slate image (optional, used by fade_in/fade_out cross-dissolves)</label>
              <AssetFileField v-model="form.slate_image" placeholder="/path/to/slate.png" />
            </div>
          </div>
        </TabPanel>

        <TabPanel value="assets">
          <div class="flex flex-column gap-3">
            <div class="timeline-sticky">
              <AssetTimeline
                :assets="form.assets"
                :selected-index="selectedAssetIndex"
                :markers="form.markers"
                :resolved-durations="resolvedAssetDurations ?? undefined"
                :selected-marker-event-id="selectedMarkerEventId"
                :hovered-marker-event-id="hoveredMarkerEventId"
                :scroll-to-marker-event-id="graphScrollTarget"
                @select="selectAsset"
                @add="addAsset"
                @tag-range="tagRange"
                @edit-marker="editMarker"
                @hover-marker="hoverMarkerFromGraph"
              >
                <template #header-actions>
                  <Button
                    label="Resolve estimated durations"
                    icon="pi pi-refresh"
                    size="small"
                    text
                    :loading="resolvingMarkers"
                    @click="previewMarkerResolution"
                  />
                </template>
              </AssetTimeline>
            </div>

            <div v-if="selectedAssetIndex === null && markerDraft === null" class="asset-empty-state">
              <i class="pi pi-images" style="font-size: 1.5rem" />
              <span>{{ form.assets.length === 0 ? 'No assets yet.' : 'Select an asset above to edit it, or shift-click a range of assets to tag a marker.' }}</span>
              <Button label="Add asset" icon="pi pi-plus" outlined @click="addAsset" />
            </div>

            <div v-else-if="markerDraft" class="p-3 border-1 surface-border border-round flex flex-column gap-2 detail-panel">
              <div class="flex justify-content-between align-items-center">
                <span class="font-semibold">{{ markerDraft.editingEventId !== null ? `Marker #${markerDraft.editingEventId}` : 'New marker' }}</span>
                <Button
                  v-if="markerDraft.editingEventId !== null"
                  icon="pi pi-trash"
                  severity="danger"
                  text
                  @click="removeMarker(markerDraft.editingEventId)"
                />
              </div>
              <span class="text-sm text-color-secondary">
                Covers: {{ markerDraft.assets.join(', ') }} -- span/nesting is derived from these assets, never
                set directly.
              </span>

              <div class="grid">
                <div class="col-4 flex flex-column gap-1">
                  <label>Event ID (unique)</label>
                  <InputNumber v-model="markerDraft.event_id" :use-grouping="false" />
                </div>
                <div class="col-8 flex flex-column gap-1">
                  <label>Splice type</label>
                  <Select
                    v-model="markerDraft.splice_type"
                    :options="[
                      { label: 'splice_insert (two-point splice)', value: 'splice_insert' },
                      { label: 'time_signal (segmentation descriptor)', value: 'time_signal' },
                    ]"
                    option-label="label"
                    option-value="value"
                  />
                </div>

                <template v-if="markerDraft.splice_type === 'time_signal'">
                  <div class="col-12"><Divider /></div>
                  <div class="col-8 flex flex-column gap-1">
                    <label>
                      Segmentation type
                      <span
                        class="text-color-secondary text-sm font-normal"
                        title="The pair this marker signals (e.g. Break Start + Break End). Pick ONE pair -- franken-ts derives the End value automatically; the timeline lane is grouped by this same value, so there's nothing else to keep in sync."
                      >(pick a Start/End pair, or a standalone instant signal)</span>
                    </label>
                    <Select
                      v-model="markerDraft.segmentation.type_id"
                      :options="SEGMENTATION_PAIR_OPTIONS"
                      option-label="label"
                      option-value="value"
                      placeholder="Select a segmentation type..."
                      filter
                    />
                  </div>
                  <div class="col-4 flex flex-column gap-1">
                    <label>Timeline lane</label>
                    <div class="lane-badge-row">
                      <template v-if="markerDraft.segmentation.type_id">
                        <span class="marker-type-dot" :style="{ background: laneColorForBadge(markerDraft) }" />
                        <span class="text-sm">{{ laneLabelForMarker(markerDraft) }}</span>
                        <span v-if="isDraftInstant" class="text-color-secondary text-sm">(instant -- no End)</span>
                      </template>
                      <span v-else class="text-color-secondary text-sm">(pick a segmentation type first)</span>
                    </div>
                  </div>
                  <div class="col-6 flex flex-column gap-1">
                    <label>UPID type</label>
                    <Select
                      v-model="markerDraft.segmentation.upid_type"
                      :options="UPID_TYPE_OPTIONS"
                      option-label="label"
                      option-value="value"
                      filter
                    />
                  </div>
                  <div class="col-6 flex flex-column gap-1">
                    <label>UPID hex</label>
                    <div class="flex gap-1">
                      <InputText v-model="markerDraft.segmentation.upid_hex" class="flex-1" />
                      <Button
                        icon="pi pi-language"
                        severity="secondary"
                        outlined
                        title="Enter as text (ASCII) instead of hex"
                        @click="openHexPopover($event)"
                      />
                    </div>
                  </div>
                  <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="markerDraft.segmentation.web_delivery_allowed" binary /><label>Web delivery allowed</label></div>
                  <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="markerDraft.segmentation.no_regional_blackout" binary /><label>No regional blackout</label></div>
                  <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="markerDraft.segmentation.archive_allowed" binary /><label>Archive allowed</label></div>
                  <div class="col-3 flex flex-column gap-1"><label>Device restrictions</label><InputNumber v-model="markerDraft.segmentation.device_restrictions" :use-grouping="false" /></div>
                </template>

                <template v-else-if="markerDraft.splice_type === 'splice_insert'">
                  <div class="col-12"><Divider /></div>
                  <div class="col-12 flex align-items-center gap-2">
                    <Checkbox v-model="markerDraft.auto_return" binary />
                    <label>
                      Auto-return
                      <span
                        class="text-color-secondary text-sm font-normal"
                        title="Auto-return (default): one splice_insert message covers the whole break -- the player returns to content on its own once break_duration elapses, no cue-in is sent. Off: an explicit splice_insert cue-in is sent at the break's real end, in addition to the cue-out -- matching time_signal's Start/End pairing."
                      >({{ markerDraft.auto_return ? 'single message, no cue-in' : 'explicit cue-out + cue-in pair' }})</span>
                    </label>
                  </div>
                </template>
              </div>

              <Divider />
              <div class="flex align-items-center gap-2 flex-wrap">
                <template v-if="isDraftInstant && markerDraft.assets.length > 1">
                  <span class="text-sm text-color-secondary">
                    Instant signal -- will create <strong>{{ markerDraft.assets.length }}</strong> separate markers,
                    one per asset ({{ markerDraft.assets.join(', ') }}), not one marker spanning the whole range.
                  </span>
                </template>
                <template v-else>
                  <span class="font-semibold text-sm">Auto-computed (from asset containment):</span>
                  <span class="text-sm text-color-secondary">
                    segment {{ (draftPreviewSpan?.segmentNum ?? 0) + 1 }}
                    of {{ draftPreviewSpan?.segmentsExpected ?? 1 }},
                    depth {{ draftPreviewSpan?.depth ?? 0 }}
                  </span>
                  <template v-if="draftResolved">
                    <span class="text-sm text-color-secondary">
                      -- resolved: {{ draftResolved?.start_seconds?.toFixed(2) }}s
                      to {{ draftResolved?.end_seconds?.toFixed(2) }}s
                    </span>
                  </template>
                </template>
              </div>

              <Divider />
              <div class="flex justify-content-end gap-2">
                <Button label="Cancel" text @click="cancelDraft" />
                <Button :label="commitButtonLabel" :disabled="!canCommitDraft" @click="commitDraft" />
              </div>
            </div>

            <div v-else-if="selectedAssetIndex !== null" class="p-3 border-1 surface-border border-round flex flex-column gap-2 detail-panel">
              <div class="flex justify-content-between align-items-center">
                <span class="font-semibold">Asset {{ selectedAssetIndex + 1 }} of {{ form.assets.length }}</span>
                <div class="flex gap-1">
                  <Button
                    icon="pi pi-chevron-left"
                    text
                    :disabled="selectedAssetIndex === 0"
                    @click="selectAsset(selectedAssetIndex - 1)"
                  />
                  <Button
                    icon="pi pi-chevron-right"
                    text
                    :disabled="selectedAssetIndex === form.assets.length - 1"
                    @click="selectAsset(selectedAssetIndex + 1)"
                  />
                  <Button icon="pi pi-trash" severity="danger" text @click="removeAsset(selectedAssetIndex)" />
                </div>
              </div>
              <div class="grid">
                <div class="col-8 flex flex-column gap-1">
                  <label>File path (local path or https:// URL)</label>
                  <AssetFileField v-model="form.assets[selectedAssetIndex].file" placeholder="content.mp4" />
                </div>
                <div class="col-4 flex flex-column gap-1">
                  <label>Asset ID (for tagging markers)</label>
                  <InputText v-model="form.assets[selectedAssetIndex].id" />
                </div>
                <div class="col-6 flex flex-column gap-1">
                  <label>Start</label>
                  <InputText v-model="form.assets[selectedAssetIndex].start" placeholder="00:00:00 / 10 min" />
                </div>
                <div class="col-6 flex flex-column gap-1">
                  <label>Duration</label>
                  <InputText v-model="form.assets[selectedAssetIndex].duration" placeholder="10 min" />
                </div>
                <div class="col-4 flex flex-column gap-1">
                  <label>Countdown</label>
                  <InputText v-model="form.assets[selectedAssetIndex].countdown" placeholder="5 or -1" />
                </div>
                <div class="col-4 flex flex-column gap-1">
                  <label>Fade in (s)</label>
                  <InputText v-model="form.assets[selectedAssetIndex].fade_in" placeholder="1.5" />
                </div>
                <div class="col-4 flex flex-column gap-1">
                  <label>Fade out (s)</label>
                  <InputText v-model="form.assets[selectedAssetIndex].fade_out" placeholder="1.5" />
                </div>
                <div class="col-12 flex flex-column gap-1">
                  <label>Per-asset slate image (overrides global)</label>
                  <AssetFileField v-model="form.assets[selectedAssetIndex].slate_image" />
                </div>
              </div>
            </div>

            <div v-if="form.markers.length" class="flex flex-column gap-2">
              <div class="flex justify-content-between align-items-center">
                <span class="font-bold">Markers ({{ form.markers.length }})</span>
              </div>
              <div
                v-for="span in markerListOrder"
                :key="span.marker.event_id"
                class="marker-row"
                :class="{
                  'marker-row-selected': span.marker.event_id === selectedMarkerEventId,
                  'marker-row-hovered': span.marker.event_id === hoveredMarkerEventId,
                }"
                :style="{ paddingLeft: `${span.depth * 1.25}rem` }"
                @click="editMarker(span.marker.event_id)"
                @mouseenter="hoverMarkerFromList(span.marker.event_id)"
                @mouseleave="hoverMarkerFromList(null)"
              >
                <span class="marker-type-dot" :style="{ background: laneColorForBadge(span.marker) }" />
                <span class="font-semibold">{{ laneLabelForMarker(span.marker) }} #{{ span.marker.event_id }}</span>
                <span class="text-color-secondary text-sm">{{ span.marker.assets.join(', ') }}</span>
                <span class="text-color-secondary text-sm">seg {{ span.segmentNum + 1 }}/{{ span.segmentsExpected }}</span>
              </div>
            </div>
          </div>
        </TabPanel>

        <TabPanel value="build">
          <div class="flex flex-column gap-3">
            <p class="text-color-secondary m-0">
              Runs franken-ts against the saved playlist and produces the <code>.ts</code> file its-a-live
              channels stage via Spark. Builds whatever's currently saved on disk -- save first if you
              have unsaved changes above.
            </p>
            <div class="flex align-items-center gap-2">
              <Button label="Build" icon="pi pi-cog" :loading="building" :disabled="isNew" @click="build" />
              <span v-if="isNew" class="text-color-secondary text-sm">Save the playlist first.</span>
            </div>
            <JobPanel v-if="buildJobId" :job-id="buildJobId" @finished="onBuildFinished" />
          </div>
        </TabPanel>
      </TabPanels>
    </Tabs>

    <Popover ref="hexPopover">
      <div class="flex flex-column gap-2" style="width: 20rem">
        <span class="font-semibold">Enter UPID as text</span>
        <span class="text-sm text-color-secondary">Converts to/from the hex value above (UTF-8 bytes, hex-encoded).</span>
        <InputText v-model="hexPopoverText" placeholder="e.g. SIGNAL:LINEAR" autofocus />
        <div class="flex justify-content-end gap-2">
          <Button label="Apply" size="small" @click="applyHexPopover" />
        </div>
      </div>
    </Popover>
  </div>
</template>

<style scoped>
.editor-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1rem;
  flex-wrap: wrap;
}

/* Make the top-level tabs read unambiguously as tabs -- bigger targets,
 * a filled/rounded active state instead of relying only on the thin
 * active-bar + a stray focus-ring rectangle, which read as plain text. */
:deep(.p-tablist) {
  border-bottom: 2px solid var(--p-surface-200, #e2e8f0);
}

:deep(.p-tablist-tab-list) {
  gap: 0.35rem;
}

:deep(.p-tab) {
  padding: 0.75rem 1.5rem;
  font-size: 1rem;
  font-weight: 600;
  border-radius: 8px 8px 0 0;
  color: var(--p-text-muted-color, #64748b);
  background: transparent;
  border: none;
  box-shadow: none !important;
  transition: background 0.15s ease, color 0.15s ease;
}

:deep(.p-tab:hover) {
  background: var(--p-surface-100, #f1f5f9);
  color: var(--p-text-color, #0f172a);
}

:deep(.p-tab-active) {
  color: var(--p-primary-color, #0e7490);
  background: var(--p-primary-50, #ecfeff);
}

:deep(.p-tablist-active-bar) {
  height: 3px;
  border-radius: 2px;
  background: var(--p-primary-color, #0e7490);
}

.editor-back {
  color: var(--p-text-muted-color, #64748b);
  text-decoration: none;
  font-size: 0.85rem;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}

.editor-back:hover {
  text-decoration: underline;
}

.asset-count-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 1.35rem;
  height: 1.35rem;
  padding: 0 0.3rem;
  margin-left: 0.4rem;
  border-radius: 999px;
  background: var(--p-surface-200, #e2e8f0);
  font-size: 0.7rem;
  font-weight: 600;
}

.timeline-sticky {
  position: sticky;
  top: 0.5rem;
  z-index: 2;
  background: var(--p-content-background, #fff);
  padding-bottom: 0.5rem;
}

.asset-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
  padding: 3rem 1rem;
  color: var(--p-text-muted-color, #64748b);
  border: 1px dashed var(--p-surface-border, #cbd5e1);
  border-radius: 8px;
}

.detail-panel {
  background: var(--p-surface-50, #f8fafc);
}

.marker-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.35rem 0.5rem;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}

.marker-row:hover {
  background: var(--p-surface-100, #f1f5f9);
}

.marker-row-selected {
  border-color: #7c3aed;
  background: var(--p-surface-100, #f1f5f9);
}

/* Hovered via the GRAPH (not the row's own native :hover, which the browser
 * already handles) -- a lighter dashed treatment, distinct from "selected"
 * so the two never look the same when they differ. */
.marker-row-hovered {
  border-color: #94a3b8;
  border-style: dashed;
  background: var(--p-surface-50, #f8fafc);
}

.marker-type-dot {
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 999px;
  flex: none;
}

.lane-badge-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  height: 2.5rem;
}
</style>
