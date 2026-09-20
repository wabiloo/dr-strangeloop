/** SCTE-35 reference tables (ANSI/SCTE 35), used to populate dropdowns in
 * the ad-break editor instead of free-text hex entry. Not exhaustive of
 * every possible value -- covers the values actually seen in practice
 * (provider/distributor placement opportunities, program/chapter/break
 * boundaries) plus an escape hatch (free text still works since these
 * are Select options bound to a plain string field, not an enum). */

export interface PresetOption {
  value: string
  label: string
}

/** Table 22: segmentation_type_id */
export const SEGMENTATION_TYPE_ID_OPTIONS: PresetOption[] = [
  { value: '0x00', label: '0x00 -- Not Indicated' },
  { value: '0x01', label: '0x01 -- Content Identification' },
  { value: '0x10', label: '0x10 -- Program Start' },
  { value: '0x11', label: '0x11 -- Program End' },
  { value: '0x12', label: '0x12 -- Program Early Termination' },
  { value: '0x13', label: '0x13 -- Program Breakaway' },
  { value: '0x14', label: '0x14 -- Program Resumption' },
  { value: '0x15', label: '0x15 -- Program Runover Planned' },
  { value: '0x16', label: '0x16 -- Program Runover Unplanned' },
  { value: '0x17', label: '0x17 -- Program Overlap Start' },
  { value: '0x18', label: '0x18 -- Program Blackout Override' },
  { value: '0x19', label: '0x19 -- Program Start -- In Progress' },
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
