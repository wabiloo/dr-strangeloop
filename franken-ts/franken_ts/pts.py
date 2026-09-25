from __future__ import annotations

import logging
from pathlib import Path

from .timeline import AdBoundary
from .utils import run_cmd

logger = logging.getLogger(__name__)

PTS_CLOCK = 90_000  # 90 kHz MPEG-TS clock


def _load_idr_timestamps(ts_file: Path) -> list[float]:
    """Return sorted list of IDR frame timestamps (in seconds) from a TS file."""
    result = run_cmd(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_frames",
            "-show_entries", "frame=key_frame,best_effort_timestamp_time",
            "-of", "csv",
            str(ts_file),
        ],
        capture=True,
    )

    timestamps: list[float] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        # csv format: frame,<key_frame>,<best_effort_timestamp_time>
        if len(parts) >= 3 and parts[1] == "1":
            try:
                timestamps.append(float(parts[2]))
            except ValueError:
                pass

    timestamps.sort()
    logger.debug("Found %d IDR frames in %s", len(timestamps), ts_file)
    return timestamps


def find_idr_pts(
    ts_file: Path,
    boundaries: list[AdBoundary],
    framerate: int,
    gop: int = 50,
) -> tuple[dict[tuple[int, bool], int], float]:
    """Find the actual IDR PTS ticks for each ad boundary.

    The MPEG-TS muxer adds a uniform PTS offset to all streams to avoid
    negative DTS values (from audio AAC encoder delay and similar sources).
    We detect this offset from the first IDR — which corresponds to the
    forced keyframe at filter t=0 — and add it to every expected position
    before searching.

    Returns:
        Tuple of (pts_map, muxer_offset_seconds) where:
        pts_map         maps (marker_index, is_start) → PTS ticks (90kHz)
          muxer_offset_s  the uniform PTS offset the muxer added (seconds)
    """
    idr_times = _load_idr_timestamps(ts_file)
    if not idr_times:
        raise RuntimeError(f"No IDR frames found in {ts_file}")

    # The forced keyframe at filter t=0 becomes the first IDR in the stream.
    # Any muxer PTS offset shifts ALL IDR timestamps uniformly, so:
    #   actual_IDR_pts = filter_time + muxer_offset
    muxer_offset = idr_times[0]
    logger.info("Muxer PTS offset: %.6fs (%d ticks)", muxer_offset,
                round(muxer_offset * PTS_CLOCK))

    frame_duration = 1.0 / framerate
    gop_duration = gop / framerate  # e.g. 2.0s for GOP=50 @ 25fps
    result: dict[tuple[int, bool], int] = {}

    for boundary in boundaries:
        # Shift the expected position by the muxer offset so we search in
        # the same PTS space as the actual IDR timestamps.
        expected = boundary.output_time + muxer_offset
        key = ("marker", boundary.marker_index, boundary.is_start)
        label = f"event {boundary.event_id} ({'start' if boundary.is_start else 'stop'})"

        candidates = [t for t in idr_times if abs(t - expected) <= gop_duration]
        if not candidates:
            raise RuntimeError(
                f"No IDR frame found within {gop_duration:.1f}s of "
                f"{expected:.3f}s for {label}"
            )

        nearest = min(candidates, key=lambda t: abs(t - expected))
        drift = abs(nearest - expected)

        if drift > frame_duration:
            logger.warning(
                "%s: IDR at %.6fs, expected %.6fs (drift %.3fs)",
                label, nearest, expected, drift,
            )
        else:
            logger.debug("%s: IDR at %.6fs (expected %.6fs, drift %.6fs)",
                         label, nearest, expected, drift)

        result[key] = round(nearest * PTS_CLOCK)

    return result, muxer_offset
