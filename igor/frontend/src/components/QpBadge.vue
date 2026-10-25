<script setup lang="ts">
import Popover from 'primevue/popover'
import { computed, ref } from 'vue'
import { overrideForField, qpSpec } from '../utils/queryParams'
import { paramName } from '../utils/timeshift'

// A small marker next to a channel setting that a request can override with a
// query parameter. Clicking it shows the parameter's name (the channel's prefix
// already applied) and what it does. Renders nothing for a setting that is fixed
// at bake time (segment duration, HLS container, ...). The registry of what is
// overridable, and under which name, is utils/queryParams.ts.
//
// `form` is the New/Edit channel form (its prefix and overrides switch are read
// live, so the badge follows what is being typed); outside a form pass `prefix`
// and `active` (the channel's [query] prefix / overrides) directly.
const props = withDefaults(
  defineProps<{
    /** The channel form / API field name (e.g. `dvr_window_seconds`). */
    field: string
    form?: { query_prefix?: string; query_overrides?: boolean }
    prefix?: string
    /** `[query] overrides` of the channel: false = the parameter is currently ignored. */
    active?: boolean
  }>(),
  { form: undefined, prefix: undefined, active: undefined },
)

const info = computed(() => overrideForField(props.field))
const spec = computed(() => (info.value ? qpSpec(info.value.name) : null))
const effectivePrefix = computed(() => props.prefix ?? props.form?.query_prefix ?? '')
const isActive = computed(() => props.active ?? props.form?.query_overrides ?? true)
const wireName = computed(() => (info.value ? paramName({ prefix: effectivePrefix.value }, info.value.name) : ''))
const formatsText = computed(() => {
  const f = info.value?.formats ?? []
  return f.length === 2 ? 'HLS and DASH manifests' : f[0] === 'hls' ? 'HLS manifests only' : 'DASH manifests only'
})

const popover = ref<InstanceType<typeof Popover> | null>(null)
function toggle(event: Event) {
  popover.value?.toggle(event)
}
</script>

<template>
  <span v-if="info && spec" class="qp-badge-wrap">
    <button
      type="button"
      class="qp-badge"
      :class="{ 'qp-badge-off': !isActive }"
      :aria-label="`${spec.label}: can be overridden per request with the query parameter ${wireName}`"
      :title="`Overridable per request: ${wireName}`"
      data-testid="qp-badge"
      @click="toggle"
    >
      <span class="qp-amp" aria-hidden="true">&amp;</span>
    </button>
    <Popover ref="popover">
      <div class="qp-popover text-sm flex flex-column gap-2">
        <div><strong>Can be overridden per request</strong></div>
        <div>
          Query parameter <code class="qp-name">{{ wireName }}</code>
          <span class="text-color-secondary">({{ formatsText }})</span>
        </div>
        <div class="text-color-secondary">{{ spec.help }}</div>
        <div class="text-color-secondary">
          The value set here is the channel's default; a request that carries the parameter replaces it for that request only.
          <template v-if="effectivePrefix">The channel's prefix <code>{{ effectivePrefix }}</code> is part of the name.</template>
        </div>
        <div v-if="!isActive" class="qp-warn">
          Per-request overrides are switched off for this channel (<code>[query] overrides</code>), so this parameter is currently ignored.
        </div>
      </div>
    </Popover>
  </span>
</template>

<style scoped>
.qp-badge-wrap {
  display: inline-flex;
  vertical-align: middle;
}
.qp-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-left: 0.3rem;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--p-text-muted-color, #6b7280);
  cursor: pointer;
}
/* an ampersand in a circle: neutral, so it does not compete with the field itself */
.qp-amp {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.2rem;
  height: 1.2rem;
  border: 1px solid currentColor;
  border-radius: 50%;
  font-size: 0.82rem;
  font-weight: 600;
  line-height: 1;
}
.qp-badge:hover,
.qp-badge:focus-visible {
  color: var(--p-text-color, #111827);
}
.qp-badge:focus-visible .qp-amp {
  outline: 2px solid var(--p-primary-color, #c0392b);
  outline-offset: 1px;
}
/* overrides switched off for the channel: the same mark, dashed and faded */
.qp-badge-off {
  opacity: 0.55;
}
.qp-badge-off .qp-amp {
  border-style: dashed;
}
.qp-popover {
  width: min(26rem, calc(100vw - 3rem));
}
.qp-name {
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
  background: var(--p-surface-200, #e5e7eb);
  color: var(--p-text-color, #111827);
}
.qp-warn {
  color: var(--p-orange-600, #c2410c);
}
</style>
