"""Smart range suggestions (suggest.py) and range trimming (boundaries.trim_timeline)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.availability import SegmentAvailability  # noqa: E402
from grave_robber.boundaries import trim_timeline  # noqa: E402
from grave_robber.models import AssetBoundary, RawMarker, TimingSegment  # noqa: E402
from grave_robber.suggest import VariantSegments, suggest_ranges  # noqa: E402

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _segs(n: int, media: set[int], offset: int = 0) -> list[SegmentAvailability]:
    return [
        SegmentAvailability(
            T0 + dt.timedelta(seconds=(offset + i) * 6),
            T0 + dt.timedelta(seconds=(offset + i + 1) * 6),
            i in media,
        )
        for i in range(n)
    ]


def test_suggestions_pick_expected_windows():
    a = VariantSegments("a", _segs(20, media=set(range(5, 15))))  # 60s of media, 120s listed
    b = VariantSegments("b", _segs(10, media=set(), offset=5))  # 60s listed, no media
    by_id = {s["id"]: s for s in suggest_ranges([a, b])}

    assert by_id["complete"]["manifest_url"] == "a"
    assert by_id["complete"]["segments_total"] == by_id["complete"]["segments_with_media"] == 10
    assert by_id["longest"]["duration_seconds"] == 120
    assert by_id["longest"]["segments_with_media"] == 10
    assert by_id["widest"]["survivor_count"] == 2
    assert by_id["widest"]["manifest_url"] == "a"  # best media among survivors


def test_trim_timeline_reindexes_segments_markers_boundaries():
    segments = [
        TimingSegment(i, 90_000 * 6, asset_boundary=(i == 3), start_time=T0 + dt.timedelta(seconds=6 * i))
        for i in range(6)
    ]
    markers = [RawMarker("daterange", pts_time_ticks=90_000 * 6 * 4), RawMarker("daterange", 0)]
    boundaries = [AssetBoundary(3, gap_ticks=5)]

    segs, marks, bounds = trim_timeline(
        segments, markers, boundaries, T0 + dt.timedelta(seconds=12), T0 + dt.timedelta(seconds=36)
    )

    assert [s.index for s in segs] == [0, 1, 2, 3]
    assert [m.pts_time_ticks for m in marks] == [90_000 * 6 * 2]  # marker at 0 dropped
    assert [(b.segment_index, b.gap_ticks) for b in bounds] == [(1, 5)]


def test_cmaf_segment_needs_its_init(tmp_path):
    from grave_robber.extract_hls import extract_hls
    from grave_robber.media_extract import extract_media_for_segments

    playlist = "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4,\ns1.m4s\n#EXTINF:4,\ns2.m4s\n"
    segments, _, _ = extract_hls(playlist, "http://x/a/p.m3u8")
    assert segments[0].init_uri == "http://x/a/init.mp4"

    class Body:
        def __init__(self, data):
            self.raw_size = len(data)

    class Entry:
        def __init__(self, data):
            self.content_bytes = data
            self.response = type("R", (), {"body": Body(data)})()

    class FakeTrace:
        def __init__(self, bodies):
            self.bodies = bodies

        def get_entries_for_url(self, url):
            return [Entry(self.bodies[url])] if url in self.bodies else []

    with_init = FakeTrace({"http://x/a/s1.m4s": b"MOOF", "http://x/a/init.mp4": b"INIT"})
    paths = extract_media_for_segments(with_init, segments, tmp_path / "a")
    assert paths[0].read_bytes() == b"INITMOOF" and paths[1] is None  # s2 not captured

    no_init = FakeTrace({"http://x/a/s1.m4s": b"MOOF"})
    assert extract_media_for_segments(no_init, segments, tmp_path / "b")[0] is None
