"""Regression test for the drift-freedom design property (SCOPE.md §5).

Required test, per SCOPE.md:

    Given a fixed epoch_ticks, a total_loop_duration_ticks that is
    deliberately *not* a round number of seconds, and a range of simulated
    now_ticks values spanning many thousands of loop iterations, assert that
    loop_number/position_in_loop computed via the required integer method
    exactly match values computed independently via Python's
    arbitrary-precision integers (i.e. the test *is* the mathematical ground
    truth, not a tolerance-based comparison) — and separately, as a negative
    test, assert that a naive float-accumulation implementation *would*
    fail this same assertion, so the test actually exercises the failure
    mode it's meant to prevent, not just the happy path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop_math import compute_loop_position  # noqa: E402

TIMESCALE = 90_000  # 90kHz, matches franken-ts / MPEG-TS clock

# Deliberately not a round number of seconds: 188.2658333...s period, taken
# directly from the concrete drift figure quoted in SCOPE.md §5.
# 188.2658333... s * 90000 ticks/s = 16_943_925 ticks (not evenly divisible
# by 90000, so this loop duration is not a whole number of seconds either).
TOTAL_LOOP_DURATION_TICKS = 16_943_925
EPOCH_TICKS = 1_700_000_000 * TIMESCALE  # arbitrary fixed epoch, in ticks

# Many thousands of loop iterations, per the spec.
NUM_ITERATIONS = 10_000_000


def _ground_truth(now_ticks: int, epoch_ticks: int, total_loop_duration_ticks: int):
    """Independent re-implementation using Python's arbitrary-precision ints.

    This mirrors compute_loop_position's arithmetic but is written completely
    separately, so the test is not just calling the same code twice.
    """
    elapsed = now_ticks - epoch_ticks
    loop_number = elapsed // total_loop_duration_ticks
    position_in_loop = elapsed % total_loop_duration_ticks
    assert 0 <= position_in_loop < total_loop_duration_ticks
    assert loop_number * total_loop_duration_ticks + position_in_loop == elapsed
    return loop_number, position_in_loop


@pytest.mark.parametrize(
    "loop_offset",
    [0, 1, 2, 1000, 12345, 999_999, NUM_ITERATIONS - 1, NUM_ITERATIONS],
)
def test_integer_method_matches_ground_truth_across_many_iterations(loop_offset):
    """The required exact-match assertion, sampled across a huge iteration range."""
    now_ticks = (
        EPOCH_TICKS
        + loop_offset * TOTAL_LOOP_DURATION_TICKS
        + 12_345  # arbitrary mid-loop offset, must stay < total_loop_duration_ticks
    )

    result = compute_loop_position(now_ticks, EPOCH_TICKS, TOTAL_LOOP_DURATION_TICKS)
    expected_loop_number, expected_position = _ground_truth(
        now_ticks, EPOCH_TICKS, TOTAL_LOOP_DURATION_TICKS
    )

    assert result.loop_number == expected_loop_number
    assert result.position_in_loop_ticks == expected_position


def test_integer_method_exact_at_loop_boundaries():
    """position_in_loop_ticks must be exactly 0 at every loop boundary, with
    no float rounding error, across many thousands of loop iterations."""
    for loop_offset in range(0, 20_000, 137):  # sparse sample, still thousands
        now_ticks = EPOCH_TICKS + loop_offset * TOTAL_LOOP_DURATION_TICKS
        result = compute_loop_position(
            now_ticks, EPOCH_TICKS, TOTAL_LOOP_DURATION_TICKS
        )
        assert result.loop_number == loop_offset
        assert result.position_in_loop_ticks == 0


def test_naive_float_accumulation_drifts_and_would_fail_this_assertion():
    """Negative test: demonstrate the failure mode the integer method avoids.

    A naive server design accumulates a nominal loop-duration-in-seconds by
    repeated float addition (e.g. `current_time += loop_duration_seconds` in
    a loop, once per iteration) instead of computing position fresh via
    integer arithmetic against a fixed epoch. This test proves that approach
    measurably drifts against the exact integer ground truth over the same
    number of iterations quoted in SCOPE.md §5 (~307ms drift after 10,000,000
    iterations of a 188.2658333...s period) — i.e. it would fail the exact
    equality assertion used above, so the regression test above is actually
    exercising a real failure mode, not a vacuous one.
    """
    loop_duration_seconds = TOTAL_LOOP_DURATION_TICKS / TIMESCALE  # 188.2658333...

    # Naive implementation: accumulate a running "current loop start" in
    # float seconds by repeated addition -- exactly the anti-pattern
    # SCOPE.md §5 warns against.
    naive_accumulated_seconds = 0.0
    for _ in range(NUM_ITERATIONS):
        naive_accumulated_seconds += loop_duration_seconds

    # Ground truth for the same point in time, computed via exact integer
    # arithmetic (no accumulation at all -- a single multiplication).
    exact_ticks = NUM_ITERATIONS * TOTAL_LOOP_DURATION_TICKS
    exact_seconds = exact_ticks / TIMESCALE  # one-shot float conversion, fine for display

    drift_seconds = abs(naive_accumulated_seconds - exact_seconds)

    # SCOPE.md quotes ~307ms of drift at this iteration count. Assert the
    # naive approach drifts by a materially large amount (well above one
    # tick, ~11.1 microseconds at 90kHz) -- i.e. it would fail an
    # exact-match assertion against the integer ground truth.
    one_tick_seconds = 1 / TIMESCALE
    assert drift_seconds > 1000 * one_tick_seconds, (
        "expected the naive float-accumulation implementation to drift "
        "measurably against the exact integer result, demonstrating the "
        "failure mode this design avoids"
    )

    # And, symmetrically: the exact integer method applied fresh at the same
    # point (no accumulation) has zero drift by construction.
    result = compute_loop_position(
        EPOCH_TICKS + exact_ticks, EPOCH_TICKS, TOTAL_LOOP_DURATION_TICKS
    )
    assert result.loop_number == NUM_ITERATIONS
    assert result.position_in_loop_ticks == 0


def test_rejects_non_integer_inputs():
    with pytest.raises(ValueError):
        compute_loop_position(1.0, 0, TOTAL_LOOP_DURATION_TICKS)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compute_loop_position(0, 0.0, TOTAL_LOOP_DURATION_TICKS)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compute_loop_position(0, 0, 0)
    with pytest.raises(ValueError):
        compute_loop_position(0, 0, -1)
