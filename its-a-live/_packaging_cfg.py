"""`[packaging]` channel config -> loop-dee-loop bake.py / serve.py flags.

    [packaging]       segment_duration, dvr_window
    [packaging.hls]   container, mux_audio
    [packaging.dash]  timeline_style, addressing

`container`/`mux_audio` decide the baked segment bytes, so they are TOML-only
(no query parameter); `timeline_style`/`addressing` only change the DASH
manifest and are also per-request query parameters.
"""

from __future__ import annotations

CONTAINER_CHOICES = ("cmaf", "ts")
TIMELINE_STYLE_CHOICES = ("full", "compact")
ADDRESSING_CHOICES = ("number", "time")


def _choice(table: str, key: str, value, choices: tuple[str, ...]) -> str:
    value = str(value).lower()
    if value not in choices:
        raise ValueError(f"[{table}] {key} must be one of {', '.join(repr(c) for c in choices)}, got {value!r}")
    return value


def packaging_settings(cfg: dict) -> dict:
    """Merged + validated packaging config, nested like the TOML."""
    packaging = cfg.get("packaging", {})
    hls, dash = packaging.get("hls", {}), packaging.get("dash", {})
    segment_duration = float(packaging.get("segment_duration", 4.0))
    dvr_window = float(packaging.get("dvr_window", 30))
    if segment_duration <= 0:
        raise ValueError("[packaging] segment_duration must be > 0")
    if dvr_window <= 0:
        raise ValueError("[packaging] dvr_window must be > 0")
    return {
        "segment_duration": segment_duration,
        "dvr_window": dvr_window,
        "hls": {
            "container": _choice("packaging.hls", "container", hls.get("container", "cmaf"), CONTAINER_CHOICES),
            "mux_audio": bool(hls.get("mux_audio", True)),
        },
        "dash": {
            "timeline_style": _choice(
                "packaging.dash", "timeline_style", dash.get("timeline_style", "full"), TIMELINE_STYLE_CHOICES
            ),
            "addressing": _choice("packaging.dash", "addressing", dash.get("addressing", "number"), ADDRESSING_CHOICES),
        },
    }


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def packaging_bake_args(cfg: dict) -> list[str]:
    """bake.py flags: segment duration and the HLS container."""
    p = packaging_settings(cfg)
    return [
        "--segment-duration", str(cfg.get("packaging", {}).get("segment_duration", 4.0)),
        "--hls-format", p["hls"]["container"],
        "--hls-ts-mux-audio" if p["hls"]["mux_audio"] else "--no-hls-ts-mux-audio",
    ]


def dvr_window_seconds(cfg: dict) -> str:
    """serve.py `--dvr-window-seconds` value (the TOML `dvr_window`)."""
    return _number(packaging_settings(cfg)["dvr_window"])


def dash_serve_args(cfg: dict) -> list[str]:
    """serve.py flags for the DASH manifest style; nothing when both are the
    defaults (`addressing = "number"`, `timeline_style = "full"`)."""
    dash = packaging_settings(cfg)["dash"]
    args: list[str] = []
    if dash["addressing"] != "number":
        args += ["--dash-addressing", dash["addressing"]]
    if dash["timeline_style"] != "full":
        args += ["--dash-timeline", dash["timeline_style"]]
    return args
