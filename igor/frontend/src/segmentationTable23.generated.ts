/**
 * GENERATED FILE -- DO NOT EDIT BY HAND.
 * Source of truth: scte35-table23/scte35_table23/__init__.py
 * Regenerate with: uv run python scripts/generate_scte35_tables.py
 *
 * One row per Table 23 Start/standalone segmentation_type_id --
 * segmentationPresets.ts builds SEGMENTATION_PAIR_OPTIONS (and its display
 * `label` strings) from this. */

export interface Table23Entry {
  value: string
  name: string
  code: string
  instant: boolean
  endValue: string | null
}

export const TABLE23: Table23Entry[] = [
  { value: '0x00', name: 'Not Indicated', code: 'NIN', instant: true, endValue: null },
  { value: '0x01', name: 'Content Identification', code: 'CID', instant: true, endValue: null },
  { value: '0x02', name: 'Call Ad Server', code: 'CAS', instant: true, endValue: null },
  { value: '0x10', name: 'Program', code: 'PRG', instant: false, endValue: '0x11' },
  { value: '0x12', name: 'Program Early Termination', code: 'PET', instant: true, endValue: null },
  { value: '0x13', name: 'Program Breakaway', code: 'PBA', instant: false, endValue: '0x14' },
  { value: '0x15', name: 'Program Runover Planned', code: 'PRP', instant: true, endValue: null },
  { value: '0x16', name: 'Program Runover Unplanned', code: 'PRU', instant: true, endValue: null },
  { value: '0x17', name: 'Program Overlap Start', code: 'POS', instant: false, endValue: '0x11' },
  { value: '0x18', name: 'Program Blackout Override', code: 'PBO', instant: true, endValue: null },
  { value: '0x19', name: 'Program Join', code: 'PJO', instant: false, endValue: '0x11' },
  { value: '0x1A', name: 'Program Immediate Resumption', code: 'PIR', instant: true, endValue: null },
  { value: '0x20', name: 'Chapter', code: 'CHP', instant: false, endValue: '0x21' },
  { value: '0x22', name: 'Break', code: 'BRK', instant: false, endValue: '0x23' },
  { value: '0x24', name: 'Opening Credit', code: 'OPN', instant: false, endValue: '0x25' },
  { value: '0x26', name: 'Closing Credit', code: 'CLC', instant: false, endValue: '0x27' },
  { value: '0x30', name: 'Provider Advertisement', code: 'PAD', instant: false, endValue: '0x31' },
  { value: '0x32', name: 'Distributor Advertisement', code: 'DAD', instant: false, endValue: '0x33' },
  { value: '0x34', name: 'Provider Placement Opportunity', code: 'PPO', instant: false, endValue: '0x35' },
  { value: '0x36', name: 'Distributor Placement Opportunity', code: 'DPO', instant: false, endValue: '0x37' },
  { value: '0x38', name: 'Provider Overlay Placement Opportunity', code: 'PVO', instant: false, endValue: '0x39' },
  { value: '0x3A', name: 'Distributor Overlay Placement Opportunity', code: 'DVO', instant: false, endValue: '0x3B' },
  { value: '0x3C', name: 'Provider Promo', code: 'PPR', instant: false, endValue: '0x3D' },
  { value: '0x3E', name: 'Distributor Promo', code: 'DPR', instant: false, endValue: '0x3F' },
  { value: '0x40', name: 'Unscheduled Event', code: 'USC', instant: false, endValue: '0x41' },
  { value: '0x42', name: 'Alternate Content Opportunity', code: 'ACO', instant: false, endValue: '0x43' },
  { value: '0x44', name: 'Provider Ad Block', code: 'PAB', instant: false, endValue: '0x45' },
  { value: '0x46', name: 'Distributor Ad Block', code: 'DAB', instant: false, endValue: '0x47' },
  { value: '0x50', name: 'Network', code: 'NET', instant: false, endValue: '0x51' },
]

/** Timeline lane colors -- port of scte35_table23.lane_key/lane_color. */
export const LANE_PALETTE = [
  '#dc2626',
  '#7c3aed',
  '#0891b2',
  '#d97706',
  '#059669',
  '#db2777',
  '#4f46e5',
  '#65a30d',
  '#0d9488',
  '#ea580c',
]

/** Lane key for a marker: `time_signal:0xNN` per segmentation type_id (any
 * case/`0X` spelling), or `splice_insert` for everything without one. */
export function laneKey(spliceType: string | undefined, typeId: string | undefined): string {
  if (spliceType === 'time_signal' && typeId) {
    return `time_signal:${typeId.toUpperCase().replace('X', 'x')}`
  }
  return 'splice_insert'
}

/** Deterministic color per lane key: `h = h*31 + ord(c)` as an unsigned 32-bit
 * hash into LANE_PALETTE. */
export function colorForLaneKey(key: string): string {
  let hash = 0
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0
  return LANE_PALETTE[hash % LANE_PALETTE.length]
}
