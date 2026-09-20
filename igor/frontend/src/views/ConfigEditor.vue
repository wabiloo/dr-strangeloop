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
import { getConfig, saveConfig } from '../api/client'
import AssetTimeline from '../components/AssetTimeline.vue'
import AssetFileField from '../components/AssetFileField.vue'
import {
  SEGMENTATION_TYPE_ID_OPTIONS,
  UPID_TYPE_OPTIONS,
  hexToText,
  textToHex,
} from '../segmentationPresets'

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

interface AdBreakForm {
  enabled: boolean
  event_id: number | null
  splice_type: 'splice_insert' | 'time_signal'
  segmentation: SegmentationForm
}

interface AssetForm {
  file: string
  start: string
  duration: string
  countdown: string
  fade_in: string
  fade_out: string
  slate_image: string
  ad_break: AdBreakForm
}

function newSegmentation(): SegmentationForm {
  return {
    type_id: '0x34',
    upid_type: '0x09',
    upid_hex: '',
    web_delivery_allowed: true,
    no_regional_blackout: false,
    archive_allowed: false,
    device_restrictions: 1,
  }
}

function newAsset(): AssetForm {
  return {
    file: '',
    start: '',
    duration: '',
    countdown: '',
    fade_in: '',
    fade_out: '',
    slate_image: '',
    ad_break: { enabled: false, event_id: null, splice_type: 'splice_insert', segmentation: newSegmentation() },
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
})

/** Resets the form to its blank-new-config state -- needed because vue-router
 * reuses this component instance when navigating between /configs/new and
 * /configs/:name, so mount-time initializers alone aren't enough. */
function resetForm() {
  outputMode.value = 'single'
  Object.assign(form.output, defaultOutput())
  form.renditions = []
  form.normalize = false
  form.slate_image = ''
  form.assets = []
}

const isNew = computed(() => props.name === null)
const activeTab = ref('settings')
const selectedAssetIndex = ref<number | null>(null)

function selectAsset(i: number) {
  selectedAssetIndex.value = i
}

function addAsset() {
  form.assets.push(newAsset())
  selectedAssetIndex.value = form.assets.length - 1
}

function removeAsset(i: number) {
  form.assets.splice(i, 1)
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

function timeOrUndefined(v: string): string | number | undefined {
  if (!v.trim()) return undefined
  const n = Number(v)
  return Number.isFinite(n) && v.trim() === String(n) ? n : v
}

/** Converts the form's local shape into the franken-ts YAML shape
 * (franken_ts.config.Config) -- server re-validates against the real
 * Pydantic model on save regardless. */
function toYamlConfig(): Record<string, unknown> {
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
      if (a.start.trim()) asset.start = timeOrUndefined(a.start)
      if (a.duration.trim()) asset.duration = timeOrUndefined(a.duration)
      if (a.countdown.trim()) asset.countdown = timeOrUndefined(a.countdown)
      if (a.fade_in.trim()) asset.fade_in = timeOrUndefined(a.fade_in)
      if (a.fade_out.trim()) asset.fade_out = timeOrUndefined(a.fade_out)
      if (a.slate_image.trim()) asset.slate_image = a.slate_image
      if (a.ad_break.enabled) {
        const adBreak: Record<string, unknown> = {
          event_id: a.ad_break.event_id,
          splice_type: a.ad_break.splice_type,
        }
        if (a.ad_break.splice_type === 'time_signal') {
          adBreak.segmentation = { ...a.ad_break.segmentation }
        }
        asset.ad_break = adBreak
      }
      return asset
    })

  const cfg: Record<string, unknown> = { output, assets }
  if (form.normalize) cfg.normalize = true
  if (form.slate_image.trim()) cfg.slate_image = form.slate_image
  return cfg
}

/** Best-effort inverse of toYamlConfig(), for loading an existing config
 * back into the form. */
function fromYamlConfig(data: Record<string, unknown>) {
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
  form.assets = rawAssets.map((a) => {
    const adBreakRaw = a.ad_break as Record<string, unknown> | undefined
    const seg = (adBreakRaw?.segmentation as Record<string, unknown>) ?? {}
    return {
      file: String(a.file ?? ''),
      start: a.start !== undefined ? String(a.start) : '',
      duration: a.duration !== undefined ? String(a.duration) : '',
      countdown: a.countdown !== undefined ? String(a.countdown) : '',
      fade_in: a.fade_in !== undefined ? String(a.fade_in) : '',
      fade_out: a.fade_out !== undefined ? String(a.fade_out) : '',
      slate_image: String(a.slate_image ?? ''),
      ad_break: {
        enabled: Boolean(adBreakRaw),
        event_id: adBreakRaw ? Number(adBreakRaw.event_id) : null,
        splice_type: (adBreakRaw?.splice_type as 'splice_insert' | 'time_signal') ?? 'splice_insert',
        segmentation: {
          type_id: String(seg.type_id ?? '0x34'),
          upid_type: String(seg.upid_type ?? '0x09'),
          upid_hex: String(seg.upid_hex ?? ''),
          web_delivery_allowed: seg.web_delivery_allowed !== false,
          no_regional_blackout: Boolean(seg.no_regional_blackout),
          archive_allowed: Boolean(seg.archive_allowed),
          device_restrictions: Number(seg.device_restrictions ?? 1),
        },
      },
    }
  })
  selectedAssetIndex.value = form.assets.length > 0 ? 0 : null
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
    const data = await getConfig(props.name)
    fromYamlConfig(data)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!nameInput.value.trim()) {
    error.value = 'Config name is required.'
    return
  }
  saving.value = true
  error.value = ''
  try {
    await saveConfig(nameInput.value.trim(), toYamlConfig())
    toast.add({ severity: 'success', summary: 'Saved', life: 3000 })
    router.push('/configs')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(() => props.name, load)

// --- ASCII <-> hex helper popover for upid_hex --------------------------
const hexPopover = ref<InstanceType<typeof Popover> | null>(null)
const hexPopoverAssetIndex = ref<number | null>(null)
const hexPopoverText = ref('')

function openHexPopover(event: Event, assetIndex: number) {
  hexPopoverAssetIndex.value = assetIndex
  hexPopoverText.value = hexToText(form.assets[assetIndex].ad_break.segmentation.upid_hex)
  hexPopover.value?.toggle(event)
}

function applyHexPopover() {
  if (hexPopoverAssetIndex.value === null) return
  form.assets[hexPopoverAssetIndex.value].ad_break.segmentation.upid_hex = textToHex(hexPopoverText.value)
  hexPopover.value?.hide()
}
</script>

<template>
  <div class="flex flex-column gap-3">
    <div class="editor-header">
      <div class="flex flex-column gap-1">
        <RouterLink to="/configs" class="editor-back"><i class="pi pi-arrow-left" /> Content configs</RouterLink>
        <h2 class="m-0">{{ isNew ? 'New content config' : `Edit: ${name}` }}</h2>
      </div>
      <Button label="Save config" icon="pi pi-check" :loading="saving" @click="save" />
    </div>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <Tabs v-model:value="activeTab">
      <TabList>
        <Tab value="settings">Channel settings</Tab>
        <Tab value="assets">
          Timeline &amp; assets
          <span class="asset-count-badge">{{ form.assets.length }}</span>
        </Tab>
      </TabList>

      <TabPanels>
        <TabPanel value="settings">
          <div class="flex flex-column gap-4" style="max-width: 56rem">
            <div class="flex flex-column gap-1">
              <label for="cfg-name">Config name</label>
              <InputText id="cfg-name" v-model="nameInput" :disabled="!isNew" placeholder="my-stream" />
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
                @select="selectAsset"
                @add="addAsset"
              />
            </div>

            <div v-if="selectedAssetIndex === null" class="asset-empty-state">
              <i class="pi pi-images" style="font-size: 1.5rem" />
              <span>{{ form.assets.length === 0 ? 'No assets yet.' : 'Select an asset above to edit it.' }}</span>
              <Button label="Add asset" icon="pi pi-plus" outlined @click="addAsset" />
            </div>

            <div v-else class="p-3 border-1 surface-border border-round flex flex-column gap-2">
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
                <div class="col-12 flex flex-column gap-1">
                  <label>File path (local path or https:// URL)</label>
                  <AssetFileField v-model="form.assets[selectedAssetIndex].file" placeholder="content.mp4" />
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

              <div class="flex align-items-center gap-2">
                <Checkbox v-model="form.assets[selectedAssetIndex].ad_break.enabled" binary :input-id="`adbreak-${selectedAssetIndex}`" />
                <label :for="`adbreak-${selectedAssetIndex}`">This asset is an ad break</label>
              </div>

              <div v-if="form.assets[selectedAssetIndex].ad_break.enabled" class="grid pl-3">
                <div class="col-4 flex flex-column gap-1">
                  <label>Event ID (unique)</label>
                  <InputNumber v-model="form.assets[selectedAssetIndex].ad_break.event_id" :use-grouping="false" />
                </div>
                <div class="col-8 flex flex-column gap-1">
                  <label>Splice type</label>
                  <Select
                    v-model="form.assets[selectedAssetIndex].ad_break.splice_type"
                    :options="[
                      { label: 'splice_insert (two-point splice)', value: 'splice_insert' },
                      { label: 'time_signal (segmentation descriptor)', value: 'time_signal' },
                    ]"
                    option-label="label"
                    option-value="value"
                  />
                </div>

                <template v-if="form.assets[selectedAssetIndex].ad_break.splice_type === 'time_signal'">
                  <div class="col-12"><Divider /></div>
                  <div class="col-4 flex flex-column gap-1">
                    <label>Segmentation type_id</label>
                    <Select
                      v-model="form.assets[selectedAssetIndex].ad_break.segmentation.type_id"
                      :options="SEGMENTATION_TYPE_ID_OPTIONS"
                      option-label="label"
                      option-value="value"
                      filter
                    />
                  </div>
                  <div class="col-4 flex flex-column gap-1">
                    <label>UPID type</label>
                    <Select
                      v-model="form.assets[selectedAssetIndex].ad_break.segmentation.upid_type"
                      :options="UPID_TYPE_OPTIONS"
                      option-label="label"
                      option-value="value"
                      filter
                    />
                  </div>
                  <div class="col-4 flex flex-column gap-1">
                    <label>UPID hex</label>
                    <div class="flex gap-1">
                      <InputText v-model="form.assets[selectedAssetIndex].ad_break.segmentation.upid_hex" class="flex-1" />
                      <Button
                        icon="pi pi-language"
                        severity="secondary"
                        outlined
                        title="Enter as text (ASCII) instead of hex"
                        @click="openHexPopover($event, selectedAssetIndex)"
                      />
                    </div>
                  </div>
                  <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="form.assets[selectedAssetIndex].ad_break.segmentation.web_delivery_allowed" binary /><label>Web delivery allowed</label></div>
                  <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="form.assets[selectedAssetIndex].ad_break.segmentation.no_regional_blackout" binary /><label>No regional blackout</label></div>
                  <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="form.assets[selectedAssetIndex].ad_break.segmentation.archive_allowed" binary /><label>Archive allowed</label></div>
                  <div class="col-3 flex flex-column gap-1"><label>Device restrictions</label><InputNumber v-model="form.assets[selectedAssetIndex].ad_break.segmentation.device_restrictions" :use-grouping="false" /></div>
                </template>
              </div>
            </div>
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
</style>
