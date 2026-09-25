<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { buildPlaylist, defineChannel, listPlaylists } from '../api/client'
import JobPanel from '../components/JobPanel.vue'
import DaterangeIdFormatHelp from '../components/DaterangeIdFormatHelp.vue'
import type { ChannelCreatePayload, Job, PlaylistListItem } from '../api/types'

const router = useRouter()
const saving = ref(false)
const error = ref('')

const playlists = ref<PlaylistListItem[]>([])
const selectedPlaylistName = ref<string | null>(null)
const buildJobId = ref<string | null>(null)
const building = ref(false)

const selectedPlaylist = computed(() => playlists.value.find((c) => c.name === selectedPlaylistName.value) ?? null)

const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/
const nameError = computed(() => {
  if (!form.name) return 'Required.'
  if (!NAME_RE.test(form.name)) {
    return 'Lowercase letters, digits, and hyphens only; cannot start or end with a hyphen.'
  }
  return ''
})

const playlistOptions = computed(() =>
  playlists.value.map((c) => ({
    label: c.error
      ? `${c.name} (invalid: ${c.error})`
      : `${c.name} -- ${c.asset_count ?? 0} asset${c.asset_count === 1 ? '' : 's'}, ${c.marker_count ?? 0} marker${c.marker_count === 1 ? '' : 's'}`,
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
  daterange_mode: 'shared',
  cue_tags: 'none',
  increment_event_ids: false,
  daterange_id_format: '{segcode}-{eventid}-{loop}',
})
// Only meaningful for local-docker (see form.port's "auto" branch below);
// kept as separate UI state rather than storing 'auto' directly in
// form.port so InputNumber always gets a number to work with, and
// switching the toggle off restores whatever port was last typed in.
const autoPort = ref(false)
const lastExplicitPort = ref(8080)
watch(autoPort, (auto) => {
  if (auto) {
    lastExplicitPort.value = typeof form.port === 'number' ? form.port : lastExplicitPort.value
    form.port = 'auto'
  } else {
    form.port = lastExplicitPort.value
  }
})

function selectPlaylist(name: string | null) {
  selectedPlaylistName.value = name
  const p = playlists.value.find((pl) => pl.name === name)
  if (p) form.source_path = p.output_dir ?? p.output_file ?? form.source_path
}

async function loadPlaylists() {
  try {
    playlists.value = await listPlaylists()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function build() {
  if (!selectedPlaylistName.value) return
  building.value = true
  error.value = ''
  try {
    const job = await buildPlaylist(selectedPlaylistName.value)
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

onMounted(loadPlaylists)

const backendOptions = [
  { label: 'ecs-express (loop-dee-loop, self-hosted, cheap)', value: 'ecs-express' },
  { label: 'aws-media (MediaLive + MediaPackage, fully managed)', value: 'aws-media' },
  { label: 'local-docker (loop-dee-loop on this machine, no AWS)', value: 'local-docker' },
]

const isEcsExpress = computed(() => form.backend === 'ecs-express')
const isLocalDocker = computed(() => form.backend === 'local-docker')
// "auto" port is local-docker-only (see backend/its_a_live.generate_toml
// validation) -- fall back to an explicit port if the backend is switched
// away from local-docker while it's toggled on.
watch(
  () => form.backend,
  (backend) => {
    if (backend !== 'local-docker' && autoPort.value) {
      autoPort.value = false
    }
  },
)
// Both ecs-express and local-docker bake+serve via loop-dee-loop and share
// [packaging]/[markers]; `port` itself lands in [express] for ecs-express
// or the local-docker-only [docker] section (generate_toml() picks the
// section, this form just shows one `port` field either way). Only
// ecs-express additionally needs Fargate cpu/memory (also in [express]).
const usesChannelSection = computed(() => isEcsExpress.value || isLocalDocker.value)

const daterangeModeOptions = [
  { label: 'shared -- one DATERANGE per descriptor, full shared payload (default)', value: 'shared' },
  { label: 'narrowed -- one DATERANGE per descriptor, payload narrowed to just that event', value: 'narrowed' },
  { label: 'grouped -- one DATERANGE per group of coincident descriptors', value: 'grouped' },
]
const cueTagsOptions = [
  { label: 'none -- DATERANGE only (default)', value: 'none' },
  { label: 'alongside -- also emit EXT-X-CUE-OUT/-CONT/-IN for splice_insert markers, next to DATERANGE', value: 'alongside' },
  { label: 'only -- EXT-X-CUE-OUT/-CONT/-IN only, no DATERANGE (splice_insert-only channels)', value: 'only' },
]

async function submit() {
  if (nameError.value) return
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

    <h4 class="mb-0">Channel</h4>
    <div class="flex flex-column gap-1">
      <label for="name">Channel name</label>
      <InputText id="name" v-model="form.name" placeholder="my-channel" :invalid="Boolean(form.name) && Boolean(nameError)" />
      <div v-if="form.name && nameError" class="text-red-500 text-xs">{{ nameError }}</div>
    </div>

    <div class="flex flex-column gap-1">
      <label for="backend">Backend</label>
      <Select id="backend" v-model="form.backend" :options="backendOptions" option-label="label" option-value="value" />
    </div>

    <template v-if="!isLocalDocker">
      <h4 class="mb-0 mt-2">AWS / S3</h4>
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
    </template>

    <h4 class="mb-0 mt-2">Content</h4>
    <div class="flex flex-column gap-1">
      <label for="source-playlist">Playlist</label>
      <Select
        id="source-playlist"
        :model-value="selectedPlaylistName"
        :options="playlistOptions"
        option-label="label"
        option-value="value"
        placeholder="Pick a franken-ts playlist, or enter a path manually below"
        show-clear
        @update:model-value="selectPlaylist"
      />
      <div v-if="selectedPlaylist" class="flex align-items-center gap-2 mt-1">
        <RouterLink :to="`/playlists/${selectedPlaylist.name}`" class="text-sm">Edit this playlist</RouterLink>
        <Button label="Build now" icon="pi pi-cog" size="small" text :loading="building" @click="build" />
      </div>
      <JobPanel v-if="buildJobId" :job-id="buildJobId" @finished="onBuildFinished" />
    </div>

    <div class="flex flex-column gap-1">
      <label for="source">franken-ts output path (.ts file or rendition-ladder dir)</label>
      <InputText id="source" v-model="form.source_path" placeholder="../outputs/mychannel" />
    </div>

    <template v-if="usesChannelSection">
      <h4 class="mb-0 mt-2">Packaging</h4>
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
          <label for="port">Serve port{{ isLocalDocker ? ' (also the host port -- http://localhost:<port>)' : '' }}</label>
          <div v-if="isLocalDocker" class="flex align-items-center gap-2">
            <Checkbox v-model="autoPort" binary input-id="auto-port" />
            <label for="auto-port" class="text-sm">Auto-select a free port</label>
          </div>
          <InputNumber
            v-if="!isLocalDocker || !autoPort"
            id="port"
            v-model="form.port as number"
            :use-grouping="false"
          />
          <div v-else class="text-color-secondary text-sm">
            A free port (8080-8179, skipping any already in use -- e.g. by other local-docker
            channels) is picked when the container is first started, and reused by
            start/refresh/status afterwards.
          </div>
        </div>
        <template v-if="isEcsExpress">
          <div class="col-6 flex flex-column gap-1">
            <label for="cpu">Express CPU units</label>
            <InputNumber id="cpu" v-model="form.cpu" :use-grouping="false" />
          </div>
          <div class="col-6 flex flex-column gap-1">
            <label for="memory">Express memory (MB)</label>
            <InputNumber id="memory" v-model="form.memory" :use-grouping="false" />
          </div>
        </template>
      </div>

      <h4 class="mb-0 mt-2">SCTE-35 signaling</h4>
      <div class="flex flex-column gap-1">
        <label for="daterange-mode">HLS DATERANGE mode</label>
        <Select id="daterange-mode" v-model="form.daterange_mode" :options="daterangeModeOptions" option-label="label" option-value="value" />
      </div>
      <div class="flex flex-column gap-1">
        <label for="cue-tags">HLS CUE-OUT/CUE-IN tags</label>
        <Select id="cue-tags" v-model="form.cue_tags" :options="cueTagsOptions" option-label="label" option-value="value" />
      </div>
      <div class="flex align-items-center gap-2">
        <Checkbox v-model="form.increment_event_ids" binary input-id="increment-event-ids" />
        <label for="increment-event-ids">Increment SCTE-35 event ids each loop (HLS + DASH)</label>
      </div>
      <div class="flex flex-column gap-1">
        <div class="flex align-items-center gap-1">
          <label for="daterange-id-format">HLS DATERANGE ID format</label>
          <DaterangeIdFormatHelp />
        </div>
        <InputText id="daterange-id-format" v-model="form.daterange_id_format" />
      </div>
      <div class="text-color-secondary text-xs">
        Off (default) repeats the same event id every loop -- easiest to test against. On bumps
        each id by loop_number &times; a shared step (a power of 10 above the channel's largest
        base id, e.g. base ids 100-190 &rarr; step 1000, so loop 1 emits 1100/1190, loop 2 emits
        2100/2190, ...) -- predictable from wall-clock time alone, and wraps back to the base id
        at the 32-bit SCTE-35 ceiling.
      </div>
    </template>

    <div>
      <Button label="Save channel definition" icon="pi pi-check" :loading="saving" :disabled="Boolean(nameError)" @click="submit" />
    </div>
  </div>
</template>
