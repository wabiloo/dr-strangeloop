"""player-lab command line."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths
from .profile import load_profile
from .judge_browser import judge_payload
from .reference import ffmpeg_check
from .report import format_table
from . import keys
from .runner import PLAYERS, RunConfig, default_players, run
from .targets import from_channel, from_manifest_urls


def cmd_setup(_args) -> int:
    npm = shutil.which("npm")
    if not npm:
        print("npm not found: install Node.js to fetch the player SDKs", file=sys.stderr)
        return 1
    home = paths.cache_dir()
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy(paths.VENDOR_PACKAGE_JSON, home / "package.json")
    print(f"installing player SDKs into {home}")
    return subprocess.call([npm, "install", "--no-audit", "--no-fund"], cwd=home)


def cmd_info(_args) -> int:
    info = {
        "players": {p: list(f) for p, f in PLAYERS.items()},
        "default_players": default_players(),
        "keys": keys.available(),
        "sdks_installed": (paths.vendor_root() / "dashjs").is_dir(),
        "npm": shutil.which("npm") is not None,
        "chrome": chrome_installed(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "harness_dir": str(paths.HARNESS_DIR),
        "vendor_root": str(paths.vendor_root()),
    }
    print(json.dumps(info))
    return 0


def cmd_keys(_args) -> int:
    """Print the licence keys as JSON (used by Igor to hand them to the user's own browser)."""
    print(json.dumps(keys.load_keys()))
    return 0


def chrome_installed() -> bool:
    if sys.platform == "darwin":
        return Path("/Applications/Google Chrome.app").exists()
    return any(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chrome"))


def cmd_run(args) -> int:
    if args.channel:
        target = from_channel(args.channel)
    else:
        target = from_manifest_urls(args.hls, args.dash, args.timeline)
    if args.no_timeline:
        target.timeline_url = None
    cfg = RunConfig(
        target=target,
        players=args.players.split(","),
        formats=args.format.split(",") if args.format else None,
        boundaries=args.boundaries,
        duration_s=args.duration,
        max_s=args.max_seconds,
        settle_s=args.settle,
        headless=not args.headed,
        profile=load_profile(Path(args.profile) if args.profile else None),
        trace=args.trace,
        out_dir=Path(args.out_dir) if args.out_dir else None,
    )
    report = run(cfg)
    if args.ffmpeg:
        report["ffmpeg"] = [ffmpeg_check(u, args.ffmpeg) for u in (target.hls_url, target.dash_url) if u]
        (Path(report["outDir"]) / "report.json").write_text(json.dumps(report, indent=1))
    print()
    print(format_table(report))
    return 0 if report["pass"] else 1


def cmd_judge(args) -> int:
    """Judge the JSON the in-browser driver collected (stdin) and print the report."""
    report = judge_payload(
        json.load(sys.stdin),
        load_profile(Path(args.profile) if args.profile else None),
        Path(args.out_dir) if args.out_dir else None,
    )
    print(json.dumps(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="player-lab", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="print what is available (players, SDKs, Chrome, ffmpeg) as JSON").set_defaults(fn=cmd_info)
    sub.add_parser("keys", help="print the configured licence keys as JSON (secret)").set_defaults(fn=cmd_keys)
    sub.add_parser("setup", help="install the player SDKs (needs npm) into the cache").set_defaults(fn=cmd_setup)

    j = sub.add_parser("judge", help="judge results collected by the in-browser driver (JSON on stdin); prints the report")
    j.add_argument("--profile", help="TOML thresholds file")
    j.add_argument("--out-dir", help="also write report.json here")
    j.set_defaults(fn=cmd_judge)

    r = sub.add_parser("run", help="play a channel in several players and report")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--channel", help="channel name (data/channels/<name>.toml) or path to a channel TOML")
    src.add_argument("--hls", help="HLS master playlist URL (use with --dash for both)")
    r.add_argument("--dash", help="DASH MPD URL")
    r.add_argument("--timeline", help="override the /timeline.json URL (default: next to the manifest)")
    r.add_argument("--no-timeline", action="store_true", help="skip /timeline.json; run for --duration")
    r.add_argument("--players", default=",".join(default_players()), help=f"comma list of {sorted(PLAYERS)}")
    r.add_argument("--format", help="hls, dash or hls,dash (default: every format the channel has)")
    r.add_argument("--boundaries", type=int, default=2, help="stop after N boundaries were crossed (default %(default)s); 0 = play for --duration")
    r.add_argument("--duration", type=float, default=120, help="seconds, when there is no timeline or --boundaries is 0 (also the cap on a continuous timeline)")
    r.add_argument("--max-seconds", type=float, default=600)
    r.add_argument("--settle", type=float, default=40, help="max seconds to wait for players to reach the boundaries")
    r.add_argument("--profile", help="TOML thresholds file")
    r.add_argument("--headed", action="store_true", help="show the browser window")
    r.add_argument("--ffmpeg", type=float, metavar="SECONDS", help="also demux each manifest with ffmpeg for SECONDS (informational: does not affect the exit code)")
    r.add_argument("--out-dir", help="write the report (and screenshots) here instead of a timestamped folder")
    r.add_argument("--trace", action="store_true", help="save a Playwright trace.zip")
    r.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"player-lab: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
