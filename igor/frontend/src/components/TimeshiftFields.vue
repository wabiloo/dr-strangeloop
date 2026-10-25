<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import { CONFIG_FIELD_LABEL as L } from '../utils/channelConfigLayout'
import type { ChannelCreatePayload } from '../api/types'
import { formatDuration } from '../utils/timeshift'
import FieldHelp from './FieldHelp.vue'

// The [query] and [timeshift] form groups (`part`), shared by the New channel
// form and the Configuration edit form on the channel detail page. Edits
// `form` in place.
const props = defineProps<{ form: ChannelCreatePayload; idPrefix: string; part: 'query' | 'timeshift' }>()

const MAX_RANGE_PRESETS = [
  { label: '15 min', seconds: 900 },
  { label: '30 min', seconds: 1800 },
  { label: '1 h', seconds: 3600 },
  { label: '2 h', seconds: 7200 },
  { label: '4 h', seconds: 14400 },
]
const maxRangeReadable = computed(() => {
  const s = props.form.timeshift_max_span_seconds
  return typeof s === 'number' && s >= 1 ? formatDuration(s) : ''
})
</script>

<template>
  <div class="grid">
    <template v-if="part === 'query'">
      <div class="col-12 flex align-items-center gap-2">
        <Checkbox v-model="form.timeshift_enabled" binary :input-id="`${idPrefix}-ts-enabled`" />
        <label :for="`${idPrefix}-ts-enabled`">{{ L.timeshift_enabled }}</label>
        <FieldHelp label="Startover and catchup (timeshift)">
          Lets the channel's normal manifest URLs take a start (and optional end) time as query parameters, for
          startover (live-style from a past point) and catchup (VOD of a past range), plus whole-loop snapping and a
          "pretend now" offset. Times may be epoch seconds, epoch milliseconds or ISO 8601. Off: those parameters are
          ignored and the channel is live only. History is re-derived from the loop and the channel epoch (nothing is
          recorded), so it is only correct while the epoch and baked content are unchanged. This is a channel
          setting, not a query parameter. See loop-dee-loop/SCOPE.md &sect;13.
        </FieldHelp>
      </div>
      <div class="col-12 flex align-items-center gap-2">
        <Checkbox v-model="form.query_overrides" binary :input-id="`${idPrefix}-q-overrides`" />
        <label :for="`${idPrefix}-q-overrides`">{{ L.query_overrides }}</label>
        <FieldHelp label="Per-request overrides">
          On (default): a request can change how this channel's manifests are rendered -- timeline mode, epoch,
          DVR window, forced Periods, DASH style, SCTE-35 signalling, ladder limits -- with query parameters. Off:
          those parameters are ignored (not rejected) and the channel's own settings always apply. Independent of
          startover/catchup. Use it for a channel that a viewer or an upstream system must not be able to re-render.
          This is a channel setting, not a query parameter.
        </FieldHelp>
      </div>
      <template v-if="form.timeshift_enabled || form.query_overrides">
        <div class="col-12 flex flex-column gap-1">
          <div class="flex align-items-center gap-2">
            <label :for="`${idPrefix}-q-prefix`">{{ L.query_prefix }}</label>
            <FieldHelp label="Parameter prefix">
              Put in front of every query parameter name, e.g. <code>dsl_</code> gives <code>dsl_start</code>,
              <code>dsl_cue_tags</code>, ... Use it when another system that takes this channel as its source
              (an SSAI service, say) uses some of the same names for its own parameters. Leave empty for none.
              The names are also what the CDN keys manifest caching on; redeploy to apply a change.
            </FieldHelp>
          </div>
          <InputText :id="`${idPrefix}-q-prefix`" v-model="form.query_prefix" placeholder="none" fluid />
        </div>
      </template>
      <template v-if="form.timeshift_enabled">
        <div class="col-12 md:col-6 flex flex-column gap-1">
          <label :for="`${idPrefix}-ts-start`">{{ L.timeshift_start_param }}</label>
          <InputText :id="`${idPrefix}-ts-start`" v-model="form.timeshift_start_param" fluid />
        </div>
        <div class="col-12 md:col-6 flex flex-column gap-1">
          <label :for="`${idPrefix}-ts-end`">{{ L.timeshift_end_param }}</label>
          <InputText :id="`${idPrefix}-ts-end`" v-model="form.timeshift_end_param" fluid />
        </div>
      </template>
    </template>
    <template v-else>
      <div class="col-12 flex flex-column gap-1">
        <label :for="`${idPrefix}-ts-span`">{{ L.timeshift_max_span_seconds }}</label>
        <div class="flex align-items-center gap-2">
          <InputNumber :input-id="`${idPrefix}-ts-span`" v-model="form.timeshift_max_span_seconds" :min="1" :use-grouping="false" :input-style="{ width: '10rem' }" />
          <span v-if="maxRangeReadable" class="text-color-secondary">= {{ maxRangeReadable }}</span>
        </div>
        <div class="flex flex-wrap align-items-center gap-2">
          <span class="text-xs text-color-secondary">Set to:</span>
          <div class="flex flex-nowrap align-items-center gap-2">
          <Button
            v-for="preset in MAX_RANGE_PRESETS"
            :key="preset.seconds"
            type="button"
            size="small"
            :label="preset.label"
            class="white-space-nowrap"
            severity="secondary"
            outlined
            @click="form.timeshift_max_span_seconds = preset.seconds"
          />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
