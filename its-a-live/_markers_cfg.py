"""`[markers]` channel config -> loop-dee-loop bake.py / serve.py flags.

    [markers]       increment_event_ids, split_on, announce_ahead, announce_on
    [markers.hls]   cue_tags, descriptor_grouping, id_template
    [markers.dash]  descriptor_grouping, signal_format

HLS and DASH-specific settings live in their own sub-table; the same names
(`cue_tags`, `descriptor_grouping`, ...) are also per-request query parameters.
bake.py/serve.py keep their own flag names (`--daterange-mode`, ...); this
module is the one place that maps the TOML names onto them.
"""

from __future__ import annotations

DEFAULT_ID_TEMPLATE = "{segcode}-{eventid}-{loop}"

CUE_TAGS_CHOICES = ("none", "alongside", "only")
HLS_GROUPING_CHOICES = ("shared", "narrowed", "grouped")
DASH_GROUPING_CHOICES = ("shared", "narrowed")
SIGNAL_FORMAT_CHOICES = ("binary", "xml")


def _choice(table: str, key: str, value, choices: tuple[str, ...]) -> str:
    value = str(value)
    if value not in choices:
        raise ValueError(f"[{table}] {key} must be one of {', '.join(repr(c) for c in choices)}, got {value!r}")
    return value


def split_on_ids(cfg: dict) -> list[int]:
    """`[markers] split_on = [0x22, 0x30, ...]` (ints or "0x22" strings): the
    SCTE-35 segmentation_type_ids that force a Period / discontinuity."""
    raw = cfg.get("markers", {}).get("split_on") or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    ids = []
    for item in raw:
        value = int(item, 0) if isinstance(item, str) else int(item)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"[markers] split_on: {item!r} is not a segmentation_type_id (0..255)")
        ids.append(value)
    return ids


MAX_ANNOUNCE_AHEAD = 3600
SPLICE_INSERT = "splice_insert"


def announce_ahead_seconds(cfg: dict) -> int | float:
    """`[markers] announce_ahead = 10`: seconds (0 = off, at most 3600)."""
    raw = cfg.get("markers", {}).get("announce_ahead", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"[markers] announce_ahead must be a number of seconds (0..{MAX_ANNOUNCE_AHEAD}), got {raw!r}")
    if not 0 <= raw <= MAX_ANNOUNCE_AHEAD:
        raise ValueError(f"[markers] announce_ahead must be between 0 and {MAX_ANNOUNCE_AHEAD} seconds, got {raw!r}")
    return int(raw) if float(raw).is_integer() else float(raw)


def announce_on_items(cfg: dict) -> list[int | str]:
    """`[markers] announce_on = [0x22, "splice_insert"]`: segmentation_type_ids
    (ints or "0x22" strings) and/or the word "splice_insert"; empty = every marker."""
    raw = cfg.get("markers", {}).get("announce_on") or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    items: list[int | str] = []
    for item in raw:
        if isinstance(item, str) and item.strip().lower() == SPLICE_INSERT:
            value: int | str = SPLICE_INSERT
        else:
            try:
                value = int(item, 0) if isinstance(item, str) else int(item)
            except ValueError:
                raise ValueError(
                    f"[markers] announce_on: {item!r} is neither a segmentation_type_id (0..255) nor 'splice_insert'"
                ) from None
            if not 0 <= value <= 0xFF:
                raise ValueError(f"[markers] announce_on: {item!r} is not a segmentation_type_id (0..255)")
        if value not in items:
            items.append(value)
    return items


def markers_settings(cfg: dict) -> dict:
    """Merged + validated markers config, nested like the TOML."""
    markers = cfg.get("markers", {})
    hls, dash = markers.get("hls", {}), markers.get("dash", {})
    return {
        "increment_event_ids": bool(markers.get("increment_event_ids", False)),
        "split_on": split_on_ids(cfg),
        "announce_ahead": announce_ahead_seconds(cfg),
        "announce_on": announce_on_items(cfg),
        "hls": {
            "cue_tags": _choice("markers.hls", "cue_tags", hls.get("cue_tags", "none"), CUE_TAGS_CHOICES),
            "descriptor_grouping": _choice(
                "markers.hls", "descriptor_grouping", hls.get("descriptor_grouping", "shared"), HLS_GROUPING_CHOICES
            ),
            "id_template": str(hls.get("id_template", DEFAULT_ID_TEMPLATE)),
        },
        "dash": {
            "descriptor_grouping": _choice(
                "markers.dash", "descriptor_grouping", dash.get("descriptor_grouping", "shared"), DASH_GROUPING_CHOICES
            ),
            "signal_format": _choice(
                "markers.dash", "signal_format", dash.get("signal_format", "binary"), SIGNAL_FORMAT_CHOICES
            ),
        },
    }


def markers_bake_args(cfg: dict) -> list[str]:
    """bake.py flags: the shape of the SCTE-35 signaling recorded in the loop
    package (serve.py reads it back as the default for each request)."""
    m = markers_settings(cfg)
    args = [
        "--daterange-mode", m["hls"]["descriptor_grouping"],
        "--cue-tags", m["hls"]["cue_tags"],
        "--daterange-id-format", m["hls"]["id_template"],
        "--dash-signal-format", m["dash"]["signal_format"],
        "--dash-descriptor-mode", m["dash"]["descriptor_grouping"],
    ]
    if m["increment_event_ids"]:
        args.append("--increment-event-ids")
    return args


def split_on_serve_args(cfg: dict) -> list[str]:
    """loop-dee-loop/SCOPE.md §14: `[markers] split_on = [0x22, 0x30, ...]`
    forces a signal-only Period (DASH) / #EXT-X-DISCONTINUITY (HLS) at the
    segment of every marker with one of those SCTE-35 segmentation_type_ids.
    Works with or without `continuous`; empty/absent = no forced breaks."""
    ids = split_on_ids(cfg)
    return ["--period-on-segmentation", ",".join(f"0x{i:02X}" for i in ids)] if ids else []


def announce_serve_args(cfg: dict) -> list[str]:
    """`[markers] announce_ahead = 10` / `announce_on = [0x22, "splice_insert"]`:
    serve.py announces markers (HLS DATERANGE / DASH Event) that many seconds
    before the segment they fall in. Off (no flags) when announce_ahead is 0;
    announce_on empty = every marker."""
    ahead = announce_ahead_seconds(cfg)
    if not ahead:
        announce_on_items(cfg)  # still validate
        return []
    args = ["--announce-ahead", str(ahead)]
    items = announce_on_items(cfg)
    if items:
        args += ["--announce-on", ",".join(i if isinstance(i, str) else f"0x{i:02X}" for i in items)]
    return args
