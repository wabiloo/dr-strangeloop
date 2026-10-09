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
    t = Target("x", "http://h/index.m3u8", "http://h/stream.mpd", None)
    cases = plan_cases(RunConfig(target=t))
    assert ("dashjs", "hls") not in cases and ("hlsjs", "dash") not in cases
    assert ("shaka", "hls") in cases and ("videojs", "dash") in cases
    with pytest.raises(ValueError):
        plan_cases(RunConfig(target=t, players=["nope"]))
    with pytest.raises(ValueError):
        plan_cases(RunConfig(target=Target("x", None, "http://h/s.mpd", None), players=["hlsjs"]))


def test_target_timeline_url_derived_from_manifest():
    t = from_manifest_urls("http://h:1/index.m3u8", "http://h:1/stream.mpd", None)
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
    target = from_manifest_urls("http://x/index.m3u8", "http://x/stream.mpd", None)
    with __import__("pytest").raises(ValueError, match="no licence key"):
        plan_cases(RunConfig(target=target, players=["bitmovin"]))

    f = tmp_path / "config.toml"
    f.write_text('[keys]\nbitmovin = "abc"\n')
    assert keys.load_keys() == {"bitmovin": "abc"}
    assert "bitmovin" in default_players()
    monkeypatch.setenv("BITMOVIN_LICENSE_KEY", "from-env")
    assert keys.load_keys()["bitmovin"] == "from-env"
    assert [c[0] for c in plan_cases(RunConfig(target=target))][:3] == ["hlsjs", "bitmovin", "shaka"]
