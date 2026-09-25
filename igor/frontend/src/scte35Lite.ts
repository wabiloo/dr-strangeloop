/** Thin wrapper around the `scte35` npm package (Comcast, Apache-2.0,
 * https://github.com/Comcast/scte35-js) for decoding the raw SCTE-35 bytes
 * that hls.js / dash.js already hand us with each marker event -- used by
 * PlaybackPanel.vue to label the "a marker just appeared" toast with the
 * segmentation type (or splice type, for splice_insert which carries no
 * segmentation descriptor at all). No hand-rolled binary parsing here;
 * the scte35 package does it all -- this module only turns the decoded
 * structure into display labels. The bundled decoder exposes only the older
 * PO sub-fields, so we also read optional trailing sub-field bytes for the
 * newer Ad/Promo/Ad Block Starts from each descriptor's own raw bytes.
 *
 * A single SCTE-35 message can describe several coincident events at once
 * (e.g. a Break start + a nested PPO start + an instant Call Ad Server,
 * all merged into one message by franken-ts, each with its own
 * segmentation descriptor). describeAllMarkers() below returns one label
 * per event found IN THE DECODED MESSAGE ITSELF -- it deliberately never
 * takes an externally-supplied event_id to filter by. The backend's
 * DATERANGE `ID` / DASH `<Event id>` attributes are that backend's own
 * signaling convention (see loop-dee-loop/scte35_signaling.py) for giving
 * each tag a wire-unique identity across playlist reloads/loop
 * iterations -- not something the frontend should have to know how to
 * parse just to recover an event_id that's already sitting, authoritative,
 * in the payload's own descriptors. (Relying on the ID's internal shape
 * here is exactly what broke this label before: the backend changed the
 * ID's format and this module silently started matching against the
 * wrong number.)
 *
 * Callers where the same payload bytes surface on more than one tag
 * (because the backend, by default, shares one raw message's bytes across
 * every DATERANGE/<Event> tag for its coincident events) should decode
 * once per unique payload and dedupe by bytes, rather than calling this
 * once per tag. */

import { SCTE35 } from 'scte35'
import type { ISpliceInfoSection, ISpliceInsertEvent } from 'scte35/build/ISCTE35'
import type { ISegmentationDescriptor, ISpliceDescriptor } from 'scte35/build/descriptors'
import { nameForTypeIdByte, segmentationRoleForTypeIdValue } from './segmentationPresets'

const decoder = new SCTE35()

/** 'start'/'end' -- one half of a Start/End pair (a segmentation
 * descriptor with an even/odd type_id, or a splice_insert with
 * outOfNetworkIndicator true/false). 'other' -- no Start/End pairing at
 * all: an instant/standalone segmentation type (e.g. Call Ad Server), a
 * splice_insert with no resolvable direction, or any non-segmentation,
 * non-splice_insert command (e.g. a bare Time Signal). Callers use this
 * to distinguish the three at a glance (icon/color), never `label`'s text
 * (which is presentation wording, not a stable value to match against). */
export type Scte35MarkerKind = 'start' | 'end' | 'other'

export interface Scte35MarkerLabel {
  /** The event's own identity from the message itself (its segmentation
   * descriptor's segmentationEventId, or a bare splice_insert's own
   * spliceEventId) -- null only if the command type carries neither
   * (e.g. splice_null). */
  eventId: number | null
  /** Human-readable label, e.g. "Break Start", "Call Ad Server", "Splice
   * Insert End". */
  label: string
  kind: Scte35MarkerKind
  /** Numbering decoded from this descriptor, not inferred from a playlist. */
  segmentNum?: number
  segmentsExpected?: number
  subSegmentNum?: number
  subSegmentsExpected?: number
}

export function bytesToHex(bytes: Uint8Array): string {
  let hex = ''
  for (let i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, '0')
  return hex
}

const SPLICE_COMMAND_TYPE_SPLICE_INSERT = 5
const SPLICE_DESCRIPTOR_TAG_SEGMENTATION = 2

export function spliceCommandTypeLabel(spliceCommandType: number): string {
  switch (spliceCommandType) {
    case 0:
      return 'Splice Null'
    case 4:
      return 'Splice Schedule'
    case 5:
      return 'Splice Insert'
    case 6:
      return 'Time Signal'
    case 7:
      return 'Bandwidth Reservation'
    case 255:
      return 'Private'
    default:
      return `Splice Command 0x${spliceCommandType.toString(16).padStart(2, '0').toUpperCase()}`
  }
}

/** Full, correctly-worded label (+ kind) for one segmentation descriptor:
 * the segmentation type name plus " Start"/" End" -- using proper
 * SCTE-35 terminology, NOT HLS's CUE-OUT/CUE-IN or DATERANGE
 * SCTE35-OUT/SCTE35-IN attribute naming, which is just how HLS happens
 * to signal the two halves of a pair on the wire and has nothing to do
 * with the segmentation type's own vocabulary. Instant/standalone types
 * (e.g. Call Ad Server) get NO suffix at all -- there is no Start/End
 * pairing for them, just one signal. */
function segmentationLabel(typeId: number): { label: string; kind: Scte35MarkerKind } {
  const name = nameForTypeIdByte(typeId)
  const kind = segmentationRoleForTypeIdValue(typeId)
  return { label: name, kind }
}

/** Splice Insert label wording distinguishes three cases -- see
 * franken-ts/franken_ts/config.py's `auto_return` field and scte35.py's
 * `_splice_insert_pair` for the encoder side of this:
 *   - 'end' (outOfNetworkIndicator false): an explicit cue-in, always
 *     paired with a preceding cue-out (auto_return=false encoder-side --
 *     an auto-return break never gets one of these).
 *   - 'start' with breakDuration.autoReturn true: a single self-contained
 *     message, no cue-in will ever follow.
 *   - 'start' otherwise (autoReturn false, or no break_duration signaled
 *     at all): a cue-out that expects an explicit cue-in later.
 *   - 'other' (outOfNetworkIndicator absent): no resolvable direction. */
function spliceInsertLabel(kind: Scte35MarkerKind, autoReturn: boolean | undefined): string {
  if (kind === 'other') return 'Splice Insert'
  if (kind === 'end') return 'Splice Insert (Cue-In)'
  return autoReturn === true ? 'Splice Insert (Auto-Return)' : 'Splice Insert (Cue-Out)'
}

/** The bundled scte35 decoder exposes sub-fields only for 0x34/0x36.
 * Newer SCTE-35 also permits them on Ad/Promo/Ad Block Starts. Read the
 * optional final two bytes from each descriptor's own length, rather than
 * guessing their presence from its type or manufacturing zero values. */
function descriptorSubNumbers(bytes: Uint8Array, descriptor: ISegmentationDescriptor): [number, number] | null {
  if (descriptor.segmentationEventCancelIndicator || descriptor.segmentationTypeId == null) return null
  if (![0x30, 0x32, 0x34, 0x36, 0x38, 0x3a, 0x3c, 0x3e, 0x44, 0x46].includes(descriptor.segmentationTypeId)) return null
  let offset = 6 + 4 + 1 + 1 // tag, length, CUEI, event ID, cancel and flags
  if (!descriptor.programSegmentationFlag) offset += 1 + (descriptor.componentCount ?? 0) * 6
  if (descriptor.segmentationDurationFlag) offset += 5
  offset += 2 + (descriptor.segmentationUpidLength ?? 0) + 3 // UPID type/length/data, type, segment pair
  return bytes.length === descriptor.descriptorLength + 2 && bytes.length - offset === 2
    ? [bytes[offset], bytes[offset + 1]] : null
}

export function markerToastLabel(marker: Scte35MarkerLabel): string {
  const numbering = marker.segmentNum == null || marker.segmentsExpected == null
    ? ''
    : ` (${marker.segmentNum}:${marker.segmentsExpected}${marker.subSegmentNum == null || marker.subSegmentsExpected == null
      ? '' : ` / ${marker.subSegmentNum}:${marker.subSegmentsExpected}`})`
  return `${marker.label}${numbering} · ${marker.eventId ?? '?'}`
}

/** Decode `bytes` (a full splice_info_section) into one label per event it
 * describes -- see module docstring for why this never takes an
 * externally-supplied event_id. Returns [] if the bytes don't parse as
 * SCTE-35 at all. */
export function describeAllMarkers(bytes: Uint8Array): Scte35MarkerLabel[] {
  let section: ISpliceInfoSection
  try {
    section = decoder.parseFromHex(bytesToHex(bytes))
  } catch {
    return []
  }
  const spliceCommandType = section.spliceCommandType as unknown as number
  if (spliceCommandType == null) return []

  const segmentationDescriptors = (section.descriptors ?? []).filter(
    (d: ISpliceDescriptor): d is ISegmentationDescriptor =>
      (d as ISegmentationDescriptor).spliceDescriptorTag === SPLICE_DESCRIPTOR_TAG_SEGMENTATION,
  )

  if (segmentationDescriptors.length > 0) {
    // DescriptorLoopLength follows the 14-byte section header and command.
    // Walk the original loop in the same order as section.descriptors so a
    // merged time_signal keeps each descriptor's own independent numbering.
    if (section.spliceCommandLength == null) return []
    let offset = 16 + section.spliceCommandLength
    return (section.descriptors ?? []).flatMap((descriptor) => {
      const length = descriptor.descriptorLength + 2
      const raw = bytes.subarray(offset, offset + length)
      offset += length
      if (descriptor.spliceDescriptorTag !== SPLICE_DESCRIPTOR_TAG_SEGMENTATION) return []
      const d = descriptor as ISegmentationDescriptor
      const sub = descriptorSubNumbers(raw, d)
      return [{
        eventId: d.segmentationEventId ?? null,
        ...segmentationLabel(d.segmentationTypeId as unknown as number),
        segmentNum: d.segmentNum,
        segmentsExpected: d.segmentsExpected,
        subSegmentNum: sub?.[0],
        subSegmentsExpected: sub?.[1],
      }]
    })
  }

  if (spliceCommandType === SPLICE_COMMAND_TYPE_SPLICE_INSERT) {
    const insert = section.spliceCommand as ISpliceInsertEvent | undefined
    const kind: Scte35MarkerKind =
      insert?.outOfNetworkIndicator == null ? 'other' : insert.outOfNetworkIndicator ? 'start' : 'end'
    const label = spliceInsertLabel(kind, insert?.breakDuration?.autoReturn)
    return [{ eventId: insert?.spliceEventId ?? null, label, kind }]
  }

  return [{ eventId: null, label: spliceCommandTypeLabel(spliceCommandType), kind: 'other' }]
}
