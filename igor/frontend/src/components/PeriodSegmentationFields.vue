<script setup lang="ts">
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import { CONFIG_FIELD_LABEL as L } from '../utils/channelConfigLayout'
import { SEGMENTATION_PAIR_OPTIONS } from '../segmentationPresets'
import type { ChannelCreatePayload } from '../api/types'
import FieldHelp from './FieldHelp.vue'

// The "new Period / discontinuity on SCTE-35 segmentations" form group
// (loop-dee-loop/SCOPE.md §14), shared by the New channel form and the
// Configuration edit form. Edits `form` in place. Only Start types are
// offered: a Start implies its End.
defineProps<{ form: ChannelCreatePayload; idPrefix: string }>()

const typeOptions = SEGMENTATION_PAIR_OPTIONS.filter((option) => option.endValue)
const applyOptions = [
  { label: 'DASH Periods and HLS discontinuities', value: 'both' },
  { label: 'DASH Periods only', value: 'dash' },
  { label: 'HLS discontinuities only', value: 'hls' },
]
</script>

<template>
  <div class="grid">
    <div class="col-12 flex flex-column gap-1">
      <div class="flex align-items-center gap-2">
        <label :for="`${idPrefix}-period-types`">{{ L.period_on_segmentation }}</label>
        <FieldHelp label="New Period on SCTE-35 segmentations">
          Forces a new DASH Period and/or HLS #EXT-X-DISCONTINUITY at every marker of the chosen SCTE-35
          segmentation types, e.g. at each break or provider advertisement. Picking a Start type also covers
          its End. This only changes the signalling: with "Continuous timeline" on, the media timestamps stay
          continuous across the new Period. Leave empty for none. Restart or redeploy the channel to apply
          a change (loop-dee-loop/SCOPE.md &sect;14).
        </FieldHelp>
      </div>
      <MultiSelect
        :input-id="`${idPrefix}-period-types`"
        v-model="form.period_on_segmentation"
        :options="typeOptions"
        option-label="label"
        option-value="value"
        display="chip"
        placeholder="None"
        show-clear
        fluid
      />
    </div>
    <div v-if="form.period_on_segmentation?.length" class="col-12 flex flex-column gap-1">
      <label :for="`${idPrefix}-period-apply`">{{ L.period_on_segmentation_apply }}</label>
      <Select
        :input-id="`${idPrefix}-period-apply`"
        v-model="form.period_on_segmentation_apply"
        :options="applyOptions"
        option-label="label"
        option-value="value"
        fluid
      />
    </div>
  </div>
</template>
