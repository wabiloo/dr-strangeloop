/** Thin wrapper around the `scte35` npm package (Comcast, Apache-2.0,
 * https://github.com/Comcast/scte35-js) for decoding the raw SCTE-35 bytes
 * that hls.js / dash.js already hand us with each marker event -- used by
 * PlaybackPanel.vue to label the "a marker just appeared" toast with the
 * segmentation type (or splice type, for splice_insert which carries no
 * segmentation descriptor at all). No hand-rolled binary parsing here;
 * only the (small) amount of glue needed to go from "raw bytes + the
 * event_id we already have from the player" to "which descriptor, if any,
 * describes THIS event" -- a single SCTE-35 message can carry several
 * coincident events (e.g. a Break start + a nested PPO start + an instant
 * Call Ad Server, all merged into one message by franken-ts), each with
 * its own segmentation descriptor. */

import { SCTE35 } from 'scte35'
import type { ISpliceInfoSection, ISpliceInsertEvent } from 'scte35/build/ISCTE35'
import type { ISegmentationDescriptor, ISpliceDescriptor } from 'scte35/build/descriptors'
import { isInstantTypeIdValue, nameForTypeIdByte } from './segmentationPresets'

const decoder = new SCTE35()

export interface Scte35MarkerInfo {
  spliceCommandType: number
  /** The segmentation descriptor matching this specific event_id, if the
   * message is a time_signal and one was found. */
  segmentationTypeId: number | null
  /** Only meaningful for splice_insert (spliceCommandType 5): true = this
   * is the "out" (break start) half of the two-point splice, false = the
   * "in" (break end) half. splice_insert has no segmentation descriptor at
   * all, so this is the only way to tell Start from End for it. */
  outOfNetworkIndicator: boolean | null
}

function bytesToHex(bytes: Uint8Array): string {
  let hex = ''
  for (let i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, '0')
  return hex
}

const SPLICE_COMMAND_TYPE_SPLICE_INSERT = 5
const SPLICE_DESCRIPTOR_TAG_SEGMENTATION = 2

/** Decode `bytes` (a full splice_info_section) and return the splice
 * command type plus the segmentation_type_id of whichever descriptor's
 * segmentationEventId matches `eventId` -- not just the first descriptor
 * found, since a message can carry several distinct events at once. */
export function parseScte35Marker(bytes: Uint8Array, eventId: number): Scte35MarkerInfo | null {
  let section: ISpliceInfoSection
  try {
    section = decoder.parseFromHex(bytesToHex(bytes))
  } catch {
    return null
  }
  const spliceCommandType = section.spliceCommandType as unknown as number
  if (spliceCommandType == null) return null

  let outOfNetworkIndicator: boolean | null = null
  if (spliceCommandType === SPLICE_COMMAND_TYPE_SPLICE_INSERT) {
    const insert = section.spliceCommand as ISpliceInsertEvent | undefined
    outOfNetworkIndicator = insert?.outOfNetworkIndicator ?? null
  }

  const descriptor = (section.descriptors ?? []).find(
    (d: ISpliceDescriptor): d is ISegmentationDescriptor =>
      (d as ISegmentationDescriptor).spliceDescriptorTag === SPLICE_DESCRIPTOR_TAG_SEGMENTATION &&
      (d as ISegmentationDescriptor).segmentationEventId === eventId,
  )

  return {
    spliceCommandType,
    segmentationTypeId: descriptor?.segmentationTypeId ?? null,
    outOfNetworkIndicator,
  }
}

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

/** Full, correctly-worded label for a marker toast: the segmentation type
 * name (or "Splice Insert" if there's no segmentation descriptor) plus
 * " Start"/" End" -- using proper SCTE-35 terminology, NOT HLS's
 * CUE-OUT/CUE-IN or DATERANGE SCTE35-OUT/SCTE35-IN attribute naming, which
 * is just how HLS happens to signal the two halves of a pair on the wire
 * and has nothing to do with the segmentation type's own vocabulary.
 * Instant/standalone types (e.g. Call Ad Server) get NO suffix at all --
 * there is no Start/End pairing for them, just one signal. */
export function describeMarkerLabel(bytes: Uint8Array, eventId: number): string {
  const info = parseScte35Marker(bytes, eventId)
  if (!info) return 'SCTE-35'

  if (info.segmentationTypeId != null) {
    const name = nameForTypeIdByte(info.segmentationTypeId)
    if (isInstantTypeIdValue(info.segmentationTypeId)) return name
    return `${name} ${info.segmentationTypeId % 2 === 0 ? 'Start' : 'End'}`
  }

  if (info.spliceCommandType === SPLICE_COMMAND_TYPE_SPLICE_INSERT) {
    if (info.outOfNetworkIndicator == null) return 'Splice Insert'
    return `Splice Insert ${info.outOfNetworkIndicator ? 'Start' : 'End'}`
  }

  return spliceCommandTypeLabel(info.spliceCommandType)
}

