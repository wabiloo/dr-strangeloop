"""Expected boundary crossings, derived from the channel's own /timeline.json.

The channel says which Period ids and HLS discontinuities exist in its window.
We poll it during a run; whatever shows up after the players started is a
boundary the players should cross, so we never have to forecast the schedule.
"""

from __future__ import annotations

import json
import urllib.request


def fetch_timeline(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


class BoundaryTracker:
    def __init__(self) -> None:
        self._baseline_periods: set[str] | None = None
        self._baseline_discs: set[int] | None = None
        self._periods: set[str] = set()
        self._discs: set[int] = set()
        self.polls = 0
        self.errors = 0
        # Continuous timeline: the loop wrap is no boundary, only forced signal-break Periods are.
        self.continuous = False

    def update(self, doc: dict) -> None:
        self.polls += 1
        self.continuous = doc.get("timeline") == "continuous"
        self._periods |= {p["id"] for p in doc.get("periods", [])}
        self._discs |= {d["segment"] for d in doc.get("discontinuities", [])}

    def mark_start(self) -> None:
        """Everything seen so far is background; only later arrivals count."""
        self._baseline_periods = set(self._periods)
        self._baseline_discs = set(self._discs)

    @property
    def started(self) -> bool:
        return self._baseline_periods is not None

    def new_periods(self) -> int:
        return len(self._periods - (self._baseline_periods or set()))

    def new_discontinuities(self) -> int:
        return len(self._discs - (self._baseline_discs or set()))

    def crossed(self, fmt: str) -> int:
        """Boundaries a player of this format should have crossed since the start."""
        return self.new_discontinuities() if fmt == "hls" else self.new_periods()
