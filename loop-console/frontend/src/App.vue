<script setup lang="ts">
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import Toast from 'primevue/toast'
import { RouterLink, RouterView, useRouter } from 'vue-router'

const router = useRouter()
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
          <RouterLink to="/channels" class="app-nav-link" active-class="app-nav-link-active">
            Channels
          </RouterLink>
          <RouterLink to="/configs" class="app-nav-link" active-class="app-nav-link-active">
            Content configs
          </RouterLink>
        </nav>

        <div class="app-header-actions">
          <Button label="New channel" icon="pi pi-plus" size="small" @click="router.push('/channels/new')" />
        </div>
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
  align-items: center;
  gap: 0.25rem;
  flex: 1;
}

.app-nav-link {
  color: #94a3b8;
  text-decoration: none;
  font-weight: 500;
  font-size: 0.9rem;
  padding: 0.5rem 0.9rem;
  border-radius: 6px;
  transition:
    background-color 0.12s ease,
    color 0.12s ease;
}

.app-nav-link:hover {
  color: #f8fafc;
  background: rgba(255, 255, 255, 0.08);
}

.app-nav-link-active {
  color: #f8fafc;
  background: rgba(14, 116, 144, 0.55);
}

.app-header-actions {
  display: flex;
  align-items: center;
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
