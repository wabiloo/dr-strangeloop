"""Serve phase entrypoint (SCOPE.md §4.2). Long-running, stateless per
request -- no in-memory "current position" variable that a timer advances.

    LINT / CODE-REVIEW CHECKLIST ITEM (SCOPE.md §5):
    No persisted or accumulated timing state in this module may be a Python
    `float`. Every persisted tick value is an `int`. Floats appear only as a
    final, one-shot conversion for display/serialization of a single
    response (e.g. an ISO8601 timestamp string) -- never fed back into a
    subsequent computation.

Every request independently derives the current position from `now()`
versus a fixed channel epoch via loop_math.compute_loop_position. Segment
bytes are served completely unmodified (same bytes every loop); only
manifest text differs per request.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import time
from pathlib import Path

from flask import Flask, Response, abort, send_file

from loop_math import (
    compute_loop_position,
    global_segment_number,
    program_date_time_ticks,
    segment_index_for_position,
    ticks_to_wall_clock_seconds,
)
from scte35_signaling import build_daterange_tags, markers_to_signaling

logger = logging.getLogger(__name__)


class LoopPackage:
    """Read-only view over an immutable loop package directory (SCOPE.md §4.1
    step 6). Loaded once at process start; never mutated by request handling.
    """

    def __init__(self, package_dir: Path):
        self.package_dir = package_dir
        descriptor_path = package_dir / "loop_descriptor.json"
        with descriptor_path.open("r", encoding="utf-8") as f:
            self.descriptor: dict = json.load(f)

        self.timescale: int = int(self.descriptor["timescale"])
        self.total_loop_duration_ticks: int = int(
            self.descriptor["total_loop_duration_ticks"]
        )
        self.segment_duration_seconds: float = float(
            self.descriptor["segment_duration_seconds"]
        )
        self.markers: list[dict] = self.descriptor["markers"]
        self.video_track_id: int = int(self.descriptor["video_track_id"])

        # Physical segment files on disk, sorted -- segment identity repeats
        # every loop, so URL scheme maps a physical segment file to every
        # loop_number that uses it (SCOPE.md §4.2). Only the video track's
        # segments are enumerated here (mirrors bake.py's own numbering);
        # audio segments live alongside them but aren't served by this
        # minimal implementation yet (see README known limitations).
        self.segments_dir = package_dir / "segments"
        self.segment_files: list[Path] = sorted(
            self.segments_dir.glob(f"*track{self.video_track_id}_*.m4s"),
            key=lambda p: int(p.stem.rsplit("_", 1)[-1]),
        )
        if not self.segment_files:
            raise RuntimeError(f"No segment files found in {self.segments_dir}")

        # Exact cue-derived segment boundaries (ground truth from bake.py),
        # NOT a uniform nominal-duration approximation -- real baked
        # segments have non-uniform durations aligned to marker PTS values.
        self.segment_boundary_ticks: list[int] = [
            int(t) for t in self.descriptor["segment_boundary_ticks"]
        ]
        if len(self.segment_boundary_ticks) != len(self.segment_files):
            raise RuntimeError(
                f"loop_descriptor.json declares "
                f"{len(self.segment_boundary_ticks)} segment boundary tick(s) "
                f"but {len(self.segment_files)} physical segment file(s) were "
                f"found on disk -- package is inconsistent, refusing to serve."
            )
        self.segments_per_loop = len(self.segment_files)

        segment_durations_ticks = [
            (
                self.segment_boundary_ticks[i + 1]
                if i + 1 < len(self.segment_boundary_ticks)
                else self.total_loop_duration_ticks
            )
            - self.segment_boundary_ticks[i]
            for i in range(len(self.segment_boundary_ticks))
        ]
        max_duration_seconds = max(segment_durations_ticks) / self.timescale
        self.max_segment_duration_seconds_rounded_up = int(max_duration_seconds) + 1

    def segment_path_for_index(self, index: int) -> Path:
        return self.segment_files[index % len(self.segment_files)]


class Channel:
    """Fixed channel epoch + loop package. All request handling goes through
    this class's stateless methods -- no attribute here is ever mutated
    after construction."""

    def __init__(self, package: LoopPackage, epoch_ticks: int):
        if not isinstance(epoch_ticks, int):
            raise ValueError("epoch_ticks must be int")
        self.package = package
        self.epoch_ticks = epoch_ticks

    def now_ticks(self) -> int:
        """The only place wall-clock time is sampled. Converted to an
        integer tick count exactly once, at the point of measurement."""
        return round(time.time() * self.package.timescale)

    def current_position(self):
        return compute_loop_position(
            self.now_ticks(), self.epoch_ticks, self.package.total_loop_duration_ticks
        )

    def build_hls_manifest(self, window_segments: int = 6) -> str:
        pos = self.current_position()
        pkg = self.package
        seg_index = segment_index_for_position(
            pos.position_in_loop_ticks, pkg.segment_boundary_ticks
        )
        media_sequence = global_segment_number(
            pos.loop_number, seg_index, pkg.segments_per_loop
        )

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{pkg.max_segment_duration_seconds_rounded_up}",
            f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
            '#EXT-X-MAP:URI="init.mp4"',
        ]

        for i in range(window_segments):
            global_index = media_sequence + i
            local_index = global_index % pkg.segments_per_loop
            local_loop_number = global_index // pkg.segments_per_loop
            segment_start_ticks = pkg.segment_boundary_ticks[local_index]

            if local_index + 1 < len(pkg.segment_boundary_ticks):
                seg_end_ticks_local = pkg.segment_boundary_ticks[local_index + 1]
            else:
                seg_end_ticks_local = pkg.total_loop_duration_ticks

            # Markers whose loop-relative tick falls inside this segment's
            # loop-relative window get their DATERANGE built here, fresh,
            # for *this* request -- never reused from a stale pre-rendered
            # string. START-DATE is computed against this segment's own
            # local_loop_number, so for a marker whose PTS sits exactly at
            # a segment boundary (true by construction here, since GPAC's
            # cue-driven dasher split segments exactly at marker ticks --
            # see bake.py), START-DATE is byte-for-byte identical to this
            # segment's own EXT-X-PROGRAM-DATE-TIME below (both derive from
            # the same absolute tick). A marker further inside a segment
            # (announced in advance of its own segment boundary) instead
            # gets a START-DATE strictly after that segment's PDT and before
            # the next one's, still correctly anchored to real wall-clock
            # time -- never a fixed placeholder epoch.
            matching_markers = [
                m for m in pkg.markers
                if segment_start_ticks <= m["pts_time_ticks"] < seg_end_ticks_local
            ]
            if matching_markers:
                loop_start_ticks = program_date_time_ticks(
                    local_loop_number, 0, pkg.total_loop_duration_ticks, self.epoch_ticks
                )
                loop_start_seconds = ticks_to_wall_clock_seconds(
                    loop_start_ticks, pkg.timescale
                )
                loop_start_datetime = _dt.datetime.utcfromtimestamp(loop_start_seconds)
                signaling_markers = markers_to_signaling(matching_markers)
                lines.extend(
                    build_daterange_tags(signaling_markers, pkg.timescale, loop_start_datetime)
                )

            program_date_ticks = program_date_time_ticks(
                local_loop_number,
                segment_start_ticks,
                pkg.total_loop_duration_ticks,
                self.epoch_ticks,
            )
            program_date_seconds = ticks_to_wall_clock_seconds(
                program_date_ticks, pkg.timescale
            )
            program_date_str = (
                _dt.datetime.utcfromtimestamp(program_date_seconds).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f"
                )[:-3]
                + "Z"
            )

            segment_duration_ticks = seg_end_ticks_local - segment_start_ticks
            segment_duration_seconds = ticks_to_wall_clock_seconds(
                segment_duration_ticks, pkg.timescale
            )

            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{program_date_str}")
            lines.append(f"#EXTINF:{segment_duration_seconds:.3f},")
            lines.append(f"/seg/{local_index}.m4s")

        return "\n".join(lines) + "\n"

    def build_dash_manifest(self, window_segments: int = 6) -> str:
        """Build a DASH MPD (@type=dynamic) covering the same sliding window
        of segments as build_hls_manifest, using a SegmentTimeline (segments
        are non-uniform duration, cue-driven -- see LoopPackage docstring)
        and an <EventStream> carrying the same markers as HLS's DATERANGE
        tags, authored directly from markers.json (never GPAC's own
        aggregation -- SCOPE.md §6).

        All SegmentTimeline `t` and EventStream `presentationTime` values are
        *period-relative* ticks measured from the fixed channel epoch (the
        Period starts at epoch, `start="PT0S"`, and is never restarted), so
        they grow monotonically across loops exactly like HLS's ever
        -increasing MEDIA-SEQUENCE -- this is what lets a marker be placed
        unambiguously at the correct absolute time on loop 5 just as
        correctly as on loop 0, with no special-casing.
        """
        pos = self.current_position()
        pkg = self.package
        seg_index = segment_index_for_position(
            pos.position_in_loop_ticks, pkg.segment_boundary_ticks
        )
        media_sequence = global_segment_number(
            pos.loop_number, seg_index, pkg.segments_per_loop
        )

        epoch_seconds = ticks_to_wall_clock_seconds(self.epoch_ticks, pkg.timescale)
        availability_start_time = (
            _dt.datetime.utcfromtimestamp(epoch_seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )
        now_seconds = ticks_to_wall_clock_seconds(self.now_ticks(), pkg.timescale)
        publish_time = (
            _dt.datetime.utcfromtimestamp(now_seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )

        segment_entries = []  # (period_relative_start_ticks, duration_ticks, local_index)
        for i in range(window_segments):
            global_index = media_sequence + i
            local_index = global_index % pkg.segments_per_loop
            local_loop_number = global_index // pkg.segments_per_loop
            segment_start_ticks = pkg.segment_boundary_ticks[local_index]
            if local_index + 1 < len(pkg.segment_boundary_ticks):
                seg_end_ticks_local = pkg.segment_boundary_ticks[local_index + 1]
            else:
                seg_end_ticks_local = pkg.total_loop_duration_ticks
            period_relative_start = (
                local_loop_number * pkg.total_loop_duration_ticks + segment_start_ticks
            )
            duration_ticks = seg_end_ticks_local - segment_start_ticks
            segment_entries.append((period_relative_start, duration_ticks, local_index))

        # For each windowed segment, find markers whose loop-relative tick
        # falls in [segment_start, segment_end) and place them at that
        # segment's own loop's period-relative tick.
        event_xml_parts = []
        for period_relative_start, duration_ticks, local_index in segment_entries:
            seg_start_local = pkg.segment_boundary_ticks[local_index]
            local_loop_number = (period_relative_start - seg_start_local) // pkg.total_loop_duration_ticks
            seg_end_local = seg_start_local + duration_ticks
            for marker in pkg.markers:
                if seg_start_local <= marker["pts_time_ticks"] < seg_end_local:
                    marker_period_relative = (
                        local_loop_number * pkg.total_loop_duration_ticks
                        + marker["pts_time_ticks"]
                    )
                    duration_attr = (
                        f' duration="{marker["segmentation_duration_ticks"]}"'
                        if marker.get("segmentation_duration_ticks") is not None
                        else ""
                    )
                    event_xml_parts.append(
                        f'    <Event presentationTime="{marker_period_relative}"'
                        f'{duration_attr} id="{marker["event_id"]}">\n'
                        f'      <Signal xmlns="urn:scte:scte35:2013:xml">\n'
                        f'        <Binary>{marker["splice_command_b64"]}</Binary>\n'
                        f"      </Signal>\n"
                        f"    </Event>"
                    )

        segment_timeline_lines = []
        for period_relative_start, duration_ticks, _ in segment_entries:
            segment_timeline_lines.append(
                f'      <S t="{period_relative_start}" d="{duration_ticks}" />'
            )

        first_number = media_sequence

        mpd = f'''<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     profiles="urn:mpeg:dash:profile:isoff-live:2011"
     type="dynamic"
     availabilityStartTime="{availability_start_time}"
     publishTime="{publish_time}"
     minimumUpdatePeriod="PT{pkg.max_segment_duration_seconds_rounded_up}S"
     timeShiftBufferDepth="PT{pkg.max_segment_duration_seconds_rounded_up * window_segments}S"
     minBufferTime="PT2S">
  <Period id="0" start="PT0S">
    <EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" timescale="{pkg.timescale}">
{chr(10).join(event_xml_parts)}
    </EventStream>
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">
      <SegmentTemplate media="seg/$Number$.m4s" initialization="init.mp4"
                       timescale="{pkg.timescale}" startNumber="{first_number}">
        <SegmentTimeline>
{chr(10).join(segment_timeline_lines)}
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="1" bandwidth="9300000" codecs="avc1.640028" />
    </AdaptationSet>
  </Period>
</MPD>
'''
        return mpd

    def segment_bytes_path(self, physical_index: int) -> Path:
        return self.package.segment_path_for_index(physical_index)


def create_app(package_dir: Path, epoch_ticks: int) -> Flask:
    package = LoopPackage(package_dir)
    channel = Channel(package, epoch_ticks)

    app = Flask(__name__)

    @app.get("/live.m3u8")
    def hls_manifest():
        body = channel.build_hls_manifest()
        return Response(body, mimetype="application/vnd.apple.mpegurl")

    @app.get("/manifest.mpd")
    def dash_manifest():
        body = channel.build_dash_manifest()
        return Response(body, mimetype="application/dash+xml")

    @app.get("/init.mp4")
    def init_segment():
        init_files = list(
            package.segments_dir.glob(f"*track{package.video_track_id}_init.mp4")
        )
        if not init_files:
            abort(404)
        return send_file(init_files[0])

    @app.get("/seg/<int:physical_index>.m4s")
    def segment(physical_index: int):
        try:
            path = channel.segment_bytes_path(physical_index)
        except IndexError:
            abort(404)
        return send_file(path, mimetype="video/iso.segment")

    return app


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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    epoch_dt = _dt.datetime.strptime(args.epoch_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )

    package_descriptor = json.loads(
        (args.package_dir / "loop_descriptor.json").read_text()
    )
    timescale = int(package_descriptor["timescale"])
    # One-shot conversion of the configured epoch wall-clock time to an
    # integer tick count -- this is the *only* place this conversion
    # happens; the result (an int) is then used for every subsequent
    # request's arithmetic, never recomputed via accumulation.
    epoch_ticks = round(epoch_dt.timestamp() * timescale)

    app = create_app(args.package_dir, epoch_ticks)
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
