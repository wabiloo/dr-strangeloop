<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import ConfirmPopup from 'primevue/confirmpopup'
import DatePicker from 'primevue/datepicker'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import BackendBadge from '../components/BackendBadge.vue'
import ItsAliveBanner from '../components/ItsAliveBanner.vue'
import JobPanel from '../components/JobPanel.vue'
import PlaybackPanel from '../components/PlaybackPanel.vue'
import EpochFields from '../components/EpochFields.vue'
import PeriodSegmentationFields from '../components/PeriodSegmentationFields.vue'
import TimeshiftFields from '../components/TimeshiftFields.vue'
import { DEFAULT_EPOCH_UTC } from '../utils/epoch'
import WindowPanel from '../components/WindowPanel.vue'
import TimeshiftPanel, { type TimeshiftPreview } from '../components/TimeshiftPanel.vue'
import { timeshiftParamsFromConfig } from '../utils/timeshift'
import DaterangeIdFormatHelp from '../components/DaterangeIdFormatHelp.vue'
import FieldHelp from '../components/FieldHelp.vue'
import {
  CONFIG_FIELD_LABEL as L,
  CONFIG_SECTION_TITLE as T,
  buildConfigSections,
  deriveSourceKind,
  periodTypesFromConfig,
  usesChannelSection,
} from '../utils/channelConfigLayout'
import { alignConfirmPopup } from '../utils/confirmPopup'
import {
  addScheduleWindow,
  createChannel,
  getChannel,
  getChannelHealth,
  getChannelOutputs,
  getChannelStatus,
  getChannelSummary,
  listJobs,
  listScheduleWindows,
  redeployChannel,
  removeScheduleWindow,
  sparkChannel,
  startChannel,
  stopChannel,
  terminateChannel,
  updateChannel,
  updateChannelContent,
} from '../api/client'
import type {
  ChannelCreatePayload,
  ChannelHealth,
  ChannelListItem,
  ChannelOutputs,
  ChannelStatus,
  Job,
  ScheduleWindow,
} from '../api/types'
import { type Phase, PHASE_LABEL, isUpButMaybeUnreachable, listItemPhase, liveStatusPhase, phaseSeverity } from '../utils/channelPhase'

const props = defineProps<{ name: string }>()
const router = useRouter()

const status = ref<ChannelStatus | null>(null)
const outputs = ref<ChannelOutputs | null>(null)
const health = ref<ChannelHealth | null>(null)
const config = ref<Record<string, unknown> | null>(null)
const statusError = ref('')
const healthError = ref('')
const scheduleWindows = ref<ScheduleWindow[]>([])
const scheduleError = ref('')
const scheduleLoading = ref(false)
const newWindowStart = ref<Date | null>(null)
const newWindowEnd = ref<Date | null>(null)
const addWindowSaving = ref(false)
const addWindowError = ref('')
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
  allow_missing_segments: false,
  segment_duration: 4.0,
  dvr_window_seconds: 30,
  epoch_utc: DEFAULT_EPOCH_UTC,
  hls_format: 'cmaf',
  hls_ts_mux_audio: true,
  continuous: false,
  period_on_segmentation: [],
  period_on_segmentation_apply: 'both',
  timeshift_enabled: true,
  timeshift_start_param: 'start',
  timeshift_end_param: 'end',
  timeshift_max_span_seconds: 21600,
  port: 8080,
  cpu: 256,
  memory: 512,
  cdn: true,
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
const editIsArchiveSource = computed(() => editForm.source_kind === 'archive' || editForm.source_kind === 'manifest')
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
  const infra = section('infrastructure')
  const sub = (key: string) => (infra[key] as Record<string, unknown>) ?? {}
  const aws = sub('aws')
  const s3 = sub('s3')
  const input = section('input')
  const packaging = section('packaging')
  const timeline = section('timeline')
  const express = sub('express')
  const docker = sub('docker')
  const markers = section('markers')
  const editTimeshift = timeshiftParamsFromConfig(section('timeshift'))
  const backend = (deploy.backend as ChannelCreatePayload['backend']) ?? 'ecs-express'
  // `port` lives in [infrastructure.express] for ecs-express, [infrastructure.docker] for local-docker
  // (see its_a_live.generate_toml()) -- one form field either way.
  const portSection = backend === 'local-docker' ? docker : express
  Object.assign(editForm, {
    name: props.name,
    backend,
    region: String(aws.region ?? ''),
    bucket_name: String(s3.bucket_name ?? ''),
    content_folder: String(s3.content_folder ?? ''),
    source_path: String(input.source_path ?? ''),
    source_kind: deriveSourceKind(input),
    allow_missing_segments: Boolean(input.allow_missing_segments ?? false),
    segment_duration: Number(packaging.segment_duration ?? 4.0),
    dvr_window_seconds: Number(packaging.dvr_window_seconds ?? 30),
    epoch_utc: String(timeline.epoch_utc ?? DEFAULT_EPOCH_UTC),
    hls_format: (packaging.hls_format as ChannelCreatePayload['hls_format']) ?? 'cmaf',
    hls_ts_mux_audio: Boolean(packaging.hls_ts_mux_audio ?? true),
    continuous: Boolean(timeline.continuous ?? true),
    period_on_segmentation: periodTypesFromConfig(markers.period_on_segmentation),
    period_on_segmentation_apply:
      (markers.period_on_segmentation_apply as ChannelCreatePayload['period_on_segmentation_apply']) ?? 'both',
    timeshift_enabled: editTimeshift.enabled,
    timeshift_start_param: editTimeshift.start_param,
    timeshift_end_param: editTimeshift.end_param,
    timeshift_max_span_seconds: editTimeshift.max_span_seconds,
    port: portSection.port === 'auto' ? 'auto' : Number(portSection.port ?? 8080),
    cpu: Number(express.cpu ?? 256),
    memory: Number(express.memory ?? 512),
    cdn: Boolean(express.cdn ?? true),
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

// Read-only Configuration panel: same sections/labels as the edit form
// and the New channel form (see utils/channelConfigLayout.ts).
const configSections = computed(() => (config.value ? buildConfigSections(config.value) : []))
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
// /status throws while there is no stack (never deployed / dismantled), so
// fall back to the backend from the channel's own TOML config -- otherwise
// byBackend() below would show the aws-media (MediaLive) wording for it.
const backend = computed(
  () => status.value?.backend ?? (section('deploy').backend as ChannelStatus['backend'] | undefined),
)

// Fires once backend first becomes known (right after loadStatus
// resolves) and again if it ever changes -- not on every status poll,
// since `backend` itself doesn't change between polls. Keyed on the live
// status (not the config fallback): schedules need a deployed channel.
const liveBackend = computed(() => status.value?.backend)
watch(
  liveBackend,
  (b) => {
    if (b && b !== 'local-docker') loadSchedule()
    else scheduleWindows.value = []
  },
  { immediate: true },
)

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
    '~4-11 min -- Docker image build + push (only when the image is new; a cold build takes a few minutes), ECS task startup, and, if CloudFront is on, a brand-new distribution that adds about 3 min. This is normal, not a hang.',
    '~5-10 min -- MediaLive channel provisioning and startup. This is normal, not a hang.',
  ),
  fn: () => createChannel(props.name),
  disabled: () => (backend.value === 'local-docker' ? isUpButMaybeUnreachable(phase.value) : phase.value !== 'not-deployed'),
  disabledReason: () =>
    backend.value === 'local-docker'
      ? 'Already running -- use the actions on the right to manage it.'
      : 'Already deployed -- use the actions on the right to manage it. After editing the channel config, run Redeploy to apply it.',
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
        'Applies your edited channel config to the already-deployed stack (runs cdk deploy; the service restarts briefly). Run this after changing any setting in the Configuration panel -- a running channel keeps using the old settings until you do. For new content, use Update content instead. If the stack is in a failed state it is deleted and recreated first.',
        'Applies your edited channel config to the already-deployed stack (runs cdk deploy). Run this after changing any setting in the Configuration panel -- a running channel keeps using the old settings until you do. For new content, use Update content instead. If the stack is in a failed state it is deleted and recreated first.',
      ),
      eta: byBackend(
        '~10-30s.',
        '~5-10 min (ECS Express rolls the new task out with a canary, and the old task keeps serving for ~3+ min); longer only if a failed stack has to be deleted and recreated -- not a hang.',
        '~1-2 min for a config change; ~5-10 min only if a failed stack has to be deleted and recreated.',
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
// Startover/catchup (loop-dee-loop/SCOPE.md §13): a generated time-shifted
// URL can be previewed in the players in place of the live one. Only
// ecs-express/local-docker run serve.py, so only they have this.
const timeshiftParams = computed(() => timeshiftParamsFromConfig(section('timeshift')))
const timeshiftAvailable = computed(
  () => usesChannelSection(String(section('deploy').backend ?? '')) && timeshiftParams.value.enabled,
)
const timeshiftPreview = ref<TimeshiftPreview | null>(null)
// Leaving a preview behind when the channel stops/changes would leave the
// players pointed at a dead URL.
watch([playbackHlsUrl, playbackDashUrl], () => (timeshiftPreview.value = null))
const effectiveHlsUrl = computed(() => timeshiftPreview.value?.hlsUrl ?? playbackHlsUrl.value)
const effectiveDashUrl = computed(() => timeshiftPreview.value?.dashUrl ?? playbackDashUrl.value)

const showPlayback = computed(
  () => (playbackHlsUrl.value || playbackDashUrl.value) && phase.value !== 'stopped' && phase.value !== 'not-deployed',
)

// loop-dee-loop's /timeline.json describes the live window, or -- while a
// startover/catchup preview is active -- the previewed range (same query).
const windowAvailable = computed(
  () => !!showPlayback.value && usesChannelSection(String(section('deploy').backend ?? '')),
)
const windowQuery = computed(() => {
  const url = timeshiftPreview.value?.hlsUrl ?? timeshiftPreview.value?.dashUrl
  if (!url) return ''
  try {
    return new URL(url, window.location.href).search.replace(/^\?/, '')
  } catch {
    return ''
  }
})

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

// The list row (stack_status/live_status/min_tasks/reachable) lets
// listPhase reuse listItemPhase() unchanged. Silently keeps the previous
// value on failure (e.g. a transient error) rather than blinking the State
// tag back to "unknown".
async function loadListPhase() {
  try {
    listItem.value = await getChannelSummary(props.name)
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

// aws-media/ecs-express only -- local-docker has no AWS presence to
// schedule against (channel.py itself refuses `schedule *`, see
// its_a_live.AGENTS.md), so this is never called for it (see the
// `backend` watcher below).
async function loadSchedule() {
  scheduleLoading.value = true
  scheduleError.value = ''
  try {
    scheduleWindows.value = await listScheduleWindows(props.name)
  } catch (e) {
    scheduleError.value = e instanceof Error ? e.message : String(e)
  } finally {
    scheduleLoading.value = false
  }
}

// DatePicker gives back a plain local Date; the API takes ISO8601 UTC
// (channel.py's _scheduler_ops.py `%Y-%m-%dT%H:%M:%SZ`) -- truncate to
// whole seconds, no milliseconds.
function toIsoUtc(d: Date | null): string | null {
  return d ? d.toISOString().replace(/\.\d{3}Z$/, 'Z') : null
}

function windowEdgeLabel(w: ScheduleWindow): string {
  const start = w.start ? new Date(w.start).toLocaleString() : `now (${new Date(w.created_at).toLocaleString()})`
  const end = w.end ? new Date(w.end).toLocaleString() : 'manual stop'
  return `${start} → ${end}`
}

// Dispatched as a background Job, not a plain await -- an immediate
// window (no start given) also runs a full `channel.py start` server-side
// (see schedule_add_job's docstring), which can take as long as the
// existing Start button already does.
async function submitAddWindow() {
  addWindowSaving.value = true
  addWindowError.value = ''
  try {
    const job = await addScheduleWindow(props.name, toIsoUtc(newWindowStart.value), toIsoUtc(newWindowEnd.value))
    newWindowStart.value = null
    newWindowEnd.value = null
    activeAction.value = 'schedule-add'
    activeActionEta.value = 'A few seconds -- longer if no start time was given (also starts the channel now).'
    phaseBeforeAction.value = phase.value
    activeJobId.value = job.id
    await loadSchedule()
  } catch (e) {
    addWindowError.value = e instanceof Error ? e.message : String(e)
  } finally {
    addWindowSaving.value = false
  }
}

async function removeWindow(windowId: string) {
  try {
    await removeScheduleWindow(props.name, windowId)
    await loadSchedule()
  } catch (e) {
    toast.add({
      severity: 'error',
      summary: 'Could not remove window',
      detail: e instanceof Error ? e.message : String(e),
      life: 6000,
    })
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
  scheduleWindows.value = []
  activeJobId.value = null
  reattachRunningJob()
  loadStatus()
  loadListPhase()
  loadHealth()
  loadConfig()
  // loadSchedule() itself is driven by the `backend` watcher above, once
  // loadStatus() resolves and backend becomes known again.
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
      <BackendBadge v-if="status" class="ml-auto" :backend="status.backend" pill />
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
      :hls-url="effectiveHlsUrl"
      :dash-url="effectiveDashUrl"
      :start-from-beginning="timeshiftPreview?.startEpochSeconds != null"
      :dash-clock-offset-seconds="timeshiftPreview?.startEpochSeconds ?? 0"
      :health="health"
      :health-error="status?.backend === 'ecs-express' || status?.backend === 'local-docker' ? healthError : ''"
    />

    <TimeshiftPanel
      v-if="showPlayback && timeshiftAvailable"
      :hls-url="playbackHlsUrl"
      :dash-url="playbackDashUrl"
      :params="timeshiftParams"
      :health="health"
      :previewing="timeshiftPreview !== null"
      @preview="timeshiftPreview = $event"
    />

    <WindowPanel v-if="windowAvailable" :name="name" :query="windowQuery" />

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

        <div v-if="liveBackend && liveBackend !== 'local-docker'" class="flex flex-column gap-2">
          <h3 class="m-0 text-sm text-color-secondary uppercase">Schedule</h3>
          <div class="flex flex-column gap-2 p-3 border-round surface-card" style="border: 1px solid var(--surface-border)">
            <Message v-if="scheduleError" severity="warn" :closable="false">{{ scheduleError }}</Message>

            <div v-if="scheduleWindows.length" class="flex flex-column gap-2">
              <div
                v-for="w in scheduleWindows"
                :key="w.id"
                class="flex align-items-center justify-content-between gap-2 p-2 surface-100 border-round text-sm"
              >
                <div class="flex align-items-center gap-2">
                  <Tag
                    :value="w.status"
                    :severity="w.status === 'active' ? 'success' : w.status === 'upcoming' ? 'info' : 'secondary'"
                  />
                  <span>{{ windowEdgeLabel(w) }}</span>
                </div>
                <Button icon="pi pi-times" severity="danger" text size="small" @click="removeWindow(w.id)" />
              </div>
            </div>
            <div v-else-if="!scheduleLoading" class="text-color-secondary text-sm">No scheduled windows.</div>

            <div class="flex flex-column gap-2 mt-1 pt-2" style="border-top: 1px solid var(--surface-border)">
              <Message v-if="addWindowError" severity="error" :closable="false">{{ addWindowError }}</Message>
              <div class="flex flex-wrap align-items-end gap-2">
                <div class="flex flex-column gap-1">
                  <label class="text-xs text-color-secondary">Start (empty = now)</label>
                  <DatePicker v-model="newWindowStart" show-time hour-format="24" show-icon show-button-bar />
                </div>
                <div class="flex flex-column gap-1">
                  <label class="text-xs text-color-secondary">End (empty = manual stop)</label>
                  <DatePicker v-model="newWindowEnd" show-time hour-format="24" show-icon show-button-bar />
                </div>
                <Button label="Add window" icon="pi pi-plus" size="small" :loading="addWindowSaving" @click="submitAddWindow" />
              </div>
              <div class="text-color-secondary text-xs">
                Windows may not overlap. Firing is AWS-native (EventBridge Scheduler), independent of igor being up --
                see its-a-live/AGENTS.md. Removing a window only cancels its remaining future triggers; it does not stop
                the channel if the window is currently active.
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="flex flex-column gap-2 p-3 border-round surface-card channel-config" style="border: 1px solid var(--surface-border)">
        <div class="flex align-items-center justify-content-between">
          <h3 class="m-0">Configuration</h3>
          <div class="flex align-items-center gap-1">
            <Button
              v-if="config && !editing && section('deploy').backend === 'local-docker'"
              label="Duplicate to ecs-express"
              icon="pi pi-clone"
              size="small"
              text
              @click="router.push({ path: '/channels/new', query: { from: name } })"
            />
            <Button
              v-if="config && !editing"
              label="Edit"
              icon="pi pi-pencil"
              size="small"
              text
              @click="startEdit"
            />
          </div>
        </div>
        <div v-if="!config" class="text-color-secondary text-sm">Loading...</div>

        <div v-else-if="!editing" class="flex flex-column gap-2">
          <div
            v-for="sec in configSections"
            :key="sec.id"
            class="surface-100 border-round p-2"
            style="border-left: 3px solid var(--p-primary-color, #b91c1c)"
          >
            <div class="text-color-secondary font-semibold mb-1" style="font-size: 0.7rem; letter-spacing: 0.06em; text-transform: uppercase">
              {{ sec.title }}
            </div>
            <table class="text-sm config-table">
              <tbody>
                <tr v-for="row in sec.rows" :key="row.key">
                  <td class="pr-3 text-color-secondary vertical-align-top config-key" :title="row.label">{{ row.key }}</td>
                  <td class="font-mono config-value">{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-else class="flex flex-column gap-3">
          <Message v-if="editError" severity="error" :closable="false">{{ editError }}</Message>

          <section class="config-group">
            <h4 class="config-group-title">{{ T.input }}</h4>
            <div class="config-fields">
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">{{ L.source_kind }}</label>
                <InputText :model-value="editForm.source_kind" disabled />
              </div>
              <div class="flex flex-column gap-1 config-field-wide">
                <label class="text-xs text-color-secondary">{{ L.source_path }}</label>
                <InputText v-model="editForm.source_path" />
              </div>
              <div class="flex align-items-center gap-2 config-field-wide">
                <Checkbox v-model="editForm.allow_missing_segments" binary input-id="edit-allow-missing-segments" />
                <label for="edit-allow-missing-segments" class="text-xs text-color-secondary">{{ L.allow_missing_segments }}</label>
                <FieldHelp label="Allow missing segments">
                  Only meaningful for a grave-robber segment-list manifest (archive or manifest import). A
                  request for a missing segment's bytes 404s, but the served manifest is otherwise
                  indistinguishable from a fully-populated one.
                </FieldHelp>
              </div>
            </div>
          </section>

          <section v-if="!editIsLocalDocker" class="config-group">
            <h4 class="config-group-title">{{ T['infrastructure.aws'] }}</h4>
            <div class="config-fields">
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">{{ L.region }}</label>
                <InputText v-model="editForm.region" />
              </div>
            </div>
          </section>

          <section v-if="!editIsLocalDocker" class="config-group">
            <h4 class="config-group-title">{{ T['infrastructure.s3'] }}</h4>
            <div class="config-fields">
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">{{ L.bucket_name }}</label>
                <InputText v-model="editForm.bucket_name" />
              </div>
              <div class="flex flex-column gap-1 config-field-wide">
                <label class="text-xs text-color-secondary">{{ L.content_folder }}</label>
                <InputText v-model="editForm.content_folder" />
              </div>
            </div>
          </section>

          <template v-if="editUsesChannelSection">
            <section class="config-group">
              <h4 class="config-group-title">{{ editIsLocalDocker ? T['infrastructure.docker'] : T['infrastructure.express'] }}</h4>
              <div class="config-fields">
                <div class="flex flex-column gap-1" :class="{ 'config-field-wide': editIsLocalDocker }">
                  <label class="text-xs text-color-secondary">{{ L.port }}</label>
                  <div v-if="editIsLocalDocker" class="flex align-items-center gap-2">
                    <Checkbox v-model="editAutoPort" binary input-id="edit-auto-port" />
                    <label for="edit-auto-port" class="text-sm">Auto-select a free port</label>
                    <FieldHelp label="Auto-select a free port">
                      A free port (8080-8179) is picked on next start/refresh and reused afterward.
                    </FieldHelp>
                  </div>
                  <InputNumber v-if="!editIsLocalDocker || !editAutoPort" v-model="editForm.port as number" :use-grouping="false" fluid />
                </div>
                <template v-if="editIsEcsExpress">
                  <div class="flex flex-column gap-1">
                    <label class="text-xs text-color-secondary">{{ L.cpu }}</label>
                    <InputNumber v-model="editForm.cpu" :use-grouping="false" fluid />
                  </div>
                  <div class="flex flex-column gap-1">
                    <label class="text-xs text-color-secondary">{{ L.memory }}</label>
                    <InputNumber v-model="editForm.memory" :use-grouping="false" fluid />
                  </div>
                  <div class="flex align-items-center gap-2 config-field-wide">
                    <Checkbox v-model="editForm.cdn" binary input-id="edit-cdn" />
                    <label for="edit-cdn" class="text-xs text-color-secondary">{{ L.cdn }}</label>
                    <FieldHelp label="CloudFront CDN">
                      Changing this on a deployed channel redeploys the stack and changes the playback URLs
                      (CloudFront domain vs the service endpoint). Having CloudFront adds about 2.5-3
                      minutes to a deploy and about 2.5 minutes to a teardown.
                    </FieldHelp>
                  </div>
                </template>
              </div>
            </section>

            <section class="config-group">
              <h4 class="config-group-title">{{ T.timeline }}</h4>
              <div class="config-fields">
                <div class="config-field-wide">
                  <EpochFields :form="editForm" id-prefix="edit" />
                </div>
                <div class="flex align-items-center gap-2 config-field-wide">
                  <Checkbox v-model="editForm.continuous" binary input-id="edit-continuous-timeline" />
                  <label for="edit-continuous-timeline" class="text-xs text-color-secondary">
                    {{ L.continuous }}
                  </label>
                  <FieldHelp label="Continuous timeline">
                    Rewrites each segment's own timestamps per request (header patch, never a re-transcode) so
                    the channel has no discontinuity/Period restart at the loop wrap. Off by default: the loop wrap
                    is then honestly signaled with #EXT-X-DISCONTINUITY / a DASH Period restart. Turn on for a
                    seamless wrap -- serve.py refuses to start in this mode against a source baked with a 32-bit
                    tfdt (loop-dee-loop/SCOPE.md &sect;12).
                  </FieldHelp>
                </div>
              </div>
            </section>

            <section class="config-group">
              <h4 class="config-group-title">{{ T.packaging }}</h4>
              <div class="config-fields">
                <div class="flex flex-column gap-1">
                  <label class="text-xs text-color-secondary">{{ L.segment_duration }}</label>
                  <div v-if="editIsArchiveSource" class="text-color-secondary p-2 surface-ground border-round">As source</div>
                  <InputNumber v-else v-model="editForm.segment_duration" :min-fraction-digits="1" fluid />
                </div>
                <div class="flex flex-column gap-1">
                  <label class="text-xs text-color-secondary">{{ L.dvr_window_seconds }}</label>
                  <InputNumber v-model="editForm.dvr_window_seconds" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">{{ L.hls_format }}</label>
                  <Select v-model="editForm.hls_format" :options="hlsFormatOptions" option-label="label" option-value="value" fluid />
                </div>
                <div v-if="editForm.hls_format === 'ts'" class="flex align-items-center gap-2 config-field-wide">
                  <Checkbox v-model="editForm.hls_ts_mux_audio" binary input-id="edit-hls-ts-mux-audio" />
                  <label for="edit-hls-ts-mux-audio" class="text-xs text-color-secondary">{{ L.hls_ts_mux_audio }}</label>
                </div>
              </div>
            </section>

            <section class="config-group">
              <h4 class="config-group-title">{{ T.timeshift }}</h4>
              <TimeshiftFields :form="editForm" id-prefix="edit" />
            </section>

            <section class="config-group">
              <h4 class="config-group-title">{{ T.markers }}</h4>
              <div class="config-fields">
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">{{ L.daterange_mode }}</label>
                  <Select v-model="editForm.daterange_mode" :options="daterangeModeOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">{{ L.cue_tags }}</label>
                  <Select v-model="editForm.cue_tags" :options="cueTagsOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">{{ L.dash_signal_format }}</label>
                  <Select v-model="editForm.dash_signal_format" :options="dashSignalFormatOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <label class="text-xs text-color-secondary">{{ L.dash_descriptor_mode }}</label>
                  <Select v-model="editForm.dash_descriptor_mode" :options="dashDescriptorModeOptions" option-label="label" option-value="value" fluid />
                </div>
                <div class="flex align-items-center gap-2 config-field-wide">
                  <Checkbox v-model="editForm.increment_event_ids" binary input-id="edit-increment-event-ids" />
                  <label for="edit-increment-event-ids" class="text-xs text-color-secondary">{{ L.increment_event_ids }}</label>
                  <FieldHelp label="Increment SCTE-35 event ids">
                    Off (default) repeats the same event id every loop -- easiest to test against. On bumps
                    each id by loop_number &times; a shared step (a power of 10 above the channel's largest
                    base id, e.g. base ids 100-190 &rarr; step 1000, so loop 1 emits 1100/1190, loop 2 emits
                    2100/2190, ...) -- predictable from wall-clock time alone, and wraps back to the base id
                    at the 32-bit SCTE-35 ceiling.
                  </FieldHelp>
                </div>
                <div class="flex flex-column gap-1 config-field-wide">
                  <div class="flex align-items-center gap-1">
                    <label class="text-xs text-color-secondary" for="edit-daterange-id-format">{{ L.daterange_id_format }}</label>
                    <DaterangeIdFormatHelp />
                  </div>
                  <InputText id="edit-daterange-id-format" v-model="editForm.daterange_id_format" />
                </div>
                <div class="config-field-wide">
                  <PeriodSegmentationFields :form="editForm" id-prefix="edit" />
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
