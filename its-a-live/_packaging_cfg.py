"""`[packaging]` channel config -> loop-dee-loop serve.py flags (DASH addressing and
SegmentTimeline style: `dash_addressing`, `dash_timeline`)."""

from __future__ import annotations

DASH_ADDRESSING_CHOICES = ("number", "time")
DASH_TIMELINE_CHOICES = ("full", "compact")


def dash_addressing_serve_args(cfg: dict) -> list[str]:
    """`[packaging] dash_addressing = "number" | "time"`: DASH segment
    addressing, `$Number$` + startNumber (default) or `$Time$` (each
    `<S t>`). HLS is unaffected. Absent/"number" = today's behavior."""
    value = str(cfg.get("packaging", {}).get("dash_addressing", "number")).lower()
    if value not in DASH_ADDRESSING_CHOICES:
        raise ValueError(f"[packaging] dash_addressing must be 'number' or 'time', got {value!r}")
    args = [] if value == "number" else ["--dash-addressing", value]
    timeline = str(cfg.get("packaging", {}).get("dash_timeline", "full")).lower()
    if timeline not in DASH_TIMELINE_CHOICES:
        raise ValueError(f"[packaging] dash_timeline must be 'full' or 'compact', got {timeline!r}")
    return args if timeline == "full" else [*args, "--dash-timeline", timeline]
