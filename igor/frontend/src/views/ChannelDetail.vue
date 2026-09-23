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
import {
  createChannel,
  getChannel,
  getChannelHealth,
  getChannelOutputs,
  getChannelStatus,
  redeployChannel,
  sparkChannel,
  startChannel,
  stopChannel,
  terminateChannel,
  updateChannel,
  updateChannelContent,
} from '../api/client'
import type { ChannelCreatePayload, ChannelHealth, ChannelOutputs, ChannelStatus, Job } from '../api/types'
import { type Phase, PHASE_LABEL, liveStatusPhase, phaseSeverity } from '../utils/channelPhase'

const props = defineProps<{ name: string }>()

const status = ref<ChannelStatus | null>(null)
const outputs = ref<ChannelOutputs | null>(null)
const health = ref<ChannelHealth | null>(null)
const config = ref<Record<string, unknown> | null>(null)
const statusError = ref('')
const healthError = ref('')
const loading = ref(false)
const activeJobId = ref<string | null>(null)
const activeAction = ref('')
const activeActionEta = ref('')
// Phase captured right as an action starts, so onJobFinished can tell a
// genuine not-running -> running transition (worth celebrating) apart from
// e.g. Update content on an already-running channel.
const phaseBeforeAction = ref<Phase | null>(null)
const itsAliveBanner = ref<InstanceType<typeof ItsAliveBanner> | null>(null)

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
  segment_duration: 4.0,
  dvr_window_seconds: 30,
  port: 8080,
  cpu: 256,
  memory: 512,
  daterange_mode: 'shared',
  cue_tags: 'none',
  increment_event_ids: false,
})
const editIsEcsExpress = computed(() => editForm.backend === 'ecs-express')
const editIsLocalDocker = computed(() => editForm.backend === 'local-docker')
const editUsesChannelSection = computed(() => editIsEcsExpress.value || editIsLocalDocker.value)

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
    segment_duration: Number(packaging.segment_duration ?? 4.0),
    dvr_window_seconds: Number(packaging.dvr_window_seconds ?? 30),
    port: Number(portSection.port ?? 8080),
    cpu: Number(express.cpu ?? 256),
    memory: Number(express.memory ?? 512),
    daterange_mode: (markers.daterange_mode as ChannelCreatePayload['daterange_mode']) ?? 'shared',
    cue_tags: (markers.cue_tags as ChannelCreatePayload['cue_tags']) ?? 'none',
    increment_event_ids: Boolean(markers.increment_event_ids ?? false),
  })
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
    toast.add({ severity: 'success', summary: 'Config saved', detail: 'Redeploy/Refresh to apply it to a deployed channel.', life: 5000 })
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e)
  } finally {
    editSaving.value = false
  }
}

// Flattened "section.key: value" rows for the read-only config panel, in
// the same section order as the TOML file (deploy, aws, s3, input, bake,
// markers, packaging, express/docker -- see its-a-live/AGENTS.md's
// config.toml reference).
const configRows = computed(() => {
  if (!config.value) return []
  const rows: { section: string; key: string; value: string }[] = []
  for (const [section, fields] of Object.entries(config.value)) {
    if (!fields || typeof fields !== 'object') continue
    for (const [key, value] of Object.entries(fields as Record<string, unknown>)) {
      rows.push({ section, key, value: String(value) })
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
  disabled: () => (backend.value === 'local-docker' ? phase.value === 'running' : phase.value !== 'not-deployed'),
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
  if (phase.value === 'running') {
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
    disabled: () => (backend.value === 'local-docker' ? phase.value === 'running' : phase.value !== 'stopped'),
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
    disabled: () => phase.value !== 'running',
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
        if (phase.value === 'running') return 'Stop the channel before terminating its stack.'
        if (phase.value === 'transitioning') return 'Status is transitioning -- wait for it to settle.'
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
  confirm.require({
    target: event.currentTarget as HTMLElement,
    message: a.confirmMessage,
    acceptClass: 'p-button-danger',
    accept: () => run(a.key, a.fn, a.eta),
  })
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
  const wasRunning = phaseBeforeAction.value === 'running'
  await loadStatus()
  if (job.status === 'succeeded' && !wasRunning && phase.value === 'running') {
    itsAliveBanner.value?.trigger()
  }
}

function reload() {
  status.value = null
  outputs.value = null
  health.value = null
  config.value = null
  activeJobId.value = null
  loadStatus()
  loadHealth()
  loadConfig()
}

onMounted(() => {
  reload()
  healthTimer = setInterval(loadHealth, 5000)
})
onBeforeUnmount(() => {
  if (healthTimer) clearInterval(healthTimer)
})
watch(() => props.name, reload)
</script>

<template>
  <div class="flex flex-column gap-4">
    <div class="flex align-items-center gap-2">
      <h2 class="m-0">{{ name }}</h2>
      <Tag v-if="status" :value="status.backend" />
      <Tag v-if="status" :value="status.status" :severity="phaseSeverity(phase)" />
      <Tag v-else :value="PHASE_LABEL[phase]" :severity="phaseSeverity(phase)" />
    </div>

    <Message v-if="statusError && !statusErrorIsMissingStack" severity="warn">
      <div>Could not fetch live status.</div>
      <details class="mt-2">
        <summary class="cursor-pointer text-sm">Show details</summary>
        <pre class="job-log mt-2">{{ statusError }}</pre>
      </details>
    </Message>

    <PlaybackPanel
      v-if="showPlayback"
      :hls-url="playbackHlsUrl"
      :dash-url="playbackDashUrl"
      :health="health"
      :health-error="status?.backend === 'ecs-express' || status?.backend === 'local-docker' ? healthError : ''"
    />

    <div class="flex gap-4 flex-wrap align-items-start">
      <div class="flex flex-column gap-4" style="flex: 2 1 40rem; min-width: 28rem">
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

      <div class="flex flex-column gap-2 p-3 border-round surface-card" style="flex: 1 1 18rem; min-width: 18rem; border: 1px solid var(--surface-border)">
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
            <table class="text-sm" style="table-layout: fixed; width: 100%">
              <tbody>
                <tr v-for="row in sec.rows" :key="row.key">
                  <td class="pr-3 text-color-secondary white-space-nowrap vertical-align-top" style="width: 9.5rem">{{ row.key }}</td>
                  <td class="font-mono" style="word-break: break-all">{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-else class="flex flex-column gap-2">
          <Message v-if="editError" severity="error" :closable="false">{{ editError }}</Message>

          <div class="flex flex-column gap-1">
            <label class="text-xs text-color-secondary">Backend (immutable)</label>
            <InputText :model-value="editForm.backend" disabled />
          </div>

          <template v-if="!editIsLocalDocker">
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">AWS region</label>
              <InputText v-model="editForm.region" />
            </div>
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">S3 bucket name</label>
              <InputText v-model="editForm.bucket_name" />
            </div>
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">S3 content folder</label>
              <InputText v-model="editForm.content_folder" />
            </div>
          </template>

          <div class="flex flex-column gap-1">
            <label class="text-xs text-color-secondary">Source path</label>
            <InputText v-model="editForm.source_path" />
          </div>

          <template v-if="editUsesChannelSection">
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">Segment duration (s)</label>
              <InputNumber v-model="editForm.segment_duration" :min-fraction-digits="1" />
            </div>
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">DVR window (s)</label>
              <InputNumber v-model="editForm.dvr_window_seconds" />
            </div>
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">Serve port</label>
              <InputNumber v-model="editForm.port" :use-grouping="false" />
            </div>
            <template v-if="editIsEcsExpress">
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">Express CPU units</label>
                <InputNumber v-model="editForm.cpu" :use-grouping="false" />
              </div>
              <div class="flex flex-column gap-1">
                <label class="text-xs text-color-secondary">Express memory (MB)</label>
                <InputNumber v-model="editForm.memory" :use-grouping="false" />
              </div>
            </template>

            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">HLS DATERANGE mode</label>
              <Select v-model="editForm.daterange_mode" :options="daterangeModeOptions" option-label="label" option-value="value" />
            </div>
            <div class="flex flex-column gap-1">
              <label class="text-xs text-color-secondary">HLS CUE-OUT/CUE-IN tags</label>
              <Select v-model="editForm.cue_tags" :options="cueTagsOptions" option-label="label" option-value="value" />
            </div>
            <div class="flex align-items-center gap-2">
              <Checkbox v-model="editForm.increment_event_ids" binary input-id="edit-increment-event-ids" />
              <label for="edit-increment-event-ids" class="text-xs text-color-secondary">Increment SCTE-35 event ids each loop (HLS + DASH)</label>
            </div>
          </template>

          <div class="flex gap-2 mt-1">
            <Button label="Save" icon="pi pi-check" size="small" :loading="editSaving" @click="saveEdit" />
            <Button label="Cancel" size="small" text :disabled="editSaving" @click="cancelEdit" />
          </div>
        </div>

        <div class="text-color-secondary text-xs mt-2">
          Editing only rewrites configs/{{ name }}.toml -- it does not
          {{ byBackend('touch the running container', 'touch AWS', 'touch AWS') }} by itself.
          {{ byBackend('Refresh', 'Redeploy', 'Redeploy') }} afterwards to apply the change.
        </div>
      </div>
    </div>

    <JobPanel :job-id="activeJobId" :eta-hint="activeActionEta" @finished="onJobFinished" />
    <ConfirmPopup />
    <ItsAliveBanner ref="itsAliveBanner" />
  </div>
</template>
