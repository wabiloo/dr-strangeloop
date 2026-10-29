<script setup lang="ts">
// The preview side of a channel: live players, the Timeshift / URL overrides panels, the
// timeline window and the Playback test. The actions and configuration are on the channel's
// detail page (ChannelDetail.vue); a channel that has just come alive lands here.
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BackendBadge from '../components/BackendBadge.vue'
import ItsAliveBanner from '../components/ItsAliveBanner.vue'
import ManifestsPanel from '../components/ManifestsPanel.vue'
import PlaybackPanel from '../components/PlaybackPanel.vue'
import PlaybackTestPanel from '../components/PlaybackTestPanel.vue'
import UrlRequestPanels, { type TimeshiftPreview } from '../components/UrlRequestPanels.vue'
import WindowPanel from '../components/WindowPanel.vue'
import { useManifestCapture } from '../composables/useManifestCapture'
import { getChannel, getChannelHealth, getChannelOutputs, getChannelStatus, getChannelSummary } from '../api/client'
import type { ChannelHealth, ChannelListItem, ChannelOutputs, ChannelStatus } from '../api/types'
import {
  channelDefaultsByParam,
  formFieldsFromConfig,
  periodTypesFromConfig,
  usesChannelSection,
} from '../utils/channelConfigLayout'
import { type Phase, PHASE_LABEL, listItemPhase, liveStatusPhase, phaseSeverity } from '../utils/channelPhase'
import { timeshiftParamsFromConfig } from '../utils/timeshift'

const props = defineProps<{ name: string }>()
// Installed at setup, before any player exists, so no poll is missed.
const manifestCapture = useManifestCapture()
const route = useRoute()
const router = useRouter()

const status = ref<ChannelStatus | null>(null)
const outputs = ref<ChannelOutputs | null>(null)
const health = ref<ChannelHealth | null>(null)
const config = ref<Record<string, unknown> | null>(null)
const listItem = ref<ChannelListItem | null>(null)
const statusError = ref('')
const healthError = ref('')
const playbackPanel = ref<InstanceType<typeof PlaybackPanel> | null>(null)
const itsAliveBanner = ref<InstanceType<typeof ItsAliveBanner> | null>(null)

function section(key: string): Record<string, unknown> {
  return (config.value?.[key] as Record<string, unknown>) ?? {}
}

// Same phase logic as the detail page and the list (utils/channelPhase.ts).
const statusErrorIsMissingStack = computed(() => /does not exist/.test(statusError.value))
const phase = computed<Phase>(() => {
  if (!status.value) return statusErrorIsMissingStack.value ? 'not-deployed' : 'unknown'
  return liveStatusPhase(status.value)
})
const listPhase = computed<Phase>(() => (listItem.value ? listItemPhase(listItem.value) : phase.value))

// local-docker has no stack outputs -- its playback URLs come from /status instead.
const playbackHlsUrl = computed(() =>
  status.value?.backend === 'local-docker' ? status.value.hls_url : outputs.value?.HlsPlaybackUrl,
)
const playbackDashUrl = computed(() =>
  status.value?.backend === 'local-docker' ? status.value.dash_url : outputs.value?.DashPlaybackUrl,
)
// Stack outputs stay populated while the channel is stopped, so the live phase gates too.
const showPlayback = computed(
  () => (playbackHlsUrl.value || playbackDashUrl.value) && phase.value !== 'stopped' && phase.value !== 'not-deployed',
)
const loaded = computed(() => status.value !== null || statusError.value !== '')

// Startover/catchup and per-request overrides: only ecs-express / aws-lambda / local-docker run serve.py.
const timeshiftParams = computed(() => timeshiftParamsFromConfig(config.value ?? undefined))
const timeshiftAvailable = computed(
  () =>
    usesChannelSection(String(section('deploy').backend ?? '')) &&
    (timeshiftParams.value.timeshift || timeshiftParams.value.overrides),
)
const channelQueryDefaults = computed(() => channelDefaultsByParam(formFieldsFromConfig(config.value ?? {})))
const timeshiftPreview = ref<TimeshiftPreview | null>(null)
// What the forms build right now (previewed or not): the Playback test runs against it.
const timeshiftBuilt = ref<{ hlsUrl: string | null; dashUrl: string | null } | null>(null)
// A preview left behind when the channel stops/changes would point the players at a dead URL.
watch([playbackHlsUrl, playbackDashUrl], () => (timeshiftPreview.value = null))
const effectiveHlsUrl = computed(() => timeshiftPreview.value?.hlsUrl ?? playbackHlsUrl.value)
const effectiveDashUrl = computed(() => timeshiftPreview.value?.dashUrl ?? playbackDashUrl.value)
const testHlsUrl = computed(() => (timeshiftBuilt.value ? timeshiftBuilt.value.hlsUrl : effectiveHlsUrl.value))
const testDashUrl = computed(() => (timeshiftBuilt.value ? timeshiftBuilt.value.dashUrl : effectiveDashUrl.value))

// loop-dee-loop's /timeline.json describes the live window, or -- while a preview is active --
// the previewed range (same query).
const windowAvailable = computed(
  () => !!showPlayback.value && usesChannelSection(String(section('deploy').backend ?? '')),
)
// A continuous timeline has no loop-wrap boundary: only forced signal-break Periods ([markers] split_on) are any.
const boundariesExpected = computed(() => {
  const continuous = Boolean(section('timeline').continuous ?? true)
  const forced = periodTypesFromConfig(section('markers').split_on)
  return !continuous || forced.length > 0
})
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
  } catch {
    config.value = null
  }
}
async function loadStatus() {
  statusError.value = ''
  try {
    ;[status.value, outputs.value] = await Promise.all([getChannelStatus(props.name), getChannelOutputs(props.name)])
  } catch (e) {
    statusError.value = e instanceof Error ? e.message : String(e)
  }
}
async function loadListPhase() {
  try {
    listItem.value = await getChannelSummary(props.name)
  } catch {
    // keep the previous value; listPhase falls back to the live phase
  }
}
async function loadHealth() {
  if (!['ecs-express', 'aws-lambda', 'local-docker'].includes(status.value?.backend ?? '')) return
  try {
    health.value = await getChannelHealth(props.name)
    healthError.value = ''
  } catch (e) {
    healthError.value = e instanceof Error ? e.message : String(e)
  }
}

function reload() {
  status.value = null
  outputs.value = null
  health.value = null
  config.value = null
  listItem.value = null
  timeshiftPreview.value = null
  timeshiftBuilt.value = null
  manifestCapture.reset()
  loadStatus().then(loadHealth)
  loadListPhase()
  loadConfig()
}

// Back on the live edge whenever the channel (re-)reaches "It's Alive!": a redeploy can leave
// the URLs identical while a player stays attached to the old run.
watch(phase, (next, prev) => {
  if (next === 'alive' && prev !== 'alive') playbackPanel.value?.reloadPlayers()
})

// The detail page sends a channel that has just come alive here with ?alive=1.
let celebrate = route.query.alive === '1'
watch(
  showPlayback,
  async (now) => {
    if (!now || !celebrate) return
    celebrate = false
    router.replace({ query: {} })
    await nextTick()
    itsAliveBanner.value?.trigger()
  },
  { immediate: true },
)

const STATUS_POLL_MS = 8000
let healthTimer: ReturnType<typeof setInterval> | null = null
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
    <div class="flex align-items-center gap-2 flex-wrap">
      <h2 class="m-0">{{ name }}</h2>
      <Tag :value="PHASE_LABEL[listPhase]" :severity="phaseSeverity(listPhase)" />
      <span class="text-color-secondary">Preview</span>
      <BackendBadge v-if="status" :backend="status.backend" pill />
      <Button
        label="Actions & configuration"
        icon="pi pi-cog"
        size="small"
        severity="secondary"
        class="ml-auto"
        data-testid="to-channel-detail"
        @click="router.push({ name: 'channel-detail', params: { name } })"
      />
    </div>

    <Message v-if="statusError && !statusErrorIsMissingStack" severity="warn">
      <div>Could not fetch live status.</div>
      <details class="mt-2">
        <summary class="cursor-pointer text-sm">Show details</summary>
        <pre class="job-log mt-2">{{ statusError }}</pre>
      </details>
    </Message>

    <Message v-if="phase === 'unreachable'" severity="error" :closable="false">
      Infrastructure reports running, but HlsPlaybackUrl/DashPlaybackUrl did not return a manifest on the last check.
      Common causes: a MediaPackage/CloudFront endpoint still propagating right after deploy, an empty target group, or
      content not actually Sparked/Started yet.
    </Message>

    <Message v-if="loaded && !showPlayback" severity="secondary" :closable="false">
      This channel is not playing ({{ PHASE_LABEL[phase] }}), so there is nothing to preview. Start it from
      <RouterLink :to="{ name: 'channel-detail', params: { name } }">its actions page</RouterLink>.
    </Message>

    <PlaybackPanel
      v-if="showPlayback"
      ref="playbackPanel"
      :hls-url="effectiveHlsUrl"
      :dash-url="effectiveDashUrl"
      :start-from-beginning="timeshiftPreview?.startEpochSeconds != null"
      :dash-clock-offset-seconds="timeshiftPreview?.startEpochSeconds ?? 0"
      :health="health"
      :health-error="['ecs-express', 'aws-lambda', 'local-docker'].includes(status?.backend ?? '') ? healthError : ''"
    />

    <WindowPanel v-if="windowAvailable" :name="name" :query="windowQuery" />

    <UrlRequestPanels
      v-if="showPlayback && timeshiftAvailable"
      :hls-url="playbackHlsUrl"
      :dash-url="playbackDashUrl"
      :params="timeshiftParams"
      :channel-defaults="channelQueryDefaults"
      :health="health"
      :previewing="timeshiftPreview !== null"
      @preview="timeshiftPreview = $event"
      @built="timeshiftBuilt = $event"
    />

    <ManifestsPanel v-if="showPlayback" :capture="manifestCapture" />

    <PlaybackTestPanel
      v-if="showPlayback"
      :name="name"
      :has-timeline="windowAvailable"
      :boundaries-expected="boundariesExpected"
      :loop-duration-s="health?.total_loop_duration_seconds"
      :hls-url="testHlsUrl"
      :dash-url="testDashUrl"
      :base-hls-url="playbackHlsUrl"
      :base-dash-url="playbackDashUrl"
    />

    <ItsAliveBanner ref="itsAliveBanner" />
  </div>
</template>
