/** SCTE-35 Table 23 segmentation_type_id definitions and Table 23 pairings,
 * used by the editor and the playback marker labels. The reference data
 * itself (name/code/instant/Start-End pairing) comes from
 * `segmentationTable23.generated.ts` -- generated from the same
 * `scte35_table23` Python package franken-ts and loop-dee-loop use, via
 * `scripts/generate_scte35_tables.py`, so it can't silently disagree with
 * what franken-ts actually authors. Only this file's own presentation
 * concerns (display `label` wording, lane grouping/coloring) stay
 * hand-written here. */

import { TABLE23 } from './segmentationTable23.generated'

export interface PresetOption {
  value: string
  label: string
}

/** Pair choices plus standalone signals. Pair end IDs are explicit because
 * program pairings are not all consecutive (e.g. 0x17 Program Overlap Start
 * pairs with 0x11 Program End). The marker editor selects a Start or
 * standalone signal. There is no separate "lane"/"type" field any more --
 * timeline lanes are grouped directly by (splice_type, type_id), labeled
 * with `name` (the bare segmentation name, no "Start/End" wording) -- see
 * `laneKeyForMarker`/`laneLabelForMarker` below. */
export interface SegmentationPairOption {
  /** segmentation.type_id to store (the Start value, or the only value for
   * standalone/instant types). */
  value: string
  label: string
  /** Bare name (no "Start/End"/hex prefix), used as the timeline lane
   * label -- e.g. "Break", "Provider Placement Opportunity". */
  name: string
  /** Stable three-letter compact code used by franken-ts OSD span labels. */
  code: string
  /** Selected type has no outgoing Start -> End pair in this editor. */
  instant: boolean
  /** Table 23 End type ID for paired types. */
  endValue?: string
}

/** Display wording for each paired type's dropdown label, i.e. everything
 * after "0xNN/0xNN -- " (instant types just show their bare `name`, no
 * override needed). Hand-authored: whether the End side repeats the full
 * name ("Program Start / Program End") or abbreviates to just "Start /
 * End" ("Provider Advertisement Start / End") isn't a rule derivable from
 * the Table 23 data itself -- it's a per-entry style choice, and (for
 * 0x13/0x17/0x19) the Start and End sides are altogether different Table
 * 23 names (e.g. "Program Breakaway" / "Program Resumption"), not a
 * name+suffix pattern at all. */
const PAIRED_LABEL_WORDING: Record<string, string> = {
  '0x10': 'Program Start / Program End',
  '0x13': 'Program Breakaway / Program Resumption',
  '0x17': 'Program Overlap Start / Program End',
  '0x19': 'Program Join / Program End',
  '0x20': 'Chapter Start / Chapter End',
  '0x22': 'Break Start / Break End',
  '0x24': 'Opening Credit Start / End (deprecated)',
  '0x26': 'Closing Credit Start / End (deprecated)',
  '0x30': 'Provider Advertisement Start / End',
  '0x32': 'Distributor Advertisement Start / End',
  '0x34': 'Provider Placement Opportunity Start / End',
  '0x36': 'Distributor Placement Opportunity Start / End',
  '0x38': 'Provider Overlay Placement Opportunity Start / End',
  '0x3A': 'Distributor Overlay Placement Opportunity Start / End',
  '0x3C': 'Provider Promo Start / End',
  '0x3E': 'Distributor Promo Start / End',
  '0x40': 'Unscheduled Event Start / End',
  '0x42': 'Alternate Content Opportunity Start / End',
  '0x44': 'Provider Ad Block Start / End',
  '0x46': 'Distributor Ad Block Start / End',
  '0x50': 'Network Start / End',
}

export const SEGMENTATION_PAIR_OPTIONS: SegmentationPairOption[] = TABLE23.map((entry) => {
  const hex = entry.endValue ? `${entry.value}/${entry.endValue}` : entry.value
  const wording = entry.instant ? entry.name : PAIRED_LABEL_WORDING[entry.value]
  return {
    value: entry.value,
    label: `${hex} -- ${wording}`,
    name: entry.name,
    code: entry.code,
    instant: entry.instant,
    endValue: entry.endValue ?? undefined,
  }
})

/** Explicit Table 23 Start -> End map. */
export const SEGMENTATION_END_TYPE_ID: Record<string, string> = Object.fromEntries(
  SEGMENTATION_PAIR_OPTIONS.filter((option) => option.endValue).map((option) => [option.value, option.endValue!]),
)

/** Explicit human-readable name for each defined Table 23 ID. */
export const SEGMENTATION_TYPE_NAME_BY_ID: Record<string, string> = {
  '0x00': 'Not Indicated', '0x01': 'Content Identification', '0x02': 'Call Ad Server',
  '0x10': 'Program Start', '0x11': 'Program End', '0x12': 'Program Early Termination',
  '0x13': 'Program Breakaway', '0x14': 'Program Resumption', '0x15': 'Program Runover Planned',
  '0x16': 'Program Runover Unplanned', '0x17': 'Program Overlap Start',
  '0x18': 'Program Blackout Override', '0x19': 'Program Join', '0x1A': 'Program Immediate Resumption',
  '0x20': 'Chapter Start', '0x21': 'Chapter End', '0x22': 'Break Start', '0x23': 'Break End',
  '0x24': 'Opening Credit Start (deprecated)', '0x25': 'Opening Credit End (deprecated)',
  '0x26': 'Closing Credit Start (deprecated)', '0x27': 'Closing Credit End (deprecated)',
  '0x30': 'Provider Advertisement Start', '0x31': 'Provider Advertisement End',
  '0x32': 'Distributor Advertisement Start', '0x33': 'Distributor Advertisement End',
  '0x34': 'Provider Placement Opportunity Start', '0x35': 'Provider Placement Opportunity End',
  '0x36': 'Distributor Placement Opportunity Start', '0x37': 'Distributor Placement Opportunity End',
  '0x38': 'Provider Overlay Placement Opportunity Start', '0x39': 'Provider Overlay Placement Opportunity End',
  '0x3A': 'Distributor Overlay Placement Opportunity Start', '0x3B': 'Distributor Overlay Placement Opportunity End',
  '0x3C': 'Provider Promo Start', '0x3D': 'Provider Promo End',
  '0x3E': 'Distributor Promo Start', '0x3F': 'Distributor Promo End',
  '0x40': 'Unscheduled Event Start', '0x41': 'Unscheduled Event End',
  '0x42': 'Alternate Content Opportunity Start', '0x43': 'Alternate Content Opportunity End',
  '0x44': 'Provider Ad Block Start', '0x45': 'Provider Ad Block End',
  '0x46': 'Distributor Ad Block Start', '0x47': 'Distributor Ad Block End',
  '0x50': 'Network Start', '0x51': 'Network End',
}

/** SCTE-35 Table 23 segmentation_type_id values used as standalone signals
 * (mirrors franken_ts.config.INSTANT_SEGMENTATION_TYPE_IDS). */
export const INSTANT_SEGMENTATION_TYPE_IDS = new Set(
  SEGMENTATION_PAIR_OPTIONS.filter((o) => o.instant).map((o) => o.value),
)

function normalizeTypeId(typeId: string): string {
  return typeId.toUpperCase().replace('X', 'x')
}

export function isInstantTypeId(typeId: string): boolean {
  return INSTANT_SEGMENTATION_TYPE_IDS.has(normalizeTypeId(typeId))
}

/** Human name for a raw segmentation_type_id byte value from Table 23. */
export function nameForTypeIdByte(value: number): string {
  const asHex = `0x${value.toString(16).padStart(2, '0').toUpperCase()}`
  return SEGMENTATION_TYPE_NAME_BY_ID[asHex] ?? asHex
}

export function segmentationRoleForTypeIdValue(value: number): 'start' | 'end' | 'other' {
  const asHex = `0x${value.toString(16).padStart(2, '0').toUpperCase()}`
  if (INSTANT_SEGMENTATION_TYPE_IDS.has(asHex)) return 'other'
  if (Object.values(SEGMENTATION_END_TYPE_ID).includes(asHex)) return 'end'
  if (SEGMENTATION_END_TYPE_ID[asHex]) return 'start'
  if (SEGMENTATION_TYPE_NAME_BY_ID[asHex]) return 'start'
  return 'other'
}

/** Same instant/standalone check as isInstantTypeId(), but taking the raw
 * decoded byte value instead of a "0xNN" string -- for callers working
 * directly off wire-decoded bytes (e.g. a live player's SCTE-35 event). */
export function isInstantTypeIdValue(value: number): boolean {
  const asHex = `0x${value.toString(16).padStart(2, '0').toUpperCase()}`
  return INSTANT_SEGMENTATION_TYPE_IDS.has(asHex)
}

/** A minimal shape covering what's needed to key/label a timeline lane --
 * matches (a subset of) MarkerLike from markerLayout.ts. */
export interface LaneableMarker {
  splice_type?: string
  segmentation?: { type_id: string }
}

/** Groups markers into timeline lanes by (splice_type, segmentation.type_id)
 * -- e.g. every "Break" marker shares a lane, every "Provider Placement
 * Opportunity" marker shares a different lane, etc. `splice_insert` markers
 * (no type_id to group by) share one lane of their own. */
export function laneKeyForMarker(marker: LaneableMarker): string {
  if (marker.splice_type === 'time_signal' && marker.segmentation?.type_id) {
    return `time_signal:${normalizeTypeId(marker.segmentation.type_id)}`
  }
  return 'splice_insert'
}

/** Human label for a lane -- the bare segmentation type name (no
 * "Start/End" wording), or a generic label for splice_insert markers. */
export function laneLabelForMarker(marker: LaneableMarker): string {
  if (marker.splice_type === 'time_signal' && marker.segmentation?.type_id) {
    const normalized = normalizeTypeId(marker.segmentation.type_id)
    const pair = SEGMENTATION_PAIR_OPTIONS.find((o) => o.value === normalized)
    return pair?.name ?? normalized
  }
  return 'Ad Break (splice_insert)'
}

/** Deterministic color per lane key (same lane always gets the same color
 * everywhere it's shown -- the timeline lane bars and the marker editor's
 * "Timeline lane" badge -- without needing a fixed enum of known types,
 * since there can be as many lanes as distinct segmentation types used. */
const LANE_PALETTE = [
  '#dc2626', '#7c3aed', '#0891b2', '#d97706', '#059669',
  '#db2777', '#4f46e5', '#65a30d', '#0d9488', '#ea580c',
]
export function colorForLaneKey(key: string): string {
  let hash = 0
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0
  return LANE_PALETTE[hash % LANE_PALETTE.length]
}

/** Table 23: segmentation_type_id (individual values, kept for reference /
 * any lingering direct-value use -- the marker editor itself now uses
 * SEGMENTATION_PAIR_OPTIONS above). */
export const SEGMENTATION_TYPE_ID_OPTIONS: PresetOption[] = [
  { value: '0x00', label: '0x00 -- Not Indicated' },
  { value: '0x01', label: '0x01 -- Content Identification' },
  { value: '0x02', label: '0x02 -- Call Ad Server' },
  { value: '0x10', label: '0x10 -- Program Start' },
  { value: '0x11', label: '0x11 -- Program End' },
  { value: '0x12', label: '0x12 -- Program Early Termination' },
  { value: '0x13', label: '0x13 -- Program Breakaway' },
  { value: '0x14', label: '0x14 -- Program Resumption' },
  { value: '0x15', label: '0x15 -- Program Runover Planned' },
  { value: '0x16', label: '0x16 -- Program Runover Unplanned' },
  { value: '0x17', label: '0x17 -- Program Overlap Start' },
  { value: '0x18', label: '0x18 -- Program Blackout Override' },
  { value: '0x19', label: '0x19 -- Program Join' },
  { value: '0x1A', label: '0x1A -- Program Immediate Resumption' },
  { value: '0x20', label: '0x20 -- Chapter Start' },
  { value: '0x21', label: '0x21 -- Chapter End' },
  { value: '0x22', label: '0x22 -- Break Start' },
  { value: '0x23', label: '0x23 -- Break End' },
  { value: '0x24', label: '0x24 -- Opening Credit Start' },
  { value: '0x25', label: '0x25 -- Opening Credit End' },
  { value: '0x26', label: '0x26 -- Closing Credit Start' },
  { value: '0x27', label: '0x27 -- Closing Credit End' },
  { value: '0x30', label: '0x30 -- Provider Advertisement Start' },
  { value: '0x31', label: '0x31 -- Provider Advertisement End' },
  { value: '0x32', label: '0x32 -- Distributor Advertisement Start' },
  { value: '0x33', label: '0x33 -- Distributor Advertisement End' },
  { value: '0x34', label: '0x34 -- Provider Placement Opportunity Start' },
  { value: '0x35', label: '0x35 -- Provider Placement Opportunity End' },
  { value: '0x36', label: '0x36 -- Distributor Placement Opportunity Start' },
  { value: '0x37', label: '0x37 -- Distributor Placement Opportunity End' },
  { value: '0x38', label: '0x38 -- Provider Overlay Placement Opportunity Start' },
  { value: '0x39', label: '0x39 -- Provider Overlay Placement Opportunity End' },
  { value: '0x3A', label: '0x3A -- Distributor Overlay Placement Opportunity Start' },
  { value: '0x3B', label: '0x3B -- Distributor Overlay Placement Opportunity End' },
  { value: '0x3C', label: '0x3C -- Provider Promo Start' },
  { value: '0x3D', label: '0x3D -- Provider Promo End' },
  { value: '0x3E', label: '0x3E -- Distributor Promo Start' },
  { value: '0x3F', label: '0x3F -- Distributor Promo End' },
  { value: '0x40', label: '0x40 -- Unscheduled Event Start' },
  { value: '0x41', label: '0x41 -- Unscheduled Event End' },
  { value: '0x42', label: '0x42 -- Alternate Content Opportunity Start' },
  { value: '0x43', label: '0x43 -- Alternate Content Opportunity End' },
  { value: '0x44', label: '0x44 -- Provider Ad Block Start' },
  { value: '0x45', label: '0x45 -- Provider Ad Block End' },
  { value: '0x46', label: '0x46 -- Distributor Ad Block Start' },
  { value: '0x47', label: '0x47 -- Distributor Ad Block End' },
  { value: '0x50', label: '0x50 -- Network Start' },
  { value: '0x51', label: '0x51 -- Network End' },
]

/** Table 20: segmentation_upid_type */
export const UPID_TYPE_OPTIONS: PresetOption[] = [
  { value: '0x00', label: '0x00 -- Not Used' },
  { value: '0x01', label: '0x01 -- User Defined' },
  { value: '0x02', label: '0x02 -- ISCI' },
  { value: '0x03', label: '0x03 -- Ad-ID' },
  { value: '0x04', label: '0x04 -- UMID' },
  { value: '0x05', label: '0x05 -- ISAN (deprecated)' },
  { value: '0x06', label: '0x06 -- ISAN' },
  { value: '0x07', label: '0x07 -- TID' },
  { value: '0x08', label: '0x08 -- TI' },
  { value: '0x09', label: '0x09 -- ADI' },
  { value: '0x0A', label: '0x0A -- EIDR' },
  { value: '0x0B', label: '0x0B -- ATSC Content Identifier' },
  { value: '0x0C', label: '0x0C -- MPU()' },
  { value: '0x0D', label: '0x0D -- MID()' },
  { value: '0x0E', label: '0x0E -- ADS Information' },
  { value: '0x0F', label: '0x0F -- URI' },
]

/** UTF-8/ASCII text -> uppercase hex string (byte-pair encoded, no
 * separators) -- the shape franken-ts/threefive expect for upid_hex. */
export function textToHex(text: string): string {
  const bytes = new TextEncoder().encode(text)
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
    .toUpperCase()
}

/** Best-effort inverse of textToHex, tolerating spaces/0x/odd length in
 * the input (existing configs in this repo use various hex spellings). */
export function hexToText(hex: string): string {
  const clean = hex.replace(/0x/gi, '').replace(/\s+/g, '')
  const bytes: number[] = []
  for (let i = 0; i + 1 < clean.length; i += 2) {
    const byte = parseInt(clean.slice(i, i + 2), 16)
    if (Number.isNaN(byte)) return ''
    bytes.push(byte)
  }
  try {
    return new TextDecoder().decode(new Uint8Array(bytes))
  } catch {
    return ''
  }
}
