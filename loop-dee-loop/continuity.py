"""PTS/DTS/PCR continuity across loop boundaries (SCOPE.md §12).

Every physical segment file is reused byte-for-byte on every loop iteration
(SCOPE.md §4.1 step 6) -- normally serve.py signals that with
#EXT-X-DISCONTINUITY / a new DASH Period each wrap, because the segment's
own internal timestamps restart from the same loop-relative values every
time. This module instead lets serve.py rewrite just the timestamp fields
of a copy of the segment bytes, per request, so the SAME physical bytes can
be presented at a different absolute position on each loop iteration --
making the whole channel one genuinely continuous timeline with no
discontinuity/Period-restart signaling needed at the wrap.

Two independent primitives, one per container:

- `shift_cmaf_fragment`: CMAF/fMP4 media segments carry exactly one `tfdt`
  box per track fragment (the fragment's own absolute decode-time anchor;
  everything else -- `trun` sample durations -- is relative to it), so this
  is an O(1) fixed-offset patch. Thin wrapper over `cmaf.rebase_fragments`,
  which already does exactly this (used today for sparse/archive-mode
  rebasing, SCOPE.md §11).
- `shift_ts_segment`: MPEG-TS has no single anchor -- PTS/DTS live in every
  PES packet header and PCR in scattered adaptation fields -- so this is an
  O(packet count) scan-and-patch, still just fixed-width integer field
  rewrites, never a re-mux/re-encode. PTS/DTS/PCR all wrap at their spec-
  defined 33-bit modulus (~26.5h at 90kHz) exactly as any long-running real
  MPEG-TS stream already must -- this is expected client-side behavior, not
  a new failure mode introduced here.

Both take a `shift_ticks` value in the channel's 90kHz timescale -- always
`loop_number * total_loop_duration_ticks` for a loop-wrap shift, but the
functions themselves don't know or care where the value comes from.
"""

from __future__ import annotations

import struct

import cmaf

TIMESCALE = 90_000

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
# PTS/DTS (Table 2-21/2-22) and PCR base (2.4.2.2) are both 33-bit fields in
# the MPEG system clock. PCR additionally carries a 9-bit, 1/300-tick
# extension (27MHz resolution) alongside its 90kHz-resolution base.
PTS_DTS_MODULUS = 1 << 33
PCR_BASE_MODULUS = 1 << 33
PCR_EXT_PER_TICK = 300


def shift_cmaf_fragment(data: bytes, shift_ticks: int, *, sequence_number: int) -> bytes:
    """Return a copy of one CMAF media segment (`moof`+`mdat`, no init) with
    its `tfdt` advanced by `shift_ticks` and its `mfhd` sequence number set
    to `sequence_number` (the caller's global, ever-increasing segment
    index -- keeps `mfhd` meaningful across loop iterations instead of
    restarting at the same value every time, same as the timestamps).

    `shift_ticks` is not itself range-checked here: `cmaf.rebase_fragments`
    raises if the shifted value overflows a v0 (32-bit) `tfdt` -- callers
    serving a long-running channel should ensure segments are baked with a
    v1 (64-bit) `tfdt` (GPAC's default for CMAF) so this never triggers.
    """
    moof = cmaf._find(data, ("moof",))
    if moof is None:
        raise ValueError("no moof box found -- not a bare CMAF media fragment")
    tfdt = cmaf._find(data, ("traf", "tfdt"), moof[0] + 8, moof[1])
    if tfdt is None:
        raise ValueError("no tfdt box found in moof/traf")
    version = data[tfdt[0] + 8]
    fmt, width = (">Q", 8) if version == 1 else (">I", 4)
    at = tfdt[0] + 12
    old_tfdt = struct.unpack(fmt, data[at : at + width])[0]
    target_start = old_tfdt + shift_ticks
    body, _next_seq = cmaf.rebase_fragments(
        [data], target_start=target_start, first_sequence_number=sequence_number
    )
    return body


def _pack_ts_timestamp(guard: int, value: int) -> bytes:
    """5-byte PTS/DTS field: a 4-bit guard nibble ('0010' PTS-only, '0011'
    PTS-of-PTS+DTS, '0001' DTS-of-PTS+DTS) then the 33-bit value split
    across three marker-bit-interleaved groups (10/6, 22/8, 15/8 bits)."""
    b0 = (guard << 4) | (((value >> 30) & 0x07) << 1) | 1
    b1 = (value >> 22) & 0xFF
    b2 = (((value >> 15) & 0x7F) << 1) | 1
    b3 = (value >> 7) & 0xFF
    b4 = ((value & 0x7F) << 1) | 1
    return bytes((b0, b1, b2, b3, b4))


def _unpack_ts_timestamp(data: bytes) -> int:
    """33-bit value from a 5-byte PTS/DTS field (see `_pack_ts_timestamp`)."""
    b0, b1, b2, b3, b4 = data
    return (
        ((b0 >> 1) & 0x07) << 30
        | b1 << 22
        | ((b2 >> 1) & 0x7F) << 15
        | b3 << 7
        | (b4 >> 1)
    )


def _pack_pcr(base: int, ext: int) -> bytes:
    """6-byte PCR field: 33-bit base, 6 reserved bits (all 1), 9-bit ext."""
    b0 = (base >> 25) & 0xFF
    b1 = (base >> 17) & 0xFF
    b2 = (base >> 9) & 0xFF
    b3 = (base >> 1) & 0xFF
    b4 = ((base & 1) << 7) | 0x7E | ((ext >> 8) & 1)
    b5 = ext & 0xFF
    return bytes((b0, b1, b2, b3, b4, b5))


def _unpack_pcr(data: bytes) -> tuple[int, int]:
    """(base, ext) from a 6-byte PCR field (see `_pack_pcr`)."""
    b0, b1, b2, b3, b4, b5 = data
    base = (b0 << 25) | (b1 << 17) | (b2 << 9) | (b3 << 1) | (b4 >> 7)
    ext = ((b4 & 0x01) << 8) | b5
    return base, ext


def shift_ts_segment(data: bytes, shift_ticks: int) -> bytes:
    """Return a copy of `data` (a self-contained MPEG-TS segment, plain
    188-byte packets) with every PES PTS/DTS and adaptation-field PCR
    advanced by `shift_ticks` 90kHz ticks, wrapping at the spec's 33-bit
    modulus. Sample/payload bytes are never touched -- only the fixed-width
    timestamp fields move, so this is a pure header rewrite, not a remux.

    PES detection: a packet with payload_unit_start_indicator set whose
    payload begins with the packet_start_code_prefix `00 00 01` is treated
    as a PES header (true for any elementary-stream PID; PSI sections
    -- PAT/PMT/etc, which this tool doesn't carry SCTE-35 in anyway, see
    module docstring -- never start with that prefix). PCR detection uses
    the adaptation field's own PCR_flag bit, independent of PID.
    """
    buf = bytearray(data)
    n = len(buf)
    if n % TS_PACKET_SIZE != 0:
        raise ValueError(f"data length {n} is not a multiple of {TS_PACKET_SIZE}")

    for offset in range(0, n, TS_PACKET_SIZE):
        if buf[offset] != TS_SYNC_BYTE:
            raise ValueError(
                f"expected TS sync byte 0x47 at offset {offset}, got "
                f"0x{buf[offset]:02x}"
            )

        pusi = (buf[offset + 1] >> 6) & 0x01
        afc = (buf[offset + 3] >> 4) & 0x03  # adaptation_field_control

        pos = offset + 4
        if afc in (0b10, 0b11):
            af_len = buf[pos]
            af_start = pos + 1
            if af_len > 0:
                flags = buf[af_start]
                pcr_flag = (flags >> 4) & 0x01
                if pcr_flag:
                    pcr_off = af_start + 1
                    base, ext = _unpack_pcr(bytes(buf[pcr_off : pcr_off + 6]))
                    total = base * PCR_EXT_PER_TICK + ext + shift_ticks * PCR_EXT_PER_TICK
                    total %= PCR_BASE_MODULUS * PCR_EXT_PER_TICK
                    new_base, new_ext = divmod(total, PCR_EXT_PER_TICK)
                    buf[pcr_off : pcr_off + 6] = _pack_pcr(new_base, new_ext)
            pos = af_start + af_len
        elif afc == 0b00:
            continue  # reserved adaptation_field_control value -- no payload

        packet_end = offset + TS_PACKET_SIZE
        if afc not in (0b01, 0b11) or not pusi:
            continue
        if pos + 9 > packet_end or buf[pos : pos + 3] != b"\x00\x00\x01":
            continue  # not a PES start (e.g. a PSI section)

        pts_dts_flags = (buf[pos + 7] >> 6) & 0x03
        if pts_dts_flags not in (0b10, 0b11) or pos + 14 > packet_end:
            continue

        pts_off = pos + 9
        old_pts = _unpack_ts_timestamp(bytes(buf[pts_off : pts_off + 5]))
        new_pts = (old_pts + shift_ticks) % PTS_DTS_MODULUS
        guard = 0b0011 if pts_dts_flags == 0b11 else 0b0010
        buf[pts_off : pts_off + 5] = _pack_ts_timestamp(guard, new_pts)

        if pts_dts_flags == 0b11 and pos + 19 <= packet_end:
            dts_off = pts_off + 5
            old_dts = _unpack_ts_timestamp(bytes(buf[dts_off : dts_off + 5]))
            new_dts = (old_dts + shift_ticks) % PTS_DTS_MODULUS
            buf[dts_off : dts_off + 5] = _pack_ts_timestamp(0b0001, new_dts)

    return bytes(buf)
