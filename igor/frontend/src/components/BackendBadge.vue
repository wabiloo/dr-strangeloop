<script setup lang="ts">
import { computed } from 'vue'
import type { ChannelListItem } from '../api/types'
import awsIcon from '../assets/backends/aws.svg'
import dockerIcon from '../assets/backends/docker.svg'

type Backend = ChannelListItem['backend']

const props = defineProps<{ backend: Backend; pill?: boolean }>()

const BACKEND_ICON: Record<Backend, string> = {
  'local-docker': dockerIcon,
  'ecs-express': awsIcon,
  'aws-media': awsIcon,
}

const BACKEND_COLOR: Record<Backend, string> = {
  'local-docker': '#2496ed',
  'ecs-express': '#ed7100',
  'aws-media': '#8c4fff',
}

// Quoted: bundled SVGs may be inlined as data URIs containing ' and ( ),
// which are invalid inside an unquoted CSS url().
const badgeStyle = computed(() => ({
  '--icon-url': `url("${BACKEND_ICON[props.backend]}")`,
  '--backend-color': BACKEND_COLOR[props.backend],
}))
</script>

<template>
  <div class="inline-flex align-items-center gap-2 backend-badge" :class="{ pill }" :style="badgeStyle">
    <span class="backend-icon" aria-hidden="true" />
    <span>{{ backend }}</span>
  </div>
</template>

<style scoped>
.backend-badge {
  white-space: nowrap;
}

.backend-badge.pill {
  padding: 0.2rem 0.75rem 0.2rem 0.5rem;
  border: 1px solid var(--backend-color);
  border-radius: 999px;
}

.backend-icon {
  display: inline-block;
  width: 1.5rem;
  height: 1.5rem;
  background-color: var(--backend-color);
  mask: var(--icon-url) center / contain no-repeat;
  -webkit-mask: var(--icon-url) center / contain no-repeat;
}

.pill .backend-icon {
  width: 1.25rem;
  height: 1.25rem;
}
</style>
