"""`[query]` / `[timeshift]` channel config -> loop-dee-loop serve.py flags and
CDN cache-key inputs (loop-dee-loop/SCOPE.md §13).

The channel's manifest URLs accept query parameters: startover/catchup
(`start`, `end`, `full_loops`, `offset`) and per-request overrides of how the
manifest is rendered (`timeline_mode`, `epoch`, `dvr_window`, `cue_tags`, ...).
Anything in front of serve.py (CloudFront) must key manifest caching on exactly
those names -- which is why the names live in one place and are derived here
for every consumer.

    [query]      timeshift, overrides, prefix, start_param, end_param
    [timeshift]  max_span_seconds (longest startover/catchup range, also caps dvr_window)

Two independent switches, TOML only (they gate the parameters, so they are not
parameters themselves): `timeshift` (`start`, `end`, `full_loops`, `offset`:
startover/catchup) and `overrides` (every other parameter: per-request overrides
of how a manifest is rendered). A switch that is off means its parameters are
ignored. `prefix` is prepended to every parameter name, so a caller that uses some of
them itself (an SSAI service with the channel as its origin, say) cannot clash.
"""

from __future__ import annotations

import re

DEFAULTS = {
    "timeshift": True,
    "overrides": True,
    "prefix": "",
    "start_param": "start",
    "end_param": "end",
    "max_span_seconds": 21600,
}

# Fixed-name parameters, in documentation order. Mirrors
# loop-dee-loop/timeshift.py FIXED_PARAMS (tests/test_query_params_parity.py
# keeps the two in lockstep).
FIXED_PARAMS = (
    "full_loops", "timeline_mode", "offset", "epoch", "dvr_window",
    "timeline_style", "addressing", "cue_tags", "descriptor_grouping",
    "increment_event_ids", "signal_format", "id_template", "split_on",
    "max_height", "max_bitrate",
)

# The startover/catchup parameters (with `start`/`end`); every other fixed name
# is a per-request override, gated by `[query] overrides`. Mirrors
# loop-dee-loop/timeshift.py TIMESHIFT_FIXED_PARAMS / OVERRIDE_PARAMS.
TIMESHIFT_FIXED_PARAMS = ("full_loops", "offset")
OVERRIDE_PARAMS = tuple(p for p in FIXED_PARAMS if p not in TIMESHIFT_FIXED_PARAMS)

_PREFIX = re.compile(r"^[A-Za-z0-9_.~-]*$")

# CloudFront rejects a cache-policy query-string allow-list longer than this
# (its default quota); past it the manifest policy keys on the whole query
# string, which is harmless for manifests (they are cached for a second at most).
CLOUDFRONT_MAX_ALLOW_LIST = 10


def query_settings(cfg: dict) -> dict:
    """Merged + validated `[query]` and `[timeshift]` tables."""
    merged = {**DEFAULTS, **cfg.get("query", {}), **{k: v for k, v in cfg.get("timeshift", {}).items() if k == "max_span_seconds"}}
    prefix = str(merged["prefix"])
    if not _PREFIX.match(prefix):
        raise ValueError(f"[query] prefix must be URL-safe (letters, digits, _ . ~ -), got {prefix!r}")
    names = [f"{prefix}{n}" for n in (merged["start_param"], merged["end_param"], *FIXED_PARAMS)]
    if any(not str(merged[k]) for k in ("start_param", "end_param")) or len(set(names)) != len(names):
        raise ValueError(f"[query] parameter names must be non-empty and distinct, got {names}")
    if int(merged["max_span_seconds"]) < 1:
        raise ValueError("[timeshift] max_span_seconds must be >= 1")
    merged["timeshift"] = bool(merged["timeshift"])
    merged["overrides"] = bool(merged["overrides"])
    merged["prefix"] = prefix
    merged["max_span_seconds"] = int(merged["max_span_seconds"])
    return merged


def query_param_names(cfg: dict) -> list[str]:
    """Query parameter names (prefix applied) a CDN must key manifest caching
    on: `start`/`end`/`full_loops`/`offset` with `timeshift`, the rest with
    `overrides` (in documentation order); [] when both are off."""
    s = query_settings(cfg)
    names = [s["start_param"], s["end_param"]] if s["timeshift"] else []
    names += [p for p in FIXED_PARAMS if (s["timeshift"] if p in TIMESHIFT_FIXED_PARAMS else s["overrides"])]
    return [f"{s['prefix']}{n}" for n in names]


def query_serve_args(cfg: dict) -> list[str]:
    """serve.py CLI flags ([] when both switches are off)."""
    s = query_settings(cfg)
    if not (s["timeshift"] or s["overrides"]):
        return []
    args = []
    if s["timeshift"]:
        args += ["--timeshift", "--timeshift-start-param", s["start_param"], "--timeshift-end-param", s["end_param"]]
    if s["overrides"]:
        args.append("--query-overrides")
    args += ["--timeshift-max-span-seconds", str(s["max_span_seconds"])]
    if s["prefix"]:
        args += ["--query-prefix", s["prefix"]]
    return args
