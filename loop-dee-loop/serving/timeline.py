"""Pure timeline helpers: the window range, asset boundaries, discontinuity sequences and forced-Period
(signal) breaks. No Flask, no I/O, no wall clock."""

from __future__ import annotations

import dataclasses

from loop_math import LoopPosition, segment_index_for_position
from scte35_signaling import SEGMENTATION_END_TYPE_ID


@dataclasses.dataclass(frozen=True)
class WindowRange:
    """Global segment range of a manifest window (see Channel.window_range)."""

    first: int
    last: int
    live_edge: int
    ended: bool
    position: LoopPosition


def compute_asset_boundary_set(asset_boundaries: list[int]) -> set[int]:
    """Every loop always has at least one real asset-boundary discontinuity
    at local index 0 -- the loop wrap itself (grave-robber/SCOPE.md §6.3:
    "the loop-wrap boundary is always real") -- whether or not the baked
    package's own ledger happened to mark index 0 explicitly. Internal
    boundaries (grave-robber/SCOPE.md §6.1) are every other declared index.
    """
    return set(asset_boundaries) | {0}


def compute_discontinuity_sequence(
    global_index: int, segments_per_loop: int, boundaries: set[int]
) -> int:
    """Generalizes the original "one discontinuity per loop wrap" counter
    (which was simply `global_index // segments_per_loop`) to also count
    internal asset-boundary discontinuities (grave-robber/SCOPE.md §6.1).

    With `boundaries == {0}` (the normal franken-ts-authored case, no
    internal joins), this reduces exactly to the original formula -- see
    tests/test_asset_boundary_discontinuity.py.
    """
    sorted_boundaries = sorted(boundaries)
    k = len(sorted_boundaries)
    loop_number, local_index = divmod(global_index, segments_per_loop)
    # Number of boundary points <= local_index, i.e. how many of this
    # loop's own discontinuities have been "reached" by this segment
    # (inclusive -- a segment sitting exactly on a boundary counts as
    # having just crossed it, same convention the original loop-only
    # formula used for local_index 0).
    local_rank = sum(1 for b in sorted_boundaries if b <= local_index)
    # Index 0 (the loop wrap) is an implicit discontinuity at the very start
    # of the timeline, already covered by the sequence header's own base of
    # 0, hence the `- 1`. A set without 0 (continuous timeline + signal-only
    # breaks, see compute_signal_breaks) has no such implicit one.
    return loop_number * k + local_rank - (1 if 0 in boundaries else 0)


def expand_segmentation_pairs(type_ids: frozenset[int]) -> frozenset[int]:
    """A Start implies its End and vice versa (SCTE-35 Table 23: 0x22 Break
    Start <-> 0x23 Break End), so listing one is enough. An End shared by
    several Starts (0x11 Program End <- 0x10/0x17/0x19) is ambiguous and is
    not expanded backwards."""
    expanded = set(type_ids)
    for start, end in SEGMENTATION_END_TYPE_ID.items():
        if start in type_ids:
            expanded.add(end)
        if end in type_ids and sum(1 for e in SEGMENTATION_END_TYPE_ID.values() if e == end) == 1:
            expanded.add(start)
    return frozenset(expanded)


def parse_segmentation_type_ids(spec: str | None) -> frozenset[int]:
    """`--period-on-segmentation` value: comma-separated segmentation_type_id
    values, hex (`0x22`) or decimal, e.g. `0x22,0x23,0x30,0x31`."""
    ids = set()
    for token in (spec or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token, 0)
        except ValueError:
            raise ValueError(f"invalid segmentation_type_id {token!r} (expected e.g. 0x22)") from None
        if not 0 <= value <= 0xFF:
            raise ValueError(f"segmentation_type_id {token!r} out of range 0..255")
        ids.add(value)
    return frozenset(ids)


def compute_signal_breaks(
    markers: list[dict], segment_boundary_ticks: list[int], type_ids: frozenset[int]
) -> frozenset[int]:
    """Local segment indices where a *signal-only* Period / discontinuity is
    forced: the segment containing each marker whose `segmentation_type_id`
    is in `type_ids`. Unlike an asset boundary (grave-robber/SCOPE.md §6.1)
    the media timestamps stay continuous across it -- only the manifest
    signals a restart. Markers with no segmentation_type_id (plain
    splice_insert) never match."""
    breaks = set()
    for m in markers:
        raw = m.get("segmentation_type_id")
        if raw is None:
            continue
        type_id = int(raw, 16) if isinstance(raw, str) else int(raw)
        if type_id in type_ids:
            breaks.add(segment_index_for_position(m["pts_time_ticks"], segment_boundary_ticks))
    return frozenset(breaks)


def compute_declared_offset_ticks_by_local_index(
    segments_per_loop: int,
    boundaries: set[int],
    gap_ticks_by_index: dict[int, int],
) -> list[int]:
    """The declared-position accumulator (grave-robber/SCOPE.md §6.2):
    `declared_offset_ticks[i]` is the sum of every `gap_ticks` crossed by
    asset boundaries at or before local index `i`, reset every loop
    iteration (declared position is always loop-relative, computed once
    here and combined with `loop_number * total_loop_duration_ticks`
    exactly like the existing serving-position formula --
    `loop_math.program_date_time_ticks` needs no change, see SCOPE.md §6.2).

    Zero for every index when `gap_ticks_by_index` is empty (the normal,
    non-archive-derived bake path) -- PDT/Period-start then reduce to
    today's plain serving-position formula unchanged.
    """
    offsets = []
    running = 0
    for i in range(segments_per_loop):
        if i in boundaries:
            running += gap_ticks_by_index.get(i, 0)
        offsets.append(running)
    return offsets
