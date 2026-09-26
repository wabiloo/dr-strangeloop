"""CLI entrypoint (SCOPE.md §10/§11 open item -- "your call", following
bake.py's own argparse style for consistency with the rest of the
toolchain).

Two subcommands:

- `grave-robber ingest <archive> <manifest-url> --output <dir>`: the
  common case -- one already-known manifest URL (single rendition, or a
  human-picked reference variant after reviewing `coverage`'s report).
  Runs the full pipeline (SCOPE.md §4) and writes the segment-list
  manifest + extracted media loop-dee-loop's bake.py sparse mode consumes.
- `grave-robber coverage <archive> [--format hls|dash]`: SCOPE.md §8 step
  2's "a text/table CLI report is an adequate first pass" fallback -- the
  real range-picker UI lives in `igor` (§10), decided there as a
  dedicated wizard, not a CLI flag; this subcommand is for
  inspecting/scripting outside that UI.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from trace_shrink import Format, open_trace

from .coverage import build_dash_variant_coverage, build_hls_variant_coverage
from .pipeline import ingest

logger = logging.getLogger(__name__)


def _cmd_ingest(args: argparse.Namespace) -> int:
    fmt = Format(args.format.upper()) if args.format else None
    manifest = ingest(
        args.archive,
        args.manifest_url,
        args.output,
        format=fmt,
        media_dir=args.media_dir,
    )
    segments = manifest["segments"]
    present = sum(1 for s in segments if s["media_file"] is not None)
    logger.info(
        "Wrote %s: %d segment(s) (%d with recovered media), %d marker(s)",
        Path(args.output) / "manifest.json", len(segments), present, len(manifest["markers"]),
    )
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    trace = open_trace(args.archive)
    urls = trace.get_abr_manifest_urls(format=args.format)
    if not urls:
        print("No HLS/DASH manifest URLs found in this archive.")
        return 1

    for decorated_url in urls:
        manifest_url = str(decorated_url.url)
        entries = trace.get_entries_for_url(manifest_url)
        snapshots = [(entry.content_bytes.decode("utf-8", errors="replace"), manifest_url) for entry in entries]
        builder = build_hls_variant_coverage if decorated_url.format == "HLS" else build_dash_variant_coverage
        coverage = builder(manifest_url, snapshots)

        print(f"\n{decorated_url.format}: {manifest_url}")
        if not coverage.covered_ranges:
            print("  (no wall-clock-referenced coverage found)")
            continue
        for start, end in coverage.covered_ranges:
            print(f"  {start.isoformat()}  ->  {end.isoformat()}  ({(end - start).total_seconds():.1f}s)")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive loop-dee-loop timing + SCTE-35 markers from a "
        "captured HTTP archive (HAR/Proxyman log) of a real HLS/DASH session"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Run the full pipeline for one manifest URL")
    ingest_parser.add_argument("archive", type=str, help="Path to a .har/.proxymanlogv2/.log/.barc/.zip archive")
    ingest_parser.add_argument("manifest_url", type=str, help="The manifest URL to ingest, as captured")
    ingest_parser.add_argument("--output", type=Path, required=True, help="Output directory")
    ingest_parser.add_argument("--format", choices=("hls", "dash"), default=None, help="Override format auto-detection")
    ingest_parser.add_argument("--media-dir", type=Path, default=None, help="Where to write recovered segment media (default: <output>/media)")
    ingest_parser.set_defaults(func=_cmd_ingest)

    coverage_parser = subparsers.add_parser("coverage", help="Print each variant's captured wall-clock coverage")
    coverage_parser.add_argument("archive", type=str)
    coverage_parser.add_argument("--format", choices=("HLS", "DASH"), default=None)
    coverage_parser.set_defaults(func=_cmd_coverage)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("grave-robber failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
