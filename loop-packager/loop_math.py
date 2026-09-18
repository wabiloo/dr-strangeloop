"""Integer tick arithmetic for stateless, drift-free loop position computation.

See SCOPE.md §4.2 and §5 for the design rationale. The short version:

    LINT / CODE-REVIEW CHECKLIST ITEM (per SCOPE.md §5):
    No persisted or accumulated timing state anywhere in serve.py (or here)
    may be a Python `float`. Every persisted tick value is an `int`. Floats
    are only ever produced as a final, one-shot conversion for display or
    serialization (e.g. formatting an ISO8601 timestamp string in a single
    response) — never fed back into a subsequent computation.

Every request recomputes its position from scratch via one integer
multiplication/division against a fixed epoch (`epoch_ticks`). There is no
"current position" variable anywhere that a timer advances, so there is
nothing that can accumulate drift: the result for loop 10 is exactly as
exact as loop 10,000,000,000.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoopPosition:
    """The result of resolving a point in wall-clock time to a loop position.

    All fields are integers in the channel's timescale (ticks), except
    `epoch_ticks`/`now_ticks` which are echoed back for convenience.
    """

    loop_number: int
    position_in_loop_ticks: int
    elapsed_ticks: int


def compute_loop_position(
    now_ticks: int,
    epoch_ticks: int,
    total_loop_duration_ticks: int,
) -> LoopPosition:
    """Resolve `now_ticks` to a loop position using exact integer arithmetic.

    Args:
        now_ticks: current wall-clock time, in ticks (same timescale as
            `total_loop_duration_ticks`), as an integer. Callers are
            responsible for converting wall-clock time to an integer tick
            count exactly once, at the point of measurement — never carry a
            float through this function.
        epoch_ticks: the fixed channel epoch (loop 0's start), in ticks.
        total_loop_duration_ticks: exact length of one loop iteration, in
            ticks, as produced by the bake phase (ground truth, read back
            from the actual baked segments — see SCOPE.md §4.1 step 4).

    Returns:
        LoopPosition with `loop_number` (int, >= 0 for now_ticks >= epoch_ticks),
        `position_in_loop_ticks` (int, in [0, total_loop_duration_ticks)),
        and `elapsed_ticks` (int, `now_ticks - epoch_ticks`).

    Raises:
        ValueError: if inputs are not integers, or total_loop_duration_ticks
            is not strictly positive.
    """
    if not isinstance(now_ticks, int):
        raise ValueError(f"now_ticks must be int, got {type(now_ticks).__name__}")
    if not isinstance(epoch_ticks, int):
        raise ValueError(f"epoch_ticks must be int, got {type(epoch_ticks).__name__}")
    if not isinstance(total_loop_duration_ticks, int):
        raise ValueError(
            "total_loop_duration_ticks must be int, "
            f"got {type(total_loop_duration_ticks).__name__}"
        )
    if total_loop_duration_ticks <= 0:
        raise ValueError("total_loop_duration_ticks must be strictly positive")

    elapsed_ticks = now_ticks - epoch_ticks
    # Exact integer floor division / modulo. Python's `//` and `%` on ints are
    # arbitrary-precision and exact — no float ever enters this computation.
    loop_number = elapsed_ticks // total_loop_duration_ticks
    position_in_loop_ticks = elapsed_ticks % total_loop_duration_ticks

    return LoopPosition(
        loop_number=loop_number,
        position_in_loop_ticks=position_in_loop_ticks,
        elapsed_ticks=elapsed_ticks,
    )


def segment_index_for_position(
    position_in_loop_ticks: int,
    segment_boundary_ticks: list[int],
) -> int:
    """Return the index of the physical segment covering `position_in_loop_ticks`.

    `segment_boundary_ticks` is the sorted list of *start* ticks of each
    physical segment within one loop iteration (segment i spans
    `[segment_boundary_ticks[i], segment_boundary_ticks[i+1])`, and the last
    segment spans up to `total_loop_duration_ticks`). All values are ints.
    """
    if not isinstance(position_in_loop_ticks, int):
        raise ValueError("position_in_loop_ticks must be int")

    # Simple linear scan is fine: segment counts per loop are small (seconds
    # to low tens of minutes of content at typical 2-6s segment durations).
    idx = 0
    for i, boundary in enumerate(segment_boundary_ticks):
        if boundary <= position_in_loop_ticks:
            idx = i
        else:
            break
    return idx


def global_segment_number(
    loop_number: int,
    segment_index_within_loop: int,
    segments_per_loop: int,
) -> int:
    """Compute the ever-increasing HLS media-sequence / DASH $Number$ value.

    `loop_number * segments_per_loop + segment_index_within_loop`, all ints.
    """
    if segments_per_loop <= 0:
        raise ValueError("segments_per_loop must be strictly positive")
    return loop_number * segments_per_loop + segment_index_within_loop


def program_date_time_ticks(
    loop_number: int,
    position_in_loop_ticks: int,
    total_loop_duration_ticks: int,
    epoch_ticks: int,
) -> int:
    """Return the absolute tick value corresponding to `position_in_loop_ticks`.

    This is `epoch_ticks + loop_number * total_loop_duration_ticks +
    position_in_loop_ticks`, computed once as an integer. Convert the result
    to an ISO8601 wall-clock string (dividing by the timescale) exactly once,
    as the very last step before serialization — never store or reuse the
    resulting float.
    """
    return (
        epoch_ticks
        + loop_number * total_loop_duration_ticks
        + position_in_loop_ticks
    )


def ticks_to_wall_clock_seconds(ticks: int, timescale: int) -> float:
    """One-shot, final conversion of an integer tick value to float seconds.

    This is the *only* place floats are allowed to appear for timing values,
    and the result must never be fed back into `compute_loop_position` or any
    other tick arithmetic — it exists purely for display/serialization (e.g.
    building an ISO8601 timestamp for a single manifest response).
    """
    if not isinstance(ticks, int):
        raise ValueError("ticks must be int")
    if not isinstance(timescale, int) or timescale <= 0:
        raise ValueError("timescale must be a strictly positive int")
    return ticks / timescale
