"""Startup helpers shared by the CLI and wsgi.py: reading the package descriptor, the epoch, the DVR window."""

from __future__ import annotations

import datetime as _dt
import json
import math
from pathlib import Path


def read_package_descriptor(package_dir: Path) -> dict:
    return json.loads((package_dir / "loop_descriptor.json").read_text())


def resolve_epoch_ticks(epoch_utc: str, timescale: int) -> int:
    """Convert an ISO8601 UTC epoch string to an integer tick count --
    callers should do this exactly once and feed the result into every
    subsequent request's arithmetic, never recompute it via accumulation."""
    epoch_dt = _dt.datetime.strptime(epoch_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )
    return round(epoch_dt.timestamp() * timescale)


def resolve_window_segments(
    package_descriptor: dict,
    *,
    dvr_window_seconds: float = 30.0,
    window_segments: int | None = None,
) -> int:
    """Exact segment count if given, else derived from a DVR-window target
    using the package's nominal segment duration from bake time."""
    if window_segments is not None:
        return window_segments
    nominal_segment_duration_seconds = float(package_descriptor["segment_duration_seconds"])
    return max(1, math.ceil(dvr_window_seconds / nominal_segment_duration_seconds))
