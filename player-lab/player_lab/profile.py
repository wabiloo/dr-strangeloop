"""Thresholds that turn raw numbers into pass/fail. Raw numbers are always reported."""

from __future__ import annotations

import statistics
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Thresholds:
    max_startup_s: float = 20.0
    max_stall_count: int = 0
    max_stall_seconds: float = 0.0
    max_errors: int = 0
    max_dropped_frame_ratio: float = 0.05
    # |player-counted transitions - boundaries the timeline says were crossed|
    boundary_tolerance: int = 1
    # Live latency (low-latency channels). Not judged unless a limit is set; the numbers are always reported.
    # Samples before `latency_settle_s` after startup are ignored: players need a while to catch up to their target.
    latency_settle_s: float = 30.0
    max_live_latency_s: float | None = None  # median latency after settling
    max_latency_target_error_s: float | None = None  # |median - the player's own target|, when it has one
    max_latency_jitter_s: float | None = None  # p95 - p5 of the latency after settling


@dataclass(frozen=True)
class Profile:
    default: Thresholds = field(default_factory=Thresholds)
    per_player: dict[str, dict] = field(default_factory=dict)

    def with_default(self, **changes) -> Profile:
        """This profile with some default thresholds replaced (per-player overrides still win)."""
        return replace(self, default=replace(self.default, **changes))

    def for_player(self, player: str, fmt: str) -> Thresholds:
        t = self.default
        for key in (player, f"{player}:{fmt}"):
            if key in self.per_player:
                t = replace(t, **self.per_player[key])
        return t


def load_profile(path: Path | None) -> Profile:
    if path is None:
        return Profile()
    raw = tomllib.loads(Path(path).read_text())
    fields = set(Thresholds.__dataclass_fields__)
    default = {k: v for k, v in raw.items() if k in fields}
    per_player = {k: v for k, v in raw.get("player", {}).items()}
    for name, d in [("default", default), *per_player.items()]:
        bad = set(d) - fields
        if bad:
            raise ValueError(f"{path}: unknown threshold(s) {sorted(bad)} in {name}")
    return Profile(Thresholds(**default), per_player)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


CONVERGED_WITHIN_S = 0.5


def summarize_live(case: dict, th: Thresholds) -> dict | None:
    """Live-latency summary of one case from the observer's `latencyTrace`
    ([t, wall-clock latency, player's own latency, player's target, playback rate] per second), or None
    when the player reported none (VOD, or a player without a live hook).

    The primary figure is the wall-clock one (same definition for every player); the player's own number
    is used only when no player gave a playhead date. Statistics skip the first `latency_settle_s` after startup."""
    trace = case.get("latencyTrace") or []
    started = case.get("startedAfterS")
    if not trace or started is None:
        return None
    wall = any(r[1] is not None for r in trace)
    col = 1 if wall else 2
    after = [r for r in trace if r[0] >= started + th.latency_settle_s]
    vals = sorted(r[col] for r in after if r[col] is not None)
    out: dict = {"source": "wallclock" if wall else "player", "settleS": th.latency_settle_s, "samples": len(vals)}
    targets = [r[3] for r in after if r[3] is not None]
    out["targetS"] = round(statistics.median(targets), 3) if targets else None
    own = [r[2] for r in after if r[2] is not None]
    out["playerMedianS"] = round(statistics.median(own), 3) if own else None
    if vals:
        med = statistics.median(vals)
        p5, p95 = _percentile(vals, 0.05), _percentile(vals, 0.95)
        out.update(medianS=round(med, 3), minS=vals[0], maxS=vals[-1], p5S=round(p5, 3), p95S=round(p95, 3),
                   jitterS=round(p95 - p5, 3))
        out["targetErrorS"] = round(abs(med - out["targetS"]), 3) if out["targetS"] is not None else None
    # When did it first get within 0.5 s of the player's own target (whole run, not just after settling).
    out["convergedAfterS"] = next(
        (round(r[0] - started, 1) for r in trace
         if r[col] is not None and r[3] is not None and abs(r[col] - r[3]) <= CONVERGED_WITHIN_S),
        None,
    )
    rate = case.get("playbackRate")
    if rate:
        out["playbackRate"] = rate
    return out


def judge(case: dict, crossed: int | None, th: Thresholds) -> list[str]:
    """Failure reasons for one case result (empty list = pass)."""
    why: list[str] = []
    if case.get("startedAfterS") is None:
        why.append("never started playing")
    elif case["startedAfterS"] > th.max_startup_s:
        why.append(f"startup {case['startedAfterS']}s > {th.max_startup_s}s")
    if case.get("stallCount", 0) > th.max_stall_count:
        why.append(f"{case['stallCount']} stalls > {th.max_stall_count}")
    if case.get("stallSeconds", 0) > th.max_stall_seconds:
        why.append(f"{case['stallSeconds']}s stalled > {th.max_stall_seconds}s")
    errs = case.get("fatalErrors", 0)
    if errs > th.max_errors:
        why.append(f"{errs} errors > {th.max_errors}")
    total = case.get("totalFrames") or 0
    if total and case.get("droppedFrames", 0) / total > th.max_dropped_frame_ratio:
        why.append(f"dropped {case['droppedFrames']}/{total} frames")
    seen = case.get("periodTransitions")
    if seen is not None and crossed is not None and abs(seen - crossed) > th.boundary_tolerance:
        why.append(f"player saw {seen} transitions, timeline says {crossed} (tolerance {th.boundary_tolerance})")
    if th.max_live_latency_s is not None or th.max_latency_target_error_s is not None or th.max_latency_jitter_s is not None:
        live = case.get("live") or summarize_live(case, th)
        if not live or live.get("samples", 0) < 5:
            why.append(
                f"no live-latency samples after the {th.latency_settle_s:g}s settle period "
                "(run longer, lower latency_settle_s, or the player reports no latency)"
            )
        else:
            if th.max_live_latency_s is not None and live["medianS"] > th.max_live_latency_s:
                why.append(f"live latency {live['medianS']}s > {th.max_live_latency_s}s")
            err = live.get("targetErrorS")
            if th.max_latency_target_error_s is not None and err is not None and err > th.max_latency_target_error_s:
                why.append(f"latency {live['medianS']}s is {err}s from the player's target {live['targetS']}s (> {th.max_latency_target_error_s}s)")
            if th.max_latency_jitter_s is not None and live["jitterS"] > th.max_latency_jitter_s:
                why.append(f"latency jitter {live['jitterS']}s > {th.max_latency_jitter_s}s")
    return why
