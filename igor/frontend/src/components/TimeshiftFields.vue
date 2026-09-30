<script setup lang="ts">
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import { CONFIG_FIELD_LABEL as L } from '../utils/channelConfigLayout'
import type { ChannelCreatePayload } from '../api/types'
import FieldHelp from './FieldHelp.vue'

// The [timeshift] form group, shared by the New channel form and the
// Configuration edit form on the channel detail page. Edits `form` in place.
defineProps<{ form: ChannelCreatePayload; idPrefix: string }>()
</script>

<template>
  <div class="grid">
    <div class="col-12 flex align-items-center gap-2">
      <Checkbox v-model="form.timeshift_enabled" binary :input-id="`${idPrefix}-ts-enabled`" />
      <label :for="`${idPrefix}-ts-enabled`">{{ L.timeshift_enabled }}</label>
      <FieldHelp label="Startover and catchup">
        Lets the channel's normal manifest URLs take a start (and optional end) time as query parameters, for
        startover (live-style from a past point) and catchup (VOD of a past range). Times may be epoch seconds,
        epoch milliseconds or ISO 8601. The parameter names below are configurable; they are also the only query
        parameters the CDN keys manifest caching on, so every distinct value gets its own cached manifest. History
        is re-derived from the loop and the channel epoch (nothing is recorded), so it is only correct while the
        epoch and baked content are unchanged. Redeploy to apply a change to the CDN cache key. See
        loop-dee-loop/SCOPE.md &sect;13.
      </FieldHelp>
    </div>
    <template v-if="form.timeshift_enabled">
      <div class="col-12 md:col-6 flex flex-column gap-1">
        <label :for="`${idPrefix}-ts-start`">{{ L.timeshift_start_param }}</label>
        <InputText :id="`${idPrefix}-ts-start`" v-model="form.timeshift_start_param" fluid />
      </div>
      <div class="col-12 md:col-6 flex flex-column gap-1">
        <label :for="`${idPrefix}-ts-end`">{{ L.timeshift_end_param }}</label>
        <InputText :id="`${idPrefix}-ts-end`" v-model="form.timeshift_end_param" fluid />
      </div>
      <div class="col-12 md:col-6 flex flex-column gap-1">
        <label :for="`${idPrefix}-ts-span`">{{ L.timeshift_max_span_seconds }}</label>
        <InputNumber :input-id="`${idPrefix}-ts-span`" v-model="form.timeshift_max_span_seconds" :min="1" :use-grouping="false" fluid />
      </div>
    </template>
  </div>
</template>
