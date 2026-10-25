// Watches the manifests the page's players fetch (hls.js / dash.js use XHR or fetch) and
// keeps a short history in a ManifestStore. It only observes the responses the players
// already consumed: it never fetches or sends anything itself. The hook is installed while
// the owning component lives and removed when it unmounts.

import { getCurrentInstance, onBeforeUnmount, ref, shallowRef } from 'vue'
import { ManifestStore, looksLikeManifestUrl, type ManifestEntry } from '../utils/manifestCapture'

export function useManifestCapture(maxPerSlot = 10) {
  const store = new ManifestStore(maxPerSlot)
  // The store mutates in place: bump `version` so computed views re-read it.
  const version = ref(0)
  const entries = shallowRef<ManifestEntry[]>([])
  const paused = ref(false)
  /** Set when a poll lands: the id of the newest entry, so the viewer can follow it. */
  const lastAdded = ref<number | null>(null)

  function sync() {
    entries.value = [...store.entries]
    version.value++
  }
  function record(url: string, status: number, contentType: string, text: string) {
    if (paused.value) return
    const entry = store.add(url, status, contentType, text)
    if (!entry) return
    lastAdded.value = entry.id
    sync()
  }

  const originalFetch = window.fetch
  const originalOpen = XMLHttpRequest.prototype.open
  const originalSend = XMLHttpRequest.prototype.send

  window.fetch = async function (this: typeof window, ...args: Parameters<typeof fetch>) {
    const response = await originalFetch.apply(this, args)
    try {
      const request = args[0]
      const url = typeof request === 'string' ? request : request instanceof URL ? request.href : request.url
      const method = (args[1]?.method ?? (request instanceof Request ? request.method : 'GET')).toUpperCase()
      const type = response.headers.get('content-type') ?? ''
      if (method === 'GET' && looksLikeManifestUrl(url, type)) {
        response
          .clone()
          .text()
          .then((text) => record(response.url || url, response.status, type, text))
          .catch(() => {})
      }
    } catch {
      /* observing must never break the player's request */
    }
    return response
  }

  type Tracked = XMLHttpRequest & { __manifestUrl?: string; __manifestMethod?: string }
  XMLHttpRequest.prototype.open = function (this: Tracked, method: string, url: string | URL, ...rest: unknown[]) {
    this.__manifestMethod = method
    this.__manifestUrl = String(url)
    return (originalOpen as (...a: unknown[]) => void).call(this, method, url, ...rest)
  } as typeof XMLHttpRequest.prototype.open
  XMLHttpRequest.prototype.send = function (this: Tracked, body?: Document | XMLHttpRequestBodyInit | null) {
    const url = this.__manifestUrl
    if (url && this.__manifestMethod?.toUpperCase() === 'GET' && looksLikeManifestUrl(url)) {
      this.addEventListener('loadend', () => {
        try {
          const type = this.getResponseHeader('content-type') ?? ''
          let text: string | null = null
          if (this.responseType === '' || this.responseType === 'text') text = this.responseText
          else if (this.responseType === 'arraybuffer' && this.response) text = new TextDecoder().decode(this.response as ArrayBuffer)
          if (text) record(this.responseURL || url, this.status, type, text)
        } catch {
          /* ignore: observing only */
        }
      })
    }
    return originalSend.call(this, body)
  }

  function uninstall() {
    if (window.fetch !== originalFetch) window.fetch = originalFetch
    XMLHttpRequest.prototype.open = originalOpen
    XMLHttpRequest.prototype.send = originalSend
  }
  if (getCurrentInstance()) onBeforeUnmount(uninstall)

  return {
    store,
    entries,
    version,
    paused,
    lastAdded,
    togglePin(id: number) {
      store.togglePin(id)
      sync()
    },
    reset() {
      store.reset()
      sync()
    },
    clearUnpinned() {
      store.clearUnpinned()
      sync()
    },
    setMaxPerSlot(n: number) {
      store.setMaxPerSlot(n)
      sync()
    },
    setSkipUnchanged(v: boolean) {
      store.skipUnchanged = v
    },
    uninstall,
  }
}

export type ManifestCapture = ReturnType<typeof useManifestCapture>
