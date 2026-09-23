<script setup lang="ts">
import ConfirmDialog from 'primevue/confirmdialog'
import Toast from 'primevue/toast'
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

const route = useRoute()
const isPlaylistsActive = computed(() => route.path.startsWith('/playlists'))
const isChannelsActive = computed(() => route.path.startsWith('/channels'))
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <!-- Purely decorative: an original, repeating wavy-line pattern
           evoking a mad scientist's psychedelic backdrop, tiled across the
           full header width (not a scan/trace of any existing artwork). -->
      <svg class="app-header-waves" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <pattern
            id="app-header-wave-pattern"
            x="0"
            y="0"
            width="180"
            height="56"
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(-7)"
          >
            <g stroke="#b91c1c" stroke-width="2.2" fill="none" opacity="0.5">
              <path d="M0,5 Q45,-5 90,5 Q135,15 180,5" />
              <path d="M0,14 Q45,4 90,14 Q135,24 180,14" />
              <path d="M0,23 Q45,13 90,23 Q135,33 180,23" />
              <path d="M0,32 Q45,22 90,32 Q135,42 180,32" />
              <path d="M0,41 Q45,31 90,41 Q135,51 180,41" />
              <path d="M0,50 Q45,40 90,50 Q135,60 180,50" />
            </g>
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#app-header-wave-pattern)" />
      </svg>

      <div class="app-header-inner">
        <RouterLink to="/channels" class="app-brand">
          <img src="/brand-mark.png" alt="" class="app-brand-icon" />
          <span class="app-brand-text">Dr. Strangeloop</span>
        </RouterLink>

        <nav class="app-nav">
          <RouterLink to="/playlists" class="app-nav-link" :class="{ 'app-nav-link-active': isPlaylistsActive }">
            Playlists
          </RouterLink>
          <RouterLink to="/channels" class="app-nav-link" :class="{ 'app-nav-link-active': isChannelsActive }">
            Channels
          </RouterLink>
        </nav>
      </div>
    </header>

    <main class="app-main">
      <RouterView />
    </main>

    <Toast />
    <ConfirmDialog />
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.app-header {
  position: relative;
  background: #0f172a;
  border-bottom: 1px solid #1e293b;
  overflow: hidden;
}

.app-header-waves {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.app-header-inner {
  position: relative;
  z-index: 1;
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 1.5rem;
  height: 3.5rem;
  display: flex;
  align-items: center;
  gap: 2rem;
}

.app-brand {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  color: #f8fafc;
  text-decoration: none;
  white-space: nowrap;
}

.app-brand-icon {
  /* Matches .app-header-inner's fixed height directly (a percentage here
     wouldn't resolve -- .app-brand's own height is auto/content-sized) so
     the mark sits flush with the header's top and bottom edges. Shown at
     its full (wide) natural width, not cropped down -- and pulled left
     past .app-header-inner's own left padding so it isn't boxed in
     starting at the same edge as the nav text. */
  height: 3.5rem;
  width: auto;
  margin-left: -5rem;
  flex-shrink: 0;
  display: block;
}

.app-brand-text {
  font-family: 'Irish Grover', 'Georgia', serif;
  font-size: 1.9rem;
  letter-spacing: 0.02em;
}

.app-nav {
  display: flex;
  align-items: stretch;
  align-self: stretch;
  gap: 0.25rem;
  flex: 1;
}

.app-nav-link {
  display: flex;
  align-items: center;
  position: relative;
  color: #94a3b8;
  text-decoration: none;
  font-weight: 500;
  font-size: 0.9rem;
  padding: 0 0.9rem;
  transition: color 0.12s ease;
}

.app-nav-link:hover {
  color: #f8fafc;
}

.app-nav-link-active {
  color: #f8fafc;
}

.app-nav-link-active::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 4px;
  background: var(--p-primary-color, #b91c1c);
  z-index: 1;
}

.app-main {
  flex: 1;
  padding: 1.5rem;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  box-sizing: border-box;
}
</style>
