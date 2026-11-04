<script setup lang="ts">
import InputNumber from 'primevue/inputnumber'
import MultiSelect from 'primevue/multiselect'
import { CONFIG_FIELD_LABEL as L, SPLICE_INSERT } from '../utils/channelConfigLayout'
import { SEGMENTATION_TYPE_ID_OPTIONS } from '../segmentationPresets'
import type { ChannelCreatePayload } from '../api/types'
import FieldHelp from './FieldHelp.vue'
import QpBadge from './QpBadge.vue'

// The "announce SCTE-35 markers ahead" form group ([markers] announce_ahead / announce_on), shared by the New
// channel form and the Configuration edit form. Edits `form` in place.
defineProps<{ form: ChannelCreatePayload; idPrefix: string }>()

const typeOptions = [
  { value: SPLICE_INSERT, label: 'splice_insert (plain ad-break markers)' },
  ...SEGMENTATION_TYPE_ID_OPTIONS,
]
</script>

<template>
  <div class="grid">
    <div class="col-12 flex flex-column gap-1">
      <div class="flex align-items-center gap-2">
        <label :for="`${idPrefix}-announce-ahead`">{{ L.announce_ahead }}</label><QpBadge field="announce_ahead" :form="form" />
        <FieldHelp label="Announce SCTE-35 markers ahead">
          Announces each marker at least this many seconds before the segment it falls in (rounded to whole
          segments, also across the loop wrap): the HLS #EXT-X-DATERANGE / DASH Event shows up earlier in the
          manifest and then stays until every segment it covers has left the window. In DASH, a marker announced
          in an earlier Period than the one it falls in is carried again by every later Period that still covers
          it. The marker's own time never changes and #EXT-X-CUE-OUT/-IN stay exact. Not applied to a closed
          catchup range. 0 turns it off (maximum 3600). Restart or redeploy the channel to apply a change.
        </FieldHelp>
      </div>
      <InputNumber
        v-model="form.announce_ahead"
        :input-id="`${idPrefix}-announce-ahead`"
        :min="0"
        :max="3600"
        :max-fraction-digits="3"
        suffix=" s"
        fluid
      />
    </div>
    <div class="col-12 flex flex-column gap-1">
      <div class="flex align-items-center gap-2">
        <label :for="`${idPrefix}-announce-on`">{{ L.announce_on }}</label><QpBadge field="announce_on" :form="form" />
        <FieldHelp label="Announce only these SCTE-35 markers">
          Limits the announcing above to markers of the chosen SCTE-35 segmentation types and/or plain
          splice_insert markers. Each type is matched on its own (a Start does not imply its End here). Leave
          empty to announce every marker.
        </FieldHelp>
      </div>
      <MultiSelect
        :input-id="`${idPrefix}-announce-on`"
        v-model="form.announce_on"
        :options="typeOptions"
        option-label="label"
        option-value="value"
        display="chip"
        placeholder="Every marker"
        show-clear
        fluid
      />
    </div>
  </div>
</template>
