"""Per-request query-parameter overrides (SCOPE.md §13 and the `[query]` config):
the parameter names, the optional global prefix, value parsing, and what each
override changes in the served manifests.

Parsing and `Channel` variants are tested with fakes; the HTTP behavior uses
the same tiny real-ffmpeg package as test_timeshift.py (2 segments/loop), with
markers and a second rendition added to its descriptor.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import Channel, LoopPackage, create_app, resolve_request_channel  # noqa: E402
from test_serve_continuity import _write_continuous_package  # noqa: E402
from test_serve_window_json import MARKERS, _channel  # noqa: E402
from timeshift import (  # noqa: E402
    FIXED_PARAMS,
    TimeshiftConfig,
    TimeshiftError,
    parse_choice,
    parse_duration_seconds,
    parse_positive_int,
    parse_split_on,
)

TS = 90_000
REAL_B64 = "/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo="
NARROWED_B64 = "AA=="  # decodes to 0x00 -> easy to tell apart from REAL_B64 in SCTE35-CMD hex

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# ── parsing ─────────────────────────────────────────────────────────────────


def test_parse_choice_is_case_insensitive_and_strict():
    assert parse_choice(" Compact ", "timeline_style", ("full", "compact")) == "compact"
    with pytest.raises(TimeshiftError, match="timeline_style.*full, compact"):
        parse_choice("tiny", "timeline_style", ("full", "compact"))


@pytest.mark.parametrize("value,expected", [("1", 1), ("720", 720), (" 4000000 ", 4_000_000)])
def test_parse_positive_int_accepts(value, expected):
    assert parse_positive_int(value, "max_height") == expected


@pytest.mark.parametrize("value", ["", "0", "-5", "7.5", "abc", "1e3"])
def test_parse_positive_int_rejects(value):
    with pytest.raises(TimeshiftError, match="max_height"):
        parse_positive_int(value, "max_height")


@pytest.mark.parametrize(
    "value,seconds",
    [("30", 30), ("7.5", Decimal("7.5")), ("PT30S", 30), ("PT1H30M", 5400), ("P1D", 86400), ("pt0.5s", Decimal("0.5"))],
)
def test_parse_duration_seconds_accepts_numbers_and_iso_durations(value, seconds):
    assert parse_duration_seconds(value, "dvr_window") == seconds


@pytest.mark.parametrize("value", ["", "0", "PT0S", "-30", "-PT30S", "abc", "P1M", "P", "PT", "30s"])
def test_parse_duration_seconds_rejects(value):
    with pytest.raises(TimeshiftError, match="dvr_window"):
        parse_duration_seconds(value, "dvr_window")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0x22", {0x22}),
        ("0x22,0x30", {0x22, 0x30}),
        (" 34 , 0x30 ", {34, 0x30}),
        ("", set()),
        ("none", set()),
        ("NONE,0x22", {0x22}),
    ],
)
def test_parse_split_on_accepts(value, expected):
    assert parse_split_on(value, "split_on") == frozenset(expected)


@pytest.mark.parametrize("value", ["abc", "0x22,zz", "256", "-1", "0x100"])
def test_parse_split_on_rejects(value):
    with pytest.raises(TimeshiftError, match="split_on"):
        parse_split_on(value, "split_on")


# ── names and prefix ────────────────────────────────────────────────────────


def test_fixed_parameter_names_are_the_decided_ones():
    assert FIXED_PARAMS == (
        "full_loops", "timeline_mode", "offset", "epoch", "dvr_window", "timeline_style", "addressing",
        "cue_tags", "descriptor_grouping", "increment_event_ids", "signal_format", "id_template",
        "split_on", "max_height", "max_bitrate",
    )


def test_param_names_without_prefix():
    cfg = TimeshiftConfig(timeshift=True, overrides=True)
    assert cfg.param_names == ("start", "end", *FIXED_PARAMS)
    assert cfg.name("offset") == "offset" and cfg.start_name == "start" and cfg.end_name == "end"


def test_prefix_applies_to_every_name_including_configurable_ones():
    cfg = TimeshiftConfig(timeshift=True, overrides=True, start_param="from", end_param="to", prefix="dsl_")
    assert cfg.param_names == ("dsl_from", "dsl_to", *(f"dsl_{n}" for n in FIXED_PARAMS))
    assert len(set(cfg.param_names)) == len(cfg.param_names)
    assert cfg.start_name == "dsl_from" and cfg.name("scope") == "dsl_scope"


@pytest.mark.parametrize("prefix", ["a b", "dsl&", "x=", "é", "a/b", "?"])
def test_prefix_must_be_url_safe(prefix):
    with pytest.raises(ValueError, match="prefix"):
        TimeshiftConfig(timeshift=True, overrides=True, prefix=prefix)


@pytest.mark.parametrize("name", FIXED_PARAMS)
def test_start_and_end_names_cannot_collide_with_any_fixed_name(name):
    with pytest.raises(ValueError, match="distinct"):
        TimeshiftConfig(timeshift=True, overrides=True, start_param=name)
    with pytest.raises(ValueError, match="distinct"):
        TimeshiftConfig(timeshift=True, overrides=True, end_param=name)


def test_start_and_end_must_differ():
    with pytest.raises(ValueError, match="distinct"):
        TimeshiftConfig(timeshift=True, overrides=True, start_param="t", end_param="t")


# ── Channel variants (fake package) ─────────────────────────────────────────

NOW = 4 * 360_000 + 100_000  # loop 4 of the 4-segment/360000-tick fake loop


def _fake(**kw):
    return _channel(now_ticks=NOW, **kw)


def _resolve(channel, query: str, cfg=None, fmt="hls"):
    from urllib.parse import parse_qsl

    return resolve_request_channel(channel, dict(parse_qsl(query, keep_blank_values=True)), cfg or TimeshiftConfig(timeshift=True, overrides=True), fmt)


def test_no_parameters_returns_the_same_channel():
    ch = _fake()
    assert _resolve(ch, "") is ch
    assert _resolve(ch, "unrelated=1&start=5") is ch  # start/end are resolved separately


def test_with_epoch_moves_the_whole_timeline():
    ch = _fake()
    assert ch.current_position().loop_number == 4
    later = ch.with_epoch(360_000)  # loop 0 starts one loop later
    assert later.current_position().loop_number == 3
    assert later.epoch_ticks == 360_000 and ch.epoch_ticks == 0  # the original is untouched
    assert ch.with_epoch(0) is ch


def test_epoch_in_the_future_is_rejected():
    ch = _fake()
    with pytest.raises(TimeshiftError, match="epoch is in the future"):
        _resolve(ch, f"epoch={(NOW + 90_000) // TS + 5}")


def test_epoch_and_offset_are_checked_together(monkeypatch):
    ch = _fake()
    del ch.__dict__["now_ticks"]  # use the real clock maths, with a fixed wall clock
    monkeypatch.setattr(Channel, "now_ticks", lambda self: NOW + self.offset_ticks)
    # an epoch 3 s after the real "now" is fine once offset pretends "now" is 10 s later
    resolved = _resolve(ch, f"epoch={NOW // TS + 3}&offset=10")
    assert resolved.epoch_ticks == (NOW // TS + 3) * TS
    with pytest.raises(TimeshiftError, match="epoch is in the future"):
        _resolve(ch, f"epoch={NOW // TS + 3}")
    with pytest.raises(TimeshiftError, match="before the channel epoch"):
        _resolve(ch, "epoch=1&offset=-1000")


def test_epoch_accepts_iso_and_epoch_milliseconds():
    ch = _fake()
    assert _resolve(ch, "epoch=1970-01-01T00:00:01Z").epoch_ticks == TS
    assert _resolve(ch, "epoch=1").epoch_ticks == TS
    assert _resolve(ch, "epoch=1.5").epoch_ticks == 135_000


def test_window_segments_from_dvr_window():
    ch = _fake()  # nominal segment duration 1.0 s in the fake package
    assert _resolve(ch, "dvr_window=10").window_segments == 10
    assert _resolve(ch, "dvr_window=PT10S").window_segments == 10
    assert _resolve(ch, "dvr_window=2.5").window_segments == 3  # rounded up
    assert _resolve(ch, "dvr_window=0.1").window_segments == 1
    assert ch.window_segments == 6  # unchanged


def test_dvr_window_is_capped_at_max_span():
    ch = _fake()
    cfg = TimeshiftConfig(timeshift=True, overrides=True, max_span_seconds=60)
    assert _resolve(ch, "dvr_window=60", cfg).window_segments == 60
    with pytest.raises(TimeshiftError, match="dvr_window.*60"):
        _resolve(ch, "dvr_window=61", cfg)


def test_dash_style_overrides():
    ch = _fake()
    v = _resolve(ch, "addressing=time&timeline_style=compact", fmt="dash")
    assert (v.dash_addressing, v.dash_timeline) == ("time", "compact")
    assert (ch.dash_addressing, ch.dash_timeline) == ("number", "full")
    only_style = _resolve(ch, "timeline_style=compact")
    assert (only_style.dash_addressing, only_style.dash_timeline) == ("number", "compact")
    with pytest.raises(TimeshiftError, match="addressing"):
        _resolve(ch, "addressing=index")
    with pytest.raises(TimeshiftError, match="timeline_style"):
        _resolve(ch, "timeline_style=tiny")


def test_split_on_replaces_the_channel_setting_and_recomputes_breaks():
    ch = _fake(type_ids=(0x22,))
    assert ch.signal_breaks == frozenset({2})
    assert _resolve(ch, "split_on=none").signal_breaks == frozenset()
    assert _resolve(ch, "split_on=").signal_breaks == frozenset()
    assert _resolve(ch, "split_on=0x22").signal_breaks == frozenset({2})
    assert ch.signal_breaks == frozenset({2})  # untouched
    plain = _fake()
    assert _resolve(plain, "split_on=0x22").signal_breaks == frozenset({2})
    assert _resolve(plain, "split_on=0x23").signal_breaks == frozenset({2})  # an End implies its Start
    assert _resolve(plain, "split_on=0x30").signal_breaks == frozenset()  # no marker of that type


def test_package_overrides_leave_the_original_package_alone():
    ch = _fake()
    v = _resolve(ch, "cue_tags=alongside&increment_event_ids=true&id_template=x-{eventid}&signal_format=xml")
    assert (v.package.cue_tags, v.package.increment_event_ids) == ("alongside", True)
    assert v.package.daterange_id_format == "x-{eventid}" and v.package.dash_signal_format == "xml"
    assert v.package.cue_breaks  # derived for the new cue_tags mode
    assert ch.package.cue_tags == "none" and ch.package.increment_event_ids is False
    assert ch.package.daterange_id_format != "x-{eventid}" and ch.package.dash_signal_format == "binary"


def test_cue_tags_only_requires_bare_splice_insert_markers():
    with pytest.raises(TimeshiftError, match="cue_tags=only.*0x00000002"):
        _resolve(_fake(), "cue_tags=only")  # MARKERS has a time_signal (0x00000002)
    splice_only = [m for m in MARKERS if m["splice_type"] == "splice_insert"]
    assert _resolve(_fake(markers=splice_only), "cue_tags=only").package.cue_tags == "only"


def test_increment_event_ids_refuses_ids_with_no_headroom():
    crowded = [{**m, "event_id": "0xFFFFFFF0"} for m in MARKERS]
    with pytest.raises(TimeshiftError, match="increment_event_ids"):
        _resolve(_fake(markers=crowded), "increment_event_ids=true")
    assert _resolve(_fake(markers=crowded), "increment_event_ids=false").package.increment_event_ids is False


def test_id_template_is_validated():
    with pytest.raises(TimeshiftError, match="placeholder"):
        _resolve(_fake(), "id_template={nope}")
    with pytest.raises(TimeshiftError):
        _resolve(_fake(), "id_template={eventid")


def test_descriptor_grouping_values_depend_on_the_format():
    ch = _fake()
    assert _resolve(ch, "descriptor_grouping=grouped", fmt="hls").package.daterange_mode == "grouped"
    assert _resolve(ch, "descriptor_grouping=narrowed", fmt="hls").package.daterange_mode == "narrowed"
    assert _resolve(ch, "descriptor_grouping=narrowed", fmt="json").package.daterange_mode == "narrowed"
    dash = _resolve(ch, "descriptor_grouping=narrowed", fmt="dash")
    assert dash.package.dash_descriptor_mode == "narrowed" and dash.package.daterange_mode == "shared"
    with pytest.raises(TimeshiftError, match="shared, narrowed"):
        _resolve(ch, "descriptor_grouping=grouped", fmt="dash")
    with pytest.raises(TimeshiftError, match="shared, narrowed, grouped"):
        _resolve(ch, "descriptor_grouping=bogus", fmt="hls")


@pytest.mark.parametrize(
    "query,message",
    [
        ("timeline_mode=sometimes", "timeline_mode"),
        ("epoch=tomorrow", "epoch"),
        ("offset=abc", "offset"),
        ("dvr_window=-1", "dvr_window"),
        ("cue_tags=maybe", "cue_tags"),
        ("increment_event_ids=perhaps", "increment_event_ids"),
        ("signal_format=json", "signal_format"),
        ("split_on=zz", "split_on"),
        ("max_height=0", "max_height"),
        ("max_bitrate=fast", "max_bitrate"),
    ],
)
def test_bad_values_name_the_offending_parameter(query, message):
    with pytest.raises(TimeshiftError, match=message):
        _resolve(_fake(), query)


def test_error_messages_carry_the_prefixed_name():
    cfg = TimeshiftConfig(timeshift=True, overrides=True, prefix="dsl_")
    with pytest.raises(TimeshiftError, match="dsl_cue_tags"):
        _resolve(_fake(), "dsl_cue_tags=maybe", cfg)
    with pytest.raises(TimeshiftError, match="dsl_dvr_window"):
        _resolve(_fake(), "dsl_dvr_window=0", cfg)


def test_with_a_prefix_only_prefixed_names_are_read():
    cfg = TimeshiftConfig(timeshift=True, overrides=True, prefix="dsl_")
    ch = _fake()
    assert _resolve(ch, "cue_tags=alongside&split_on=0x22&dvr_window=3", cfg) is ch
    v = _resolve(ch, "dsl_cue_tags=alongside&dsl_dvr_window=3", cfg)
    assert v.package.cue_tags == "alongside" and v.window_segments == 3


def test_a_variant_chains_every_override_and_still_renders():
    ch = _fake()
    v = _resolve(
        ch,
        "timeline_mode=periodic&dvr_window=4&addressing=time&timeline_style=compact&split_on=0x22"
        "&cue_tags=alongside&signal_format=binary&id_template=z-{loop}",
        fmt="dash",
    )
    assert v.window_segments == 4 and v.dash_addressing == "time"
    dash = v.build_dash_manifest()
    assert "<MPD" in dash and "$Time$" in dash
    assert "#EXTM3U" in v.build_hls_manifest("archive")


# ── served over HTTP ────────────────────────────────────────────────────────


def _package(tmp_path, *, markers=True, ladder=False):
    """The tiny 2-segment package, optionally with SCTE-35 markers (one
    splice_insert out/in pair) and a second, smaller rendition."""
    seg, loop = _write_continuous_package(tmp_path)
    descriptor = json.loads((tmp_path / "loop_descriptor.json").read_text())
    if markers:
        base = {
            "event_id": "0x00000001", "type": "ad_break", "splice_type": "splice_insert",
            "assets": ["ad"], "splice_command_b64": REAL_B64, "splice_command_b64_narrowed": NARROWED_B64,
        }
        descriptor["markers"] = [
            {**base, "pts_time_ticks": 0, "is_out": True},
            {**base, "pts_time_ticks": seg, "is_out": False},
        ]
    descriptor["scte35_payloads"] = "shared+narrowed"  # what bake.py (>= 2.0) records
    first = descriptor["video_renditions"][0]
    first["video_variant"].update(height=1080, width=1920, bandwidth=8_000_000)
    if ladder:
        shutil.copytree(tmp_path / "segments" / "1080p", tmp_path / "segments" / "360p")
        second = json.loads(json.dumps(first))
        second.update(name="360p")
        second["video_variant"].update(height=360, width=640, bandwidth=900_000)
        descriptor["video_renditions"].append(second)
    (tmp_path / "loop_descriptor.json").write_text(json.dumps(descriptor))
    return seg, loop


def _app(tmp_path, *, markers=True, ladder=False, **cfg):
    cfg.setdefault("timeshift", True)
    cfg.setdefault("overrides", True)
    seg, loop = _package(tmp_path, markers=markers, ladder=ladder)
    epoch = round(time.time() * TS) - 60 * loop  # `now` is loop ~60
    app = create_app(
        tmp_path, epoch_ticks=epoch, window_segments=4, timeshift=TimeshiftConfig(**cfg),
    )
    return app.test_client(), epoch / TS, loop / TS


def _get(client, path):
    resp = client.get(path)
    return resp.status_code, resp.get_data(as_text=True)


def _tag_lines(body, tag):
    return [line for line in body.splitlines() if line.startswith(tag)]


def _segments(body):
    return [line for line in body.splitlines() if line and not line.startswith("#")]


@ffmpeg
def test_no_parameters_changes_nothing(tmp_path):
    client, *_ = _app(tmp_path)
    status, plain = _get(client, "/video.m3u8")
    assert status == 200
    assert _get(client, "/video.m3u8?unrelated=1")[1] == plain
    assert "#EXT-X-CUE-OUT" not in plain and "DATERANGE" in plain


@ffmpeg
def test_cue_tags_adds_cue_out_in_alongside_dateranges(tmp_path):
    client, *_ = _app(tmp_path)
    _, plain = _get(client, "/video.m3u8")
    _, alongside = _get(client, "/video.m3u8?cue_tags=alongside")
    assert not _tag_lines(plain, "#EXT-X-CUE")
    assert _tag_lines(alongside, "#EXT-X-CUE-OUT") and _tag_lines(alongside, "#EXT-X-CUE-IN")
    assert _tag_lines(alongside, "#EXT-X-DATERANGE")
    _, only = _get(client, "/video.m3u8?cue_tags=only")
    assert _tag_lines(only, "#EXT-X-CUE-OUT") and not _tag_lines(only, "#EXT-X-DATERANGE")
    _, back = _get(client, "/video.m3u8?cue_tags=none")
    assert back == plain


@ffmpeg
def test_cue_tags_only_is_a_400_for_time_signal_markers(tmp_path):
    seg, loop = _package(tmp_path)
    descriptor = json.loads((tmp_path / "loop_descriptor.json").read_text())
    for m in descriptor["markers"]:
        m.update(splice_type="time_signal", segmentation_type_id="0x22")
    (tmp_path / "loop_descriptor.json").write_text(json.dumps(descriptor))
    app = create_app(tmp_path, epoch_ticks=0, window_segments=4, timeshift=TimeshiftConfig(timeshift=True, overrides=True))
    client = app.test_client()
    status, body = _get(client, "/video.m3u8?cue_tags=only")
    assert status == 400 and "cue_tags=only" in body
    assert _get(client, "/video.m3u8?cue_tags=alongside")[0] == 200


@ffmpeg
def test_descriptor_grouping_narrowed_swaps_the_hls_payload(tmp_path):
    client, *_ = _app(tmp_path)
    _, shared = _get(client, "/video.m3u8")
    _, narrowed = _get(client, "/video.m3u8?descriptor_grouping=narrowed")
    assert "0xFC302F" in shared and "0xFC302F" not in narrowed
    assert "SCTE35-OUT=0x00" in narrowed
    _, back = _get(client, "/video.m3u8?descriptor_grouping=shared")
    assert back == shared


@ffmpeg
def test_descriptor_grouping_is_validated_per_format(tmp_path):
    client, *_ = _app(tmp_path)
    assert _get(client, "/video.m3u8?descriptor_grouping=grouped")[0] == 200
    status, body = _get(client, "/stream.mpd?descriptor_grouping=grouped")
    assert status == 400 and "shared, narrowed" in body
    assert _get(client, "/stream.mpd?descriptor_grouping=narrowed")[0] == 200


@ffmpeg
def test_dash_narrowed_uses_the_narrowed_payload(tmp_path):
    client, *_ = _app(tmp_path)
    _, shared = _get(client, "/stream.mpd")
    _, narrowed = _get(client, "/stream.mpd?descriptor_grouping=narrowed")
    assert REAL_B64 in shared and REAL_B64 not in narrowed
    assert f"<scte35:Binary>{NARROWED_B64}</scte35:Binary>" in narrowed


@ffmpeg
def test_signal_format_xml_switches_the_dash_event_encoding(tmp_path):
    client, *_ = _app(tmp_path)
    _, binary = _get(client, "/stream.mpd")
    _, xml = _get(client, "/stream.mpd?signal_format=xml")
    assert "urn:scte:scte35:2014:xml+bin" in binary and "<scte35:Binary>" in binary
    assert "urn:scte:scte35:2014:xml+bin" not in xml and "SpliceInfoSection" in xml
    assert _get(client, "/stream.mpd?signal_format=json")[0] == 400


@ffmpeg
def test_increment_event_ids_changes_ids_per_loop(tmp_path):
    client, *_ = _app(tmp_path)
    _, plain = _get(client, "/video.m3u8")
    _, inc = _get(client, "/video.m3u8?increment_event_ids=true")
    assert plain != inc
    ids = {re.search(r'ID="([^"]+)"', l).group(1) for l in _tag_lines(inc, "#EXT-X-DATERANGE")}
    assert len(ids) >= 1
    _, off = _get(client, "/video.m3u8?increment_event_ids=false")
    assert off == plain
    status, body = _get(client, "/video.m3u8?increment_event_ids=maybe")
    assert status == 400 and "increment_event_ids" in body


@ffmpeg
def test_id_template_renames_dateranges(tmp_path):
    client, *_ = _app(tmp_path)
    _, body = _get(client, "/video.m3u8?id_template=ad-{eventid}")
    ids = [re.search(r'ID="([^"]+)"', l).group(1) for l in _tag_lines(body, "#EXT-X-DATERANGE")]
    assert ids and all(i.startswith("ad-") for i in ids)
    status, error = _get(client, "/video.m3u8?id_template={bogus}")
    assert status == 400 and "placeholder" in error


@ffmpeg
def test_split_on_forces_a_discontinuity_for_matching_markers_only(tmp_path):
    seg, loop = _package(tmp_path)
    descriptor = json.loads((tmp_path / "loop_descriptor.json").read_text())
    descriptor["markers"] = [
        {"event_id": "0x00000002", "type": "ad_break", "splice_type": "time_signal", "assets": [],
         "pts_time_ticks": seg, "is_out": True, "segmentation_type_id": "0x22",
         "splice_command_b64": REAL_B64, "splice_command_b64_narrowed": REAL_B64},
    ]
    (tmp_path / "loop_descriptor.json").write_text(json.dumps(descriptor))
    app = create_app(
        tmp_path, epoch_ticks=round(time.time() * TS) - 60 * loop, window_segments=4,
        continuous=True, timeshift=TimeshiftConfig(timeshift=True, overrides=True),
    )
    client = app.test_client()

    def discontinuities(path):
        return _get(client, path)[1].count("#EXT-X-DISCONTINUITY\n")

    assert discontinuities("/video.m3u8") == 0
    assert discontinuities("/video.m3u8?split_on=0x22") >= 1
    assert discontinuities("/video.m3u8?split_on=0x23") >= 1  # a Start implies its End and vice versa
    assert discontinuities("/video.m3u8?split_on=0x30") == 0  # nothing of that type
    assert discontinuities("/video.m3u8?split_on=none") == 0
    assert _get(client, "/video.m3u8?split_on=0x22")[1].count("#EXT-X-DISCONTINUITY\n") >= 1
    periods = lambda body: len(re.findall(r"<Period\b", body))
    assert periods(_get(client, "/stream.mpd")[1]) == 1
    assert periods(_get(client, "/stream.mpd?split_on=0x22")[1]) >= 2


@ffmpeg
def test_dvr_window_resizes_the_sliding_window(tmp_path):
    client, *_ = _app(tmp_path)  # configured window: 4 segments
    seg_len = lambda path: len(_segments(_get(client, path)[1]))
    assert seg_len("/video.m3u8") == 4
    nominal = json.loads((tmp_path / "loop_descriptor.json").read_text())["segment_duration_seconds"]
    assert seg_len(f"/video.m3u8?dvr_window={nominal * 8}") == 8
    assert seg_len("/video.m3u8?dvr_window=PT0.001S") == 1
    status, body = _get(client, "/video.m3u8?dvr_window=99999999")
    assert status == 400 and "dvr_window" in body


@ffmpeg
def test_dash_addressing_and_timeline_style(tmp_path):
    client, *_ = _app(tmp_path)
    _, number = _get(client, "/stream.mpd")
    _, time_addr = _get(client, "/stream.mpd?addressing=time")
    assert "$Number$" in number and "$Time$" not in number
    assert "$Time$" in time_addr and "$Number$" not in time_addr
    _, full = _get(client, "/stream.mpd")
    _, compact = _get(client, "/stream.mpd?timeline_style=compact")
    assert full != compact and len(compact) <= len(full)
    for bad in ("addressing=index", "timeline_style=tiny"):
        assert _get(client, f"/stream.mpd?{bad}")[0] == 400


@ffmpeg
def test_time_addressed_segment_urls_are_served(tmp_path):
    client, *_ = _app(tmp_path)
    _, mpd = _get(client, "/stream.mpd?addressing=time")
    media = re.search(r'media="([^"]+)"', mpd).group(1)
    t = re.search(r'<S t="(\d+)"', mpd).group(1)
    span = re.search(r"seg/t/(\d+)/", media)
    url = "/" + media.replace("$RepresentationID$", "1080p").replace("$Time$", t)
    assert span or "cseg" in media
    assert client.get(url).status_code == 200


@ffmpeg
def test_epoch_override_moves_loop_numbering(tmp_path):
    client, epoch, loop = _app(tmp_path)
    base = json.loads(_get(client, "/timeline.json")[1])
    shifted_epoch = epoch + 20 * loop  # 20 loops later than the channel's own
    moved = json.loads(_get(client, f"/timeline.json?epoch={shifted_epoch}")[1])
    assert base["loop"]["number"] - moved["loop"]["number"] == 20
    assert moved["epoch_utc"] != base["epoch_utc"]
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(shifted_epoch))
    again = json.loads(_get(client, f"/timeline.json?epoch={iso}")[1])
    assert abs(again["loop"]["number"] - moved["loop"]["number"]) <= 1


@ffmpeg
def test_epoch_override_applies_to_manifests_and_validates_start(tmp_path):
    client, epoch, loop = _app(tmp_path)
    new_epoch = epoch + 50 * loop
    # start before the overridden epoch is rejected, while the same start is fine for the channel's own
    assert _get(client, f"/video.m3u8?start={epoch + 5 * loop}&end={epoch + 7 * loop}")[0] == 200
    status, body = _get(client, f"/video.m3u8?epoch={new_epoch}&start={epoch + 5 * loop}&end={epoch + 7 * loop}")
    assert status == 400 and "before the channel epoch" in body
    ok = f"/video.m3u8?epoch={new_epoch}&start={new_epoch + 2 * loop}&end={new_epoch + 4 * loop}"
    assert _get(client, ok)[0] == 200
    status, body = _get(client, f"/video.m3u8?epoch={epoch + 10_000 * loop}")
    assert status == 400 and "future" in body


@ffmpeg
def test_epoch_override_changes_dash_availability_start_time(tmp_path):
    client, epoch, loop = _app(tmp_path)
    ast = lambda body: re.search(r'availabilityStartTime="([^"]+)"', body).group(1)
    base = ast(_get(client, "/stream.mpd")[1])
    other = ast(_get(client, f"/stream.mpd?epoch={epoch + 10 * loop}")[1])
    assert base != other


@ffmpeg
def test_timeline_mode_renamed_from_timeline(tmp_path):
    client, *_ = _app(tmp_path)
    assert 'id="continuous"' in _get(client, "/stream.mpd?timeline_mode=continuous")[1]
    assert 'id="continuous"' not in _get(client, "/stream.mpd?timeline_mode=periodic")[1]
    assert 'id="continuous"' not in _get(client, "/stream.mpd?timeline=continuous")[1]  # the old name is gone
    assert _get(client, "/video.m3u8?timeline_mode=sometimes")[0] == 400


@ffmpeg
def test_full_loops_renamed_from_full_hyphen_loops(tmp_path):
    client, epoch, loop = _app(tmp_path)
    q = f"start={epoch + 5.4 * loop}&end={epoch + 7.2 * loop}"
    narrow = _segments(_get(client, f"/video.m3u8?{q}")[1])
    widened = _segments(_get(client, f"/video.m3u8?{q}&full_loops=true")[1])
    assert len(widened) > len(narrow)
    ignored = _segments(_get(client, f"/video.m3u8?{q}&full-loops=true")[1])
    assert ignored == narrow  # the old hyphenated name is not read any more
    assert _get(client, f"/video.m3u8?{q}&full_loops=2")[0] == 400


@ffmpeg
def test_ladder_filter_by_height_and_bitrate(tmp_path):
    client, *_ = _app(tmp_path, ladder=True)
    _, full = _get(client, "/index.m3u8")
    assert full.count("#EXT-X-STREAM-INF") == 2
    _, by_height = _get(client, "/index.m3u8?max_height=720")
    assert by_height.count("#EXT-X-STREAM-INF") == 1 and "360p" not in by_height.replace("video_2", "") and "RESOLUTION=640x360" in by_height
    _, by_rate = _get(client, "/index.m3u8?max_bitrate=1000000")
    assert by_rate.count("#EXT-X-STREAM-INF") == 1 and "RESOLUTION=640x360" in by_rate
    _, both = _get(client, "/index.m3u8?max_height=2000&max_bitrate=20000000")
    assert both.count("#EXT-X-STREAM-INF") == 2
    _, mpd = _get(client, "/stream.mpd?max_height=720")
    assert 'height="360"' in mpd and 'height="1080"' not in mpd  # (one Representation per Period)
    full_mpd = _get(client, "/stream.mpd")[1]
    assert 'height="1080"' in full_mpd and 'height="360"' in full_mpd
    doc = json.loads(_get(client, "/timeline.json?max_height=720")[1])
    assert [r["name"] for r in doc["renditions"]] == ["360p"]


@ffmpeg
def test_filtered_ladder_keeps_the_original_playlist_names(tmp_path):
    client, *_ = _app(tmp_path, ladder=True)
    _, full = _get(client, "/index.m3u8")
    names_full = [l for l in full.splitlines() if l.endswith(".m3u8") or ".m3u8?" in l]
    assert [n.split("?")[0] for n in names_full] == ["video_1.m3u8", "video_2.m3u8"]
    _, filtered = _get(client, "/index.m3u8?max_height=720")
    only = [l for l in filtered.splitlines() if ".m3u8" in l and not l.startswith("#")]
    assert [n.split("?")[0] for n in only] == ["video_2.m3u8"]  # still _2, not renumbered to a bare video.m3u8
    assert _get(client, "/video_2.m3u8?max_height=720")[0] == 200


@ffmpeg
def test_ladder_filter_with_no_match_is_a_400_listing_the_ladder(tmp_path):
    client, *_ = _app(tmp_path, ladder=True)
    status, body = _get(client, "/index.m3u8?max_height=100")
    assert status == 400 and "1080p" in body and "360p" in body
    assert _get(client, "/stream.mpd?max_bitrate=10")[0] == 400


@ffmpeg
def test_overrides_propagate_to_child_playlists(tmp_path):
    client, *_ = _app(tmp_path, ladder=True)
    query = "cue_tags=alongside&descriptor_grouping=narrowed&dvr_window=3&max_height=720&unrelated=1"
    _, master = _get(client, f"/index.m3u8?{query}")
    child = [l for l in master.splitlines() if ".m3u8?" in l]
    assert child, master
    for line in child:
        assert "cue_tags=alongside" in line and "descriptor_grouping=narrowed" in line
        assert "dvr_window=3" in line and "max_height=720" in line
        assert "unrelated" not in line  # only known parameters are forwarded
    status, media = _get(client, "/" + child[0])
    nominal = json.loads((tmp_path / "loop_descriptor.json").read_text())["segment_duration_seconds"]
    assert status == 200 and _tag_lines(media, "#EXT-X-CUE-OUT")
    assert len(_segments(media)) == round(3 / nominal)  # dvr_window=3 s


@ffmpeg
def test_prefix_renames_every_parameter_and_ignores_unprefixed(tmp_path):
    client, epoch, loop = _app(tmp_path, prefix="dsl_", ladder=True)
    _, plain = _get(client, "/video_1.m3u8")
    assert _get(client, "/video_1.m3u8?cue_tags=alongside")[1] == plain  # unprefixed: ignored
    assert _tag_lines(_get(client, "/video_1.m3u8?dsl_cue_tags=alongside")[1], "#EXT-X-CUE-OUT")
    assert _get(client, "/video_1.m3u8?dsl_cue_tags=maybe")[0] == 400
    # startover/catchup names are prefixed too
    status, body = _get(client, f"/video_1.m3u8?dsl_start={epoch + 5 * loop}&dsl_end={epoch + 7 * loop}")
    assert status == 200 and "ENDLIST" in body
    assert "ENDLIST" not in _get(client, f"/video_1.m3u8?start={epoch + 5 * loop}&end={epoch + 7 * loop}")[1]
    # and so is the propagation to child playlists
    _, master = _get(client, "/index.m3u8?dsl_cue_tags=alongside&cue_tags=only")
    child = [l for l in master.splitlines() if ".m3u8?" in l]
    assert child and all("dsl_cue_tags=alongside" in l and "cue_tags=only" not in l.replace("dsl_cue_tags", "") for l in child)


@ffmpeg
def test_prefix_applies_to_scope_and_error_messages(tmp_path):
    client, *_ = _app(tmp_path, prefix="dsl_")
    assert _get(client, "/timeline.json?scope=loops")[0] == 200  # unprefixed scope ignored (default window)
    doc = json.loads(_get(client, "/timeline.json?dsl_scope=loops")[1])
    assert doc["range"]["scope"] == "loops"
    status, body = _get(client, "/timeline.json?dsl_scope=nope")
    assert status == 400 and "dsl_scope" in body
    status, body = _get(client, "/video.m3u8?dsl_end=5")
    assert status == 400 and "dsl_end" in body and "dsl_start" in body


@ffmpeg
def test_health_reports_the_effective_parameter_names(tmp_path):
    client, *_ = _app(tmp_path, prefix="dsl_", start_param="from", end_param="to")
    ts = client.get("/health").get_json()["query"]
    assert ts["prefix"] == "dsl_" and ts["start_param"] == "dsl_from" and ts["end_param"] == "dsl_to"
    assert ts["timeline_param"] == "dsl_timeline_mode" and ts["full_loops_param"] == "dsl_full_loops"
    assert ts["params"] == list(TimeshiftConfig(timeshift=True, overrides=True, prefix="dsl_", start_param="from", end_param="to").param_names)


@ffmpeg
def test_every_override_is_ignored_when_the_query_parameters_are_disabled(tmp_path):
    seg, loop = _package(tmp_path)
    client = create_app(
        tmp_path, epoch_ticks=0, window_segments=4, timeshift=TimeshiftConfig(timeshift=False, overrides=False)
    ).test_client()
    _, plain = _get(client, "/video.m3u8")
    query = "&".join(f"{n}=1" for n in ("cue_tags", "dvr_window", "max_height", "epoch", "split_on", "offset"))
    assert _get(client, f"/video.m3u8?{query}") == (200, plain)


@ffmpeg
def test_overrides_do_not_leak_between_requests(tmp_path):
    client, *_ = _app(tmp_path, ladder=True)
    _, before = _get(client, "/video_1.m3u8")
    _get(client, "/video_1.m3u8?cue_tags=alongside&descriptor_grouping=narrowed&dvr_window=9&split_on=0x22")
    _get(client, "/index.m3u8?max_height=720")
    _get(client, "/stream.mpd?addressing=time&timeline_style=compact&signal_format=xml")
    assert _get(client, "/video_1.m3u8")[1] == before
    assert _get(client, "/index.m3u8")[1].count("#EXT-X-STREAM-INF") == 2
    assert "$Number$" in _get(client, "/stream.mpd")[1]


# ── documented in openapi.yaml ──────────────────────────────────────────────


def _spec():
    import yaml

    return yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi.yaml").read_text())


def test_every_parameter_is_documented_in_the_openapi_spec():
    spec = _spec()
    documented = {p["name"] for p in spec["components"]["parameters"].values()}
    assert documented == {"start", "end", "scope", *FIXED_PARAMS}


@pytest.mark.parametrize("path", ["/index.m3u8", "/stream.mpd", "/timeline.json"])
def test_each_manifest_path_lists_the_parameters_it_honours(path):
    spec = _spec()
    components = spec["components"]["parameters"]
    listed = {components[r["$ref"].rsplit("/", 1)[1]]["name"] for r in spec["paths"][path]["get"]["parameters"]}
    always = {"start", "end", "full_loops", "timeline_mode", "offset", "epoch", "dvr_window", "split_on",
              "max_height", "max_bitrate", "descriptor_grouping", "increment_event_ids"}
    assert always <= listed
    hls_only, dash_only = {"cue_tags", "id_template"}, {"timeline_style", "addressing", "signal_format"}
    assert (hls_only <= listed) == (path != "/stream.mpd")
    assert (dash_only <= listed) == (path != "/index.m3u8")
    assert ("scope" in listed) == (path == "/timeline.json")


# ── the per-channel overrides switch ────────────────────────────────────────


def test_overrides_switch_removes_only_the_override_names():
    from timeshift import OVERRIDE_PARAMS, TIMESHIFT_FIXED_PARAMS

    assert TIMESHIFT_FIXED_PARAMS == ("full_loops", "offset")
    assert set(OVERRIDE_PARAMS) | set(TIMESHIFT_FIXED_PARAMS) == set(FIXED_PARAMS)
    assert not set(OVERRIDE_PARAMS) & set(TIMESHIFT_FIXED_PARAMS)
    on = TimeshiftConfig(timeshift=True, overrides=True)
    off = TimeshiftConfig(timeshift=True, overrides=False)
    assert on.param_names == ("start", "end", *FIXED_PARAMS)
    assert off.param_names == ("start", "end", "full_loops", "offset")
    prefixed = TimeshiftConfig(timeshift=True, overrides=False, prefix="dsl_")
    assert prefixed.param_names == ("dsl_start", "dsl_end", "dsl_full_loops", "dsl_offset")


def test_overrides_default_to_on():
    assert TimeshiftConfig(timeshift=True, overrides=True).overrides is True


def test_names_stay_distinct_even_with_overrides_off():
    with pytest.raises(ValueError, match="distinct"):
        TimeshiftConfig(timeshift=True, overrides=False, start_param="cue_tags")


@pytest.mark.parametrize(
    "query",
    [
        "timeline_mode=periodic", "epoch=1", "dvr_window=3", "split_on=0x22", "timeline_style=compact",
        "addressing=time", "cue_tags=alongside", "descriptor_grouping=narrowed", "increment_event_ids=true",
        "signal_format=xml", "id_template=x", "max_height=1", "max_bitrate=1",
        "cue_tags=bogus&max_height=0",  # invalid values are ignored too, not a 400
    ],
)
def test_overrides_off_ignores_every_override(query):
    ch = _fake()
    assert _resolve(ch, query, TimeshiftConfig(timeshift=True, overrides=False)) is ch


def test_overrides_off_still_honours_the_startover_clock(monkeypatch):
    ch = _fake()
    del ch.__dict__["now_ticks"]
    monkeypatch.setattr(Channel, "now_ticks", lambda self: NOW + self.offset_ticks)
    cfg = TimeshiftConfig(timeshift=True, overrides=False)
    v = _resolve(ch, "offset=-1&cue_tags=alongside", cfg)
    assert v.offset_ticks == -TS and v.package.cue_tags == "none"
    with pytest.raises(TimeshiftError, match="offset"):
        _resolve(ch, "offset=abc", cfg)


@ffmpeg
def test_overrides_off_over_http(tmp_path):
    client, epoch, loop = _app(tmp_path, ladder=True, overrides=False)
    _, plain = _get(client, "/video_1.m3u8")
    assert _get(client, "/video_1.m3u8?cue_tags=alongside&dvr_window=9&descriptor_grouping=narrowed")[1] == plain
    assert _get(client, "/video_1.m3u8?cue_tags=bogus")[0] == 200  # ignored, not rejected
    assert _get(client, "/index.m3u8?max_height=100")[0] == 200  # would be a 400 with overrides on
    mpd = _get(client, "/stream.mpd?addressing=time&timeline_mode=continuous&timeline_style=compact")[1]
    assert "$Number$" in mpd and "$Time$" not in mpd and 'id="continuous"' not in mpd
    # startover/catchup is untouched
    status, body = _get(client, f"/video_1.m3u8?start={epoch + 5 * loop}&end={epoch + 7 * loop}")
    assert status == 200 and "ENDLIST" in body
    # and so is the propagation: known timeshift names are forwarded, ignored overrides are not
    _, master = _get(client, f"/index.m3u8?start={epoch + 5 * loop}&end={epoch + 7 * loop}&cue_tags=alongside")
    child = [l for l in master.splitlines() if ".m3u8?" in l]
    assert child and all("start=" in l and "end=" in l and "cue_tags" not in l for l in child)


@ffmpeg
def test_health_reports_the_overrides_switch(tmp_path):
    on, *_ = _app(tmp_path)
    ts = on.get("/health").get_json()["query"]
    assert ts["overrides"] is True and len(ts["params"]) == 17
    off, *_ = _app(tmp_path / "off", overrides=False) if (tmp_path / "off").mkdir() is None else (None,)
    ts = off.get("/health").get_json()["query"]
    assert ts["overrides"] is False and ts["params"] == ["start", "end", "full_loops", "offset"]


# ── the two independent switches ────────────────────────────────────────────


def test_each_switch_selects_its_own_names():
    from timeshift import OVERRIDE_PARAMS, TIMESHIFT_FIXED_PARAMS

    only_ts = TimeshiftConfig(timeshift=True, overrides=False)
    only_ov = TimeshiftConfig(timeshift=False, overrides=True)
    neither = TimeshiftConfig(timeshift=False, overrides=False)
    assert only_ts.param_names == ("start", "end", "full_loops", "offset")
    assert only_ov.param_names == OVERRIDE_PARAMS and "start" not in only_ov.param_names
    assert neither.param_names == () and not neither.active
    assert only_ts.active and only_ov.active
    assert not hasattr(TimeshiftConfig(), "enabled")  # no master switch


def test_the_switches_are_not_query_parameters():
    """They gate the parameters, so a request must never be able to set them."""
    for flag in ("timeshift", "overrides", "enabled"):
        assert flag not in FIXED_PARAMS and flag != "start" and flag != "end"


def test_overrides_without_timeshift_ignores_start_end_full_loops_and_offset():
    ch = _fake()
    cfg = TimeshiftConfig(timeshift=False, overrides=True)
    assert _resolve(ch, "offset=-5&full_loops=true&start=1&end=2", cfg) is ch
    assert _resolve(ch, "dvr_window=3", cfg).window_segments == 3  # overrides still work


@ffmpeg
def test_timeshift_off_overrides_on_over_http(tmp_path):
    client, epoch, loop = _app(tmp_path, timeshift=False, overrides=True)
    _, plain = _get(client, "/video.m3u8")
    # start/end ignored -> plain live; the override is applied
    assert _get(client, f"/video.m3u8?start={epoch + 5 * loop}&end={epoch + 7 * loop}")[1].count("ENDLIST") == 0
    assert _tag_lines(_get(client, "/video.m3u8?cue_tags=alongside")[1], "#EXT-X-CUE-OUT")
    q = client.get("/health").get_json()["query"]
    assert (q["timeshift"], q["overrides"]) == (False, True) and "start" not in q["params"]
    _, master = _get(client, f"/index.m3u8?start={epoch + 5 * loop}&cue_tags=alongside")
    child = [l for l in master.splitlines() if ".m3u8?" in l]
    assert child and all("cue_tags=alongside" in l and "start=" not in l for l in child)


@ffmpeg
def test_both_switches_off_is_inert(tmp_path):
    client, epoch, loop = _app(tmp_path, timeshift=False, overrides=False, ladder=True)
    _, plain = _get(client, "/index.m3u8")
    noisy = f"?start={epoch + 5 * loop}&cue_tags=alongside&max_height=1&offset=-9&dvr_window=1"
    assert _get(client, "/index.m3u8" + noisy)[1] == plain
    assert "?" not in plain
    q = client.get("/health").get_json()["query"]
    assert (q["timeshift"], q["overrides"], q["params"]) == (False, False, [])


# ── descriptor_grouping needs both SCTE-35 message forms ────────────────────


def _legacy_package(tmp_path, *, daterange_mode="shared", drop_narrowed=True):
    """A package baked before bake.py stored both message forms."""
    _package(tmp_path)
    descriptor = json.loads((tmp_path / "loop_descriptor.json").read_text())
    del descriptor["scte35_payloads"]
    descriptor["daterange_mode"] = daterange_mode
    if drop_narrowed:
        for m in descriptor["markers"]:
            del m["splice_command_b64_narrowed"]
    (tmp_path / "loop_descriptor.json").write_text(json.dumps(descriptor))
    return create_app(
        tmp_path, epoch_ticks=round(time.time() * TS) - 60 * 90_000, window_segments=4,
        timeshift=TimeshiftConfig(timeshift=True, overrides=True),
    ).test_client()


@ffmpeg
def test_a_new_package_is_marked_as_holding_both_payloads(tmp_path):
    _package(tmp_path)
    assert LoopPackage(tmp_path).has_both_scte35_payloads is True


@ffmpeg
def test_an_old_package_cannot_switch_its_dash_payload(tmp_path):
    client = _legacy_package(tmp_path)
    assert LoopPackage(tmp_path).has_both_scte35_payloads is False
    assert _get(client, "/stream.mpd")[0] == 200
    status, body = _get(client, "/stream.mpd?descriptor_grouping=narrowed")  # used to be a 500 (KeyError)
    assert status == 400 and "re-bake" in body and "DASH" in body


@ffmpeg
def test_an_old_package_cannot_switch_its_hls_payload_either_way(tmp_path):
    shared = _legacy_package(tmp_path / "s", daterange_mode="shared") if (tmp_path / "s").mkdir() is None else None
    status, body = _get(shared, "/video.m3u8?descriptor_grouping=narrowed")  # used to be silently ignored
    assert status == 400 and "re-bake" in body and "HLS" in body
    narrowed = _legacy_package(tmp_path / "n", daterange_mode="narrowed", drop_narrowed=False) if (tmp_path / "n").mkdir() is None else None
    status, body = _get(narrowed, "/video.m3u8?descriptor_grouping=shared")  # the stored payload is the narrowed one
    assert status == 400 and "re-bake" in body


@ffmpeg
def test_an_old_package_still_accepts_what_needs_no_other_payload(tmp_path):
    client = _legacy_package(tmp_path)
    assert _get(client, "/video.m3u8?descriptor_grouping=shared")[0] == 200  # same as baked
    assert _get(client, "/video.m3u8?descriptor_grouping=grouped")[0] == 200  # grouped uses the whole-message payload
    assert _get(client, "/stream.mpd?descriptor_grouping=shared")[0] == 200
    assert _get(client, "/video.m3u8?cue_tags=alongside")[0] == 200  # every other override is unaffected


@ffmpeg
def test_an_old_package_baked_narrowed_may_repeat_narrowed(tmp_path):
    client = _legacy_package(tmp_path, daterange_mode="narrowed", drop_narrowed=False)
    assert _get(client, "/video.m3u8?descriptor_grouping=narrowed")[0] == 200


def test_the_payload_check_is_skipped_for_packages_without_the_attribute():
    """Fakes in unit tests carry both forms; only a real LoopPackage can say otherwise."""
    ch = _fake()
    assert _resolve(ch, "descriptor_grouping=narrowed", fmt="dash").package.dash_descriptor_mode == "narrowed"
