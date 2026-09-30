<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { buildPlaylist, defineChannel, listArchives, listManifests, listPlaylists } from '../api/client'
import JobPanel from '../components/JobPanel.vue'
import DaterangeIdFormatHelp from '../components/DaterangeIdFormatHelp.vue'
import FieldHelp from '../components/FieldHelp.vue'
import type { ArchiveListItem, ChannelCreatePayload, Job, ManifestListItem, PlaylistListItem } from '../api/types'

const router = useRouter()
const saving = ref(false)
const error = ref('')

const playlists = ref<PlaylistListItem[]>([])
const selectedPlaylistName = ref<string | null>(null)
const buildJobId = ref<string | null>(null)
const building = ref(false)

// ── Source kind: franken-ts playlist vs. grave-robber archive import
// (grave-robber/SCOPE.md §10) -- both resolve to the same form.source_path
// field underneath once a path is picked; bake.py/its-a-live only ever see
// the resulting path, never "playlist" or "archive" as a concept. ─────────
const sourceKindOptions = [
  { label: 'franken-ts playlist', value: 'playlist' },
  { label: 'Archive import', value: 'archive' },
  { label: 'Manifest import', value: 'manifest' },
]
const sourceKind = ref<'playlist' | 'archive' | 'manifest'>('playlist')
const archives = ref<ArchiveListItem[]>([])
const selectedArchiveName = ref<string | null>(null)
const selectedArchive = computed(() => archives.value.find((a) => a.name === selectedArchiveName.value) ?? null)
const archiveOptions = computed(() =>
  archives.value.map((a) => ({
    label: a.import
      ? `${a.display_name || a.name} -- imported (${a.variant_count ?? 0} variant${a.variant_count === 1 ? '' : 's'})`
      : `${a.display_name || a.name} -- not yet imported (${a.variant_count ?? 0} variant${a.variant_count === 1 ? '' : 's'})`,
    value: a.name,
    disabled: !a.import,
  })),
)

function selectArchive(name: string | null) {
  selectedArchiveName.value = name
  const a = archives.value.find((ar) => ar.name === name)
  if (a?.import?.manifest_path) form.source_path = a.import.manifest_path
}

async function loadArchives() {
  try {
    archives.value = await listArchives()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

// ── Manifest imports (grave-robber ingest-url): a downloaded VOD ladder ──
const manifests = ref<ManifestListItem[]>([])
const selectedManifestName = ref<string | null>(null)
const selectedManifest = computed(() => manifests.value.find((m) => m.name === selectedManifestName.value) ?? null)
const manifestOptions = computed(() =>
  manifests.value.map((m) => {
    const summary = m.import?.summary
    const ladder = summary ? `${summary.renditions.length} rendition${summary.renditions.length === 1 ? '' : 's'}` : ''
    return {
      label: m.import ? `${m.display_name || m.name} -- imported (${ladder})` : `${m.display_name || m.name} -- not yet imported`,
      value: m.name,
      disabled: !m.import,
    }
  }),
)

function selectManifest(name: string | null) {
  selectedManifestName.value = name
  const m = manifests.value.find((x) => x.name === name)
  if (m?.import?.manifest_path) form.source_path = m.import.manifest_path
  // A full download has no missing segments unless the import was told to skip failures.
  form.allow_missing_segments = m?.import_options?.allow_missing_segments ?? false
}

async function loadManifests() {
  try {
    manifests.value = await listManifests()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

watch(sourceKind, (kind) => {
  // Defaults on for archive-import source kind (SCOPE.md §10) -- the
  // near-certain case (HAR captures are frequently manifest-only), but
  // stays visible/editable regardless of source kind, since it's a plain
  // bake.py-level flag, not intrinsically tied to where the content came from.
  form.source_kind = kind
  form.allow_missing_segments = kind === 'archive'
  if (kind === 'archive' && archives.value.length === 0) loadArchives()
  if (kind === 'manifest' && manifests.value.length === 0) loadManifests()
})

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
  backend: 'local-docker',
  region: 'eu-west-1',
  bucket_name: '',
  content_folder: 'its-a-live/content',
  source_path: '',
  source_kind: 'playlist',
  allow_missing_segments: false,
  segment_duration: 4.0,
  dvr_window_seconds: 30,
  hls_format: 'cmaf',
  hls_ts_mux_audio: true,
  continuous_timeline: false,
  port: 'auto',
  cpu: 256,
  memory: 512,
  daterange_mode: 'shared',
  cue_tags: 'none',
  increment_event_ids: false,
  daterange_id_format: '{segcode}-{eventid}-{loop}',
  dash_signal_format: 'binary',
  dash_descriptor_mode: 'shared',
})
// Only meaningful for local-docker (see form.port's "auto" branch below);
// kept as separate UI state rather than storing 'auto' directly in
// form.port so InputNumber always gets a number to work with, and
// switching the toggle off restores whatever port was last typed in.
const autoPort = ref(true)
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
    autoPort.value = backend === 'local-docker'
  },
)
// Both ecs-express and local-docker bake+serve via loop-dee-loop and share
// [packaging]/[markers]; `port` itself lands in [express] for ecs-express
// or the local-docker-only [docker] section (generate_toml() picks the
// section, this form just shows one `port` field either way). Only
// ecs-express additionally needs Fargate cpu/memory (also in [express]).
const usesChannelSection = computed(() => isEcsExpress.value || isLocalDocker.value)
const hlsFormatOptions = [
  { label: 'CMAF (fragmented MP4)', value: 'cmaf' },
  { label: 'MPEG-TS', value: 'ts' },
]

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
const dashSignalFormatOptions = [
  { label: 'binary -- raw SCTE-35 payload in a <Binary> element (default)', value: 'binary' },
  { label: 'xml -- full decoded <SpliceInfoSection> per the SCTE-35 XML binding', value: 'xml' },
]
const dashDescriptorModeOptions = [
  { label: 'shared -- full multi-descriptor message on every coincident event (default)', value: 'shared' },
  { label: 'narrowed -- each event carries only its own descriptor, re-encoded', value: 'narrowed' },
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

    <h4 class="mb-0 mt-2">Infrastructure</h4>
    <div class="flex flex-column gap-1">
      <label for="backend">Backend</label>
      <Select id="backend" v-model="form.backend" :options="backendOptions" option-label="label" option-value="value" />
    </div>

    <div v-if="usesChannelSection" class="flex flex-column gap-1">
      <label for="port">Serve port{{ isLocalDocker ? ' (also the host port -- http://localhost:<port>)' : '' }}</label>
      <div v-if="isLocalDocker" class="flex align-items-center gap-2">
        <Checkbox v-model="autoPort" binary input-id="auto-port" />
        <label for="auto-port" class="text-sm">Auto-select a free port</label>
        <FieldHelp label="Auto-select a free port">
          A free port (8080-8179, skipping any already in use -- e.g. by other local-docker
          channels) is picked when the container is first started, and reused by
          start/refresh/status afterwards.
        </FieldHelp>
      </div>
      <InputNumber
        v-if="!isLocalDocker || !autoPort"
        id="port"
        v-model="form.port as number"
        :use-grouping="false"
      />
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
      <label>Source kind</label>
      <SelectButton v-model="sourceKind" :options="sourceKindOptions" option-label="label" option-value="value" />
    </div>

    <div v-if="sourceKind === 'playlist'" class="flex flex-column gap-1">
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

    <div v-else-if="sourceKind === 'manifest'" class="flex flex-column gap-1">
      <label for="source-manifest">Manifest import</label>
      <Select
        id="source-manifest"
        :model-value="selectedManifestName"
        :options="manifestOptions"
        option-label="label"
        option-value="value"
        placeholder="Pick an imported manifest, or enter a manifest.json path manually below"
        show-clear
        @update:model-value="selectManifest"
      />
      <div v-if="selectedManifest" class="flex align-items-center gap-2 mt-1">
        <RouterLink :to="`/manifests/${selectedManifest.name}`" class="text-sm">
          {{ selectedManifest.import ? 'Re-run import' : 'Import this manifest' }}
        </RouterLink>
      </div>
      <div v-if="manifests.length === 0" class="text-color-secondary text-sm">
        No manifests yet. Add a VOD manifest URL on the <RouterLink to="/manifests">Manifests</RouterLink> page
        and import it first.
      </div>
    </div>

    <div v-else class="flex flex-column gap-1">
      <label for="source-archive">Archive import</label>
      <Select
        id="source-archive"
        :model-value="selectedArchiveName"
        :options="archiveOptions"
        option-label="label"
        option-value="value"
        placeholder="Pick an imported archive, or enter a manifest.json path manually below"
        show-clear
        @update:model-value="selectArchive"
      />
      <div v-if="selectedArchive" class="flex align-items-center gap-2 mt-1">
        <RouterLink :to="`/archives/${selectedArchive.name}`" class="text-sm">
          {{ selectedArchive.import ? 'Re-run import' : 'Import this archive' }}
        </RouterLink>
      </div>
      <div v-if="archives.length === 0" class="text-color-secondary text-sm">
        No archives found. Drop a HAR/Proxyman log into <code>data/archives/</code> and visit
        <RouterLink to="/archives">Archives</RouterLink> to import one first.
      </div>
    </div>

    <div class="flex flex-column gap-1">
      <label for="source">{{ sourceKind === 'playlist' ? 'franken-ts output path (.ts file or rendition-ladder dir)' : 'grave-robber manifest.json path' }}</label>
      <InputText id="source" v-model="form.source_path" placeholder="../outputs/mychannel" />
    </div>

    <div class="flex align-items-center gap-2">
      <Checkbox v-model="form.allow_missing_segments" binary input-id="allow-missing-segments" />
      <label for="allow-missing-segments">
        Allow missing segments (manifest-complete, media-optional)
      </label>
      <FieldHelp label="Allow missing segments">
        Only meaningful for a grave-robber segment-list manifest (an archive import almost always has
        some segments with no recovered media -- HAR captures are frequently manifest-only). A
        request for a missing segment's bytes 404s, but the served manifest is otherwise
        indistinguishable from a fully-populated one. Off by default for a franken-ts playlist, since
        that path never has missing segments to begin with.
      </FieldHelp>
    </div>

    <template v-if="usesChannelSection">
      <h4 class="mb-0 mt-2">Packaging</h4>
      <div class="grid">
        <div v-if="sourceKind === 'playlist'" class="col-6 flex flex-column gap-1">
          <label for="segdur">Segment duration (s)</label>
          <InputNumber id="segdur" v-model="form.segment_duration" :min-fraction-digits="1" />
        </div>
        <div :class="[sourceKind === 'playlist' ? 'col-6' : 'col-12', 'flex flex-column gap-1']">
          <label for="dvr">DVR window (s)</label>
          <InputNumber id="dvr" v-model="form.dvr_window_seconds" />
        </div>
        <div class="col-12 flex flex-column gap-1">
          <label for="hls-format">HLS segment format</label>
          <Select id="hls-format" v-model="form.hls_format" :options="hlsFormatOptions" option-label="label" option-value="value" />
        </div>
        <div v-if="form.hls_format === 'ts'" class="col-12 flex align-items-center gap-2">
          <Checkbox v-model="form.hls_ts_mux_audio" binary input-id="hls-ts-mux-audio" />
          <label for="hls-ts-mux-audio">Mux audio into each HLS TS video segment</label>
        </div>
        <div class="col-12 flex align-items-center gap-2">
          <Checkbox v-model="form.continuous_timeline" binary input-id="continuous-timeline" />
          <label for="continuous-timeline">Continuous timeline across the loop wrap</label>
          <FieldHelp label="Continuous timeline">
            Rewrites each segment's own timestamps per request (header patch, never a re-transcode) so
            the channel has no discontinuity/Period restart at the loop wrap. Off by default: the loop wrap
            is then honestly signaled with #EXT-X-DISCONTINUITY / a DASH Period restart. Turn on for a
            seamless wrap -- serve.py refuses to start in this mode against a source baked with a 32-bit
            tfdt (loop-dee-loop/SCOPE.md &sect;12).
          </FieldHelp>
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
      <div class="flex flex-column gap-1">
        <label for="dash-signal-format">DASH SCTE-35 signal format</label>
        <Select id="dash-signal-format" v-model="form.dash_signal_format" :options="dashSignalFormatOptions" option-label="label" option-value="value" />
      </div>
      <div class="flex flex-column gap-1">
        <label for="dash-descriptor-mode">DASH coincident descriptor mode</label>
        <Select id="dash-descriptor-mode" v-model="form.dash_descriptor_mode" :options="dashDescriptorModeOptions" option-label="label" option-value="value" />
      </div>
      <div class="flex align-items-center gap-2">
        <Checkbox v-model="form.increment_event_ids" binary input-id="increment-event-ids" />
        <label for="increment-event-ids">Increment SCTE-35 event ids each loop (HLS + DASH)</label>
        <FieldHelp label="Increment SCTE-35 event ids">
          Off (default) repeats the same event id every loop -- easiest to test against. On bumps
          each id by loop_number &times; a shared step (a power of 10 above the channel's largest
          base id, e.g. base ids 100-190 &rarr; step 1000, so loop 1 emits 1100/1190, loop 2 emits
          2100/2190, ...) -- predictable from wall-clock time alone, and wraps back to the base id
          at the 32-bit SCTE-35 ceiling.
        </FieldHelp>
      </div>
      <div class="flex flex-column gap-1">
        <div class="flex align-items-center gap-1">
          <label for="daterange-id-format">HLS DATERANGE ID format</label>
          <DaterangeIdFormatHelp />
        </div>
        <InputText id="daterange-id-format" v-model="form.daterange_id_format" />
      </div>
    </template>

    <div>
      <Button label="Save channel definition" icon="pi pi-check" :loading="saving" :disabled="Boolean(nameError)" @click="submit" />
    </div>
  </div>
</template>
