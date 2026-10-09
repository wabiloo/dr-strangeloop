"""Thresholds that turn raw numbers into pass/fail. Raw numbers are always reported."""

from __future__ import annotations

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


@dataclass(frozen=True)
class Profile:
    default: Thresholds = field(default_factory=Thresholds)
    per_player: dict[str, dict] = field(default_factory=dict)

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
    return why
