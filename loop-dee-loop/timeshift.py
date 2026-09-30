"""Startover / catchup range resolution (SCOPE.md §13).

Pure integer-tick helpers -- no Flask, no I/O -- so the drift-freedom rule
(loop_math.py: no floats in timing state) holds here too. Seconds given as
a decimal string are converted with `Decimal`/`Fraction`, never a float.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal

from loop_math import compute_loop_position, global_segment_number, segment_index_for_position

# Numeric instants above this are milliseconds, below are seconds
# (1e11 s is year 5138; 1e11 ms is 1973).
_MS_THRESHOLD = 10**11
_NUMERIC = re.compile(r"^\d+(\.\d+)?$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class TimeshiftError(ValueError):
    """A bad startover/catchup request (maps to HTTP 400)."""


@dataclass(frozen=True)
class TimeshiftConfig:
    enabled: bool = False
    start_param: str = "start"
    end_param: str = "end"
    continuous_param: str = "continuous_timeline"
    full_loop_param: str = "full_loop"
    max_span_seconds: int = 21600

    def __post_init__(self):
        names = [self.start_param, self.end_param, self.continuous_param, self.full_loop_param]
        if any(not n for n in names) or len(set(names)) != len(names):
            raise ValueError(f"timeshift parameter names must be non-empty and distinct: {names}")
        if self.max_span_seconds < 1:
            raise ValueError("max_span_seconds must be >= 1")

    @property
    def param_names(self) -> tuple[str, ...]:
        return (self.start_param, self.end_param, self.continuous_param, self.full_loop_param)


def parse_bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise TimeshiftError(f"'{name}' must be a boolean (true/false/1/0), got {value!r}")


def parse_instant_ticks(value: str, timescale: int, name: str = "time") -> int:
    """Epoch seconds, epoch milliseconds (value >= 1e11) or ISO8601 -> an
    integer tick count since the Unix epoch. A naive ISO8601 string is UTC."""
    raw = value.strip()
    if _NUMERIC.match(raw):
        number = Decimal(raw)
        if number >= _MS_THRESHOLD:
            number = number / 1000
        return int((number * timescale).to_integral_value())
    try:
        dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise TimeshiftError(
            f"'{name}' must be epoch seconds/milliseconds or ISO8601, got {value!r}"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    delta = dt - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return (micros * timescale + 500_000) // 1_000_000


@dataclass(frozen=True)
class TimeWindow:
    """A resolved, segment-snapped startover/catchup range (inclusive of
    both global indices). `ended` -> the whole range is in the past, so the
    manifest is a finished VOD; otherwise it is still growing."""

    first_global: int
    last_global: int
    ended: bool
    origin_loop: int  # loop number containing first_global (rseg origin)


def global_index_at(
    abs_ticks: int,
    epoch_ticks: int,
    total_loop_duration_ticks: int,
    segment_boundary_ticks: list[int],
    segments_per_loop: int,
) -> int:
    pos = compute_loop_position(abs_ticks, epoch_ticks, total_loop_duration_ticks)
    seg = segment_index_for_position(pos.position_in_loop_ticks, segment_boundary_ticks)
    return global_segment_number(pos.loop_number, seg, segments_per_loop)


def resolve_window(
    *,
    start_ticks: int,
    end_ticks: int | None,
    now_ticks: int,
    epoch_ticks: int,
    total_loop_duration_ticks: int,
    segment_boundary_ticks: list[int],
    segments_per_loop: int,
    max_span_ticks: int,
    full_loop: bool = False,
) -> TimeWindow:
    D = total_loop_duration_ticks
    if start_ticks < epoch_ticks:
        raise TimeshiftError("start is before the channel epoch")
    if start_ticks > now_ticks:
        raise TimeshiftError("start is in the future")
    if end_ticks is not None and end_ticks <= start_ticks:
        raise TimeshiftError("end must be after start")

    if full_loop:
        start_ticks = epoch_ticks + ((start_ticks - epoch_ticks) // D) * D
        if end_ticks is not None:
            end_ticks = epoch_ticks + -(-(end_ticks - epoch_ticks) // D) * D
    if end_ticks is None:
        end_ticks = start_ticks + max_span_ticks
        if full_loop:
            loops = max_span_ticks // D
            if loops < 1:
                raise TimeshiftError("a single loop is longer than the maximum span")
            end_ticks = start_ticks + loops * D
    if end_ticks - start_ticks > max_span_ticks:
        raise TimeshiftError(
            f"requested range is {(end_ticks - start_ticks) // 90000}s (after snapping), "
            f"longer than the maximum span of {max_span_ticks // 90000}s"
        )

    def at(t: int) -> int:
        return global_index_at(t, epoch_ticks, D, segment_boundary_ticks, segments_per_loop)

    first = at(start_ticks)
    end_global = at(end_ticks - 1)  # segment containing the last tick of the range
    now_global = at(now_ticks)
    return TimeWindow(
        first_global=first,
        last_global=min(end_global, now_global),
        ended=end_ticks <= now_ticks,
        origin_loop=first // segments_per_loop,
    )
