"""Golden snapshots of HLS/DASH manifests over a configuration matrix.

Guards refactors of the manifest builders: every case must stay byte-identical
(sha256 recorded in fixtures/golden_manifests.json). After an *intended*
manifest change, regenerate with `UPDATE_GOLDEN=1 pytest tests/test_serve_golden_manifests.py`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scte35_signaling import build_cue_breaks  # noqa: E402
from test_serve_window_json import LOOP, MARKERS, _channel  # noqa: E402
from timeshift import TimeWindow  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "golden_manifests.json"
REAL_B64 = "/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo="

NOWS = {"start": 100_000, "mid": 4 * LOOP + 100_000, "wrap": 5 * LOOP - 10_000}
WINDOWS = {"live": None, "range": TimeWindow(first_global=2, last_global=9, ended=True, origin_loop=0)}


def _cases():
    for (continuous, boundaries, type_ids, increment, cue_tags, daterange, now, window) in itertools.product(
        (False, True), ((), (2,)), ((), ("0x22",)), (False, True), ("none", "alongside", "only"),
        ("shared", "grouped"), NOWS, WINDOWS,
    ):
        if continuous and boundaries:
            continue  # rejected by Channel._validate_continuous
        yield (continuous, boundaries, type_ids, increment, cue_tags, daterange, now, window)


def _key(case) -> str:
    continuous, boundaries, type_ids, increment, cue_tags, daterange, now, window = case
    return "|".join(
        [
            "cont" if continuous else "per",
            "b" + ",".join(map(str, boundaries)),
            "t" + ",".join(type_ids),
            "inc" if increment else "noinc",
            cue_tags,
            daterange,
            now,
            window,
        ]
    )


def _build(case) -> dict[str, str]:
    continuous, boundaries, type_ids, increment, cue_tags, daterange, now, window = case
    markers = [{**m, "splice_command_b64": REAL_B64, "splice_command_b64_narrowed": REAL_B64} for m in MARKERS]
    frozen = frozenset(int(t, 16) for t in type_ids)
    channel = _channel(
        boundaries=boundaries, markers=markers, now_ticks=NOWS[now], continuous=continuous, type_ids=frozen
    )
    pkg = channel.package
    pkg.increment_event_ids = increment
    pkg.cue_tags = cue_tags
    pkg.daterange_mode = daterange
    pkg.cue_breaks = build_cue_breaks(markers) if cue_tags in ("alongside", "only") else []
    win = WINDOWS[window]
    out = {}
    for name, fn in (
        ("hls", lambda: channel.build_hls_manifest("archive", window=win)),
        ("dash", lambda: channel.build_dash_manifest(window=win)),
    ):
        try:
            text = fn()
        except Exception as exc:  # noqa: BLE001 - unsupported combinations stay stable too
            text = f"EXC {type(exc).__name__}: {exc}"
        out[name] = hashlib.sha256(text.encode()).hexdigest()
    return out


def test_manifests_match_golden():
    got = {_key(c): _build(c) for c in _cases()}
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=0, sort_keys=True) + "\n")
        pytest.skip("golden manifests regenerated")
    want = json.loads(GOLDEN.read_text())
    assert got.keys() == want.keys()
    changed = sorted(f"{k}:{part}" for k in want for part in want[k] if got[k][part] != want[k][part])
    assert not changed, f"{len(changed)} manifests changed, e.g. {changed[:5]}"
