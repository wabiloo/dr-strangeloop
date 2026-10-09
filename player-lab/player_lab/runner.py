"""Headless driver: Playwright + installed Chrome run the harness page per case."""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .boundaries import BoundaryTracker, fetch_timeline
from .harness_server import HarnessServer
from .profile import Profile, judge
from .targets import Target

# player id -> formats it can play (mirrors harness/adapters/*.js)
# (dict order is the display order: hls.js and dash.js first, then the others alphabetically)
PLAYERS: dict[str, tuple[str, ...]] = {
    "hlsjs": ("hls",),
    "dashjs": ("dash",),
    "shaka": ("hls", "dash"),
    "videojs": ("hls", "dash"),
}
DEFAULT_PLAYERS = list(PLAYERS)


@dataclass
class RunConfig:
    target: Target
    players: list[str] = field(default_factory=lambda: list(DEFAULT_PLAYERS))
    formats: list[str] | None = None
    boundaries: int = 2          # stop once this many boundaries were crossed (needs /timeline.json)
    duration_s: float = 120.0    # fixed run length without a timeline; with one, the timeout is max_s
    max_s: float = 600.0
    settle_s: float = 40.0       # max wait after the timeline shows the boundaries (players trail the live edge)
    startup_grace_s: float = 40.0
    headless: bool = True
    profile: Profile = field(default_factory=Profile)
    out_dir: Path | None = None
    trace: bool = False
    progress: bool = True


class _Net:
    """Per-page network observations (independent of what the player reports)."""

    def __init__(self) -> None:
        self.manifest_times: list[float] = []
        self.http_errors: list[dict] = []
        self.requests = 0

    def on_response(self, resp) -> None:
        url = resp.url
        self.requests += 1
        path = url.split("?")[0]
        if path.endswith((".m3u8", ".mpd")):
            self.manifest_times.append(time.monotonic())
        if resp.status >= 400 and "127.0.0.1" not in url.split("/")[2]:
            self.http_errors.append({"status": resp.status, "url": url[:200]})

    def summary(self) -> dict:
        gaps = [b - a for a, b in zip(self.manifest_times, self.manifest_times[1:])]
        return {
            "requests": self.requests,
            "manifestRequests": len(self.manifest_times),
            "manifestGapMeanS": round(sum(gaps) / len(gaps), 2) if gaps else None,
            "manifestGapMaxS": round(max(gaps), 2) if gaps else None,
            "httpErrors": len(self.http_errors),
            "httpErrorSamples": self.http_errors[:5],
        }


def plan_cases(cfg: RunConfig) -> list[tuple[str, str]]:
    unknown = [p for p in cfg.players if p not in PLAYERS]
    if unknown:
        raise ValueError(f"unknown player(s) {unknown}; known: {sorted(PLAYERS)}")
    formats = cfg.formats or cfg.target.formats()
    cases = []
    for fmt in formats:
        if not cfg.target.url_for(fmt):
            raise ValueError(f"target has no {fmt} URL")
        cases += [(p, fmt) for p in PLAYERS if p in cfg.players and fmt in PLAYERS[p]]
    if not cases:
        raise ValueError("no player/format combination to run")
    return cases


def _log(cfg: RunConfig, msg: str) -> None:
    if cfg.progress:
        print(msg, flush=True)


def run(cfg: RunConfig) -> dict:
    from playwright.sync_api import sync_playwright

    if not (paths.vendor_root() / "dashjs").is_dir():
        raise RuntimeError("player SDKs not installed: run `player-lab setup` first")
    cases = plan_cases(cfg)
    tracker = BoundaryTracker()
    use_timeline = bool(cfg.target.timeline_url)
    out_dir = cfg.out_dir or paths.outputs_dir() / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = dt.datetime.now(dt.timezone.utc)

    with HarnessServer() as server, sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",  # real Chrome: Chromium builds lack H.264
            headless=cfg.headless,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        ctx = browser.new_context(viewport={"width": 800, "height": 600})
        if cfg.trace:
            ctx.tracing.start(screenshots=True, snapshots=True)
        pages, nets = [], []
        for player, fmt in cases:
            page, net = ctx.new_page(), _Net()
            page.on("response", net.on_response)
            q = urllib.parse.urlencode({"player": player, "format": fmt, "url": cfg.target.url_for(fmt)})
            page.goto(f"{server.base}/index.html?{q}")
            pages.append(page)
            nets.append(net)
        _log(cfg, f"playing {len(cases)} case(s): " + ", ".join(f"{p}/{f}" for p, f in cases))

        def snaps() -> list[dict]:
            return [pg.evaluate("window.__lab && window.__lab.snapshot()") or {} for pg in pages]

        t0 = time.monotonic()
        last_poll, marked_at, reached_at = -99.0, None, None
        formats_run = sorted({f for _, f in cases})
        while True:
            pages[0].wait_for_timeout(1000)
            now = time.monotonic() - t0
            if use_timeline and now - last_poll >= 5:
                last_poll = now
                try:
                    tracker.update(fetch_timeline(cfg.target.timeline_url))
                except Exception:  # noqa: BLE001 - a flaky poll must not abort the run
                    tracker.errors += 1
            s = snaps()
            if marked_at is None:
                all_started = all(x.get("startedAfterS") is not None for x in s)
                if all_started or now > cfg.startup_grace_s:
                    if use_timeline and tracker.polls == 0:
                        continue
                    tracker.mark_start()
                    marked_at = now
                    _log(cfg, f"t={now:.0f}s players started; counting boundaries from here")
            elif use_timeline:
                crossed = {f: tracker.crossed(f) for f in formats_run}
                if reached_at is None and all(c >= cfg.boundaries for c in crossed.values()):
                    reached_at = now
                    _log(cfg, f"t={now:.0f}s {cfg.boundaries} boundary(ies) crossed {crossed}; settling {cfg.settle_s:.0f}s")
                if reached_at is not None:
                    since = now - reached_at
                    reporting = [x for x in s if x.get("periodTransitions") is not None]
                    caught_up = bool(reporting) and all(x["periodTransitions"] >= cfg.boundaries for x in reporting)
                    silent = len(reporting) < len(s)  # Shaka/Video.js give no transitions: wait the full settle time
                    if since >= cfg.settle_s or (caught_up and not silent and since >= 3):
                        break
            elif now >= cfg.duration_s:
                break
            if now >= cfg.max_s:
                _log(cfg, f"t={now:.0f}s timeout")
                break
            if int(now) % 15 == 0:
                _log(cfg, f"t={now:.0f}s " + "  ".join(
                    f"{p}/{f}:{x.get('playhead', 0):.0f}s st{x.get('stallCount', 0)}" for (p, f), x in zip(cases, s)))

        final = snaps()
        results = []
        for (player, fmt), snap, net, page in zip(cases, final, nets, pages):
            crossed = tracker.crossed(fmt) if use_timeline and tracker.started else None
            th = cfg.profile.for_player(player, fmt)
            case = dict(snap)
            case.update(player=player, format=fmt, crossed=crossed, network=net.summary())
            case["failures"] = judge(case, crossed, th)
            if net.http_errors:
                case["warnings"] = [f"{len(net.http_errors)} HTTP error response(s) from the channel"]
            case["pass"] = not case["failures"]
            if not case["pass"]:
                shot = out_dir / f"{player}-{fmt}.png"
                try:
                    page.screenshot(path=str(shot))
                    case["screenshot"] = shot.name
                except Exception:  # noqa: BLE001
                    pass
            results.append(case)
        if cfg.trace:
            ctx.tracing.stop(path=str(out_dir / "trace.zip"))
        browser.close()

    report = {
        "generatedAt": started_at.isoformat(),
        "durationS": round(time.monotonic() - t0, 1),
        "target": {"name": cfg.target.name, "hls": cfg.target.hls_url, "dash": cfg.target.dash_url,
                   "timeline": cfg.target.timeline_url},
        "boundaries": {
            "requested": cfg.boundaries,
            "source": "timeline.json" if use_timeline else "none (fixed duration)",
            "newPeriods": tracker.new_periods() if tracker.started else None,
            "newDiscontinuities": tracker.new_discontinuities() if tracker.started else None,
            "timelinePolls": tracker.polls,
            "timelinePollErrors": tracker.errors,
        },
        "cases": results,
        "pass": all(c["pass"] for c in results),
        "outDir": str(out_dir),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    return report
