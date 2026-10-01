"""`[packaging]` channel config -> loop-dee-loop serve.py flags."""

from __future__ import annotations


def period_on_segmentation_serve_args(cfg: dict) -> list[str]:
    """loop-dee-loop/SCOPE.md §14: `[packaging] period_on_segmentation =
    [0x22, 0x30, ...]` (ints or "0x22" strings) forces a signal-only
    Period / #EXT-X-DISCONTINUITY at the segment of every marker with one of
    those SCTE-35 segmentation_type_ids. Works with or without
    `[timeline] continuous`; empty/absent = today's behavior."""
    raw = cfg.get("packaging", {}).get("period_on_segmentation") or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    ids = []
    for item in raw:
        value = int(item, 0) if isinstance(item, str) else int(item)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"[packaging] period_on_segmentation: {item!r} is not a segmentation_type_id (0..255)")
        ids.append(f"0x{value:02X}")
    if not ids:
        return []
    apply_to = str(cfg.get("packaging", {}).get("period_on_segmentation_apply", "both")).lower()
    if apply_to not in ("both", "dash", "hls"):
        raise ValueError(
            f"[packaging] period_on_segmentation_apply must be 'both', 'dash' or 'hls', got {apply_to!r}"
        )
    return [
        "--period-on-segmentation", ",".join(ids),
        "--period-on-segmentation-apply", apply_to,
    ]
