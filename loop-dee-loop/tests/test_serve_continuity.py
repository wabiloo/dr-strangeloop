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
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmaf  # noqa: E402
from serve import Channel, LoopPackage, compute_asset_boundary_set, create_app  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# ── Channel._validate_continuous ────────────────────────────────────────────


def _minimal_video_rendition(tmp_path: Path, *, tfdt_version: int = 1, sparse: bool = False) -> SimpleNamespace:
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
        sparse=sparse,
        video_track_id=1,
        segment_files=[segments_dir / "seg_track1_0.m4s", segments_dir / "seg_track1_1.m4s"],
    )


def test_validate_continuous_rejects_internal_asset_boundaries():
    package = SimpleNamespace(boundaries={0, 1}, video_renditions=[], audio_rendition=None)
    with pytest.raises(RuntimeError, match="internal asset-boundary"):
        Channel._validate_continuous(package)


def test_validate_continuous_accepts_sparse_rendition_with_no_internal_boundaries(tmp_path):
    """A grave-robber/archive-derived (sparse) package is fine for
    continuity mode as long as the SOURCE itself has no discontinuity --
    i.e. boundaries == {0}, same gate a franken-ts package must pass too.
    `.sparse` alone is no longer a blanket rejection (SCOPE.md §12.6)."""
    rendition = _minimal_video_rendition(tmp_path, tfdt_version=1, sparse=True)
    package = SimpleNamespace(boundaries={0}, video_renditions=[rendition], audio_rendition=None)
    Channel._validate_continuous(package)  # must not raise


def test_validate_continuous_rejects_sparse_rendition_with_internal_boundaries(tmp_path):
    """The real gate is boundaries == {0}, not `.sparse` -- a sparse
    package that DOES have an internal join is still rejected."""
    rendition = _minimal_video_rendition(tmp_path, tfdt_version=1, sparse=True)
    package = SimpleNamespace(boundaries={0, 2}, video_renditions=[rendition], audio_rendition=None)
    with pytest.raises(RuntimeError, match="internal asset-boundary"):
        Channel._validate_continuous(package)


def test_validate_continuous_finds_tfdt_version_past_a_sparse_hole(tmp_path):
    """A sparse package's index 0 may itself be a hole (SCOPE.md §11) --
    the tfdt check must look past it to the first PRESENT segment, not
    assume index 0 exists."""
    rendition = _minimal_video_rendition(tmp_path, tfdt_version=0, sparse=True)
    rendition.segment_files[0] = None  # index 0 is a hole
    package = SimpleNamespace(boundaries={0}, video_renditions=[rendition], audio_rendition=None)
    with pytest.raises(RuntimeError, match="64-bit"):  # still finds+rejects the v0 tfdt at index 1
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
    assert f"cseg/{media_sequence}.m4s" in body


def test_continuous_segment_route_shifts_tfdt_by_loop_number(tmp_path):
    seg_dur, total = _write_continuous_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    # global index 0 -> loop 0, local 0: byte-identical to the source (shift 0).
    resp0 = client.get("/1080p/cseg/0.m4s")
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

    resp3 = client.get("/1080p/cseg/3.m4s")
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


def test_continuous_dash_audio_timeline_stays_joined_across_rolling_windows(tmp_path):
    """Audio can begin after video in each loop. The last audio S must
    extend to the next loop's audio start, including when that next entry
    is not yet present in the bounded window."""
    seg_dur, total = _write_continuous_package(tmp_path)
    channel = Channel(LoopPackage(tmp_path), epoch_ticks=0, window_segments=4, continuous=True)
    audio_offset = 1920
    channel.package.audio_rendition = SimpleNamespace(
        audio_variant={"bandwidth": 64000, "codecs": "mp4a.40.2"},
        audio_sparse=False,
        audio_segment_boundary_ticks=[audio_offset, seg_dur + audio_offset],
    )

    def timeline(xml: str, mime_type: str):
        root = ET.fromstring(xml)
        ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
        assert len(root.findall("d:Period", ns)) == 1
        adaptation = next(a for a in root.findall(".//d:AdaptationSet", ns) if a.get("mimeType") == mime_type)
        template = adaptation.find(".//d:SegmentTemplate", ns)
        segments = [(int(s.get("t")), int(s.get("d"))) for s in template.findall(".//d:S", ns)]
        return int(template.get("startNumber")), segments

    # Capture MPDs immediately before/at/after the boundary, then well into
    # the next loop when the previous loop has left the DVR window.
    for ticks in (total - 1, total, total + seg_dur, 2 * total, 2 * total + seg_dur):
        channel.now_ticks = lambda ticks=ticks: ticks
        mpd = channel.build_dash_manifest()
        video_first, video = timeline(mpd, "video/mp4")
        audio_first, audio = timeline(mpd, "audio/mp4")
        assert video_first == audio_first
        assert len(video) == len(audio) <= 4
        for (v_start, v_duration), (a_start, a_duration) in zip(video, audio):
            assert a_start == v_start + audio_offset
            assert a_duration == v_duration
        for (start, duration), (next_start, _) in zip(audio, audio[1:]):
            assert start + duration == next_start


# ── SCOPE.md §12.6: sparse (grave-robber/archive) input, single span ───────


def _self_initializing_segment(path: Path) -> None:
    """One independently-encoded, self-initializing fragment (own ftyp+moov,
    exactly one moof+mdat) -- the shape grave-robber's
    remux_segment_to_self_initializing_fragment produces for each archive
    segment (SCOPE.md §11), as opposed to _write_continuous_package's bare
    fragments sharing one init."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=0.5",
            "-c:v", "libx264", "-g", "999", "-video_track_timescale", "90000",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4",
            str(path),
        ],
        check=True,
    )


def _write_continuous_sparse_package(package_dir: Path) -> int:
    """A grave-robber-style sparse package (self-initializing segments, no
    shared init) with NO internal asset boundaries -- a single-span archive
    capture, the case SCOPE.md §12.6 now allows into continuity mode.
    Returns total_loop_duration_ticks."""
    segments_dir = package_dir / "segments" / "archive"
    segments_dir.mkdir(parents=True)
    _self_initializing_segment(segments_dir / "seg_000000.m4s")
    _self_initializing_segment(segments_dir / "seg_000001.m4s")

    def _tfdt(path: Path) -> int:
        data = path.read_bytes()
        moof = cmaf._find(data, ("moof",))
        tfdt = cmaf._find(data, ("traf", "tfdt"), moof[0] + 8, moof[1])
        return struct.unpack(">Q", data[tfdt[0] + 12 : tfdt[0] + 20])[0]

    dur0 = _tfdt(segments_dir / "seg_000000.m4s")  # 0, by construction (independent encodes)
    dur1 = _tfdt(segments_dir / "seg_000001.m4s")
    assert dur0 == 0 and dur1 == 0  # each is independently encoded, starts at its own tick 0

    # A sparse package's declared boundary ticks come from the ledger
    # (bake.py's compute_segment_list_boundary_ticks), not from re-probing
    # each independently-encoded segment's own (meaningless, always-0) tfdt
    # -- pick a nominal duration matching the real encoded content.
    segment_duration_ticks = 45_000  # 0.5s at 90kHz
    total_loop_duration_ticks = 2 * segment_duration_ticks

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
                "name": "archive",
                "sparse": True,
                "video_track_id": None,
                "audio_track_id": None,
                "segment_boundary_ticks": [0, segment_duration_ticks],
                "segment_present": [True, True],
                "video_variant": {
                    "codecs": "avc1.640028", "width": 64, "height": 64,
                    "frame_rate": 24.0, "bandwidth": 500_000,
                },
                "audio_variant": None,
            }
        ],
        "source_input": "manifest.json",
        "source_markers_json": "manifest.json",
    }
    (package_dir / "loop_descriptor.json").write_text(json.dumps(descriptor))
    return total_loop_duration_ticks


def test_continuous_accepts_a_single_span_sparse_package(tmp_path):
    """Channel construction (LoopPackage load + _validate_continuous)
    succeeds for a sparse package with boundaries == {0} -- SCOPE.md §12.6's
    whole point: the source has no discontinuity of its own, so it's
    treated the same as a franken-ts encode as far as continuity goes."""
    _write_continuous_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    assert app is not None


def test_continuous_sparse_hls_has_no_map_and_no_discontinuity(tmp_path):
    _write_continuous_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    resp = client.get("/video.m3u8")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "#EXT-X-DISCONTINUITY\n" not in body
    # Self-initializing rendition -- no shared init to point #EXT-X-MAP at.
    assert "#EXT-X-MAP" not in body


def test_continuous_sparse_dash_has_no_initialization_attribute(tmp_path):
    """A self-initializing rendition's <SegmentTemplate> must omit
    `initialization=` entirely (SCOPE.md §12.6) -- there is no shared init
    segment to point it at."""
    _write_continuous_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    resp = client.get("/stream.mpd")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert body.count("<Period ") == 1
    assert "initialization=" not in body


def test_continuous_sparse_segment_route_shifts_tfdt_by_loop_number(tmp_path):
    total = _write_continuous_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, continuous=True)
    client = app.test_client()

    # global index 0 -> loop 0, local 0: unshifted (each segment's own
    # independent encode starts at its own tfdt 0).
    resp0 = client.get("/archive/cseg/0.m4s")
    assert resp0.status_code == 200
    moof0 = cmaf._find(resp0.data, ("moof",))
    tfdt0 = cmaf._find(resp0.data, ("traf", "tfdt"), moof0[0] + 8, moof0[1])
    value0 = struct.unpack(">Q", resp0.data[tfdt0[0] + 12 : tfdt0[0] + 20])[0]
    assert value0 == 0

    # global index 2 -> loop 1, local 0: same physical bytes, shifted by
    # exactly one total_loop_duration_ticks.
    resp2 = client.get("/archive/cseg/2.m4s")
    assert resp2.status_code == 200
    moof2 = cmaf._find(resp2.data, ("moof",))
    tfdt2 = cmaf._find(resp2.data, ("traf", "tfdt"), moof2[0] + 8, moof2[1])
    value2 = struct.unpack(">Q", resp2.data[tfdt2[0] + 12 : tfdt2[0] + 20])[0]
    assert value2 == total

    # the leading ftyp+moov (this rendition's own, self-initializing) must
    # survive the patch byte-for-byte -- only the tfdt inside moof changes.
    assert resp2.data[:8] == resp0.data[:8]  # same ftyp box size+type
    assert resp2.data[4:8] == b"ftyp"


# Known-valid splice_insert (tests/test_scte35_signaling.py), re-encodable by threefive.
_SPLICE_INSERT_B64 = "/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo="


def _manifests(tmp_path, *, continuous: bool):
    _write_continuous_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=6, continuous=continuous)
    client = app.test_client()
    return {
        "master": client.get("/index.m3u8").get_data(as_text=True),
        "hls": client.get("/video.m3u8").get_data(as_text=True),
        "dash": client.get("/stream.mpd").get_data(as_text=True),
    }


@pytest.mark.parametrize("continuous", [True, False])
def test_manifests_are_stamped_with_generator_version(tmp_path, continuous):
    import re

    import serve

    m = _manifests(tmp_path, continuous=continuous)
    assert serve.GENERATOR_VERSION != "unknown"
    stamp = f"Generated by Dr Strangeloop v{serve.GENERATOR_VERSION}"
    assert m["master"].splitlines()[1] == f"# {stamp}"
    assert m["hls"].splitlines()[1] == f"# {stamp}"
    assert m["dash"].splitlines()[1] == f"<!-- {stamp} -->"
    # Mode + time anchors follow the stamp, in HLS media and DASH alike.
    assert m["hls"].splitlines()[2] == "# mode: live"
    state = "on" if continuous else "off"
    assert m["hls"].splitlines()[3] == f"# continuous timeline: {state}"
    assert m["hls"].splitlines()[4].startswith("# epoch: 1970-01-01T00:00:00")
    assert m["dash"].splitlines()[2] == "<!-- mode: live -->"
    assert m["dash"].splitlines()[3] == f"<!-- continuous timeline: {state} -->"
    assert m["dash"].splitlines()[4].startswith("<!-- epoch: 1970-01-01T00:00:00")
    # HLS media: then the loop of the window's first segment, with its start.
    current = next(l for l in m["hls"].splitlines() if l.startswith("# current loop: "))
    first_loop = int(current.removeprefix("# current loop: ").split(" ")[0])
    media_seq = int(next(l for l in m["hls"].splitlines() if l.startswith("#EXT-X-MEDIA-SEQUENCE:")).split(":")[1])
    assert first_loop == media_seq // 2  # 2 segments per loop in this package
    assert re.search(r"\(starts \d{4}-\d\d-\d\dT[\d:.]+Z\)$", current)


@pytest.mark.parametrize("continuous", [True, False])
def test_hls_loop_number_comment_precedes_first_segment_of_each_loop(tmp_path, continuous):
    lines = _manifests(tmp_path, continuous=continuous)["hls"].splitlines()
    loops = [int(l.split(": ")[1].split(" ")[0]) for l in lines if l.startswith("# loop: ")]
    assert len(loops) >= 2 and loops == sorted(set(loops))
    for i, line in enumerate(lines):
        if line.startswith("# loop: "):
            nxt = next(l for l in lines[i + 1 :] if not l.startswith("#EXT-X-DISCONTINUITY") and not l.startswith("#EXT-X-MAP"))
            assert nxt.startswith("#EXT-X-PROGRAM-DATE-TIME")


def test_dash_continuous_has_loop_comment_before_first_segment_of_window_and_each_loop(tmp_path):
    import re

    body = _manifests(tmp_path, continuous=True)["dash"]
    assert body.count("<Period ") == 1
    loops = [int(n) for n in re.findall(r"<!-- loop (\d+) \(starts [^)]+\) -->", body)]
    assert loops and loops == sorted(loops)
    # Comment sits immediately before an <S> of the loop's first segment.
    assert re.search(r"<!-- loop \d+ \(starts [^)]+\) -->\n\s+<S t=", body)


def test_dash_default_mode_has_loop_comment_inside_each_period_before_first_segment(tmp_path):
    import re

    body = _manifests(tmp_path, continuous=False)["dash"]
    assert "<!-- loop" not in body.split("<Period ")[0]  # nothing before the first Period
    periods = re.findall(
        r'<Period id="loop(\d+)".*?<SegmentTimeline>\n\s+<!-- loop (\d+) \(starts [^)]+\) -->\n\s+<S t=', body, re.S
    )
    assert len(periods) >= 1
    assert all(a == b for a, b in periods)



def _marker(event_id, pts, type_id, *, out=True, duration=None):
    return {
        "event_id": event_id,
        "pts_time_ticks": pts,
        "segmentation_type_id": type_id,
        "segmentation_duration_ticks": duration,
        "is_out": out,
        "splice_command_b64": _SPLICE_INSERT_B64,
        "splice_command_b64_narrowed": _SPLICE_INSERT_B64,
    }


def _set_markers(package_dir, markers, **extra):
    path = package_dir / "loop_descriptor.json"
    descriptor = json.loads(path.read_text())
    descriptor["markers"] = markers
    descriptor.update(extra)
    path.write_text(json.dumps(descriptor))


@pytest.mark.parametrize("continuous", [True, False])
def test_marker_comments_use_compact_codes_and_event_ids(tmp_path, continuous):
    import re

    _write_continuous_package(tmp_path)
    _set_markers(tmp_path, [_marker("0x00000001", 0, "0x34", duration=45_000)])
    client = create_app(tmp_path, epoch_ticks=0, window_segments=6, continuous=continuous).test_client()

    hls = client.get("/video.m3u8").get_data(as_text=True).splitlines()
    marker_lines = [i for i, l in enumerate(hls) if l.startswith("# markers @ ")]
    assert marker_lines, "no marker description in HLS"
    for i in marker_lines:
        assert re.fullmatch(r"# markers @ \S+Z: PPOs \(1\)", hls[i]), hls[i]
        iso = hls[i].split(" ")[3].rstrip(":")
        assert f'START-DATE="{iso}"' in hls[i + 1]  # right before the marker tags

    dash = client.get("/stream.mpd").get_data(as_text=True)
    descriptions = re.findall(r"<!-- markers @ ([^>]*) -->\n\s+<EventStream", dash)
    assert descriptions
    assert all(re.match(r"\S+Z( \(loop \d+\))?: PPOs \(1\)$", d) for d in descriptions)
    if continuous:  # one Period spans several loops, so each group names its loop
        assert "(loop " in descriptions[0]


def test_coincident_markers_share_one_timestamp_and_others_get_their_own(tmp_path):
    import re

    _write_continuous_package(tmp_path)
    # PPOs + a coincident DPOs at 0, then a PPOe later in the same loop.
    _set_markers(
        tmp_path,
        [
            _marker("0x00000001", 0, "0x34", duration=45_000),
            _marker("0x00000002", 0, "0x36", duration=45_000),
            _marker("0x00000001", 45_000, "0x35", out=False),
        ],
    )
    client = create_app(tmp_path, epoch_ticks=0, window_segments=6, continuous=False).test_client()

    hls = client.get("/video.m3u8").get_data(as_text=True)
    assert re.search(r"# markers @ (\S+Z): PPOs \(1\), DPOs \(2\)\n", hls)
    # the later marker sits in the next segment, with its own (different) time
    times = re.findall(r"# markers @ (\S+Z): ", hls)
    assert len(set(times)) == 2

    dash = client.get("/stream.mpd").get_data(as_text=True)
    # each Period lists its coincident pair on one line, the later PPOe on its own
    assert re.search(
        r"<!-- markers @ (\S+Z): PPOs \(1\), DPOs \(2\) -->\n"
        r"\s+<!-- markers @ (?!\1)\S+Z: PPOe \(1\) -->\n\s+<EventStream",
        dash,
    )


def test_marker_comments_show_original_event_id_when_incrementing(tmp_path):
    import re

    _write_continuous_package(tmp_path)
    _set_markers(tmp_path, [_marker("0x00000064", 0, "0x34", duration=45_000)], increment_event_ids=True)
    client = create_app(
        tmp_path, epoch_ticks=round(time.time() * 90_000) - 5 * 90_000, window_segments=6, continuous=False
    ).test_client()

    for body in (
        client.get("/video.m3u8").get_data(as_text=True),
        client.get("/stream.mpd").get_data(as_text=True),
    ):
        m = re.search(r"PPOs \((\d+), orig 100\)", body)
        assert m and m.group(1) != "100"  # remapped past loop 0, original alongside
