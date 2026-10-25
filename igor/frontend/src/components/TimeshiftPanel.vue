<script setup lang="ts">
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'
import type { UrlRequestForm } from '../composables/useUrlRequestForm'
import { floorToLoopMs, FULL_LOOPS_PARAM, OFFSET_PARAM, paramName, type TimeshiftParams } from '../utils/timeshift'
import FieldHelp from './FieldHelp.vue'

// Start / end / whole loops / offset. The state is shared with the URL overrides panel (both
// build the same manifest URLs): see useUrlRequestForm.
const props = defineProps<{ params: TimeshiftParams; form: UrlRequestForm }>()
const { time, offsetText, offsetHint, fullTiming, pretendNow, nowMs, setRange, setNow, onZoneChange } = props.form

// Collapsed by default so live viewing isn't crowded by a big form; the choice is remembered
// per browser. The body is v-show (not v-if) so form state survives collapsing.
const STORAGE_KEY = 'igor.timeshiftPanel.open'
function readOpen(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false // storage blocked/unavailable: just stay collapsed
  }
}
const open = ref(readOpen())
function onHeaderClick(e: MouseEvent) {
  // The whole header row toggles; other buttons/links in it (help, ...) keep their own behavior.
  const hit = (e.target as HTMLElement).closest('button, a, input')
  if (hit && !hit.classList.contains('ts-toggle')) return
  open.value = !open.value
  try {
    localStorage.setItem(STORAGE_KEY, open.value ? '1' : '0')
  } catch {
    /* non-persistent is fine */
  }
}

// --- presets --------------------------------------------------------------------
function presetLast(minutes: number) {
  const now = pretendNow()
  nowMs.value = Date.now()
  setRange(new Date(now - minutes * 60_000), new Date(now), false)
}
function presetStartoverFrom(minutesAgo: number) {
  const now = pretendNow()
  nowMs.value = Date.now()
  setRange(new Date(now - minutesAgo * 60_000), null, false)
}
function presetCurrentLoop() {
  const t = fullTiming.value
  if (!t) return
  const now = pretendNow()
  nowMs.value = Date.now()
  setRange(new Date(floorToLoopMs(now, t)), null, true)
}
function presetPreviousLoop() {
  const t = fullTiming.value
  if (!t) return
  const now = pretendNow()
  nowMs.value = Date.now()
  const thisLoop = floorToLoopMs(now, t)
  setRange(new Date(thisLoop - t.loopMs), new Date(thisLoop), true)
}

const offsetModeOptions = [
  { label: 'Duration', value: 'duration' },
  { label: 'Pretend “now” is (datetime)', value: 'datetime' },
]
const zoneOptions = [
  { label: 'UTC', value: true },
  { label: 'Local', value: false },
]
const formatOptions = [
  { label: 'ISO 8601', value: 'iso' },
  { label: 'Epoch seconds', value: 'epoch' },
  { label: 'Epoch milliseconds', value: 'epoch_ms' },
]
</script>

<template>
  <section class="timeshift-panel surface-card border-round p-3 flex flex-column gap-3">
    <div class="ts-toggle-row flex align-items-center gap-2 flex-wrap" @click="onHeaderClick">
      <button type="button" class="ts-toggle flex align-items-center gap-2" :aria-expanded="open" aria-controls="ts-body">
        <i :class="['pi', open ? 'pi-chevron-down' : 'pi-chevron-right']" aria-hidden="true" />
        <h3 class="m-0 text-base">Timeshift: startover, catchup &amp; offset</h3>
      </button>
      <FieldHelp label="Timeshift">
        The same manifest URLs the channel serves live also accept a start (and optional end) time as query
        parameters. With a past end it is a finished VOD (catchup); with a future or missing end it is a live-style
        stream that starts at the start point (startover). An offset instead plays the stream as it was, or will be,
        at another time. Nothing is recorded: the past is re-derived from the loop and the channel epoch, so it stays
        correct only while the epoch and the baked content are unchanged. The URLs built here also carry whatever is
        set in the URL overrides panel.
      </FieldHelp>
      <span v-if="!open" class="text-sm text-color-secondary">Play past ranges, or “as if it were another time”</span>
    </div>

    <!-- v-show on a plain wrapper: primeflex's `.flex` is display:flex !important and would defeat it. -->
    <div v-show="open" id="ts-body">
      <div class="flex flex-column gap-3">
        <div class="grid">
      <div v-if="!params.timeshift" class="col-12">
        <Message severity="secondary" :closable="false">
          Startover and catchup are switched off for this channel (<code>[query] timeshift</code>): start, end,
          whole loops and offset are ignored, so it only plays live.
        </Message>
      </div>
      <template v-if="params.timeshift">
      <div class="col-12 md:col-6 lg:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-start">
          Start <code class="qp">{{ paramName(params, params.start_param) }}</code>
        </label>
        <div class="flex gap-2">
          <input id="ts-start" v-model="time.startText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button label="Now" size="small" severity="secondary" text @click="setNow('start')" />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear start" @click="time.startText = ''" />
        </div>
      </div>
      <div class="col-12 md:col-6 lg:col-4 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-end">
          End <code class="qp">{{ paramName(params, params.end_param) }}</code> <span>(optional)</span>
        </label>
        <div class="flex gap-2">
          <input id="ts-end" v-model="time.endText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button label="Now" size="small" severity="secondary" text @click="setNow('end')" />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear end" @click="time.endText = ''" />
        </div>
      </div>
      <div class="col-12 md:col-6 lg:col-4 flex flex-column justify-content-end">
        <SelectButton
          :model-value="time.useUtc"
          :options="zoneOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          @update:model-value="onZoneChange"
        />
      </div>
      <div class="col-12 flex gap-2 flex-wrap align-items-center">
        <span class="text-xs text-color-secondary uppercase">Presets</span>
        <Button label="Last 5 min" size="small" severity="secondary" outlined @click="presetLast(5)" />
        <Button label="Last 15 min" size="small" severity="secondary" outlined @click="presetLast(15)" />
        <Button label="Last hour" size="small" severity="secondary" outlined @click="presetLast(60)" />
        <Button label="Startover from 2 min ago" size="small" severity="secondary" outlined @click="presetStartoverFrom(2)" />
        <Button
          label="Previous loop"
          size="small"
          severity="secondary"
          outlined
          :disabled="!fullTiming"
          :title="fullTiming ? '' : 'Needs the channel running (loop timing from /health)'"
          @click="presetPreviousLoop"
        />
        <Button
          label="Startover from current loop"
          size="small"
          severity="secondary"
          outlined
          :disabled="!fullTiming"
          :title="fullTiming ? '' : 'Needs the channel running (loop timing from /health)'"
          @click="presetCurrentLoop"
        />
      </div>
      <div class="col-12 flex align-items-center gap-2">
        <Checkbox v-model="time.fullLoop" binary input-id="ts-full-loop" />
        <label for="ts-full-loop">Whole loops only <code class="qp">{{ paramName(params, FULL_LOOPS_PARAM) }}</code></label>
        <FieldHelp label="Whole loops only">
          Widens the range to complete loop iterations: the start moves back to the nearest loop start at or before
          it, and the end (if given) moves forward to the nearest loop end at or after it. With no end, the
          range runs for as many whole loops as fit within the maximum span. The maximum span applies to the
          widened range.
        </FieldHelp>
      </div>
      <div class="col-12 flex flex-column gap-1">
        <label class="text-xs text-color-secondary" for="ts-offset">
          Pretend “now” is… <code class="qp">{{ paramName(params, OFFSET_PARAM) }}</code>
          <FieldHelp label="Offset">
            Plays the stream as it was (negative) or will be (positive) that long from now: the live edge and the
            DVR window behind it sit at <em>now + offset</em>, every timestamp is the true content time, and
            “past”/“future” for start and end are judged against that moment. Signed seconds
            (<code>-3600</code>) or an ISO 8601 duration (<code>-PT1H</code>, <code>P1DT2H</code>). A negative
            offset can't reach back before the channel epoch. Instead of a duration you can pick the
            datetime “now” should pretend to be: Igor converts it to an offset when you press Preview or Copy
            (<em>that datetime minus the moment you start</em>) and fixes it in the URL, so the stream plays on in
            real time from that instant; copy again later and the offset will differ accordingly. DASH manifests carry a
            <code>UTCTiming</code> value so players use that time as “now”.
          </FieldHelp>
        </label>
        <SelectButton
          v-model="time.offsetMode"
          :options="offsetModeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
        />
        <div v-if="time.offsetMode === 'duration'" class="flex gap-2 flex-wrap align-items-center">
          <InputText id="ts-offset" v-model="time.offsetDurationText" placeholder="e.g. -PT1H or -3600" class="flex-1" style="min-width: 10rem" />
          <Button label="−1 h" size="small" severity="secondary" outlined @click="offsetText = '-PT1H'" />
          <Button label="−10 min" size="small" severity="secondary" outlined @click="offsetText = '-PT10M'" />
          <Button label="+1 h" size="small" severity="secondary" outlined @click="offsetText = 'PT1H'" />
          <Button
            label="−1 loop"
            size="small"
            severity="secondary"
            outlined
            :disabled="!fullTiming"
            @click="offsetText = String(-Math.round(fullTiming!.loopMs / 1000))"
          />
          <Button
            label="+1 loop"
            size="small"
            severity="secondary"
            outlined
            :disabled="!fullTiming"
            @click="offsetText = String(Math.round(fullTiming!.loopMs / 1000))"
          />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear offset" @click="offsetText = ''" />
        </div>
        <div v-else class="flex gap-2 align-items-center">
          <input id="ts-pretend" v-model="time.pretendText" type="datetime-local" step="1" class="p-inputtext flex-1" />
          <Button icon="pi pi-times" size="small" severity="secondary" text aria-label="Clear" @click="time.pretendText = ''" />
        </div>
        <div v-if="time.offsetMode === 'datetime'" class="text-xs text-color-secondary">
          Turned into an offset when you press Preview or Copy (the stream then plays on in real time from
          that instant), in the same zone as the other times above.
        </div>
        <div v-if="offsetHint" class="text-xs text-color-secondary">{{ offsetHint }}</div>
      </div>
      </template>

          <template v-if="params.timeshift">
            <div class="col-12 flex align-items-center gap-2">
              <label class="text-xs text-color-secondary" for="ts-format">Value format in URL</label>
              <Select v-model="time.format" input-id="ts-format" :options="formatOptions" option-label="label" option-value="value" />
            </div>
          </template>
        </div>

        <details class="text-sm">
          <summary class="cursor-pointer font-semibold">How it works</summary>
          <div class="flex flex-column gap-2 mt-2 text-color-secondary">
            <p class="m-0">
              These are ordinary channel manifest URLs with extra query parameters, so any player (or the CDN) can use
              them; nothing is stored per viewer. The start and end parameter names, and a prefix put in front of every
              parameter name, are set per channel in <code>[query]</code> (currently
              <code>{{ paramName(params, params.start_param) }}</code> and
              <code>{{ paramName(params, params.end_param) }}</code>); <code>{{ paramName(params, FULL_LOOPS_PARAM) }}</code>
              (boolean) and <code>{{ paramName(params, OFFSET_PARAM) }}</code> (seconds or ISO 8601 duration) have fixed
              names. A channel can switch this off (<code>[query] timeshift</code>) independently of the URL overrides.
            </p>
            <ul class="m-0 pl-4">
              <li>
                <strong>Values</strong>: epoch seconds, epoch milliseconds (anything ≥ 1e11) or ISO 8601
                (<code>2026-09-30T08:00:00Z</code>, an offset, or no zone = UTC).
              </li>
              <li>
                <strong>Catchup</strong> (start and a past end): a VOD playlist (<code>ENDLIST</code> / static MPD) over
                the range, cacheable forever.
              </li>
              <li>
                <strong>Startover</strong> (start, end in the future or absent): plays from the start point at normal
                speed; the manifest grows to the live edge and becomes a VOD once the end passes. With no end it stops
                after the maximum span.
              </li>
              <li>
                <strong>Limits</strong>: start no earlier than the channel epoch and not in the future; range at most
                the channel's maximum span. Violations return HTTP 400 with a message.
              </li>
              <li>
                <strong>Precision</strong>: start and end snap to segment boundaries (no re-muxing). SCTE-35 markers
                inside the range are carried through.
              </li>
              <li>
                <strong>Discontinuities</strong>: unless the timeline is continuous, each loop wrap inside the range is an
                #EXT-X-DISCONTINUITY / DASH Period, so a long range over a short loop has many.
              </li>
              <li>
                <strong>Caveat</strong>: history is re-derived from the loop and epoch, not recorded. Restarting the
                channel with a new epoch, or re-baking, silently changes what past times contain.
              </li>
              <li>The playhead time shown for DASH in a preview is approximate for time-shifted streams.</li>
            </ul>
          </div>
        </details>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ts-toggle-row {
  cursor: pointer;
}
.ts-toggle {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  color: inherit;
}
.qp {
  font-size: 0.75rem;
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
  color: var(--p-text-color, #111827);
}
</style>
