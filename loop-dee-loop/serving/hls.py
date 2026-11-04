"""HLS playlists (classic and Apple LL-HLS): the multivariant playlist and every media playlist."""

from __future__ import annotations

import bisect
import dataclasses
import datetime as _dt
import re
from collections.abc import Callable

import lowlatency
from loop_math import program_date_time_ticks, segment_index_for_position, ticks_to_wall_clock_seconds
from scte35_signaling import (
    build_cue_in_tag,
    build_cue_out_cont_tag,
    build_cue_out_tag,
    build_daterange_tags,
    build_event_id_map,
    build_grouped_daterange_tags,
    markers_to_signaling,
    SignalingMarker,
)
from serving import constants
from timeshift import TimeWindow

from serving.constants import LL_HLS_PART_SEGMENTS
from serving.markers import (
    _asset_ids_starting_in_segment,
    _describe_markers,
    _hls_payload_markers,
    _remap_signaling_markers,
)
from serving.package import LoopPackage
from serving.timeline import compute_discontinuity_sequence


@dataclasses.dataclass(frozen=True)
class _HlsLL:
    """What an LL-HLS media playlist needs besides its own boundaries: how to get the parts of one of its segments,
    and the other playlists (name, boundaries, parts) to point `EXT-X-RENDITION-REPORT` at."""

    chunks: Callable[[int], "list[lowlatency.Chunk]"]
    others: "list[tuple[str, list[int], Callable[[int], list[lowlatency.Chunk]]]]"


def _part_uri(segment_uri: str, part: int) -> str:
    """The URI of part `part` of a CMAF segment: `.../seg/12.m4s` -> `.../seg/12.3.m4s`."""
    return re.sub(r"\.m4s$", f".{part}.m4s", segment_uri)


class HlsMixin:
    """HLS playlists (classic and Apple LL-HLS): the multivariant playlist and every media playlist."""

    def hls_ll_live(self, window: "TimeWindow | None") -> bool:
        """Whether an HLS media playlist for this request is a low-latency one: advertised, live (no startover or
        catchup range, no `offset`), and the package has chunks to make parts of."""
        return (
            self.ll_hls and window is None and self.offset_ticks == 0 and self.package.part_target_seconds is not None
        )

    def _ll_chunks(self, rendition_name: str | None, boundary_ticks: list[int]) -> Callable[[int], "list[lowlatency.Chunk]"]:
        pkg = self.package

        def chunks(local_index: int) -> "list[lowlatency.Chunk]":
            end = boundary_ticks[local_index + 1] if local_index + 1 < len(boundary_ticks) else pkg.total_loop_duration_ticks
            return pkg.chunk_table(rendition_name, local_index, end - boundary_ticks[local_index])

        return chunks

    def _hls_ll(self, own: str | None) -> _HlsLL:
        """`own` is this playlist's rendition name, or None for the audio playlist."""
        pkg = self.package
        by_name = {r.name: r for r in pkg.video_renditions}
        mine = by_name[own] if own is not None else None
        boundaries = mine.segment_boundary_ticks if mine else pkg.audio_rendition.audio_segment_boundary_ticks
        others = [
            (pkg.video_playlist_name(r), r.segment_boundary_ticks, self._ll_chunks(r.name, r.segment_boundary_ticks))
            for r in pkg.video_renditions
            if r.name != own
        ]
        if pkg.has_audio and own is not None and pkg.hls_format == "cmaf":
            ab = pkg.audio_rendition.audio_segment_boundary_ticks
            others.append((LoopPackage.AUDIO_PLAYLIST, ab, self._ll_chunks(None, ab)))
        return _HlsLL(chunks=self._ll_chunks(own, boundaries), others=others)

    def build_hls_master_playlist(self, query: str = "") -> str:
        """Build the HLS multivariant (master) playlist -- required by the
        HLS spec and by most real players (Safari/hls.js, etc. generally
        expect the entry-point URL to be a multivariant playlist, even for
        a single-rendition/bitrate stream, not a bare media playlist).

        One #EXT-X-STREAM-INF per video rendition, each pointing at its own
        `video[_audio][_N].m3u8` (LoopPackage.video_playlist_name). #EXT-X-STREAM-INF attributes
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
        # SCOPE.md §13.5: a time-shifted request's params must ride along on
        # every child playlist URI, else players would fetch the live ones.
        suffix = f"?{query}" if query else ""

        lines = [
            "#EXTM3U",
            f"# {constants.GENERATOR_COMMENT}",
            "#EXT-X-VERSION:6" if pkg.hls_format == "ts" else "#EXT-X-VERSION:7",
        ]

        audio_codecs: list[str] = []
        audio_bandwidth = 0
        separate_audio = pkg.has_audio and (pkg.hls_format == "cmaf" or not pkg.hls_ts_mux_audio)
        if pkg.has_audio and pkg.audio_rendition.audio_variant is not None:
            a = pkg.audio_rendition.audio_variant
            audio_codecs = [a["codecs"]]
            audio_bandwidth = a["bandwidth"]
            if separate_audio:
                lines.append(
                    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Default",'
                    f'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8{suffix}"'
                )

        for rendition in pkg.video_renditions:
            v = rendition.video_variant
            stream_inf_attrs = [
                f"BANDWIDTH={v['bandwidth'] + audio_bandwidth}",
                f'CODECS="{",".join([v["codecs"], *audio_codecs])}"',
                f"RESOLUTION={v['width']}x{v['height']}",
                f"FRAME-RATE={v['frame_rate']:.3f}",
            ]
            if separate_audio:
                stream_inf_attrs.append('AUDIO="audio"')

            lines.append("#EXT-X-STREAM-INF:" + ",".join(stream_inf_attrs))
            lines.append(pkg.video_playlist_name(rendition) + suffix)

        return "\n".join(lines) + "\n"

    def build_hls_manifest(
        self, rendition_name: str, window_segments: int | None = None, window: TimeWindow | None = None,
        query: str = "",
    ) -> str:
        rendition = self.package.rendition_by_name(rendition_name)
        is_ts = self.package.hls_format == "ts"
        # A sparse-mode rendition's segments are self-initializing (SCOPE.md
        # §11) -- there's no shared init segment to point #EXT-X-MAP at,
        # same as the "ts" format's own no-init convention.
        no_init = is_ts or rendition.self_initializing
        per_span = not is_ts and rendition.shared_init
        return self._build_hls_media_playlist(
            boundary_ticks=rendition.segment_boundary_ticks,
            init_uri=None if no_init or per_span else f"{rendition.name}/init.mp4",
            span_init_uri=(lambda k: f"{rendition.name}/init_{k}.mp4") if per_span else None,
            seg_uri_template=f"{rendition.name}/seg/{{index}}.ts" if is_ts else f"{rendition.name}/seg/{{index}}.m4s",
            window_segments=window_segments,
            window=window,
            ll=self._hls_ll(rendition.name) if self.hls_ll_live(window) and not rendition.sparse else None,
            query=query,
        )

    def build_hls_audio_manifest(
        self, window_segments: int | None = None, window: TimeWindow | None = None, query: str = ""
    ) -> str:
        pkg = self.package
        if not pkg.has_audio:
            raise RuntimeError("This loop package has no audio track to serve.")
        if pkg.hls_format == "ts" and pkg.hls_ts_mux_audio:
            raise RuntimeError("Audio is muxed into the HLS TS video segments.")
        is_ts = pkg.hls_format == "ts"
        return self._build_hls_media_playlist(
            boundary_ticks=pkg.audio_rendition.audio_segment_boundary_ticks,
            init_uri=None if is_ts or pkg.audio_rendition.audio_sparse else "audio/init.mp4",
            span_init_uri=(lambda k: f"audio/init_{k}.mp4") if pkg.audio_rendition.audio_sparse and not is_ts else None,
            seg_uri_template="audio/seg/{index}.ts" if is_ts else "audio/seg/{index}.m4s",
            window_segments=window_segments,
            window=window,
            ll=self._hls_ll(None) if self.hls_ll_live(window) and not is_ts and not pkg.audio_rendition.audio_sparse else None,
            query=query,
        )

    def _build_hls_media_playlist(
        self,
        *,
        boundary_ticks: list[int],
        init_uri: str | None,
        seg_uri_template: str,
        window_segments: int | None = None,
        span_init_uri: Callable[[int], str] | None = None,
        window: TimeWindow | None = None,
        ll: _HlsLL | None = None,
        query: str = "",
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
        pkg = self.package
        # SCOPE.md §13: explicit startover/catchup range, else the sliding
        # live window ending at "now".
        now = self.now_ticks()  # one reading: the window and the parts released must agree
        win = self.window_range(window, window_segments, now=now)
        media_sequence = win.last
        ll_on = ll is not None and self.hls_ll_live(window)
        part_target = pkg.part_target_seconds if ll_on else None

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
        #
        # `media_sequence` (from segment_index_for_position/
        # global_segment_number above) is the segment CONTAINING "now" --
        # i.e. the live edge, the LAST entry the window should end at. The
        # window itself must span BACKWARD from there (recent history up
        # to and including the live edge), never forward past it: segments
        # after the live edge haven't been "reached" in real time yet, even
        # though the underlying files already exist (every segment is
        # pre-baked -- see LoopPackage docstring), so serving them as if
        # already live lets a client play ahead of true wall-clock time.
        # Clamped to 0 only matters in the first few seconds after this
        # process starts, when fewer than `window_segments` have "aged"
        # yet -- the window is simply smaller than requested until then,
        # same as any real live stream's startup ramp-up.
        first_global_index = win.first
        # grave-robber/SCOPE.md §6.1: generalizes the original "one
        # discontinuity per loop wrap" counter (previously just
        # `first_global_index // segments_per_loop`) to also count internal
        # asset-boundary discontinuities -- reduces to exactly that formula
        # when pkg.boundaries == {0} (see compute_discontinuity_sequence).
        # SCOPE.md §12: in continuity mode there is never a discontinuity to
        # report -- every loop wrap's timestamps are rewritten to be
        # genuinely continuous (see the segment byte-serving routes) -- so
        # the sequence header is always 0 rather than computed.
        period_starts = self.hls_period_starts
        first_discontinuity_sequence = compute_discontinuity_sequence(
            first_global_index, pkg.segments_per_loop, period_starts
        ) if period_starts else 0

        lines = [
            "#EXTM3U",
            f"# {constants.GENERATOR_COMMENT}",
            *(f"# {c}" for c in self._header_comments(window)),
            f"# current loop: {self._loop_label(first_global_index // pkg.segments_per_loop)}",
            "#EXT-X-VERSION:9" if ll_on else (
                "#EXT-X-VERSION:6" if init_uri is None and span_init_uri is None else "#EXT-X-VERSION:7"
            ),
            f"#EXT-X-TARGETDURATION:{pkg.max_segment_duration_seconds_rounded_up}",
            *(
                [
                    # Parts of the newest segments, a blocking playlist reload, a preload hint: Apple's LL-HLS
                    # (RFC 8216bis). PART-HOLD-BACK at 3 part targets is the spec's recommendation.
                    f"#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK={3 * part_target:.3f}",
                    f"#EXT-X-PART-INF:PART-TARGET={part_target:.3f}",
                ]
                if ll_on
                else []
            ),
            f"#EXT-X-MEDIA-SEQUENCE:{first_global_index}",
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{first_discontinuity_sequence}",
        ]
        if window is not None:
            lines.append("#EXT-X-PLAYLIST-TYPE:VOD" if window.ended else "#EXT-X-PLAYLIST-TYPE:EVENT")
            # Without this, players treat a growing EVENT playlist like live
            # and join a few segments from its end, ignoring the requested
            # start point. TIME-OFFSET=0 == the first segment (RFC 8216 4.3.5.2).
            lines.append("#EXT-X-START:TIME-OFFSET=0")
        sorted_boundaries = sorted(pkg.boundaries)

        def _span_map_line(local_index: int) -> str:
            # One init per output span (discontinuity): the MAP must follow
            # every #EXT-X-DISCONTINUITY and lead the playlist.
            span = bisect.bisect_right(sorted_boundaries, local_index) - 1
            return f'#EXT-X-MAP:URI="{span_init_uri(span)}"'

        if span_init_uri is not None:
            lines.append(_span_map_line(first_global_index % pkg.segments_per_loop))
        elif init_uri is not None:
            lines.append(f'#EXT-X-MAP:URI="{init_uri}"')

        # An EXT-X-DATERANGE describes one point on the presentation
        # timeline; it appears exactly ONCE per manifest response, on the
        # earliest in-window segment its active interval overlaps (see
        # Channel.marker_anchors) -- not repeated on every segment the
        # interval spans. Same ID and byte-identical attributes on every
        # poll (RFC 8216 4.4.5.1).
        anchors = self.marker_anchors(first_global_index, media_sequence)

        # [markers].cue_tags: which cue_breaks (see LoopPackage.__init__)
        # have already had their opening #EXT-X-CUE-OUT/-CONT emitted in
        # this response -- same per-response, visited-in-increasing-order
        # dedupe pattern as the marker anchors above.
        seen_cue_event_ids: set[str] = set()

        # Never emit past `media_sequence` (the live edge) -- when
        # first_global_index was clamped to 0 above (only possible in the
        # first few seconds of this process's life), a plain
        # `range(window_segments)` would overshoot past the live edge and
        # reintroduce future segments, the exact bug being fixed here.
        entries_in_window = media_sequence - first_global_index + 1
        ll_first_part_segment = max(first_global_index, media_sequence - LL_HLS_PART_SEGMENTS + 1)
        ll_last: tuple[int, int, int] | None = None  # (segment, parts released, parts in it) of the newest segment
        discontinuities = set(self.hls_discontinuities(first_global_index, media_sequence))
        for i in range(entries_in_window):
            global_index = first_global_index + i
            local_index = global_index % pkg.segments_per_loop
            local_loop_number = global_index // pkg.segments_per_loop

            if global_index in discontinuities:
                # This segment starts a new loop iteration OR an internal
                # asset-boundary join (grave-robber/SCOPE.md §6.1), and
                # isn't the very first entry in the window (whose implicit
                # discontinuity sequence is already covered by the header
                # above) -- signal the timestamp discontinuity here. Never
                # reached in continuity mode (SCOPE.md §12): that mode
                # requires boundaries == {0} (Channel._validate_continuous),
                # and the loop-wrap boundary itself needs no signal there
                # since the served bytes are rewritten to be genuinely
                # continuous across it.
                lines.append("#EXT-X-DISCONTINUITY")
                if span_init_uri is not None:
                    lines.append(_span_map_line(local_index))

            # Plain `#` comment (ignored by every HLS client) naming the
            # loop iteration that starts here -- also in continuity mode,
            # where the wrap carries no #EXT-X-DISCONTINUITY.
            if local_index == 0:
                lines.append("")
                lines.append(f"# loop: {self._loop_label(local_loop_number)}")

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
            ref_seg_start_ticks, ref_seg_end_ticks = self.ref_segment_bounds(local_index)

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
            #
            # A CUE-OUT's DATERANGE stays "active" (per _marker_covers_segment)
            # across every segment its interval overlaps, but must only be
            # emitted on the FIRST such segment still present in this window
            # -- not repeated on each one (see marker_anchors above).
            # Skipped entirely when cue_tags="only" -- no DATERANGE fallback
            # in that mode, #EXT-X-CUE-OUT/-IN below is the only signaling.
            segment_markers = anchors.get(global_index, [])
            if segment_markers:
                segment_id_map = (
                    build_event_id_map(pkg.markers, local_loop_number) if pkg.increment_event_ids else {}
                )
                lines.extend(
                    f"# {line}"
                    for line in _describe_markers(
                        [(m, segment_id_map, None, self._marker_start_iso(m, local_loop_number)) for m in segment_markers]
                    )
                )

            if pkg.cue_tags != "only":
                matching_markers = segment_markers
                if matching_markers:
                    loop_start_ticks = program_date_time_ticks(
                        local_loop_number, 0, pkg.total_loop_duration_ticks, self.epoch_ticks
                    )
                    loop_start_seconds = ticks_to_wall_clock_seconds(
                        loop_start_ticks, pkg.timescale
                    )
                    loop_start_datetime = _dt.datetime.utcfromtimestamp(loop_start_seconds)
                    signaling_markers = markers_to_signaling(
                        _hls_payload_markers(matching_markers, pkg.daterange_mode),
                        all_markers=_hls_payload_markers(pkg.markers, pkg.daterange_mode),
                    )
                    # grave-robber/SCOPE.md §6.2: DATERANGE START-DATE is an
                    # absolute wall-clock value (unlike DASH's Period-relative
                    # <Event presentationTime>, which needs no adjustment --
                    # the Period's own start= already carries the declared
                    # offset, see build_dash_manifest), so each marker's own
                    # declared position must be computed individually here:
                    # real serving-position tick -> whichever segment it
                    # falls in -> that segment's accumulated declared offset.
                    # A no-op (0 for every index) for any package with no
                    # internal asset boundaries.
                    def _declared(sm: SignalingMarker) -> SignalingMarker:
                        return dataclasses.replace(
                            sm,
                            pts_time_ticks=sm.pts_time_ticks
                            + pkg.declared_offset_ticks_by_local_index[
                                segment_index_for_position(
                                    sm.pts_time_ticks, pkg.segment_boundary_ticks
                                )
                            ],
                            paired_out=_declared(sm.paired_out) if sm.paired_out is not None else None,
                        )

                    signaling_markers = [_declared(sm) for sm in signaling_markers]
                    if pkg.increment_event_ids:
                        signaling_markers = _remap_signaling_markers(
                            signaling_markers, pkg.markers, local_loop_number
                        )
                    daterange_builder = (
                        build_grouped_daterange_tags
                        if pkg.daterange_mode == "grouped"
                        else build_daterange_tags
                    )
                    lines.extend(
                        daterange_builder(
                            signaling_markers,
                            pkg.timescale,
                            loop_start_datetime,
                            loop_number=local_loop_number,
                            daterange_id_format=pkg.daterange_id_format,
                        )
                    )

            # [markers].cue_tags = "alongside" | "only": #EXT-X-CUE-OUT on
            # the segment where a break opens (or, for a window that opens
            # mid-break, #EXT-X-CUE-OUT-CONT with the correct ELAPSED-TIME
            # right away), #EXT-X-CUE-OUT-CONT on every interior segment,
            # #EXT-X-CUE-IN on the segment where it closes. No raw SCTE-35
            # payload in any of these -- see LoopPackage.cue_breaks.
            if pkg.cue_tags in ("alongside", "only"):
                for brk in pkg.cue_breaks:
                    start, end = brk["start_ticks"], brk["end_ticks"]
                    if ref_seg_start_ticks <= end < ref_seg_end_ticks:
                        # `end` lands exactly on this segment's own start,
                        # by construction (see bake.py: GPAC splits
                        # segments exactly at marker ticks) -- closes the
                        # break regardless of the general overlap test
                        # below, which a boundary-touching interval fails.
                        lines.append(build_cue_in_tag())
                        continue
                    if not (ref_seg_start_ticks < end and start < ref_seg_end_ticks):
                        continue
                    elapsed_ticks = ref_seg_start_ticks - start
                    if brk["event_id"] not in seen_cue_event_ids:
                        seen_cue_event_ids.add(brk["event_id"])
                        if ref_seg_start_ticks <= start < ref_seg_end_ticks:
                            lines.append(build_cue_out_tag(brk["duration_ticks"], pkg.timescale))
                            continue
                    lines.append(
                        build_cue_out_cont_tag(elapsed_ticks, brk["duration_ticks"], pkg.timescale)
                    )

            # grave-robber/SCOPE.md §6.2: the declared position fed into PDT
            # is the real serving-position tick PLUS every gap_ticks crossed
            # by an asset boundary at or before this segment -- the serving
            # position itself (used for the live-edge/window math above) is
            # never perturbed. 0 for every index on a package with no
            # internal asset boundaries, so this is exactly today's value
            # unchanged in that (the common) case.
            # Asset-boundary comments (from franken-ts's .timeline.json, see
            # bake.py's load_asset_boundaries): plain `#` playlist comments
            # -- ignored by every HLS client -- naming which playlist asset
            # starts in this segment. Decided from the reference rendition's
            # timeline, same as markers, so every playlist (and the audio
            # playlist) places the same asset's comment at the same segment
            # index. Not deduped across polls (unlike DATERANGE): re-emitting
            # a comment for a segment still in the window on every poll is
            # harmless, exactly like PROGRAM-DATE-TIME itself.
            for asset_id in _asset_ids_starting_in_segment(
                pkg.asset_boundaries, ref_seg_start_ticks, ref_seg_end_ticks
            ):
                lines.append(f"# asset: {asset_id}")

            program_date_ticks = self.segment_start_ticks(global_index, boundary_ticks)
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
            segment_uri = self.segment_uri(seg_uri_template, global_index, local_index, window)
            # Low-latency HLS: the parts of the newest segments precede that segment's EXTINF; the segment in
            # progress has only the parts released so far and no EXTINF/URI yet (it is not complete).
            in_progress = False
            if ll_on and global_index >= ll_first_part_segment:
                chunks = ll.chunks(local_index)
                segment_abs_start = self.epoch_ticks + local_loop_number * pkg.total_loop_duration_ticks + segment_start_ticks
                released = lowlatency.parts_released(chunks, segment_abs_start, now)
                for part, chunk in enumerate(chunks[:released]):
                    lines.append(
                        f'#EXT-X-PART:DURATION={ticks_to_wall_clock_seconds(chunk.duration_ticks, pkg.timescale):.5f},'
                        f'URI="{_part_uri(segment_uri, part)}"' + (",INDEPENDENT=YES" if chunk.independent else "")
                    )
                in_progress = released < len(chunks)
                ll_last = (global_index, released, len(chunks))
            if not in_progress:
                lines.append(f"#EXTINF:{segment_duration_seconds:.3f},")
                # SCOPE.md §12.2: continuity mode's byte-serving routes need the
                # ever-increasing global index (not the plain physical/local
                # one) to know which loop iteration -- and therefore which tick
                # shift -- a given segment request belongs to, since the same
                # physical file is reused every loop. This does trade away the
                # default mode's "one URL forever, cacheable across every loop
                # iteration" property (SCOPE.md §4.2) for continuity mode.
                lines.append(segment_uri)

        if ll_on:
            # The next part to be released: the one after those listed, or the first of the next segment.
            if ll_last is not None and ll_last[1] < ll_last[2]:
                hint_segment, hint_part = ll_last[0], ll_last[1]
            else:
                hint_segment, hint_part = media_sequence + 1, 0
            hint_base = self.segment_uri(seg_uri_template, hint_segment, hint_segment % pkg.segments_per_loop, window)
            lines.append(f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="{_part_uri(hint_base, hint_part)}"')
            suffix = f"?{query}" if query else ""
            for uri, boundaries, chunks_for in ll.others:
                msn, part = lowlatency.live_position(
                    boundaries, pkg.total_loop_duration_ticks, self.epoch_ticks, pkg.segments_per_loop, chunks_for, now
                )
                lines.append(f'#EXT-X-RENDITION-REPORT:URI="{uri}{suffix}",LAST-MSN={msn},LAST-PART={part}')
        if window is not None and window.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"
