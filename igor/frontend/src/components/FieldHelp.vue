<script setup lang="ts">
import Button from 'primevue/button'
import Popover from 'primevue/popover'
import { ref } from 'vue'

defineProps<{ label: string }>()

const popover = ref<InstanceType<typeof Popover> | null>(null)

function toggle(event: Event) {
  popover.value?.toggle(event)
}
</script>

<template>
  <Button
    type="button"
    icon="pi pi-question-circle"
    :aria-label="`${label} help`"
    title="Show explanation"
    size="small"
    text
    rounded
    @click="toggle"
  />
  <Popover ref="popover">
    <div class="field-help text-sm">
      <slot />
    </div>
  </Popover>
</template>

<style scoped>
.field-help {
  width: min(30rem, calc(100vw - 3rem));
}
</style>
