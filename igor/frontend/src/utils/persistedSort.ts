import { ref, watch } from 'vue'

const PREFIX = 'igor.sort.'

interface StoredSort {
  field: string | null
  order: 1 | -1 | null
}

function load(key: string): StoredSort {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (typeof parsed?.field === 'string' && (parsed.order === 1 || parsed.order === -1)) {
        return { field: parsed.field, order: parsed.order }
      }
    }
  } catch {
    // storage unavailable or corrupt -- fall through to unsorted
  }
  return { field: null, order: null }
}

/** v-model:sortField / v-model:sortOrder pair for a DataTable, remembered per list in localStorage. */
export function usePersistedSort(key: string) {
  const initial = load(key)
  const sortField = ref<string | null>(initial.field)
  const sortOrder = ref<number | null>(initial.order)

  watch([sortField, sortOrder], ([field, order]) => {
    try {
      if (field && (order === 1 || order === -1)) {
        localStorage.setItem(PREFIX + key, JSON.stringify({ field, order }))
      } else {
        localStorage.removeItem(PREFIX + key)
      }
    } catch {
      // storage unavailable -- sorting still works for this session
    }
  })

  return { sortField, sortOrder }
}
