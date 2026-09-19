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

Multi-rendition (ABR ladder): `loop_descriptor.json` (v2) carries a list of
`video_renditions`, each independently baked (bake.py) but sharing the same
`segments_per_loop` count and the same marker/timeline data. Exactly one
rendition also carries audio (see bake.py's `include_audio`) -- audio is
never duplicated per video rendition.
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


def _numeric_segment_index(path: Path) -> int:
    stem = path.stem
    tail = stem.rsplit("_", 1)[-1]
    return int(tail)


class VideoRendition:
    """Read-only view over one rendition's segments within a loop package
    (`<package_dir>/segments/<name>/`). At most one rendition also carries
    the shared audio track (see `has_audio`)."""

    def __init__(self, package_dir: Path, rendition: dict):
        self.name: str = rendition["name"]
        self.video_track_id: int = int(rendition["video_track_id"])
        self.video_variant: dict = rendition["video_variant"]

        self.segments_dir = package_dir / "segments" / self.name
        self.segment_files: list[Path] = sorted(
            self.segments_dir.glob(f"*track{self.video_track_id}_*.m4s"),
            key=_numeric_segment_index,
        )
        if not self.segment_files:
            raise RuntimeError(f"No segment files found in {self.segments_dir}")

        self.segment_boundary_ticks: list[int] = [
            int(t) for t in rendition["segment_boundary_ticks"]
        ]
        if len(self.segment_boundary_ticks) != len(self.segment_files):
            raise RuntimeError(
                f"Rendition '{self.name}': loop_descriptor.json declares "
                f"{len(self.segment_boundary_ticks)} segment boundary "
                f"tick(s) but {len(self.segment_files)} physical segment "
                f"file(s) were found on disk -- package is inconsistent, "
                f"refusing to serve."
            )

        self.audio_track_id: int | None = rendition.get("audio_track_id")
        self.audio_variant: dict | None = rendition.get("audio_variant")
        self.audio_segment_files: list[Path] = []
        self.audio_segment_boundary_ticks: list[int] = []
        if self.audio_track_id is not None:
            self.audio_segment_files = sorted(
                self.segments_dir.glob(f"*track{self.audio_track_id}_*.m4s"),
                key=_numeric_segment_index,
            )
            self.audio_segment_boundary_ticks = [
                int(t) for t in rendition.get("audio_segment_boundary_ticks") or []
            ]
            if len(self.audio_segment_files) != len(self.segment_files):
                raise RuntimeError(
                    f"Rendition '{self.name}': audio track has "
                    f"{len(self.audio_segment_files)} segment file(s) but "
                    f"video track has {len(self.segment_files)} -- must "
                    f"match 1:1. Package inconsistent, refusing to serve."
                )
            if len(self.audio_segment_boundary_ticks) != len(self.audio_segment_files):
                raise RuntimeError(
                    f"Rendition '{self.name}': loop_descriptor.json declares "
                    f"{len(self.audio_segment_boundary_ticks)} audio segment "
                    f"boundary tick(s) but {len(self.audio_segment_files)} "
                    f"physical audio segment file(s) found. Package "
                    f"inconsistent, refusing to serve."
                )

    @property
    def has_audio(self) -> bool:
        return self.audio_track_id is not None

    def init_path(self) -> Path:
        candidates = list(self.segments_dir.glob(f"*track{self.video_track_id}_init.mp4"))
        if not candidates:
            raise RuntimeError(f"No init segment found for rendition '{self.name}'")
        return candidates[0]

    def audio_init_path(self) -> Path:
        assert self.audio_track_id is not None
        candidates = list(self.segments_dir.glob(f"*track{self.audio_track_id}_init.mp4"))
        if not candidates:
            raise RuntimeError(f"No audio init segment found for rendition '{self.name}'")
        return candidates[0]

    def segment_path_for_index(self, index: int) -> Path:
        return self.segment_files[index % len(self.segment_files)]

    def audio_segment_path_for_index(self, index: int) -> Path:
        return self.audio_segment_files[index % len(self.audio_segment_files)]


class LoopPackage:
    """Read-only view over an immutable loop package directory (SCOPE.md §4.1
    step 6). Loaded once at process start; never mutated by request handling.
    Generalizes to N video renditions (an ABR ladder) sharing one audio
    track and one marker/timeline set.
    """

    def __init__(self, package_dir: Path):
        self.package_dir = package_dir
        descriptor_path = package_dir / "loop_descriptor.json"
        with descriptor_path.open("r", encoding="utf-8") as f:
            self.descriptor: dict = json.load(f)

        version = int(self.descriptor.get("version", 1))
        if version < 2:
            raise RuntimeError(
                f"loop_descriptor.json version {version} is too old (this "
                f"serve.py requires version >= 2, with a 'video_renditions' "
                f"list) -- rebake with the current bake.py."
            )

        self.timescale: int = int(self.descriptor["timescale"])
        self.total_loop_duration_ticks: int = int(
            self.descriptor["total_loop_duration_ticks"]
        )
        self.segment_duration_seconds: float = float(
            self.descriptor["segment_duration_seconds"]
        )
        self.markers: list[dict] = self.descriptor["markers"]

        rendition_dicts = self.descriptor.get("video_renditions") or []
        if not rendition_dicts:
            raise RuntimeError("loop_descriptor.json has no video_renditions")

        self.video_renditions: list[VideoRendition] = [
            VideoRendition(package_dir, r) for r in rendition_dicts
        ]
        # Highest-bandwidth first -- conventional master playlist ordering;
        # not semantically required (players pick by BANDWIDTH value), but
        # a consistent, predictable order regardless of filename/discovery
        # order on disk.
        self.video_renditions.sort(
            key=lambda r: r.video_variant["bandwidth"], reverse=True
        )

        segment_counts = {len(r.segment_files) for r in self.video_renditions}
        if len(segment_counts) != 1:
            raise RuntimeError(
                f"Renditions do not all have the same segment count: "
                f"{[(r.name, len(r.segment_files)) for r in self.video_renditions]} "
                f"-- every rendition in a ladder must share the same "
                f"segments_per_loop. Package inconsistent, refusing to serve."
            )
        self.segments_per_loop = segment_counts.pop()

        audio_renditions = [r for r in self.video_renditions if r.has_audio]
        if len(audio_renditions) > 1:
            raise RuntimeError(
                f"More than one rendition carries audio: "
                f"{[r.name for r in audio_renditions]} -- exactly one "
                f"rendition should carry the shared audio track."
            )
        self.audio_rendition: VideoRendition | None = (
            audio_renditions[0] if audio_renditions else None
        )

        # Reference rendition for boundary-tick-derived quantities that are
        # conceptually shared across the whole ladder (ad-decision timeline,
        # nominal segment durations) -- highest-bandwidth rendition, i.e.
        # video_renditions[0] after the sort above.
        reference = self.video_renditions[0]
        self.segment_boundary_ticks: list[int] = reference.segment_boundary_ticks

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

    @property
    def has_audio(self) -> bool:
        return self.audio_rendition is not None

    def rendition_by_name(self, name: str) -> VideoRendition:
        for r in self.video_renditions:
            if r.name == name:
                return r
        raise KeyError(name)


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

    def build_hls_master_playlist(self) -> str:
        """Build the HLS multivariant (master) playlist -- required by the
        HLS spec and by most real players (Safari/hls.js, etc. generally
        expect the entry-point URL to be a multivariant playlist, even for
        a single-rendition/bitrate stream, not a bare media playlist).

        One #EXT-X-STREAM-INF per video rendition, each pointing at its own
        `<rendition-name>/live.m3u8`. #EXT-X-STREAM-INF attributes
        (BANDWIDTH/CODECS/RESOLUTION/FRAME-RATE) come from each rendition's
        `video_variant`, the exact codec/resolution/bandwidth GPAC itself
        computed while producing the real segments at bake time (bake.py's
        read_variant_metadata) -- not re-derived or guessed here.

        If the package has an audio track, one #EXT-X-MEDIA audio group is
        declared (shared across all video renditions) and referenced from
        every #EXT-X-STREAM-INF via AUDIO="audio", with CODECS listing both
        the video and audio codec strings (per HLS spec).
        """
        pkg = self.package

        lines = ["#EXTM3U", "#EXT-X-VERSION:7"]

        audio_codecs: list[str] = []
        audio_bandwidth = 0
        if pkg.has_audio and pkg.audio_rendition.audio_variant is not None:
            a = pkg.audio_rendition.audio_variant
            audio_codecs = [a["codecs"]]
            audio_bandwidth = a["bandwidth"]
            lines.append(
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Default",'
                'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"'
            )

        for rendition in pkg.video_renditions:
            v = rendition.video_variant
            stream_inf_attrs = [
                f"BANDWIDTH={v['bandwidth'] + audio_bandwidth}",
                f'CODECS="{",".join([v["codecs"], *audio_codecs])}"',
                f"RESOLUTION={v['width']}x{v['height']}",
                f"FRAME-RATE={v['frame_rate']:.3f}",
            ]
            if audio_codecs:
                stream_inf_attrs.append('AUDIO="audio"')

            lines.append("#EXT-X-STREAM-INF:" + ",".join(stream_inf_attrs))
            lines.append(f"{rendition.name}/live.m3u8")

        return "\n".join(lines) + "\n"

    def build_hls_manifest(self, rendition_name: str, window_segments: int | None = None) -> str:
        rendition = self.package.rendition_by_name(rendition_name)
        return self._build_hls_media_playlist(
            boundary_ticks=rendition.segment_boundary_ticks,
            init_uri="init.mp4",
            seg_uri_template="seg/{index}.m4s",
            window_segments=window_segments,
        )

    def build_hls_audio_manifest(self, window_segments: int | None = None) -> str:
        pkg = self.package
        if not pkg.has_audio:
            raise RuntimeError("This loop package has no audio track to serve.")
        return self._build_hls_media_playlist(
            boundary_ticks=pkg.audio_rendition.audio_segment_boundary_ticks,
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
        """Shared builder for every video rendition's HLS media playlist and
        for the (single, shared) audio HLS media playlist.

        Segment *indexing*/loop-number sequencing (media_sequence,
        local_index, local_loop_number) is always driven by the reference
        rendition's `segments_per_loop` -- by construction (bake.py) every
        rendition/track has exactly the same segment count per loop, with
        segment i corresponding to the same conceptual time window
        everywhere, even though each rendition/track's own boundary tick
        VALUES were snapped independently. Only `boundary_ticks` (used for
        this playlist's own PROGRAM-DATE-TIME/EXTINF durations) and the
        URIs differ between renditions/audio.

        Marker (DATERANGE) placement is always decided using the
        reference rendition's segment_boundary_ticks (the ad-decision
        timeline), applied to the shared segment index -- so the same
        marker appears at the same segment index in every rendition's
        playlist and in the audio playlist.
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

        # Every physical segment file is reused, byte-for-byte, on every
        # loop iteration (SCOPE.md §4.1 step 6): its internal fMP4
        # timestamps (baseMediaDecodeTime/tfdt) therefore always restart
        # from the same loop-relative values, regardless of which real-world
        # loop iteration is being served. A playlist that presents segment
        # index `segments_per_loop - 1` immediately followed by segment
        # index 0 *without* signaling a discontinuity lies to the player:
        # it claims the two segments are timestamp-continuous when they are
        # not. Browsers' MSE demuxers (observed: Chrome's ChunkDemuxer)
        # detect this as a "RunSegmentParserLoop: stream parsing failed"
        # append failure, which cascades into MediaSource.readyState
        # "ended" and a fatal `mediaSourceRequiresReset` -- i.e. playback
        # dies exactly once per loop, after the first iteration completes.
        # #EXT-X-DISCONTINUITY-SEQUENCE (RFC 8216 §4.3.3.3) identifies the
        # discontinuity sequence of the *first* segment in this window --
        # here, simply its loop number, since every loop boundary is
        # exactly one discontinuity. An #EXT-X-DISCONTINUITY tag (§4.3.2.3)
        # is then emitted immediately before every subsequent segment that
        # starts a new loop iteration (local_index == 0), so the player
        # resets its timestamp-continuity expectations there.
        first_loop_number = media_sequence // pkg.segments_per_loop

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{pkg.max_segment_duration_seconds_rounded_up}",
            f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{first_loop_number}",
            f'#EXT-X-MAP:URI="{init_uri}"',
        ]

        for i in range(window_segments):
            global_index = media_sequence + i
            local_index = global_index % pkg.segments_per_loop
            local_loop_number = global_index // pkg.segments_per_loop

            if i > 0 and local_index == 0:
                # This segment is the first of a new loop iteration and
                # isn't the very first entry in the window (whose implicit
                # discontinuity sequence is already covered by the header
                # above) -- signal the timestamp discontinuity here.
                lines.append("#EXT-X-DISCONTINUITY")

            # This playlist's own segment start/end (for PDT/EXTINF).
            segment_start_ticks = boundary_ticks[local_index]
            if local_index + 1 < len(boundary_ticks):
                seg_end_ticks_local = boundary_ticks[local_index + 1]
            else:
                seg_end_ticks_local = pkg.total_loop_duration_ticks

            # Marker placement always decided from the reference rendition's
            # timeline (the ad-decision authority), applied to this same
            # segment index, so every playlist advertises the same marker
            # at the same index even though boundary tick VALUES differ.
            ref_seg_start_ticks = pkg.segment_boundary_ticks[local_index]
            if local_index + 1 < len(pkg.segment_boundary_ticks):
                ref_seg_end_ticks = pkg.segment_boundary_ticks[local_index + 1]
            else:
                ref_seg_end_ticks = pkg.total_loop_duration_ticks

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
                if ref_seg_start_ticks <= m["pts_time_ticks"] < ref_seg_end_ticks
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
                    build_daterange_tags(
                        signaling_markers,
                        pkg.timescale,
                        loop_start_datetime,
                        loop_number=local_loop_number,
                    )
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
        of segments as the HLS playlists, using one <AdaptationSet> per
        media type. The video AdaptationSet contains one <Representation>
        per rendition, each with its OWN <SegmentTemplate>/<SegmentTimeline>
        (DASH allows this at Representation level) since each rendition's
        segment boundary tick VALUES were snapped independently (same
        reasoning as audio -- see LoopPackage/VideoRendition docstrings).
        The <EventStream> carries the same markers as HLS's DATERANGE tags,
        authored directly from markers.json (never GPAC's own aggregation
        -- SCOPE.md §6).

        All SegmentTimeline `t` and EventStream `presentationTime` values
        are *period-relative* ticks measured from the fixed channel epoch
        (the Period starts at epoch, `start="PT0S"`, never restarted), so
        they grow monotonically across loops exactly like HLS's
        ever-increasing MEDIA-SEQUENCE.
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
        first_number = media_sequence

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

        def _segment_entries(boundary_ticks: list[int]) -> list[tuple[int, int, int]]:
            entries = []
            for i in range(window_segments):
                global_index = media_sequence + i
                local_index = global_index % pkg.segments_per_loop
                local_loop_number = global_index // pkg.segments_per_loop
                segment_start_ticks = boundary_ticks[local_index]
                if local_index + 1 < len(boundary_ticks):
                    seg_end_ticks_local = boundary_ticks[local_index + 1]
                else:
                    seg_end_ticks_local = pkg.total_loop_duration_ticks
                period_relative_start = (
                    local_loop_number * pkg.total_loop_duration_ticks + segment_start_ticks
                )
                duration_ticks = seg_end_ticks_local - segment_start_ticks
                entries.append((period_relative_start, duration_ticks, local_index))
            return entries

        # Reference (ad-decision authority) entries, used for marker placement.
        reference_entries = _segment_entries(pkg.segment_boundary_ticks)

        event_xml_parts = []
        for period_relative_start, duration_ticks, local_index in reference_entries:
            seg_start_local = pkg.segment_boundary_ticks[local_index]
            local_loop_number = (
                (period_relative_start - seg_start_local) // pkg.total_loop_duration_ticks
            )
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

        video_representations = []
        for idx, rendition in enumerate(pkg.video_renditions):
            entries = _segment_entries(rendition.segment_boundary_ticks)
            timeline_lines = "\n".join(
                f'        <S t="{t}" d="{d}" />' for t, d, _ in entries
            )
            v = rendition.video_variant
            video_representations.append(f'''      <Representation id="v{idx}" bandwidth="{v["bandwidth"]}" codecs="{v["codecs"]}" width="{v["width"]}" height="{v["height"]}" frameRate="{v["frame_rate"]:.3f}">
        <SegmentTemplate media="{rendition.name}/seg/$Number$.m4s" initialization="{rendition.name}/init.mp4"
                         timescale="{pkg.timescale}" startNumber="{first_number}">
          <SegmentTimeline>
{timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>''')

        audio_adaptation_set = ""
        if pkg.has_audio:
            a = pkg.audio_rendition.audio_variant
            audio_entries = _segment_entries(pkg.audio_rendition.audio_segment_boundary_ticks)
            audio_timeline_lines = "\n".join(
                f'        <S t="{t}" d="{d}" />' for t, d, _ in audio_entries
            )
            audio_adaptation_set = f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <Representation id="a0" bandwidth="{a["bandwidth"]}" codecs="{a["codecs"]}">
        <SegmentTemplate media="audio/seg/$Number$.m4s" initialization="audio/init.mp4"
                         timescale="{pkg.timescale}" startNumber="{first_number}">
          <SegmentTimeline>
{audio_timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
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
{chr(10).join(video_representations)}
    </AdaptationSet>{audio_adaptation_set}
  </Period>
</MPD>
'''
        return mpd

    def segment_bytes_path(self, rendition_name: str, physical_index: int) -> Path:
        return self.package.rendition_by_name(rendition_name).segment_path_for_index(physical_index)

    def audio_segment_bytes_path(self, physical_index: int) -> Path:
        assert self.package.audio_rendition is not None
        return self.package.audio_rendition.audio_segment_path_for_index(physical_index)


def create_app(package_dir: Path, epoch_ticks: int, window_segments: int = 6) -> Flask:
    package = LoopPackage(package_dir)
    channel = Channel(package, epoch_ticks, window_segments=window_segments)

    app = Flask(__name__)

    @app.get("/master.m3u8")
    def hls_master_playlist():
        body = channel.build_hls_master_playlist()
        return Response(body, mimetype="application/vnd.apple.mpegurl")

    @app.get("/manifest.mpd")
    def dash_manifest():
        body = channel.build_dash_manifest()
        return Response(body, mimetype="application/dash+xml")

    @app.get("/<rendition_name>/live.m3u8")
    def hls_manifest(rendition_name: str):
        try:
            body = channel.build_hls_manifest(rendition_name)
        except KeyError:
            abort(404)
        return Response(body, mimetype="application/vnd.apple.mpegurl")

    @app.get("/<rendition_name>/init.mp4")
    def init_segment(rendition_name: str):
        try:
            rendition = package.rendition_by_name(rendition_name)
        except KeyError:
            abort(404)
        return send_file(rendition.init_path())

    @app.get("/<rendition_name>/seg/<int:physical_index>.m4s")
    def segment(rendition_name: str, physical_index: int):
        try:
            path = channel.segment_bytes_path(rendition_name, physical_index)
        except (KeyError, IndexError):
            abort(404)
        return send_file(path, mimetype="video/iso.segment")

    if package.has_audio:
        @app.get("/audio.m3u8")
        def hls_audio_manifest():
            body = channel.build_hls_audio_manifest()
            return Response(body, mimetype="application/vnd.apple.mpegurl")

        @app.get("/audio/init.mp4")
        def audio_init_segment():
            return send_file(package.audio_rendition.audio_init_path())

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
