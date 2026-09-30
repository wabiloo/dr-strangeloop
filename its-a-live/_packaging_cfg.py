"""`[packaging]` channel config -> loop-dee-loop serve.py flags."""

from __future__ import annotations


def period_on_segmentation_serve_args(cfg: dict) -> list[str]:
    """loop-dee-loop/SCOPE.md §14: `[packaging] period_on_segmentation =
    [0x22, 0x30, ...]` (ints or "0x22" strings) forces a signal-only
    Period / #EXT-X-DISCONTINUITY at the segment of every marker with one of
    those SCTE-35 segmentation_type_ids. Works with or without
    `continuous_timeline`; empty/absent = today's behavior."""
    raw = cfg.get("packaging", {}).get("period_on_segmentation") or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    ids = []
    for item in raw:
        value = int(item, 0) if isinstance(item, str) else int(item)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"[packaging] period_on_segmentation: {item!r} is not a segmentation_type_id (0..255)")
        ids.append(f"0x{value:02X}")
    return ["--period-on-segmentation", ",".join(ids)] if ids else []
