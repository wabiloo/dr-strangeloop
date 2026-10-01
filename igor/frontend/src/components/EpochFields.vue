<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import { CONFIG_FIELD_LABEL as L } from '../utils/channelConfigLayout'
import type { ChannelCreatePayload } from '../api/types'
import {
  EPOCH_PRESETS,
  epochPresetFor,
  epochToInputValue,
  inputValueToEpoch,
  isValidEpochUtc,
} from '../utils/epoch'
import FieldHelp from './FieldHelp.vue'

// The [timeline] epoch_utc field, shared by the New channel form and the
// Configuration edit form on the channel detail page. Edits `form` in place.
const props = defineProps<{ form: ChannelCreatePayload; idPrefix: string }>()

// A UTC datetime-local input over the stored ISO string. An incomplete or
// cleared input leaves the stored value alone rather than blanking it.
const inputValue = computed({
  get: () => epochToInputValue(props.form.epoch_utc ?? ''),
  set: (v: string) => {
    const epoch = inputValueToEpoch(v)
    if (epoch) props.form.epoch_utc = epoch
  },
})
const valid = computed(() => isValidEpochUtc(props.form.epoch_utc ?? ''))
const activePreset = computed(() => epochPresetFor(props.form.epoch_utc ?? ''))
</script>

<template>
  <div class="flex flex-column gap-1">
    <div class="flex align-items-center gap-2">
      <label :for="`${idPrefix}-epoch`">{{ L.epoch_utc }}</label>
      <FieldHelp label="Channel epoch">
        Loop 0 starts at the epoch, and it is the DASH availabilityStartTime. Loop numbers, media sequence
        numbers and Period ids count from it, so a recent epoch keeps them small (the Unix epoch gives loop
        numbers in the billions). It also decides where in the loop playback lands: change it by a whole
        number of loop durations to keep the position. It is applied on redeploy (and a local-docker
        refresh), not by a plain start. Changing it on a running channel restarts the numbering, and
        startover cannot reach back before it. &quot;Now&quot; fills in the current UTC time as a fixed
        value; it does not follow the clock.
      </FieldHelp>
    </div>
    <div class="flex flex-wrap align-items-center gap-2">
      <input
        :id="`${idPrefix}-epoch`"
        v-model="inputValue"
        type="datetime-local"
        step="1"
        class="p-inputtext"
        :class="{ 'p-invalid': !valid }"
      />
      <span class="text-xs text-color-secondary">UTC</span>
      <Button
        v-for="preset in EPOCH_PRESETS"
        :key="preset.id"
        type="button"
        size="small"
        :label="preset.label"
        :severity="activePreset === preset.id ? undefined : 'secondary'"
        :outlined="activePreset !== preset.id"
        @click="form.epoch_utc = preset.value()"
      />
    </div>
    <small v-if="valid" class="text-color-secondary">{{ form.epoch_utc }}</small>
    <small v-else class="p-error">Pick a date and time (UTC).</small>
  </div>
</template>
