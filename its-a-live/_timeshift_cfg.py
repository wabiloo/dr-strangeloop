"""`[timeshift]` channel config -> loop-dee-loop serve.py flags and CDN
cache-key inputs (loop-dee-loop/SCOPE.md §13).

Startover/catchup is served from the normal manifest URLs via query
parameters, so anything in front of serve.py (CloudFront) must key manifest
caching on exactly those parameter names -- which is why the names live in
one place and are derived here for every consumer.
"""

from __future__ import annotations

DEFAULTS = {
    "enabled": True,
    "start_param": "start",
    "end_param": "end",
    "full_loop_param": "full_loop",
    "max_span_seconds": 21600,
}

_PARAM_KEYS = ("start_param", "end_param", "full_loop_param")

# Fixed name of the per-request timeline-mode override (timeline=default|
# continuous|discontinuous) -- not configurable, mirrors loop-dee-loop's
# timeshift.TIMELINE_PARAM.
TIMELINE_PARAM = "timeline"


def timeshift_settings(cfg: dict) -> dict:
    """Merged + validated `[timeshift]` table."""
    merged = {**DEFAULTS, **cfg.get("timeshift", {})}
    names = [str(merged[k]) for k in _PARAM_KEYS] + [TIMELINE_PARAM]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError(f"[timeshift] parameter names must be non-empty and distinct, got {names}")
    if int(merged["max_span_seconds"]) < 1:
        raise ValueError("[timeshift] max_span_seconds must be >= 1")
    merged["enabled"] = bool(merged["enabled"])
    merged["max_span_seconds"] = int(merged["max_span_seconds"])
    return merged


def timeshift_param_names(cfg: dict) -> list[str]:
    """Query parameter names a CDN must key manifest caching on ([] if off)."""
    s = timeshift_settings(cfg)
    return [*(s[k] for k in _PARAM_KEYS), TIMELINE_PARAM] if s["enabled"] else []


def timeshift_serve_args(cfg: dict) -> list[str]:
    """serve.py CLI flags ([] if timeshift is disabled)."""
    s = timeshift_settings(cfg)
    if not s["enabled"]:
        return []
    return [
        "--timeshift",
        "--timeshift-start-param", s["start_param"],
        "--timeshift-end-param", s["end_param"],
        "--timeshift-full-loop-param", s["full_loop_param"],
        "--timeshift-max-span-seconds", str(s["max_span_seconds"]),
    ]
