"""Low-latency HLS (Apple LL-HLS): the parts of the newest segments, the segment in progress, the preload hint,
rendition reports, part delivery and blocking playlist reload -- on a fake clock, against a real chunked package.
Also the pure helpers (`live_position`, `parts_released`) and that the non-LL playlist is untouched."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time

import lowlatency  # noqa: E402
import serve  # noqa: E402
from serve import Channel, create_app  # noqa: E402
from test_lowlatency import TS, _write_chunked_package  # noqa: E402
from timeshift import TimeshiftConfig, TimeshiftError  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# The fixture package: two renditions, two 1 s segments of four 0.25 s chunks. 55 000 ticks (0.61 s) into segment 1
# parts 0 and 1 (ending at 22 500 and 45 000) are out; part 2 (ends at 67 500) is not.
INTO_SEGMENT_1 = 55_000


@pytest.fixture
def pkg(tmp_path):
    seg, loop = _write_chunked_package(tmp_path, names=("1080p", "720p"))
    return tmp_path, seg, loop


def playlist_names(path: Path) -> list[str]:
    package = serve.LoopPackage(path)
    return [package.video_playlist_name(r) for r in package.video_renditions]


def client(path, clock, monkeypatch, *, timeshift=False, **kw):
    monkeypatch.setattr(Channel, "now_ticks", lambda self: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + round(s * TS)))
    cfg = TimeshiftConfig(timeshift=timeshift, overrides=True)
    return create_app(path, epoch_ticks=0, window_segments=4, timeshift=cfg, **kw).test_client()


def lines(resp) -> list[str]:
    return [ln for ln in resp.get_data(as_text=True).splitlines() if ln and not ln.startswith("# ")]


# ── the playlist ───────────────────────────────────────────────────────────────────────────────


def test_part_target_covers_the_longest_baked_chunk(pkg):
    path, seg, _ = pkg
    package = serve.LoopPackage(path)
    longest = max(c.duration_ticks for i in (0, 1) for c in package.chunk_table("1080p", i, seg))
    assert package.part_target_seconds == pytest.approx(longest / TS, abs=0.001)


def test_header_parts_segment_in_progress_and_preload_hint(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    out = lines(client(path, clock, monkeypatch, ll_hls=True).get(f"/{playlist_names(path)[0]}"))
    target = serve.LoopPackage(path).part_target_seconds
    assert "#EXT-X-VERSION:9" in out
    assert f"#EXT-X-PART-INF:PART-TARGET={target:.3f}" in out
    assert f"#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK={3 * target:.3f}" in out
    assert not any("SKIP" in ln for ln in out)  # delta updates are not offered

    # segment 0 is complete: four parts, then its EXTINF and URI
    first_part = next(i for i, ln in enumerate(out) if ln.startswith("#EXT-X-PART:") and "/0.0.m4s" in ln)
    assert [ln.startswith("#EXT-X-PART:") for ln in out[first_part : first_part + 4]] == [True] * 4
    assert out[first_part + 4].startswith("#EXTINF") and out[first_part + 5].endswith("/0.m4s")

    # segment 1 is in progress: its two released parts, no EXTINF / segment URI, then the hint for part 2
    in_progress = [ln for ln in out if ln.startswith("#EXT-X-PART:") and "/1." in ln]
    assert len(in_progress) == 2 and "/1.0.m4s" in in_progress[0] and "/1.1.m4s" in in_progress[1]
    assert not any(ln.endswith("/1.m4s") for ln in out)
    assert out[-2].startswith("#EXT-X-PRELOAD-HINT:TYPE=PART") and out[-2].endswith('/1.2.m4s"')

    # every part carries a duration, and the keyframe-started ones are flagged independent
    parts = [ln for ln in out if ln.startswith("#EXT-X-PART:")]
    assert all("DURATION=" in ln and ln.endswith(",INDEPENDENT=YES") for ln in parts)  # the fixture cuts at keyframes


def test_a_segment_with_no_part_released_yet_hints_its_first_part(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + 1]
    out = lines(client(path, clock, monkeypatch, ll_hls=True).get(f"/{playlist_names(path)[0]}"))
    assert next(ln for ln in out if ln.startswith("#EXT-X-PRELOAD-HINT")).endswith('/1.0.m4s"')
    assert not any(ln.startswith("#EXT-X-PART:") and "/1." in ln for ln in out)


def test_the_non_ll_playlist_has_none_of_it(pkg, monkeypatch):
    path, seg, _ = pkg
    body = client(path, [seg + INTO_SEGMENT_1], monkeypatch).get(f"/{playlist_names(path)[0]}").get_data(as_text=True)
    for needle in ("EXT-X-PART", "PRELOAD-HINT", "SERVER-CONTROL", "RENDITION-REPORT", "VERSION:9"):
        assert needle not in body


def test_per_request_on_off_and_optin(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    name = playlist_names(path)[0]
    on = client(path, clock, monkeypatch, ll_hls=True)
    assert "EXT-X-PART:" in on.get(f"/{name}").get_data(as_text=True)
    assert "EXT-X-PART" not in on.get(f"/{name}?low_latency=false").get_data(as_text=True)
    optin = client(path, clock, monkeypatch, ll_optin=True)  # delivered, but advertised only on request
    assert "EXT-X-PART" not in optin.get(f"/{name}").get_data(as_text=True)
    assert "EXT-X-PART:" in optin.get(f"/{name}?low_latency=true").get_data(as_text=True)


def test_a_startover_range_is_never_low_latency(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg * 5]  # well into the third loop, so a past range exists
    ll = client(path, clock, monkeypatch, ll_hls=True, timeshift=True)
    resp = ll.get(f"/{playlist_names(path)[0]}?start=0&end=1")
    assert resp.status_code == 200
    assert "ENDLIST" in resp.get_data(as_text=True)  # it really is the past-range playlist
    for needle in ("EXT-X-PART", "PRELOAD-HINT", "SERVER-CONTROL"):
        assert needle not in resp.get_data(as_text=True)


def test_rendition_reports_name_the_other_playlist_with_its_own_last_part(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    ll = client(path, clock, monkeypatch, ll_hls=True)
    first, second = playlist_names(path)
    reports = [ln for ln in lines(ll.get(f"/{first}")) if ln.startswith("#EXT-X-RENDITION-REPORT")]
    assert len(reports) == 1 and f'URI="{second}"' in reports[0]
    assert "LAST-MSN=1" in reports[0] and "LAST-PART=1" in reports[0]  # same timeline: segment 1, last released part 1
    # the query a request carried rides along, like every child URI a master playlist hands out
    assert any(f'URI="{second}?dvr_window=60"' in ln for ln in lines(ll.get(f"/{first}?dvr_window=60")))


# ── parts ──────────────────────────────────────────────────────────────────────────────────────


def test_a_finished_part_is_served_at_once_and_the_parts_join_into_the_segment(pkg, monkeypatch):
    # Continuous timeline (the default): the URL's global index names the loop, so "finished" is unambiguous. In
    # periodic mode `seg/<local>` is the same URL every loop, and in a loop this short the next occurrence of segment 0
    # is imminent, so the server cannot tell it from the finished one and holds it (loop-dee-loop/README, low latency).
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    ll = client(path, clock, monkeypatch, ll_hls=True, continuous=True)
    data = (path / "segments" / "1080p" / "seg_track1_0.m4s").read_bytes()
    table = serve.LoopPackage(path).chunk_table("1080p", 0, seg)
    resp = ll.get("/1080p/cseg/0.2.m4s")
    assert resp.status_code == 200 and resp.mimetype == "video/iso.segment"
    assert clock[0] == seg + INTO_SEGMENT_1  # nothing waited
    # (continuity rewrites the timestamps, so compare the structure, not the bytes: same size, one moof+mdat)
    assert len(resp.get_data()) == table[2].byte_end - table[2].byte_start
    joined = b"".join(ll.get(f"/1080p/cseg/0.{k}.m4s").get_data() for k in range(len(table)))
    assert len(joined) == len(data) and clock[0] == seg + INTO_SEGMENT_1


def test_a_part_not_due_yet_is_held_until_it_is(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    ll = client(path, clock, monkeypatch, ll_hls=True)
    table = serve.LoopPackage(path).chunk_table("1080p", 1, seg)
    due = seg + table[2].start_ticks + table[2].duration_ticks
    data = (path / "segments" / "1080p" / "seg_track1_1.m4s").read_bytes()
    resp = ll.get("/1080p/seg/1.2.m4s")
    assert resp.status_code == 200 and resp.get_data() == data[table[2].byte_start : table[2].byte_end]
    assert due <= clock[0] <= due + round(0.06 * TS)  # it waited for the part's own end, and not much longer


def test_part_requests_that_cannot_be_served(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    ll = client(path, clock, monkeypatch, ll_hls=True)
    assert ll.get("/1080p/seg/0.9.m4s").status_code == 404  # no such part
    assert ll.get("/nope/seg/0.0.m4s").status_code == 404  # no such rendition
    assert client(path, clock, monkeypatch).get("/1080p/seg/0.0.m4s").status_code == 404  # low latency not enabled


# ── blocking playlist reload ───────────────────────────────────────────────────────────────────


def test_blocking_reload_waits_for_the_part_then_lists_it(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    ll = client(path, clock, monkeypatch, ll_hls=True)
    table = serve.LoopPackage(path).chunk_table("1080p", 1, seg)
    due = seg + table[2].start_ticks + table[2].duration_ticks
    resp = ll.get(f"/{playlist_names(path)[0]}?_HLS_msn=1&_HLS_part=2")
    assert resp.status_code == 200 and due <= clock[0] <= due + round(0.06 * TS)
    out = lines(resp)
    assert any(re.match(r'#EXT-X-PART:.*/1\.2\.m4s"', ln) for ln in out)  # part 2 is a real part now...
    assert any(ln.startswith("#EXT-X-PRELOAD-HINT") and ln.endswith('/1.3.m4s"') for ln in out)  # ...and the hint moved on


def test_blocking_reload_for_a_segment_waits_for_its_end(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    resp = client(path, clock, monkeypatch, ll_hls=True).get(f"/{playlist_names(path)[0]}?_HLS_msn=1")
    assert resp.status_code == 200 and seg * 2 <= clock[0] <= seg * 2 + round(0.06 * TS)
    assert any(ln.endswith("/1.m4s") for ln in lines(resp))  # complete now: its EXTINF and URI are listed


@pytest.mark.parametrize(
    "query", ["_HLS_msn=99999", "_HLS_part=1", "_HLS_msn=x", "_HLS_msn=-1", "_HLS_msn=1&_HLS_part=9", "_HLS_msn=1&_HLS_part=y"]
)
def test_blocking_reload_refuses_what_it_cannot_serve(pkg, monkeypatch, query):
    path, seg, _ = pkg
    ll = client(path, [seg + INTO_SEGMENT_1], monkeypatch, ll_hls=True)
    assert ll.get(f"/{playlist_names(path)[0]}?{query}").status_code == 400


def test_blocking_parameters_do_nothing_on_a_playlist_that_is_not_low_latency(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg + INTO_SEGMENT_1]
    resp = client(path, clock, monkeypatch).get(f"/{playlist_names(path)[0]}?_HLS_msn=1&_HLS_part=2")
    assert resp.status_code == 200 and clock[0] == seg + INTO_SEGMENT_1  # no hold


def test_skip_is_accepted_and_ignored(pkg, monkeypatch):
    path, seg, _ = pkg
    body = client(path, [seg + INTO_SEGMENT_1], monkeypatch, ll_hls=True).get(f"/{playlist_names(path)[0]}?_HLS_skip=YES").get_data(as_text=True)
    assert "EXT-X-SKIP" not in body and "CAN-SKIP-UNTIL" not in body


# ── what the channel refuses ───────────────────────────────────────────────────────────────────


def test_ll_hls_needs_cmaf_and_chunks(pkg, tmp_path_factory):
    path, _, _ = pkg
    ts_package = serve.LoopPackage(path)
    ts_package.hls_format = "ts"
    with pytest.raises(ValueError, match="CMAF"):
        Channel(ts_package, 0, ll_hls=True)
    from test_serve_continuity import _write_continuous_package

    plain = tmp_path_factory.mktemp("plain")
    _write_continuous_package(plain)  # baked without chunks
    with pytest.raises(ValueError, match="chunk-duration"):
        Channel(serve.LoopPackage(plain), 0, ll_hls=True)


def test_ll_hls_refuses_a_chunked_package_baked_before_the_part_target_was_recorded(pkg):
    """Otherwise `--ll-hls` starts and every playlist silently stays plain (no PART-TARGET to advertise)."""
    path, _, _ = pkg
    old = serve.LoopPackage(path)
    for rendition in old.video_renditions:
        rendition.max_chunk_ticks = 0  # what a package baked by an earlier version loads as
    assert old.part_target_seconds is None
    with pytest.raises(ValueError, match="baked by an older version"):
        Channel(old, 0, ll_hls=True)
    Channel(old, 0, ll_dash=True)  # DASH never needed it


def test_requesting_low_latency_on_an_mpeg_ts_playlist_is_refused_not_ignored(pkg):
    path, _, _ = pkg
    package = serve.LoopPackage(path)
    channel = Channel(package, 0, ll_optin=True)  # DASH low latency is fine with TS HLS...
    package.hls_format = "ts"
    with pytest.raises(TimeshiftError, match="MPEG-TS"):
        channel.with_ll(True, fmt="hls")  # ...but an HLS playlist cannot carry parts
    assert channel.with_ll(True, fmt="dash").ll_dash is True


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────


def _chunks(*durations: int):
    out, at = [], 0
    for d in durations:
        out.append(lowlatency.Chunk(start_ticks=at, duration_ticks=d, independent=True, byte_start=0, byte_end=0))
        at += d
    return out


def test_parts_released_counts_the_parts_whose_end_has_passed():
    chunks = _chunks(100, 100, 50)
    assert [lowlatency.parts_released(chunks, 1000, now) for now in (1000, 1099, 1100, 1199, 1200, 1250, 9999)] == [0, 0, 1, 1, 2, 3, 3]
    assert lowlatency.part_release_tick(chunks, 1000, 2) == 1250


def test_live_position_is_the_newest_part_that_exists():
    # three segments of a 1200-tick loop: 4 x 100, 4 x 100, 2 x 200
    chunks = {0: _chunks(100, 100, 100, 100), 1: _chunks(100, 100, 100, 100), 2: _chunks(200, 200)}
    pos = lambda now: lowlatency.live_position([0, 400, 800], 1200, 0, 3, lambda i: chunks[i], now)  # noqa: E731
    assert pos(450) == (0, 3)  # segment 1 has released nothing yet: the last part of segment 0
    assert pos(500) == (1, 0)
    assert pos(701) == (1, 2)
    assert pos(850) == (1, 3)  # segment 2's first part ends at 1000
    assert pos(1000) == (2, 0)
    assert pos(1210) == (2, 1)  # the next loop's first segment (msn 3) has released nothing: the previous loop's last part
    assert pos(1300) == (3, 0)


# ── delta updates (RFC 8216bis 6.2.5.1) ────────────────────────────────────────────────────────

# 1 s segments, a 12-segment window, the clock 20 s + INTO_SEGMENT_1 in: the window is segments 9..20 and segment 20 is
# in progress (it ends at 21 s). The Skip Boundary is 6 target durations = 6 s, so a segment is skippable when it ends at
# or before 21 - 6 = 15 s: segments 9..14 (six of them). Segments 15..20 are kept.
DELTA_NOW = 20


def delta_client(path, clock, monkeypatch, *, window=12, **kw):
    monkeypatch.setattr(Channel, "now_ticks", lambda self: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + round(s * TS)))
    cfg = TimeshiftConfig(timeshift=False, overrides=True)
    return create_app(path, epoch_ticks=0, window_segments=window, timeshift=cfg, **kw).test_client()


def segment_uris(out: list[str]) -> list[str]:
    return [ln for ln in out if not ln.startswith("#") and ln.endswith(".m4s")]


def delta_pair(pkg, monkeypatch, **kw):
    path, seg, _ = pkg
    clock = [DELTA_NOW * seg + INTO_SEGMENT_1]
    client = delta_client(path, clock, monkeypatch, ll_hls=True, **kw)
    name = playlist_names(path)[0]
    return lines(client.get(f"/{name}")), lines(client.get(f"/{name}?_HLS_skip=YES")), client, name


def test_delta_updates_are_offered_only_when_there_is_something_to_skip(pkg, monkeypatch):
    full, _, _, _ = delta_pair(pkg, monkeypatch)
    control = next(ln for ln in full if ln.startswith("#EXT-X-SERVER-CONTROL"))
    assert control.startswith("#EXT-X-SERVER-CONTROL:CAN-SKIP-UNTIL=6.000,CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=")
    assert not any("CAN-SKIP-DATERANGES" in ln for ln in full)  # v2 is not offered, and Shaka reads even "NO" as yes
    # a 4-segment window is shorter than the Skip Boundary: nothing to skip, so nothing offered
    short, _, _, _ = delta_pair(pkg, monkeypatch, window=4)
    assert not any("SKIP" in ln for ln in short)


def test_a_delta_replaces_the_old_segments_and_changes_nothing_else(pkg, monkeypatch):
    full, delta, _, _ = delta_pair(pkg, monkeypatch)
    skip = [ln for ln in delta if ln.startswith("#EXT-X-SKIP")]
    assert skip == ["#EXT-X-SKIP:SKIPPED-SEGMENTS=6"]  # once, and six of the twelve
    assert not any(ln.startswith("#EXT-X-SKIP") for ln in full)

    # the counters name the first segment of the FULL playlist, so the client can splice the skipped ones back in
    for tag in ("#EXT-X-MEDIA-SEQUENCE", "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-TARGETDURATION", "#EXT-X-VERSION"):
        assert [ln for ln in full if ln.startswith(tag)] == [ln for ln in delta if ln.startswith(tag)]

    # the kept segments are the newest ones, byte for byte; the six oldest are gone (segment 20 has no URI yet)
    assert len(segment_uris(full)) == 11 and len(segment_uris(delta)) == 5
    assert segment_uris(full)[6:] == segment_uris(delta)
    # parts, the preload hint and the rendition reports are untouched, and the skip sits before the first kept segment
    for tag in ("#EXT-X-PART:", "#EXT-X-PRELOAD-HINT", "#EXT-X-RENDITION-REPORT"):
        assert [ln for ln in full if ln.startswith(tag)] == [ln for ln in delta if ln.startswith(tag)]
    assert delta.index(skip[0]) < next(i for i, ln in enumerate(delta) if ln.startswith("#EXT-X-PROGRAM-DATE-TIME"))

    # apart from date ranges, everything after the skip is exactly the tail of the full playlist: what the client
    # merges is what the full one would have said (discontinuities, program dates, durations, parts, hint, reports)
    sixth_uri = [i for i, ln in enumerate(full) if ln.endswith(".m4s") and not ln.startswith("#")][5]
    def not_ranges(ls):
        return [ln for ln in ls if not ln.startswith("#EXT-X-DATERANGE:")]

    assert not_ranges(full[sixth_uri + 1 :]) == not_ranges(delta[delta.index(skip[0]) + 1 :])
    assert len([ln for ln in delta if ln.startswith("#EXT-X-PROGRAM-DATE-TIME")]) + 6 == len(
        [ln for ln in full if ln.startswith("#EXT-X-PROGRAM-DATE-TIME")]
    )


def test_a_delta_never_drops_a_date_range(pkg, monkeypatch):
    """`YES` cannot say which date ranges were removed (that is `v2`), so every one stays, after the skip."""
    path = pkg[0]
    from test_serve_continuity import _marker, _set_markers

    _set_markers(path, [_marker("0x00000001", 0, "0x34", duration=45_000)])
    full, delta, _, _ = delta_pair(pkg, monkeypatch)
    full_ranges = [ln for ln in full if ln.startswith("#EXT-X-DATERANGE:")]
    assert len(full_ranges) >= 4, "the marker recurs every loop: some are in the skipped region"
    assert [ln for ln in delta if ln.startswith("#EXT-X-DATERANGE:")] == full_ranges
    skip_at = next(i for i, ln in enumerate(delta) if ln.startswith("#EXT-X-SKIP"))
    first_kept = next(i for i, ln in enumerate(delta) if ln.startswith("#EXT-X-PROGRAM-DATE-TIME"))
    assert any(ln.startswith("#EXT-X-DATERANGE:") for ln in delta[skip_at:first_kept])  # those from skipped segments


@pytest.mark.parametrize("value", ["v2", "NO", "yes", "", "1"])
def test_a_directive_that_is_not_offered_is_ignored(pkg, monkeypatch, value):
    full, _, client, name = delta_pair(pkg, monkeypatch)
    out = lines(client.get(f"/{name}?_HLS_skip={value}"))
    assert out == full and not any(ln.startswith("#EXT-X-SKIP") for ln in out)


def test_a_delta_can_be_blocked_on_a_part_at_the_same_time(pkg, monkeypatch):
    full, _, client, name = delta_pair(pkg, monkeypatch)
    resp = client.get(f"/{name}?_HLS_msn={DELTA_NOW + 1}&_HLS_part=0&_HLS_skip=YES")
    out = lines(resp)
    assert resp.status_code == 200
    assert any(ln.startswith("#EXT-X-SKIP") for ln in out)
    # it waited for segment 21's first part, so the window has moved on by one (the full one began at segment 9)
    assert "#EXT-X-MEDIA-SEQUENCE:10" in out and "#EXT-X-MEDIA-SEQUENCE:9" in full


def test_a_playlist_that_is_not_low_latency_has_no_delta(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [DELTA_NOW * seg + INTO_SEGMENT_1]
    client = delta_client(path, clock, monkeypatch, ll_hls=True)
    name = playlist_names(path)[0]
    out = lines(client.get(f"/{name}?low_latency=false&_HLS_skip=YES"))
    assert not any("SKIP" in ln for ln in out) and not any(ln.startswith("#EXT-X-PART") for ln in out)
