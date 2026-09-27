"""Tests for serve.py's continuity mode (SCOPE.md §12): rewriting segment
timestamps per request so the channel has no #EXT-X-DISCONTINUITY / DASH
Period restart at the loop wrap.

Uses a real, tiny on-disk loop package built from real ffmpeg fmp4 output
(same convention as tests/test_serve_asset_boundaries.py's sparse-package
fixture) so the tfdt-patch route is exercised end-to-end through Flask's
test client, not just unit-tested against fabricated bytes.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmaf  # noqa: E402
from serve import Channel, LoopPackage, compute_asset_boundary_set, create_app  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# ── Channel._validate_continuous ────────────────────────────────────────────


def _minimal_video_rendition(tmp_path: Path, *, tfdt_version: int = 1) -> SimpleNamespace:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=1",
            "-c:v", "libx264", "-g", "12", "-video_track_timescale", "90000",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4",
            str(tmp_path / "a.mp4"),
        ],
        check=True,
    )
    data = (tmp_path / "a.mp4").read_bytes()
    init, fragments = cmaf.split_init_and_fragments(data)
    fragment = fragments[0]
    if tfdt_version == 0:
        moof = cmaf._find(fragment, ("moof",))
        tfdt = cmaf._find(fragment, ("traf", "tfdt"), moof[0] + 8, moof[1])
        buf = bytearray(fragment)
        old_value = struct.unpack(">Q", buf[tfdt[0] + 12 : tfdt[0] + 20])[0]
        buf[tfdt[0] + 8] = 0  # version 0
        # rebuild as a v0 (32-bit) tfdt: 12-byte box (header+version/flags+value)
        new_tfdt = struct.pack(">I4sBBBBI", 12, b"tfdt", 0, 0, 0, 0, old_value)
        rest_of_traf_after_tfdt = bytes(buf[tfdt[1] :])
        traf_start = cmaf._find(bytes(buf), ("traf",), moof[0] + 8, moof[1])[0]
        before_tfdt = bytes(buf[traf_start + 8 : tfdt[0]])
        old_traf_size = cmaf._find(bytes(buf), ("traf",), moof[0] + 8, moof[1])
        traf_type_and_size = bytes(buf[traf_start : traf_start + 8])
        new_traf_payload = before_tfdt + new_tfdt + rest_of_traf_after_tfdt
        new_traf_size = 8 + len(new_traf_payload)
        new_traf = struct.pack(">I", new_traf_size) + b"traf" + new_traf_payload
        moof_start, moof_end = cmaf._find(bytes(buf), ("moof",))
        before_traf = bytes(buf[moof_start + 8 : traf_start])
        new_moof_payload = before_traf + new_traf
        new_moof_size = 8 + len(new_moof_payload)
        new_moof = struct.pack(">I", new_moof_size) + b"moof" + new_moof_payload
        fragment = new_moof + bytes(buf[moof_end:])

    segments_dir = tmp_path / "segments" / "1080p"
    segments_dir.mkdir(parents=True)
    (segments_dir / "seg_track1_init.mp4").write_bytes(init)
    (segments_dir / "seg_track1_0.m4s").write_bytes(fragment)
    (segments_dir / "seg_track1_1.m4s").write_bytes(fragment)

    return SimpleNamespace(
        name="1080p",
        sparse=False,
        video_track_id=1,
        segment_files=[segments_dir / "seg_track1_0.m4s", segments_dir / "seg_track1_1.m4s"],
    )


def test_validate_continuous_rejects_internal_asset_boundaries():
    package = SimpleNamespace(boundaries={0, 1}, video_renditions=[], audio_rendition=None)
    with pytest.raises(RuntimeError, match="internal asset-boundary"):
        Channel._validate_continuous(package)


def test_validate_continuous_rejects_sparse_rendition():
    rendition = SimpleNamespace(sparse=True, name="archive")
    package = SimpleNamespace(boundaries={0}, video_renditions=[rendition], audio_rendition=None)
    with pytest.raises(RuntimeError, match="sparse"):
        Channel._validate_continuous(package)


def test_validate_continuous_accepts_64bit_tfdt(tmp_path):
    rendition = _minimal_video_rendition(tmp_path, tfdt_version=1)
    package = SimpleNamespace(boundaries={0}, video_renditions=[rendition], audio_rendition=None)
    Channel._validate_continuous(package)  # must not raise


def test_validate_continuous_rejects_32bit_tfdt(tmp_path):
    rendition = _minimal_video_rendition(tmp_path, tfdt_version=0)
    package = SimpleNamespace(boundaries={0}, video_renditions=[rendition], audio_rendition=None)
    with pytest.raises(RuntimeError, match="64-bit"):
        Channel._validate_continuous(package)


# ── Real on-disk package, end to end via Flask's test client ──────────────


def _write_continuous_package(package_dir: Path) -> tuple[int, int]:
    """2 segments/loop, video-only, real ffmpeg fmp4 fragments. Returns
    (segment_duration_ticks, total_loop_duration_ticks)."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=1",
            "-c:v", "libx264", "-g", "12", "-video_track_timescale", "90000",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4",
            str(package_dir / "a.mp4"),
        ],
        check=True,
    )
    data = (package_dir / "a.mp4").read_bytes()
    init, fragments = cmaf.split_init_and_fragments(data)
    assert len(fragments) >= 2

    def _tfdt(fragment: bytes) -> int:
        moof = cmaf._find(fragment, ("moof",))
        tfdt = cmaf._find(fragment, ("traf", "tfdt"), moof[0] + 8, moof[1])
        return struct.unpack(">Q", fragment[tfdt[0] + 12 : tfdt[0] + 20])[0]

    seg0, seg1 = fragments[0], fragments[1]
    start0, start1 = _tfdt(seg0), _tfdt(seg1)
    segment_duration_ticks = start1 - start0
    total_loop_duration_ticks = start1 + segment_duration_ticks  # ground truth, 2 equal-length segments

    segments_dir = package_dir / "segments" / "1080p"
    segments_dir.mkdir(parents=True)
    (segments_dir / "seg_track1_init.mp4").write_bytes(init)
    (segments_dir / "seg_track1_0.m4s").write_bytes(seg0)
    (segments_dir / "seg_track1_1.m4s").write_bytes(seg1)

    descriptor = {
        "version": 2,
        "timescale": 90_000,
        "total_loop_duration_ticks": total_loop_duration_ticks,
        "segment_duration_seconds": segment_duration_ticks / 90_000,
        "hls_format": "cmaf",
        "hls_ts_mux_audio": True,
        "daterange_mode": "shared",
        "cue_tags": "none",
        "increment_event_ids": False,
        "daterange_id_format": None,
        "markers": [],
        "asset_boundaries": [],
        "video_renditions": [
            {
                "name": "1080p",
                "sparse": False,
                "video_track_id": 1,
                "audio_track_id": None,
                "segment_boundary_ticks": [start0, start1],
                "video_variant": {
                    "codecs": "avc1.640028", "width": 64, "height": 64,
                    "frame_rate": 24.0, "bandwidth": 500_000,
                },
                "audio_variant": None,
            }
        ],
        "source_input": "a.mp4",
        "source_markers_json": "a.markers.json",
    }
    (package_dir / "loop_descriptor.json").write_text(json.dumps(descriptor))
    return segment_duration_ticks, total_loop_duration_ticks


def test_continuous_hls_manifest_has_no_discontinuity_and_uses_global_index(tmp_path):
    seg_dur, total = _write_continuous_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    resp = client.get("/video.m3u8")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "#EXT-X-DISCONTINUITY\n" not in body  # the standalone tag, not -SEQUENCE
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in body
    # Segment URIs must carry the ever-increasing GLOBAL index (real
    # wall-clock-derived, so large and not a small local 0/1) in continuity
    # mode, not the plain local/physical index every other mode uses.
    media_sequence_line = next(l for l in body.splitlines() if l.startswith("#EXT-X-MEDIA-SEQUENCE:"))
    media_sequence = int(media_sequence_line.split(":")[1])
    assert media_sequence > 1  # sanity: this really is a huge running index
    assert f"seg/{media_sequence}.m4s" in body


def test_continuous_segment_route_shifts_tfdt_by_loop_number(tmp_path):
    seg_dur, total = _write_continuous_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    # global index 0 -> loop 0, local 0: byte-identical to the source (shift 0).
    resp0 = client.get("/1080p/seg/0.m4s")
    assert resp0.status_code == 200
    moof0 = cmaf._find(resp0.data, ("moof",))
    tfdt0 = cmaf._find(resp0.data, ("traf", "tfdt"), moof0[0] + 8, moof0[1])
    value0 = struct.unpack(">Q", resp0.data[tfdt0[0] + 12 : tfdt0[0] + 20])[0]
    assert value0 == 0

    # global index 3 -> loop 1, local 1: shifted by exactly one
    # total_loop_duration_ticks relative to the unshifted local-1 segment.
    resp_local1 = client.get("/1080p/seg/1.m4s")
    moof_l1 = cmaf._find(resp_local1.data, ("moof",))
    tfdt_l1 = cmaf._find(resp_local1.data, ("traf", "tfdt"), moof_l1[0] + 8, moof_l1[1])
    value_local1_loop0 = struct.unpack(">Q", resp_local1.data[tfdt_l1[0] + 12 : tfdt_l1[0] + 20])[0]

    resp3 = client.get("/1080p/seg/3.m4s")
    assert resp3.status_code == 200
    moof3 = cmaf._find(resp3.data, ("moof",))
    tfdt3 = cmaf._find(resp3.data, ("traf", "tfdt"), moof3[0] + 8, moof3[1])
    value3 = struct.unpack(">Q", resp3.data[tfdt3[0] + 12 : tfdt3[0] + 20])[0]
    assert value3 == value_local1_loop0 + total


def test_continuous_dash_manifest_has_exactly_one_period_spanning_the_wrap(tmp_path):
    seg_dur, total = _write_continuous_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    resp = client.get("/stream.mpd")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert body.count("<Period ") == 1
    assert 'id="continuous"' in body
    assert 'start="PT0S"' in body
    # <S t=...> values must be absolute and strictly increasing across the
    # loop wrap (no restart back to 0 for the second loop iteration).
    t_values = [int(v) for v in __import__("re").findall(r'<S t="(\d+)"', body)]
    assert t_values == sorted(t_values)
    assert t_values[-1] >= total  # window reaches into loop 1
