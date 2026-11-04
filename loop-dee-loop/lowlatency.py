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

import struct
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
