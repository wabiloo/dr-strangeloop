"""Minimal ISO-BMFF helpers for turning independently-remuxed fragmented-MP4
segment files (sparse/archive mode, SCOPE.md §11) into a proper CMAF layout:
ONE shared init segment (ftyp+moov) plus bare `moof+mdat` media segments whose
`tfdt` continues the loop timeline.

Only the boxes this needs are parsed; everything else is copied verbatim.
"""

from __future__ import annotations

import struct


def iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    """Yield (type, box_start, box_end) for each box in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        box_type = data[pos + 4 : pos + 8].decode("latin1")
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError(f"Malformed box {box_type!r} at offset {pos}")
        yield box_type, pos, pos + size
        pos += size


def _find(data: bytes, path: tuple[str, ...], start: int = 0, end: int | None = None):
    """(box_start, box_end) of the first box reached by descending `path`."""
    end = len(data) if end is None else end
    for box_type, b_start, b_end in iter_boxes(data, start, end):
        if box_type == path[0]:
            if len(path) == 1:
                return b_start, b_end
            found = _find(data, path[1:], b_start + 8, b_end)
            if found:
                return found
    return None


def split_init_and_fragments(data: bytes) -> tuple[bytes, list[bytes]]:
    """(init = ftyp+moov, [moof+mdat, ...]) of a fragmented MP4. sidx/styp/
    mfra and any other boxes are dropped."""
    init_parts: list[bytes] = []
    fragments: list[bytes] = []
    pending_moof: bytes | None = None
    for box_type, b_start, b_end in iter_boxes(data):
        chunk = data[b_start:b_end]
        if box_type in ("ftyp", "moov"):
            init_parts.append(chunk)
        elif box_type == "moof":
            pending_moof = chunk
        elif box_type == "mdat" and pending_moof is not None:
            fragments.append(pending_moof + chunk)
            pending_moof = None
    if not init_parts or not fragments:
        raise ValueError("Not a fragmented MP4 with an init and at least one fragment")
    return b"".join(init_parts), fragments


def avcc_config(init: bytes) -> bytes | None:
    """Raw avcC payload (profile/level + SPS/PPS) from an init segment, for
    checking that separately-remuxed segments really share one decoder config."""
    pos = init.find(b"avcC")
    if pos < 0:
        return None
    size = struct.unpack(">I", init[pos - 4 : pos])[0]
    return init[pos + 4 : pos - 4 + size]


def track_timescale(init: bytes) -> int | None:
    """Timescale of the first track's mdhd."""
    mdhd = _find(init, ("moov", "trak", "mdia", "mdhd"))
    if not mdhd:
        return None
    version = init[mdhd[0] + 8]
    offset = mdhd[0] + 8 + 4 + (16 if version == 1 else 8)
    return struct.unpack(">I", init[offset : offset + 4])[0]


video_track_timescale = track_timescale


def _aac_specific_config(esds_payload: bytes) -> bytes | None:
    """AudioSpecificConfig (descriptor tag 0x05) inside an esds box payload."""
    pos = 4  # version/flags
    while pos < len(esds_payload):
        tag = esds_payload[pos]
        pos += 1
        length = 0
        for _ in range(4):
            byte = esds_payload[pos]
            pos += 1
            length = (length << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        if tag == 0x05:
            return esds_payload[pos : pos + length]
        if tag in (0x03, 0x04):  # ES_Descriptor / DecoderConfigDescriptor: descend past their fixed heads
            pos += 3 if tag == 0x03 else 13
        else:
            pos += length
    return None


def stsd_config(init: bytes) -> bytes | None:
    """What must match for segments to share one init, for any track type.
    For AAC (mp4a) that is the channel layout, sample rate and
    AudioSpecificConfig -- NOT the whole sample entry, whose esds carries
    per-file bitrate fields; anything else compares the whole stsd."""
    stsd = _find(init, ("moov", "trak", "mdia", "minf", "stbl", "stsd"))
    if not stsd:
        return None
    entry_start = stsd[0] + 16  # box header + version/flags + entry_count
    entry_type = init[entry_start + 4 : entry_start + 8]
    if entry_type == b"mp4a":
        entry_end = entry_start + struct.unpack(">I", init[entry_start : entry_start + 4])[0]
        esds = _find(init, ("esds",), entry_start + 36, entry_end)
        asc = _aac_specific_config(init[esds[0] + 8 : esds[1]]) if esds else None
        return b"mp4a" + init[entry_start + 24 : entry_start + 36] + (asc or b"")
    return init[stsd[0] : stsd[1]]


def rebase_fragments(fragments: list[bytes], target_start: int, first_sequence_number: int) -> tuple[bytes, int]:
    """Concatenate `fragments`, rewriting each `tfdt` so the first fragment
    starts at `target_start` (track-timescale ticks) and the rest keep their
    relative offsets, and renumbering `mfhd` sequence numbers from
    `first_sequence_number`. Returns (bytes, next_sequence_number)."""
    out = bytearray()
    shift: int | None = None
    seq = first_sequence_number
    for fragment in fragments:
        buf = bytearray(fragment)
        moof = _find(bytes(buf), ("moof",))
        assert moof is not None
        mfhd = _find(bytes(buf), ("mfhd",), moof[0] + 8, moof[1])
        if mfhd:
            struct.pack_into(">I", buf, mfhd[0] + 12, seq)
            seq += 1
        tfdt = _find(bytes(buf), ("traf", "tfdt"), moof[0] + 8, moof[1])
        if tfdt:
            version = buf[tfdt[0] + 8]
            fmt, width = (">Q", 8) if version == 1 else (">I", 4)
            at = tfdt[0] + 12
            old = struct.unpack(fmt, buf[at : at + width])[0]
            if shift is None:
                shift = target_start - old
            new = old + shift
            if new < 0 or (version == 0 and new >= 1 << 32):
                raise ValueError(f"Rebased tfdt {new} out of range for tfdt v{version}")
            struct.pack_into(fmt, buf, at, new)
        out += buf
    return bytes(out), seq
