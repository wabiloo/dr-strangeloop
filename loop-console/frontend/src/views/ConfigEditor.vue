<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Divider from 'primevue/divider'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { getConfig, saveConfig } from '../api/client'

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

const form = reactive({
  output: {
    file: '../outputs/output.ts',
    dir: '../outputs/mychannel',
    resolution: '1920x1080',
    framerate: 25,
    bitrate_kbps: 10000,
    gop: null as number | null,
    service_provider: 'broadpeak',
    service_name: 'broadpeak.io',
  },
  renditions: [] as RenditionForm[],
  normalize: false,
  slate_image: '',
  assets: [newAsset()] as AssetForm[],
})

const isNew = computed(() => props.name === null)

function addAsset() {
  form.assets.push(newAsset())
}
function removeAsset(i: number) {
  form.assets.splice(i, 1)
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
  if (form.assets.length === 0) form.assets = [newAsset()]
}

async function load() {
  if (!props.name) return
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
</script>

<template>
  <div class="flex flex-column gap-4" style="max-width: 56rem">
    <h2 class="m-0">{{ isNew ? 'New content config' : `Edit: ${name}` }}</h2>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <div class="flex flex-column gap-1">
      <label for="cfg-name">Config name</label>
      <InputText id="cfg-name" v-model="nameInput" :disabled="!isNew" placeholder="my-stream" />
    </div>

    <Divider align="left"><span class="font-bold">Output</span></Divider>

    <div class="flex gap-3 align-items-center">
      <label><input type="radio" value="single" v-model="outputMode" /> Single file (one bitrate)</label>
      <label><input type="radio" value="ladder" v-model="outputMode" /> ABR ladder (multi-rendition, for ecs-express)</label>
    </div>

    <div class="grid">
      <div v-if="outputMode === 'single'" class="col-6 flex flex-column gap-1">
        <label>Output file</label>
        <InputText v-model="form.output.file" />
      </div>
      <div v-if="outputMode === 'single'" class="col-6 flex flex-column gap-1">
        <label>Resolution</label>
        <InputText v-model="form.output.resolution" />
      </div>
      <div v-if="outputMode === 'ladder'" class="col-12 flex flex-column gap-1">
        <label>Output directory</label>
        <InputText v-model="form.output.dir" />
      </div>
      <div class="col-4 flex flex-column gap-1">
        <label>Framerate</label>
        <InputNumber v-model="form.output.framerate" :use-grouping="false" />
      </div>
      <div v-if="outputMode === 'single'" class="col-4 flex flex-column gap-1">
        <label>Bitrate (kbps)</label>
        <InputNumber v-model="form.output.bitrate_kbps" :use-grouping="false" />
      </div>
      <div class="col-4 flex flex-column gap-1">
        <label>GOP (default = framerate x2)</label>
        <InputNumber v-model="form.output.gop" :use-grouping="false" placeholder="auto" />
      </div>
      <div class="col-6 flex flex-column gap-1">
        <label>Service provider</label>
        <InputText v-model="form.output.service_provider" />
      </div>
      <div class="col-6 flex flex-column gap-1">
        <label>Service name</label>
        <InputText v-model="form.output.service_name" />
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
      <InputText v-model="form.slate_image" placeholder="/path/to/slate.png" />
    </div>

    <Divider align="left"><span class="font-bold">Assets (ordered timeline)</span></Divider>

    <div v-for="(a, i) in form.assets" :key="i" class="p-3 border-1 surface-border border-round flex flex-column gap-2">
      <div class="flex justify-content-between align-items-center">
        <span class="font-semibold">Asset {{ i + 1 }}</span>
        <Button icon="pi pi-trash" severity="danger" text @click="removeAsset(i)" />
      </div>
      <div class="grid">
        <div class="col-12 flex flex-column gap-1">
          <label>File path</label>
          <InputText v-model="a.file" placeholder="content.mp4" />
        </div>
        <div class="col-3 flex flex-column gap-1">
          <label>Start</label>
          <InputText v-model="a.start" placeholder="00:00:00 / 10 min" />
        </div>
        <div class="col-3 flex flex-column gap-1">
          <label>Duration</label>
          <InputText v-model="a.duration" placeholder="10 min" />
        </div>
        <div class="col-3 flex flex-column gap-1">
          <label>Countdown</label>
          <InputText v-model="a.countdown" placeholder="5 or -1" />
        </div>
        <div class="col-3 flex flex-column gap-1">
          <label>Fade in / out (s)</label>
          <div class="flex gap-1">
            <InputText v-model="a.fade_in" placeholder="in" />
            <InputText v-model="a.fade_out" placeholder="out" />
          </div>
        </div>
        <div class="col-12 flex flex-column gap-1">
          <label>Per-asset slate image (overrides global)</label>
          <InputText v-model="a.slate_image" />
        </div>
      </div>

      <div class="flex align-items-center gap-2">
        <Checkbox v-model="a.ad_break.enabled" binary :input-id="`adbreak-${i}`" />
        <label :for="`adbreak-${i}`">This asset is an ad break</label>
      </div>

      <div v-if="a.ad_break.enabled" class="grid pl-3">
        <div class="col-4 flex flex-column gap-1">
          <label>Event ID (unique)</label>
          <InputNumber v-model="a.ad_break.event_id" :use-grouping="false" />
        </div>
        <div class="col-8 flex flex-column gap-1">
          <label>Splice type</label>
          <Select
            v-model="a.ad_break.splice_type"
            :options="[
              { label: 'splice_insert (two-point splice)', value: 'splice_insert' },
              { label: 'time_signal (segmentation descriptor)', value: 'time_signal' },
            ]"
            option-label="label"
            option-value="value"
          />
        </div>

        <template v-if="a.ad_break.splice_type === 'time_signal'">
          <div class="col-12"><Divider /></div>
          <div class="col-3 flex flex-column gap-1"><label>Segmentation type_id</label><InputText v-model="a.ad_break.segmentation.type_id" /></div>
          <div class="col-3 flex flex-column gap-1"><label>UPID type</label><InputText v-model="a.ad_break.segmentation.upid_type" /></div>
          <div class="col-6 flex flex-column gap-1"><label>UPID hex</label><InputText v-model="a.ad_break.segmentation.upid_hex" /></div>
          <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="a.ad_break.segmentation.web_delivery_allowed" binary /><label>Web delivery allowed</label></div>
          <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="a.ad_break.segmentation.no_regional_blackout" binary /><label>No regional blackout</label></div>
          <div class="col-3 flex align-items-center gap-2"><Checkbox v-model="a.ad_break.segmentation.archive_allowed" binary /><label>Archive allowed</label></div>
          <div class="col-3 flex flex-column gap-1"><label>Device restrictions</label><InputNumber v-model="a.ad_break.segmentation.device_restrictions" :use-grouping="false" /></div>
        </template>
      </div>
    </div>

    <Button label="Add asset" icon="pi pi-plus" outlined @click="addAsset" />

    <Divider />
    <div>
      <Button label="Save config" icon="pi pi-check" :loading="saving" @click="save" />
    </div>
  </div>
</template>
