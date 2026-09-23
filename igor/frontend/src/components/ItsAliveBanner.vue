<script setup lang="ts">
// Fired once, briefly, the moment a channel actually transitions from
// not-running to running -- see ChannelDetail.vue's onJobFinished, which
// is the only caller of `trigger()`. Purely decorative (its-a-live's own
// namesake pun), so it fails silently/never blocks anything: no props, no
// events, no state the rest of the page depends on.
import { ref } from 'vue'

const visible = ref(false)
let hideTimer: ReturnType<typeof setTimeout> | null = null

function trigger() {
  if (hideTimer) clearTimeout(hideTimer)
  // Re-trigger the enter transition even if a previous banner is still
  // fading, instead of just extending the current one -- two starts close
  // together should each get their own flourish.
  visible.value = false
  requestAnimationFrame(() => {
    visible.value = true
    hideTimer = setTimeout(() => {
      visible.value = false
    }, 3500)
  })
}

defineExpose({ trigger })
</script>

<template>
  <Teleport to="body">
    <Transition name="its-alive-backdrop">
      <div v-if="visible" class="its-alive-backdrop" />
    </Transition>
    <Transition name="its-alive">
      <div v-if="visible" class="its-alive-banner" role="status" aria-live="polite">
        <img class="its-alive-image" src="/alive.png" alt="It's a Live!" />
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.its-alive-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1999;
  background: rgba(0, 0, 0, 0.4);
  /* Decorative only -- never block the page underneath. */
  pointer-events: none;
}

.its-alive-backdrop-enter-active,
.its-alive-backdrop-leave-active {
  transition: opacity 0.4s ease;
}
.its-alive-backdrop-enter-from,
.its-alive-backdrop-leave-to {
  opacity: 0;
}

.its-alive-banner {
  position: fixed;
  top: 14rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.its-alive-image {
  width: 24rem;
  max-width: 85vw;
  height: auto;
  display: block;
  filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22));
  animation: its-alive-flicker 1.3s ease-in-out infinite;
}

@keyframes its-alive-flicker {
  0%,
  100% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22)) drop-shadow(0 0 0 transparent);
  }
  12% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22))
      drop-shadow(0 0 16px var(--p-primary-color, #b91c1c));
  }
  24% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22)) drop-shadow(0 0 0 transparent);
  }
  34% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22))
      drop-shadow(0 0 20px var(--p-primary-color, #b91c1c));
  }
  46% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22)) drop-shadow(0 0 0 transparent);
  }
}

.its-alive-enter-active {
  transition:
    opacity 0.35s ease,
    transform 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.its-alive-leave-active {
  transition: opacity 0.6s ease;
}
.its-alive-enter-from {
  opacity: 0;
  transform: translateX(-50%) translateY(-10px) scale(0.92);
}
.its-alive-leave-to {
  opacity: 0;
}
</style>
