import urllib.error
import urllib.request

import pytest

from player_lab.boundaries import BoundaryTracker
from player_lab.harness_server import HarnessServer
from player_lab.profile import Profile, Thresholds, judge, load_profile
from player_lab.runner import RunConfig, plan_cases
from player_lab.targets import Target, from_manifest_urls


def doc(periods=(), discs=()):
    return {"periods": [{"id": p} for p in periods], "discontinuities": [{"segment": d} for d in discs]}


def test_tracker_counts_only_arrivals_after_baseline():
    t = BoundaryTracker()
    t.update(doc(["a"], [10]))
    t.mark_start()
    assert t.crossed("dash") == 0 and t.crossed("hls") == 0
    t.update(doc(["a", "b"], [10, 20]))
    t.update(doc(["b", "c"], [20, 30]))  # window slides: old ids vanish, nothing double counted
    assert (t.crossed("dash"), t.crossed("hls")) == (2, 2)
    assert t.polls == 3


def good_case(**kw):
    return {"startedAfterS": 1.0, "stallCount": 0, "stallSeconds": 0, "fatalErrors": 0,
            "totalFrames": 100, "droppedFrames": 0, "periodTransitions": 2, **kw}


def test_judge_pass_and_each_failure():
    th = Thresholds()
    assert judge(good_case(), 2, th) == []
    assert judge(good_case(startedAfterS=None), 2, th)
    assert judge(good_case(startedAfterS=30), 2, th)
    assert judge(good_case(stallCount=1, stallSeconds=2), 2, th)
    assert judge(good_case(fatalErrors=1), 2, th)
    assert judge(good_case(droppedFrames=50), 2, th)
    assert judge(good_case(periodTransitions=0), 3, th)          # off by 3 > tolerance 1
    assert judge(good_case(periodTransitions=1), 2, th) == []    # within tolerance
    assert judge(good_case(periodTransitions=None), 5, th) == []  # player does not report: not judged
    assert judge(good_case(periodTransitions=0), None, th) == []  # no timeline: not judged


def test_profile_overrides(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text('max_startup_s = 5\n[player.shaka]\nmax_stall_count = 2\n[player."hlsjs:hls"]\nboundary_tolerance = 3\n')
    p = load_profile(f)
    assert p.for_player("dashjs", "dash").max_startup_s == 5
    assert p.for_player("shaka", "dash").max_stall_count == 2
    assert p.for_player("hlsjs", "hls").boundary_tolerance == 3
    assert p.for_player("hlsjs", "dash").boundary_tolerance == 1


def test_profile_rejects_unknown_key(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text("max_stalls = 1\n")
    # unknown top-level keys are ignored as non-thresholds; unknown per-player keys are errors
    f.write_text("[player.shaka]\nmax_stalls = 1\n")
    with pytest.raises(ValueError):
        load_profile(f)


def test_plan_cases_respects_player_formats():
    t = Target("x", "http://h/index.m3u8", "http://h/index.mpd", None)
    cases = plan_cases(RunConfig(target=t))
    assert ("dashjs", "hls") not in cases and ("hlsjs", "dash") not in cases
    assert ("shaka", "hls") in cases and ("videojs", "dash") in cases
    with pytest.raises(ValueError):
        plan_cases(RunConfig(target=t, players=["nope"]))
    with pytest.raises(ValueError):
        plan_cases(RunConfig(target=Target("x", None, "http://h/s.mpd", None), players=["hlsjs"]))


def test_target_timeline_url_derived_from_manifest():
    t = from_manifest_urls("http://h:1/index.m3u8", "http://h:1/index.mpd", None)
    assert t.timeline_url == "http://h:1/timeline.json"


def test_harness_server_serves_page_and_blocks_traversal():
    with HarnessServer() as s:
        assert b"harness.js" in urllib.request.urlopen(f"{s.base}/index.html").read()
        for bad in ("/vendor/../../pyproject.toml", "/vendor/%2e%2e/%2e%2e/pyproject.toml", "/../pyproject.toml"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(s.base + bad)
            assert e.value.code in (400, 403, 404)


def test_judge_browser_payload_matches_headless_judging(tmp_path):
    from player_lab.judge_browser import judge_payload
    from player_lab.profile import Profile

    good = {"startedAfterS": 1.5, "stallCount": 0, "stallSeconds": 0, "fatalErrors": 0, "periodTransitions": 1}
    bad = {**good, "stallCount": 2, "stallSeconds": 3.0}
    payload = {
        "durationS": 70, "userAgent": "UA", "target": {"name": "ch", "hls": "h", "dash": "d", "timeline": "t"},
        "boundaries": {"requested": 1, "source": "timeline.json", "newPeriods": 1, "newDiscontinuities": 1,
                       "crossed": {"hls": 1, "dash": 1}},
        "cases": [{"player": "hlsjs", "format": "hls", "snapshot": good},
                  {"player": "dashjs", "format": "dash", "snapshot": bad}],
    }
    report = judge_payload(payload, Profile(), tmp_path / "run")
    assert report["mode"] == "browser" and report["pass"] is False
    assert [c["pass"] for c in report["cases"]] == [True, False]
    assert report["cases"][0]["crossed"] == 1 and "crossed" not in report["boundaries"]
    assert (tmp_path / "run" / "report.json").is_file()


def test_keyed_players_need_a_key(tmp_path, monkeypatch):
    from player_lab import keys
    from player_lab.runner import PLAYERS, RunConfig, default_players, plan_cases
    from player_lab.targets import from_manifest_urls

    monkeypatch.delenv("BITMOVIN_LICENSE_KEY", raising=False)
    monkeypatch.setenv("DR_STRANGELOOP_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("[paths]\n")
    assert list(PLAYERS) == ["hlsjs", "dashjs", "bitmovin", "shaka", "videojs"]
    assert "bitmovin" not in default_players()
    assert keys.available() == {"bitmovin": False}
    target = from_manifest_urls("http://x/index.m3u8", "http://x/index.mpd", None)
    with __import__("pytest").raises(ValueError, match="no licence key"):
        plan_cases(RunConfig(target=target, players=["bitmovin"]))

    f = tmp_path / "config.toml"
    f.write_text('[keys]\nbitmovin = "abc"\n')
    assert keys.load_keys() == {"bitmovin": "abc"}
    assert "bitmovin" in default_players()
    monkeypatch.setenv("BITMOVIN_LICENSE_KEY", "from-env")
    assert keys.load_keys()["bitmovin"] == "from-env"
    assert [c[0] for c in plan_cases(RunConfig(target=target))][:3] == ["hlsjs", "bitmovin", "shaka"]


def test_with_queries_appends_to_manifests_and_timeline():
    from player_lab.targets import Target, with_queries

    t = Target("c", "http://h/hls/master.m3u8", "http://h/dash/manifest.mpd?x=1", "http://h/timeline.json")
    out = with_queries(t, "timeline_mode=continuous", "addressing=time")
    assert out.hls_url == "http://h/hls/master.m3u8?timeline_mode=continuous"
    assert out.dash_url == "http://h/dash/manifest.mpd?x=1&addressing=time"
    assert out.timeline_url == "http://h/timeline.json?timeline_mode=continuous"
    # DASH only: the timeline follows the DASH query
    d = with_queries(Target("c", None, "http://h/m.mpd", "http://h/timeline.json"), None, "addressing=time")
    assert d.timeline_url == "http://h/timeline.json?addressing=time"
    # nothing to add: unchanged
    assert with_queries(t, None, None) == t


# ── live latency ───────────────────────────────────────────────────────────

from player_lab.profile import summarize_live  # noqa: E402
from player_lab.report import format_table  # noqa: E402


def live_case(latencies, *, target=2.0, started=1.0, own_offset=0.1, **kw):
    """A case whose observer sampled `latencies` (wall-clock) once a second from t=2."""
    trace = [[2 + i, lat, round(lat + own_offset, 3), target, 1.0] for i, lat in enumerate(latencies)]
    return good_case(startedAfterS=started, latencyTrace=trace,
                     playbackRate={"min": 1.0, "max": 1.04, "offNominalS": 12.0, "changes": 5}, **kw)


def test_summarize_skips_the_settle_period_and_reports_the_distribution():
    th = Thresholds(latency_settle_s=10)
    # t=2..10 converging from 5 s down to 2.6 s (ignored: before started+10 = 11), then 30 samples of ~2 s.
    lats = [5.0 - 0.3 * i for i in range(9)] + [2.0 + 0.01 * (i % 5) for i in range(30)]
    s = summarize_live(live_case(lats), th)
    assert s["source"] == "wallclock" and s["samples"] > 20
    assert 2.0 <= s["medianS"] <= 2.04 and s["maxS"] <= 2.04  # the early catch-up does not leak in
    assert s["targetS"] == 2.0 and s["targetErrorS"] <= 0.04
    assert s["jitterS"] < 0.05
    assert s["playerMedianS"] == pytest.approx(s["medianS"] + 0.1, abs=0.001)
    assert s["playbackRate"]["max"] == 1.04
    assert s["convergedAfterS"] is not None and s["convergedAfterS"] <= 12  # first within 0.5 s of target


def test_summarize_none_without_samples_and_falls_back_to_the_players_own_number():
    th = Thresholds(latency_settle_s=0)
    assert summarize_live(good_case(), th) is None
    assert summarize_live(good_case(startedAfterS=None, latencyTrace=[[1, 2.0, 2.0, 2.0, 1.0]]), th) is None
    only_own = good_case(latencyTrace=[[3 + i, None, 3.0, None, 1.0] for i in range(10)])
    s = summarize_live(only_own, th)
    assert s["source"] == "player" and s["medianS"] == 3.0 and s["targetS"] is None and s["targetErrorS"] is None


def test_latency_is_not_judged_by_default():
    assert judge(live_case([9.0] * 40), 2, Thresholds()) == []


def test_judge_latency_limits():
    th = Thresholds(latency_settle_s=5, max_live_latency_s=3.0, max_latency_target_error_s=0.5, max_latency_jitter_s=0.5)
    assert judge(live_case([2.1] * 30), 2, th) == []
    too_high = judge(live_case([4.0] * 30), 2, th)
    assert any("live latency 4.0s > 3.0s" in w for w in too_high)
    assert any("from the player's target" in w for w in too_high)
    wobbly = judge(live_case([2.0, 3.0] * 15), 2, th)
    assert any("jitter" in w for w in wobbly)


def test_judge_latency_needs_samples_when_a_limit_is_set():
    th = Thresholds(latency_settle_s=30, max_live_latency_s=3.0)
    too_short = judge(live_case([2.0] * 20), 2, th)  # all inside the settle period
    assert any("no live-latency samples" in w for w in too_short)
    assert any("no live-latency samples" in w for w in judge(good_case(), 2, th))  # player reports none


def test_profile_and_cli_override_latency_thresholds(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text("latency_settle_s = 10\nmax_live_latency_s = 4\n[player.shaka]\nmax_live_latency_s = 6\n")
    p = load_profile(f)
    assert p.for_player("dashjs", "dash").max_live_latency_s == 4
    assert p.for_player("shaka", "dash").max_live_latency_s == 6
    assert p.with_default(max_live_latency_s=2.5).for_player("dashjs", "dash").max_live_latency_s == 2.5
    assert p.with_default(max_live_latency_s=2.5).for_player("shaka", "dash").max_live_latency_s == 6  # per-player wins


def test_report_table_shows_latency_columns_only_when_there_are_some():
    th = Thresholds(latency_settle_s=5)
    base = {"boundaries": {"newPeriods": None, "newDiscontinuities": None, "source": "x", "timelinePolls": 0},
            "pass": True, "durationS": 1, "outDir": "o"}
    with_live = dict(live_case([2.0] * 20), player="dashjs", format="dash", version="5", failures=[], pass_=True)
    with_live["pass"] = True
    with_live["live"] = summarize_live(with_live, th)
    plain = dict(good_case(), player="hlsjs", format="hls", version="1", failures=[], crossed=None)
    plain["pass"] = True
    assert "latency" in format_table({**base, "cases": [with_live, plain]})
    assert "latency" not in format_table({**base, "cases": [plain]})
