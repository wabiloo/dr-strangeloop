<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import { computed, ref } from 'vue'
import type { UrlRequestForm } from '../composables/useUrlRequestForm'
import { QP_SPECS, type QpSpec } from '../utils/queryParams'
import { paramName, type TimeshiftParams } from '../utils/timeshift'
import FieldHelp from './FieldHelp.vue'

// The per-request overrides of the channel's rendering settings, as query parameters on the
// manifest URLs. The state is shared with the Timeshift panel (both build the same URLs):
// see useUrlRequestForm.
const props = defineProps<{
  params: TimeshiftParams
  form: UrlRequestForm
  /** The channel's own value for each override, by parameter name (display text), shown
   * as "channel: X" so it is clear what leaving a field empty gives. */
  channelDefaults?: Record<string, string>
}>()
const { overrides, added, clearOverrides } = props.form

const STORAGE_KEY = 'igor.urlOverridesPanel.open'
function readOpen(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
const open = ref(readOpen())
function onHeaderClick(e: MouseEvent) {
  const hit = (e.target as HTMLElement).closest('button, a, input')
  if (hit && !hit.classList.contains('ts-toggle')) return
  open.value = !open.value
  try {
    localStorage.setItem(STORAGE_KEY, open.value ? '1' : '0')
  } catch {
    /* non-persistent is fine */
  }
}

// Opt-in so the form stays short: an "Add override" picker (grouped like the rows: both
// formats, HLS only, DASH only) adds one. Each added override is a row with its own remove
// button; removing clears its value. Rows keep the grouping under subheadings.
const OVERRIDE_GROUPS: { id: string; title: string; note: string; match: (spec: QpSpec) => boolean }[] = [
  { id: 'common', title: 'HLS and DASH', note: 'Read by both manifests.', match: (spec) => spec.formats.length === 2 },
  { id: 'hls', title: 'HLS', note: 'Only on the HLS URL.', match: (spec) => spec.formats.length === 1 && spec.formats[0] === 'hls' },
  { id: 'dash', title: 'DASH', note: 'Only on the DASH URL.', match: (spec) => spec.formats.length === 1 && spec.formats[0] === 'dash' },
]
const overrideGroups = computed(() =>
  OVERRIDE_GROUPS.map((g) => ({
    ...g,
    specs: QP_SPECS.filter((spec) => g.match(spec) && added.value.includes(spec.name)),
  })).filter((g) => g.specs.length),
)
// grouped options for the picker: only what is not added yet
const addOptions = computed(() =>
  OVERRIDE_GROUPS.map((g) => ({
    label: g.title,
    items: QP_SPECS.filter((spec) => g.match(spec) && !added.value.includes(spec.name)).map((spec) => ({
      label: spec.label,
      value: spec.name,
    })),
  })).filter((g) => g.items.length),
)
const pickerValue = ref<string | null>(null)
function addOverride(name: string | null) {
  if (name && !added.value.includes(name)) added.value.push(name)
  pickerValue.value = null // back to the "Add override…" placeholder
}
function removeOverride(name: string) {
  overrides[name] = ''
  added.value = added.value.filter((n) => n !== name)
}
function defaultOptionLabel(spec: QpSpec): string {
  const d = props.channelDefaults?.[spec.name]
  return d ? `default (channel: ${d})` : 'default (channel setting)'
}
function selectOptions(spec: QpSpec) {
  return [{ label: defaultOptionLabel(spec), value: '' }, ...(spec.options ?? [])]
}
</script>

<template>
  <section class="timeshift-panel surface-card border-round p-3 flex flex-column gap-3">
    <div class="ts-toggle-row flex align-items-center gap-2 flex-wrap" @click="onHeaderClick">
      <button type="button" class="ts-toggle flex align-items-center gap-2" :aria-expanded="open" aria-controls="uo-body">
        <i :class="['pi', open ? 'pi-chevron-down' : 'pi-chevron-right']" aria-hidden="true" />
        <h3 class="m-0 text-base">URL overrides</h3>
      </button>
      <FieldHelp label="URL overrides">
        Each override replaces a channel setting for the request that carries it; the manifests change, the segment
        bytes never do. Nothing is shown until you add an override; an added one with no value keeps the channel's
        own. Rows under the <em>HLS</em> or <em>DASH</em> subheading are only put on that format's URL. Anything the
        server cannot honour (for example <code>cue_tags=only</code> with markers that are not plain splice_insert)
        comes back as HTTP 400 in the player. Which parameters exist, and their names, is a channel setting:
        <code>[query] overrides</code>. The URLs built here also carry whatever is set in the Timeshift panel.
      </FieldHelp>
      <span v-if="!open" class="text-sm text-color-secondary">Re-render the manifests with other settings, per request</span>
    </div>

    <div v-show="open" id="uo-body">
      <Message v-if="!params.overrides" severity="secondary" :closable="false">
        Per-request overrides are switched off for this channel (<code>[query] overrides</code>): the channel's own
        settings always apply.
      </Message>
      <div v-else class="flex flex-column gap-2">
        <div class="flex align-items-center gap-2">
          <Select
            v-if="addOptions.length"
            v-model="pickerValue"
            input-id="ts-add-override"
            :options="addOptions"
            option-group-label="label"
            option-group-children="items"
            option-label="label"
            option-value="value"
            placeholder="Add override…"
            size="small"
            data-testid="ts-add-override"
            @update:model-value="addOverride"
          />
          <Button
            v-if="added.length"
            label="Remove all"
            icon="pi pi-times"
            size="small"
            severity="secondary"
            text
            @click="clearOverrides"
          />
        </div>
        <div v-if="!added.length" class="text-xs text-color-secondary">
          None added: requests use the channel's own settings. Add one to change how the manifests are rendered.
        </div>
        <div v-for="group in overrideGroups" :key="group.id" class="flex flex-column gap-2" :data-testid="`ts-overrides-${group.id}`">
          <div class="flex align-items-baseline gap-2">
            <h5 class="m-0 text-sm">{{ group.title }}</h5>
            <span class="text-xs text-color-secondary">{{ group.note }}</span>
          </div>
          <div class="grid">
            <div v-for="spec in group.specs" :key="spec.name" class="col-12 md:col-6 lg:col-4 flex flex-column gap-1" :data-testid="`ts-override-${spec.name}`">
              <label class="text-xs text-color-secondary ts-label" :for="`ts-ov-${spec.name}`">
                {{ spec.label }} <code class="qp">{{ paramName(params, spec.name) }}</code>
                <FieldHelp :label="spec.label">{{ spec.help }}</FieldHelp>
                <Button
                  icon="pi pi-times"
                  size="small"
                  severity="secondary"
                  text
                  rounded
                  class="ml-auto"
                  :aria-label="`Remove ${spec.label}`"
                  title="Remove this override"
                  @click="removeOverride(spec.name)"
                />
              </label>
              <Select
                v-if="spec.kind === 'choice' || spec.kind === 'bool'"
                v-model="overrides[spec.name]"
                :input-id="`ts-ov-${spec.name}`"
                :options="selectOptions(spec)"
                :placeholder="defaultOptionLabel(spec)"
                option-label="label"
                option-value="value"
                fluid
              />
              <template v-else>
                <InputText
                  :id="`ts-ov-${spec.name}`"
                  v-model="overrides[spec.name]"
                  :placeholder="spec.placeholder"
                  fluid
                />
                <span v-if="channelDefaults?.[spec.name]" class="text-xs text-color-secondary">
                  channel: {{ channelDefaults[spec.name] }}
                </span>
              </template>
            </div>
          </div>
        </div>
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
.ts-label {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  min-height: 2rem;
}
.qp {
  font-size: 0.75rem;
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
  color: var(--p-text-color, #111827);
}
</style>
