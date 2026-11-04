<script setup lang="ts">
import { computed, watch } from 'vue'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import {
  CONFIG_FIELD_LABEL as L,
  DEFAULT_CHUNK_DURATION,
  LOW_LATENCY_BACKENDS,
  MAX_TARGET_LATENCY,
  lowLatencyProblems,
} from '../utils/channelConfigLayout'
import type { ChannelCreatePayload } from '../api/types'
import FieldHelp from './FieldHelp.vue'
import QpBadge from './QpBadge.vue'

// The low-latency settings of the [packaging.dash] form group, shared by the New channel form and the
// Configuration edit form on the channel detail page. Edits `form` in place. Low latency holds responses open
// while chunks are released, so only ecs-express and local-docker offer it; a backend change to any other
// resets it to off rather than leaving a hidden setting the server would refuse.
// See loop-dee-loop/README.md "Low-latency DASH" and docs/low-latency.md.
const props = defineProps<{ form: ChannelCreatePayload; idPrefix: string }>()

const supported = computed(() => LOW_LATENCY_BACKENDS.includes(props.form.backend))
const on = computed(() => supported.value && !!props.form.low_latency && props.form.low_latency !== 'off')
const problems = computed(() => lowLatencyProblems(props.form))

watch(
  () => props.form.backend,
  () => {
    if (!supported.value) props.form.low_latency = 'off'
  },
)

const lowLatencyOptions = [
  { label: 'Off', value: 'off' },
  { label: 'On (every MPD advertises it)', value: 'on' },
  { label: 'Opt-in (only on ?low_latency=true)', value: 'optin' },
]
const utcTimingOptions = [
  { label: 'direct (the time in the MPD, served no-store)', value: 'direct' },
  { label: 'http (a polled /time endpoint)', value: 'http' },
  { label: 'none (the player’s own clock)', value: 'none' },
]
</script>

<template>
  <template v-if="supported">
    <div class="col-12 flex flex-column gap-1">
      <div class="flex align-items-center gap-2">
        <label :for="`${idPrefix}-ll`">{{ L.low_latency }}</label>
        <QpBadge field="low_latency" :form="form" />
        <FieldHelp label="Low-latency DASH">
          Chunked CMAF delivery: segments are baked as short chunks and the segment in progress is streamed, each chunk
          released when it would have existed on a live encoder, so players can sit about a second or two behind the
          live edge. <b>Opt-in</b> delivers low latency but an MPD only advertises it when asked
          (<code>?low_latency=true</code>, needs per-request overrides). Changing this changes how the loop is baked:
          run <b>spark</b> again afterwards. Not available on aws-lambda or aws-media. See docs/low-latency.md.
        </FieldHelp>
      </div>
      <Select
        :input-id="`${idPrefix}-ll`"
        v-model="form.low_latency"
        :options="lowLatencyOptions"
        option-label="label"
        option-value="value"
        fluid
      />
    </div>

    <template v-if="on">
      <div class="col-12 flex flex-column gap-1">
        <div class="flex align-items-center gap-2">
          <label :for="`${idPrefix}-ll-chunk`">{{ L.chunk_duration }}</label>
          <FieldHelp label="Chunk duration">
            How long each baked chunk is (about 0.3&ndash;1 s is typical), shorter than the segment duration. A
            bake-time setting, not a query parameter. Source GOP length still decides where segments can start.
          </FieldHelp>
        </div>
        <InputNumber
          :input-id="`${idPrefix}-ll-chunk`"
          v-model="form.chunk_duration"
          :min="0.05"
          :step="0.05"
          :max-fraction-digits="3"
          :placeholder="String(DEFAULT_CHUNK_DURATION)"
          fluid
        />
      </div>

      <div class="col-12 flex flex-column gap-1">
        <div class="flex align-items-center gap-2">
          <label :for="`${idPrefix}-ll-target`">{{ L.target_latency }}</label>
          <QpBadge field="target_latency" :form="form" />
          <FieldHelp label="Target latency">
            The latency the MPD asks players to hold. Empty = automatic (4 chunks, at least 1 s). At most
            {{ MAX_TARGET_LATENCY }} seconds.
          </FieldHelp>
        </div>
        <InputNumber
          :input-id="`${idPrefix}-ll-target`"
          v-model="form.target_latency"
          :min="0.1"
          :max="MAX_TARGET_LATENCY"
          :step="0.5"
          :max-fraction-digits="3"
          placeholder="automatic"
          fluid
        />
      </div>

      <div class="col-12 flex flex-column gap-1">
        <div class="flex align-items-center gap-2">
          <label :for="`${idPrefix}-ll-utc`">{{ L.utc_timing }}</label>
          <QpBadge field="utc_timing" :form="form" />
          <FieldHelp label="Clock sync (UTCTiming)">
            How the MPD lets a player sync its clock to the channel's. <b>direct</b> (default) writes the current time
            into the MPD and serves it <code>no-store</code>, so MPDs are never cached by a CDN but need no URL.
            <b>http</b> points players at a polled <code>/time</code>; its URL is set automatically at deploy time
            (local-docker: <code>http://localhost:&lt;port&gt;/time</code>; ecs-express: the CloudFront domain, via one
            extra deploy for a new channel). <b>none</b> leaves it to the player's own clock.
          </FieldHelp>
        </div>
        <Select
          :input-id="`${idPrefix}-ll-utc`"
          v-model="form.utc_timing"
          :options="utcTimingOptions"
          option-label="label"
          option-value="value"
          fluid
        />
      </div>

      <div v-if="form.utc_timing === 'http'" class="col-12 flex flex-column gap-1">
        <div class="flex align-items-center gap-2">
          <label :for="`${idPrefix}-ll-url`">{{ L.utc_timing_url }}</label>
          <FieldHelp label="UTCTiming URL">
            Optional absolute http(s) URL of the channel's <code>/time</code>, instead of the automatic one &mdash; for
            a tunnel or a custom domain. Configuration only: a query parameter must not choose viewers' clock source.
          </FieldHelp>
        </div>
        <InputText
          :id="`${idPrefix}-ll-url`"
          v-model="form.utc_timing_url"
          placeholder="automatic (set at deploy time)"
          fluid
        />
      </div>
    </template>

    <div v-if="problems.length" class="col-12">
      <small v-for="p in problems" :key="p" class="block text-red-500">{{ p }}</small>
    </div>
  </template>
</template>
