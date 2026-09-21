"""Tests for bake.py's validation step (SCOPE.md §4.1 step 1, §10 checklist).

These tests exercise validate_markers_against_ts using fabricated
DecodedMarker/markers.json data (not real .ts/GPAC/threefive round-trips,
which require the pinned GPAC build and real franken-ts fixtures -- see
tests/fixtures/README.md for how to add those for full end-to-end coverage).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bake import (  # noqa: E402
    DecodedMarker,
    ValidationError,
    discover_renditions,
    validate_cue_tags_only,
    validate_increment_event_ids,
    validate_markers_against_ts,
)


def _marker(event_id: str, pts_time_ticks: int) -> dict:
    return {
        "event_id": event_id,
        "splice_type": "time_signal",
        "pts_time_ticks": pts_time_ticks,
        "pts_time_seconds": pts_time_ticks / 90_000,
    }


def test_validation_passes_on_exact_match():
    markers = [_marker("0x00000001", 5_400_000), _marker("0x00000002", 9_000_000)]
    decoded = [
        DecodedMarker("0x00000001", 5_400_000, "AAAA"),
        DecodedMarker("0x00000002", 9_000_000, "BBBB"),
    ]

    result = validate_markers_against_ts(markers, decoded)

    assert len(result) == 2
    assert result[0]["splice_command_b64"] in ("AAAA", "BBBB")
    for m in result:
        assert "splice_command_b64" in m


def test_validation_fails_on_missing_event_in_ts():
    markers = [_marker("0x00000001", 5_400_000)]
    decoded: list[DecodedMarker] = []  # nothing actually embedded

    with pytest.raises(ValidationError, match="not present in the .ts"):
        validate_markers_against_ts(markers, decoded)


def test_validation_fails_on_extra_event_in_ts():
    markers: list[dict] = []
    decoded = [DecodedMarker("0x00000001", 5_400_000, "AAAA")]

    with pytest.raises(ValidationError, match="not declared in markers.json"):
        validate_markers_against_ts(markers, decoded)


def test_validation_fails_on_one_tick_pts_mismatch():
    """Even a single-tick PTS drift must be a hard failure -- no tolerance."""
    markers = [_marker("0x00000001", 5_400_000)]
    decoded = [DecodedMarker("0x00000001", 5_400_001, "AAAA")]

    with pytest.raises(ValidationError, match="off by 1 tick"):
        validate_markers_against_ts(markers, decoded)


# ── decode_embedded_scte35 (real threefive-decode, stubbed) ──────────────────


class _FakeDescriptor:
    def __init__(self, segmentation_event_id: str | None) -> None:
        self.segmentation_event_id = segmentation_event_id


class _FakeCommand:
    def __init__(self, pts_time: float, splice_event_id=None) -> None:
        self.pts_time = pts_time
        self.splice_event_id = splice_event_id


class _FakeCue:
    """Stand-in for `threefive.Cue`. Supports the narrow subset bake.py
    actually uses: `.command`/`.descriptors` (read), `.base64()`, and
    (for the re-encode-per-event path) `.decode()`/`.encode()` -- encode()
    returns a value that deterministically reflects whichever descriptors
    are still present, so tests can tell a "narrowed" re-encode apart from
    the original merged message without needing real SCTE-35 bit-packing.
    """

    def __init__(self, pts_time: float, event_ids: list[str], b64: str) -> None:
        self.command = _FakeCommand(pts_time)
        self.descriptors = [_FakeDescriptor(eid) for eid in event_ids]
        self._b64 = b64

    def base64(self) -> str:
        return self._b64

    def decode(self) -> None:
        pass  # already "decoded" -- constructed straight from the registry

    def encode(self) -> str:
        ids = ",".join(d.segmentation_event_id for d in self.descriptors)
        return f"{self._b64}|{ids}"


class _FakeStream:
    def __init__(self, _path: str, cues: list[_FakeCue]) -> None:
        self._cues = cues

    def decode(self, func) -> None:
        for cue in self._cues:
            func(cue)


def _install_fake_threefive(monkeypatch, cues: list[_FakeCue]) -> None:
    """Install a fake `threefive` module backing both `Stream(...).decode()`
    (initial pass) and `Cue(b64)` (bake.py's per-event re-encode pass,
    which reconstructs a Cue from just the raw base64 it captured
    earlier) -- keyed by the original merged message's own base64, mirroring
    how bake.py always narrows from `cue.base64()`, never from a fresh
    decode of the .ts.
    """
    by_b64 = {cue._b64: cue for cue in cues}

    def _fake_cue_ctor(b64: str) -> _FakeCue:
        original = by_b64[b64]
        # Fresh copy so narrowing one event's descriptors doesn't mutate
        # the shared original (each event narrows independently).
        copy = _FakeCue(original.command.pts_time, [], original._b64)
        copy.descriptors = list(original.descriptors)
        return copy

    fake_threefive = type(sys)("threefive")
    fake_threefive.Stream = lambda path: _FakeStream(path, cues)
    fake_threefive.Cue = _fake_cue_ctor
    monkeypatch.setitem(sys.modules, "threefive", fake_threefive)


def test_decode_embedded_scte35_splits_multi_descriptor_message(monkeypatch, tmp_path):
    """A single SCTE-35 message carrying several segmentation descriptors
    (franken-ts merges every event coincident at the same PTS into one
    message) must decode into one DecodedMarker PER descriptor, not just
    the first one found -- this was the bug that made bake.py wrongly
    reject markers.json as having 'missing' events whenever two or more
    markers landed on the same instant (e.g. a Break start + a nested PPO
    start + an instant Call Ad Server, all at once).

    By default (narrow_descriptors=False), each of those per-descriptor
    DecodedMarkers still gets the raw, SHARED message bytes verbatim --
    that's the standard way coincident events are signaled on the wire
    (and what other systems, e.g. MediaPackage, do for co-located
    DATERANGEs too), not a bug to fix."""
    import bake

    fake_cues = [
        _FakeCue(pts_time=1.0, event_ids=["0x00000064", "0x00000066", "0x000000D0"], b64="AAAA"),
        _FakeCue(pts_time=2.0, event_ids=["0x00000065"], b64="BBBB"),
    ]
    _install_fake_threefive(monkeypatch, fake_cues)

    decoded = bake.decode_embedded_scte35(tmp_path / "fake.ts")

    assert len(decoded) == 4
    by_event = {d.event_id: d for d in decoded}
    assert set(by_event) == {"0x00000064", "0x00000066", "0x000000D0", "0x00000065"}
    # All four descriptors keep the shared message's own raw bytes.
    for eid in ("0x00000064", "0x00000066", "0x000000D0"):
        assert by_event[eid].pts_time_ticks == round(1.0 * 90_000)
        assert by_event[eid].splice_command_b64 == "AAAA"
    assert by_event["0x00000065"].pts_time_ticks == round(2.0 * 90_000)
    assert by_event["0x00000065"].splice_command_b64 == "BBBB"


def test_decode_embedded_scte35_narrow_descriptors_opt_in(monkeypatch, tmp_path):
    """With narrow_descriptors=True (set by `--daterange-mode narrowed` /
    channel config `[markers] daterange_mode = "narrowed"`), each
    per-descriptor DecodedMarker instead gets its OWN re-encoded,
    single-descriptor `splice_command_b64` -- for downstream consumers
    that can't cope with more than one segmentation descriptor per
    message. A message with only one descriptor to begin with needs no
    re-encode -- its already-single-event raw bytes are reused as-is."""
    import bake

    fake_cues = [
        _FakeCue(pts_time=1.0, event_ids=["0x00000064", "0x00000066", "0x000000D0"], b64="AAAA"),
        _FakeCue(pts_time=2.0, event_ids=["0x00000065"], b64="BBBB"),
    ]
    _install_fake_threefive(monkeypatch, fake_cues)

    decoded = bake.decode_embedded_scte35(tmp_path / "fake.ts", narrow_descriptors=True)

    by_event = {d.event_id: d for d in decoded}
    for eid in ("0x00000064", "0x00000066", "0x000000D0"):
        assert by_event[eid].splice_command_b64 == f"AAAA|{eid}"
    assert by_event["0x00000065"].splice_command_b64 == "BBBB"


# ── discover_renditions (directory-based rendition auto-discovery) ──────────


def test_discover_renditions_single_file_mode(tmp_path):
    ts_file = tmp_path / "myoutput.ts"
    ts_file.write_bytes(b"fake")
    markers_file = tmp_path / "myoutput.markers.json"
    markers_file.write_text("[]")

    renditions, markers_json = discover_renditions(ts_file)

    assert renditions == [("myoutput", ts_file)]
    assert markers_json == markers_file


def test_discover_renditions_directory_mode_multiple_ts(tmp_path):
    (tmp_path / "markers.json").write_text("[]")
    (tmp_path / "1080p.ts").write_bytes(b"fake")
    (tmp_path / "720p.ts").write_bytes(b"fake")
    (tmp_path / "360p.ts").write_bytes(b"fake")

    renditions, markers_json = discover_renditions(tmp_path)

    assert [name for name, _ in renditions] == ["1080p", "360p", "720p"]  # sorted
    assert markers_json == tmp_path / "markers.json"


def test_discover_renditions_directory_mode_single_ts_degenerates_to_one_rendition(tmp_path):
    (tmp_path / "markers.json").write_text("[]")
    (tmp_path / "only.ts").write_bytes(b"fake")

    renditions, markers_json = discover_renditions(tmp_path)

    assert renditions == [("only", tmp_path / "only.ts")]


def test_discover_renditions_fails_on_missing_markers_json(tmp_path):
    (tmp_path / "1080p.ts").write_bytes(b"fake")

    with pytest.raises(ValidationError, match="does not exist"):
        discover_renditions(tmp_path)


def test_discover_renditions_fails_on_empty_directory(tmp_path):
    (tmp_path / "markers.json").write_text("[]")

    with pytest.raises(ValidationError, match="No .ts files found"):
        discover_renditions(tmp_path)


def test_discover_renditions_respects_markers_override(tmp_path):
    (tmp_path / "1080p.ts").write_bytes(b"fake")
    custom_markers = tmp_path / "custom.markers.json"
    custom_markers.write_text("[]")

    renditions, markers_json = discover_renditions(tmp_path, markers_override=custom_markers)

    assert markers_json == custom_markers


def _splice_insert_marker(event_id: str, pts_time_ticks: int) -> dict:
    return {
        "event_id": event_id,
        "splice_type": "splice_insert",
        "pts_time_ticks": pts_time_ticks,
        "pts_time_seconds": pts_time_ticks / 90_000,
    }


def test_validate_cue_tags_only_passes_when_every_marker_is_splice_insert():
    markers = [
        _splice_insert_marker("0x00000001", 0),
        _splice_insert_marker("0x00000001", 900_000),
    ]

    validate_cue_tags_only(markers)  # no raise


def test_validate_cue_tags_only_fails_on_time_signal_marker():
    markers = [
        _splice_insert_marker("0x00000001", 0),
        _marker("0x00000002", 900_000),  # time_signal, from the module-level helper
    ]

    with pytest.raises(ValidationError, match="cue_tags='only'"):
        validate_cue_tags_only(markers)


def test_validate_increment_event_ids_passes_for_small_base_ids():
    # Realistic franken-ts-generated ids (small, sequential) -- plenty of
    # 32-bit headroom for any decade-sized step.
    markers = [_marker("0x00000064", 0), _marker("0x000000BE", 900_000)]

    validate_increment_event_ids(markers)  # no raise


def test_validate_increment_event_ids_fails_when_base_id_leaves_no_room_for_a_step():
    # A base id already using most of the 32-bit range: the smallest
    # power of 10 above it overshoots the ceiling entirely, so every
    # increment would silently no-op forever.
    markers = [_marker("0x48000000", 0)]  # ~1.2 billion

    with pytest.raises(ValidationError, match="increment_event_ids=true"):
        validate_increment_event_ids(markers)
