// Shared state for the bottom Jobs drawer (components/JobDrawer.vue).
//
// Every job-launching page registers its job id here (via JobPanel.vue or
// trackJob()); this module is the single poller. On startup it also loads the
// server's job list, so output survives closing/reloading the page (the
// server keeps jobs in memory until it restarts).
import { computed, reactive, ref } from 'vue'
import { cancelJob, getJob, listJobs } from './api/client'
import type { Job } from './api/types'

export interface TrackedJob {
  job: Job
  etaHint?: string
  /** Started or finished while the user wasn't looking at it. */
  unseen: boolean
}

const LS_DISMISSED = 'igor.jobDrawer.dismissed'
const LS_HEIGHT = 'igor.jobDrawer.height'
const LS_OPEN = 'igor.jobDrawer.open'
const INITIAL_LOAD = 20
const POLL_MS = 1500

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}
function lsSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* private mode etc. -- the drawer just won't remember */
  }
}

const dismissed = new Set<string>(JSON.parse(lsGet(LS_DISMISSED) ?? '[]'))
const tracked = reactive<Record<string, TrackedJob>>({})
const activeId = ref<string | null>(null)
const open = ref(lsGet(LS_OPEN) === '1')
const height = ref(Math.min(Math.max(Number(lsGet(LS_HEIGHT)) || 320, 120), 900))
let timer: ReturnType<typeof setInterval> | null = null
let loaded = false

const isTerminal = (j: Job) => j.status === 'succeeded' || j.status === 'failed'

/** Tabs, newest job first. */
const tabs = computed(() =>
  Object.values(tracked).sort((a, b) => b.job.created_at - a.job.created_at),
)
const active = computed(() => (activeId.value ? tracked[activeId.value] ?? null : null))
const runningCount = computed(() => tabs.value.filter((t) => !isTerminal(t.job)).length)
const unseenCount = computed(() => tabs.value.filter((t) => t.unseen).length)

function persistDismissed() {
  lsSet(LS_DISMISSED, JSON.stringify([...dismissed].slice(-500)))
}

function ensurePolling() {
  if (timer) return
  timer = setInterval(pollAll, POLL_MS)
}

async function pollOne(t: TrackedJob) {
  try {
    const update = await getJob(t.job.id, t.job.log_length)
    const wasTerminal = isTerminal(t.job)
    t.job = { ...update, log: [...t.job.log, ...update.log] }
    if (!wasTerminal && isTerminal(t.job) && !(open.value && activeId.value === t.job.id)) t.unseen = true
  } catch {
    // 404 after a backend restart, or a transient error: leave as-is.
  }
}

async function pollAll() {
  const pending = Object.values(tracked).filter((t) => !isTerminal(t.job))
  if (!pending.length) {
    if (timer) clearInterval(timer)
    timer = null
    return
  }
  await Promise.all(pending.map(pollOne))
}

/** Registers a job in the drawer (idempotent). Doesn't open or focus it. */
export function trackJob(job: Job | { id: string }, opts: { etaHint?: string } = {}) {
  dismissed.delete(job.id)
  const existing = tracked[job.id]
  if (existing) {
    if (opts.etaHint !== undefined) existing.etaHint = opts.etaHint
    return
  }
  const stub: Job =
    'status' in job
      ? job
      : {
          id: job.id, type: '…', channel_name: null, command: [], status: 'queued', log: [],
          log_length: 0, return_code: null, created_at: Date.now() / 1000, started_at: null, finished_at: null,
        }
  const t: TrackedJob = { job: stub, etaHint: opts.etaHint, unseen: !(open.value && !activeId.value) }
  tracked[job.id] = t
  if (!activeId.value) activeId.value = job.id
  ensurePolling()
  void pollOne(t)
}

/** Loads the server's recent jobs once, so a fresh page load sees them. */
export async function loadServerJobs() {
  if (loaded) return
  loaded = true
  try {
    const jobs = await listJobs()
    for (const j of jobs.slice(0, INITIAL_LOAD).reverse()) {
      if (dismissed.has(j.id) || tracked[j.id]) continue
      tracked[j.id] = { job: j, unseen: false }
    }
    // Most recent running job, else most recent job.
    if (!activeId.value) activeId.value = (jobs.find((j) => !isTerminal(j)) ?? jobs.find((j) => tracked[j.id]))?.id ?? null
    if (Object.values(tracked).some((t) => !isTerminal(t.job))) ensurePolling()
  } catch {
    loaded = false
  }
}

export function selectJob(id: string) {
  activeId.value = id
  if (tracked[id]) tracked[id].unseen = false
}

export function setOpen(v: boolean) {
  open.value = v
  lsSet(LS_OPEN, v ? '1' : '0')
  if (v && active.value) active.value.unseen = false
}

export function setHeight(px: number) {
  height.value = Math.min(Math.max(Math.round(px), 120), Math.round(window.innerHeight * 0.85))
  lsSet(LS_HEIGHT, String(height.value))
}

export function closeTab(id: string) {
  const t = tracked[id]
  if (!t || !isTerminal(t.job)) return // running jobs can't be dismissed; cancel first
  dismissed.add(id)
  persistDismissed()
  const wasActive = activeId.value === id
  delete tracked[id]
  if (wasActive) activeId.value = tabs.value[0]?.job.id ?? null
}

export function clearFinished() {
  for (const t of [...tabs.value]) closeTab(t.job.id)
}

export async function cancelTrackedJob(id: string) {
  const t = tracked[id]
  if (!t) return
  const update = await cancelJob(id)
  t.job = { ...update, log: [...t.job.log, ...update.log] }
  ensurePolling()
}

export function useJobDrawer() {
  return { tracked, tabs, active, activeId, open, height, runningCount, unseenCount }
}
