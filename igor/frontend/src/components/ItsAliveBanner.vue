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
        <svg class="its-alive-icon" viewBox="0 0 64 64" aria-hidden="true">
          <rect x="12" y="14" width="40" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="3" />
          <path d="M14 26 H50 V49 Q50 58 41 58 H23 Q14 58 14 49 Z" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round" />
          <circle cx="9" cy="39" r="4" fill="currentColor" />
          <circle cx="55" cy="39" r="4" fill="currentColor" />
          <path d="M20 30 L28 33.5 M44 30 L36 33.5" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
          <circle cx="24" cy="41" r="2.4" fill="currentColor" />
          <circle cx="40" cy="41" r="2.4" fill="currentColor" />
          <path d="M22 50 L26 47 L30 50 L34 47 L38 50 L42 47" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span class="its-alive-text">It's a Live!</span>
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
  gap: 1rem;
  padding: 1.1rem 2.2rem;
  border-radius: 999px;
  background: var(--p-content-background, #fff);
  border: 2px solid var(--p-primary-color, #0e7490);
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.22);
  color: var(--p-text-color, inherit);
  pointer-events: none;
  white-space: nowrap;
}

.its-alive-icon {
  width: 3rem;
  height: 3rem;
  flex-shrink: 0;
  color: var(--p-primary-color, #0e7490);
  animation: its-alive-flicker 1.3s ease-in-out infinite;
}

.its-alive-text {
  font-weight: 700;
  font-size: 1.75rem;
  letter-spacing: 0.02em;
}

@keyframes its-alive-flicker {
  0%,
  100% {
    filter: drop-shadow(0 0 0 transparent);
  }
  12% {
    filter: drop-shadow(0 0 7px var(--p-primary-color, #0e7490));
  }
  24% {
    filter: drop-shadow(0 0 0 transparent);
  }
  34% {
    filter: drop-shadow(0 0 9px var(--p-primary-color, #0e7490));
  }
  46% {
    filter: drop-shadow(0 0 0 transparent);
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
