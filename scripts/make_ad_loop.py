#!/usr/bin/env python3
"""Generate a franken-ts playlist: one ad break every N seconds of content.

Loop layout (period = delay + break):

    content[0 : delay/2]  ->  break (marked)  ->  content[delay/2 : delay]

When the loop wraps, the two content halves join, so there are exactly
`next_break_delay_s` seconds of content between the end of one break and the
start of the next, and `break_duration_s` seconds of break.

Usage:
    uv run python scripts/make_ad_loop.py my-loop \
        --content /path/content.mp4 --ad /path/ad.mp4 \
        --next-break-delay-s 60 --break-duration-s 30
    uv run franken-ts data/playlists/my-loop.yaml --verify

Or with a recipe file (same keys as the CLI flags, `ad_breaks:` block):
    uv run python scripts/make_ad_loop.py my-loop --recipe recipe.yaml

No source media? `--content` / `--ad` also take `generator:<preset>[:<colour>]`,
rendered by ffmpeg (see docs/generator-assets.md):
    uv run python scripts/make_ad_loop.py my-loop \
        --content generator:testsrc2 --ad generator:color:red \
        --next-break-delay-s 60 --break-duration-s 30
In a recipe, `content:` / `ad:` may instead be a full mapping
(`{generator: {preset: smptebars, timecode: true, label: SHOW}}`).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from dr_strangeloop_config import get_paths  # noqa: E402


def fmt(seconds: float) -> str:
    return f"{seconds:g}s"


GENERATOR_PRESETS = ("testsrc", "testsrc2", "smptebars", "smptehdbars", "rgbtestsrc", "color")
GENERATOR_PREFIX = "generator:"


def parse_source(value, duration_s: float, *, role: str) -> tuple[dict, bool]:
    """Turn a `--content`/`--ad` value into asset keys.

    Returns (keys, is_generator). `generator:<preset>[:<colour>]` or a mapping
    `{generator: {...}}` becomes a `generator:` block of `duration_s` seconds
    (role-specific defaults: content gets a burned-in timecode; an ad is a
    labelled colour fill with silent audio); anything else is a file/URL.

    Generated clips are rendered whole at their own length -- franken-ts does
    not trim them with start/duration -- so the caller asks for exactly the
    length each asset needs."""
    if isinstance(value, dict) and "generator" in value:
        gen = dict(value["generator"] or {})
    elif isinstance(value, str) and value.startswith(GENERATOR_PREFIX):
        preset, _, arg = value[len(GENERATOR_PREFIX):].partition(":")
        if preset not in GENERATOR_PRESETS:
            raise ValueError(f"unknown generator preset {preset!r} in {value!r} "
                             f"(choose from {', '.join(GENERATOR_PRESETS)})")
        if arg and preset != "color":
            raise ValueError(f"{value!r}: only the 'color' preset takes an argument")
        gen = {"preset": preset}
        if arg:
            gen["color"] = arg
    else:
        return {"file": value}, False
    if role == "content":
        gen.setdefault("timecode", True)
    else:
        gen.setdefault("audio", "silence")
        gen.setdefault("label", "AD")
        if "preset" not in gen and "video" not in gen:
            gen["preset"] = "color"
            gen.setdefault("color", "red")
    gen.setdefault("duration", fmt(duration_s))
    return {"generator": gen}, True


def build_playlist(
    name: str,
    content: str,
    ad: str,
    delay_s: float,
    break_s: float,
    *,
    content_start_s: float = 0,
    splice_type: str = "splice_insert",
    event_id: int = 1,
    segmentation: dict | None = None,
    auto_return: bool = False,
    resolution: str = "1920x1080",
    framerate: int = 25,
    gop: int = 50,
    bitrate_kbps: int = 5000,
    output_file: str | None = None,
    ad_start_s: float = 0,
) -> dict:
    if delay_s <= 0 or break_s <= 0:
        raise ValueError("next_break_delay_s and break_duration_s must be > 0")
    half = delay_s / 2
    gop_s = gop / framerate
    for label, val in (("next_break_delay_s/2", half), ("break_duration_s", break_s)):
        if abs(val / gop_s - round(val / gop_s)) > 1e-6:
            print(f"warning: {label}={val}s is not a multiple of the GOP "
                  f"({gop_s:g}s); franken-ts will have to snap/re-encode", file=sys.stderr)

    # A file is cut in two around the break with start/duration. A generated
    # clip can't be trimmed, so each half is its own clip of exactly `half`
    # seconds (its timecode restarts at 0 in the second half).
    content_srcs = []
    for n in (1, 2):
        src, is_gen = parse_source(content, half, role="content")
        if is_gen:
            if content_start_s:
                raise ValueError("content_start_s can't be used with a generated content")
            src["generator"].setdefault("label", f"content-{n}")
        content_srcs.append((src, is_gen))
    ad_src, ad_is_gen = parse_source(ad, break_s, role="ad")
    ad_asset: dict = {**ad_src, "id": "break-1"}
    if ad_is_gen:
        if ad_start_s:
            raise ValueError("ad_start_s can't be used with a generated ad")
    else:
        ad_asset["duration"] = fmt(break_s)
        if ad_start_s:
            ad_asset["start"] = fmt(ad_start_s)

    pl: dict = {
        "output": {
            "framerate": framerate,
            "bitrate_kbps": bitrate_kbps,
            "gop": gop,
            "resolution": resolution,
            "file": output_file or f"outputs/{name}.ts",
        },
        "assets": [
            {**content_srcs[0][0], "id": "content-1",
             **({} if content_srcs[0][1] else
                {"start": fmt(content_start_s), "duration": fmt(half)})},
            ad_asset,
            {**content_srcs[1][0], "id": "content-2",
             **({} if content_srcs[1][1] else
                {"start": fmt(content_start_s + half), "duration": fmt(half)})},
        ],
        "enforce_scte35_marker_semantics": True,
        "scte35_numbering_scheme": "SCTE35_2023R1",
        "osd": {
            "enabled": True,
            "progress_bar": {"mode": "loop", "height_pct": 2},
            "ad_break_label": "ad break",
        },
    }
    marker: dict = {"event_id": event_id, "splice_type": splice_type, "assets": ["break-1"]}
    if splice_type == "splice_insert":
        marker["auto_return"] = auto_return
    else:
        marker["segmentation"] = segmentation or {
            "type_id": "0x34", "upid_type": "0x09",
            "upid_hex": "53 49 47 4e 41 4c 3a 43 52",
            "web_delivery_allowed": True, "no_regional_blackout": False,
            "archive_allowed": False, "device_restrictions": 1,
        }
    pl["markers"] = [marker]
    return pl


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="playlist name (written to <playlists_dir>/<name>.yaml)")
    p.add_argument("--recipe", help="YAML file with defaults for any option below")
    p.add_argument("--content", help="content video (file or URL), or generator:<preset>")
    p.add_argument("--ad", help="break filler: ad video or slate image (file or URL), "
                                "or generator:<preset>[:<colour>] e.g. generator:color:red")
    p.add_argument("--next-break-delay-s", type=float, help="content seconds between breaks")
    p.add_argument("--break-duration-s", type=float, help="break length in seconds")
    p.add_argument("--content-start-s", type=float, default=None)
    p.add_argument("--splice-type", choices=["splice_insert", "time_signal"])
    p.add_argument("--event-id", type=int)
    p.add_argument("--auto-return", action="store_true")
    p.add_argument("--resolution")
    p.add_argument("--framerate", type=int)
    p.add_argument("--gop", type=int)
    p.add_argument("--bitrate-kbps", type=int)
    p.add_argument("--output-file", help="output .ts path (default outputs/<name>.ts)")
    p.add_argument("-o", "--out", help="playlist path (default <playlists_dir>/<name>.yaml)")
    p.add_argument("--force", action="store_true", help="overwrite an existing playlist")
    a = p.parse_args()

    r: dict = yaml.safe_load(Path(a.recipe).read_text()) if a.recipe else {}
    ab = r.get("ad_breaks", {})

    def pick(cli, *keys, default=None):
        if cli is not None:
            return cli
        for src in (ab, r):
            for k in keys:
                if k in src:
                    return src[k]
        return default

    content = pick(a.content, "content")
    ad = pick(a.ad, "ad", "break_asset")
    delay = pick(a.next_break_delay_s, "next_break_delay_s")
    brk = pick(a.break_duration_s, "break_duration_s")
    missing = [n for n, v in (("content", content), ("ad", ad),
               ("next_break_delay_s", delay), ("break_duration_s", brk)) if v is None]
    if missing:
        p.error(f"missing: {', '.join(missing)}")

    try:
        pl = build_playlist(
            a.name, content, ad, float(delay), float(brk),
            content_start_s=pick(a.content_start_s, "content_start_s", default=0),
            splice_type=pick(a.splice_type, "splice_type", default="splice_insert"),
            event_id=pick(a.event_id, "event_id", default=1),
            segmentation=r.get("segmentation"),
            auto_return=a.auto_return or bool(r.get("auto_return", False)),
            resolution=pick(a.resolution, "resolution", default="1920x1080"),
            framerate=pick(a.framerate, "framerate", default=25),
            gop=pick(a.gop, "gop", default=50),
            bitrate_kbps=pick(a.bitrate_kbps, "bitrate_kbps", default=5000),
            output_file=pick(a.output_file, "output_file"),
            ad_start_s=r.get("ad_start_s", 0),
        )
    except ValueError as exc:
        p.error(str(exc))

    out = Path(a.out) if a.out else get_paths().playlists_dir / f"{a.name}.yaml"
    if out.exists() and not a.force:
        p.error(f"{out} exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(pl, sort_keys=False))
    period = float(delay) + float(brk)
    print(f"wrote {out}  (loop {period:g}s: {float(delay)/2:g}s content + "
          f"{float(brk):g}s break + {float(delay)/2:g}s content)")
    print(f"next: uv run franken-ts {out} --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
