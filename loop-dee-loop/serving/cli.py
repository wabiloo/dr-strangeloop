"""The `python serve.py` command line."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from timeshift import TimeshiftConfig

from serving.app import create_app
from serving.constants import (
    DASH_ADDRESSING_CHOICES,
    DASH_TIMELINE_CHOICES,
    LL_UTC_TIMING_CHOICES,
)
from serving.startup import (
    read_package_descriptor,
    resolve_epoch_ticks,
    resolve_window_segments,
)
from serving.timeline import parse_segmentation_type_ids

logger = logging.getLogger("serve")

def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a loop package as live HLS/DASH")
    parser.add_argument("package_dir", type=Path, help="Path to a baked loop package directory")
    parser.add_argument(
        "--epoch-utc",
        type=str,
        required=True,
        help="Channel epoch, ISO8601 UTC (e.g. 2026-01-01T00:00:00Z). loop 0 starts here.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--channel-name",
        default="",
        help="Channel name, shown in the manifests' header comments.",
    )
    parser.add_argument(
        "--dvr-window-seconds",
        type=float,
        default=30.0,
        help="Approximate size of the DVR window / sliding manifest, in "
        "seconds (default: 30). Converted to a segment count using the "
        "package's nominal --segment-duration from bake time. Ignored if "
        "--window-segments is also given.",
    )
    parser.add_argument(
        "--window-segments",
        type=int,
        default=None,
        help="Exact number of segments to advertise per manifest response "
        "(HLS sliding window / DASH SegmentTimeline). Overrides "
        "--dvr-window-seconds if given.",
    )
    parser.add_argument(
        "--continuous-timeline",
        action="store_true",
        help="SCOPE.md §12: rewrite every segment's internal PTS/DTS/PCR "
        "(TS) or tfdt (CMAF) per request so the channel is one genuinely "
        "continuous timeline with no #EXT-X-DISCONTINUITY / DASH Period "
        "restart at the loop wrap. Requires a package with no internal "
        "asset-boundary discontinuities and 64-bit (v1) tfdt CMAF "
        "fragments -- checked once at startup, hard-fails otherwise.",
    )
    parser.add_argument(
        "--period-on-segmentation",
        default="",
        metavar="IDS",
        help="Comma-separated segmentation_type_id values (e.g. "
        "0x22,0x23,0x30,0x31) whose markers force a new DASH Period / "
        "#EXT-X-DISCONTINUITY at the segment they fall in, in either "
        "timeline mode. Signal only: with --continuous-timeline the "
        "timestamps stay continuous across the new Period. Channel-level; "
        "SCOPE.md §14.",
    )
    parser.add_argument(
        "--dash-addressing",
        choices=DASH_ADDRESSING_CHOICES,
        default="number",
        help="DASH SegmentTemplate addressing: number ($Number$ + startNumber, "
        "default) or time ($Time$ = each <S t=...>). HLS is unaffected.",
    )
    parser.add_argument(
        "--dash-timeline",
        choices=DASH_TIMELINE_CHOICES,
        default="full",
        help="DASH SegmentTimeline style: full (every <S> has @t and @d, a "
        "SegmentTemplate per representation; default) or compact (implicit @t "
        "where contiguous, @r for runs of equal @d, one shared SegmentTemplate "
        "when the video renditions match). HLS is unaffected.",
    )
    parser.add_argument(
        "--ll-dash",
        action="store_true",
        help="Low-latency DASH: advertise availabilityTimeOffset/ServiceDescription in the "
        "MPD and stream the segment in progress chunk by chunk, each chunk released at its "
        "own wall-clock end. Needs a package baked with `bake.py --chunk-duration`, and a "
        "threaded/async server (held responses) -- not the default sync gunicorn worker.",
    )
    parser.add_argument(
        "--ll-hls",
        action="store_true",
        help="Low-latency HLS (Apple LL-HLS): HLS media playlists advertise parts, a preload hint, rendition reports "
        "and blocking reload; parts are the baked chunks, served at .../seg/<n>.<k>.m4s and held until due. Needs a "
        "package baked with `bake.py --chunk-duration` and --hls-format cmaf, and a threaded/async server.",
    )
    parser.add_argument(
        "--ll-optin",
        action="store_true",
        help="Low-latency DASH delivery is available (as with --ll-dash) but an MPD only advertises it "
        "when asked with ?low_latency=true (needs --query-overrides). Implied by --ll-dash, which advertises by default.",
    )
    parser.add_argument(
        "--ll-target-latency",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Latency the low-latency MPD asks players to hold (default: 4 x chunk, at least 1 s).",
    )
    parser.add_argument(
        "--ll-utc-timing",
        choices=LL_UTC_TIMING_CHOICES,
        default="direct",
        help="How the low-latency MPD lets players sync their clock: direct (default: the time "
        "written into the MPD, which is then served no-store -- accurate only while the MPD is "
        "not cached), http (a polled /time endpoint; needs --ll-utc-timing-url except on localhost) "
        "or none (the player's own clock)."
    )
    parser.add_argument(
        "--ll-utc-timing-url",
        default="",
        metavar="URL",
        help="Absolute URL of this server's /time, for --ll-utc-timing http. Without it the URL is derived "
        "from the request only when it is addressed to localhost; for any other Host no UTCTiming is "
        "written (a Host header is client-controlled, and a shared cache could keep a forged one).",
    )
    parser.add_argument(
        "--timeshift",
        action="store_true",
        help="SCOPE.md §13: honour the startover/catchup query parameters on the "
        "normal manifest URLs (start, end, full_loops, offset).",
    )
    parser.add_argument(
        "--query-overrides",
        action="store_true",
        help="SCOPE.md §13.8: honour the per-request override parameters "
        "(timeline_mode, epoch, dvr_window, split_on, cue_tags, ...); without it "
        "they are ignored and the channel's own settings always apply.",
    )
    parser.add_argument(
        "--query-prefix",
        default="",
        metavar="PREFIX",
        help="Prefix for every query parameter name (e.g. 'dsl_' -> dsl_start, "
        "dsl_offset), so a caller that uses some of these names itself cannot clash.",
    )
    parser.add_argument("--timeshift-start-param", default="start")
    parser.add_argument("--timeshift-end-param", default="end")
    parser.add_argument("--timeshift-max-span-seconds", type=int, default=21600)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    package_descriptor = read_package_descriptor(args.package_dir)
    timescale = int(package_descriptor["timescale"])
    epoch_ticks = resolve_epoch_ticks(args.epoch_utc, timescale)
    window_segments = resolve_window_segments(
        package_descriptor,
        dvr_window_seconds=args.dvr_window_seconds,
        window_segments=args.window_segments,
    )

    logger.info(
        "DVR window: %d segment(s) (~%.1fs nominal, requested %.1fs)",
        window_segments,
        window_segments * float(package_descriptor["segment_duration_seconds"]),
        args.dvr_window_seconds,
    )

    logger.warning(
        "Running Flask's built-in dev server -- single-threaded, serializes "
        "concurrent requests. Fine for local dev/testing and local-docker; "
        "the ecs-express Docker image's production path fronts this app "
        "with gunicorn instead (see wsgi.py, docker-entrypoint.sh, PERFS.md)."
    )
    app = create_app(
        args.package_dir,
        epoch_ticks,
        window_segments=window_segments,
        continuous=args.continuous_timeline,
        period_on_segmentation=parse_segmentation_type_ids(args.period_on_segmentation),
        channel_name=args.channel_name,
        dash_addressing=args.dash_addressing,
        dash_timeline=args.dash_timeline,
        ll_dash=args.ll_dash,
        ll_target_latency=args.ll_target_latency,
        ll_utc_timing_url=args.ll_utc_timing_url,
        ll_utc_timing=args.ll_utc_timing,
        ll_optin=args.ll_optin,
        ll_hls=args.ll_hls,
        timeshift=TimeshiftConfig(
            timeshift=args.timeshift,
            overrides=args.query_overrides,
            start_param=args.timeshift_start_param,
            end_param=args.timeshift_end_param,
            max_span_seconds=args.timeshift_max_span_seconds,
            prefix=args.query_prefix,
        ),
    )
    app.run(host=args.host, port=args.port, threaded=True)
    return 0
