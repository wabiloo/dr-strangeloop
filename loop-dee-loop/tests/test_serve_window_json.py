"""`/timeline.json`: JSON view of the current window, and its OpenAPI schema
(openapi.yaml) kept in lockstep with the real output."""

from __future__ import annotations

import datetime as dt
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import jsonschema
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scte35_signaling import is_out_marker  # noqa: E402
from serve import Channel, _marker_covers_segment, create_app  # noqa: E402
from test_serve_asset_boundaries import _sparse_fake_package, _write_sparse_package  # noqa: E402
from timeshift import TimeshiftConfig, TimeWindow  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi.yaml"
TICKS = [0, 90_000, 180_000, 270_000]  # 1s segments, 4 per loop
LOOP = 360_000
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _marker(event_id, pts, type_id, is_out, splice_type="time_signal", **extra) -> dict:
    return {
        "event_id": event_id,
        "type": "ad_break",
        "splice_type": splice_type,
        "pts_time_ticks": pts,
        "segmentation_type_id": type_id,
        "is_out": is_out,
        "assets": ["ad1"],
        "splice_command_b64": "AA==",
        "splice_command_b64_narrowed": "AA==",
        **extra,
    }


MARKERS = [
    _marker("0x00000001", 90_000, None, True, splice_type="splice_insert"),   # out, 3 s (paired with the in)
    _marker("0x00000001", 270_000, None, False, splice_type="splice_insert"),
    _marker("0x00000002", 180_000, "0x22", True),                              # forces a signal break
]


def _channel(*, boundaries=(), markers=MARKERS, now_ticks, window_segments=6, continuous=False, type_ids=()):
    package = _sparse_fake_package(
        boundaries=list(boundaries), gap_ticks_by_index={}, segment_boundary_ticks=TICKS,
        total_loop_duration_ticks=LOOP,
    )
    package.markers = markers
    package.segment_duration_seconds = 1.0
    package.asset_boundaries = (
        [{"asset_id": "a", "start_ticks": 0}, {"asset_id": "b", "start_ticks": 180_000}] if boundaries else []
    )
    channel = Channel(
        package, epoch_ticks=0, window_segments=window_segments, period_on_segmentation=frozenset(type_ids)
    )
    channel.continuous = continuous  # the fake package has no real tfdt to validate
    channel.now_ticks = lambda: now_ticks
    return channel


def _validator() -> jsonschema.Draft202012Validator:
    spec = yaml.safe_load(SPEC_PATH.read_text())
    return jsonschema.Draft202012Validator(
        {"$ref": "#/components/schemas/WindowDocument", "components": spec["components"]}
    )


def _walk_utc(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("_utc") or k == "utc" or k == "generated_at":
                assert v is None or UTC_RE.match(v), (k, v)
            _walk_utc(v)
    elif isinstance(node, list):
        for v in node:
            _walk_utc(v)


def _assert_valid(doc: dict) -> None:
    _validator().validate(doc)
    _walk_utc(doc)


# 4 loops + 100 ms past the start of segment 1 -> loop 4, live edge = global segment 17.
NOW_LOOP4 = 4 * LOOP + 100_000


def test_live_window_periodic_with_asset_boundary():
    doc = _channel(boundaries=[2], now_ticks=NOW_LOOP4).build_window_json()
    _assert_valid(doc)

    assert doc["mode"] == "live" and doc["timeline"] == "periodic"
    assert doc["loop"] == {"number": 4, "position_s": 1.111, "duration_s": 4.0, "segments": 4, "segment_duration_s": 1.0}
    win = doc["window"]
    assert (win["segments"]["first"], win["segments"]["last"], win["ended"]) == (12, 17, False)
    assert win["start_utc"] == "1970-01-01T00:00:12.000Z"
    assert win["end_utc"] == "1970-01-01T00:00:18.000Z"
    assert win["live_edge_utc"] == "1970-01-01T00:00:17.000Z"
    assert win["duration_s"] == 6.0

    assert [(p["id"], p["segments"]["first"], p["segments"]["last"]) for p in doc["periods"]] == [
        ("loop3-0", 12, 13),
        ("loop3-2", 14, 15),
        ("loop4-0", 16, 17),
    ]
    assert [(d["segment"], d["reason"], d["sequence"]) for d in doc["discontinuities"]] == [
        (14, "asset_boundary", 7),
        (16, "loop_wrap", 8),
    ]

    # asset b of loop 4 starts at segment 18, after the window
    assets = [(a["asset_id"], a["loop"], a["segments"]["first"], a["segments"]["last"]) for a in doc["assets"]]
    assert assets == [("a", 3, 12, 13), ("b", 3, 14, 15), ("a", 4, 16, 17)]
    first_a = doc["assets"][0]
    assert first_a["start_utc"] == "1970-01-01T00:00:12.000Z" and first_a["duration_s"] == 2.0
    assert not first_a["starts_before_range"] and not first_a["ends_after_range"]


def test_window_clamps_to_zero_right_after_start():
    doc = _channel(now_ticks=100_000).build_window_json()
    assert (doc["window"]["segments"]["first"], doc["window"]["segments"]["last"]) == (0, 1)
    _assert_valid(doc)


def test_marker_instances_clipped_to_window():
    doc = _channel(now_ticks=NOW_LOOP4).build_window_json()
    _assert_valid(doc)
    got = [(m["event_id"], m["loop"], m["is_out"], m["segments"]["first"], m["segments"]["last"]) for m in doc["markers"]]
    assert got == [
        ("0x00000001", 3, True, 13, 14),
        ("0x00000002", 3, True, 14, 14),
        ("0x00000001", 3, False, 15, 15),
        ("0x00000001", 4, True, 17, 17),
    ]
    out_loop4 = doc["markers"][-1]
    assert out_loop4["start_utc"] == "1970-01-01T00:00:17.000Z"
    assert out_loop4["end_utc"] == "1970-01-01T00:00:19.000Z"  # 2 s until the matching in
    assert out_loop4["duration_s"] == 2.0
    assert out_loop4["ends_after_range"] is True and out_loop4["starts_before_range"] is False
    cue_in = doc["markers"][2]
    assert cue_in["end_utc"] is None and cue_in["duration_s"] is None
    assert cue_in["splice_command_b64"] == "AA==" and cue_in["assets"] == ["ad1"]


def test_marker_started_before_window_is_flagged():
    # window = segments 14..17: the loop-3 out (segments 13..14) started before it
    doc = _channel(now_ticks=NOW_LOOP4, window_segments=4).build_window_json()
    _assert_valid(doc)
    first = doc["markers"][0]
    assert (first["event_id"], first["loop"]) == ("0x00000001", 3)
    assert first["starts_before_range"] is True and first["segments"]["first"] == 14


def test_continuous_timeline_periods_and_signal_break():
    # now in loop 1, segment 1 -> live edge 5, window covers 0..5; break at local segment 2
    channel = _channel(now_ticks=LOOP + 100_000, continuous=True, type_ids={0x22})
    doc = channel.build_window_json()
    _assert_valid(doc)

    assert doc["timeline"] == "continuous"
    assert [(p["id"], p["segments"]["first"], p["segments"]["last"], p["end_utc"]) for p in doc["periods"]] == [
        ("continuous", 0, 1, "1970-01-01T00:00:02.000Z"),
        ("break2", 2, 5, "1970-01-01T00:00:06.000Z"),
    ]
    assert [(d["segment"], d["reason"], d["sequence"]) for d in doc["discontinuities"]] == [(2, "signal_break", 1)]


def test_continuous_without_breaks_is_one_open_period_and_no_discontinuity():
    doc = _channel(now_ticks=LOOP + 100_000, continuous=True, markers=[]).build_window_json()
    _assert_valid(doc)
    assert [(p["id"], p["end_utc"]) for p in doc["periods"]] == [("continuous", None)]
    assert doc["discontinuities"] == [] and doc["markers"] == []


def test_timeshift_range_is_reported_as_ended():
    channel = _channel(boundaries=[2], now_ticks=10 * LOOP)
    doc = channel.build_window_json(TimeWindow(first_global=2, last_global=5, ended=True, origin_loop=0))
    _assert_valid(doc)
    assert doc["mode"] == "timeshift"
    assert doc["window"]["ended"] is True and doc["window"]["live_edge_utc"] is None
    assert (doc["window"]["segments"]["first"], doc["window"]["segments"]["last"]) == (2, 5)
    assert doc["window"]["start_utc"] == "1970-01-01T00:00:02.000Z"


@pytest.mark.parametrize("boundaries", [(), (2,)])
def test_consistent_with_hls_media_playlist(boundaries):
    channel = _channel(boundaries=boundaries, now_ticks=NOW_LOOP4)
    doc = channel.build_window_json()
    body = channel.build_hls_manifest("archive")
    lines = body.splitlines()

    assert f"#EXT-X-MEDIA-SEQUENCE:{doc['window']['segments']['first']}" in lines
    assert sum(1 for line in lines if line == "#EXT-X-DISCONTINUITY") == len(doc["discontinuities"])
    pdts = [line.split(":", 1)[1] for line in lines if line.startswith("#EXT-X-PROGRAM-DATE-TIME:")]
    assert pdts[0] == doc["window"]["start_utc"]
    assert pdts[-1] == doc["window"]["live_edge_utc"]
    seq = next(int(line.split(":")[1]) for line in lines if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"))
    for d in doc["discontinuities"]:
        seq += 1
        assert d["sequence"] == seq


def test_declared_gap_offsets_shift_times():
    channel = _channel(boundaries=[2], now_ticks=NOW_LOOP4)
    channel.package.declared_offset_ticks_by_local_index = [0, 0, 45_000, 45_000]
    doc = channel.build_window_json()
    _assert_valid(doc)
    disc = next(d for d in doc["discontinuities"] if d["segment"] == 14)
    assert disc["utc"] == "1970-01-01T00:00:14.500Z"
    marker = next(m for m in doc["markers"] if m["event_id"] == "0x00000002")  # at segment 2 of loop 3
    assert marker["start_utc"] == "1970-01-01T00:00:14.500Z"


# ── routes (real on-disk package) ───────────────────────────────────────────


def _client(tmp_path, **kwargs):
    _write_sparse_package(tmp_path)
    return create_app(tmp_path, epoch_ticks=0, window_segments=2, channel_name="demo", **kwargs).test_client()


def test_timeline_route_matches_schema(tmp_path):
    client = _client(tmp_path)
    for path in ("/timeline.json",):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "public, max-age=1"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        doc = resp.get_json()
        _assert_valid(doc)
        assert doc["channel"] == "demo" and doc["mode"] == "live"
        assert [r["name"] for r in doc["renditions"]] == ["archive"]
        assert doc["renditions"][0] | {"reference": True} == doc["renditions"][0]


def test_timeline_route_timeshift_range_and_errors(tmp_path):
    client = _client(tmp_path, timeshift=TimeshiftConfig(enabled=True))

    resp = client.get("/timeline.json?start=0&end=2")
    assert resp.status_code == 200
    doc = resp.get_json()
    _assert_valid(doc)
    assert doc["mode"] == "timeshift" and doc["window"]["ended"] is True
    assert (doc["window"]["segments"]["first"], doc["window"]["segments"]["last"]) == (0, 1)
    assert "immutable" in resp.headers["Cache-Control"]

    assert client.get("/timeline.json?end=2").status_code == 400
    assert client.get("/timeline.json?start=2&end=1").status_code == 400
    assert client.get("/timeline.json?start=99999999999999").status_code == 400


def test_scope_loops_widens_content_to_whole_loops():
    ch = _channel(boundaries=[2], now_ticks=NOW_LOOP4)
    narrow = ch.build_window_json()
    assert narrow["range"] == {
        "scope": "window", "start_utc": narrow["window"]["start_utc"], "end_utc": narrow["window"]["end_utc"],
        "duration_s": 6.0, "segments": {"first": 12, "last": 17, "count": 6},
    }

    doc = ch.build_window_json(scope="loops")
    _assert_valid(doc)
    assert doc["window"] == narrow["window"]  # manifest window is unchanged
    # live edge 17 is in loop 4 -> previous, current, next = loops 3, 4, 5 = segments 12..23
    rng = doc["range"]
    assert (rng["scope"], rng["segments"]["first"], rng["segments"]["last"], rng["duration_s"]) == ("loops", 12, 23, 12.0)
    assert rng["start_utc"] == "1970-01-01T00:00:12.000Z" and rng["end_utc"] == "1970-01-01T00:00:24.000Z"
    assert [(l["number"], l["segments"]["first"], l["segments"]["last"], l["current"]) for l in doc["loops"]] == [
        (3, 12, 15, False), (4, 16, 19, True), (5, 20, 23, False),
    ]
    assert doc["loops"][1]["start_utc"] == "1970-01-01T00:00:16.000Z"
    assert [(p["id"], p["segments"]["first"], p["segments"]["last"]) for p in doc["periods"]][-1] == ("loop5-2", 22, 23)
    assert {(a["loop"], a["asset_id"]) for a in doc["assets"]} == {(n, x) for n in (3, 4, 5) for x in "ab"}
    assert not any(a["starts_before_range"] or a["ends_after_range"] for a in doc["assets"])


def test_scope_loops_at_first_loop_has_no_previous():
    doc = _channel(now_ticks=100_000).build_window_json(scope="loops")  # loop 0, live edge segment 1
    _assert_valid(doc)
    assert [(l["number"], l["current"]) for l in doc["loops"]] == [(0, True), (1, False)]
    assert doc["range"]["segments"]["first"] == 0 and doc["range"]["segments"]["last"] == 7


def test_scope_loops_ignored_for_timeshift_and_validated_on_route(tmp_path):
    ch = _channel(now_ticks=NOW_LOOP4)
    doc = ch.build_window_json(TimeWindow(first_global=12, last_global=13, ended=True, origin_loop=3), "loops")
    assert doc["range"]["scope"] == "window" and doc["range"]["segments"]["last"] == 13

    client = _client(tmp_path)
    resp = client.get("/timeline.json?scope=loops")
    assert resp.status_code == 200
    _assert_valid(resp.get_json())
    assert resp.get_json()["range"]["scope"] == "loops"
    assert client.get("/timeline.json?scope=bogus").status_code == 400


# ── OpenAPI document ────────────────────────────────────────────────────────


def test_openapi_document_is_served_and_valid(tmp_path):
    resp = _client(tmp_path).get("/openapi.yaml")
    assert resp.status_code == 200
    spec = yaml.safe_load(resp.get_data(as_text=True))
    assert spec["openapi"].startswith("3.1")
    for path in ("/timeline.json", "/health", "/index.m3u8", "/stream.mpd", "/openapi.yaml", "/docs"):
        assert path in spec["paths"]
    jsonschema.Draft202012Validator.check_schema(
        {"$ref": "#/components/schemas/WindowDocument", "components": spec["components"]}
    )


def test_docs_page_renders_the_spec(tmp_path):
    resp = _client(tmp_path).get("/docs")
    assert resp.status_code == 200 and resp.mimetype == "text/html"
    assert 'spec-url="openapi.yaml"' in resp.get_data(as_text=True)


def test_schema_rejects_undocumented_and_missing_fields():
    doc = _channel(boundaries=[2], now_ticks=NOW_LOOP4).build_window_json()
    validator = _validator()
    validator.validate(doc)

    extra = {**doc, "surprise": 1}
    assert not validator.is_valid(extra)
    missing = {k: v for k, v in doc.items() if k != "periods"}
    assert not validator.is_valid(missing)
    marker_extra = {**doc, "markers": [{**doc["markers"][0], "surprise": 1}]}
    assert not validator.is_valid(marker_extra)


# ── consistency with the manifests ──────────────────────────────────────────
# The JSON builder and the manifest builders share their timeline helpers; these
# tests pin the two views together anyway: same periods, same marker ids and times.

REAL_B64 = "/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo="  # valid SCTE-35
DIRECTION_CODE = {"out": 0, "in": 1, "instant": 2}


def _epoch_s(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _consistency_channel(increment: bool):
    markers = [{**m, "splice_command_b64": REAL_B64, "splice_command_b64_narrowed": REAL_B64} for m in MARKERS]
    channel = _channel(boundaries=[2], markers=markers, now_ticks=NOW_LOOP4)
    channel.package.increment_event_ids = increment
    return channel


def _direction(m: dict) -> str:
    return "instant" if m["is_instant"] else "out" if m["is_out"] else "in"


@pytest.mark.parametrize("increment", [False, True])
def test_hls_dateranges_match_markers(increment):
    channel = _consistency_channel(increment)
    doc = channel.build_window_json(scope="loops")
    lines = channel.build_hls_manifest("archive").splitlines()
    first_pdt = _epoch_s(next(ln.split(":", 1)[1] for ln in lines if ln.startswith("#EXT-X-PROGRAM-DATE-TIME:")))
    last_end = _epoch_s(doc["window"]["end_utc"])

    # default ID format "...-{event id, decimal}-{loop}"
    got = set()
    for ln in lines:
        if not ln.startswith("#EXT-X-DATERANGE:"):
            continue
        attrs = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', ln.split(":", 1)[1]))
        event, loop = re.search(r"-(\d+)-(\d+)$", attrs["ID"].strip('"')).groups()
        direction = "out" if "SCTE35-OUT" in attrs else "in" if "SCTE35-IN" in attrs else "instant"
        got.add((attrs["START-DATE"].strip('"'), int(event), int(loop), direction))

    expected = {
        (m["start_utc"], int(m["event_id"], 16), m["loop"], _direction(m))
        for m in doc["markers"]
        if first_pdt <= _epoch_s(m["start_utc"]) < last_end
    }
    assert got and got == expected
    if increment:  # ids really are remapped per loop, so the check above is not vacuous
        assert len({(e, lp) for _, e, lp, _ in got}) > len({e for _, e, _, _ in got}) - 1
        assert any(e > 10 for _, e, _, _ in got)


@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("increment", [False, True])
def test_dash_periods_and_events_match_json(increment, continuous):
    channel = _consistency_channel(increment)
    channel.continuous = continuous
    doc = channel.build_window_json(scope="loops")
    root = ET.fromstring(channel.build_dash_manifest().split("?>", 1)[1])
    ns = {"m": "urn:mpeg:dash:schema:mpd:2011"}
    availability = _epoch_s(root.attrib["availabilityStartTime"])
    json_periods = {p["id"]: p for p in doc["periods"]}

    seen, mpd_segments = set(), set()
    periods = root.findall("m:Period", ns)
    assert periods
    for period in periods:
        pid = period.attrib["id"]
        start_s = float(period.attrib["start"].removeprefix("PT").removesuffix("S"))
        assert pid in json_periods, f"MPD period {pid} missing from JSON"
        jp = json_periods[pid]
        assert _epoch_s(jp["start_utc"]) == availability + start_s
        template = period.find(".//m:SegmentTemplate", ns)
        assert int(template.attrib["startNumber"]) == jp["segments"]["first"]
        mpd_first = int(template.attrib["startNumber"])
        mpd_segments.update(range(mpd_first, mpd_first + len(template.findall(".//m:S", ns))))

        for event in period.findall(".//{*}Event"):
            # continuous mode folds the loop parity into the id (id = base * 2 + loop & 1)
            event_id = int(event.attrib["id"]) // (2 if continuous else 1)
            at = availability + start_s + int(event.attrib["presentationTime"]) / 90_000
            match = [
                m for m in doc["markers"]
                if int(m["event_id"], 16) == event_id // 4
                and DIRECTION_CODE[_direction(m)] == event_id % 4
                and _epoch_s(m["start_utc"]) == at
            ]
            assert len(match) == 1, (pid, event.attrib)
            seen.add((match[0]["loop"], match[0]["is_out"], match[0]["start_utc"]))

    # ... and every marker covering a segment the MPD advertises is in the MPD
    pkg = channel.package
    expected = set()
    for g in mpd_segments:
        loop, local = divmod(g, pkg.segments_per_loop)
        start, end = channel.ref_segment_bounds(local)
        for m in pkg.markers:
            if _marker_covers_segment(m, start, end):
                expected.add((loop, is_out_marker(m), channel._marker_start_iso(m, loop)))
    assert seen == expected
