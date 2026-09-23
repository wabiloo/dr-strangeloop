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
      <div class="app-header-inner">
        <RouterLink to="/channels" class="app-brand">
          <i class="pi pi-sync" />
          <span>Dr. Strangeloop</span>
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
  background: #0f172a;
  border-bottom: 1px solid #1e293b;
}

.app-header-inner {
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
  gap: 0.5rem;
  color: #f8fafc;
  text-decoration: none;
  font-weight: 700;
  font-size: 1.05rem;
  white-space: nowrap;
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
  background: var(--p-primary-color, #0e7490);
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
