<script setup lang="ts">
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import JobPanel from '../components/JobPanel.vue'
import PlaybackPanel from '../components/PlaybackPanel.vue'
import {
  createChannel,
  getChannel,
  getChannelHealth,
  getChannelOutputs,
  getChannelStatus,
  redeployChannel,
  refreshChannel,
  sparkChannel,
  startChannel,
  stopChannel,
} from '../api/client'
import type { ChannelHealth, ChannelOutputs, ChannelStatus } from '../api/types'

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

const toast = useToast()
let healthTimer: ReturnType<typeof setInterval> | null = null

// Flattened "section.key: value" rows for the read-only config panel, in
// the same section order as the TOML file (deploy, aws, s3, input,
// channel, express -- see its-a-live/AGENTS.md's config.toml reference).
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

// A missing CloudFormation stack is an expected, common state (not yet
// deployed) rather than a real error -- `channel.py status`'s traceback
// always contains this phrase in that case. Anything else is a genuine
// failure (bad AWS creds, wrong region, etc.) and gets shown in full.
const statusErrorIsMissingStack = computed(() => /does not exist/.test(statusError.value))

// Lifecycle phase inferred from the last successful /status call, used to
// gate which actions make sense right now. `getChannelStatus` throws (and
// `statusError` gets set) whenever the stack doesn't exist yet, so that's
// our "not deployed" signal -- see igor/AGENTS.md's API surface table.
type Phase = 'not-deployed' | 'stopped' | 'running' | 'unknown'

const phase = computed<Phase>(() => {
  if (!status.value) return statusErrorIsMissingStack.value ? 'not-deployed' : 'unknown'
  const s = status.value
  if (s.backend === 'aws-media') {
    if (s.status === 'IDLE' || s.status === 'DELETED') return 'stopped'
    if (s.status === 'RUNNING' || s.status === 'STARTING') return 'running'
    return 'unknown'
  }
  // ecs-express: scaled to 0 tasks == stopped, otherwise running.
  if (s.min_tasks === 0) return 'stopped'
  if ((s.min_tasks ?? 0) > 0) return 'running'
  return 'unknown'
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

const firstDeployAction = computed<ActionDef>(() => ({
  key: 'create',
  label: 'Create / deploy',
  icon: 'pi pi-cloud-upload',
  description:
    'One-time, first deploy of this channel: stages content to S3, deploys the AWS stack, then starts it. This is the only action available before the channel exists in AWS.',
  eta: '~5-10 min -- Docker image build + push, ECS task startup, and (ecs-express) a brand-new CloudFront distribution, which alone typically takes several minutes to propagate. This is normal AWS behavior, not a hang.',
  fn: () => createChannel(props.name),
  disabled: () => phase.value !== 'not-deployed',
  disabledReason: () => 'Already deployed -- use the actions on the right to manage it, or Redeploy for stack/config changes.',
}))

const lifecycleActions = computed<ActionDef[]>(() => [
  {
    key: 'spark',
    label: 'Spark',
    icon: 'pi pi-bolt',
    severity: 'secondary',
    description:
      'Stages the franken-ts output to S3 (bakes it locally first for ecs-express). Run this after building new content, before Start or Refresh content pick it up.',
    eta: '~10-60s, depending on content size.',
    fn: () => sparkChannel(props.name),
    disabled: () => phase.value === 'not-deployed',
    disabledReason: () => 'Requires the channel to be deployed first -- there is no stack to stage content for yet.',
  },
  {
    key: 'start',
    label: 'Start',
    icon: 'pi pi-play',
    severity: 'success',
    description:
      'Goes live: scales ecs-express to 1 task, or starts the MediaLive channel. Requires content to already be Sparked at least once.',
    eta: '~30-60s for ecs-express (task startup + health check); a couple of minutes for aws-media (MediaLive channel start).',
    fn: () => startChannel(props.name),
    disabled: () => phase.value !== 'stopped',
    disabledReason: () =>
      phase.value === 'not-deployed' ? 'Requires the channel to be deployed first.' : 'Channel is already running.',
  },
  {
    key: 'stop',
    label: 'Stop',
    icon: 'pi pi-stop',
    severity: 'danger',
    description:
      'Stops paying for compute: scales ecs-express to 0 tasks (ALB stays up for other channels), or stops the MediaLive channel.',
    eta: '~10-30s for ecs-express; up to a couple of minutes for aws-media.',
    fn: () => stopChannel(props.name),
    disabled: () => phase.value !== 'running',
    disabledReason: () =>
      phase.value === 'not-deployed' ? 'Requires the channel to be deployed first.' : 'Channel is already stopped.',
  },
  {
    key: 'refresh',
    label: 'Refresh content',
    icon: 'pi pi-refresh',
    severity: 'secondary',
    description:
      'Picks up newly-Sparked content on an already-running channel: a fast re-sync for ecs-express, a full stop/start cycle (real interruption) for aws-media.',
    eta: '~30-60s for ecs-express; a full stop/start cycle (a few minutes) for aws-media.',
    fn: () => refreshChannel(props.name),
    disabled: () => phase.value !== 'running',
    disabledReason: () =>
      phase.value === 'not-deployed' ? 'Requires the channel to be deployed first.' : 'Start the channel before refreshing its content.',
  },
  {
    key: 'redeploy',
    label: 'Redeploy',
    icon: 'pi pi-wrench',
    severity: 'warn',
    outlined: true,
    description:
      'Deletes a broken/rolled-back stack if needed, then cdk deploys the current config again. Use this after editing the channel config, or to recover from a failed Create.',
    eta: '~1-2 min for a small config change; ~5-10 min if the stack has to be deleted and recreated (e.g. after a failed Create), for the same CloudFront/ECS reasons as above.',
    fn: () => redeployChannel(props.name),
    disabled: () => phase.value === 'unknown' && !statusError.value,
    disabledReason: () => 'Status is still loading.',
  },
])

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
  if (status.value?.backend !== 'ecs-express') return
  try {
    health.value = await getChannelHealth(props.name)
    healthError.value = ''
  } catch (e) {
    healthError.value = e instanceof Error ? e.message : String(e)
  }
}

async function run(action: string, fn: () => Promise<{ id: string }>, eta = '') {
  loading.value = true
  activeAction.value = action
  activeActionEta.value = eta
  try {
    const job = await fn()
    activeJobId.value = job.id
  } catch (e) {
    toast.add({ severity: 'error', summary: `${action} failed`, detail: e instanceof Error ? e.message : String(e), life: 6000 })
  } finally {
    loading.value = false
  }
}

function onJobFinished() {
  toast.add({
    severity: 'info',
    summary: `${activeAction.value} finished`,
    detail: `See the job log above for details.`,
    life: 4000,
  })
  loadStatus()
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
      <Tag v-if="status" :value="status.status" severity="info" />
      <Tag v-else :value="phase === 'not-deployed' ? 'not deployed' : 'unknown'" severity="warn" />
    </div>

    <Message v-if="statusError && !statusErrorIsMissingStack" severity="warn">
      <div>Could not fetch live status.</div>
      <details class="mt-2">
        <summary class="cursor-pointer text-sm">Show details</summary>
        <pre class="job-log mt-2">{{ statusError }}</pre>
      </details>
    </Message>

    <PlaybackPanel
      v-if="outputs && (outputs.HlsPlaybackUrl || outputs.DashPlaybackUrl)"
      :hls-url="outputs.HlsPlaybackUrl"
      :dash-url="outputs.DashPlaybackUrl"
      :health="health"
      :health-error="status?.backend === 'ecs-express' ? healthError : ''"
    />

    <div class="flex gap-4 flex-wrap align-items-start">
      <div class="flex flex-column gap-4" style="flex: 2 1 40rem; min-width: 28rem">
        <div class="flex flex-column gap-2">
          <h3 class="m-0 text-sm text-color-secondary uppercase">Step 1 -- first deploy</h3>
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
          <h3 class="m-0 text-sm text-color-secondary uppercase">Manage the deployed channel</h3>
          <Message v-if="phase === 'not-deployed'" severity="secondary" :closable="false">
            These require the channel to be deployed -- run Step 1 above first.
          </Message>
          <div
            v-for="a in lifecycleActions"
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
      </div>

      <div class="flex flex-column gap-2 p-3 border-round surface-card" style="flex: 1 1 18rem; min-width: 18rem; border: 1px solid var(--surface-border)">
        <h3 class="m-0">Configuration</h3>
        <div v-if="!config" class="text-color-secondary text-sm">Loading...</div>
        <table v-else class="text-sm">
          <tbody>
            <template v-for="(row, i) in configRows" :key="`${row.section}.${row.key}`">
              <tr v-if="i === 0 || configRows[i - 1].section !== row.section">
                <td colspan="2" class="pt-2 pb-1 font-semibold text-color-secondary uppercase" style="font-size: 0.75rem">
                  [{{ row.section }}]
                </td>
              </tr>
              <tr>
                <td class="pr-3 text-color-secondary white-space-nowrap vertical-align-top">{{ row.key }}</td>
                <td class="font-mono" style="word-break: break-all">{{ row.value }}</td>
              </tr>
            </template>
          </tbody>
        </table>
        <div class="text-color-secondary text-xs mt-2">
          Read-only -- edit configs/{{ name }}.toml directly, then Redeploy to apply changes.
        </div>
      </div>
    </div>

    <JobPanel :job-id="activeJobId" :eta-hint="activeActionEta" @finished="onJobFinished" />

    <details v-if="outputs" class="text-sm">
      <summary class="cursor-pointer text-color-secondary">Stack outputs (raw)</summary>
      <pre class="job-log mt-2">{{ JSON.stringify(outputs, null, 2) }}</pre>
    </details>
  </div>
</template>
