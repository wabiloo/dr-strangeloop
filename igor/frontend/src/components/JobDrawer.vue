<script setup lang="ts">
import Button from 'primevue/button'
import Message from 'primevue/message'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  cancelTrackedJob, clearFinished, closeTab, loadServerJobs, selectJob, setHeight, setOpen, useJobDrawer,
} from '../jobDrawer'

const { tabs, active, activeId, open, height, runningCount, unseenCount } = useJobDrawer()

const BAR_PX = 40
const visible = computed(() => tabs.value.length > 0)
const drawerPx = computed(() => (visible.value ? BAR_PX + (open.value ? height.value : 0) : 0))

// Keep page content from hiding behind the drawer.
watch(drawerPx, (px) => document.documentElement.style.setProperty('--job-drawer-h', `${px}px`), { immediate: true })

// ── log auto-scroll (only when already near the bottom) ───────────────────
const logEl = ref<HTMLPreElement | null>(null)
const stick = ref(true)
function onScroll() {
  const el = logEl.value
  if (el) stick.value = el.scrollHeight - el.scrollTop - el.clientHeight <= 24
}
watch(
  () => [active.value?.job.log_length, activeId.value, open.value],
  async () => {
    await nextTick()
    if (logEl.value && stick.value) logEl.value.scrollTop = logEl.value.scrollHeight
  },
)
watch(activeId, () => { stick.value = true })

// ── elapsed clock ─────────────────────────────────────────────────────────
const now = ref(Date.now())
let clock: ReturnType<typeof setInterval> | null = null
const fmt = (s: number) => {
  s = Math.max(0, Math.round(s))
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}
const timing = computed(() => {
  const j = active.value?.job
  if (!j || j.started_at == null) return ''
  if (j.finished_at != null) return `${j.status === 'succeeded' ? 'took' : j.cancelled ? 'cancelled after' : 'failed after'} ${fmt(j.finished_at - j.started_at)}`
  return `elapsed ${fmt(now.value / 1000 - j.started_at)}`
})

// ── helpers ───────────────────────────────────────────────────────────────
const isDone = (s: string) => s === 'succeeded' || s === 'failed'
const label = (t: { job: { type: string; channel_name: string | null } }) =>
  t.job.channel_name ? `${t.job.type} · ${t.job.channel_name}` : t.job.type
function icon(t: { job: { status: string } }) {
  return t.job.status === 'succeeded' ? 'pi pi-check-circle jd-ok' : t.job.status === 'failed' ? 'pi pi-times-circle jd-bad' : 'pi pi-spin pi-spinner jd-run'
}
const logText = computed(() => active.value?.job.log.join('\n') ?? '')

const copied = ref(false)
async function copyLog() {
  try {
    await navigator.clipboard.writeText(logText.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch { /* clipboard unavailable (non-secure context) */ }
}
function downloadLog() {
  const j = active.value?.job
  if (!j) return
  const url = URL.createObjectURL(new Blob([logText.value + '\n'], { type: 'text/plain' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `${j.type}${j.channel_name ? '-' + j.channel_name : ''}-${j.id}.log`
  a.click()
  URL.revokeObjectURL(url)
}
async function cancel() {
  if (active.value) await cancelTrackedJob(active.value.job.id)
}

// ── resize by dragging the top edge ───────────────────────────────────────
let dragStartY = 0, dragStartH = 0
function onDrag(e: PointerEvent) { setHeight(dragStartH + (dragStartY - e.clientY)) }
function endDrag() {
  window.removeEventListener('pointermove', onDrag)
  window.removeEventListener('pointerup', endDrag)
  document.body.style.userSelect = ''
}
function startDrag(e: PointerEvent) {
  dragStartY = e.clientY
  dragStartH = height.value
  document.body.style.userSelect = 'none'
  window.addEventListener('pointermove', onDrag)
  window.addEventListener('pointerup', endDrag)
}

onMounted(() => {
  clock = setInterval(() => (now.value = Date.now()), 1000)
  void loadServerJobs()
})
onBeforeUnmount(() => {
  if (clock) clearInterval(clock)
  endDrag()
  document.documentElement.style.removeProperty('--job-drawer-h')
})

function onTabClick(id: string) {
  if (open.value && activeId.value === id) return
  selectJob(id)
  setOpen(true)
}
</script>

<template>
  <div v-if="visible" class="jd" :style="{ height: drawerPx + 'px' }">
    <div v-if="open" class="jd-resize" title="Drag to resize" @pointerdown.prevent="startDrag" />
    <div class="jd-bar">
      <button class="jd-toggle" :title="open ? 'Hide jobs' : 'Show jobs'" @click="setOpen(!open)">
        <i :class="open ? 'pi pi-chevron-down' : 'pi pi-chevron-up'" />
        <span>Jobs</span>
        <span v-if="runningCount" class="jd-pill jd-pill-run">{{ runningCount }} running</span>
        <span v-else-if="unseenCount && !open" class="jd-pill">{{ unseenCount }} new</span>
      </button>
      <div class="jd-tabs" role="tablist">
        <div
          v-for="t in tabs"
          :key="t.job.id"
          class="jd-tab"
          :class="{ 'jd-tab-active': t.job.id === activeId }"
          role="tab"
          :title="`${t.job.command.join(' ')}\n(job ${t.job.id})`"
          @click="onTabClick(t.job.id)"
        >
          <i :class="icon(t)" />
          <span class="jd-tab-label">{{ label(t) }}</span>
          <span v-if="t.unseen" class="jd-dot" />
          <button v-if="isDone(t.job.status)" class="jd-x" title="Dismiss" @click.stop="closeTab(t.job.id)">
            <i class="pi pi-times" />
          </button>
        </div>
      </div>
      <Button
        v-if="tabs.some((t) => isDone(t.job.status))"
        label="Clear finished" icon="pi pi-trash" size="small" text severity="secondary" @click="clearFinished"
      />
    </div>

    <div v-if="open && active" class="jd-body">
      <div class="jd-head">
        <span class="jd-title">{{ label(active) }}</span>
        <span class="jd-badge" :class="`jd-badge-${active.job.cancelled ? 'cancelled' : active.job.status}`">
          <i :class="icon(active)" />{{ active.job.cancelled ? 'cancelled' : active.job.status }}
        </span>
        <span v-if="timing" class="jd-meta"><i class="pi pi-clock" />{{ timing }}</span>
        <span class="jd-meta jd-id" title="Job id"><i class="pi pi-hashtag" />{{ active.job.id }}</span>
        <span class="jd-spacer" />
        <Button
          v-if="!isDone(active.job.status)" label="Cancel" icon="pi pi-stop-circle" size="small" severity="danger"
          outlined :disabled="active.job.cancelled" @click="cancel"
        />
        <Button :label="copied ? 'Copied' : 'Copy'" icon="pi pi-copy" size="small" text severity="secondary" @click="copyLog" />
        <Button label="Download" icon="pi pi-download" size="small" text severity="secondary" @click="downloadLog" />
      </div>
      <Message v-if="active.etaHint && !isDone(active.job.status)" severity="info" :closable="false" class="jd-msg">
        <i class="pi pi-clock mr-1" />Still running -- this is expected: {{ active.etaHint }}
      </Message>
      <pre ref="logEl" class="jd-log" @scroll="onScroll">{{ logText || '(no output yet)' }}</pre>
    </div>
  </div>
</template>

<style scoped>
.jd {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  /* Under PrimeVue's Toast (1100), above page content. */
  z-index: 900;
  display: flex;
  flex-direction: column;
  background: #0f172a;
  color: #e2e8f0;
  border-top: 1px solid #334155;
  box-shadow: 0 -4px 16px rgb(0 0 0 / 25%);
}
.jd-resize {
  position: absolute;
  top: -3px;
  left: 0;
  right: 0;
  height: 6px;
  cursor: ns-resize;
  z-index: 1;
}
.jd-resize:hover { background: #b91c1c; opacity: 0.6; }
.jd-bar {
  flex: none;
  height: 40px;
  display: flex;
  align-items: stretch;
  gap: 0.25rem;
  padding-right: 0.5rem;
  background: #111c33;
}
.jd-toggle {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0 0.9rem; background: none; border: 0; color: #f8fafc;
  font-weight: 600; cursor: pointer; white-space: nowrap;
}
.jd-pill { font-size: 0.7rem; font-weight: 500; padding: 0.1rem 0.5rem; border-radius: 999px; background: #334155; }
.jd-pill-run { background: #0369a1; }
.jd-tabs { flex: 1; display: flex; overflow-x: auto; scrollbar-width: thin; }
.jd-tab {
  display: flex; align-items: center; gap: 0.45rem; padding: 0 0.75rem; cursor: pointer;
  color: #94a3b8; white-space: nowrap; border-bottom: 2px solid transparent; font-size: 0.85rem;
}
.jd-tab:hover { color: #f8fafc; }
.jd-tab-active { color: #f8fafc; background: #0f172a; border-bottom-color: var(--p-primary-color, #b91c1c); }
.jd-tab-label { max-width: 16rem; overflow: hidden; text-overflow: ellipsis; }
.jd-ok { color: #4ade80; }
.jd-bad { color: #f87171; }
.jd-run { color: #38bdf8; }
.jd-dot { width: 7px; height: 7px; border-radius: 50%; background: #f59e0b; }
.jd-x { background: none; border: 0; color: inherit; cursor: pointer; padding: 0.15rem; border-radius: 4px; font-size: 0.7rem; }
.jd-x:hover { background: #334155; }
.jd-body { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 0.5rem 0.75rem 0.75rem; gap: 0.5rem; }
.jd-head { display: flex; align-items: center; gap: 1.1rem; flex-wrap: wrap; padding: 0.25rem 0; }
.jd-badge {
  display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.25rem 0.7rem;
  border-radius: 6px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
  background: #1e293b; color: #cbd5e1;
}
.jd-badge-succeeded { background: #14532d; color: #86efac; }
.jd-badge-failed { background: #7f1d1d; color: #fca5a5; }
.jd-badge-cancelled { background: #78350f; color: #fcd34d; }
.jd-badge-running, .jd-badge-queued { background: #0c4a6e; color: #7dd3fc; }
.jd-badge i { font-size: 0.8rem; color: inherit; }
.jd-meta { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; color: #94a3b8; }
.jd-meta i { font-size: 0.75rem; }
.jd-id { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; }
.jd-title { font-weight: 600; font-size: 1rem; }
.jd-muted { color: #94a3b8; }
.jd-spacer { flex: 1; }
.jd-msg { flex: none; }
.jd-log {
  flex: 1; min-height: 0; margin: 0; overflow: auto; white-space: pre-wrap;
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.8rem;
  background: #020617; padding: 0.6rem 0.75rem; border-radius: 6px;
}
</style>
