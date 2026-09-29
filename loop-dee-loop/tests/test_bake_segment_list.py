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
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bake  # noqa: E402
from bake import (  # noqa: E402
    ValidationError,
    bake_segment_list,
    compute_asset_boundary_gap_ticks,
    compute_asset_boundary_indices,
    compute_segment_list_boundary_ticks,
    load_segment_list_manifest,
    validate_segment_list_missing_media,
)


def _segment(index, duration_ticks=360_000, asset_boundary=False, media_file="seg.bin", gap_ticks=0):
    seg = {
        "index": index,
        "duration_ticks": duration_ticks,
        "asset_boundary": asset_boundary,
        "media_file": media_file,
    }
    if gap_ticks:
        seg["gap_ticks"] = gap_ticks
    return seg


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


def test_load_segment_list_manifest_rejects_non_int_gap_ticks(tmp_path):
    segments = [_segment(0, asset_boundary=True)]
    segments[0]["gap_ticks"] = "45000"
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="gap_ticks"):
        load_segment_list_manifest(manifest_path)


def test_load_segment_list_manifest_rejects_gap_ticks_without_asset_boundary(tmp_path):
    segments = [_segment(0, asset_boundary=False, gap_ticks=1000)]
    manifest_path = _write_manifest(tmp_path, segments)

    with pytest.raises(ValidationError, match="asset_boundary=False"):
        load_segment_list_manifest(manifest_path)


def test_asset_boundary_gap_ticks_map():
    segments = [
        _segment(0, asset_boundary=True),
        _segment(1),
        _segment(2, asset_boundary=True, gap_ticks=45_000),
        _segment(3, asset_boundary=True, gap_ticks=-9_000),
    ]

    assert compute_asset_boundary_gap_ticks(segments) == {"2": 45_000, "3": -9_000}


# ── bake_segment_list end-to-end (ffmpeg/ffprobe mocked out) ─────────────


def _box(box_type: bytes, payload: bytes) -> bytes:
    import struct

    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def _fake_fmp4() -> bytes:
    """Smallest fragmented MP4 cmaf.py can split: ftyp + moov(avcC) + moof(mfhd, traf/tfdt) + mdat."""
    import struct

    mfhd = _box(b"mfhd", struct.pack(">II", 0, 1))
    tfdt = _box(b"tfdt", struct.pack(">IQ", 1 << 24, 7777))
    moof = _box(b"moof", mfhd + _box(b"traf", tfdt))
    moov = _box(b"moov", _box(b"avcC", b"\x01\x4d\x40\x1f"))
    return _box(b"ftyp", b"isom0000") + moov + moof + _box(b"mdat", b"x")


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
        dest.write_bytes(_fake_fmp4())
        remux_calls.append((Path(src), dest))

    def _fake_remux_ts(src, dest, **_kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-ts-segment")
        ts_calls.append((Path(src), dest))

    def _fake_probe(path, init_path=None):
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

    monkeypatch.setattr(bake, "remux_segment_to_fragmented_mp4", _fake_remux)
    monkeypatch.setattr(bake, "remux_segment_to_ts", _fake_remux_ts)
    monkeypatch.setattr(bake, "probe_segment_variant_metadata", _fake_probe)
    monkeypatch.setattr(bake, "probe_has_audio", lambda path: False)
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
        _segment(
            2, duration_ticks=50, media_file=_write_source_segment(tmp_path, "s2.bin"),
            asset_boundary=True, gap_ticks=45_000,
        ),
    ]
    manifest_path = _write_manifest(tmp_path, segments, markers=[{"event_id": "0x1", "pts_time_ticks": 300}])
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir, allow_missing_segments=True)

    descriptor = json.loads((output_dir / "loop_descriptor.json").read_text())
    assert descriptor["total_loop_duration_ticks"] == 350
    assert descriptor["asset_boundary_indices"] == [2]
    assert descriptor["asset_boundary_gap_ticks"] == {"2": 45_000}
    assert descriptor["markers"] == [{"event_id": "0x1", "pts_time_ticks": 300}]

    rendition = descriptor["video_renditions"][0]
    assert rendition["sparse"] is True
    assert rendition["segment_boundary_ticks"] == [0, 100, 300]
    assert rendition["segment_present"] == [True, False, True]

    segments_dir = output_dir / "segments" / rendition["name"]
    assert (segments_dir / "seg_000000.m4s").exists()
    assert not (segments_dir / "seg_000001.m4s").exists()
    assert (segments_dir / "seg_000002.m4s").exists()

    # Proper CMAF: one init per span (an asset boundary starts a new one),
    # bare moof+mdat segments with tfdt relative to their span's start.
    assert rendition["init_span_starts"] == [0, 2]
    assert rendition["init_files"] == ["init_0.mp4", "init_1.mp4"]
    for init_name in rendition["init_files"]:
        assert (segments_dir / init_name).read_bytes()[4:8] == b"ftyp"
    seg2 = (segments_dir / "seg_000002.m4s").read_bytes()
    assert seg2[4:8] == b"moof" and b"moov" not in seg2 and b"ftyp" not in seg2
    assert struct.pack(">Q", 0) in seg2  # segment 2 opens span 1: tfdt restarts at 0


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


# ── multi-rendition (ladder) segment-list manifests ──────────────────────


def _write_ladder_manifest(path: Path, segments: list[dict], renditions: list[dict], markers=None) -> Path:
    manifest_path = path / "manifest.json"
    timing = [{k: v for k, v in s.items() if k != "media_file"} for s in segments]
    manifest_path.write_text(json.dumps({"segments": timing, "renditions": renditions, "markers": markers or []}))
    return manifest_path


def _ladder(tmp_path: Path, count: int = 3, missing: dict[str, set[int]] | None = None) -> list[dict]:
    missing = missing or {}
    return [
        {
            "name": name,
            "variant": {"bandwidth": bandwidth, "resolution": resolution},
            "media_files": [
                None if i in missing.get(name, set()) else _write_source_segment(tmp_path, f"{name}_{i}.bin")
                for i in range(count)
            ],
        }
        for name, bandwidth, resolution in (("720p", 3_000_000, "1280x720"), ("360p", 800_000, "640x360"))
    ]


def test_load_segment_list_manifest_accepts_renditions_without_segment_media_file(tmp_path):
    segments = [_segment(0), _segment(1)]
    manifest_path = _write_ladder_manifest(tmp_path, segments, _ladder(tmp_path, 2))

    data = load_segment_list_manifest(manifest_path)

    assert [r["name"] for r in data["renditions"]] == ["720p", "360p"]


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda rs: rs[1].update(name="720p"), "duplicate rendition name"),
        (lambda rs: rs[0].update(name="a/b"), "must be a non-empty string"),
        (lambda rs: rs[1]["media_files"].pop(), "exactly one entry per segment"),
        (lambda rs: rs[0]["media_files"].__setitem__(0, 5), "string path or null"),
        (lambda rs: rs.clear(), "non-empty list"),
    ],
)
def test_load_segment_list_manifest_rejects_bad_renditions(tmp_path, mutate, match):
    segments = [_segment(0), _segment(1)]
    renditions = _ladder(tmp_path, 2)
    mutate(renditions)
    manifest_path = _write_ladder_manifest(tmp_path, segments, renditions)

    with pytest.raises(ValidationError, match=match):
        load_segment_list_manifest(manifest_path)


def test_bake_segment_list_ladder_writes_one_rendition_dir_each(tmp_path, _stub_media_io, monkeypatch):
    remux_calls, _ = _stub_media_io
    bandwidth_by_dir = {"720p": 3_000_000, "360p": 800_000}

    def _fake_probe(path, init_path=None):
        return {"video": {"codecs": "avc1.640028", "width": 1, "height": 1, "frame_rate": 25.0,
                          "bandwidth": bandwidth_by_dir[init_path.parent.name]}, "audio": None}

    monkeypatch.setattr(bake, "probe_segment_variant_metadata", _fake_probe)
    segments = [_segment(0, duration_ticks=100), _segment(1, duration_ticks=200), _segment(2, duration_ticks=50)]
    manifest_path = _write_ladder_manifest(tmp_path, segments, _ladder(tmp_path))
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir)

    assert len(remux_calls) == 6  # every rendition's every segment
    descriptor = json.loads((output_dir / "loop_descriptor.json").read_text())
    renditions = descriptor["video_renditions"]
    assert [r["name"] for r in renditions] == ["720p", "360p"]
    for rendition in renditions:
        assert rendition["sparse"] is True
        assert rendition["segment_boundary_ticks"] == [0, 100, 300]  # one shared timeline
        assert rendition["total_loop_duration_ticks"] == 350
        assert rendition["segment_present"] == [True, True, True]
        segments_dir = output_dir / "segments" / rendition["name"]
        assert (segments_dir / "init_0.mp4").exists()
        assert all((segments_dir / f"seg_{i:06d}.m4s").exists() for i in range(3))
    assert renditions[0]["video_variant"]["bandwidth"] == 3_000_000
    assert renditions[1]["video_variant"]["bandwidth"] == 800_000


def test_bake_segment_list_ladder_applies_each_renditions_declared_variant(tmp_path, _stub_media_io):
    segments = [_segment(0), _segment(1)]
    manifest_path = _write_ladder_manifest(tmp_path, segments, _ladder(tmp_path, 2))
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir)

    renditions = json.loads((output_dir / "loop_descriptor.json").read_text())["video_renditions"]
    assert [r["video_variant"]["bandwidth"] for r in renditions] == [3_000_000, 800_000]


def test_bake_segment_list_ladder_holes_are_per_rendition(tmp_path, _stub_media_io):
    segments = [_segment(0), _segment(1), _segment(2)]
    ladder = _ladder(tmp_path, missing={"360p": {1}})
    manifest_path = _write_ladder_manifest(tmp_path, segments, ladder)
    output_dir = tmp_path / "out"

    with pytest.raises(ValidationError, match="Rendition '360p'.*allow-missing-segments"):
        bake_segment_list(manifest_path, output_dir)

    bake_segment_list(manifest_path, output_dir, allow_missing_segments=True)

    by_name = {r["name"]: r for r in json.loads((output_dir / "loop_descriptor.json").read_text())["video_renditions"]}
    assert by_name["720p"]["segment_present"] == [True, True, True]
    assert by_name["360p"]["segment_present"] == [True, False, True]


def test_bake_segment_list_ladder_rendition_with_no_media_fails(tmp_path, _stub_media_io):
    segments = [_segment(0), _segment(1)]
    ladder = _ladder(tmp_path, 2, missing={"360p": {0, 1}})
    manifest_path = _write_ladder_manifest(tmp_path, segments, ladder)

    with pytest.raises(ValidationError, match="Rendition '360p'.*cannot probe"):
        bake_segment_list(manifest_path, tmp_path / "out", allow_missing_segments=True)


def test_bake_segment_list_ladder_hls_ts_writes_a_ts_store_per_rendition(tmp_path, _stub_media_io):
    _, ts_calls = _stub_media_io
    segments = [_segment(0), _segment(1)]
    manifest_path = _write_ladder_manifest(tmp_path, segments, _ladder(tmp_path, 2))
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir, hls_format="ts")

    assert len(ts_calls) == 4
    for name in ("720p", "360p"):
        assert (output_dir / "hls-ts" / name / "0.ts").exists()


def test_bake_segment_list_ladder_bakes_muxed_audio_once_from_the_first_rendition(
    tmp_path, _stub_media_io, monkeypatch
):
    audio_calls = []

    def _fake_remux(src, dest, *, stream="v"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_fake_fmp4())
        if stream == "a":
            audio_calls.append(Path(src).name)

    monkeypatch.setattr(bake, "remux_segment_to_fragmented_mp4", _fake_remux)
    monkeypatch.setattr(bake, "probe_has_audio", lambda path: True)
    monkeypatch.setattr(bake.cmaf, "stsd_config", lambda init: b"aac")
    monkeypatch.setattr(bake.cmaf, "track_timescale", lambda init: 48_000)
    monkeypatch.setattr(bake, "probe_audio_variant", lambda path: {"codecs": "mp4a.40.2", "bandwidth": 128_000})
    segments = [_segment(0), _segment(1)]
    manifest_path = _write_ladder_manifest(tmp_path, segments, _ladder(tmp_path, 2))
    output_dir = tmp_path / "out"

    bake_segment_list(manifest_path, output_dir)

    assert audio_calls == ["720p_0.bin", "720p_1.bin"]  # the 360p sources' audio is never used
    renditions = json.loads((output_dir / "loop_descriptor.json").read_text())["video_renditions"]
    assert renditions[0]["audio_sparse"] is True
    assert renditions[0]["audio_variant"]["codecs"] == "mp4a.40.2"
    assert "audio_variant" in renditions[1] and renditions[1]["audio_variant"] is None
    assert (output_dir / "segments" / "720p" / "seg_a_000000.m4s").exists()
    assert not (output_dir / "segments" / "360p" / "seg_a_000000.m4s").exists()
    # Declared bandwidth covers video + audio: the shared audio is subtracted from every rendition.
    assert [r["video_variant"]["bandwidth"] for r in renditions] == [3_000_000 - 128_000, 800_000 - 128_000]


def test_baked_ladder_is_served_as_a_multivariant_playlist_and_multi_representation_mpd(tmp_path, _stub_media_io):
    """Round trip: a baked ladder loads in serve.py, which advertises every
    rendition in the HLS multivariant playlist / DASH MPD and serves each
    one's segments from its own directory."""
    from serve import Channel, LoopPackage, create_app

    segments = [_segment(0, duration_ticks=90_000), _segment(1, duration_ticks=90_000)]
    manifest_path = _write_ladder_manifest(tmp_path, segments, _ladder(tmp_path, 2))
    output_dir = tmp_path / "out"
    bake_segment_list(manifest_path, output_dir)

    channel = Channel(LoopPackage(output_dir), epoch_ticks=0, window_segments=2)
    channel.now_ticks = lambda: 0

    master = channel.build_hls_master_playlist()
    assert master.count("#EXT-X-STREAM-INF") == 2
    assert "video_1.m3u8" in master and "video_2.m3u8" in master  # highest bandwidth first
    for name in ("720p", "360p"):
        assert f"{name}/seg/0.m4s" in channel.build_hls_manifest(name)
    mpd = channel.build_dash_manifest()
    assert mpd.count("<Representation") == 2

    client = create_app(output_dir, epoch_ticks=0, window_segments=2).test_client()
    for name in ("720p", "360p"):
        assert client.get(f"/{name}/seg/0.m4s").status_code == 200
