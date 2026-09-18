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
import math
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
        # Codec/resolution/bandwidth GPAC itself computed at bake time (see
        # bake.py's read_variant_metadata) -- needed for the HLS
        # multivariant playlist's #EXT-X-STREAM-INF attributes. Optional
        # for backward compatibility with packages baked before this field
        # existed (master.m3u8 will 404 with a clear error in that case).
        self.video_variant: dict | None = self.descriptor.get("video_variant")

        # Audio track, if the source had one. All-or-nothing: a package
        # either has no audio_track_id (silent output) or has
        # audio_track_id + audio_variant + audio_segment_boundary_ticks +
        # real audio segment files on disk, all agreeing with each other.
        self.audio_track_id: int | None = self.descriptor.get("audio_track_id")
        self.audio_variant: dict | None = self.descriptor.get("audio_variant")

        # Physical segment files on disk, sorted -- segment identity repeats
        # every loop, so URL scheme maps a physical segment file to every
        # loop_number that uses it (SCOPE.md §4.2).
        self.segments_dir = package_dir / "segments"
        self.segment_files: list[Path] = sorted(
            self.segments_dir.glob(f"*track{self.video_track_id}_*.m4s"),
            key=lambda p: int(p.stem.rsplit("_", 1)[-1]),
        )
        if not self.segment_files:
            raise RuntimeError(f"No segment files found in {self.segments_dir}")

        self.audio_segment_files: list[Path] = []
        self.audio_segment_boundary_ticks: list[int] = []
        if self.audio_track_id is not None:
            self.audio_segment_files = sorted(
                self.segments_dir.glob(f"*track{self.audio_track_id}_*.m4s"),
                key=lambda p: int(p.stem.rsplit("_", 1)[-1]),
            )
            self.audio_segment_boundary_ticks = [
                int(t) for t in self.descriptor.get("audio_segment_boundary_ticks") or []
            ]
            if len(self.audio_segment_files) != len(self.segment_files):
                raise RuntimeError(
                    f"Audio track has {len(self.audio_segment_files)} segment "
                    f"file(s) on disk but video track has "
                    f"{len(self.segment_files)} -- they must match 1:1 for "
                    f"serve.py to pair them up per request. Package is "
                    f"inconsistent, refusing to serve."
                )
            if len(self.audio_segment_boundary_ticks) != len(self.audio_segment_files):
                raise RuntimeError(
                    f"loop_descriptor.json declares "
                    f"{len(self.audio_segment_boundary_ticks)} audio segment "
                    f"boundary tick(s) but {len(self.audio_segment_files)} "
                    f"physical audio segment file(s) were found on disk -- "
                    f"package is inconsistent, refusing to serve."
                )

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

    def audio_segment_path_for_index(self, index: int) -> Path:
        return self.audio_segment_files[index % len(self.audio_segment_files)]

    @property
    def has_audio(self) -> bool:
        return self.audio_track_id is not None


class Channel:
    """Fixed channel epoch + loop package. All request handling goes through
    this class's stateless methods -- no attribute here is ever mutated
    after construction."""

    def __init__(self, package: LoopPackage, epoch_ticks: int, window_segments: int = 6):
        if not isinstance(epoch_ticks, int):
            raise ValueError("epoch_ticks must be int")
        if window_segments < 1:
            raise ValueError("window_segments must be >= 1")
        self.package = package
        self.epoch_ticks = epoch_ticks
        # Controls the DVR window / manifest size: how many segments ahead
        # of the live edge are advertised in each manifest response (HLS
        # sliding window, DASH SegmentTimeline + timeShiftBufferDepth).
        # Larger = more seekable-back history for players, larger manifest
        # responses. See --dvr-window-seconds / --window-segments in
        # serve.py's CLI.
        self.window_segments = window_segments

    def now_ticks(self) -> int:
        """The only place wall-clock time is sampled. Converted to an
        integer tick count exactly once, at the point of measurement."""
        return round(time.time() * self.package.timescale)

    def current_position(self):
        return compute_loop_position(
            self.now_ticks(), self.epoch_ticks, self.package.total_loop_duration_ticks
        )

    def build_hls_master_playlist(
        self,
        media_playlist_path: str = "live.m3u8",
        audio_playlist_path: str = "audio.m3u8",
    ) -> str:
        """Build the HLS multivariant (master) playlist -- required by the
        HLS spec and by most real players (Safari/hls.js, etc. generally
        expect the entry-point URL to be a multivariant playlist, even for
        a single-rendition/bitrate stream, not a bare media playlist).

        #EXT-X-STREAM-INF attributes (BANDWIDTH/CODECS/RESOLUTION/
        FRAME-RATE) come from `video_variant`, which is the exact
        codec/resolution/bandwidth GPAC itself computed while producing
        the real segments at bake time (bake.py's read_variant_metadata) --
        not re-derived or guessed here.

        If the package has an audio track, an #EXT-X-MEDIA audio group is
        declared and referenced from #EXT-X-STREAM-INF via AUDIO="audio",
        and CODECS includes both the video and audio codec strings (per
        HLS spec: a comma-separated list of every codec actually used by
        the variant, audio included).
        """
        pkg = self.package
        if pkg.video_variant is None:
            raise RuntimeError(
                "This loop package has no 'video_variant' metadata (it was "
                "baked before this field existed) -- rebake with the "
                "current bake.py to serve a multivariant playlist."
            )

        v = pkg.video_variant
        codecs = [v["codecs"]]
        bandwidth = v["bandwidth"]

        lines = ["#EXTM3U", "#EXT-X-VERSION:7"]

        if pkg.has_audio and pkg.audio_variant is not None:
            a = pkg.audio_variant
            codecs.append(a["codecs"])
            bandwidth += a["bandwidth"]
            lines.append(
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Default",'
                f'DEFAULT=YES,AUTOSELECT=YES,URI="{audio_playlist_path}"'
            )

        stream_inf_attrs = [
            f"BANDWIDTH={bandwidth}",
            f'CODECS="{",".join(codecs)}"',
            f'RESOLUTION={v["width"]}x{v["height"]}',
            f'FRAME-RATE={v["frame_rate"]:.3f}',
        ]
        if pkg.has_audio and pkg.audio_variant is not None:
            stream_inf_attrs.append('AUDIO="audio"')

        lines.append("#EXT-X-STREAM-INF:" + ",".join(stream_inf_attrs))
        lines.append(media_playlist_path)
        return "\n".join(lines) + "\n"

    def build_hls_manifest(self, window_segments: int | None = None) -> str:
        return self._build_hls_media_playlist(
            boundary_ticks=self.package.segment_boundary_ticks,
            init_uri="init.mp4",
            seg_uri_template="/seg/{index}.m4s",
            window_segments=window_segments,
        )

    def build_hls_audio_manifest(self, window_segments: int | None = None) -> str:
        pkg = self.package
        if not pkg.has_audio:
            raise RuntimeError("This loop package has no audio track to serve.")
        return self._build_hls_media_playlist(
            boundary_ticks=pkg.audio_segment_boundary_ticks,
            init_uri="audio/init.mp4",
            seg_uri_template="/audio/seg/{index}.m4s",
            window_segments=window_segments,
        )

    def _build_hls_media_playlist(
        self,
        *,
        boundary_ticks: list[int],
        init_uri: str,
        seg_uri_template: str,
        window_segments: int | None = None,
    ) -> str:
        """Shared builder for both the video and audio HLS media playlists.

        Segment *indexing*/loop-number sequencing (media_sequence,
        local_index, local_loop_number) is always driven by the video
        track's `segments_per_loop` -- by construction (bake.py) every
        track has exactly the same segment count per loop, with segment i
        on every track corresponding to the same conceptual time window,
        even though each track's own boundary tick VALUES were snapped
        independently (see bake.py's compute_segment_boundary_ticks). Only
        `boundary_ticks` (used for this playlist's own PROGRAM-DATE-TIME/
        EXTINF durations) and the URIs differ between video and audio.

        Marker (DATERANGE) placement is always decided using the VIDEO
        track's segment_boundary_ticks (the ad-decision timeline), applied
        to the shared segment index -- so the same marker appears at the
        same segment index in both the video and audio playlists.
        """
        window_segments = window_segments or self.window_segments
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
            f'#EXT-X-MAP:URI="{init_uri}"',
        ]

        for i in range(window_segments):
            global_index = media_sequence + i
            local_index = global_index % pkg.segments_per_loop
            local_loop_number = global_index // pkg.segments_per_loop

            # This playlist's own segment start/end (for PDT/EXTINF).
            segment_start_ticks = boundary_ticks[local_index]
            if local_index + 1 < len(boundary_ticks):
                seg_end_ticks_local = boundary_ticks[local_index + 1]
            else:
                seg_end_ticks_local = pkg.total_loop_duration_ticks

            # Marker placement always decided from the VIDEO timeline (the
            # ad-decision authority), applied to this same segment index,
            # so both playlists advertise the same marker at the same index
            # even though their own boundary tick VALUES differ slightly.
            video_seg_start_ticks = pkg.segment_boundary_ticks[local_index]
            if local_index + 1 < len(pkg.segment_boundary_ticks):
                video_seg_end_ticks = pkg.segment_boundary_ticks[local_index + 1]
            else:
                video_seg_end_ticks = pkg.total_loop_duration_ticks

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
                if video_seg_start_ticks <= m["pts_time_ticks"] < video_seg_end_ticks
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
            lines.append(seg_uri_template.format(index=local_index))

        return "\n".join(lines) + "\n"

    def build_dash_manifest(self, window_segments: int | None = None) -> str:
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
        window_segments = window_segments or self.window_segments
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

        # Same shared (local_index, local_loop_number) sequence as the video
        # track, but using the audio track's own (independently-snapped)
        # boundary tick VALUES for its own SegmentTimeline -- see
        # _build_hls_media_playlist's docstring for why this is correct.
        audio_segment_entries = []
        if pkg.has_audio:
            for i in range(window_segments):
                global_index = media_sequence + i
                local_index = global_index % pkg.segments_per_loop
                local_loop_number = global_index // pkg.segments_per_loop
                segment_start_ticks = pkg.audio_segment_boundary_ticks[local_index]
                if local_index + 1 < len(pkg.audio_segment_boundary_ticks):
                    seg_end_ticks_local = pkg.audio_segment_boundary_ticks[local_index + 1]
                else:
                    seg_end_ticks_local = pkg.total_loop_duration_ticks
                period_relative_start = (
                    local_loop_number * pkg.total_loop_duration_ticks + segment_start_ticks
                )
                duration_ticks = seg_end_ticks_local - segment_start_ticks
                audio_segment_entries.append((period_relative_start, duration_ticks, local_index))

        # For each windowed segment, find markers whose loop-relative tick
        # falls in [segment_start, segment_end) and place them at that
        # segment's own loop's period-relative tick. Always decided from
        # the VIDEO timeline (the ad-decision authority) -- shared as-is
        # between the video and audio AdaptationSets below.
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

        audio_segment_timeline_lines = []
        for period_relative_start, duration_ticks, _ in audio_segment_entries:
            audio_segment_timeline_lines.append(
                f'      <S t="{period_relative_start}" d="{duration_ticks}" />'
            )

        first_number = media_sequence
        v = pkg.video_variant or {}
        video_bandwidth = v.get("bandwidth", 0)
        video_codecs = v.get("codecs", "")

        audio_adaptation_set = ""
        if pkg.has_audio and pkg.audio_variant is not None:
            a = pkg.audio_variant
            audio_adaptation_set = f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <SegmentTemplate media="audio/seg/$Number$.m4s" initialization="audio/init.mp4"
                       timescale="{pkg.timescale}" startNumber="{first_number}">
        <SegmentTimeline>
{chr(10).join(audio_segment_timeline_lines)}
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="2" bandwidth="{a["bandwidth"]}" codecs="{a["codecs"]}" />
    </AdaptationSet>'''

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
      <Representation id="1" bandwidth="{video_bandwidth}" codecs="{video_codecs}" />
    </AdaptationSet>{audio_adaptation_set}
  </Period>
</MPD>
'''
        return mpd

    def segment_bytes_path(self, physical_index: int) -> Path:
        return self.package.segment_path_for_index(physical_index)

    def audio_segment_bytes_path(self, physical_index: int) -> Path:
        return self.package.audio_segment_path_for_index(physical_index)


def create_app(package_dir: Path, epoch_ticks: int, window_segments: int = 6) -> Flask:
    package = LoopPackage(package_dir)
    channel = Channel(package, epoch_ticks, window_segments=window_segments)

    app = Flask(__name__)

    @app.get("/master.m3u8")
    def hls_master_playlist():
        body = channel.build_hls_master_playlist()
        return Response(body, mimetype="application/vnd.apple.mpegurl")

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

    if package.has_audio:
        @app.get("/audio.m3u8")
        def hls_audio_manifest():
            body = channel.build_hls_audio_manifest()
            return Response(body, mimetype="application/vnd.apple.mpegurl")

        @app.get("/audio/init.mp4")
        def audio_init_segment():
            init_files = list(
                package.segments_dir.glob(f"*track{package.audio_track_id}_init.mp4")
            )
            if not init_files:
                abort(404)
            return send_file(init_files[0])

        @app.get("/audio/seg/<int:physical_index>.m4s")
        def audio_segment(physical_index: int):
            try:
                path = channel.audio_segment_bytes_path(physical_index)
            except IndexError:
                abort(404)
            return send_file(path, mimetype="audio/iso.segment")

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

    if args.window_segments is not None:
        window_segments = args.window_segments
    else:
        nominal_segment_duration_seconds = float(package_descriptor["segment_duration_seconds"])
        window_segments = max(
            1, math.ceil(args.dvr_window_seconds / nominal_segment_duration_seconds)
        )

    logger.info(
        "DVR window: %d segment(s) (~%.1fs nominal, requested %.1fs)",
        window_segments,
        window_segments * float(package_descriptor["segment_duration_seconds"]),
        args.dvr_window_seconds,
    )

    app = create_app(args.package_dir, epoch_ticks, window_segments=window_segments)
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
