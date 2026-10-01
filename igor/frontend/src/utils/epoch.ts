// Channel epoch ([timeline].epoch_utc): loop 0's start and the DASH
// availabilityStartTime. serve.py's --epoch-utc only accepts the exact form
// YYYY-MM-DDTHH:MM:SSZ (UTC, whole seconds), so everything here produces it.
import { dateToInputValue, inputValueToDate } from './timeshift.ts'

export const DEFAULT_EPOCH_UTC = '2026-01-01T00:00:00Z'

export interface EpochPreset {
  id: 'unix' | 'y2026' | 'now'
  label: string
  /** The value to store (resolved at click time for "now"). */
  value: (now?: Date) => string
}

export const EPOCH_PRESETS: EpochPreset[] = [
  { id: 'unix', label: 'Unix epoch (1970)', value: () => '1970-01-01T00:00:00Z' },
  { id: 'y2026', label: '1 Jan 2026', value: () => DEFAULT_EPOCH_UTC },
  { id: 'now', label: 'Now', value: (now = new Date()) => formatEpochUtc(now) },
]

/** A Date as serve.py's epoch form: UTC, whole seconds (truncated). */
export function formatEpochUtc(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

export function isValidEpochUtc(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return false
  const d = new Date(value)
  // Rejects overflowing parts (month 13, Feb 30, ...): they must round-trip.
  return !Number.isNaN(d.getTime()) && formatEpochUtc(d) === value
}

/** The preset a stored value corresponds to, if any ("now" is never one:
 * it is just a timestamp once chosen). */
export function epochPresetFor(value: string): EpochPreset['id'] | null {
  if (value === '1970-01-01T00:00:00Z') return 'unix'
  if (value === DEFAULT_EPOCH_UTC) return 'y2026'
  return null
}

/** `<input type="datetime-local">` value (UTC) for a stored epoch. */
export function epochToInputValue(value: string): string {
  return isValidEpochUtc(value) ? dateToInputValue(new Date(value), true) : ''
}

/** Stored epoch for a UTC `<input type="datetime-local">` value, or null. */
export function inputValueToEpoch(value: string): string | null {
  const d = inputValueToDate(value, true)
  return d ? formatEpochUtc(d) : null
}
