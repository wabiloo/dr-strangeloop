<script setup lang="ts">
import Button from 'primevue/button'
import ConfirmDialog from 'primevue/confirmdialog'
import Menubar from 'primevue/menubar'
import Toast from 'primevue/toast'
import { RouterLink, RouterView, useRouter } from 'vue-router'

const router = useRouter()

const menuItems = [
  { label: 'Channels', command: () => router.push('/channels') },
  { label: 'Content configs', command: () => router.push('/configs') },
]
</script>

<template>
  <div>
    <Menubar :model="menuItems">
      <template #start>
        <RouterLink to="/channels" class="flex align-items-center gap-2 no-underline text-color font-bold mr-4">
          <i class="pi pi-sync" />
          <span>loop-console</span>
        </RouterLink>
      </template>
      <template #item="{ item }">
        <a class="p-menuitem-link" @click="item.command && item.command({} as never)">
          {{ item.label }}
        </a>
      </template>
      <template #end>
        <Button
          label="New channel"
          icon="pi pi-plus"
          size="small"
          @click="router.push('/channels/new')"
        />
      </template>
    </Menubar>

    <main class="p-4">
      <RouterView />
    </main>

    <Toast />
    <ConfirmDialog />
  </div>
</template>
