<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import ConfirmPopup from 'primevue/confirmpopup'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import ItsAliveBanner from '../components/ItsAliveBanner.vue'
import JobPanel from '../components/JobPanel.vue'
import PlaybackPanel from '../components/PlaybackPanel.vue'
import DaterangeIdFormatHelp from '../components/DaterangeIdFormatHelp.vue'
import { alignConfirmPopup } from '../utils/confirmPopup'
import {
  createChannel,
  getChannel,
  getChannelHealth,
  getChannelOutputs,
  getChannelStatus,
  listChannels,
  listJobs,
  redeployChannel,
  sparkChannel,
  startChannel,
  stopChannel,
  terminateChannel,
  updateChannel,
  updateChannelContent,
} from '../api/client'
import type { ChannelCreatePayload, ChannelHealth, ChannelListItem, ChannelOutputs, ChannelStatus, Job } from '../api/types'
import { type Phase, PHASE_LABEL, isUpButMaybeUnreachable, listItemPhase, liveStatusPhase, phaseSeverity } from '../utils/channelPhase'

const props = defineProps<{ name: string }>()

const status = ref<ChannelStatus | null>(null)
const outputs = ref<ChannelOutputs | null>(null)
const health = ref<ChannelHealth | null>(null)
const config = ref<Record<string, unknown> | null>(null)
const statusError = ref('')
const healthError = ref('')
// Row for this channel from the list endpoint (`channel.py list`) -- kept
// around purely so the top-of-page "State" tag can use the exact same
// listItemPhase() computation as the list page's State column, rather
// than each page independently re-deriving what should be the same
// answer (see channelPhase.ts's module comment). `phase` below stays on
// liveStatusPhase()/getChannelStatus for action-gating, since that's a
// single-channel live check with no soft-fail fallback -- it's the more
// trustworthy signal for "is it safe to Start/Stop right now", while
// listPhase is what should visually match the list.
const listItem = ref<ChannelListItem | null>(null)
const loading = ref(false)
const activeJobId = ref<string | null>(null)
const activeAction = ref('')
const activeActionEta = ref('')
// Phase captured right as an action starts, so onJobFinished can tell a
// genuine not-running -> running transition (worth celebrating) apart from
// e.g. Update content on an already-running channel.
const phaseBeforeAction = ref<Phase | null>(null)
const itsAliveBanner = ref<InstanceType<typeof ItsAliveBanner> | null>(null)
const playbackPanel = ref<InstanceType<typeof PlaybackPanel> | null>(null)

const editing = ref(false)
const editSaving = ref(false)
const editError = ref('')
const editForm = reactive<ChannelCreatePayload>({
  name: props.name,
  backend: 'ecs-express',
  region: '',
  bucket_name: '',
  content_folder: '',
  source_path: '',
  source_kind: 'playlist',
  segment_duration: 4.0,
  dvr_window_seconds: 30,
  hls_format: 'cmaf',
  hls_ts_mux_audio: true,
  continuous_timeline: true,
  port: 8080,
  cpu: 256,
  memory: 512,
  daterange_mode: 'shared',
  cue_tags: 'none',
  increment_event_ids: false,
  daterange_id_format: '{segcode}-{eventid}-{loop}',
  dash_signal_format: 'binary',
  dash_descriptor_mode: 'shared',
})
const editIsEcsExpress = computed(() => editForm.backend === 'ecs-express')
const editIsLocalDocker = computed(() => editForm.backend === 'local-docker')
const editUsesChannelSection = computed(() => editIsEcsExpress.value || editIsLocalDocker.value)
const editIsArchiveSource = computed(() => editForm.source_kind === 'archive')
const hlsFormatOptions = [
  { label: 'CMAF (fragmented MP4)', value: 'cmaf' },
  { label: 'MPEG-TS', value: 'ts' },
]
// Mirrors ChannelNew.vue's autoPort/lastExplicitPort split -- kept as
// separate UI state so InputNumber always gets a number, and toggling
// off "auto" restores the last explicit port instead of losing it.
const editAutoPort = ref(false)
const editLastExplicitPort = ref(8080)
watch(editAutoPort, (auto) => {
  if (auto) {
    editLastExplicitPort.value = typeof editForm.port === 'number' ? editForm.port : editLastExplicitPort.value
    editForm.port = 'auto'
  } else {
    editForm.port = editLastExplicitPort.value
  }
})
watch(
  () => editForm.backend,
  (backend) => {
    if (backend !== 'local-docker' && editAutoPort.value) {
      editAutoPort.value = false
    }
  },
)

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

const toast = useToast()
const confirm = useConfirm()
let healthTimer: ReturnType<typeof setInterval> | null = null

function section(key: string): Record<string, unknown> {
  return (config.value?.[key] as Record<string, unknown>) ?? {}
}

// Editing is allowed regardless of lifecycle phase (running/stopped/never
// deployed) -- this only rewrites the local TOML config file, it never
// touches AWS or the local container/stack by itself. Apply the change
// afterwards with Redeploy/Refresh.
function startEdit() {
  if (!config.value) return
  const deploy = section('deploy')
  const aws = section('aws')
  const s3 = section('s3')
  const input = section('input')
  const packaging = section('packaging')
  const express = section('express')
  const docker = section('docker')
  const markers = section('markers')
  const backend = (deploy.backend as ChannelCreatePayload['backend']) ?? 'ecs-express'
  // `port` lives in [express] for ecs-express, [docker] for local-docker
  // (see its_a_live.generate_toml()) -- one form field either way.
  const portSection = backend === 'local-docker' ? docker : express
  Object.assign(editForm, {
    name: props.name,
    backend,
    region: String(aws.region ?? ''),
    bucket_name: String(s3.bucket_name ?? ''),
    content_folder: String(s3.content_folder ?? ''),
    source_path: String(input.source_path ?? ''),
    source_kind: input.source_kind === 'archive' || /(?:^|[\\/])manifest\.json$/i.test(String(input.source_path ?? ''))
      ? 'archive'
      : 'playlist',
    segment_duration: Number(packaging.segment_duration ?? 4.0),
    dvr_window_seconds: Number(packaging.dvr_window_seconds ?? 30),
    hls_format: (packaging.hls_format as ChannelCreatePayload['hls_format']) ?? 'cmaf',
    hls_ts_mux_audio: Boolean(packaging.hls_ts_mux_audio ?? true),
    continuous_timeline: Boolean(packaging.continuous_timeline ?? true),
    port: portSection.port === 'auto' ? 'auto' : Number(portSection.port ?? 8080),
    cpu: Number(express.cpu ?? 256),
    memory: Number(express.memory ?? 512),
    daterange_mode: (markers.daterange_mode as ChannelCreatePayload['daterange_mode']) ?? 'shared',
    cue_tags: (markers.cue_tags as ChannelCreatePayload['cue_tags']) ?? 'none',
    increment_event_ids: Boolean(markers.increment_event_ids ?? false),
    daterange_id_format: String(markers.daterange_id_format ?? '{segcode}-{eventid}-{loop}'),
    dash_signal_format: (markers.dash_signal_format as ChannelCreatePayload['dash_signal_format']) ?? 'binary',
    dash_descriptor_mode: (markers.dash_descriptor_mode as ChannelCreatePayload['dash_descriptor_mode']) ?? 'shared',
  })
  editAutoPort.value = editForm.port === 'auto'
  editLastExplicitPort.value = typeof editForm.port === 'number' ? editForm.port : editLastExplicitPort.value
  editError.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editError.value = ''
}

async function saveEdit() {
  editSaving.value = true
  editError.value = ''
  try {
    await updateChannel(props.name, editForm)
    editing.value = false
    await loadConfig()
    toast.add({ severity: 'success', summary: 'Config saved', detail: 'Update content to rebake packaging changes on a running channel.', life: 5000 })
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e)
  } finally {
    editSaving.value = false
  }
}

// Flattened "section.key: value" rows for the read-only config panel, in
// TOML order. Local Docker configs may still contain placeholder AWS/S3
// values, but those sections are unused by that backend.
const configRows = computed(() => {
  if (!config.value) return []
  const rows: { section: string; key: string; value: string }[] = []
  const isLocalDocker = (config.value.deploy as Record<string, unknown> | undefined)?.backend === 'local-docker'
  const input = config.value.input as Record<string, unknown> | undefined
  const sourcePath = String(input?.source_path ?? '')
  // Archive imports use grave-robber's segment-list manifest.json. Sparse
  // baking preserves each segment's declared source duration; the channel's
  // packaging.segment_duration value is not used for this input mode. The
  // path match supports configs created before input.source_kind was added.
  const isArchiveSegmentList = input?.source_kind === 'archive' || /(?:^|[\\/])manifest\.json$/i.test(sourcePath)
  for (const [section, fields] of Object.entries(config.value)) {
    if (isLocalDocker && (section === 'aws' || section === 's3')) continue
    if (!fields || typeof fields !== 'object') continue
    for (const [key, value] of Object.entries(fields as Record<string, unknown>)) {
      const displayValue = isArchiveSegmentList && section === 'packaging' && key === 'segment_duration'
        ? 'as source'
        : String(value)
      rows.push({ section, key, value: displayValue })
    }
  }
  return rows
})

// Friendly display names for known TOML section keys -- anything not
// listed here (e.g. a future section) falls back to a capitalized form
// of the raw key, so the panel never silently drops a section.
const SECTION_TITLES: Record<string, string> = {
  deploy: 'Deploy', aws: 'AWS', s3: 'S3', input: 'Input', bake: 'Bake',
  markers: 'Markers', packaging: 'Packaging', express: 'Express', docker: 'Docker',
}
function sectionTitle(section: string): string {
  return SECTION_TITLES[section] ?? section.charAt(0).toUpperCase() + section.slice(1)
}

// `configRows` grouped back into per-section blocks, for the read-only
// panel's visual sections (one card per TOML section, with a real title
// instead of a raw "[section]" label).
const configSections = computed(() => {
  const sections: { section: string; title: string; rows: { key: string; value: string }[] }[] = []
  for (const row of configRows.value) {
    const current = sections[sections.length - 1]
    if (!current || current.section !== row.section) {
      sections.push({ section: row.section, title: sectionTitle(row.section), rows: [{ key: row.key, value: row.value }] })
    } else {
      current.rows.push({ key: row.key, value: row.value })
    }
  }
  return sections
})

// A missing CloudFormation stack is an expected, common state (not yet
// deployed) rather than a real error -- `channel.py status`'s traceback
// always contains this phrase in that case. Anything else is a genuine
// failure (bad AWS creds, wrong region, etc.) and gets shown in full.
const statusErrorIsMissingStack = computed(() => /does not exist/.test(statusError.value))

// Lifecycle phase inferred from the last successful /status call, used to
// gate which actions make sense right now, and to color the status Tag
// the same way ChannelList.vue does (see utils/channelPhase.ts).
// `getChannelStatus` throws (and `statusError` gets set) whenever the
// stack doesn't exist yet, so that's our "not deployed" signal -- see
// igor/AGENTS.md's API surface table.
const phase = computed<Phase>(() => {
  if (!status.value) return statusErrorIsMissingStack.value ? 'not-deployed' : 'unknown'
  return liveStatusPhase(status.value)
})

// Whenever the channel (re-)reaches "It's Alive!", force both players to
// tear down and restart from the live edge. PlaybackPanel already does this
// itself when the playback URLs change or when it gets freshly mounted (via
// `v-if="showPlayback"` toggling off/on), but a redeploy/restart can leave
// the URLs identical while a player instance stays attached across the
// gap -- left alone, it would just keep showing the tail end of the DVR
// window from the previous run instead of jumping to the new stream.
watch(phase, (next, prev) => {
  if (next === 'alive' && prev !== 'alive') playbackPanel.value?.reloadPlayers()
})

// The State tag at the top of the page: same computation, same labels,
// same colors as ChannelList.vue's State column (listItemPhase), so a
// channel never reads differently depending on which page you're
// looking at it from. Falls back to the live-only `phase` above until
// the list row has loaded (or if this channel is somehow missing from
// the list response) -- e.g. right on first mount, before loadListPhase()
// resolves.
const listPhase = computed<Phase>(() => (listItem.value ? listItemPhase(listItem.value) : phase.value))

interface ActionDef {
  key: string
  label: string
  icon: string
  severity?: 'secondary' | 'success' | 'danger' | 'warn'
  outlined?: boolean
  description: string
  eta: string
  fn: () => Promise<{ id: string }>
  disabled: () => boolean
  disabledReason: () => string
  // When set, clicking the button shows a confirm popup with this message
  // before actually invoking `fn` -- reserved for actions that are costly
  // to reverse (see Terminate), matching ChannelList.vue's Delete pattern.
  confirmMessage?: string
}

// Buttons keep their "live" color (green Start, red Stop, ...) only while
// they're actually clickable. Disabled, they all fall back to the same
// muted/outlined style so it's unambiguous at a glance which ones are
// currently off-limits, instead of a dimmed-but-still-green button reading
// as "kind of available".
function effectiveSeverity(a: ActionDef) {
  return a.disabled() ? 'secondary' : a.severity
}
function effectiveOutlined(a: ActionDef) {
  return a.disabled() ? true : a.outlined
}

// Text below is written per-backend (not "X for ecs-express, Y for
// aws-media", and not "(no AWS involved)" / other phrasing that only makes
// sense as a contrast with a DIFFERENT backend) so it reads as a
// self-contained description of THIS channel, not a reference sheet for
// every backend its-a-live supports.
const backend = computed(() => status.value?.backend)

function byBackend(localDocker: string, ecsExpress: string, awsMedia: string): string {
  if (backend.value === 'local-docker') return localDocker
  if (backend.value === 'ecs-express') return ecsExpress
  return awsMedia
}

const firstDeployAction = computed<ActionDef>(() => ({
  key: 'create',
  label: 'Galvanise',
  icon: 'pi pi-bolt',
  description: byBackend(
    'Bakes the content locally and starts the container. Equivalent to running Spark then Start below.',
    'One-time, first deploy of this channel: bakes the content locally, stages it to S3, deploys the ECS/CloudFront stack, then starts it.',
    'One-time, first deploy of this channel: uploads the content to S3, deploys the MediaLive/MediaPackage stack, then starts it.',
  ),
  eta: byBackend(
    '~10-60s for the bake, plus a one-time Docker image build the first time (a few minutes).',
    '~5-10 min -- Docker image build + push, ECS task startup, and a brand-new CloudFront distribution, which alone typically takes several minutes to propagate. This is normal, not a hang.',
    '~5-10 min -- MediaLive channel provisioning and startup. This is normal, not a hang.',
  ),
  fn: () => createChannel(props.name),
  disabled: () => (backend.value === 'local-docker' ? isUpButMaybeUnreachable(phase.value) : phase.value !== 'not-deployed'),
  disabledReason: () =>
    backend.value === 'local-docker'
      ? 'Already running -- use the actions on the right to manage it.'
      : 'Already deployed -- use the actions on the right to manage it, or Redeploy for stack/config changes.',
}))

// The "ship content" action: while the channel is running, staging new
// content is only useful paired with picking it up, so this is Spark+
// Refresh combined into one "Update content" job (channel.py's `update`
// command) -- the docs never show one without the other for a running
// channel anyway. Before first deploy (or while stopped), there's nothing
// to refresh yet, so this is plain Spark instead -- still never disabled
// by phase, since staging content has never depended on the stack/
// container existing (see channel.py's docstring: "content MUST be staged
// before the channel stack is deployed").
const contentAction = computed<ActionDef>(() => {
  if (isUpButMaybeUnreachable(phase.value)) {
    return {
      key: 'update',
      label: 'Update content',
      icon: 'pi pi-refresh',
      severity: 'secondary',
      description: byBackend(
        'Bakes the franken-ts output locally and recreates the container from it, in one step -- the routine way to ship new content to a running channel.',
        'Bakes the franken-ts output locally, stages it to S3, then re-syncs the running channel -- the routine way to ship new content, in one step.',
        'Uploads the franken-ts output to S3, then cycles the channel to pick it up -- the routine way to ship new content, in one step.',
      ),
      eta: byBackend('~15-90s.', '~40-90s.', 'A full stop/start cycle (a few minutes) on top of the bake/upload.'),
      fn: () => updateChannelContent(props.name),
      disabled: () => false,
      disabledReason: () => '',
    }
  }
  return {
    key: 'spark',
    label: 'Spark',
    icon: 'pi pi-sparkles',
    severity: 'secondary',
    description: byBackend(
      'Bakes the franken-ts output locally into the loop package the container serves. Run this after building new content, before Start picks it up.',
      'Bakes the franken-ts output locally, then stages the result to S3. Run this after building new content, before Start picks it up.',
      'Uploads the franken-ts output to S3. Run this after building new content, before Start picks it up.',
    ),
    eta: '~10-60s, depending on content size.',
    fn: () => sparkChannel(props.name),
    disabled: () => false,
    disabledReason: () => '',
  }
})

// Stream: is content actually playing right now. Available for every
// backend -- these buttons never need to be hidden.
const streamActions = computed<ActionDef[]>(() => [
  contentAction.value,
  {
    key: 'start',
    label: 'Start',
    icon: 'pi pi-play',
    severity: 'success',
    description: byBackend(
      'Goes live: starts the local-docker container. Requires content to already be Sparked at least once.',
      'Goes live: scales the ECS Express service to 1 task. Requires content to already be Sparked at least once.',
      'Goes live: starts the MediaLive channel. Requires content to already be Sparked at least once.',
    ),
    eta: byBackend(
      '~10-30s (container startup + health check).',
      '~30-60s (task startup + health check).',
      'A couple of minutes (MediaLive channel start).',
    ),
    fn: () => startChannel(props.name),
    // aws-media/ecs-express: `start` reads CloudFormation stack outputs, so
    // it genuinely requires the stack to already be deployed (Galvanise
    // or `cdk deploy` first) -- 'not-deployed' stays disabled.
    // local-docker: `start` IS `docker run` -- it creates the container
    // fresh, so it works straight from 'not-deployed' too, same as the
    // Galvanise button above already allows for this backend.
    disabled: () => (backend.value === 'local-docker' ? isUpButMaybeUnreachable(phase.value) : phase.value !== 'stopped'),
    disabledReason: () =>
      backend.value === 'local-docker'
        ? 'Channel is already running.'
        : phase.value === 'not-deployed'
          ? 'Requires the channel to be deployed first.'
          : 'Channel is already running.',
  },
  {
    key: 'stop',
    label: 'Stop',
    icon: 'pi pi-stop',
    severity: 'danger',
    description: byBackend(
      'Stops and removes the local container.',
      'Stops paying for compute: scales the ECS Express service to 0 tasks (the shared ALB stays up for other channels).',
      'Stops the MediaLive channel.',
    ),
    eta: byBackend('~5-10s.', '~10-30s.', 'Up to a couple of minutes.'),
    fn: () => stopChannel(props.name),
    disabled: () => !isUpButMaybeUnreachable(phase.value),
    disabledReason: () =>
      phase.value === 'not-deployed' ? 'Requires the channel to be deployed first.' : 'Channel is already stopped.',
  },
])

// Infrastructure: does the stack/container exist at all. `redeploy` only
// means anything where there's a CloudFormation stack to repair/reapply --
// for local-docker it's a pure alias of Refresh content server-side
// (channel.py's cmd_redeploy calls cmd_refresh verbatim), so it's omitted
// entirely rather than shown as a redundant/confusing button.
const infrastructureActions = computed<ActionDef[]>(() => {
  if (backend.value === 'local-docker') return []
  return [
    {
      key: 'redeploy',
      label: 'Redeploy',
      icon: 'pi pi-wrench',
      severity: 'warn',
      outlined: true,
      description: byBackend(
        'Recreates the local container from whatever was last Sparked (same as the content-update action above).',
        'Deletes a broken/rolled-back stack if needed, then cdk deploys the current config again. Use this after editing the channel config, or to recover from a failed Galvanise.',
        'Deletes a broken/rolled-back stack if needed, then cdk deploys the current config again. Use this after editing the channel config, or to recover from a failed Galvanise.',
      ),
      eta: byBackend(
        '~10-30s.',
        '~1-2 min for a small config change; ~5-10 min if the stack has to be deleted and recreated (e.g. after a failed Galvanise) -- CloudFront/ECS propagation, not a hang.',
        '~1-2 min for a small config change; ~5-10 min if the stack has to be deleted and recreated (e.g. after a failed Galvanise).',
      ),
      fn: () => redeployChannel(props.name),
      disabled: () => phase.value === 'unknown' && !statusError.value,
      disabledReason: () => 'Status is still loading.',
    },
    {
      key: 'terminate',
      label: 'Terminate',
      icon: 'pi pi-trash',
      severity: 'danger',
      outlined: true,
      description: byBackend(
        '', // never rendered -- local-docker has no infrastructureActions at all
        'Deletes the CloudFormation stack for good (ECS service, CloudFront distribution, load balancer, ...) -- stops billing. Uploaded content in S3 is not deleted. Use Galvanise above to bring it back.',
        'Deletes the CloudFormation stack for good (MediaLive channel, MediaPackage channel/endpoints) -- stops billing. Uploaded content in S3 is not deleted. Use Galvanise above to bring it back.',
      ),
      eta: byBackend(
        '',
        '~1-2 min, though CloudFront can take 15+ min to finish deleting its distribution in the background after this returns.',
        '~1-5 min for MediaLive/MediaPackage resource deletion.',
      ),
      fn: () => terminateChannel(props.name),
      disabled: () => phase.value !== 'stopped' && phase.value !== 'failed',
      disabledReason: () => {
        if (phase.value === 'not-deployed') return 'Nothing to terminate -- channel is not deployed.'
        if (isUpButMaybeUnreachable(phase.value)) return 'Stop the channel before terminating its stack.'
        if (phase.value === 'transitioning' || phase.value === 'deleting') return 'Status is transitioning -- wait for it to settle.'
        return 'Status is still loading.'
      },
      confirmMessage: `Permanently delete the deployed stack for "${props.name}"? Uploaded content in S3 is kept, but the channel will need a fresh Galvanise to run again.`,
    },
  ]
})

// local-docker has no stack outputs (see /outputs route) -- its playback
// URLs come straight from /status instead (http://localhost:<port>/...).
const playbackHlsUrl = computed(() =>
  status.value?.backend === 'local-docker' ? status.value.hls_url : outputs.value?.HlsPlaybackUrl,
)
const playbackDashUrl = computed(() =>
  status.value?.backend === 'local-docker' ? status.value.dash_url : outputs.value?.DashPlaybackUrl,
)

// CloudFormation stack outputs (and hence playbackHlsUrl/playbackDashUrl)
// stay populated whether or not the channel is actually serving -- a
// stopped ecs-express service (scaled to 0) or an IDLE MediaLive channel
// both still have an HlsPlaybackUrl/DashPlaybackUrl output. Gating on the
// live phase too (not just URL presence) means Stop actually tears the
// players down (PlaybackPanel's onBeforeUnmount destroys both) instead of
// leaving them mounted and auto-playing/erroring against a dead stream.
const showPlayback = computed(
  () => (playbackHlsUrl.value || playbackDashUrl.value) && phase.value !== 'stopped' && phase.value !== 'not-deployed',
)

async function loadConfig() {
  try {
    config.value = await getChannel(props.name)
  } catch (e) {
    config.value = null
  }
}

async function loadStatus() {
  statusError.value = ''
  try {
    ;[status.value, outputs.value] = await Promise.all([
      getChannelStatus(props.name),
      getChannelOutputs(props.name),
    ])
  } catch (e) {
    statusError.value = e instanceof Error ? e.message : String(e)
  }
}

// There's no single-channel equivalent of `channel.py list`'s enriched
// row (stack_status/live_status/min_tasks/reachable) -- only the full
// list endpoint computes those. Best-effort: pull the whole list and
// pick out this channel's row purely so listPhase can reuse
// listItemPhase() unchanged. Silently keeps the previous value on
// failure (e.g. a transient error) rather than blinking the State tag
// back to "unknown".
async function loadListPhase() {
  try {
    const rows = await listChannels()
    listItem.value = rows.find((c) => c.name === props.name) ?? null
  } catch {
    // ignore -- listPhase falls back to the live-only `phase` computed
  }
}

async function loadHealth() {
  if (status.value?.backend !== 'ecs-express' && status.value?.backend !== 'local-docker') return
  try {
    health.value = await getChannelHealth(props.name)
    healthError.value = ''
  } catch (e) {
    healthError.value = e instanceof Error ? e.message : String(e)
  }
}

// Actions with a `confirmMessage` (currently just Terminate) show a
// confirm popup before running -- everything else runs immediately on
// click, same as before this existed.
function onActionClick(event: MouseEvent, a: ActionDef) {
  if (!a.confirmMessage) {
    run(a.key, a.fn, a.eta)
    return
  }
  const target = (event.target as HTMLElement).closest('button') ?? (event.target as HTMLElement)
  confirm.require({
    target,
    message: a.confirmMessage,
    acceptClass: 'p-button-danger',
    accept: () => run(a.key, a.fn, a.eta),
  })
  alignConfirmPopup(target)
}

async function run(action: string, fn: () => Promise<{ id: string }>, eta = '') {
  loading.value = true
  activeAction.value = action
  activeActionEta.value = eta
  phaseBeforeAction.value = phase.value
  try {
    const job = await fn()
    activeJobId.value = job.id
  } catch (e) {
    toast.add({ severity: 'error', summary: `${action} failed`, detail: e instanceof Error ? e.message : String(e), life: 6000 })
  } finally {
    loading.value = false
  }
}

async function onJobFinished(job: Job) {
  toast.add({
    severity: 'info',
    summary: `${activeAction.value} finished`,
    detail: `See the job log above for details.`,
    life: 4000,
  })
  const wasRunning = phaseBeforeAction.value === 'alive'
  await loadStatus()
  if (job.status === 'succeeded' && !wasRunning && phase.value === 'alive') {
    itsAliveBanner.value?.trigger()
  }
}

async function reattachRunningJob() {
  try {
    const jobs = await listJobs(props.name)
    const running = jobs
      .filter((j) => j.status === 'running' || j.status === 'queued')
      .sort((a, b) => b.created_at - a.created_at)[0]
    if (running) {
      activeAction.value = running.type
      activeActionEta.value = ''
      phaseBeforeAction.value = null
      activeJobId.value = running.id
    }
  } catch {
    // Best-effort -- if the jobs endpoint is unreachable, just fall back
    // to no active job rather than blocking the rest of the page.
  }
}

function reload() {
  status.value = null
  outputs.value = null
  health.value = null
  config.value = null
  listItem.value = null
  activeJobId.value = null
  reattachRunningJob()
  loadStatus()
  loadListPhase()
  loadHealth()
  loadConfig()
}

// Auto-poll cadence for the live status + list-derived State tag --
// independent of `healthTimer` below and of JobPanel's own 1.5s job
// polling, so the header stays fresh even when nothing on this page is
// actively running a job (e.g. the channel changed state from
// elsewhere: another tab, ECS scaling, MediaLive recovering, ...).
const STATUS_POLL_MS = 8000
let statusTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  reload()
  healthTimer = setInterval(loadHealth, 5000)
  statusTimer = setInterval(() => {
    loadStatus()
    loadListPhase()
  }, STATUS_POLL_MS)
})
onBeforeUnmount(() => {
  if (healthTimer) clearInterval(healthTimer)
  if (statusTimer) clearInterval(statusTimer)
})
watch(() => props.name, reload)
</script>

<template>
  <div class="flex flex-column gap-4">
    <div class="flex align-items-center gap-2">
      <h2 class="m-0">{{ name }}</h2>
      <div class="state-badge">
        <Tag class="state-badge-tag" :value="PHASE_LABEL[listPhase]" :severity="phaseSeverity(listPhase)" />
        <span
          v-if="status"
          class="state-badge-drawer text-color-secondary text-xs"
          :title="'Raw backend status: ' + status.status"
        >
          {{ status.status
          }}<template v-if="status.reachable != null">{{ status.reachable ? ', reachable' : ', unreachable' }}</template
          ><template v-if="status.min_tasks != null">, {{ status.min_tasks }}/{{ status.max_tasks }} tasks</template>
        </span>
      </div>
      <span
        v-if="listItem && listPhase !== phase"
        class="pi pi-exclamation-triangle text-yellow-600 text-sm"
        :title="`List and live status briefly disagree (list: ${PHASE_LABEL[listPhase]}, live: ${PHASE_LABEL[phase]}) -- usually settles within a poll or two.`"
      />
      <Tag v-if="status" class="ml-auto" :value="status.backend" />
    </div>

    <Message v-if="statusError && !statusErrorIsMissingStack" severity="warn">
      <div>Could not fetch live status.</div>
      <details class="mt-2">
        <summary class="cursor-pointer text-sm">Show details</summary>
        <pre class="job-log mt-2">{{ statusError }}</pre>
      </details>
    </Message>

    <Message v-if="phase === 'unreachable'" severity="error" :closable="false">
      Infrastructure reports running, but HlsPlaybackUrl/DashPlaybackUrl did not return a manifest
      on the last check. Common causes: a MediaPackage/CloudFront endpoint still propagating right
      after deploy, an empty target group, or content not actually Sparked/Started yet. Re-check
      with Refresh, or re-visit this page in a minute if you just deployed.
    </Message>

    <PlaybackPanel
      v-if="showPlayback"
      ref="playbackPanel"
      :hls-url="playbackHlsUrl"
      :dash-url="playbackDashUrl"
      :health="health"
      :health-error="status?.backend === 'ecs-express' || status?.backend === 'local-docker' ? healthError : ''"
    />

    <div class="channel-detail-layout">
      <div class="flex flex-column gap-4 channel-actions">
        <div class="flex flex-column gap-2">
          <h3 class="m-0 text-sm text-color-secondary uppercase">First deploy</h3>
          <div
            class="flex align-items-start gap-3 p-3 border-round"
            :class="firstDeployAction.disabled() ? 'surface-100' : 'surface-card'"
            style="border: 2px solid var(--primary-color)"
          >
            <Button
              :label="firstDeployAction.label"
              :icon="firstDeployAction.icon"
              :severity="effectiveSeverity(firstDeployAction)"
              :outlined="effectiveOutlined(firstDeployAction)"
              :disabled="firstDeployAction.disabled()"
              :loading="loading && activeAction === firstDeployAction.key"
              style="min-width: 11rem"
              @click="run(firstDeployAction.key, firstDeployAction.fn, firstDeployAction.eta)"
            />
            <div class="flex flex-column gap-1 text-sm">
              <div :class="firstDeployAction.disabled() ? 'text-color-secondary' : 'text-color'">{{ firstDeployAction.description }}</div>
              <div class="text-color-secondary text-xs"><i class="pi pi-clock mr-1" />{{ firstDeployAction.eta }}</div>
              <div v-if="firstDeployAction.disabled()" class="text-color-secondary font-italic">{{ firstDeployAction.disabledReason() }}</div>
            </div>
          </div>
        </div>

        <div class="flex flex-column gap-2">
          <h3 class="m-0 text-sm text-color-secondary uppercase">Stream</h3>
          <Message v-if="phase === 'not-deployed'" severity="secondary" :closable="false">
            {{
              byBackend(
                'Spark and Start can both run now -- Start builds/launches the container directly, no separate deploy step for this backend.',
                'Spark can run now to stage content ahead of the first deploy. Start/Stop need the channel deployed first -- run Galvanise above.',
                'Spark can run now to stage content ahead of the first deploy. Start/Stop need the channel deployed first -- run Galvanise above.',
              )
            }}
          </Message>
          <div
            v-for="a in streamActions"
            :key="a.key"
            class="flex align-items-start gap-3 p-3 border-round"
            :class="a.disabled() ? 'surface-100' : 'surface-card'"
            style="border: 1px solid var(--surface-border)"
          >
            <Button
              :label="a.label"
              :icon="a.icon"
              :severity="effectiveSeverity(a)"
              :outlined="effectiveOutlined(a)"
              :disabled="a.disabled()"
              :loading="loading && activeAction === a.key"
              style="min-width: 11rem"
              @click="run(a.key, a.fn, a.eta)"
            />
            <div class="flex flex-column gap-1 text-sm">
              <div :class="a.disabled() ? 'text-color-secondary' : 'text-color'">{{ a.description }}</div>
              <div class="text-color-secondary text-xs"><i class="pi pi-clock mr-1" />{{ a.eta }}</div>
              <div v-if="a.disabled()" class="text-color-secondary font-italic">{{ a.disabledReason() }}</div>
            </div>
          </div>
        </div>

        <div v-if="infrastructureActions.length" class="flex flex-column gap-2">
          <h3 class="m-0 text-sm text-color-secondary uppercase">Infrastructure</h3>
          <div
            v-for="a in infrastructureActions"
            :key="a.key"
            class="flex align-items-start gap-3 p-3 border-round"
            :class="a.disabled() ? 'surface-100' : 'surface-card'"
            style="border: 1px solid var(--surface-border)"
          >
            <Button
              :label="a.label"
              :icon="a.icon"
              :severity="effectiveSeverity(a)"
              :outlined="effectiveOutlined(a)"
              :disabled="a.disabled()"
              :loading="loading && activeAction === a.key"
              style="min-width: 11rem"
              @click="onActionClick($event, a)"
            />
            <div class="flex flex-column gap-1 text-sm">
              <div :class="a.disabled() ? 'text-color-secondary' : 'text-color'">{{ a.description }}</div>
              <div class="text-color-secondary text-xs"><i class="pi pi-clock mr-1" />{{ a.eta }}</div>
              <div v-if="a.disabled()" class="text-color-secondary font-italic">{{ a.disabledReason() }}</div>
            </div>
          </div>
          <details v-if="outputs" class="text-sm">
            <summary class="cursor-pointer text-color-secondary">Stack outputs (raw)</summary>
            <pre class="job-log mt-2">{{ JSON.stringify(outputs, null, 2) }}</pre>
          </details>
        </div>
      </div>

      <div class="flex flex-column gap-2 p-3 border-round surface-card channel-config" style="border: 1px solid var(--surface-border)">
        <div class="flex align-items-center justify-content-between">
          <h3 class="m-0">Configuration</h3>
          <Button
            v-if="config && !editing"
            label="Edit"
            icon="pi pi-pencil"
            size="small"
            text
            @click="startEdit"
          />
        </div>
        <div v-if="!config" class="text-color-secondary text-sm">Loading...</div>

        <div v-else-if="!editing" class="flex flex-column gap-2">
          <div
            v-for="sec in configSections"
            :key="sec.section"
            class="surface-100 border-round p-2"
            style="border-left: 3px solid var(--p-primary-color, #b91c1c)"
          >
            <div class="text-color-secondary font-semibold mb-1" style="font-size: 0.7rem; letter-spacing: 0.06em; text-transform: uppercase">
              {{ sec.title }}
            </div>
            <table class="text-sm config-table">
              <tbody>
                <tr v-for="row in sec.rows" :key="row.key">
                  <td class="pr-3 text-color-secondary vertical-align-top config-key">{{ row.key }}</td>
                  <td class="font-mono config-value">{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-else class="flex flex-column gap-3">
          <Message v-if="editError" severity="error" :closable="false">{{ editError }}</Message>

          <section class="config-group">
            <h4 class="config-group-title">Channel &amp; source</h4>
            <div class="config-fields">
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">Backend (immutable)</label>
                <InputText :model-value="editForm.backend" disabled />
              </div>
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">Source kind (immutable)</label>
                <InputText :model-value="editForm.source_kind" disabled />
              </div>
              <div class="flex flex-column gap-1 config-field-wide">
                <label class="text-xs text-color-secondary">Source path</label>
                <InputText v-model="editForm.source_path" />
              </div>
            </div>
          </section>

          <section v-if="!editIsLocalDocker" class="config-group">
            <h4 class="config-group-title">AWS / S3</h4>
            <div class="config-fields">
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">AWS region</label>
                <InputText v-model="editForm.region" />
              </div>
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">S3 bucket name</label>
                <InputText v-model="editForm.bucket_name" />
              </div>
              <div class="flex flex-column gap-1 config-field-wide">
                <label class="text-xs text-color-secondary">S3 content folder</label>
                <InputText v-model="editForm.content_folder" />
              </div>
            </div>
          </section>

          <template v-if="editUsesChannelSection">
            <section class="config-group">
              <h4 class="config-group-title">HLS packaging</h4>
              <div class="config-fields">
                <div class="flex flex-column gap-1">
                  <label class="text-xs text-color-secondary">Segment duration (s)</label>
                  <div v-if="editIsArchiveSource" class="text-color-secondary p-2 surface-ground border-round">As source</div>
                  <InputNumber v-else v-model="editForm.segment_duration" :min-fraction-digits="1" fluid />
                </div>
                <div class="flex flex-column gap-1">
                  <label class="text-xs text-color-secondary">DVR window (s)</label>
                  <InputNumber v-model="editForm.dvr_window_seconds" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">HLS segment format</label>
                  <Select v-model="editForm.hls_format" :options="hlsFormatOptions" option-label="label" option-value="value" fluid />
                </div>
                <div v-if="editForm.hls_format === 'ts'" class="flex align-items-center gap-2 config-field-wide">
                  <Checkbox v-model="editForm.hls_ts_mux_audio" binary input-id="edit-hls-ts-mux-audio" />
                  <label for="edit-hls-ts-mux-audio" class="text-xs text-color-secondary">Mux audio into each HLS TS video segment</label>
                </div>
              </div>
            </section>

            <section class="config-group">
              <h4 class="config-group-title">Serving</h4>
              <div class="config-fields">
                <div class="flex align-items-center gap-2 config-field-wide">
                  <Checkbox v-model="editForm.continuous_timeline" binary input-id="edit-continuous-timeline" />
                  <label for="edit-continuous-timeline" class="text-xs text-color-secondary">
                    Continuous timeline across the loop wrap
                  </label>
                </div>
                <div class="flex flex-column gap-1" :class="{ 'config-field-wide': editIsLocalDocker }">
                  <label class="text-xs text-color-secondary">Serve port</label>
                  <div v-if="editIsLocalDocker" class="flex align-items-center gap-2">
                    <Checkbox v-model="editAutoPort" binary input-id="edit-auto-port" />
                    <label for="edit-auto-port" class="text-sm">Auto-select a free port</label>
                  </div>
                  <InputNumber v-if="!editIsLocalDocker || !editAutoPort" v-model="editForm.port as number" :use-grouping="false" fluid />
                  <div v-else class="text-color-secondary text-xs">
                    A free port (8080-8179) is picked on next start/refresh and reused afterward.
                  </div>
                </div>
                <template v-if="editIsEcsExpress">
                  <div class="flex flex-column gap-1">
                    <label class="text-xs text-color-secondary">Express CPU units</label>
                    <InputNumber v-model="editForm.cpu" :use-grouping="false" fluid />
                  </div>
                  <div class="flex flex-column gap-1">
                    <label class="text-xs text-color-secondary">Express memory (MB)</label>
                    <InputNumber v-model="editForm.memory" :use-grouping="false" fluid />
                  </div>
                </template>
              </div>
            </section>

            <section class="config-group">
              <h4 class="config-group-title">SCTE-35 signaling</h4>
              <div class="config-fields">
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">HLS DATERANGE mode</label>
                  <Select v-model="editForm.daterange_mode" :options="daterangeModeOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">HLS CUE-OUT/CUE-IN tags</label>
                  <Select v-model="editForm.cue_tags" :options="cueTagsOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">DASH SCTE-35 signal format</label>
                  <Select v-model="editForm.dash_signal_format" :options="dashSignalFormatOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">DASH coincident descriptor mode</label>
                  <Select v-model="editForm.dash_descriptor_mode" :options="dashDescriptorModeOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex align-items-center gap-2 config-field-wide">
                  <Checkbox v-model="editForm.increment_event_ids" binary input-id="edit-increment-event-ids" />
                  <label for="edit-increment-event-ids" class="text-xs text-color-secondary">Increment SCTE-35 event ids each loop (HLS + DASH)</label>
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <div class="flex align-items-center gap-1">
                    <label class="text-xs text-color-secondary" for="edit-daterange-id-format">HLS DATERANGE ID format</label>
                    <DaterangeIdFormatHelp />
                  </div>
                  <InputText id="edit-daterange-id-format" v-model="editForm.daterange_id_format" />
                </div>
              </div>
            </section>
          </template>

          <div class="flex gap-2 mt-1">
            <Button label="Save" icon="pi pi-check" size="small" :loading="editSaving" @click="saveEdit" />
            <Button label="Cancel" size="small" text :disabled="editSaving" @click="cancelEdit" />
          </div>
        </div>

        <div class="text-color-secondary text-xs mt-2">
          Editing only rewrites configs/{{ name }}.toml -- it does not
          {{ byBackend('touch the running container', 'touch AWS', 'touch AWS') }} by itself.
          Use Update content to rebake HLS packaging after saving.
        </div>
      </div>
    </div>

    <JobPanel :job-id="activeJobId" :eta-hint="activeActionEta" @finished="onJobFinished" />
    <ConfirmPopup />
    <ItsAliveBanner ref="itsAliveBanner" />
  </div>
</template>

<style scoped>
.channel-detail-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
  gap: 1.5rem;
  align-items: start;
}

.channel-actions,
.channel-config {
  min-width: 0;
}

.config-table {
  table-layout: fixed;
  width: 100%;
}

.config-key {
  width: 11rem;
  overflow-wrap: anywhere;
}

.config-value {
  overflow-wrap: anywhere;
}

.config-group {
  padding: 0.85rem;
  background: var(--p-surface-50, #f8fafc);
  border: 1px solid var(--surface-border);
  border-radius: 6px;
}

.config-group-title {
  margin: 0 0 0.85rem;
  color: var(--p-primary-color, #b91c1c);
  font-size: 0.85rem;
  font-weight: 700;
}

.config-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.85rem;
}

.config-fields > div {
  min-width: 0;
}

.config-field-wide {
  grid-column: 1 / -1;
}

.config-fields .p-inputtext {
  width: 100%;
}

@media (max-width: 1050px) {
  .channel-detail-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 560px) {
  .config-fields {
    grid-template-columns: minmax(0, 1fr);
  }

  .config-key {
    width: 7rem;
  }
}

/* The fine-grained status text reads as a drawer pulled out from under
   the State tag: tucked slightly behind/under its right edge (negative
   margin + lower z-index), open on the left where it meets the tag (no
   left border, so the two blend into one shape) and bordered on the
   other three sides. */
.state-badge {
  display: inline-flex;
  align-items: stretch;
}

.state-badge-tag {
  position: relative;
  z-index: 1;
}

.state-badge-drawer {
  position: relative;
  z-index: 0;
  display: inline-flex;
  align-items: center;
  box-sizing: border-box;
  margin-left: -0.5rem;
  padding: 0 0.5rem 0 1.25rem;
  border: 2px solid var(--p-content-border-color, #dee2e6);
  border-left: none;
  border-radius: 0 4px 4px 0;
  white-space: nowrap;
}
</style>
