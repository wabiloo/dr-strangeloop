<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import ColorPicker from 'primevue/colorpicker'
import ConfirmPopup from 'primevue/confirmpopup'
import Dialog from 'primevue/dialog'
import Divider from 'primevue/divider'
import InputGroup from 'primevue/inputgroup'
import InputGroupAddon from 'primevue/inputgroupaddon'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Popover from 'primevue/popover'
import RadioButton from 'primevue/radiobutton'
import Select from 'primevue/select'
import Tab from 'primevue/tab'
import TabList from 'primevue/tablist'
import TabPanel from 'primevue/tabpanel'
import TabPanels from 'primevue/tabpanels'
import Tabs from 'primevue/tabs'
import Textarea from 'primevue/textarea'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { buildPlaylist, buildScteVerify, getOutputStatus, getPlaylist, getPreviewStatus, getScteVerifyStatus, playlistPreviewUrl, probeMedia, resolveMarkers, savePlaylist, scteVerifyReportUrl } from '../api/client'
import type { Job, ResolvedMarker } from '../api/types'
import AssetTimeline from '../components/AssetTimeline.vue'
import AssetFileField from '../components/AssetFileField.vue'
import JobPanel from '../components/JobPanel.vue'
import { parseApproxSeconds } from '../utils/duration'
import { alignConfirmPopup } from '../utils/confirmPopup'
import {
  SEGMENTATION_PAIR_OPTIONS,
  UPID_TYPE_OPTIONS,
  colorForLaneKey,
  hexToText,
  laneKeyForMarker,
  laneLabelForMarker,
  textToHex,
} from '../segmentationPresets'
import { isInstantMarker, layoutMarkers, nextEventId, orderForDisplay, semanticMarkerIssues } from '../markerLayout'
import type { AssetRole, NumberingScheme } from '../markerLayout'
import { filesFromDrop, isDragInside, isFileDrag, sourceFromDroppedFile } from '../utils/fileDrop'

const props = defineProps<{ name: string | null }>()
const router = useRouter()
const confirm = useConfirm()
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
  segment_num?: number | null
  segments_expected?: number | null
  sub_segment_num?: number | null
  sub_segments_expected?: number | null
}

interface AssetForm {
  id: string
  file: string
  role: AssetRole | null
  start: string
  duration: string
  fade_in: string
  fade_out: string
  slate_image: string
  no_osd: boolean
  osd_label: string
}

/** Mirrors franken_ts.config.CornerContent -- '' means "nothing shown". */
type CornerContent =
  | ''
  | 'asset_id'
  | 'time'
  | 'next_asset_id'
  | 'scte35_spans'
  | 'is_adbreak'
  | 'osd_label'

const CORNER_CONTENT_OPTIONS: { label: string; value: CornerContent }[] = [
  { label: 'None', value: '' },
  { label: 'Asset ID', value: 'asset_id' },
  { label: 'Time', value: 'time' },
  { label: 'Next asset', value: 'next_asset_id' },
  { label: 'SCTE-35 spans', value: 'scte35_spans' },
  { label: 'Ad break indicator', value: 'is_adbreak' },
  { label: 'OSD label', value: 'osd_label' },
]

interface OsdForm {
  enabled: boolean
  countdown: { enabled: boolean; height_pct: number }
  text_size_pct: number
  text_color: string
  ad_break_label: string
  corner_box: { enabled: boolean; color: string }
  corners: {
    top_left: CornerContent
    top_right: CornerContent
    bottom_left: CornerContent
    bottom_right: CornerContent
  }
}

/** Representative placeholder text for the OSD frame preview -- not tied to
 * real asset/timeline data (the editor has no "current playback position"),
 * just enough to show roughly what each corner content type will look
 * like. `is_adbreak` is handled separately in previewTextFor() since its
 * display text is itself configurable (osd.ad_break_label). */
const CORNER_PREVIEW_TEXT: Partial<Record<CornerContent, string>> = {
  asset_id: 'asset-1',
  time: '12.32/34.60',
  next_asset_id: 'next: asset-2',
  scte35_spans: 'BRK / PPO / PAD',
  osd_label: 'Weather',
}

function hexToRgba(hex: string, alpha: number): string {
  const clean = hex.replace('#', '')
  const value = parseInt(clean, 16) || 0
  const r = (value >> 16) & 255
  const g = (value >> 8) & 255
  const b = value & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

interface MarkerForm {
  _ui_id: number
  event_id: number
  splice_type: 'splice_insert' | 'time_signal'
  assets: string[]
  segmentation: SegmentationForm
  /** `splice_insert` only: true (default) emits a single self-contained
   * message with no cue-in (SCTE-35 auto_return); false emits an explicit
   * cue-out/cue-in pair. Ignored for `time_signal`. */
  auto_return: boolean
  break_interval: number
}

const ASSET_ROLE_OPTIONS: { label: string; value: AssetRole | null }[] = [
  { label: 'None', value: null },
  { label: 'Advert', value: 'advert' },
  { label: 'Jingle', value: 'jingle' },
]

const NUMBERING_OPTIONS = [
  { label: 'SCTE 35 2023r1', value: 'SCTE35_2023R1' },
  { label: 'SCTE 35 2019a', value: 'SCTE35_2019A' },
  { label: 'af2m / SNPTV', value: 'AF2M_SNPTV' },
]

/** A marker being created or edited, not yet committed to `form.markers`.
 * `editingEventId` is the event_id of the existing committed marker being
 * replaced on commit, or `null` for a brand-new marker (from "Add marker"). */
interface MarkerDraft extends MarkerForm {
  editingMarkerUiId: number | null
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

// Separate counter/prefix so ids stay meaningful for the bootstrap flow's
// auto-detected ads (below) -- distinct namespace from `asset-N`, so no
// collision risk with ids generated by generateAssetId().
let adIdCounter = 1
function generateAdId(): string {
  return `ad-${adIdCounter++}`
}
let markerUiIdCounter = 1
function generateMarkerUiId(): number { return markerUiIdCounter++ }

function newAsset(): AssetForm {
  return {
    id: generateAssetId(),
    file: '',
    role: null,
    start: '',
    duration: '',
    fade_in: '',
    fade_out: '',
    slate_image: '',
    no_osd: false,
    osd_label: '',
  }
}

function defaultOsd(): OsdForm {
  return {
    enabled: false,
    countdown: { enabled: true, height_pct: 5 },
    text_size_pct: 4,
    text_color: '#FFFFFF',
    ad_break_label: 'ad break',
    corner_box: { enabled: false, color: '#000000' },
    corners: { top_left: '', top_right: '', bottom_left: 'asset_id', bottom_right: 'time' },
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
    service_provider: '',
    service_name: '',
  }
}

const form = reactive({
  output: defaultOutput(),
  renditions: [] as RenditionForm[],
  normalize: false,
  slate_image: '',
  osd: defaultOsd(),
  assets: [] as AssetForm[],
  markers: [] as MarkerForm[],
  enforce_scte35_marker_semantics: true,
  scte35_numbering_scheme: 'SCTE35_2023R1' as NumberingScheme,
  break_numbering_supported: false,
})

function previewTextFor(corner: 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'): string {
  const content = form.osd.corners[corner]
  if (!content) return ''
  if (content === 'is_adbreak') return form.osd.ad_break_label.trim() || 'ad break'
  return CORNER_PREVIEW_TEXT[content] ?? ''
}

/** Fixed corner-box fill color, matching osd.py's non-configurable
 * `_CORNER_BOX_FILL_COLOR` -- corner_box.color no longer sets the fill,
 * only the border accent stripe (see below). */
const OSD_CORNER_BOX_FILL = '#262626'

/** Style for one preview-text element in the frame mockup -- same text
 * color for every corner (mirroring osd.text_color applying uniformly),
 * plus, when the corner box is enabled: a fixed, semi-transparent dark fill
 * and a semi-transparent border accent in corner_box.color flush with the
 * box's outer edge (left edge for left corners, right for right), mirroring
 * osd.py's build_corner_text_filter/build_corner_accent_stripe_graph. The
 * `.osd-preview-text` rule's `background-clip: padding-box` keeps these two
 * alpha layers from double-blending in the border's own area -- see that
 * rule's comment. */
function osdPreviewTextStyle(side: 'left' | 'right') {
  if (!form.osd.corner_box.enabled) {
    return { color: form.osd.text_color, background: 'transparent' }
  }
  const borderSide = side === 'left' ? 'borderLeft' : 'borderRight'
  return {
    color: form.osd.text_color,
    background: hexToRgba(OSD_CORNER_BOX_FILL, 0.6),
    [borderSide]: `4px solid ${hexToRgba(form.osd.corner_box.color, 0.9)}`,
  }
}

/** Bottom preview text needs to clear the countdown bar preview, which is
 * sized as a percentage of the mockup's own height. */
const osdPreviewBottomOffset = computed(() =>
  form.osd.countdown.enabled ? `calc(${form.osd.countdown.height_pct}% + 0.5rem)` : '0.5rem',
)

/** Resets the form to its blank-new-playlist state -- needed because vue-router
 * reuses this component instance when navigating between /playlists/new and
 * /playlists/:name, so mount-time initializers alone aren't enough. */
function resetForm() {
  outputMode.value = 'single'
  Object.assign(form.output, defaultOutput())
  form.renditions = []
  form.normalize = false
  form.slate_image = ''
  Object.assign(form.osd, defaultOsd())
  form.assets = []
  form.markers = []
  form.enforce_scte35_marker_semantics = true
  form.scte35_numbering_scheme = 'SCTE35_2023R1'
  form.break_numbering_supported = false
  markerUiIdCounter = 1
  assetIdCounter = 1
  markerUiIdCounter = 1
  markerDraft.value = null
  selectedMarkerEventId.value = null
}

watch(() => [form.markers, form.assets, form.scte35_numbering_scheme, form.break_numbering_supported, form.enforce_scte35_marker_semantics], () => {
  resolvedMarkers.value = null
}, { deep: true })

// PrimeVue's <ColorPicker> (format="hex", the default) reads/writes a bare
// "RRGGBB" string, but franken_ts.config.OsdConfig.text_color is "#RRGGBB"
// -- this bridges the two so form.osd.text_color stays in the schema's format.
const osdTextColorHex = computed({
  get: () => form.osd.text_color.replace(/^#/, ''),
  set: (v: string) => {
    form.osd.text_color = `#${v.replace(/^#/, '').toUpperCase()}`
  },
})

const osdBoxColorHex = computed({
  get: () => form.osd.corner_box.color.replace(/^#/, ''),
  set: (v: string) => {
    form.osd.corner_box.color = `#${v.replace(/^#/, '').toUpperCase()}`
  },
})

const isNew = computed(() => props.name === null)

/** Serialized snapshot of everything `save()` persists (the playlist name
 * plus toYamlPlaylist()'s output), captured right after a load/reset and
 * again after every successful save. `isDirty` just diffs the current form
 * against that snapshot -- reuses toYamlPlaylist() as the single source of
 * truth for "what save() would send" instead of hand-tracking which fields
 * count as a change. */
function currentSnapshot(): string {
  return JSON.stringify({ name: nameInput.value.trim(), cfg: toYamlPlaylist() })
}
const savedSnapshot = ref('')
const isDirty = computed(() => currentSnapshot() !== savedSnapshot.value)
const activeTab = ref('settings')
const selectedAssetIndex = ref<number | null>(null)
const selectedMarkerEventId = ref<number | null>(null)

// ── Hover cross-highlight between the marker list and the timeline graph ──
const hoveredMarkerEventId = ref<number | null>(null)

function hoverMarkerFromList(eventId: number | null) {
  hoveredMarkerEventId.value = eventId
}
function hoverMarkerFromGraph(eventId: number | null) {
  hoveredMarkerEventId.value = eventId
}

function selectAsset(i: number) {
  selectedAssetIndex.value = i
  selectedMarkerEventId.value = null
  markerDraft.value = null
}

/** Keep marker references attached to the same asset when its id changes.
 * Asset ids are the references used by marker spans, so changing the asset
 * object alone would make every span that covered it look unresolved. */
function updateAssetId(index: number | null, newId: string) {
  if (index === null) return
  const asset = form.assets[index]
  if (!asset || asset.id === newId) return

  const oldId = asset.id
  asset.id = newId

  function replaceInSpans(spans: { assets: string[] }[]) {
    spans.forEach((span) => {
      if (span.assets.includes(oldId)) {
        span.assets = span.assets.map((id) => (id === oldId ? newId : id))
      }
    })
  }

  replaceInSpans(form.markers)
  if (markerDraft.value) replaceInSpans([markerDraft.value])
  if (resolvedMarkers.value) replaceInSpans(resolvedMarkers.value)

  // Preserve the resolved width for this asset after moving its duration
  // cache entry to the new key.
  if (resolvedAssetDurations.value && oldId in resolvedAssetDurations.value) {
    const durations = { ...resolvedAssetDurations.value, [newId]: resolvedAssetDurations.value[oldId] }
    delete durations[oldId]
    resolvedAssetDurations.value = durations
  }
}

function addAsset(index?: number) {
  const asset = newAsset()
  if (index === undefined || index === null) {
    form.assets.push(asset)
    selectAsset(form.assets.length - 1)
  } else {
    insertAssetsAt(index, [asset])
  }
}

// ── Bootstrap timeline from a flat list of files/URLs ──────────────────────
// Lets a user paste an ordered list of local paths/URLs instead of adding
// assets one at a time. Every source is probed (reusing the same
// /files/probe endpoint as the per-asset "probe" button) for its duration;
// anything at or under the configured threshold is classified as an ad, and
// every *consecutive* run of ads is folded into one splice_insert `break`
// marker spanning them -- mirrors franken-ts-bootstrap's CLI counterpart
// (see franken-ts/franken_ts/bootstrap.py) but scoped to this form instead
// of a standalone YAML file.
interface BootstrapRow {
  source: string
  duration: number | null
  error: string | null
}

const bootstrapOpen = ref(false)
const bootstrapText = ref('')
const bootstrapThreshold = ref('1 min')
const bootstrapRows = ref<BootstrapRow[]>([])
const bootstrapProbing = ref(false)
const bootstrapProgress = ref(0)
// Where commitBootstrap() splices the probed batch in -- undefined means
// "append at the end" (the original behavior, still used by the toolbar's
// "Bootstrap from files..." menu item); set when opened from an in-timeline
// insert-joint "+" button instead (see insertMenuItems in AssetTimeline.vue).
const bootstrapTargetIndex = ref<number | undefined>(undefined)

function openBootstrap(index?: number) {
  bootstrapTargetIndex.value = index
  bootstrapText.value = ''
  bootstrapRows.value = []
  bootstrapProgress.value = 0
  bootstrapOpen.value = true
}

function parseBootstrapSources(): string[] {
  return bootstrapText.value
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'))
}

const bootstrapThresholdSeconds = computed(() => parseApproxSeconds(bootstrapThreshold.value))

async function probeBootstrapSources() {
  const sources = parseBootstrapSources()
  if (sources.length === 0) return
  bootstrapProbing.value = true
  bootstrapRows.value = []
  bootstrapProgress.value = 0
  for (const source of sources) {
    try {
      const result = await probeMedia(source)
      bootstrapRows.value.push({ source, duration: result.duration_seconds, error: null })
    } catch (e) {
      bootstrapRows.value.push({ source, duration: null, error: e instanceof Error ? e.message : String(e) })
    }
    bootstrapProgress.value += 1
  }
  bootstrapProbing.value = false
}

function bootstrapLabelFor(source: string): string {
  try {
    const withoutQuery = source.split('?')[0]
    return withoutQuery.split('/').pop() || source
  } catch {
    return source
  }
}

function isBootstrapAd(row: BootstrapRow): boolean {
  const threshold = bootstrapThresholdSeconds.value
  return row.duration !== null && threshold !== null && row.duration <= threshold
}

const bootstrapHasErrors = computed(() => bootstrapRows.value.some((r) => r.error !== null))
const bootstrapAdCount = computed(() => bootstrapRows.value.filter(isBootstrapAd).length)
const bootstrapCanCommit = computed(
  () => bootstrapRows.value.length > 0 && !bootstrapHasErrors.value && !bootstrapProbing.value,
)

/** Builds the probed rows into new assets (plus one `break` marker per
 * consecutive run of ads within this batch), then inserts the whole batch
 * at bootstrapTargetIndex (or appends, if unset) via insertAssetsAt --
 * same marker-boundary handling as a single inserted asset (see
 * insertAssetsAt/planInsert). */
function commitBootstrap() {
  if (!bootstrapCanCommit.value) return

  const usedSoFar = [...usedEventIds.value]
  let adRun: string[] = []
  const newAssets: AssetForm[] = []
  const extraMarkers: MarkerForm[] = []

  function flushRun() {
    if (adRun.length === 0) return
    const eventId = nextEventId(usedSoFar)
    usedSoFar.push(eventId)
    extraMarkers.push({
      _ui_id: generateMarkerUiId(),
      event_id: eventId,
      splice_type: 'splice_insert',
      assets: [...adRun],
      segmentation: newSegmentation(),
      auto_return: true,
      break_interval: 1,
    })
    adRun = []
  }

  for (const row of bootstrapRows.value) {
    const asset = newAsset()
    asset.file = row.source
    // Already probed above -- set duration explicitly so the timeline
    // renders at its real width immediately, without a separate "Resolve
    // estimated durations" round-trip.
    if (row.duration !== null) asset.duration = String(Number(row.duration.toFixed(3)))
    const isAd = isBootstrapAd(row)
    if (isAd) {
      // Auto-detected ads get an "ad-N" id instead of the default
      // "asset-N" -- more meaningful once it shows up in the marker list
      // and clip-durations table.
      asset.id = generateAdId()
      asset.osd_label = bootstrapLabelFor(row.source)
      adRun.push(asset.id)
    } else {
      flushRun()
    }
    newAssets.push(asset)
  }
  flushRun()

  insertAssetsAt(bootstrapTargetIndex.value ?? form.assets.length, newAssets, extraMarkers)
  bootstrapOpen.value = false
}

// ── Browser file drop targets ------------------------------------------------
// Normal browsers do not expose the absolute path of a dropped local file.
// sourceFromDroppedFile uploads it to Igor when necessary, while desktop
// shells that expose File.path can use the original path directly.
const emptyAssetDropActive = ref(false)
const emptyAssetDropBusy = ref(false)
const emptyAssetDropError = ref('')
const bootstrapDropActive = ref(false)
const bootstrapDropBusy = ref(false)
const bootstrapDropError = ref('')

function handleEmptyAssetDragOver(event: DragEvent) {
  if (!isFileDrag(event)) return
  event.preventDefault()
  emptyAssetDropActive.value = true
}

function handleEmptyAssetDragLeave(event: DragEvent) {
  if (!isDragInside(event)) emptyAssetDropActive.value = false
}

async function handleEmptyAssetDrop(event: DragEvent) {
  if (!isFileDrag(event)) return
  event.preventDefault()
  emptyAssetDropActive.value = false
  const files = filesFromDrop(event)
  if (!files.length) return

  emptyAssetDropBusy.value = true
  emptyAssetDropError.value = ''
  try {
    const newAssets: AssetForm[] = []
    for (const file of files) {
      const asset = newAsset()
      asset.file = await sourceFromDroppedFile(file)
      newAssets.push(asset)
    }
    insertAssetsAt(form.assets.length, newAssets)
  } catch (e) {
    emptyAssetDropError.value = e instanceof Error ? e.message : String(e)
  } finally {
    emptyAssetDropBusy.value = false
  }
}

function handleBootstrapDragOver(event: DragEvent) {
  if (!isFileDrag(event)) return
  event.preventDefault()
  bootstrapDropActive.value = true
}

function handleBootstrapDragLeave(event: DragEvent) {
  if (!isDragInside(event)) bootstrapDropActive.value = false
}

async function handleBootstrapDrop(event: DragEvent) {
  if (!isFileDrag(event)) return
  event.preventDefault()
  bootstrapDropActive.value = false
  const files = filesFromDrop(event)
  if (!files.length) return

  bootstrapDropBusy.value = true
  bootstrapDropError.value = ''
  bootstrapRows.value = []
  try {
    const sources: string[] = []
    for (const file of files) sources.push(await sourceFromDroppedFile(file))
    const existing = bootstrapText.value.trimEnd()
    bootstrapText.value = [existing, ...sources].filter(Boolean).join('\n')
  } catch (e) {
    bootstrapDropError.value = e instanceof Error ? e.message : String(e)
  } finally {
    bootstrapDropBusy.value = false
  }
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

// ── Reordering assets (move earlier/later by one slot) ─────────────────────
// Markers reference assets by id, not position, so a plain swap never
// dangles a reference -- the only thing that can break is the backend's
// contiguity requirement (a marker's assets must occupy a contiguous run,
// see franken_ts.config.Config.validate_markers). An adjacent-position swap
// can only threaten that for a marker that has exactly one of the two
// swapped assets as a member, and only when that member sits at the span's
// boundary facing the swap (spans are already contiguous, so an interior
// member can't be adjacent to a non-member).

/** A marker's [lo, hi] asset-index span, or null if none of its assets
 * exist in the current asset list (shouldn't normally happen). */
function markerSpan(m: MarkerForm, idToIndex: Map<string, number>): { lo: number; hi: number } | null {
  const indices = m.assets.map((id) => idToIndex.get(id)).filter((i): i is number => i !== undefined)
  if (!indices.length) return null
  return { lo: Math.min(...indices), hi: Math.max(...indices) }
}

interface PendingAssetMove {
  index: number
  direction: -1 | 1
  /** Markers where the MOVING asset is the boundary member being displaced,
   * innermost (smallest span) first -- the user gets to choose, per marker
   * from the inside out, whether it detaches or grows to keep the asset. */
  chain: MarkerForm[]
  /** Markers on the other side where the swap pushes the moving asset INTO
   * their boundary -- always grows to absorb it (no ambiguity: nothing else
   * is being displaced from the user's point of view). */
  neighborChain: MarkerForm[]
  /** How many of `chain`, counting from the innermost, the user has chosen
   * to detach the asset from; the rest grow to keep it. Only 0..chain.length
   * are valid choices (detaching an outer marker while its inner one stays
   * would itself be non-contiguous). */
  detachCount: number
}
const pendingAssetMove = ref<PendingAssetMove | null>(null)
const highlightedMovedAssetId = ref<string | null>(null)
let movedAssetHighlightTimer: ReturnType<typeof setTimeout> | undefined

function highlightMovedAsset(assetId: string) {
  highlightedMovedAssetId.value = assetId
  if (movedAssetHighlightTimer) clearTimeout(movedAssetHighlightTimer)
  movedAssetHighlightTimer = setTimeout(() => {
    if (highlightedMovedAssetId.value === assetId) highlightedMovedAssetId.value = null
    movedAssetHighlightTimer = undefined
  }, 1500)
}
onBeforeUnmount(() => {
  if (movedAssetHighlightTimer) clearTimeout(movedAssetHighlightTimer)
})

function applyAssetMove(opts: {
  index: number
  direction: -1 | 1
  detachFromMoving: MarkerForm[]
  growWithOther: MarkerForm[]
  growWithMoving: MarkerForm[]
}) {
  const { index, direction, detachFromMoving, growWithOther, growWithMoving } = opts
  const otherIndex = index + direction
  const movingId = form.assets[index].id
  const movedAssetId = movingId
  const otherId = form.assets[otherIndex].id

  detachFromMoving.forEach((m) => {
    m.assets = m.assets.filter((id) => id !== movingId)
  })
  growWithOther.forEach((m) => {
    if (!m.assets.includes(otherId)) m.assets = [...m.assets, otherId]
  })
  growWithMoving.forEach((m) => {
    if (!m.assets.includes(movingId)) m.assets = [...m.assets, movingId]
  })

  const tmp = form.assets[index]
  form.assets[index] = form.assets[otherIndex]
  form.assets[otherIndex] = tmp

  if (selectedAssetIndex.value === index) selectedAssetIndex.value = otherIndex
  else if (selectedAssetIndex.value === otherIndex) selectedAssetIndex.value = index
  highlightMovedAsset(movedAssetId)
}

function moveAsset(index: number, direction: -1 | 1) {
  const otherIndex = index + direction
  if (otherIndex < 0 || otherIndex >= form.assets.length) return

  const idToIndex = new Map(form.assets.map((a, i) => [a.id, i] as const))
  const movingId = form.assets[index].id
  const otherId = form.assets[otherIndex].id

  const movingChain = form.markers
    .filter((m) => {
      if (m.assets.length <= 1) return false // single-asset markers just move with the asset
      if (!m.assets.includes(movingId) || m.assets.includes(otherId)) return false
      const span = markerSpan(m, idToIndex)
      return span !== null && (direction === -1 ? span.lo === index : span.hi === index)
    })
    .sort((a, b) => {
      const sa = markerSpan(a, idToIndex)!
      const sb = markerSpan(b, idToIndex)!
      return (sa.hi - sa.lo) - (sb.hi - sb.lo)
    })

  const neighborChain = form.markers.filter((m) => {
    if (m.assets.length <= 1) return false
    if (!m.assets.includes(otherId) || m.assets.includes(movingId)) return false
    const span = markerSpan(m, idToIndex)
    return span !== null && (direction === -1 ? span.hi === otherIndex : span.lo === otherIndex)
  })

  if (movingChain.length === 0) {
    applyAssetMove({ index, direction, detachFromMoving: [], growWithOther: [], growWithMoving: neighborChain })
    return
  }

  pendingAssetMove.value = { index, direction, chain: movingChain, neighborChain, detachCount: 0 }
}

function confirmPendingAssetMove() {
  const p = pendingAssetMove.value
  if (!p) return
  applyAssetMove({
    index: p.index,
    direction: p.direction,
    detachFromMoving: p.chain.slice(0, p.detachCount),
    growWithOther: p.chain.slice(p.detachCount),
    growWithMoving: p.neighborChain,
  })
  pendingAssetMove.value = null
}

function cancelPendingAssetMove() {
  pendingAssetMove.value = null
}

function markerMoveLabel(m: MarkerForm): string {
  return `${laneLabelForMarker(m)} #${m.event_id}`
}

/** Radio options for the pending-move dialog: 0 = keep the asset in every
 * marker in the chain (all grow), N = detach it from the N innermost
 * markers and keep/grow the rest -- the only choices that stay
 * nesting-consistent (see PendingAssetMove.detachCount). */
const pendingAssetMoveOptions = computed(() => {
  const p = pendingAssetMove.value
  if (!p) return []
  const options: { value: number; label: string }[] = [
    {
      value: 0,
      label:
        p.chain.length === 1
          ? `Grow "${markerMoveLabel(p.chain[0])}" to include the shifted asset`
          : `Grow all ${p.chain.length} markers to include the shifted asset`,
    },
  ]
  for (let i = 1; i <= p.chain.length; i++) {
    const detached = p.chain.slice(0, i).map(markerMoveLabel).join(', ')
    const rest = p.chain.length - i
    options.push({
      value: i,
      label: rest > 0 ? `Detach from ${detached} (keep/grow the other ${rest})` : `Detach from ${detached}`,
    })
  }
  return options
})

// ── Inserting new asset(s) between existing ones ────────────────────────────
// Mirrors the moveAsset/pendingAssetMove machinery above, but for insertion
// rather than reordering: dropping new asset(s) at position `index` (i.e.
// splice(index, 0, ...)) relative to a marker's [lo, hi] span can be:
//  - unaffected (index <= lo or index > hi + 1): outside the span entirely.
//  - forced (lo < index <= hi): strictly INSIDE the span -- there's no valid
//    "leave the marker alone" option, since that would make its assets list
//    non-contiguous, so the new asset(s) always join it (and, by
//    containment, every ancestor marker too -- see the proof this relies on
//    in the comment on InsertMarkerChain below).
//  - optional (index === lo or index === hi + 1): exactly at one edge --
//    the user chooses whether it extends the marker or not, same
//    grow-innermost-first chain UI as pendingAssetMove's detachCount.
interface InsertMarkerChain {
  /** 'before': markers whose span ENDS right where the insertion happens
   * (hi + 1 === index); 'after': markers whose span STARTS there
   * (lo === index). A single insertion point can have at most one chain of
   * each side (adjacent sibling marker trees meeting exactly at that point),
   * and a marker can never appear in both -- see planInsert. */
  side: 'before' | 'after'
  /** Innermost (smallest span) first -- same ordering rationale as
   * pendingAssetMove.chain: choosing to extend an inner marker but not its
   * container would violate the container's own contiguity. */
  markers: MarkerForm[]
  /** How many of `markers`, counting from the innermost, the user has
   * chosen to extend -- the rest are left untouched. Only 0..markers.length
   * are valid (same reasoning as PendingAssetMove.detachCount). */
  growCount: number
}

interface PendingAssetInsert {
  index: number
  newAssets: AssetForm[]
  extraMarkers: MarkerForm[]
  forced: MarkerForm[]
  chains: InsertMarkerChain[]
}
const pendingAssetInsert = ref<PendingAssetInsert | null>(null)

function planInsert(index: number, idToIndex: Map<string, number>): { forced: MarkerForm[]; chains: InsertMarkerChain[] } {
  const forced: MarkerForm[] = []
  const beforeCandidates: { m: MarkerForm; span: { lo: number; hi: number } }[] = []
  const afterCandidates: { m: MarkerForm; span: { lo: number; hi: number } }[] = []

  for (const m of form.markers) {
    const span = markerSpan(m, idToIndex)
    if (!span) continue
    if (span.lo < index && index <= span.hi) {
      forced.push(m)
    } else if (span.hi + 1 === index) {
      beforeCandidates.push({ m, span })
    } else if (span.lo === index) {
      afterCandidates.push({ m, span })
    }
  }

  function toChain(candidates: { m: MarkerForm; span: { lo: number; hi: number } }[], side: 'before' | 'after'): InsertMarkerChain | null {
    if (candidates.length === 0) return null
    candidates.sort((a, b) => (a.span.hi - a.span.lo) - (b.span.hi - b.span.lo))
    return { side, markers: candidates.map((c) => c.m), growCount: 0 }
  }

  const chains = [toChain(beforeCandidates, 'before'), toChain(afterCandidates, 'after')].filter(
    (c): c is InsertMarkerChain => c !== null,
  )
  return { forced, chains }
}

function applyInsert(p: PendingAssetInsert) {
  const newIds = p.newAssets.map((a) => a.id)
  p.forced.forEach((m) => {
    m.assets = [...m.assets, ...newIds]
  })
  p.chains.forEach((chain) => {
    chain.markers.slice(0, chain.growCount).forEach((m) => {
      m.assets = [...m.assets, ...newIds]
    })
  })
  form.assets.splice(p.index, 0, ...p.newAssets)
  form.markers.push(...p.extraMarkers)
  selectAsset(p.index + p.newAssets.length - 1)
}

/** Inserts `newAssets` (plus any `extraMarkers` already scoped to just that
 * batch, e.g. bootstrap's auto-detected ad-run markers -- always added
 * unconditionally, since they only reference ids within the new batch and
 * can't conflict with anything pre-existing) at `index`. Applies
 * immediately when no existing marker's edge sits exactly at `index`;
 * otherwise opens the pendingAssetInsert dialog for the user to choose. */
function insertAssetsAt(index: number, newAssets: AssetForm[], extraMarkers: MarkerForm[] = []) {
  const idToIndex = new Map(form.assets.map((a, i) => [a.id, i] as const))
  const { forced, chains } = planInsert(index, idToIndex)

  if (chains.length === 0) {
    applyInsert({ index, newAssets, extraMarkers, forced, chains })
    if (forced.length) {
      toast.add({
        severity: 'info',
        summary: `Added into ${forced.length} marker${forced.length > 1 ? 's' : ''} to keep it contiguous`,
        life: 3000,
      })
    }
    return
  }

  pendingAssetInsert.value = { index, newAssets, extraMarkers, forced, chains }
}

function confirmPendingAssetInsert() {
  const p = pendingAssetInsert.value
  if (!p) return
  applyInsert(p)
  pendingAssetInsert.value = null
}

function cancelPendingAssetInsert() {
  pendingAssetInsert.value = null
}

/** Radio options for one chain in the pending-insert dialog: 0 = leave every
 * marker in the chain unchanged, N = extend the N innermost markers to
 * include the new asset(s) (the rest left as-is) -- mirrors
 * pendingAssetMoveOptions, just growing instead of detaching. */
function insertChainOptions(chain: InsertMarkerChain): { value: number; label: string }[] {
  const options: { value: number; label: string }[] = [
    {
      value: 0,
      label: chain.markers.length === 1 ? `Leave "${markerMoveLabel(chain.markers[0])}" unchanged` : `Leave all ${chain.markers.length} markers unchanged`,
    },
  ]
  for (let i = 1; i <= chain.markers.length; i++) {
    const grown = chain.markers.slice(0, i).map(markerMoveLabel).join(', ')
    const rest = chain.markers.length - i
    options.push({
      value: i,
      label: rest > 0 ? `Extend ${grown} (leave the other ${rest} unchanged)` : `Extend ${grown}`,
    })
  }
  return options
}

// ── Splitting an asset into two ─────────────────────────────────────────────
const splitDialogIndex = ref<number | null>(null)
const splitPopupTarget = ref<HTMLElement | null>(null)
const splitFirstPartInput = ref('')
const splitError = ref('')

/** Best known total duration (seconds) for an asset: a resolved (ffprobe'd)
 * duration takes priority over the parsed `duration` field, same preference
 * order the timeline itself uses (see AssetTimeline.vue's segments). Null
 * if neither is known -- the split can still proceed, it just can't bound
 * the first part against the total or give the second part an explicit
 * duration (left blank, meaning "rest of the file"). */
function knownTotalSeconds(asset: AssetForm): number | null {
  const resolved = resolvedAssetDurations.value?.[asset.id]
  if (resolved !== undefined) return resolved
  return parseApproxSeconds(asset.duration)
}

function startSplit(payload: { index: number; target: HTMLElement }) {
  const index = payload.index
  const asset = form.assets[index]
  if (!asset) return
  splitDialogIndex.value = index
  splitPopupTarget.value = payload.target
  splitError.value = ''
  const total = knownTotalSeconds(asset)
  splitFirstPartInput.value = total !== null ? `${(total / 2).toFixed(2)}s` : ''
  confirm.require({
    target: payload.target,
    message: '',
    accept: () => undefined,
    reject: cancelSplit,
  })
  alignConfirmPopup(payload.target)
}

function cancelSplit() {
  splitDialogIndex.value = null
  splitPopupTarget.value = null
}

function acceptSplit(acceptCallback: () => void) {
  if (confirmSplit()) acceptCallback()
}

/** A fresh id for the split-off second half: "<original>-2", "-3", ... to
 * stay meaningful and avoid colliding with an id some other asset already
 * uses (however unlikely). */
function generateSplitId(baseId: string): string {
  const existing = new Set(form.assets.map((a) => a.id))
  let suffix = 2
  while (existing.has(`${baseId}-${suffix}`)) suffix++
  return `${baseId}-${suffix}`
}

function confirmSplit(): boolean {
  const index = splitDialogIndex.value
  if (index === null) return false
  const asset = form.assets[index]
  if (!asset) return false

  const firstPartSeconds = parseApproxSeconds(splitFirstPartInput.value)
  if (firstPartSeconds === null || firstPartSeconds <= 0) {
    splitError.value = 'Enter a valid duration (e.g. "90s", "1:30", "1 min 30 sec").'
    return false
  }

  const originalStartSeconds = asset.start.trim() ? parseApproxSeconds(asset.start) : 0
  if (originalStartSeconds === null) {
    splitError.value = `Can't parse this asset's own start ("${asset.start}") -- fix it before splitting.`
    return false
  }

  const totalSeconds = knownTotalSeconds(asset)
  if (totalSeconds !== null && firstPartSeconds >= totalSeconds) {
    splitError.value = `Must be less than the asset's total duration (${totalSeconds.toFixed(2)}s).`
    return false
  }

  const secondId = generateSplitId(asset.id)
  const first: AssetForm = { ...asset, duration: `${firstPartSeconds.toFixed(3)}s`, fade_out: '' }
  const second: AssetForm = {
    ...asset,
    id: secondId,
    start: `${(originalStartSeconds + firstPartSeconds).toFixed(3)}s`,
    duration: totalSeconds !== null ? `${(totalSeconds - firstPartSeconds).toFixed(3)}s` : '',
    fade_in: '',
  }

  form.assets.splice(index, 1, first, second)
  // Any marker covering the original asset must now cover both halves to
  // stay contiguous -- there's no ambiguity here (unlike insertAssetsAt),
  // since both halves always belong together.
  form.markers.forEach((m) => {
    if (m.assets.includes(asset.id)) m.assets = [...m.assets, secondId]
  })

  splitDialogIndex.value = null
  splitPopupTarget.value = null
  selectAsset(index)
  return true
}

function addRendition() {
  form.renditions.push({ name: '', resolution: '1280x720', bitrate_kbps: 4500 })
}
function removeRendition(i: number) {
  form.renditions.splice(i, 1)
}

// ── Markers (nested, multi-asset SCTE-35 spans) ────────────────────────────

const usedEventIds = computed(() => form.markers.map((m) => m.event_id))
const duplicateEventIds = computed(() => {
  const seen = new Set<number>()
  const duplicates = new Set<number>()
  for (const marker of form.markers) {
    if (seen.has(marker.event_id)) duplicates.add(marker.event_id)
    seen.add(marker.event_id)
  }
  return [...duplicates].sort((a, b) => a - b)
})
const semanticIssues = computed(() => {
  if (!form.enforce_scte35_marker_semantics) return []
  const idToIndex = new Map(form.assets.map((asset, index) => [asset.id, index] as const))
  const issues = semanticMarkerIssues(form.markers, idToIndex)
  for (const marker of form.markers) {
    const t = Number.parseInt(marker.segmentation.type_id, 16)
    if (marker.splice_type !== 'time_signal') continue
    if (form.scte35_numbering_scheme === 'AF2M_SNPTV' && ![0x22, 0x34, 0x30, 0x02].includes(t))
      issues.push(`Marker #${marker.event_id}: this segmentation type is not in the af2m profile.`)
    if (form.scte35_numbering_scheme === 'SCTE35_2019A' && [0x44, 0x46].includes(t))
      issues.push(`Marker #${marker.event_id}: Ad Block is unavailable in SCTE 35 2019a.`)
  }
  if (form.scte35_numbering_scheme === 'AF2M_SNPTV') {
    for (const marker of form.markers) {
      if (marker.splice_type !== 'time_signal' || Number.parseInt(marker.segmentation.type_id, 16) !== 0x30) continue
      const roles = new Set(marker.assets.map((id) => form.assets.find((asset) => asset.id === id)?.role === 'jingle'))
      if (roles.size > 1) issues.push(`Marker #${marker.event_id}: af2m Provider Advertisement covers both Jingle and non-Jingle assets.`)
    }
  }
  if (form.break_numbering_supported && form.scte35_numbering_scheme !== 'AF2M_SNPTV') {
    const programRanges = form.markers.filter((marker) => marker.splice_type === 'time_signal' && marker.segmentation.type_id === '0x10')
      .map((marker) => marker.assets.map((asset) => idToIndex.get(asset) ?? -1))
    const intervals = form.markers.filter((marker) => marker.splice_type === 'time_signal' && marker.segmentation.type_id === '0x22')
      .map((marker) => ({ lo: Math.min(...marker.assets.map((asset) => idToIndex.get(asset) ?? -1)), hi: Math.max(...marker.assets.map((asset) => idToIndex.get(asset) ?? -1)), interval: marker.break_interval }))
      .filter((br) => !programRanges.some((program) => Math.min(...program) <= br.lo && br.hi <= Math.max(...program)))
      .sort((a, b) => a.lo - b.lo)
      .map((br) => br.interval)
    if (intervals.some((interval, index) => index > 0 && interval < intervals[index - 1]))
      issues.push('Break intervals must increase in playback order outside a Program.')
  }
  return issues
})
const draftHasDuplicateEventId = computed(() => {
  const draft = markerDraft.value
  if (!draft) return false
  return form.markers.some((marker) =>
    marker.event_id === draft.event_id && marker._ui_id !== draft._ui_id,
  )
})

const resolvedMarkers = ref<ResolvedMarker[] | null>(null)
const resolvedAssetDurations = ref<Record<string, number> | null>(null)
const resolvingMarkers = ref(false)

function tagRange({ startIndex, endIndex }: { startIndex: number; endIndex: number }) {
  const assetIds = form.assets.slice(startIndex, endIndex + 1).map((a) => a.id)
  markerDraft.value = {
    _ui_id: generateMarkerUiId(),
    editingMarkerUiId: null,
    event_id: nextEventId(usedEventIds.value),
    splice_type: 'time_signal',
    assets: assetIds,
    segmentation: newSegmentation(),
    auto_return: true,
    break_interval: 1,
  }
  selectedMarkerEventId.value = markerDraft.value._ui_id
  selectedAssetIndex.value = null
}

function editMarker(eventId: number, markerUiId?: number) {
  const existing = form.markers.find((m) => markerUiId === undefined ? m.event_id === eventId : m._ui_id === markerUiId)
  if (!existing) return
  markerDraft.value = {
    _ui_id: existing._ui_id,
    editingMarkerUiId: existing._ui_id,
    event_id: existing.event_id,
    splice_type: existing.splice_type,
    assets: [...existing.assets],
    segmentation: { ...existing.segmentation },
    auto_return: existing.auto_return,
    break_interval: existing.break_interval,
  }
  selectedMarkerEventId.value = existing._ui_id
  selectedAssetIndex.value = null
}

function removeMarker(markerUiId: number) {
  form.markers = form.markers.filter((m) => m._ui_id !== markerUiId)
  if (markerDraft.value?._ui_id === markerUiId) markerDraft.value = null
  if (selectedMarkerEventId.value === markerUiId) selectedMarkerEventId.value = null
}

const markerDraft = ref<MarkerDraft | null>(null)

/** Asset-index bounds for the draft's current span. Marker assets are stored
 * by id, while the controls operate on the visible playlist order. */
const markerDraftBounds = computed(() => {
  const draft = markerDraft.value
  if (!draft) return null
  const indices = draft.assets
    .map((id) => form.assets.findIndex((asset) => asset.id === id))
    .filter((index) => index >= 0)
  if (!indices.length) return null
  return { lo: Math.min(...indices), hi: Math.max(...indices) }
})

/** A proposed marker span is valid when it is disjoint from every other
 * marker, contains it, or is contained by it. This mirrors franken-ts's
 * nested-or-disjoint rule and avoids letting a resize create partial overlap. */
function canSetMarkerDraftBounds(lo: number, hi: number): boolean {
  const draft = markerDraft.value
  if (!draft || lo < 0 || hi >= form.assets.length || lo > hi) return false
  if (!form.enforce_scte35_marker_semantics) return true
  const idToIndex = new Map(form.assets.map((asset, index) => [asset.id, index] as const))
  const adjusted: MarkerForm = {
    ...draft,
    assets: form.assets.slice(lo, hi + 1).map((asset) => asset.id),
  }
  const others = form.markers.filter((marker) => marker._ui_id !== draft._ui_id)
  return semanticMarkerIssues([...others, adjusted], idToIndex).length === 0
}

const canExtendDraftLeft = computed(() => {
  const bounds = markerDraftBounds.value
  return bounds !== null && canSetMarkerDraftBounds(bounds.lo - 1, bounds.hi)
})
const canShrinkDraftLeft = computed(() => {
  const bounds = markerDraftBounds.value
  return bounds !== null && bounds.lo < bounds.hi && canSetMarkerDraftBounds(bounds.lo + 1, bounds.hi)
})
const canExtendDraftRight = computed(() => {
  const bounds = markerDraftBounds.value
  return bounds !== null && canSetMarkerDraftBounds(bounds.lo, bounds.hi + 1)
})
const canShrinkDraftRight = computed(() => {
  const bounds = markerDraftBounds.value
  return bounds !== null && bounds.lo < bounds.hi && canSetMarkerDraftBounds(bounds.lo, bounds.hi - 1)
})

function adjustMarkerDraftSpan(edge: 'start' | 'end', direction: -1 | 1) {
  const draft = markerDraft.value
  const bounds = markerDraftBounds.value
  if (!draft || !bounds) return
  const lo = bounds.lo + (edge === 'start' ? direction : 0)
  const hi = bounds.hi + (edge === 'end' ? direction : 0)
  if (!canSetMarkerDraftBounds(lo, hi)) return
  const assets = form.assets.slice(lo, hi + 1).map((asset) => asset.id)
  draft.assets = assets

  // Span resizing is an in-memory playlist edit, not a temporary form draft:
  // retain it when the user selects another marker, while leaving persistence
  // to the playlist's normal Save action.
  if (draft.editingMarkerUiId !== null) {
    const marker = form.markers.find((item) => item._ui_id === draft.editingMarkerUiId)
    if (marker) marker.assets = [...assets]
  }
}

function resizeTimelineMarker(payload: { eventId: number; edge: 'start' | 'end'; direction: -1 | 1 }) {
  if (markerDraft.value?._ui_id !== payload.eventId) return
  adjustMarkerDraftSpan(payload.edge, payload.direction)
}

/** Show the edited marker's draft assets in the timeline immediately, while
 * keeping the form's committed marker unchanged until the user presses Update. */
const timelineMarkers = computed(() => {
  const draft = markerDraft.value
  if (!draft || draft.editingMarkerUiId === null) return form.markers
  return form.markers.map((marker) => marker._ui_id === draft.editingMarkerUiId
    ? { ...marker, assets: [...draft.assets] }
    : marker)
})

const markerResizeAvailability = computed(() => {
  if (markerDraft.value?._ui_id !== selectedMarkerEventId.value) return null
  return {
    extendStart: canExtendDraftLeft.value,
    shortenStart: canShrinkDraftLeft.value,
    shortenEnd: canShrinkDraftRight.value,
    extendEnd: canExtendDraftRight.value,
  }
})

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
  if (form.enforce_scte35_marker_semantics && draftHasDuplicateEventId.value) return false
  if (form.enforce_scte35_marker_semantics) {
    const d = markerDraft.value
    const withoutDraft = form.markers.filter((m) => m._ui_id !== d._ui_id)
    const draftMarker: MarkerForm = { ...d, assets: isDraftInstant.value ? d.assets.slice(0, 1) : d.assets }
    const idToIndex = new Map(form.assets.map((a, i) => [a.id, i] as const))
    if (semanticMarkerIssues([...withoutDraft, draftMarker], idToIndex).length) return false
    const t = Number.parseInt(d.segmentation.type_id, 16)
    if (d.splice_type === 'time_signal' && form.scte35_numbering_scheme === 'AF2M_SNPTV' && ![0x22, 0x34, 0x30, 0x02].includes(t)) return false
    if (d.splice_type === 'time_signal' && form.scte35_numbering_scheme === 'SCTE35_2019A' && [0x44, 0x46].includes(t)) return false
  }
  return true
})

const commitButtonLabel = computed(() => {
  if (!markerDraft.value) return ''
  if (markerDraft.value.editingMarkerUiId !== null) return 'Update'
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

  if (d.editingMarkerUiId !== null) {
    form.markers = form.markers.filter((m) => m._ui_id !== d.editingMarkerUiId)
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
        _ui_id: generateMarkerUiId(),
        event_id: eventId,
        splice_type: d.splice_type,
        assets: [assetId],
        segmentation: { ...d.segmentation },
        auto_return: d.auto_return,
        break_interval: d.break_interval,
      }
    })
    form.markers.push(...newMarkers)
  } else {
    form.markers.push({
      _ui_id: d._ui_id,
      event_id: d.event_id,
      splice_type: d.splice_type,
      assets: [...d.assets],
      segmentation: { ...d.segmentation },
      auto_return: d.auto_return,
      break_interval: d.break_interval,
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
  const assetRoles = new Map(form.assets.map((a) => [a.id, a.role] as const))
  return layoutMarkers(form.markers, idToIndex, form.enforce_scte35_marker_semantics, form.scte35_numbering_scheme, form.break_numbering_supported, assetRoles)
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

/** Live preview of the draft's containment (segment/depth), computed
 * against form.markers with the draft substituted in for whatever it's
 * replacing (or added fresh) -- lets the "Auto-computed" info in the
 * detail panel update as you edit, even before committing. Not used to
 * feed the graph/list (those only ever show committed markers). */
const draftPreviewSpan = computed(() => {
  const d = markerDraft.value
  if (!d) return null
  const withoutEditing = form.markers.filter((m) => m._ui_id !== d._ui_id)
  const draftAsMarker: MarkerForm = {
    _ui_id: d._ui_id,
    event_id: d.event_id,
    splice_type: d.splice_type,
    assets: isDraftInstant.value ? d.assets.slice(0, 1) : [...d.assets],
    segmentation: { ...d.segmentation },
    auto_return: d.auto_return,
    break_interval: d.break_interval,
  }
  const idToIndex = new Map(form.assets.map((a, i) => [a.id, i] as const))
  const assetRoles = new Map(form.assets.map((a) => [a.id, a.role] as const))
  const preview = layoutMarkers(
    [...withoutEditing, draftAsMarker], idToIndex, form.enforce_scte35_marker_semantics, form.scte35_numbering_scheme, form.break_numbering_supported, assetRoles,
  )
  return preview.find((s) => s.marker._ui_id === d._ui_id) ?? null
})

const draftResolved = computed(() => {
  if (!markerDraft.value || markerDraft.value.editingMarkerUiId === null) return undefined
  const index = form.markers.findIndex((m) => m._ui_id === markerDraft.value?._ui_id)
  return resolvedMarkers.value?.find((m) => m.marker_index === index)
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
      if (a.role !== null) asset.role = a.role
      if (a.start.trim()) asset.start = timeOrUndefined(a.start)
      if (a.duration.trim()) asset.duration = timeOrUndefined(a.duration)
      if (a.fade_in.trim()) asset.fade_in = timeOrUndefined(a.fade_in)
      if (a.fade_out.trim()) asset.fade_out = timeOrUndefined(a.fade_out)
      if (a.slate_image.trim()) asset.slate_image = a.slate_image
      if (a.no_osd) asset.no_osd = true
      if (a.osd_label.trim()) asset.osd_label = a.osd_label
      return asset
    })

  const cfg: Record<string, unknown> = { output, assets }
  cfg.enforce_scte35_marker_semantics = form.enforce_scte35_marker_semantics
  cfg.scte35_numbering_scheme = form.scte35_numbering_scheme
  cfg.break_numbering_supported = form.break_numbering_supported
  if (form.normalize) cfg.normalize = true
  if (form.slate_image.trim()) cfg.slate_image = form.slate_image
  if (form.osd.enabled) {
    // Every corner key is written explicitly (null for "none") rather than
    // omitted when empty -- franken_ts.config.OsdCornersConfig defaults
    // bottom_left/bottom_right to non-null, so omitting a key the user
    // deliberately cleared would silently revert it to that default on load.
    const corners: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(form.osd.corners)) {
      corners[key] = value || null
    }
    cfg.osd = {
      enabled: true,
      countdown: { ...form.osd.countdown },
      text_size_pct: form.osd.text_size_pct,
      text_color: form.osd.text_color,
      ad_break_label: form.osd.ad_break_label,
      corner_box: { ...form.osd.corner_box },
      corners,
    }
  }
  if (form.markers.length) {
    cfg.markers = form.markers.map((m) => {
      const marker: Record<string, unknown> = {
        event_id: m.event_id,
        splice_type: m.splice_type,
        assets: [...m.assets],
      }
      if (m.splice_type === 'time_signal') {
        const seg = { ...m.segmentation }
        if (form.enforce_scte35_marker_semantics) {
          delete seg.segment_num
          delete seg.segments_expected
          delete seg.sub_segment_num
          delete seg.sub_segments_expected
        }
        marker.segmentation = seg
        if (m.break_interval !== 1) marker.break_interval = m.break_interval
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
  form.enforce_scte35_marker_semantics = data.enforce_scte35_marker_semantics !== false
  form.scte35_numbering_scheme = (data.scte35_numbering_scheme as NumberingScheme) ?? 'SCTE35_2023R1'
  form.break_numbering_supported = data.break_numbering_supported === true
  form.slate_image = (data.slate_image as string) ?? ''

  const rawOsd = (data.osd as Record<string, unknown>) ?? {}
  const rawOsdCountdown = (rawOsd.countdown as Record<string, unknown>) ?? {}
  const rawOsdCornerBox = (rawOsd.corner_box as Record<string, unknown>) ?? {}
  const rawOsdCorners = (rawOsd.corners as Record<string, unknown>) ?? {}
  form.osd = {
    enabled: Boolean(rawOsd.enabled),
    countdown: {
      enabled: rawOsdCountdown.enabled !== undefined ? Boolean(rawOsdCountdown.enabled) : true,
      height_pct: (rawOsdCountdown.height_pct as number) ?? 5,
    },
    text_size_pct: (rawOsd.text_size_pct as number) ?? 4,
    text_color: (rawOsd.text_color as string) ?? '#FFFFFF',
    ad_break_label: (rawOsd.ad_break_label as string) ?? 'ad break',
    corner_box: {
      enabled: Boolean(rawOsdCornerBox.enabled),
      color: (rawOsdCornerBox.color as string) ?? '#000000',
    },
    corners: {
      // Matches franken_ts.config.OsdCornersConfig's per-field defaults --
      // these apply only when the corner key is ABSENT (not when it's
      // explicitly null, which means the user deliberately cleared it).
      top_left: rawOsdCorners.top_left !== undefined ? ((rawOsdCorners.top_left as CornerContent) ?? '') : '',
      top_right: rawOsdCorners.top_right !== undefined ? ((rawOsdCorners.top_right as CornerContent) ?? '') : '',
      bottom_left:
        rawOsdCorners.bottom_left !== undefined ? ((rawOsdCorners.bottom_left as CornerContent) ?? '') : 'asset_id',
      bottom_right:
        rawOsdCorners.bottom_right !== undefined ? ((rawOsdCorners.bottom_right as CornerContent) ?? '') : 'time',
    },
  }

  const rawAssets = (data.assets as Record<string, unknown>[]) ?? []
  const rawMarkers = (data.markers as Record<string, unknown>[]) ?? []
  const legacyRoles = new Map<string, AssetRole>()
  for (const marker of rawMarkers) {
    const ids = marker.assets as string[] | undefined
    if (marker.jingle_role === 'opening' || marker.jingle_role === 'closing')
      for (const id of ids ?? []) legacyRoles.set(id, 'jingle')
  }
  form.assets = rawAssets.map((a) => ({
    // Playlists saved before this field existed won't have an id --
    // generate one so markers can still be tagged against these assets;
    // it gets written back on next save.
    id: a.id ? String(a.id) : generateAssetId(),
    file: String(a.file ?? ''),
    role: (a.role as AssetRole | null) ?? (a.jingle_role === 'opening' || a.jingle_role === 'closing' ? 'jingle' : null) ?? legacyRoles.get(String(a.id)) ?? null,
    start: a.start !== undefined ? String(a.start) : '',
    duration: a.duration !== undefined ? String(a.duration) : '',
    fade_in: a.fade_in !== undefined ? String(a.fade_in) : '',
    fade_out: a.fade_out !== undefined ? String(a.fade_out) : '',
    slate_image: String(a.slate_image ?? ''),
    no_osd: Boolean(a.no_osd),
    osd_label: a.osd_label !== undefined ? String(a.osd_label) : '',
  }))

  form.markers = rawMarkers.map((m) => {
    const seg = (m.segmentation as Record<string, unknown>) ?? {}
    return {
      _ui_id: generateMarkerUiId(),
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
        segment_num: seg.segment_num == null ? null : Number(seg.segment_num),
        segments_expected: seg.segments_expected == null ? null : Number(seg.segments_expected),
        sub_segment_num: seg.sub_segment_num == null ? null : Number(seg.sub_segment_num),
        sub_segments_expected: seg.sub_segments_expected == null ? null : Number(seg.sub_segments_expected),
      },
      auto_return: m.auto_return !== false,
      break_interval: Number(m.break_interval ?? 1),
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
  previewError.value = false
  previewVersion.value = Date.now()
  refreshPreviewStatus()
  refreshScteVerifyStatus()
  refreshOutputStatus()
  if (!props.name) {
    resetForm()
    savedSnapshot.value = currentSnapshot()
    return
  }
  loading.value = true
  error.value = ''
  try {
    const data = await getPlaylist(props.name)
    fromYamlPlaylist(data)
    savedSnapshot.value = currentSnapshot()
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
  if (form.enforce_scte35_marker_semantics && semanticIssues.value.length) {
    error.value = semanticIssues.value.join(' ')
    return
  }
  if (!form.enforce_scte35_marker_semantics && duplicateEventIds.value.length) {
    error.value = `Relaxed mode allows duplicate IDs to be saved, but franken-ts cannot build them safely yet. Assign unique event ID(s): ${duplicateEventIds.value.join(', ')}`
    return
  }
  saving.value = true
  error.value = ''
  try {
    const savedName = nameInput.value.trim()
    await savePlaylist(savedName, toYamlPlaylist())
    savedSnapshot.value = currentSnapshot()
    // The save just bumped the playlist YAML's mtime past any existing
    // preview .mp4's -- re-check server-side status so a stale preview
    // (rendered from the pre-save version) gets hidden.
    refreshPreviewStatus()
    refreshOutputStatus()
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

/** Discards unsaved edits by re-running the same load path used on mount --
 * re-fetches from the server for an existing playlist, or resets to blank
 * for a new one -- so this always lands back on exactly the saved state. */
async function revertChanges() {
  await load()
}

// --- Build (franken-ts, runs as a background job -- builds whatever's ----
// currently saved on disk, NOT unsaved in-progress edits) ------------------
const building = ref(false)
const buildJobId = ref<string | null>(null)
const verifyingScte = ref(false)
const scteVerifyJobId = ref<string | null>(null)
// True from the moment a build job is spawned until it finishes -- the
// preview player is removed for this whole window (not just while the
// POST is in flight, unlike `building`) since the .ts/.preview.mp4 files
// it points at are being overwritten mid-build.
const buildRunning = ref(false)
// Server-computed (mtime-based, see igor's preview_status) rather than
// tracked purely client-side -- stays correct across page reloads and
// other browser tabs/clients editing the same playlist, not just this
// session's save/build actions.
const previewStatus = ref<{ exists: boolean; stale: boolean } | null>(null)
const scteVerifyStatus = ref<{ exists: boolean; stale: boolean } | null>(null)
const outputStatus = ref<{ exists: boolean; stale: boolean } | null>(null)
async function refreshPreviewStatus() {
  if (!props.name) {
    previewStatus.value = null
    return
  }
  try {
    previewStatus.value = await getPreviewStatus(props.name)
  } catch {
    previewStatus.value = null
  }
}
async function refreshScteVerifyStatus() {
  if (!props.name) {
    scteVerifyStatus.value = null
    return
  }
  try {
    scteVerifyStatus.value = await getScteVerifyStatus(props.name)
  } catch {
    scteVerifyStatus.value = null
  }
}
async function refreshOutputStatus() {
  if (!props.name) {
    outputStatus.value = null
    return
  }
  try {
    outputStatus.value = await getOutputStatus(props.name)
  } catch {
    outputStatus.value = null
  }
}
// Bumped whenever a build finishes successfully, and appended to the
// preview <video>'s src as a cache-busting query param -- otherwise the
// browser happily keeps showing a stale cached preview after a rebuild
// (the URL itself never changes across builds).
const previewVersion = ref(Date.now())
const scteVerifyVersion = ref(Date.now())
const previewUrl = computed(() =>
  props.name ? playlistPreviewUrl(props.name, previewVersion.value) : null,
)
const scteVerifyUrl = computed(() =>
  props.name ? scteVerifyReportUrl(props.name, scteVerifyVersion.value) : null,
)
const scteVerifyCardsUrl = computed(() =>
  props.name ? scteVerifyReportUrl(props.name, scteVerifyVersion.value, 'cards') : null,
)
const assembleLabel = computed(() =>
  outputStatus.value?.exists && !outputStatus.value.stale ? 'Reassemble' : 'Assemble',
)

async function build() {
  if (!props.name) return
  building.value = true
  buildRunning.value = true
  previewStatus.value = null
  scteVerifyStatus.value = null
  outputStatus.value = null
  scteVerifyJobId.value = null
  error.value = ''
  try {
    const job = await buildPlaylist(props.name)
    buildJobId.value = job.id
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    buildRunning.value = false
  } finally {
    building.value = false
  }
}

function onBuildFinished(job: Job) {
  buildRunning.value = false
  if (job.status !== 'succeeded') {
    error.value = 'Assemble failed -- see log below.'
  } else {
    previewError.value = false
    previewVersion.value = Date.now()
    refreshPreviewStatus()
    refreshScteVerifyStatus()
    refreshOutputStatus()
  }
}

async function runScteVerify() {
  if (!props.name || isDirty.value) return
  verifyingScte.value = true
  error.value = ''
  try {
    const job = await buildScteVerify(props.name)
    scteVerifyJobId.value = job.id
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    verifyingScte.value = false
  }
}

// The report is served same-origin, so the frame can be sized to its content
// (and kept in sync if the content reflows, e.g. on window resize).
function fitFrameToContent(e: Event) {
  const frame = e.target as HTMLIFrameElement
  const body = frame.contentDocument?.body
  if (!body) return
  const fit = () => { frame.style.height = `${body.offsetHeight + 2}px` }
  fit()
  new ResizeObserver(fit).observe(body)
}

function onScteVerifyFinished(job: Job) {
  verifyingScte.value = false
  if (job.status !== 'succeeded') {
    error.value = 'SCTE-35 verify failed -- see log below.'
  } else {
    scteVerifyVersion.value = Date.now()
    refreshScteVerifyStatus()
  }
}

const previewError = ref(false)
function onPreviewError() {
  previewError.value = true
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
        <h2 class="m-0">{{ isNew ? 'New playlist' : name }}</h2>
      </div>
      <div class="flex align-items-center gap-2">
        <span v-if="isDirty" class="text-color-secondary text-sm">Unsaved changes</span>
        <Button
          label="Revert changes"
          icon="pi pi-undo"
          severity="secondary"
          outlined
          :loading="loading"
          :disabled="!isDirty"
          @click="revertChanges"
        />
        <Button label="Save playlist" icon="pi pi-check" :loading="saving" :disabled="!isDirty" @click="save" />
      </div>
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <Tabs v-model:value="activeTab">
      <TabList>
        <Tab value="settings">Settings</Tab>
        <Tab value="osd">OSD</Tab>
        <Tab value="assets">
          Playlist
          <span class="asset-count-badge">{{ form.assets.length }}</span>
        </Tab>
        <Tab value="build">Assemble</Tab>
      </TabList>

      <TabPanels>
        <TabPanel value="settings">
          <div class="flex flex-column gap-4" style="max-width: 56rem">
            <div class="flex flex-column gap-1">
              <label for="playlist-name">Playlist name</label>
              <InputText id="playlist-name" v-model="nameInput" :disabled="!isNew" placeholder="my-stream" class="field-medium" />
            </div>

            <Divider align="left"><span class="font-bold">Output</span></Divider>

            <div class="flex gap-4 flex-wrap">
              <div class="flex flex-column gap-1">
                <label>Framerate</label>
                <InputNumber
                  v-model="form.output.framerate"
                  :use-grouping="false"
                  :min-fraction-digits="0"
                  :max-fraction-digits="3"
                  class="field-tiny"
                />
              </div>
              <div class="flex flex-column gap-1">
                <label>GOP (default = framerate x2)</label>
                <InputNumber
                  v-model="form.output.gop"
                  :use-grouping="false"
                  :min-fraction-digits="0"
                  :max-fraction-digits="0"
                  placeholder="auto"
                  class="field-tiny"
                />
              </div>
            </div>

            <div class="flex gap-4 flex-wrap">
              <div class="flex flex-column gap-1">
                <label>Service provider</label>
                <InputText v-model="form.output.service_provider" placeholder="e.g. broadpeak" class="field-medium" />
              </div>
              <div class="flex flex-column gap-1">
                <label>Service name</label>
                <InputText v-model="form.output.service_name" placeholder="e.g. broadpeak.io" class="field-medium" />
              </div>
            </div>

            <Divider />

            <div class="flex gap-3 align-items-center">
              <label><input type="radio" value="single" v-model="outputMode" /> Single file (one bitrate)</label>
              <label><input type="radio" value="ladder" v-model="outputMode" /> ABR ladder (multi-rendition, for ecs-express)</label>
            </div>

            <div class="flex gap-4 flex-wrap">
              <div v-if="outputMode === 'single'" class="flex flex-column gap-1">
                <label>Output file</label>
                <InputText v-model="form.output.file" class="field-medium" />
              </div>
              <div v-if="outputMode === 'single'" class="flex flex-column gap-1">
                <label>Resolution</label>
                <InputText v-model="form.output.resolution" class="field-small" />
              </div>
              <div v-if="outputMode === 'single'" class="flex flex-column gap-1">
                <label>Bitrate (kbps)</label>
                <InputNumber v-model="form.output.bitrate_kbps" :use-grouping="false" :min-fraction-digits="0" :max-fraction-digits="0" class="field-small" />
              </div>
              <div v-if="outputMode === 'ladder'" class="flex flex-column gap-1">
                <label>Output directory</label>
                <InputText v-model="form.output.dir" class="field-medium" />
              </div>
            </div>

            <template v-if="outputMode === 'ladder'">
              <div class="flex justify-content-between align-items-center">
                <span class="font-bold">Renditions</span>
                <Button label="Add rendition" icon="pi pi-plus" size="small" text @click="addRendition" />
              </div>
              <div v-for="(r, i) in form.renditions" :key="i" class="flex gap-4 align-items-end flex-wrap">
                <div class="flex flex-column gap-1"><label>Name</label><InputText v-model="r.name" class="field-medium" /></div>
                <div class="flex flex-column gap-1"><label>Resolution</label><InputText v-model="r.resolution" class="field-small" /></div>
                <div class="flex flex-column gap-1">
                  <label>Bitrate (kbps)</label>
                  <InputNumber v-model="r.bitrate_kbps" :use-grouping="false" :min-fraction-digits="0" :max-fraction-digits="0" class="field-small" />
                </div>
                <Button icon="pi pi-trash" severity="danger" text @click="removeRendition(i)" />
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

        <TabPanel value="osd">
          <div class="flex flex-column gap-4">
            <div class="flex align-items-center gap-2">
              <Checkbox v-model="form.osd.enabled" binary input-id="osd-enabled" />
              <label for="osd-enabled">Enabled (applies to every asset except those with "no OSD" set)</label>
            </div>

            <div class="flex gap-5 flex-wrap">
              <div class="flex flex-column gap-2" style="width: 32rem; max-width: 100%">
                <!-- Corner pickers sit outside the frame (so they never cover the
                     preview), but left/right-align with the frame's own corners. -->
                <div class="flex justify-content-between gap-2">
                  <Select
                    v-model="form.osd.corners.top_left"
                    :options="CORNER_CONTENT_OPTIONS"
                    option-label="label"
                    option-value="value"
                    :disabled="!form.osd.enabled"
                    size="small"
                  />
                  <Select
                    v-model="form.osd.corners.top_right"
                    :options="CORNER_CONTENT_OPTIONS"
                    option-label="label"
                    option-value="value"
                    :disabled="!form.osd.enabled"
                    size="small"
                  />
                </div>

                <div class="osd-frame-mockup" :class="{ 'osd-frame-disabled': !form.osd.enabled }">
                  <div
                    v-if="form.osd.corners.top_left"
                    class="osd-preview-text osd-preview-tl"
                    :style="osdPreviewTextStyle('left')"
                  >{{ previewTextFor('top_left') }}</div>
                  <div
                    v-if="form.osd.corners.top_right"
                    class="osd-preview-text osd-preview-tr"
                    :style="osdPreviewTextStyle('right')"
                  >{{ previewTextFor('top_right') }}</div>
                  <div
                    v-if="form.osd.corners.bottom_left"
                    class="osd-preview-text osd-preview-bl"
                    :style="{ ...osdPreviewTextStyle('left'), bottom: osdPreviewBottomOffset }"
                  >{{ previewTextFor('bottom_left') }}</div>
                  <div
                    v-if="form.osd.corners.bottom_right"
                    class="osd-preview-text osd-preview-br"
                    :style="{ ...osdPreviewTextStyle('right'), bottom: osdPreviewBottomOffset }"
                  >{{ previewTextFor('bottom_right') }}</div>

                  <div
                    v-if="form.osd.countdown.enabled"
                    class="osd-bar-preview"
                    :style="{ height: form.osd.countdown.height_pct + '%' }"
                  />
                </div>

                <div class="flex justify-content-between gap-2">
                  <Select
                    v-model="form.osd.corners.bottom_left"
                    :options="CORNER_CONTENT_OPTIONS"
                    option-label="label"
                    option-value="value"
                    :disabled="!form.osd.enabled"
                    size="small"
                  />
                  <Select
                    v-model="form.osd.corners.bottom_right"
                    :options="CORNER_CONTENT_OPTIONS"
                    option-label="label"
                    option-value="value"
                    :disabled="!form.osd.enabled"
                    size="small"
                  />
                </div>
              </div>

              <!-- Global options: a real 4-column CSS grid (label, control,
                   secondary label, secondary control), not flexbox rows --
                   flex rows let each row's control stretch to whatever
                   space its own siblings left over, which is what made
                   "Text size" balloon while paired rows stayed narrow.
                   Every row always emits all 4 cells (empty <span>s for
                   rows with nothing in columns 3/4) so grid auto-flow
                   places exactly one logical row per 4 cells, and
                   `justify-items: start` stops every cell from stretching
                   to fill its column. -->
              <div class="osd-options-grid">
                <label class="osd-option-label">Text size</label>
                <InputGroup class="osd-narrow-pct">
                  <InputNumber v-model="form.osd.text_size_pct" :min="0" :max="100" :disabled="!form.osd.enabled" />
                  <InputGroupAddon>%</InputGroupAddon>
                </InputGroup>
                <span /><span />

                <label class="osd-option-label">Text color</label>
                <div class="flex align-items-center gap-2">
                  <ColorPicker v-model="osdTextColorHex" :disabled="!form.osd.enabled" />
                  <InputText v-model="form.osd.text_color" :disabled="!form.osd.enabled" class="osd-narrow-hex" />
                </div>
                <span /><span />

                <label class="osd-option-label" for="osd-corner-box-enabled">Box background</label>
                <Checkbox v-model="form.osd.corner_box.enabled" binary input-id="osd-corner-box-enabled" :disabled="!form.osd.enabled" />
                <label class="osd-option-label-secondary">Border color</label>
                <div class="flex align-items-center gap-2">
                  <ColorPicker v-model="osdBoxColorHex" :disabled="!form.osd.enabled || !form.osd.corner_box.enabled" />
                  <InputText v-model="form.osd.corner_box.color" :disabled="!form.osd.enabled || !form.osd.corner_box.enabled" class="osd-narrow-hex" />
                </div>

                <label class="osd-option-label">Ad break label</label>
                <InputText v-model="form.osd.ad_break_label" :disabled="!form.osd.enabled" placeholder="ad break" class="osd-narrow-hex" />
                <span /><span />

                <label class="osd-option-label" for="osd-countdown-enabled">Countdown bar</label>
                <Checkbox v-model="form.osd.countdown.enabled" binary input-id="osd-countdown-enabled" :disabled="!form.osd.enabled" />
                <label class="osd-option-label-secondary">Bar height</label>
                <InputGroup class="osd-narrow-pct">
                  <InputNumber
                    v-model="form.osd.countdown.height_pct"
                    :min="0" :max="100"
                    :disabled="!form.osd.enabled || !form.osd.countdown.enabled"
                  />
                  <InputGroupAddon>%</InputGroupAddon>
                </InputGroup>
              </div>
            </div>
          </div>
        </TabPanel>

        <TabPanel value="assets">
          <div class="flex flex-column gap-3">
            <div class="timeline-sticky">
              <AssetTimeline
                :assets="form.assets"
                :selected-index="selectedAssetIndex"
                :highlighted-asset-id="highlightedMovedAssetId"
                :markers="timelineMarkers"
                :enforce-scte35-marker-semantics="form.enforce_scte35_marker_semantics"
                :scte35-numbering-scheme="form.scte35_numbering_scheme"
                :break-numbering-supported="form.break_numbering_supported"
                :resolved-durations="resolvedAssetDurations ?? undefined"
                :selected-marker-event-id="selectedMarkerEventId"
                :marker-resize-availability="markerResizeAvailability"
                :hovered-marker-event-id="hoveredMarkerEventId"
                @select="selectAsset"
                @move-asset="({ index, direction }) => moveAsset(index, direction)"
                @add="addAsset"
                @bootstrap="openBootstrap"
                @split="startSplit"
                @tag-range="tagRange"
                @edit-marker="(uiId) => { const marker = form.markers.find((m) => m._ui_id === uiId); if (marker) editMarker(marker.event_id, uiId) }"
                @resize-marker="resizeTimelineMarker"
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

            <div
              v-if="selectedAssetIndex === null && markerDraft === null"
              class="asset-empty-state"
              :class="{ 'asset-empty-state-drop-active': emptyAssetDropActive }"
              @dragover="handleEmptyAssetDragOver"
              @dragleave="handleEmptyAssetDragLeave"
              @drop="handleEmptyAssetDrop"
            >
              <i class="pi pi-images" style="font-size: 1.5rem" />
              <span>{{ form.assets.length === 0 ? 'No assets yet.' : 'Select an asset above to edit it, or shift-click a range of assets to tag a marker.' }}</span>
              <span v-if="emptyAssetDropBusy" class="text-sm text-color-secondary">
                <i class="pi pi-spin pi-spinner" /> Uploading dropped file(s)...
              </span>
              <span v-else class="text-sm text-color-secondary">Drop local file(s) here to add them</span>
              <Message v-if="emptyAssetDropError" severity="error" class="text-sm m-0">{{ emptyAssetDropError }}</Message>
              <div class="flex gap-2">
                <Button label="Add asset" icon="pi pi-plus" outlined @click="addAsset()" />
                <Button label="Bootstrap from files" icon="pi pi-list" outlined severity="secondary" @click="openBootstrap()" />
              </div>
            </div>

            <div v-else-if="markerDraft" class="p-3 border-1 surface-border border-round flex flex-column gap-2 detail-panel">
              <div class="flex justify-content-between align-items-center">
                <span class="font-semibold">{{ markerDraft.editingMarkerUiId !== null ? `Marker #${markerDraft.event_id}` : 'New marker' }}</span>
                <Button
                  v-if="markerDraft.editingMarkerUiId !== null"
                  icon="pi pi-trash"
                  severity="danger"
                  text
                  @click="removeMarker(markerDraft._ui_id)"
                />
              </div>
              <span class="text-sm text-color-secondary">
                Covers: {{ markerDraft.assets.join(', ') }} -- span/nesting is derived from these assets, never
                set directly.
              </span>
              <div class="grid">
                <div class="col-4 flex flex-column gap-1">
                  <label>Event ID</label>
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
                      :options="SEGMENTATION_PAIR_OPTIONS.filter((option) => form.scte35_numbering_scheme === 'AF2M_SNPTV' ? ['0x22', '0x34', '0x30', '0x02'].includes(option.value) : form.scte35_numbering_scheme === 'SCTE35_2019A' ? !['0x44', '0x46'].includes(option.value) : true)"
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
                  <div v-if="markerDraft.segmentation.type_id === '0x22' && form.scte35_numbering_scheme !== 'AF2M_SNPTV' && form.break_numbering_supported" class="col-6 flex flex-column gap-1">
                    <label>Provider-defined Break interval</label>
                    <InputNumber v-model="markerDraft.break_interval" :min="1" :use-grouping="false" />
                  </div>
                  <div v-if="!form.enforce_scte35_marker_semantics" class="col-12 flex gap-3 flex-wrap">
                    <div v-for="field in (['segment_num', 'segments_expected', 'sub_segment_num', 'sub_segments_expected'] as const)" :key="field" class="flex flex-column gap-1">
                      <label>{{ field }}</label>
                      <InputNumber v-model="markerDraft.segmentation[field]" :min="0" :max="255" :use-grouping="false" placeholder="unset" class="field-tiny" />
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
                  <span class="font-semibold text-sm">{{ form.enforce_scte35_marker_semantics ? 'Auto-computed numbering:' : 'Authored numbering:' }}</span>
                  <span class="text-sm text-color-secondary">
                    segment {{ draftPreviewSpan?.segmentNum ?? 0 }}/{{ draftPreviewSpan?.segmentsExpected ?? 0 }}
                    <template v-if="draftPreviewSpan?.subSegmentNum != null"> · sub {{ draftPreviewSpan.subSegmentNum }}/{{ draftPreviewSpan.subSegmentsExpected }}</template>,
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
                    title="Select previous asset"
                    :disabled="selectedAssetIndex === 0"
                    @click="selectAsset(selectedAssetIndex - 1)"
                  />
                  <Button
                    icon="pi pi-chevron-right"
                    text
                    title="Select next asset"
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
                  <InputText
                    :model-value="form.assets[selectedAssetIndex].id"
                    @update:model-value="updateAssetId(selectedAssetIndex, String($event ?? ''))"
                  />
                </div>
                <div class="col-4 flex flex-column gap-1">
                  <label for="asset-role">Role</label>
                  <Select input-id="asset-role" v-model="form.assets[selectedAssetIndex].role" :options="ASSET_ROLE_OPTIONS" option-label="label" option-value="value" />
                </div>
                <div class="col-3 flex flex-column gap-1">
                  <label>Start</label>
                  <InputText v-model="form.assets[selectedAssetIndex].start" placeholder="00:00:00 / 10 min" />
                </div>
                <div class="col-3 flex flex-column gap-1">
                  <label>Duration</label>
                  <InputText v-model="form.assets[selectedAssetIndex].duration" placeholder="10 min" />
                </div>
                <div class="col-3 flex flex-column gap-1">
                  <label>Fade in (s)</label>
                  <InputText v-model="form.assets[selectedAssetIndex].fade_in" placeholder="1.5" />
                </div>
                <div class="col-3 flex flex-column gap-1">
                  <label>Fade out (s)</label>
                  <InputText v-model="form.assets[selectedAssetIndex].fade_out" placeholder="1.5" />
                </div>
                <div class="col-12 flex flex-column gap-1">
                  <label>Per-asset slate image (overrides global)</label>
                  <AssetFileField v-model="form.assets[selectedAssetIndex].slate_image" />
                </div>
                <div class="col-12 flex align-items-center gap-4">
                  <div class="flex align-items-center gap-2 flex-shrink-0">
                    <Checkbox v-model="form.assets[selectedAssetIndex].no_osd" binary input-id="asset-no-osd" />
                    <label for="asset-no-osd" class="white-space-nowrap">No OSD (suppress the on-screen display entirely for this asset)</label>
                  </div>
                  <div class="flex flex-column gap-1 flex-grow-1">
                    <label>OSD label (free text, shown by an "OSD label" corner)</label>
                    <InputText
                      v-model="form.assets[selectedAssetIndex].osd_label"
                      placeholder="e.g. Weather"
                      :disabled="form.assets[selectedAssetIndex].no_osd"
                    />
                  </div>
                </div>
              </div>
            </div>

            <div class="marker-section">
              <div class="flex flex-column gap-2">
                <span class="font-bold">Markers ({{ form.markers.length }})</span>
                <Message v-if="duplicateEventIds.length" severity="warn" class="text-sm m-0">
                  Duplicate event ID(s): {{ duplicateEventIds.join(', ') }}.
                  {{ form.enforce_scte35_marker_semantics ? 'Resolve these before saving.' : 'Allowed to save while semantic enforcement is off, but franken-ts cannot build duplicate IDs safely yet.' }}
                </Message>
                <Message v-for="(issue, i) in semanticIssues" :key="`semantic-${i}`" severity="error" class="text-sm m-0">
                  {{ issue }}
                </Message>
                <div
                  v-for="span in markerListOrder"
                  :key="span.marker._ui_id"
                  class="marker-row"
                  :class="{
                    'marker-row-selected': span.marker._ui_id === selectedMarkerEventId,
                    'marker-row-hovered': span.marker._ui_id === hoveredMarkerEventId,
                  }"
                  :style="{ paddingLeft: `${span.depth * 1.25}rem` }"
                  @click="editMarker(span.marker.event_id, span.marker._ui_id)"
                  @mouseenter="hoverMarkerFromList(span.marker._ui_id)"
                  @mouseleave="hoverMarkerFromList(null)"
                >
                  <span class="marker-type-dot" :style="{ background: laneColorForBadge(span.marker) }" />
                  <span class="font-semibold">{{ laneLabelForMarker(span.marker) }} #{{ span.marker.event_id }}</span>
                  <span class="text-color-secondary text-sm">{{ span.marker.assets.join(', ') }}</span>
                  <span class="text-color-secondary text-sm">seg {{ span.segmentNum }}/{{ span.segmentsExpected }}<template v-if="span.subSegmentNum != null"> · sub {{ span.subSegmentNum }}/{{ span.subSegmentsExpected }}</template></span>
                </div>
              </div>
              <div class="scte-panel flex flex-column gap-3 p-3 border-1 surface-border border-round">
                <span class="font-bold">SCTE-35</span>
                <div class="flex align-items-center gap-2">
                  <Checkbox v-model="form.enforce_scte35_marker_semantics" binary input-id="marker-semantics" />
                  <label for="marker-semantics">Enforce marker semantics</label>
                </div>
                <div class="flex flex-column gap-1">
                  <label for="numbering-scheme">Numbering scheme</label>
                  <Select input-id="numbering-scheme" v-model="form.scte35_numbering_scheme" :options="NUMBERING_OPTIONS" option-label="label" option-value="value" />
                </div>
                <div v-if="form.scte35_numbering_scheme !== 'AF2M_SNPTV'" class="flex align-items-center gap-2">
                  <Checkbox v-model="form.break_numbering_supported" binary input-id="break-numbering" :disabled="!form.enforce_scte35_marker_semantics" />
                  <label for="break-numbering">Number Breaks within Program / interval</label>
                </div>
                <span class="text-sm text-color-secondary">{{ form.enforce_scte35_marker_semantics ? 'The selected scheme computes numbering on save and build.' : 'Semantic checks and automatic numbering are off; authored fields pass through.' }}</span>
              </div>
            </div>
          </div>
        </TabPanel>

        <TabPanel value="build">
          <div class="flex flex-column gap-3">
            <p class="text-color-secondary m-0">
              Runs franken-ts against the saved playlist and produces the <code>.ts</code> file its-a-live
              channels stage via Spark. Assembles whatever's currently saved on disk -- save first if you
              have unsaved changes above.
            </p>
            <div class="flex align-items-center gap-2">
              <Button :label="assembleLabel" icon="pi pi-cog" :loading="building" :disabled="isNew || isDirty || verifyingScte" @click="build" />
              <span v-if="isNew" class="text-color-secondary text-sm">Save the playlist first.</span>
              <span v-else-if="isDirty" class="text-color-secondary text-sm">Save your changes first.</span>
            </div>
            <JobPanel v-if="buildJobId" :job-id="buildJobId" @finished="onBuildFinished" />

            <div
              v-if="previewStatus?.exists && !previewStatus.stale && previewUrl && !previewError && !buildRunning"
              class="flex flex-column gap-2"
            >
              <span class="font-bold">Preview</span>
              <video
                :key="previewUrl"
                :src="previewUrl"
                controls
                autoplay
                muted
                preload="metadata"
                class="preview-video"
                @error="onPreviewError"
              />
              <span class="text-color-secondary text-sm">
                Quick 540p sanity-check render (fast preset, low quality) -- not representative of the
                real output's bitrate/quality.
              </span>
            </div>

            <div v-if="outputStatus?.exists && !outputStatus.stale && !buildRunning" class="flex flex-column gap-2">
              <span class="font-bold">Verification</span>
              <span class="text-color-secondary text-sm">
                Inspector Krogh scans the assembled TS itself for its actual SCTE-35 markers, independently of
                franken-ts, and extracts frames around every splice boundary. If the build's markers.json sits
                next to the TS, every marker is also checked against what was intended (time, type, duration,
                UPID, segment numbers, flags).
              </span>
              <div class="flex align-items-center gap-2">
                <Button
                  label="Verify SCTE-35 markers"
                  icon="pi pi-shield"
                  severity="secondary"
                  outlined
                  :loading="verifyingScte"
                  :disabled="isNew || isDirty || building"
                  @click="runScteVerify"
                  style="width: fit-content"
                />
                <Button
                  v-if="scteVerifyStatus?.exists && !scteVerifyStatus.stale && scteVerifyUrl && !verifyingScte"
                  as="a"
                  :href="scteVerifyUrl"
                  target="_blank"
                  rel="noopener"
                  label="Open in new tab"
                  icon="pi pi-external-link"
                  severity="secondary"
                  outlined
                  class="report-open-button"
                />
                <Button
                  v-if="scteVerifyStatus?.exists && !scteVerifyStatus.stale && scteVerifyCardsUrl && !verifyingScte"
                  as="a"
                  :href="scteVerifyCardsUrl"
                  target="_blank"
                  rel="noopener"
                  label="Detailed report"
                  icon="pi pi-list"
                  severity="secondary"
                  text
                />
              </div>
            </div>
            <div v-if="scteVerifyStatus?.exists && !scteVerifyStatus.stale && scteVerifyUrl && !buildRunning && !verifyingScte" class="flex flex-column gap-2">
              <iframe :src="scteVerifyUrl" title="krogh SCTE-35 filmstrip" class="report-frame" @load="fitFrameToContent" />
            </div>
            <JobPanel v-if="scteVerifyJobId" :job-id="scteVerifyJobId" @finished="onScteVerifyFinished" />
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

    <Dialog
      :visible="pendingAssetMove !== null"
      modal
      header="This move crosses a marker boundary"
      :style="{ width: '32rem' }"
      @update:visible="cancelPendingAssetMove"
    >
      <div v-if="pendingAssetMove" class="flex flex-column gap-3">
        <p class="text-sm text-color-secondary m-0">
          The asset being moved is at the edge of {{ pendingAssetMove.chain.length > 1 ? 'nested markers' : 'a marker' }}
          that {{ pendingAssetMove.chain.length > 1 ? "don't" : "doesn't" }} include its new neighbor. Choose how to
          keep {{ pendingAssetMove.chain.length > 1 ? 'them' : 'it' }} contiguous.
        </p>
        <div v-for="opt in pendingAssetMoveOptions" :key="opt.value" class="flex align-items-center gap-2">
          <RadioButton
            v-model="pendingAssetMove.detachCount"
            :input-id="`move-opt-${opt.value}`"
            :value="opt.value"
          />
          <label :for="`move-opt-${opt.value}`" class="text-sm">{{ opt.label }}</label>
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="cancelPendingAssetMove" />
        <Button label="Move" @click="confirmPendingAssetMove" />
      </template>
    </Dialog>

    <Dialog
      :visible="pendingAssetInsert !== null"
      modal
      header="This insertion touches a marker boundary"
      :style="{ width: '34rem' }"
      @update:visible="cancelPendingAssetInsert"
    >
      <div v-if="pendingAssetInsert" class="flex flex-column gap-3">
        <p class="text-sm text-color-secondary m-0">
          The new asset{{ pendingAssetInsert.newAssets.length > 1 ? 's' : '' }} would land right at the edge of
          {{ pendingAssetInsert.chains.length > 1 ? 'markers' : 'a marker' }} -- choose whether to extend
          {{ pendingAssetInsert.chains.length > 1 ? 'them' : 'it' }} to include the new asset{{
            pendingAssetInsert.newAssets.length > 1 ? 's' : ''
          }}.
        </p>
        <div v-for="(chain, ci) in pendingAssetInsert.chains" :key="ci" class="flex flex-column gap-2">
          <span class="font-semibold text-sm">
            {{ chain.side === 'before' ? 'Marker(s) ending right before the new asset' : 'Marker(s) starting right after the new asset' }}
          </span>
          <div v-for="opt in insertChainOptions(chain)" :key="opt.value" class="flex align-items-center gap-2">
            <RadioButton v-model="chain.growCount" :input-id="`insert-opt-${ci}-${opt.value}`" :value="opt.value" />
            <label :for="`insert-opt-${ci}-${opt.value}`" class="text-sm">{{ opt.label }}</label>
          </div>
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="cancelPendingAssetInsert" />
        <Button label="Insert" @click="confirmPendingAssetInsert" />
      </template>
    </Dialog>

    <ConfirmPopup>
      <template #container="{ acceptCallback, rejectCallback }">
        <div v-if="splitDialogIndex !== null" class="flex flex-column gap-3 p-3" style="width: 28rem; max-width: calc(100vw - 1rem)">
          <strong>Split asset</strong>
          <p class="text-sm text-color-secondary m-0">
            Splits "{{ form.assets[splitDialogIndex].id }}" into two assets at the given offset -- the first part
            keeps this asset's id, start and fade-in; the second gets a new id, the shifted start, and this
            asset's fade-out.
          </p>
          <div class="flex flex-column gap-1">
            <label>Duration of the first part</label>
            <InputText
              v-model="splitFirstPartInput"
              placeholder="e.g. 90s, 1:30, 1 min 30 sec"
              autofocus
              @keydown.enter="acceptSplit(acceptCallback)"
            />
          </div>
          <Message v-if="splitError" severity="error" class="text-sm">{{ splitError }}</Message>
          <div class="flex justify-content-end gap-2">
            <Button label="Cancel" text @click="rejectCallback" />
            <Button label="Split" @click="acceptSplit(acceptCallback)" />
          </div>
        </div>
      </template>
    </ConfirmPopup>

    <Dialog
      v-model:visible="bootstrapOpen"
      modal
      header="Bootstrap timeline from files"
      :style="{ width: '46rem' }"
    >
      <div
        class="bootstrap-drop-zone flex flex-column gap-3"
        :class="{ 'bootstrap-drop-zone-active': bootstrapDropActive }"
        @dragover="handleBootstrapDragOver"
        @dragleave="handleBootstrapDragLeave"
        @drop="handleBootstrapDrop"
      >
        <p class="text-sm text-color-secondary m-0">
          Paste an ordered list of local file paths or URLs (one per line, blank lines and
          <code>#</code> comments ignored). Each is probed for duration; anything at or under the
          threshold below is classified as an ad and every consecutive run of ads is folded into
          one break marker. Assets are appended to the end of the current timeline -- review the
          result before saving/building.
        </p>

        <div class="flex flex-column gap-1">
          <label>Sources</label>
          <Textarea
            v-model="bootstrapText"
            rows="8"
            auto-resize
            placeholder="/path/to/content1.mp4&#10;/path/to/ad1.mp4&#10;https://cdn.example.com/ad2.mp4&#10;/path/to/content2.mp4"
          />
          <span v-if="bootstrapDropBusy" class="text-sm text-color-secondary">
            <i class="pi pi-spin pi-spinner" /> Uploading dropped file(s)...
          </span>
          <span v-else class="text-sm text-color-secondary">Drop local file(s) anywhere in this modal to add sources</span>
          <Message v-if="bootstrapDropError" severity="error" class="text-sm m-0">{{ bootstrapDropError }}</Message>
        </div>

        <div class="flex align-items-end gap-2">
          <div class="flex flex-column gap-1">
            <label>Ad threshold (assets at or under this duration are ads)</label>
            <InputText v-model="bootstrapThreshold" placeholder="2 min" style="width: 14rem" />
          </div>
          <Button
            label="Probe sources"
            icon="pi pi-search"
            :loading="bootstrapProbing"
            :disabled="parseBootstrapSources().length === 0"
            @click="probeBootstrapSources"
          />
        </div>
        <Message v-if="bootstrapThresholdSeconds === null" severity="warn" class="text-sm">
          Couldn't parse the ad threshold -- try "2 min", "00:02:00", or a plain number of seconds.
        </Message>

        <div v-if="bootstrapProbing" class="text-sm text-color-secondary">
          Probing {{ bootstrapProgress }} / {{ parseBootstrapSources().length }}...
        </div>

        <table v-if="bootstrapRows.length" class="bootstrap-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Source</th>
              <th>Duration</th>
              <th>Classified as</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in bootstrapRows" :key="i">
              <td>{{ i + 1 }}</td>
              <td style="word-break: break-all">{{ row.source }}</td>
              <td>
                <span v-if="row.error" class="text-red-500">error</span>
                <span v-else-if="row.duration !== null">{{ row.duration.toFixed(1) }}s</span>
                <span v-else class="text-color-secondary">?</span>
              </td>
              <td>
                <Message v-if="row.error" severity="error" class="text-xs m-0">{{ row.error }}</Message>
                <span v-else-if="isBootstrapAd(row)" class="text-orange-500">ad</span>
                <span v-else class="text-green-500">content</span>
              </td>
            </tr>
          </tbody>
        </table>

        <div v-if="bootstrapRows.length && !bootstrapHasErrors" class="text-sm text-color-secondary">
          {{ bootstrapRows.length }} asset(s), {{ bootstrapAdCount }} classified as ads.
        </div>
      </div>

      <template #footer>
        <Button label="Cancel" text @click="bootstrapOpen = false" />
        <Button
          label="Add to timeline"
          icon="pi pi-plus"
          :disabled="!bootstrapCanCommit"
          @click="commitBootstrap"
        />
      </template>
    </Dialog>
  </div>
</template>

<style scoped>
.bootstrap-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.bootstrap-table th {
  text-align: left;
  padding: 0.25rem 0.5rem;
  border-bottom: 1px solid var(--p-surface-300, #cbd5e1);
  color: var(--p-text-muted-color, #64748b);
  font-weight: 600;
}
.bootstrap-table td {
  padding: 0.25rem 0.5rem;
  border-bottom: 1px solid var(--p-surface-200, #e2e8f0);
  vertical-align: top;
}

/* A stand-in for the video frame: fixed 16:9. The corner pickers live
 * OUTSIDE this box (in flex rows above/below it) so they never cover the
 * preview text; only the simulated corner text and the countdown-bar
 * preview render inside it, at the same percent-of-height sizing the real
 * overlay uses. */
.osd-frame-mockup {
  position: relative;
  width: 100%;
  max-width: 32rem;
  aspect-ratio: 16 / 9;
  /* A mid-tone gradient standing in for video content -- a near-black
   * background here would make the semi-transparent black bar preview
   * below all but invisible against it. */
  background: linear-gradient(135deg, #64748b, #1e293b);
  border: 1px solid var(--p-surface-300, #cbd5e1);
  border-radius: 8px;
  overflow: hidden;
  transition: opacity 0.15s ease;
}

.osd-frame-disabled {
  opacity: 0.45;
}

/* Simulated corner text -- represents what the real drawtext overlay will
 * roughly look like, including the optional box background. Box sizing is
 * automatic (padding via CSS here; the real overlay's box is auto-sized by
 * ffmpeg's own drawtext `box`/`boxborderw`, not measured by hand). */
.osd-preview-text {
  position: absolute;
  max-width: 45%;
  font-size: 0.8rem;
  font-weight: 600;
  padding: 0.15rem 0.4rem;
  border-radius: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  /* Without this, the semi-transparent background (background-clip's
   * default is border-box) would paint UNDER the semi-transparent border
   * too, so the border's own color would blend twice -- once against the
   * mockup behind it, once against the background already painted there --
   * tinting the border differently than the flat corner_box.color picked.
   * Clipping the background to the padding box keeps the two independent,
   * mirroring osd.py's crop-based fix for the same issue in real ffmpeg
   * output (see build_corner_accent_stripe_graph's docstring). */
  background-clip: padding-box;
}

.osd-preview-tl { top: 0.5rem; left: 0.5rem; }
.osd-preview-tr { top: 0.5rem; right: 0.5rem; }
.osd-preview-bl { left: 0.5rem; }
.osd-preview-br { right: 0.5rem; }

/* PrimeVue's ColorPicker preview swatch has no border by default -- with
 * a white (the schema default) or other light color selected, it
 * disappears entirely against the page background. */
:deep(.p-colorpicker-preview) {
  border: 1px solid var(--p-surface-300, #cbd5e1) !important;
  border-radius: 4px;
}

.preview-video {
  max-width: 640px;
  width: 100%;
  border-radius: 4px;
  background: #000;
}

.report-open-button {
  text-decoration: none;
}

.report-frame {
  width: min(100%, 72rem);
  height: 42rem;
  border: 1px solid var(--surface-border);
  border-radius: 4px;
  background: #fff;
}

.osd-bar-preview {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 40%;
  min-height: 3px;
  background: rgba(0, 0, 0, 0.55);
  border-top: 1px solid rgba(255, 255, 255, 0.2);
}

/* Percentage fields (bar height, text size) are at most 2 digits (0-100)
 * -- just wide enough for "100", nothing more. */
.osd-narrow-pct :deep(input) {
  width: 2rem;
}

/* Hex color / short label fields -- "#RRGGBB" or "ad break" don't need a
 * full-width InputText either. */
.osd-narrow-hex {
  width: 6rem;
}

/* Settings tab fields: fixed, content-sized widths instead of stretching
 * to fill their flex row -- a playlist name or a 4-digit bitrate doesn't
 * need a field wide enough for a full sentence. */
.field-tiny {
  width: 6rem;
}

.field-tiny :deep(input) {
  width: 100%;
}

.field-small {
  width: 9rem;
}

.field-small :deep(input) {
  width: 100%;
}

.field-medium {
  width: 16rem;
}

.field-medium :deep(input) {
  width: 100%;
}


/* Global OSD options list: a REAL 4-column grid (label, control, secondary
 * label, secondary control) -- not flexbox rows. Flexbox rows let each
 * row's control stretch to fill whatever space its own siblings left
 * over, which is what made a 2-cell row's control balloon while a
 * 4-cell row's stayed narrow: same class, different rendered width.
 * `justify-items: start` is the other half of the fix -- grid items
 * stretch to fill their cell by default, so without it every control
 * would still expand to its (possibly wide) column's full width. */
.osd-options-grid {
  display: grid;
  grid-template-columns: repeat(4, auto);
  column-gap: 0.75rem;
  row-gap: 0.6rem;
  align-items: center;
  justify-items: start;
  /* The outer flex row (frame column + this column) defaults to
   * align-items: stretch, which stretches this grid to match the much
   * taller frame column's height. Grid's `align-content: normal` then
   * behaves like `stretch` for `auto` row tracks, spreading that extra
   * height evenly across every row -- the actual cause of the huge gaps
   * between rows. Both properties below independently prevent that:
   * align-self stops the stretch from happening at all; align-content is
   * a defensive fallback so rows still pack tightly even if something
   * else forces this container taller in the future. */
  align-self: flex-start;
  align-content: start;
}

.osd-option-label {
  font-weight: 500;
}

.osd-option-label-secondary {
  color: var(--p-text-muted-color, #64748b);
}

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
  /* Allow the active-bar (positioned inside the scrollable content div)
   * to paint over this border instead of being clipped by it. */
  overflow: visible;
}

:deep(.p-tablist-content) {
  overflow: visible;
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
  color: var(--p-primary-color, #b91c1c);
  background: var(--p-primary-50, #ecfeff);
}

:deep(.p-tablist-active-bar) {
  height: 3px;
  border-radius: 2px;
  background: var(--p-primary-color, #b91c1c);
  /* Shift down so the underline sits on top of the gray divider line
   * instead of leaving a visible gap above it. */
  bottom: -3px;
  z-index: 1;
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
  transition: border-color 0.15s ease, background 0.15s ease;
}

.asset-empty-state-drop-active {
  border-color: var(--p-primary-color, #b91c1c);
  background: var(--p-primary-50, #ecfeff);
}

.bootstrap-drop-zone {
  padding: 0.25rem;
  border: 2px dashed transparent;
  border-radius: 8px;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.bootstrap-drop-zone-active {
  border-color: var(--p-primary-color, #b91c1c);
  background: var(--p-primary-50, #ecfeff);
}

.detail-panel {
  background: var(--p-surface-50, #f8fafc);
}

.marker-section {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(16rem, 20rem);
  gap: 1.5rem;
  align-items: start;
}

.scte-panel {
  background: var(--p-surface-50, #f8fafc);
}

@media (max-width: 800px) {
  .marker-section {
    grid-template-columns: minmax(0, 1fr);
  }
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
