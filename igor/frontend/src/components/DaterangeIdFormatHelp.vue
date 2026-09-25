<script setup lang="ts">
import Button from 'primevue/button'
import Popover from 'primevue/popover'
import { ref } from 'vue'

const popover = ref<InstanceType<typeof Popover> | null>(null)

const placeholders = [
  { token: '{loop}', description: 'Zero-based loop iteration number.', example: '3' },
  { token: '{eventid}', description: 'Emitted SCTE-35 event ID in decimal. Reflects increment_event_ids when enabled.', example: '100' },
  { token: '{segid}', description: 'Segmentation type ID in decimal. Bare splice_insert markers use command type 5.', example: '34' },
  { token: '{seghex}', description: 'Segmentation type ID in hexadecimal. Bare splice_insert markers use 0x05.', example: '0x22' },
  { token: '{segcode}', description: 'Segmentation code plus s (start) or e (end). Uses SPI for splice_insert. Grouped mode uses its representative marker.', example: 'BRKs / BRKe' },
  { token: '{segname}', description: 'Full segmentation type name, lowercase and hyphen-separated, followed by start or end. Paired end types use their corresponding start name.', example: 'provider-advertisement-start' },
  { token: '{epoch}', description: 'Marker program date-time as Unix epoch milliseconds.', example: '1780000000123' },
  { token: '{pd}', description: 'Marker program date-time in UTC ISO-8601, with millisecond precision.', example: '2026-05-28T14:13:20.123Z' },
]

function toggle(event: Event) {
  popover.value?.toggle(event)
}
</script>

<template>
  <Button
    type="button"
    icon="pi pi-question-circle"
    aria-label="DATERANGE ID format help"
    title="Show placeholder descriptions and examples"
    size="small"
    text
    rounded
    @click="toggle"
  />
  <Popover ref="popover">
    <div class="daterange-format-help">
      <div class="font-semibold mb-2">DATERANGE ID placeholders</div>
      <p class="text-sm text-color-secondary mt-0 mb-3">
        Combine placeholders with any static text. Example:
        <code>{segcode}-{eventid}-{loop}</code> produces <code>BRKs-100-3</code>.
        The default format is <code>{segcode}-{eventid}-{loop}</code>.
      </p>
      <div class="overflow-auto">
        <table class="help-table">
          <thead>
            <tr>
              <th>Placeholder</th>
              <th>Description</th>
              <th>Example</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in placeholders" :key="item.token">
              <td><code>{{ item.token }}</code></td>
              <td>{{ item.description }}</td>
              <td><code>{{ item.example }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="text-xs text-color-secondary mb-0 mt-2">
        Unknown placeholders are rejected. In grouped mode, the first start marker supplies the ID;
        groups containing only ends use the first end marker.
      </p>
    </div>
  </Popover>
</template>

<style scoped>
.daterange-format-help {
  width: min(48rem, calc(100vw - 3rem));
}

.help-table {
  border-collapse: collapse;
  font-size: 0.875rem;
  width: 100%;
}

.help-table th,
.help-table td {
  border-bottom: 1px solid var(--surface-border);
  padding: 0.5rem;
  text-align: left;
  vertical-align: top;
}

.help-table th {
  white-space: nowrap;
}
</style>
