<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  getPlaybackInfo,
  getPlaybackReport,
  listPlaybackTests,
  playbackBrowserRunUrl,
  playbackScreenshotUrl,
  setupPlayback,
  startPlaybackTest,
} from '../api/client'
import type { Job, PlaybackCase, PlaybackInfo, PlaybackReport, PlaybackRunSummary } from '../api/types'
import { formatDateTime } from '../utils/dateFormat'
import FieldHelp from './FieldHelp.vue'
import JobPanel from './JobPanel.vue'

const props = defineProps<{
  name: string
  /** ecs-express / local-docker serve /timeline.json: the run then ends after N boundaries.
   * Otherwise it plays for a fixed duration and only the generic checks apply. */
  hasTimeline: boolean
  /** Live manifest URLs the player-side of the in-browser mode loads (as the Live Playback panel does). */
  hlsUrl?: string | null
  dashUrl?: string | null
}>()

const STORAGE_KEY = 'igor.playbackTestPanel.open'
function loadOpen(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
const open = ref(loadOpen())
function toggle() {
  open.value = !open.value
  try {
    localStorage.setItem(STORAGE_KEY, open.value ? '1' : '0')
  } catch {
    // per-browser convenience only
  }
  if (open.value) void ensureLoaded()
}

const PLAYER_LABEL: Record<string, string> = {
  dashjs: 'dash.js',
  shaka: 'Shaka',
  hlsjs: 'hls.js',
  videojs: 'Video.js',
  bitmovin: 'Bitmovin',
}
const label = (p: string) => PLAYER_LABEL[p] ?? p
const noKey = (p: string) => info.value?.keys?.[p] === false

const info = ref<PlaybackInfo | null>(null)
const infoError = ref('')
const runs = ref<PlaybackRunSummary[]>([])
const selectedPlayers = ref<string[]>([])
const formatChoice = ref<'both' | 'hls' | 'dash'>('both')
const boundaries = ref(2)
const durationS = ref(120)
const withFfmpeg = ref(false)
const mode = ref<'server' | 'browser'>('browser')

// In-browser mode: the driver page runs in its own tab and posts its measurements to Igor.
const browserActive = ref(false)
const browserStatus = ref('')
let browserWin: Window | null = null
let browserWatch: ReturnType<typeof setInterval> | null = null

const activeJobId = ref<string | null>(null)
const activeKind = ref<'run' | 'setup' | null>(null)
const activeRunId = ref<string | null>(null)
const starting = ref(false)
const actionError = ref('')

const report = ref<PlaybackReport | null>(null)
const reportRunId = ref<string | null>(null)
const reportError = ref('')

const busy = computed(() => (!!activeJobId.value && !jobDone.value) || browserActive.value)
const jobDone = ref(false)

const ready = computed(() => !!info.value && info.value.sdks_installed && (mode.value === 'browser' || info.value.chrome))
const playersList = computed(() => Object.keys(info.value?.players ?? {}))
const canRun = computed(() => ready.value && selectedPlayers.value.length > 0 && !busy.value && !starting.value)

let loaded = false
async function ensureLoaded() {
  if (loaded) return
  loaded = true
  await refresh()
}

async function refresh() {
  infoError.value = ''
  try {
    info.value = await getPlaybackInfo()
    if (selectedPlayers.value.length === 0) selectedPlayers.value = [...info.value.default_players]
    selectedPlayers.value = selectedPlayers.value.filter((p) => !noKey(p))
    if (!info.value.chrome) mode.value = 'browser'
  } catch (e) {
    infoError.value = e instanceof Error ? e.message : String(e)
    return
  }
  try {
    const r = await listPlaybackTests(props.name)
    runs.value = r.runs
    if (r.active && !activeJobId.value) {
      activeJobId.value = r.active.id
      activeKind.value = 'run'
      activeRunId.value = r.active.run_id
      jobDone.value = false
    } else if (!report.value && r.runs.length) {
      await showRun(r.runs[0].run_id)
    }
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  }
}

async function showRun(runId: string) {
  reportError.value = ''
  try {
    report.value = await getPlaybackReport(props.name, runId)
    reportRunId.value = runId
  } catch (e) {
    report.value = null
    reportError.value = e instanceof Error ? e.message : String(e)
  }
}

async function run() {
  actionError.value = ''
  starting.value = true
  try {
    const job = await startPlaybackTest(props.name, {
      players: selectedPlayers.value,
      formats: formatChoice.value === 'both' ? undefined : [formatChoice.value],
      boundaries: boundaries.value,
      duration_s: durationS.value,
      ffmpeg_s: withFfmpeg.value ? 30 : null,
    })
    jobDone.value = false
    activeKind.value = 'run'
    activeRunId.value = job.run_id
    activeJobId.value = job.id
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    starting.value = false
  }
}

function runInBrowser() {
  actionError.value = ''
  const urls = { hls: props.hlsUrl, dash: props.dashUrl }
  const formats = (formatChoice.value === 'both' ? ['hls', 'dash'] : [formatChoice.value]).filter((f) => urls[f as 'hls' | 'dash'])
  const cases = formats.flatMap((f) =>
    playersList.value.filter((p) => selectedPlayers.value.includes(p) && info.value?.players[p]?.includes(f)).map((p) => `${p}:${f}`),
  )
  if (!cases.length) {
    actionError.value = 'No player/format combination to run (is the channel running, and does a selected player support that format?).'
    return
  }
  const src = playbackBrowserRunUrl(props.name, {
    cases,
    hls: props.hlsUrl,
    dash: props.dashUrl,
    withTimeline: props.hasTimeline,
    boundaries: boundaries.value,
    durationS: durationS.value,
    skipped: playersList.value.filter(noKey),
  })
  // Opened synchronously from the click so popup blockers allow it.
  const win = window.open(src, '_blank')
  if (!win) {
    actionError.value = 'The browser blocked the new tab: allow pop-ups for Igor and try again.'
    return
  }
  browserWin = win
  browserActive.value = true
  browserStatus.value = 'starting...'
  browserWatch = setInterval(() => {
    if (browserWin && browserWin.closed) endBrowserRun('The test tab was closed before the run finished; nothing was saved.')
  }, 1000)
}

function endBrowserRun(error?: string) {
  if (browserWatch) clearInterval(browserWatch)
  browserWatch = null
  browserWin = null
  browserActive.value = false
  if (error) actionError.value = error
}

function stopBrowserRun() {
  try {
    browserWin?.close()
  } catch {
    // already gone
  }
  endBrowserRun()
}

async function onDriverMessage(ev: MessageEvent) {
  const d = ev.data
  if (ev.origin !== window.location.origin || !d || d.source !== 'player-lab' || !browserActive.value) return
  if (d.type === 'progress') {
    browserStatus.value = String(d.text)
  } else if (d.type === 'error') {
    endBrowserRun(`In-browser run failed: ${d.message}`)
  } else if (d.type === 'done') {
    endBrowserRun()
    try {
      runs.value = (await listPlaybackTests(props.name)).runs
    } catch {
      // history is a convenience
    }
    await showRun(String(d.runId))
  }
}
onMounted(() => window.addEventListener('message', onDriverMessage))
onBeforeUnmount(() => {
  window.removeEventListener('message', onDriverMessage)
  if (browserWatch) clearInterval(browserWatch)
})

async function install() {
  actionError.value = ''
  try {
    const job = await setupPlayback()
    jobDone.value = false
    activeKind.value = 'setup'
    activeJobId.value = job.id
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  }
}

async function onFinished(job: Job) {
  jobDone.value = true
  if (activeKind.value === 'setup') {
    loaded = false
    await ensureLoaded()
    return
  }
  try {
    runs.value = (await listPlaybackTests(props.name)).runs
  } catch {
    // history is a convenience
  }
  if (activeRunId.value) await showRun(activeRunId.value)
  if (!report.value && job.status === 'failed') reportError.value = 'The run ended without a report; see the log above.'
}

watch(() => props.name, () => {
  loaded = false
  report.value = null
  runs.value = []
  activeJobId.value = null
  stopBrowserRun()
  if (open.value) void ensureLoaded()
})
onMounted(() => {
  if (open.value) void ensureLoaded()
})

function fmtStart(c: PlaybackCase) {
  return c.startedAfterS == null ? 'never' : `${c.startedAfterS.toFixed(1)} s`
}
function expected(c: PlaybackCase) {
  return c.crossed == null ? '–' : String(c.crossed)
}
function seen(c: PlaybackCase) {
  return c.periodTransitions == null ? 'n/a' : String(c.periodTransitions)
}
function runLabel(r: PlaybackRunSummary) {
  const when = r.generated_at ? formatDateTime(r.generated_at) : r.run_id
  return `${r.passed ? '✓' : '✗'} ${when}`
}
const selectedRun = computed({
  get: () => reportRunId.value,
  set: (v: string | null) => {
    if (v) void showRun(v)
  },
})
const summaryLine = computed(() => {
  const r = report.value
  if (!r) return ''
  const bad = r.cases.filter((c) => !c.pass).length
  return bad ? `${bad} of ${r.cases.length} failed` : `all ${r.cases.length} passed`
})
</script>

<template>
  <div class="playback-test-panel surface-card border-round p-3 flex flex-column gap-3">
    <div class="flex align-items-center gap-2 flex-wrap">
      <button type="button" class="ptp-toggle flex align-items-center gap-2" :aria-expanded="open" @click="toggle">
        <i :class="['pi', open ? 'pi-chevron-down' : 'pi-chevron-right']" aria-hidden="true" />
        <h3 class="m-0 text-base">Playback test</h3>
      </button>
      <FieldHelp label="Playback test">
        Plays this channel with several players (hls.js, dash.js, Bitmovin, Shaka, Video.js), either in headless Chrome on
        the machine running Igor, or in this browser (keep the tab in the foreground; the browser must be able to reach
        the channel, and its codecs decide what plays, e.g. Safari for native-like behaviour). For each player it measures the startup time, stalls (playhead frozen for a second or more, measured on
        the video element itself), player errors and dropped frames. If the channel serves /timeline.json, the run lasts
        until the requested number of loop boundaries (Periods / discontinuities) went by, and the transitions seen by
        the players that report them (hls.js, dash.js) are compared with what the channel says it crossed.
      </FieldHelp>
      <span v-if="!open" class="text-sm text-color-secondary">Stalls, errors and Period/discontinuity counts in several players</span>
      <template v-else-if="report">
        <Tag :value="report.pass ? 'pass' : 'fail'" :severity="report.pass ? 'success' : 'danger'" />
        <span class="text-sm text-color-secondary">{{ summaryLine }}</span>
      </template>
    </div>

    <template v-if="open">
      <Message v-if="infoError" severity="error" :closable="false">{{ infoError }}</Message>
      <Message v-else-if="info && !info.chrome && mode === 'server'" severity="warn" :closable="false">
        Google Chrome was not found on the machine running Igor; the headless test needs it (H.264).
      </Message>
      <Message v-else-if="info && !info.sdks_installed" severity="info" :closable="false">
        <div class="flex align-items-center gap-3">
          <span>The player libraries are not installed yet{{ info.npm ? '.' : ' and npm was not found.' }}</span>
          <Button label="Install players" icon="pi pi-download" size="small" :disabled="!info.npm || busy" @click="install" />
        </div>
      </Message>

      <div v-if="info" class="flex align-items-center gap-4 flex-wrap">
        <Select
          v-model="mode"
          :options="[
            { label: 'In this browser', value: 'browser' },
            { label: 'Headless Chrome', value: 'server', disabled: !info.chrome },
          ]"
          option-label="label"
          option-value="value"
          option-disabled="disabled"
          size="small"
          aria-label="Where to run"
        />
        <div v-for="p in playersList" :key="p" class="flex align-items-center gap-2">
          <Checkbox v-model="selectedPlayers" :input-id="`ptp-${p}`" :value="p" :disabled="noKey(p)" />
          <label
            :for="`ptp-${p}`"
            :class="{ 'text-color-secondary': noKey(p) }"
            :title="noKey(p) ? 'No licence key: set BITMOVIN_LICENSE_KEY or add a bitmovin entry under [keys] in ~/.dr-strangeloop/config.toml' : `formats: ${info.players[p].join(', ')}`"
            >{{ label(p) }}<span v-if="noKey(p)"> (no licence key)</span></label
          >
        </div>
        <Select
          v-model="formatChoice"
          :options="[
            { label: 'HLS and DASH', value: 'both' },
            { label: 'HLS only', value: 'hls' },
            { label: 'DASH only', value: 'dash' },
          ]"
          option-label="label"
          option-value="value"
          size="small"
          aria-label="Formats"
        />
        <label v-if="hasTimeline" class="flex align-items-center gap-2 text-sm">
          Boundaries
          <InputNumber v-model="boundaries" :min="1" :max="20" show-buttons input-class="ptp-num" size="small" />
        </label>
        <label v-else class="flex align-items-center gap-2 text-sm" title="No /timeline.json on this backend: play for a fixed time; only stalls and errors are judged">
          Duration (s)
          <InputNumber v-model="durationS" :min="10" :max="3600" input-class="ptp-num" size="small" />
        </label>
        <label v-if="info.ffmpeg && mode === 'server'" class="flex align-items-center gap-2 text-sm" title="Also demux each manifest with ffmpeg for 30 s (informational)">
          <Checkbox v-model="withFfmpeg" binary />
          ffmpeg check
        </label>
        <Button
          label="Run test"
          icon="pi pi-play"
          size="small"
          class="ml-auto"
          :disabled="!canRun"
          :loading="starting || busy"
          @click="mode === 'browser' ? runInBrowser() : run()"
        />
      </div>
      <Message v-if="actionError" severity="error" :closable="false">{{ actionError }}</Message>

      <div v-if="browserActive" class="flex flex-column gap-2">
        <div class="flex align-items-center gap-2 text-sm">
          <i class="pi pi-spin pi-spinner" aria-hidden="true" />
          <span class="ptp-status">Running in another tab: {{ browserStatus }}</span>
          <Button label="Stop" icon="pi pi-stop" size="small" severity="secondary" class="ml-auto" @click="stopBrowserRun" />
        </div>
        <Message severity="info" :closable="false">
          The players run in the new tab: keep it in the foreground (browsers throttle background tabs, which would
          show up as stalls). The result appears here when the run ends; closing the tab discards it.
        </Message>
      </div>

      <JobPanel
        :job-id="activeJobId"
        :eta-hint="activeKind === 'run' ? 'the players play in real time, until the boundaries have gone by (a loop can be long).' : undefined"
        @finished="onFinished"
      />

      <Message v-if="reportError" severity="warn" :closable="false">{{ reportError }}</Message>

      <template v-if="report">
        <div class="flex align-items-center gap-2 flex-wrap text-sm text-color-secondary">
          <Select
            v-if="runs.length > 1"
            v-model="selectedRun"
            :options="runs"
            :option-label="runLabel"
            option-value="run_id"
            size="small"
            aria-label="Earlier runs"
          />
          <span>
            {{ formatDateTime(report.generatedAt) }} · {{ report.durationS }} s ·
            boundaries from {{ report.boundaries.source }}
            <template v-if="report.mode === 'browser'"> · run in a browser ({{ report.userAgent }})</template>
            <template v-if="report.boundaries.newPeriods != null">
              (timeline: {{ report.boundaries.newPeriods }} Period(s), {{ report.boundaries.newDiscontinuities }} discontinuity(ies) crossed)
            </template>
          </span>
        </div>

        <div class="ptp-scroll">
          <table class="ptp-table">
            <thead>
              <tr>
                <th>Player</th>
                <th>Format</th>
                <th class="num">Startup</th>
                <th class="num">Stalls</th>
                <th class="num">Stalled</th>
                <th class="num">Errors</th>
                <th class="num" title="Period/discontinuity transitions the player reported">Seen</th>
                <th class="num" title="Boundaries the channel's /timeline.json says were crossed">Expected</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="c in report.cases" :key="c.player + c.format">
                <tr :class="{ 'ptp-fail': !c.pass }">
                  <td>{{ label(c.player) }} <span class="text-xs text-color-secondary">{{ c.version }}</span></td>
                  <td>{{ c.format.toUpperCase() }}</td>
                  <td class="num">{{ fmtStart(c) }}</td>
                  <td class="num">{{ c.stallCount }}</td>
                  <td class="num">{{ c.stallSeconds }} s</td>
                  <td class="num">{{ c.fatalErrors }}</td>
                  <td class="num">{{ seen(c) }}</td>
                  <td class="num">{{ expected(c) }}</td>
                  <td><Tag :value="c.pass ? 'pass' : 'fail'" :severity="c.pass ? 'success' : 'danger'" /></td>
                </tr>
                <tr v-if="c.failures.length || c.warnings?.length || c.errors?.length" class="ptp-detail">
                  <td colspan="9">
                    <div v-for="f in c.failures" :key="f" class="text-sm">✗ {{ f }}</div>
                    <div v-for="w in c.warnings ?? []" :key="w" class="text-sm text-color-secondary">! {{ w }}</div>
                    <div v-for="(e, i) in (c.errors ?? []).slice(0, 3)" :key="i" class="text-xs text-color-secondary ptp-err">
                      error: {{ e.message }}
                    </div>
                    <a
                      v-if="c.screenshot && reportRunId"
                      :href="playbackScreenshotUrl(name, reportRunId, c.screenshot)"
                      target="_blank"
                      rel="noopener"
                      class="text-xs"
                    >screenshot at the end of the run</a>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
        <div v-for="f in report.ffmpeg ?? []" :key="f.url" class="text-sm text-color-secondary">
          ffmpeg {{ f.skipped ? `skipped (${f.skipped})` : f.pass ? 'ok' : 'reported problems' }}: {{ f.url }}
          <div v-for="p in f.problems ?? []" :key="p" class="text-xs ptp-err">{{ p }}</div>
        </div>
        <div class="text-xs text-color-secondary">
          Shaka does not report Period transitions on DASH, so only its stalls and errors are judged there. Players
          start a little behind the live edge, so the seen count may differ from the expected one by one.
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.ptp-status {
  font-variant-numeric: tabular-nums;
  word-break: break-all;
}
.ptp-toggle {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  color: inherit;
}
.ptp-scroll {
  overflow-x: auto;
}
.ptp-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}
.ptp-table th,
.ptp-table td {
  text-align: left;
  padding: 0.35rem 0.6rem;
  border-bottom: 1px solid var(--surface-border);
}
.ptp-table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.ptp-table .ptp-detail td {
  border-bottom: 1px solid var(--surface-border);
  padding-top: 0;
}
.ptp-fail td {
  border-bottom: 0;
}
.ptp-err {
  word-break: break-all;
}
:deep(.ptp-num) {
  width: 5rem;
}
</style>
