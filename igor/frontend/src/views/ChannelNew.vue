<script setup lang="ts">
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { buildConfig, defineChannel, listConfigs } from '../api/client'
import JobPanel from '../components/JobPanel.vue'
import type { ChannelCreatePayload, ConfigListItem, Job } from '../api/types'

const router = useRouter()
const saving = ref(false)
const error = ref('')

const configs = ref<ConfigListItem[]>([])
const selectedConfigName = ref<string | null>(null)
const buildJobId = ref<string | null>(null)
const building = ref(false)

const selectedConfig = computed(() => configs.value.find((c) => c.name === selectedConfigName.value) ?? null)

const configOptions = computed(() =>
  configs.value.map((c) => ({
    label: c.error
      ? `${c.name} (invalid: ${c.error})`
      : `${c.name} -- ${c.asset_count ?? 0} asset${c.asset_count === 1 ? '' : 's'}, ${c.ad_break_count ?? 0} ad break${c.ad_break_count === 1 ? '' : 's'}`,
    value: c.name,
    disabled: Boolean(c.error),
  })),
)

const form = reactive<ChannelCreatePayload>({
  name: '',
  backend: 'ecs-express',
  region: 'eu-west-1',
  bucket_name: '',
  content_folder: 'its-a-live/content',
  source_path: '',
  segment_duration: 4.0,
  dvr_window_seconds: 30,
  port: 8080,
  cpu: 256,
  memory: 512,
})

function selectConfig(name: string | null) {
  selectedConfigName.value = name
  const c = configs.value.find((cfg) => cfg.name === name)
  if (c) form.source_path = c.output_dir ?? c.output_file ?? form.source_path
}

async function loadConfigs() {
  try {
    configs.value = await listConfigs()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function build() {
  if (!selectedConfigName.value) return
  building.value = true
  error.value = ''
  try {
    const job = await buildConfig(selectedConfigName.value)
    buildJobId.value = job.id
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    building.value = false
  }
}

function onBuildFinished(job: Job) {
  if (job.status !== 'succeeded') error.value = 'Build failed -- see log above.'
}

onMounted(loadConfigs)

const backendOptions = [
  { label: 'ecs-express (loop-dee-loop, self-hosted, cheap)', value: 'ecs-express' },
  { label: 'aws-media (MediaLive + MediaPackage, fully managed)', value: 'aws-media' },
]

const isEcsExpress = computed(() => form.backend === 'ecs-express')

async function submit() {
  saving.value = true
  error.value = ''
  try {
    await defineChannel(form)
    router.push(`/channels/${form.name}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="flex flex-column gap-3" style="max-width: 40rem">
    <h2 class="m-0">Define a new channel</h2>
    <p class="text-color-secondary m-0">
      This writes an its-a-live TOML config only -- it does not touch AWS yet. Use the "Create"
      action on the channel's detail page to actually deploy it (ensures the shared stack if
      needed, <code>cdk deploy</code>, <code>spark</code>, <code>start</code>).
    </p>

    <Message v-if="error" severity="error">{{ error }}</Message>

    <div class="flex flex-column gap-1">
      <label for="name">Channel name</label>
      <InputText id="name" v-model="form.name" placeholder="my-channel" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="backend">Backend</label>
      <Select id="backend" v-model="form.backend" :options="backendOptions" option-label="label" option-value="value" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="region">AWS region</label>
      <InputText id="region" v-model="form.region" placeholder="eu-west-1" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="bucket">Existing S3 bucket name</label>
      <InputText id="bucket" v-model="form.bucket_name" placeholder="my-existing-bucket" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="folder">S3 content folder (prefix)</label>
      <InputText id="folder" v-model="form.content_folder" placeholder="its-a-live/content" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="source-config">Content config</label>
      <Select
        id="source-config"
        :model-value="selectedConfigName"
        :options="configOptions"
        option-label="label"
        option-value="value"
        placeholder="Pick a franken-ts content config, or enter a path manually below"
        show-clear
        @update:model-value="selectConfig"
      />
      <div v-if="selectedConfig" class="flex align-items-center gap-2 mt-1">
        <RouterLink :to="`/configs/${selectedConfig.name}`" class="text-sm">Edit this config</RouterLink>
        <Button label="Build now" icon="pi pi-cog" size="small" text :loading="building" @click="build" />
      </div>
      <JobPanel v-if="buildJobId" :job-id="buildJobId" @finished="onBuildFinished" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="source">franken-ts output path (.ts file or rendition-ladder dir)</label>
      <InputText id="source" v-model="form.source_path" placeholder="../outputs/mychannel" />
    </div>

    <template v-if="isEcsExpress">
      <div class="grid">
        <div class="col-6 flex flex-column gap-1">
          <label for="segdur">Segment duration (s)</label>
          <InputNumber id="segdur" v-model="form.segment_duration" :min-fraction-digits="1" />
        </div>
        <div class="col-6 flex flex-column gap-1">
          <label for="dvr">DVR window (s)</label>
          <InputNumber id="dvr" v-model="form.dvr_window_seconds" />
        </div>
        <div class="col-12 flex flex-column gap-1">
          <label for="port">Serve port</label>
          <InputNumber id="port" v-model="form.port" :use-grouping="false" />
        </div>
        <div class="col-6 flex flex-column gap-1">
          <label for="cpu">Express CPU units</label>
          <InputNumber id="cpu" v-model="form.cpu" :use-grouping="false" />
        </div>
        <div class="col-6 flex flex-column gap-1">
          <label for="memory">Express memory (MB)</label>
          <InputNumber id="memory" v-model="form.memory" :use-grouping="false" />
        </div>
      </div>
    </template>

    <div>
      <Button label="Save channel definition" icon="pi pi-check" :loading="saving" @click="submit" />
    </div>
  </div>
</template>
