"""Tests for bake.py's segment-list ('sparse') input mode (SCOPE.md §11).

Works through §11.4's acceptance checklist. Real GPAC/ffmpeg aren't
available in this environment (and grave-robber's own real-HAR testing
happens separately -- see loop-dee-loop/SCOPE.md §11's own note and the
grave-robber SCOPE.md §9 non-goals) -- ffmpeg-invoking helpers
(remux_segment_to_self_initializing_fragment / remux_segment_to_ts /
probe_segment_variant_metadata) are monkeypatched out so these tests
exercise the pure ledger/validation logic and loop_descriptor.json shape,
consistent with tests/test_bake_validation.py's existing style (fabricated
data, not real media round-trips).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bake  # noqa: E402
from bake import (  # noqa: E402
    ValidationError,
    bake_segment_list,
    compute_asset_boundary_indices,
    compute_segment_list_boundary_ticks,
    load_segment_list_manifest,
    validate_segment_list_missing_media,
)


def _segment(index, duration_ticks=360_000, asset_boundary=False, media_file="seg.bin"):
    return {
        "index": index,
        "duration_ticks": duration_ticks,
        "asset_boundary": asset_boundary,
        "media_file": media_file,
    }


def _write_manifest(path: Path, segments: list[dict], markers: list[dict] | None = None) -> Path:
    manifest_path = path / "manifest.json"
    manifest_path.write_text(json.dumps({"segments": segments, "markers": markers or []}))
    return manifest_path


# ── load_segment_list_manifest ───────────────────────────────────────────


def test_load_segment_list_manifest_valid(tmp_path):
    segments = [_segment(0), _segment(1)]
    manifest_path = _write_manifest(tmp_path, segments)

    data = load_segment_list_manifest(manifest_path)

    assert len(data["segments"]) == 2
    assert data["markers"] == []


def test_load_segment_list_manifest_rejects_missing_segments_key(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"markers": []}))

    with pytest.raises(ValidationError, match="'segments' list"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_missing_markers_key(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"segments": [_segment(0)]}))

    with pytest.raises(ValidationError, match="'markers' list"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_out_of_order_index(tmp_path):
    segments = [_segment(0), _segment(2)]
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match=r"expected 1"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_non_positive_duration(tmp_path):
    segments = [_segment(0, duration_ticks=0)]
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="duration_ticks"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_non_bool_asset_boundary(tmp_path):
    segments = [_segment(0)]
    segments[0]["asset_boundary"] = "yes"
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="asset_boundary"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_non_string_media_file(tmp_path):
    segments = [_segment(0)]
    segments[0]["media_file"] = 123
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="media_file"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_missing_field(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    segments = [{"index": 0, "duration_ticks": 1, "asset_boundary": False}]  # no media_file
    manifest_path.write_text(json.dumps({"segments": segments, "markers": []}))

    with pytest.raises(ValidationError, match="missing required field"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_allows_null_media_file(tmp_path):
    segments = [_segment(0, media_file=None)]
    manifest_path = _write_manifest(tmp_path, segments)

    data = load_segment_list_manifest(manifest_path)

    assert data["segments"][0]["media_file"] is None


# ── validate_segment_list_missing_media ──────────────────────────────────


def test_missing_media_hard_fails_by_default():
    segments = [_segment(0), _segment(1, media_file=None)]

    with pytest.raises(ValidationError, match="allow-missing-segments"):
        validate_segment_list_missing_media(segments, allow_missing_segments=False)


def test_missing_media_allowed_with_flag():
    segments = [_segment(0), _segment(1, media_file=None), _segment(2)]

    missing = validate_segment_list_missing_media(segments, allow_missing_segments=True)

    assert missing == [1]


def test_no_missing_media_passes_regardless_of_flag():
    segments = [_segment(0), _segment(1)]

    assert validate_segment_list_missing_media(segments, allow_missing_segments=False) == []


# ── ledger computation ────────────────────────────────────────────────────


def test_segment_list_boundary_ticks_is_exclusive_prefix_sum():
    segments = [
        _segment(0, duration_ticks=100),
        _segment(1, duration_ticks=200),
        _segment(2, duration_ticks=50),
    ]

    boundaries = compute_segment_list_boundary_ticks(segments)

    assert boundaries == [0, 100, 300]


def test_segment_list_boundary_ticks_unaffected_by_missing_media():
    """SCOPE.md §11.1: the ledger is always complete -- a null media_file
    entry still contributes its full duration_ticks to the timeline."""
    segments = [
        _segment(0, duration_ticks=100),
        _segment(1, duration_ticks=200, media_file=None),
        _segment(2, duration_ticks=50),
    ]

    boundaries = compute_segment_list_boundary_ticks(segments)

    assert boundaries == [0, 100, 300]


def test_asset_boundary_indices():
    segments = [
        _segment(0, asset_boundary=True),
        _segment(1),
        _segment(2, asset_boundary=True),
        _segment(3),
    ]

    assert compute_asset_boundary_indices(segments) == [0, 2]


# ── bake_segment_list end-to-end (ffmpeg/ffprobe mocked out) ─────────────


@pytest.fixture()
def _stub_media_io(monkeypatch):
    """Replace every ffmpeg/ffprobe-invoking helper with a fast fake that
    just records calls and, for the probe, returns fixed variant metadata --
    no real media tooling needed (none is installed in this environment;
    see loop-dee-loop/SCOPE.md §11's own note that real-archive validation
    happens separately)."""
    remux_calls = []
    ts_calls = []

    def _fake_remux(src, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-cmaf-fragment")
        remux_calls.append((Path(src), dest))

    def _fake_remux_ts(src, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-ts-segment")
        ts_calls.append((Path(src), dest))

    def _fake_probe(path):
        return {
            "video": {
                "codecs": "avc1.640028",
                "width": 1920,
                "height": 1080,
                "frame_rate": 25.0,
                "bandwidth": 5_000_000,
            },
            "audio": None,
        }

    monkeypatch.setattr(bake, "remux_segment_to_self_initializing_fragment", _fake_remux)
    monkeypatch.setattr(bake, "remux_segment_to_ts", _fake_remux_ts)
    monkeypatch.setattr(bake, "probe_segment_variant_metadata", _fake_probe)
    return remux_calls, ts_calls


def _write_source_segment(tmp_path: Path, name: str) -> str:
    src = tmp_path / name
    src.write_bytes(b"source-bytes")
    return str(src)


def test_bake_segment_list_hard_fails_on_missing_media_by_default(tmp_path, _stub_media_io):
    segments = [
        _segment(0, media_file=_write_source_segment(tmp_path, "s0.bin")),
        _segment(1, media_file=None),
    ]
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="allow-missing-segments"):
        bake_segment_list(manifest_path, tmp_path / "out", allow_missing_segments=False)


def test_bake_segment_list_with_middle_null_entry_produces_complete_ledger(tmp_path, _stub_media_io):
    """§11.4 checklist: "verified against a segment-list input with at
    least one null entry in the middle of the sequence (not just at the
    end)"."""
    segments = [
        _segment(0, duration_ticks=100, media_file=_write_source_segment(tmp_path, "s0.bin")),
        _segment(1, duration_ticks=200, media_file=None),
        _segment(2, duration_ticks=50, media_file=_write_source_segment(tmp_path, "s2.bin"), asset_boundary=True),
    ]
    manifest_path = _write_manifest(tmp_path, segments, markers=[{"event_id": "0x1", "pts_time_ticks": 300}])
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir, allow_missing_segments=True)

    descriptor = json.loads((output_dir / "loop_descriptor.json").read_text())
    assert descriptor["total_loop_duration_ticks"] == 350
    assert descriptor["asset_boundaries"] == [2]
    assert descriptor["markers"] == [{"event_id": "0x1", "pts_time_ticks": 300}]

    rendition = descriptor["video_renditions"][0]
    assert rendition["sparse"] is True
    assert rendition["segment_boundary_ticks"] == [0, 100, 300]
    assert rendition["segment_present"] == [True, False, True]

    segments_dir = output_dir / "segments" / rendition["name"]
    assert (segments_dir / "seg_000000.m4s").exists()
    assert not (segments_dir / "seg_000001.m4s").exists()
    assert (segments_dir / "seg_000002.m4s").exists()


def test_bake_segment_list_hls_ts_format_also_writes_ts_store(tmp_path, _stub_media_io):
    remux_calls, ts_calls = _stub_media_io
    segments = [
        _segment(0, media_file=_write_source_segment(tmp_path, "s0.bin")),
        _segment(1, media_file=_write_source_segment(tmp_path, "s1.bin")),
    ]
    manifest_path = _write_manifest(tmp_path, segments)
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir, hls_format="ts")

    assert len(ts_calls) == 2
    ts_dir = output_dir / "hls-ts" / "archive"
    assert (ts_dir / "0.ts").exists()
    assert (ts_dir / "1.ts").exists()


def test_bake_segment_list_dry_run_writes_nothing(tmp_path, _stub_media_io):
    remux_calls, ts_calls = _stub_media_io
    segments = [_segment(0, media_file=_write_source_segment(tmp_path, "s0.bin"))]
    manifest_path = _write_manifest(tmp_path, segments)
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir, dry_run=True)

    assert not (output_dir / "loop_descriptor.json").exists()
    assert remux_calls == []


def test_bake_segment_list_rejects_bad_hls_format(tmp_path, _stub_media_io):
    segments = [_segment(0, media_file=_write_source_segment(tmp_path, "s0.bin"))]
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="hls_format"):
        bake_segment_list(manifest_path, tmp_path / "out", hls_format="bogus")


def test_bake_segment_list_all_missing_media_fails_even_with_flag(tmp_path, _stub_media_io):
    """At least one real segment is required to probe variant metadata from."""
    segments = [_segment(0, media_file=None), _segment(1, media_file=None)]
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="cannot probe"):
        bake_segment_list(manifest_path, tmp_path / "out", allow_missing_segments=True)


def test_main_cli_dispatches_json_input_to_segment_list_mode(tmp_path, _stub_media_io, monkeypatch):
    """bake.py's CLI selects the sparse mode purely by input-file shape
    (.json) per SCOPE.md §11.2, no separate flag/binary."""
    segments = [_segment(0, media_file=_write_source_segment(tmp_path, "s0.bin"))]
    manifest_path = _write_manifest(tmp_path, segments)
    output_dir = tmp_path / "out"

    called_with = {}

    def _fake_bake_segment_list(manifest_path_arg, output_dir_arg, **kwargs):
        called_with["manifest_path"] = manifest_path_arg
        called_with["output_dir"] = output_dir_arg
        called_with.update(kwargs)

    monkeypatch.setattr(bake, "bake_segment_list", _fake_bake_segment_list)
    monkeypatch.setattr(bake, "bake", lambda *a, **k: pytest.fail("should not call the normal bake() path"))

    exit_code = bake.main([str(manifest_path), "--output", str(output_dir), "--allow-missing-segments"])

    assert exit_code == 0
    assert called_with["manifest_path"] == manifest_path
    assert called_with["output_dir"] == output_dir
    assert called_with["allow_missing_segments"] is True
