"""Low-latency DASH (CMAF chunked) delivery: pacing of a baked segment's chunks.

A package baked with `bake.py --chunk-duration` stores every segment as
several `moof`+`mdat` chunks, each with its own `tfdt`. For a segment that is
still "in progress" on the wall clock, `serve.py` streams it chunked and
releases each chunk only when it would have been complete on a real live
encoder -- i.e. at the chunk's own END, which is the next chunk's start (the
last chunk ends with the segment). A player that is allowed to request the
segment early (MPD `availabilityTimeOffset`) therefore receives the first
chunk about one chunk-duration after the segment started instead of a whole
segment later.

Everything here is integer ticks of the package timescale, no floats: the
schedule is derived from the epoch and the baked timestamps only, like the
rest of the timeline (loop_math.py), so it cannot drift.

Chunk boundaries come from the REAL `tfdt`s, never from the nominal chunk
size -- GPAC does not cut chunks evenly (e.g. 15/13/13/9 frames in a 2 s
segment).
"""

from __future__ import annotations

import dataclasses
import struct
from bisect import bisect_right
from collections.abc import Callable, Iterator

import cmaf


def split_chunks(data: bytes) -> list[tuple[bytes, int]]:
    """[(moof+mdat bytes, tfdt), ...] of a bare CMAF media segment.

    Raises ValueError if a chunk has no `tfdt` (bake.py's repair would have
    removed it; a chunked package must carry one per chunk)."""
    chunks: list[tuple[bytes, int]] = []
    for fragment in cmaf.split_fragments(data):
        moof = cmaf._find(fragment, ("moof",))
        tfdt = cmaf._find(fragment, ("traf", "tfdt"), moof[0] + 8, moof[1]) if moof else None
        if tfdt is None:
            raise ValueError("chunk without tfdt")
        at = tfdt[0] + 8
        version = fragment[at]
        fmt, width = (">Q", 8) if version == 1 else (">I", 4)
        chunks.append((fragment, struct.unpack(fmt, fragment[at + 4 : at + 4 + width])[0]))
    return chunks


def release_offsets_ticks(
    tfdts: list[int], track_timescale: int, package_timescale: int, segment_duration_ticks: int
) -> list[int]:
    """When each chunk is complete, in package ticks after its segment's start.

    Chunk k ends where chunk k+1 begins (`tfdt[k+1] - tfdt[0]`); the last chunk
    ends with the segment itself, so its release is `segment_duration_ticks`
    (the manifest's own segment length, not a re-derived one)."""
    base = tfdts[0]
    offsets = [(t - base) * package_timescale // track_timescale for t in tfdts[1:]]
    offsets.append(segment_duration_ticks)
    # Never later than the segment end, never going backwards.
    out: list[int] = []
    previous = 0
    for offset in offsets:
        previous = max(previous, min(offset, segment_duration_ticks))
        out.append(previous)
    return out


def paced(
    chunks: list[bytes],
    release_ticks: list[int],
    now_ticks: Callable[[], int],
    sleep: Callable[[float], None],
    package_timescale: int,
    max_slice_seconds: float = 0.05,
) -> Iterator[bytes]:
    """Yield each chunk once `now_ticks()` has reached its absolute release tick.

    Waits in short slices so a worker thread never sleeps through a
    disconnect for long, and re-reads the clock after each slice rather than
    trusting one long sleep."""
    for chunk, release in zip(chunks, release_ticks):
        while True:
            remaining = release - now_ticks()
            if remaining <= 0:
                break
            sleep(min(remaining / package_timescale, max_slice_seconds))
        yield chunk


def segment_occurrence(
    now: int,
    epoch_ticks: int,
    loop_duration_ticks: int,
    start_in_loop_ticks: int,
    duration_ticks: int,
    lookahead_ticks: int,
    loop_number: int | None = None,
) -> int | None:
    """Absolute start tick of the occurrence of a segment that should be paced,
    or None when it is already complete (serve it at once).

    `loop_number` is known for continuous-timeline URLs (the global index names
    it). Loop-local URLs (`/seg/<local>`) repeat every loop, so the occurrence
    is whichever of the current and the next loop has not ended yet -- and only
    if it starts within `lookahead_ticks`, so a request for some far-away
    segment is not held for minutes."""
    if loop_number is not None:
        candidates = [loop_number]
    else:
        current = max(0, (now - epoch_ticks) // loop_duration_ticks)
        candidates = [current, current + 1]
    for loop in candidates:
        start = epoch_ticks + loop * loop_duration_ticks + start_in_loop_ticks
        if start + duration_ticks > now:
            return start if start - now <= lookahead_ticks else None
    return None


# ── chunk table: how a baked segment divides into parts (LL-HLS) ───────────────────────────────

_NON_SYNC = 0x00010000  # sample_is_non_sync_sample, in ISO/IEC 14496-12 sample flags


@dataclasses.dataclass(frozen=True)
class Chunk:
    """One `moof`+`mdat` of a segment: where it sits in time (package ticks after the segment start), how long it
    is, whether it can be decoded on its own (starts on a sync sample = LL-HLS `INDEPENDENT=YES`), and where its
    bytes are in the segment file. The first chunk's span starts at byte 0, so it carries any `styp`/`sidx` ahead
    of the first `moof` and the spans of a segment tile the whole file."""

    start_ticks: int
    duration_ticks: int
    independent: bool
    byte_start: int
    byte_end: int


def _first_sample_is_sync(fragment: bytes, all_sync: bool) -> bool:
    """Whether the first sample of a `moof`+`mdat` is a sync sample: the `trun` first-sample-flags, else the first
    sample's own flags, else the `tfhd` default. Audio is all sync (`all_sync`); an unreadable fragment is not
    claimed independent."""
    if all_sync:
        return True
    moof = cmaf._find(fragment, ("moof",))
    if moof is None:
        return False
    traf = cmaf._find(fragment, ("traf",), moof[0] + 8, moof[1])
    if traf is None:
        return False
    default_flags = None
    tfhd = cmaf._find(fragment, ("tfhd",), traf[0] + 8, traf[1])
    if tfhd is not None:
        flags = int.from_bytes(fragment[tfhd[0] + 9 : tfhd[0] + 12], "big")
        at = tfhd[0] + 16  # box header 8 + version/flags 4 + track_ID 4
        if flags & 0x01:
            at += 8  # base_data_offset
        if flags & 0x02:
            at += 4  # sample_description_index
        if flags & 0x08:
            at += 4  # default_sample_duration
        if flags & 0x10:
            at += 4  # default_sample_size
        if flags & 0x20:
            default_flags = int.from_bytes(fragment[at : at + 4], "big")
    trun = cmaf._find(fragment, ("trun",), traf[0] + 8, traf[1])
    if trun is None:
        return False
    flags = int.from_bytes(fragment[trun[0] + 9 : trun[0] + 12], "big")
    at = trun[0] + 16  # header 8 + version/flags 4 + sample_count 4
    if flags & 0x01:
        at += 4  # data_offset
    if flags & 0x04:  # first_sample_flags present: overrides the first sample's own
        return not int.from_bytes(fragment[at : at + 4], "big") & _NON_SYNC
    if flags & 0x400:  # per-sample flags: the first sample's
        at += 4 * (bool(flags & 0x100) + bool(flags & 0x200))
        return not int.from_bytes(fragment[at : at + 4], "big") & _NON_SYNC
    return default_flags is not None and not default_flags & _NON_SYNC


def chunk_table(
    data: bytes, track_timescale: int, package_timescale: int, segment_duration_ticks: int, *, all_sync: bool = False
) -> list[Chunk]:
    """The chunks of a bare CMAF media segment, from its real `tfdt`s (never from the nominal chunk size). The
    last chunk runs to the segment end, so the durations always sum to `segment_duration_ticks`."""
    spans: list[tuple[int, int, bytes]] = []  # (moof start, mdat end, fragment bytes)
    pending: int | None = None
    for box_type, start, end in cmaf.iter_boxes(data):
        if box_type == "moof":
            pending = start
        elif box_type == "mdat" and pending is not None:
            spans.append((pending, end, data[pending:end]))
            pending = None
    if not spans:
        return []
    tfdts = [t for _, t in split_chunks(data)]
    starts = release_offsets_ticks(tfdts, track_timescale, package_timescale, segment_duration_ticks)
    # `release_offsets_ticks` gives each chunk's END; a chunk starts where the previous one ended.
    begin = [0, *starts[:-1]]
    out: list[Chunk] = []
    for i, (moof_start, mdat_end, fragment) in enumerate(spans):
        out.append(
            Chunk(
                start_ticks=begin[i],
                duration_ticks=starts[i] - begin[i],
                independent=_first_sample_is_sync(fragment, all_sync),
                byte_start=0 if i == 0 else moof_start,
                byte_end=mdat_end,
            )
        )
    return out


# ── LL-HLS: which parts exist at a given moment ────────────────────────────────────────────────


def part_release_tick(chunks: list[Chunk], segment_start: int, part: int) -> int:
    """Absolute tick at which part `part` of a segment starting at `segment_start` is complete."""
    c = chunks[part]
    return segment_start + c.start_ticks + c.duration_ticks


def parts_released(chunks: list[Chunk], segment_start: int, now: int) -> int:
    """How many parts of the segment starting at `segment_start` are complete at `now` (all of them once it ended)."""
    return sum(1 for part in range(len(chunks)) if part_release_tick(chunks, segment_start, part) <= now)


def live_position(
    boundary_ticks: list[int],
    total_ticks: int,
    epoch_ticks: int,
    segments_per_loop: int,
    chunks_for: Callable[[int], list[Chunk]],
    now: int,
) -> tuple[int, int]:
    """(msn, part) of the newest part that exists at `now` for one playlist's own boundaries: the part before the
    one in progress (LL-HLS `RENDITION-REPORT` LAST-MSN / LAST-PART). When the segment in progress has released no
    part yet, that is the last part of the segment before it."""
    elapsed = now - epoch_ticks
    loop, position = divmod(elapsed, total_ticks)
    local = max(0, bisect_right(boundary_ticks, position) - 1)
    msn = loop * segments_per_loop + local
    start = epoch_ticks + loop * total_ticks + boundary_ticks[local]
    released = parts_released(chunks_for(local), start, now)
    if released > 0:
        return msn, released - 1
    if local > 0:
        return msn - 1, len(chunks_for(local - 1)) - 1
    if loop > 0:  # the last segment of the previous loop
        return msn - 1, len(chunks_for(segments_per_loop - 1)) - 1
    return msn, 0

