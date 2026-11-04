"""`[packaging]` channel config -> loop-dee-loop bake.py / serve.py flags.

    [packaging]       segment_duration, dvr_window, chunk_duration
    [packaging.hls]   container, mux_audio, low_latency
    [packaging.dash]  timeline_style, addressing,
                      low_latency, target_latency, utc_timing, utc_timing_url

`container`/`mux_audio` decide the baked segment bytes, so they are TOML-only
(no query parameter); `timeline_style`/`addressing` only change the DASH
manifest and are also per-request query parameters.

Low latency (docs/low-latency.md): `[packaging.dash] low_latency` (DASH, chunked delivery) and
`[packaging.hls] low_latency` (Apple LL-HLS: parts, blocking reload) are each "off" (default), "on" (advertised in
every manifest of that format) or "optin" (delivered, but advertised only on `?low_latency=true`, which needs
`[query] overrides`). Both are made from the same baked chunks, so `[packaging] chunk_duration` -- a bake-time
property of the segments, TOML-only -- is shared. LL-HLS needs `container = "cmaf"`. `low_latency` is also a
per-request query parameter; `target_latency` and `utc_timing` are DASH-only (also query parameters).
`utc_timing` is "direct" (default: the time in the MPD, which is then served no-store), "http" (a polled /time; its
URL is filled in at deploy time from the channel's public address unless `utc_timing_url` is set) or "none". The URL
itself is configuration only: a query parameter must not choose viewers' clock source.
"""

from __future__ import annotations

from _query_cfg import LL_HLS_PROTOCOL_PARAMS, query_param_names, query_settings

CONTAINER_CHOICES = ("cmaf", "ts")
TIMELINE_STYLE_CHOICES = ("full", "compact")
ADDRESSING_CHOICES = ("number", "time")
LOW_LATENCY_CHOICES = ("off", "on", "optin")
UTC_TIMING_CHOICES = ("direct", "http", "none")
DEFAULT_CHUNK_DURATION = 0.5
MAX_TARGET_LATENCY = 120.0
# Held responses (chunks released on the wall clock) need a long-lived server process: not the one-request-per-
# environment Lambda backend, and not MediaPackage v1.
LOW_LATENCY_BACKENDS = ("ecs-express", "local-docker")


def _choice(table: str, key: str, value, choices: tuple[str, ...]) -> str:
    value = str(value).lower()
    if value not in choices:
        raise ValueError(f"[{table}] {key} must be one of {', '.join(repr(c) for c in choices)}, got {value!r}")
    return value


def _low_latency(value, table: str) -> str:
    """`low_latency` accepts the TOML booleans too: true = "on", false = "off"."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return _choice(table, "low_latency", value, LOW_LATENCY_CHOICES)


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
    if "chunk_duration" in dash:
        raise ValueError(
            "[packaging.dash] chunk_duration moved to [packaging] chunk_duration: the baked chunks are shared by "
            "low-latency DASH and low-latency HLS"
        )
    low_latency = _low_latency(dash.get("low_latency", "off"), "packaging.dash")
    hls_low_latency = _low_latency(hls.get("low_latency", "off"), "packaging.hls")
    container = _choice("packaging.hls", "container", hls.get("container", "cmaf"), CONTAINER_CHOICES)
    chunk_duration = float(packaging.get("chunk_duration", DEFAULT_CHUNK_DURATION))
    target = dash.get("target_latency")
    target_latency = None if target is None else float(target)
    utc_timing = _choice("packaging.dash", "utc_timing", dash.get("utc_timing", "direct"), UTC_TIMING_CHOICES)
    utc_timing_url = str(dash.get("utc_timing_url", "")).strip()
    if "off" != low_latency or "off" != hls_low_latency:
        if not 0 < chunk_duration < segment_duration:
            raise ValueError(
                f"[packaging] chunk_duration must be > 0 and shorter than [packaging] segment_duration "
                f"({segment_duration:g}), got {chunk_duration:g}"
            )
        for table, mode in (("packaging.dash", low_latency), ("packaging.hls", hls_low_latency)):
            if mode == "optin" and not query_settings(cfg)["overrides"]:
                raise ValueError(f"[{table}] low_latency = \"optin\" needs [query] overrides = true (?low_latency=true)")
    if hls_low_latency != "off" and container != "cmaf":
        raise ValueError("[packaging.hls] low_latency needs container = \"cmaf\" (MPEG-TS segments cannot carry parts)")
    if target_latency is not None and not 0 < target_latency <= MAX_TARGET_LATENCY:
        raise ValueError(f"[packaging.dash] target_latency must be > 0 and at most {MAX_TARGET_LATENCY:g} seconds")
    if utc_timing_url:
        if utc_timing != "http":
            raise ValueError("[packaging.dash] utc_timing_url only applies to utc_timing = \"http\"")
        if not utc_timing_url.startswith(("http://", "https://")):
            raise ValueError("[packaging.dash] utc_timing_url must be an absolute http(s) URL (DASH does not allow a relative one)")
    return {
        "segment_duration": segment_duration,
        "dvr_window": dvr_window,
        "chunk_duration": chunk_duration,
        "hls": {
            "container": container,
            "mux_audio": bool(hls.get("mux_audio", True)),
            "low_latency": hls_low_latency,
        },
        "dash": {
            "timeline_style": _choice(
                "packaging.dash", "timeline_style", dash.get("timeline_style", "full"), TIMELINE_STYLE_CHOICES
            ),
            "addressing": _choice("packaging.dash", "addressing", dash.get("addressing", "number"), ADDRESSING_CHOICES),
            "low_latency": low_latency,
            "target_latency": target_latency,
            "utc_timing": utc_timing,
            "utc_timing_url": utc_timing_url,
        },
    }


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def packaging_bake_args(cfg: dict) -> list[str]:
    """bake.py flags: segment duration, the HLS container and, when either format is low-latency, the chunk size."""
    p = packaging_settings(cfg)
    args = [
        "--segment-duration", str(cfg.get("packaging", {}).get("segment_duration", 4.0)),
        "--hls-format", p["hls"]["container"],
        "--hls-ts-mux-audio" if p["hls"]["mux_audio"] else "--no-hls-ts-mux-audio",
    ]
    if p["dash"]["low_latency"] != "off" or p["hls"]["low_latency"] != "off":
        args += ["--chunk-duration", _number(p["chunk_duration"])]
    return args


def dvr_window_seconds(cfg: dict) -> str:
    """serve.py `--dvr-window-seconds` value (the TOML `dvr_window`)."""
    return _number(packaging_settings(cfg)["dvr_window"])


def check_low_latency(cfg: dict, backend: str) -> None:
    """Raise ValueError when low latency is configured on a backend that cannot deliver it."""
    p = packaging_settings(cfg)
    configured = [t for t, mode in (("packaging.dash", p["dash"]["low_latency"]), ("packaging.hls", p["hls"]["low_latency"])) if mode != "off"]
    if configured and backend not in LOW_LATENCY_BACKENDS:
        raise ValueError(
            f"[{configured[0]}] low_latency is not supported on the {backend} backend "
            f"(only {', '.join(LOW_LATENCY_BACKENDS)}): its responses are held open while chunks are released"
        )


def needs_public_url(cfg: dict) -> bool:
    """Whether the serve flags need the channel's public address, which only exists once the channel is
    deployed: low-latency DASH with `utc_timing = "http"` and no explicit `utc_timing_url`."""
    dash = packaging_settings(cfg)["dash"]
    return dash["low_latency"] != "off" and dash["utc_timing"] == "http" and not dash["utc_timing_url"]


def dash_serve_args(cfg: dict) -> list[str]:
    """serve.py flags for the DASH manifest style. Nothing for the defaults (`addressing = "number"`,
    `timeline_style = "full"`). Low-latency flags are `ll_serve_args`."""
    dash = packaging_settings(cfg)["dash"]
    args: list[str] = []
    if dash["addressing"] != "number":
        args += ["--dash-addressing", dash["addressing"]]
    if dash["timeline_style"] != "full":
        args += ["--dash-timeline", dash["timeline_style"]]
    return args


def ll_serve_args(cfg: dict, *, public_url: str | None = None) -> list[str]:
    """serve.py low-latency flags: `--ll-dash` / `--ll-hls` advertise it in that format's manifests, `--ll-optin`
    delivers it but advertises only on request (either format), and the DASH-only target latency and clock
    source. `public_url` is the channel's public base URL (no trailing slash), used for `utc_timing = "http"`
    when no `utc_timing_url` is configured. Nothing when both formats are off."""
    p = packaging_settings(cfg)
    dash, hls = p["dash"], p["hls"]
    args: list[str] = []
    if dash["low_latency"] == "on":
        args.append("--ll-dash")
    if hls["low_latency"] == "on":
        args.append("--ll-hls")
    if "optin" in (dash["low_latency"], hls["low_latency"]):
        args.append("--ll-optin")
    if dash["low_latency"] != "off":
        if dash["target_latency"] is not None:
            args += ["--ll-target-latency", _number(dash["target_latency"])]
        args += ["--ll-utc-timing", dash["utc_timing"]]
        url = dash["utc_timing_url"] or (f"{public_url.rstrip('/')}/time" if public_url else "")
        if dash["utc_timing"] == "http" and url:
            args += ["--ll-utc-timing-url", url]
    return args


def cdn_query_names(cfg: dict) -> list[str]:
    """The query parameter names a CDN must key manifest caching on: `_query_cfg.query_param_names` plus, for a
    low-latency HLS channel, the blocking-reload parameters (spec-fixed names, never prefixed). Without them a
    narrow allow-list (e.g. `[query] overrides = false`) would drop `_HLS_msn`/`_HLS_part` and every blocking
    reload would be answered with the same cached playlist."""
    names = query_param_names(cfg)
    if packaging_settings(cfg)["hls"]["low_latency"] != "off":
        names = [*names, *(p for p in LL_HLS_PROTOCOL_PARAMS if p not in names)]
    return names
