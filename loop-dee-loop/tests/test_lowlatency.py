"""Low-latency DASH (lowlatency.py + serve.py --ll-dash): chunk release
schedule, pacing on a fake clock, the MPD attributes, and that the default
(non-LL) output is untouched."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cmaf  # noqa: E402
import lowlatency  # noqa: E402
import serve  # noqa: E402
from serve import Channel, create_app  # noqa: E402
from test_serve_continuity import _write_continuous_package  # noqa: E402

TS = 90_000
NS = {"d": "urn:mpeg:dash:schema:mpd:2011"}


# ── pure helpers ───────────────────────────────────────────────────────────


def test_release_offsets_follow_the_real_tfdts_and_end_with_the_segment():
    # Uneven chunks (13/13/13/11 frames at 25 fps), as GPAC really cuts them.
    tfdts = [306_000, 352_800, 399_600, 446_400]
    offsets = lowlatency.release_offsets_ticks(tfdts, TS, TS, 180_000)
    assert offsets == [46_800, 93_600, 140_400, 180_000]


def test_release_offsets_rescale_the_track_timescale_and_never_go_backwards():
    # Audio: 48 kHz tfdts against a 90 kHz package timescale.
    offsets = lowlatency.release_offsets_ticks([0, 24_000, 48_000], 48_000, TS, 135_000)
    assert offsets == [45_000, 90_000, 135_000]
    # A chunk claiming to end after the segment is clamped to it.
    assert lowlatency.release_offsets_ticks([0, 999_999], TS, TS, 180_000) == [180_000, 180_000]


def test_paced_releases_each_chunk_only_once_its_tick_is_reached():
    clock = [1_000]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += round(seconds * TS)

    released: list[tuple[bytes, int]] = []
    for chunk in lowlatency.paced(
        [b"a", b"b", b"c"], [2_000, 2_000 + 9_000, 2_000 + 18_000], lambda: clock[0], sleep, TS
    ):
        released.append((chunk, clock[0]))
    assert [c for c, _ in released] == [b"a", b"b", b"c"]
    for (_, at), due in zip(released, [2_000, 11_000, 20_000]):
        assert at >= due
        assert at - due <= round(0.05 * TS)  # within one wait slice
    assert max(slept) <= 0.05 + 1e-9


def test_paced_does_not_wait_for_chunks_already_due():
    clock = [50_000]
    out = list(lowlatency.paced([b"a", b"b"], [10, 20], lambda: clock[0], lambda s: pytest.fail("slept"), TS))
    assert out == [b"a", b"b"]


D = 70 * TS  # a 70 s loop
SEG = 2 * TS


@pytest.mark.parametrize(
    "now_s, start_in_loop_s, expected_start_s",
    [
        (11.0, 10.0, 10.0),  # in progress: pace against this loop's occurrence
        (9.5, 10.0, 10.0),  # starts in half a second: hold until then
        (13.0, 10.0, None),  # already complete: serve at once
        (68.5, 0.0, 70.0),  # near the wrap, index 0 is the upcoming one in the NEXT loop
        (30.0, 0.0, None),  # mid-loop, index 0 is long gone and the next one far away
        (30.0, 50.0, None),  # a far-future segment is not held for 20 s
    ],
)
def test_segment_occurrence_loop_local(now_s, start_in_loop_s, expected_start_s):
    got = lowlatency.segment_occurrence(round(now_s * TS), 0, D, round(start_in_loop_s * TS), SEG, SEG)
    assert got == (None if expected_start_s is None else round(expected_start_s * TS))


def test_segment_occurrence_continuous_uses_the_named_loop():
    # /cseg/ names the loop: loop 1's segment 0 starts at 70 s, so at 69 s it is upcoming...
    assert lowlatency.segment_occurrence(69 * TS, 0, D, 0, SEG, SEG, loop_number=1) == 70 * TS
    # ...and loop 0's is long complete.
    assert lowlatency.segment_occurrence(69 * TS, 0, D, 0, SEG, SEG, loop_number=0) is None


# ── a real chunked package ─────────────────────────────────────────────────

pytestmark_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _write_chunked_package(
    package_dir: Path, chunks_per_segment: int = 4, names: tuple[str, ...] = ("1080p",)
) -> tuple[int, int]:
    """Two segments of `chunks_per_segment` real fMP4 chunks each (0.25 s). Returns
    (segment_ticks, loop_ticks)."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=3",
            "-c:v", "libx264", "-g", "6", "-keyint_min", "6", "-sc_threshold", "0",
            "-video_track_timescale", "90000",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4",
            str(package_dir / "a.mp4"),
        ],
        check=True,
    )
    init, fragments = cmaf.split_init_and_fragments((package_dir / "a.mp4").read_bytes())
    assert len(fragments) >= 2 * chunks_per_segment
    segments = [
        b"".join(fragments[i * chunks_per_segment : (i + 1) * chunks_per_segment]) for i in range(2)
    ]
    starts = [lowlatency.split_chunks(seg)[0][1] for seg in segments]
    segment_ticks = starts[1] - starts[0]
    loop_ticks = 2 * segment_ticks

    # What a real bake records (bake.max_chunk_ticks): LL-HLS PART-TARGET must cover the longest part.
    max_chunk = max(c.duration_ticks for seg in segments for c in lowlatency.chunk_table(seg, TS, TS, segment_ticks))
    for name in names:
        seg_dir = package_dir / "segments" / name
        seg_dir.mkdir(parents=True)
        (seg_dir / "seg_track1_init.mp4").write_bytes(init)
        for i, seg in enumerate(segments):
            (seg_dir / f"seg_track1_{i}.m4s").write_bytes(seg)
    descriptor = {
        "version": 2,
        "timescale": 90_000,
        "total_loop_duration_ticks": loop_ticks,
        "segment_duration_seconds": segment_ticks / TS,
        "chunk_duration_seconds": segment_ticks / TS / chunks_per_segment,
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
                "name": name,
                "sparse": False,
                "video_track_id": 1,
                "audio_track_id": None,
                "segment_boundary_ticks": starts,
                "video_variant": {
                    "codecs": "avc1.640028", "width": 64, "height": 64,
                    "frame_rate": 24.0, "bandwidth": 500_000,
                },
                "audio_variant": None,
                "max_chunk_ticks": max_chunk,
            }
            for name in names
        ],
        "source_input": "a.mp4",
    }
    (package_dir / "loop_descriptor.json").write_text(json.dumps(descriptor))
    return segment_ticks, loop_ticks


@pytest.fixture
def chunked(tmp_path):
    seg, loop = _write_chunked_package(tmp_path)
    return tmp_path, seg, loop


@pytestmark_ffmpeg
def test_ll_mpd_advertises_early_availability_latency_and_clock(chunked, monkeypatch):
    path, seg, loop = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    app = create_app(path, epoch_ticks=0, window_segments=4, ll_dash=True, ll_utc_timing="http")
    mpd = app.test_client().get("/index.mpd").get_data(as_text=True)
    root = ET.fromstring(mpd)  # well-formed
    chunk_s = seg / TS / 4
    template = root.find(".//d:SegmentTemplate", NS)
    assert float(template.get("availabilityTimeOffset")) == pytest.approx(seg / TS - chunk_s, abs=1e-3)
    assert template.get("availabilityTimeComplete") == "false"
    latency = root.find("d:ServiceDescription/d:Latency", NS)
    assert int(latency.get("target")) == max(round(4 * chunk_s * 1000), 1000)
    assert int(latency.get("min")) < int(latency.get("target")) < int(latency.get("max"))
    timing = root.find("d:UTCTiming", NS)
    assert timing.get("schemeIdUri") == "urn:mpeg:dash:utc:http-xsdate:2014"
    assert timing.get("value").endswith("/time")
    assert root.get("minBufferTime") == "PT1S"
    # ServiceDescription precedes the Period (schema order).
    children = [c.tag.split("}")[1] for c in root]
    assert children.index("ServiceDescription") < children.index("Period")


@pytestmark_ffmpeg
def test_non_ll_mpd_has_none_of_it(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    mpd = create_app(path, epoch_ticks=0, window_segments=4).test_client().get("/index.mpd").get_data(as_text=True)
    for needle in ("availabilityTimeOffset", "availabilityTimeComplete", "ServiceDescription", "http-xsdate"):
        assert needle not in mpd


@pytestmark_ffmpeg
def test_ll_dash_refuses_a_package_without_chunks(tmp_path):
    _write_continuous_package(tmp_path)
    with pytest.raises(ValueError, match="chunk-duration"):
        create_app(tmp_path, epoch_ticks=0, ll_dash=True)


@pytestmark_ffmpeg
def test_configured_utc_timing_url_wins(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    app = create_app(path, epoch_ticks=0, ll_dash=True, ll_utc_timing="http", ll_utc_timing_url="https://cdn.example/time")
    mpd = app.test_client().get("/index.mpd").get_data(as_text=True)
    assert 'value="https://cdn.example/time"' in mpd


@pytestmark_ffmpeg
def test_time_endpoint_is_uncached_and_in_the_channel_clock(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: 90_000 * 3600)
    resp = create_app(path, epoch_ticks=0).test_client().get("/time")
    assert resp.get_data(as_text=True) == "1970-01-01T01:00:00.000Z"
    assert resp.headers["Cache-Control"] == "no-store"


@pytestmark_ffmpeg
def test_in_progress_segment_streams_one_chunk_per_release_time(chunked, monkeypatch):
    path, seg, loop = chunked
    clock = [seg // 10]  # 0.2 s into segment 0

    monkeypatch.setattr(Channel, "now_ticks", lambda self: clock[0])
    monkeypatch.setattr(serve.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + round(s * TS)))
    app = create_app(path, epoch_ticks=0, window_segments=4, ll_dash=True)

    on_disk = (path / "segments" / "1080p" / "seg_track1_0.m4s").read_bytes()
    expected_offsets = lowlatency.release_offsets_ticks(
        [t for _, t in lowlatency.split_chunks(on_disk)], TS, TS, seg
    )

    resp = app.test_client().get("/1080p/seg/0.m4s", buffered=False)
    assert resp.status_code == 200
    assert "Content-Length" not in resp.headers  # streamed -> chunked transfer encoding
    arrivals, body = [], b""
    for piece in resp.response:
        arrivals.append(clock[0])
        body += piece
    assert body == on_disk  # byte-identical once complete
    assert len(arrivals) == len(expected_offsets) == 4
    for at, due in zip(arrivals, expected_offsets):
        assert due <= at <= due + round(0.06 * TS)
    assert arrivals[-1] >= seg  # the last chunk comes with the segment end


@pytestmark_ffmpeg
def test_without_ll_a_segment_is_one_plain_response(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg // 10)
    monkeypatch.setattr(serve.time, "sleep", lambda s: pytest.fail("must not pace"))
    resp = create_app(path, epoch_ticks=0).test_client().get("/1080p/seg/0.m4s")
    assert resp.get_data() == (path / "segments" / "1080p" / "seg_track1_0.m4s").read_bytes()


# ── --ll-utc-timing modes ──────────────────────────────────────────────────


def _utc_timing(mpd: str):
    return ET.fromstring(mpd).find("d:UTCTiming", NS)


@pytestmark_ffmpeg
def test_utc_timing_direct_writes_the_time_and_is_never_cached(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: 90_000 * 3600)
    resp = create_app(path, epoch_ticks=0, ll_dash=True, ll_utc_timing="direct").test_client().get("/index.mpd")
    timing = _utc_timing(resp.get_data(as_text=True))
    assert timing.get("schemeIdUri") == "urn:mpeg:dash:utc:direct:2014"
    assert timing.get("value") == "1970-01-01T01:00:00.000Z"
    assert resp.headers["Cache-Control"] == "no-store"


@pytestmark_ffmpeg
def test_utc_timing_none_omits_it(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    resp = create_app(path, epoch_ticks=0, ll_dash=True, ll_utc_timing="none").test_client().get("/index.mpd")
    assert _utc_timing(resp.get_data(as_text=True)) is None
    assert "Cache-Control" not in resp.headers or "no-store" not in resp.headers["Cache-Control"]


@pytestmark_ffmpeg
def test_utc_timing_direct_is_the_default(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    resp = create_app(path, epoch_ticks=0, ll_dash=True).test_client().get("/index.mpd")
    assert _utc_timing(resp.get_data(as_text=True)).get("schemeIdUri") == "urn:mpeg:dash:utc:direct:2014"
    assert resp.headers["Cache-Control"] == "no-store"


@pytestmark_ffmpeg
def test_utc_timing_http_derives_its_url_only_for_a_localhost_request(chunked, monkeypatch):
    path, seg, _ = chunked
    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    client = create_app(path, epoch_ticks=0, ll_dash=True, ll_utc_timing="http").test_client()
    local = _utc_timing(client.get("/index.mpd", base_url="http://localhost:8080").get_data(as_text=True))
    assert local.get("value") == "http://localhost:8080/time"
    assert "no-store" not in client.get("/index.mpd").headers.get("Cache-Control", "")  # http: the MPD stays cacheable
    # A forged / proxied Host is client-controlled: it must never reach the MPD (a shared cache could keep it).
    assert _utc_timing(client.get("/index.mpd", base_url="http://evil.example").get_data(as_text=True)) is None
    assert _utc_timing(client.get("/index.mpd", headers={"Host": "evil.example"}).get_data(as_text=True)) is None


def test_loopback_host_detection():
    for ok in ("localhost", "localhost:8080", "127.0.0.1:9000", "[::1]:80", "[::1]", "channel.localhost", "LOCALHOST"):
        assert serve._is_loopback_host(ok), ok
    for bad in ("evil.example", "localhost.evil.example", "127.0.0.1.evil.example", "10.0.0.5:8080", "x.cloudfront.net", ""):
        assert not serve._is_loopback_host(bad), bad


@pytestmark_ffmpeg
def test_utc_timing_rejects_unknown_mode(chunked):
    path, _, _ = chunked
    with pytest.raises(ValueError, match="ll_utc_timing"):
        create_app(path, epoch_ticks=0, ll_dash=True, ll_utc_timing="ntp")


# ── per-request overrides (?low_latency=, ?ll_target_latency=, ?ll_utc_timing=) ───────────────


def _qapp(path, monkeypatch, seg, **kw):
    from timeshift import TimeshiftConfig

    monkeypatch.setattr(Channel, "now_ticks", lambda self: seg + seg // 2)
    overrides = kw.pop("overrides", True)
    return create_app(path, epoch_ticks=0, window_segments=4, timeshift=TimeshiftConfig(overrides=overrides), **kw).test_client()


def _is_ll(mpd: str) -> bool:
    return "availabilityTimeOffset" in mpd and "ServiceDescription" in mpd


@pytestmark_ffmpeg
def test_low_latency_false_turns_an_ll_channel_into_a_plain_mpd(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg, ll_dash=True)
    assert _is_ll(c.get("/index.mpd").get_data(as_text=True))
    plain = c.get("/index.mpd?low_latency=false").get_data(as_text=True)
    assert not _is_ll(plain) and "http-xsdate" not in plain and "availabilityTimeComplete" not in plain


@pytestmark_ffmpeg
def test_optin_channel_advertises_only_when_asked_but_always_paces(chunked, monkeypatch):
    path, seg, _ = chunked
    clock = [seg // 10]
    monkeypatch.setattr(serve.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + round(s * TS)))
    c = _qapp(path, monkeypatch, seg, ll_optin=True)
    monkeypatch.setattr(Channel, "now_ticks", lambda self: clock[0])
    assert not _is_ll(c.get("/index.mpd").get_data(as_text=True))
    assert _is_ll(c.get("/index.mpd?low_latency=true").get_data(as_text=True))
    resp = c.get("/1080p/seg/0.m4s", buffered=False)  # delivery is a server capability, not per MPD
    assert "Content-Length" not in resp.headers
    assert b"".join(resp.response) == (path / "segments" / "1080p" / "seg_track1_0.m4s").read_bytes()


@pytestmark_ffmpeg
def test_low_latency_true_is_a_400_where_the_channel_has_none(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg)  # LL not enabled
    resp = c.get("/index.mpd?low_latency=true")
    assert resp.status_code == 400 and "low latency is not available" in resp.get_data(as_text=True)
    assert c.get("/index.mpd?low_latency=false").status_code == 200  # harmless
    assert c.get("/index.m3u8?low_latency=true").status_code == 400  # validated for every format


@pytestmark_ffmpeg
def test_ll_target_latency_override_and_bounds(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg, ll_dash=True)
    mpd = c.get("/index.mpd?ll_target_latency=3").get_data(as_text=True)
    latency = ET.fromstring(mpd).find("d:ServiceDescription/d:Latency", NS)
    assert latency.get("target") == "3000" and latency.get("min") == "1500" and latency.get("max") == "6000"
    assert 'suggestedPresentationDelay="PT3S"' in mpd
    assert c.get("/index.mpd?ll_target_latency=PT2.5S").status_code == 200
    for bad in ("0", "-1", "soon", "121"):
        assert c.get(f"/index.mpd?ll_target_latency={bad}").status_code == 400, bad


@pytestmark_ffmpeg
def test_ll_utc_timing_override(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg, ll_dash=True)
    direct = c.get("/index.mpd?ll_utc_timing=direct")
    assert _utc_timing(direct.get_data(as_text=True)).get("schemeIdUri") == "urn:mpeg:dash:utc:direct:2014"
    assert direct.headers["Cache-Control"] == "no-store"
    assert _utc_timing(c.get("/index.mpd?ll_utc_timing=none").get_data(as_text=True)) is None
    assert c.get("/index.mpd?ll_utc_timing=ntp").status_code == 400


@pytestmark_ffmpeg
def test_the_utc_timing_url_is_not_overridable(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg, ll_dash=True, ll_utc_timing="http", ll_utc_timing_url="https://good.example/time")
    mpd = c.get("/index.mpd?ll_utc_timing_url=https://evil.example/time&utc_timing_url=https://evil.example/time").get_data(as_text=True)
    assert "evil.example" not in mpd and 'value="https://good.example/time"' in mpd


@pytestmark_ffmpeg
def test_overrides_off_ignores_the_ll_parameters(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg, ll_dash=True, overrides=False)
    mpd = c.get("/index.mpd?low_latency=false&ll_utc_timing=none&ll_target_latency=9").get_data(as_text=True)
    assert _is_ll(mpd) and _utc_timing(mpd) is not None and 'target="4000"' not in mpd.replace("target=\"1000\"", "")


@pytestmark_ffmpeg
def test_hls_and_timeline_json_accept_but_ignore_the_ll_parameters(chunked, monkeypatch):
    path, seg, _ = chunked
    c = _qapp(path, monkeypatch, seg, ll_dash=True)
    q = "?low_latency=false&ll_target_latency=3&ll_utc_timing=none"
    # The master playlist passes the request's query on to its child playlist URLs; nothing else differs.
    assert c.get("/index.m3u8" + q).get_data().replace(q.encode(), b"") == c.get("/index.m3u8").get_data()
    assert c.get("/timeline.json" + q).get_json() == c.get("/timeline.json").get_json()


# ── chunk table (the parts of a segment) ──────────────────────────────────────────────────────


@pytestmark_ffmpeg
def test_chunk_table_tiles_the_segment_and_follows_the_real_timestamps(chunked):
    path, seg, _ = chunked
    data = (path / "segments" / "1080p" / "seg_track1_0.m4s").read_bytes()
    table = lowlatency.chunk_table(data, TS, TS, seg)
    assert len(table) == 4
    assert sum(c.duration_ticks for c in table) == seg  # the last chunk runs to the segment end
    assert [c.start_ticks for c in table] == [0, *(sum(c.duration_ticks for c in table[:i]) for i in range(1, 4))]
    # byte spans tile the file: no gap, no overlap, the first one carries anything ahead of the first moof
    assert table[0].byte_start == 0 and table[-1].byte_end == len(data)
    assert all(a.byte_end == b.byte_start for a, b in zip(table, table[1:]))
    assert all(c.independent for c in table)  # the fixture cuts a fragment at every keyframe
    # the spans reproduce the chunks the pacer streams
    assert [data[c.byte_start : c.byte_end] for c in table][1:] == [c for c, _ in lowlatency.split_chunks(data)][1:]


@pytestmark_ffmpeg
def test_chunk_table_marks_chunks_that_do_not_start_on_a_keyframe(tmp_path):
    # Fragments cut by duration rather than at keyframes: GOP 12 frames, fragments of 6 -> every second one is not a sync start.
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=2",
            "-c:v", "libx264", "-g", "12", "-keyint_min", "12", "-sc_threshold", "0", "-video_track_timescale", "90000",
            "-movflags", "frag_custom+empty_moov+default_base_moof", "-frag_duration", "250000", "-f", "mp4", str(tmp_path / "a.mp4"),
        ],
        check=True,
    )
    _, fragments = cmaf.split_init_and_fragments((tmp_path / "a.mp4").read_bytes())
    data = b"".join(fragments[:4])
    table = lowlatency.chunk_table(data, TS, TS, sum(c.duration_ticks for c in lowlatency.chunk_table(data, TS, TS, 90_000)) or 90_000)
    flags = [c.independent for c in table]
    assert flags[0] is True and False in flags  # the first starts on a keyframe, a later one does not


def test_chunk_table_audio_is_all_independent_and_empty_input_is_empty():
    assert lowlatency.chunk_table(b"", TS, TS, 90_000) == []
