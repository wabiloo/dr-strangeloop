"""Startover / catchup (SCOPE.md §13): parameter parsing, range resolution,
and the manifests/segments served for a time-shifted range.

Uses the same tiny real-ffmpeg package as test_serve_continuity.py: 2
segments/loop, 1 loop = 90000 ticks (1s), so a range of a few seconds spans
many loop wraps.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import continuity  # noqa: E402
from serve import Channel, LoopPackage, create_app  # noqa: E402
from test_serve_continuity import _write_continuous_package  # noqa: E402
from timeshift import (  # noqa: E402
    TimeshiftConfig,
    TimeshiftError,
    parse_bool,
    parse_instant_ticks,
    resolve_window,
)

TS = 90_000


# ── parsing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected_seconds",
    [
        ("1700000000", 1700000000),
        ("1700000000.5", 1700000000.5),
        ("1700000000000", 1700000000),  # milliseconds
        ("2023-11-14T22:13:20Z", 1700000000),
        ("2023-11-14T22:13:20+00:00", 1700000000),
        ("2023-11-14T23:13:20+01:00", 1700000000),
        ("2023-11-14T22:13:20", 1700000000),  # naive == UTC
        ("2023-11-14T22:13:20.500Z", 1700000000.5),
    ],
)
def test_parse_instant_accepts_epoch_and_iso8601(value, expected_seconds):
    assert parse_instant_ticks(value, TS) == int(expected_seconds * TS)


@pytest.mark.parametrize("value", ["", "tomorrow", "2023-13-45T00:00:00Z", "12abc"])
def test_parse_instant_rejects_garbage(value):
    with pytest.raises(TimeshiftError):
        parse_instant_ticks(value, TS, "start")


def test_parse_bool():
    assert parse_bool("true", "x") and parse_bool("1", "x") and parse_bool("YES", "x")
    assert not parse_bool("false", "x") and not parse_bool("0", "x") and not parse_bool("", "x")
    with pytest.raises(TimeshiftError):
        parse_bool("maybe", "x")


def test_config_rejects_duplicate_param_names():
    with pytest.raises(ValueError):
        TimeshiftConfig(enabled=True, start_param="t", end_param="t")


# ── resolve_window (pure integer math) ──────────────────────────────────────

D = TS  # one loop = 1s; 2 segments of 0.5s
BOUNDS = [0, D // 2]


def _resolve(start, end, now, *, full_loop=False, max_span=3600):
    return resolve_window(
        start_ticks=start,
        end_ticks=end,
        now_ticks=now,
        epoch_ticks=0,
        total_loop_duration_ticks=D,
        segment_boundary_ticks=BOUNDS,
        segments_per_loop=2,
        max_span_ticks=max_span * TS,
        full_loop=full_loop,
    )


def test_resolve_window_snaps_to_segments_and_ends():
    # start inside loop 3 second segment; end exactly at the end of loop 4
    w = _resolve(3 * D + D // 2 + 10, 5 * D, now=100 * D)
    assert (w.first_global, w.last_global) == (7, 9)
    assert w.ended and w.origin_loop == 3


def test_resolve_window_growing_when_end_in_future():
    w = _resolve(3 * D, 50 * D, now=10 * D + 1)
    assert not w.ended
    assert w.first_global == 6
    assert w.last_global == 20  # live edge: loop 10, segment 0


def test_resolve_window_open_ended_is_capped_at_max_span():
    w = _resolve(0, None, now=10_000 * D, max_span=10)
    assert w.last_global == 2 * 10 - 1 and w.ended


def test_resolve_window_full_loop_widens_to_whole_loops():
    # start mid loop 3 -> loop 3 start; end just after loop 4 start -> end of loop 4
    w = _resolve(3 * D + 12345, 4 * D + 1, now=100 * D, full_loop=True)
    assert w.first_global == 6
    assert w.last_global == 9  # end of loop 4 == last segment of loop 4
    # end exactly on a loop boundary is unchanged
    assert _resolve(3 * D + 5, 5 * D, now=100 * D, full_loop=True).last_global == 9


def test_resolve_window_full_loop_open_ended_uses_whole_loops_only():
    w = _resolve(3 * D + 5, None, now=10_000 * D, full_loop=True, max_span=10)
    assert w.first_global == 6 and w.last_global == 6 + 2 * 10 - 1


def test_resolve_window_full_loop_span_is_checked_after_snapping():
    # raw span 1.2 loops fits under 1.5 loops, widened to 3 whole loops it doesn't
    with pytest.raises(TimeshiftError):
        _resolve(D - 100, 2 * D + 100, now=100 * D, full_loop=True, max_span=1)  # 1s < 2 loops


@pytest.mark.parametrize(
    "start,end,now",
    [
        (-1, None, 10 * D),  # before epoch
        (11 * D, None, 10 * D),  # future start
        (5 * D, 5 * D, 10 * D),  # empty
        (5 * D, 4 * D, 10 * D),  # reversed
        (0, 5000 * D, 10_000 * D),  # over max span (3600s)
    ],
)
def test_resolve_window_rejects_bad_ranges(start, end, now):
    with pytest.raises(TimeshiftError):
        _resolve(start, end, now)


# ── served manifests ────────────────────────────────────────────────────────

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _app(tmp_path, *, continuous=False, enabled=True, **cfg):
    _write_continuous_package(tmp_path)
    # epoch 60 loops (seconds) in the past, so `now` is loop ~60
    epoch = round(time.time() * TS) - 60 * D
    app = create_app(
        tmp_path,
        epoch_ticks=epoch,
        window_segments=4,
        continuous=continuous,
        timeshift=TimeshiftConfig(enabled=enabled, **cfg),
    )
    return app.test_client(), epoch / TS


def _seg_lines(body):
    return [l for l in body.splitlines() if l and not l.startswith("#")]


@ffmpeg
def test_no_params_is_unchanged_live(tmp_path):
    client, _ = _app(tmp_path)
    body = client.get("/video.m3u8").get_data(as_text=True)
    assert "PLAYLIST-TYPE" not in body and "ENDLIST" not in body
    assert len(_seg_lines(body)) == 4


@ffmpeg
def test_params_are_ignored_when_timeshift_disabled(tmp_path):
    client, epoch = _app(tmp_path, enabled=False)
    body = client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 8}").get_data(as_text=True)
    assert "ENDLIST" not in body and len(_seg_lines(body)) == 4


@ffmpeg
def test_catchup_hls_non_continuous_is_vod_with_one_discontinuity_per_wrap(tmp_path):
    client, epoch = _app(tmp_path)
    resp = client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 8}")
    assert resp.status_code == 200
    assert "immutable" in resp.headers["Cache-Control"]
    body = resp.get_data(as_text=True)
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in body and body.rstrip().endswith("#EXT-X-ENDLIST")
    uris = _seg_lines(body)
    assert len(uris) == 6  # 3 loops x 2 segments
    assert uris == [f"1080p/seg/{i}.m4s" for i in (0, 1, 0, 1, 0, 1)]  # local, like live
    assert body.count("#EXT-X-DISCONTINUITY\n") == 2  # one per wrap inside the range


@ffmpeg
def test_catchup_hls_continuous_uses_cseg_and_no_discontinuity(tmp_path):
    client, epoch = _app(tmp_path)
    body = client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 8}&continuous_timeline=true").get_data(
        as_text=True
    )
    uris = _seg_lines(body)
    assert [u.split("/")[1] for u in uris] == ["cseg"] * 6
    assert [int(re.search(r"(\d+)\.m4s", u).group(1)) for u in uris] == list(range(10, 16))
    assert "#EXT-X-DISCONTINUITY\n" not in body


@ffmpeg
def test_continuous_server_can_opt_out_per_request(tmp_path):
    client, epoch = _app(tmp_path, continuous=True)
    live = client.get("/video.m3u8").get_data(as_text=True)
    assert "cseg/" in live and "DISCONTINUITY\n" not in live
    body = client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 7}&continuous_timeline=false").get_data(
        as_text=True
    )
    assert "cseg/" not in body and "1080p/seg/0.m4s" in body
    assert body.count("#EXT-X-DISCONTINUITY\n") == 1


@ffmpeg
def test_growing_startover_is_event_style_until_end_passes(tmp_path):
    client, epoch = _app(tmp_path)
    body = client.get(f"/video.m3u8?start={epoch + 50}&end={epoch + 500}").get_data(as_text=True)
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in body and "ENDLIST" not in body
    # from the start point up to the live edge (~loop 60): ~20 segments, well past the 4-seg live window
    assert len(_seg_lines(body)) >= 18
    assert body.count("#EXT-X-MEDIA-SEQUENCE:100") == 1


@ffmpeg
def test_open_ended_startover_grows_and_respects_max_span(tmp_path):
    client, epoch = _app(tmp_path, max_span_seconds=5)
    body = client.get(f"/video.m3u8?start={epoch + 10}").get_data(as_text=True)
    assert "ENDLIST" in body and len(_seg_lines(body)) == 10  # capped: 5 loops x 2


@ffmpeg
def test_full_loop_widens_start_and_end_to_loop_boundaries(tmp_path):
    client, epoch = _app(tmp_path)
    body = client.get(f"/video.m3u8?start={epoch + 5.6}&end={epoch + 7.2}&full_loop=true").get_data(as_text=True)
    uris = _seg_lines(body)
    # loops 5, 6 and 7 in full: start floored to 5.0, end 7.2 ceiled to 8.0
    assert len(uris) == 6
    first_seq = int(re.search(r"EXT-X-MEDIA-SEQUENCE:(\d+)", body).group(1))
    assert first_seq % 2 == 0
    assert body.count("#EXT-X-PROGRAM-DATE-TIME") == 6


@ffmpeg
def test_master_playlist_propagates_timeshift_params(tmp_path):
    client, epoch = _app(tmp_path)
    q = f"start={epoch + 5}&end={epoch + 8}&full_loop=true&unrelated=1"
    body = client.get(f"/index.m3u8?{q}").get_data(as_text=True)
    variant = next(l for l in body.splitlines() if "m3u8?" in l)
    assert "start=" in variant and "end=" in variant and "full_loop=true" in variant
    assert "unrelated" not in variant  # only configured params are forwarded
    assert "?" not in client.get("/index.m3u8").get_data(as_text=True)


@ffmpeg
def test_custom_param_names(tmp_path):
    client, epoch = _app(tmp_path, start_param="from", end_param="to", full_loop_param="whole")
    body = client.get(f"/video.m3u8?from={epoch + 5}&to={epoch + 7}").get_data(as_text=True)
    assert "ENDLIST" in body
    # the default names mean nothing here
    assert "ENDLIST" not in client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 7}").get_data(as_text=True)


@ffmpeg
@pytest.mark.parametrize(
    "query",
    [
        "end=1700000000",  # end without start
        "start=nonsense",
        "start=99999999999999",  # future
        "start=1&end=2",  # before epoch
        "start=$S&end=$S1",  # malformed end
        "start=$S&continuous_timeline=maybe",
        "start=$S&full_loop=2",
    ],
)
def test_bad_requests_are_400_with_a_message(tmp_path, query):
    client, epoch = _app(tmp_path)
    query = query.replace("$S1", "x").replace("$S", str(epoch + 5))
    resp = client.get(f"/video.m3u8?{query}")
    assert resp.status_code == 400 and resp.get_data(as_text=True).strip()


@ffmpeg
def test_span_over_max_is_400(tmp_path):
    client, epoch = _app(tmp_path, max_span_seconds=3)
    assert client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 9}").status_code == 400
    assert client.get(f"/video.m3u8?start={epoch + 5}&end={epoch + 8}").status_code == 200


# ── DASH ────────────────────────────────────────────────────────────────────


@ffmpeg
def test_catchup_dash_non_continuous_is_static_with_a_period_per_loop(tmp_path):
    client, epoch = _app(tmp_path)
    mpd = client.get(f"/stream.mpd?start={epoch + 5}&end={epoch + 8}").get_data(as_text=True)
    assert 'type="static"' in mpd and "mediaPresentationDuration" in mpd
    assert "availabilityStartTime" not in mpd and "timeShiftBufferDepth" not in mpd
    assert mpd.count("<Period ") == 3
    assert mpd.count('start="PT0.0S"') == 1  # rebased: range starts at presentation 0
    assert re.search(r'mediaPresentationDuration="PT3(\.0)?S"', mpd)


@ffmpeg
def test_catchup_dash_continuous_is_one_period_with_rebased_timeline(tmp_path):
    client, epoch = _app(tmp_path)
    mpd = client.get(f"/stream.mpd?start={epoch + 5}&end={epoch + 8}&continuous_timeline=true").get_data(
        as_text=True
    )
    assert 'type="static"' in mpd and mpd.count("<Period ") == 1
    assert "cseg/$Number$.m4s" in mpd
    # first segment t is absolute (== the tfdt cseg writes); PTO rebases it to 0
    first_t = int(re.search(r'<S t="(\d+)"', mpd).group(1))
    assert f'presentationTimeOffset="{first_t}"' in mpd


@ffmpeg
def test_growing_dash_is_dynamic_anchored_at_range_start(tmp_path):
    client, epoch = _app(tmp_path)
    mpd = client.get(f"/stream.mpd?start={epoch + 50}&end={epoch + 500}").get_data(as_text=True)
    assert 'type="dynamic"' in mpd and "timeShiftBufferDepth" not in mpd
    ast = re.search(r'availabilityStartTime="([^"]+)"', mpd).group(1)
    assert ast.startswith("20")  # a real wall-clock instant, not the channel epoch
    assert mpd.count("<Period ") >= 9


@ffmpeg
def test_live_dash_unchanged(tmp_path):
    client, _ = _app(tmp_path)
    mpd = client.get("/stream.mpd").get_data(as_text=True)
    assert 'type="dynamic"' in mpd and "timeShiftBufferDepth" in mpd


# ── segment routes & URL forms ──────────────────────────────────────────────


@ffmpeg
def test_seg_is_always_local_and_cseg_always_global(tmp_path):
    client, _ = _app(tmp_path)  # NOT started in continuous mode
    assert client.get("/1080p/seg/1.m4s").status_code == 200
    assert client.get("/1080p/cseg/121.m4s").status_code == 200  # opt-in per request is supported


@ffmpeg
def test_cseg_404s_when_package_cannot_be_continuous(tmp_path, monkeypatch):
    _write_continuous_package(tmp_path)
    monkeypatch.setattr(
        Channel, "_validate_continuous", staticmethod(lambda pkg: (_ for _ in ()).throw(RuntimeError("nope")))
    )
    app = create_app(tmp_path, epoch_ticks=0, timeshift=TimeshiftConfig(enabled=True))
    client = app.test_client()
    assert client.get("/1080p/cseg/3.m4s").status_code == 404
    resp = client.get("/video.m3u8?start=1&continuous_timeline=true")
    assert resp.status_code == 400 and "nope" in resp.get_data(as_text=True)


@ffmpeg
def test_health_reports_timeshift_config(tmp_path):
    client, _ = _app(tmp_path, max_span_seconds=123)
    ts = client.get("/health").get_json()["timeshift"]
    assert ts["enabled"] and ts["max_span_seconds"] == 123 and ts["continuous_supported"]
    assert ts["start_param"] == "start" and ts["full_loop_param"] == "full_loop"
    off, _ = _app(tmp_path / "off" if (tmp_path / "off").mkdir() is None else tmp_path, enabled=False)
    assert off.get("/health").get_json()["timeshift"] == {"enabled": False}


def test_segment_uri_forms():
    ch = object.__new__(Channel)
    ch.continuous = False
    w = type("W", (), {"origin_loop": 7})()
    assert ch.segment_uri("r/seg/{index}.ts", 99, 1, None) == "r/seg/1.ts"
    ch.continuous = True
    assert ch.segment_uri("r/seg/{index}.ts", 99, 1, None) == "r/cseg/99.ts"
    assert ch.segment_uri("r/seg/{index}.m4s", 99, 1, w) == "r/cseg/99.m4s"
    assert ch.segment_uri("r/seg/{index}.ts", 99, 1, w) == "r/rseg/7/99.ts"
    assert ch.segment_uri("audio/seg/{index}.ts", 99, 1, w) == "audio/rseg/7/99.ts"


@ffmpeg
def test_rseg_shifts_ts_timestamps_relative_to_range_origin(tmp_path):
    """HLS-TS continuous ranges must not carry the since-the-epoch shift
    (33-bit PTS wrap, SCOPE.md §13.5)."""
    seg_dur, total = _write_continuous_package(tmp_path)
    # a TS file per local segment, plus flip the package to hls_format=ts
    ts_dir = tmp_path / "hls-ts" / "1080p"
    ts_dir.mkdir(parents=True)
    for i in (0, 1):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=0.5",
             "-c:v", "libx264", "-f", "mpegts", str(ts_dir / f"{i}.ts")],
            check=True,
        )
    desc = json.loads((tmp_path / "loop_descriptor.json").read_text())
    desc["hls_format"] = "ts"
    (tmp_path / "loop_descriptor.json").write_text(json.dumps(desc))

    epoch = round(time.time() * TS) - 100_000 * total  # range is ~28h after the epoch
    app = create_app(tmp_path, epoch_ticks=epoch, continuous=True, timeshift=TimeshiftConfig(enabled=True))
    client = app.test_client()
    start = epoch / TS + 99_990 * total / TS
    body = client.get(f"/video.m3u8?start={start}&end={start + 4 * total / TS}").get_data(as_text=True)
    uris = _seg_lines(body)
    assert uris and all("/rseg/" in u for u in uris)
    origin = int(re.search(r"/rseg/(\d+)/", uris[0]).group(1))
    assert origin >= 99_980

    def first_pts(data: bytes) -> int:
        return min(continuity_pts(data))

    def continuity_pts(data: bytes) -> list[int]:
        import test_continuity as tc

        return tc._extract_all_pts_dts(data)

    unshifted = first_pts((ts_dir / "0.ts").read_bytes())
    resp = client.get("/" + uris[0])
    assert resp.status_code == 200
    assert first_pts(resp.data) == unshifted  # first loop of the range: shift 0, not origin*D
    # a later loop in the range is shifted by whole loops relative to the origin only
    later = next(u for u in uris if int(re.search(r"(\d+)\.ts", u).group(1)) >= 2 * (origin + 2))
    assert first_pts(client.get("/" + later).data) == unshifted + 2 * total
