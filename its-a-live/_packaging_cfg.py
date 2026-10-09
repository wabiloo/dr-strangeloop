"""`[packaging]` channel config -> loop-dee-loop serve.py flags."""

from __future__ import annotations

DASH_ADDRESSING_CHOICES = ("number", "time")


def dash_addressing_serve_args(cfg: dict) -> list[str]:
    """`[packaging] dash_addressing = "number" | "time"`: DASH segment
    addressing, `$Number$` + startNumber (default) or `$Time$` (each
    `<S t>`). HLS is unaffected. Absent/"number" = today's behavior."""
    value = str(cfg.get("packaging", {}).get("dash_addressing", "number")).lower()
    if value not in DASH_ADDRESSING_CHOICES:
        raise ValueError(f"[packaging] dash_addressing must be 'number' or 'time', got {value!r}")
    return [] if value == "number" else ["--dash-addressing", value]
