"""HTTP side of low latency: paced segments, LL-HLS parts, held requests."""

from __future__ import annotations

import time

from flask import abort, request, Response

import cmaf
import continuity
import lowlatency
import package_io
from serving.channel import Channel


class LowLatencyHttpMixin:
    """HTTP side of low latency: pacing an in-progress segment, serving LL-HLS parts, and holding requests
    (a part, a blocking playlist reload) until the wall clock reaches their release time. Mixed into
    ServeContext, which provides `channel`, `package`, `epoch_ticks`, `decode_segment_index` and friends."""

    def ll_pace(self, boundaries: list[int], local_index: int, track_timescale: int, mode: str, index: int):
        """(start_tick, duration_ticks, track_timescale) when this segment is
        still in progress on the wall clock (low-latency DASH only), else None."""
        if not self.channel.ll_capable or local_index >= len(boundaries):
            return None
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else self.package.total_loop_duration_ticks
        duration = end - boundaries[local_index]
        start = lowlatency.segment_occurrence(
            self.channel.now_ticks(),
            self.epoch_ticks,
            self.package.total_loop_duration_ticks,
            boundaries[local_index],
            duration,
            round(self.package.segment_duration_seconds * self.package.timescale),
            loop_number=index // self.package.segments_per_loop if mode == "global" else None,
        )
        return None if start is None else (start, duration, track_timescale)

    def paced_response(self, data: bytes, mimetype: str, start_tick: int, duration_ticks: int, track_timescale: int):
        """Low-latency DASH: stream an in-progress segment chunk by chunk, each
        released at its own wall-clock end (lowlatency.py). Sent with chunked
        transfer encoding (no Content-Length), so a player reads each chunk as
        it arrives."""
        try:
            chunks = lowlatency.split_chunks(data)
        except ValueError:
            return Response(data, mimetype=mimetype)  # not chunked: serve whole
        offsets = lowlatency.release_offsets_ticks(
            [t for _, t in chunks], track_timescale, self.package.timescale, duration_ticks
        )
        moof = cmaf._find(data, ("moof",))
        bodies = [c for c, _ in chunks]
        if moof and moof[0]:
            bodies[0] = data[: moof[0]] + bodies[0]  # styp/sidx ahead of the first chunk
        stream = lowlatency.paced(
            bodies, [start_tick + o for o in offsets], self.channel.now_ticks, time.sleep, self.package.timescale
        )
        resp = Response(stream, mimetype=mimetype)
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    def wait_until(self, release_tick: int) -> None:
        """Hold the request until the wall clock reaches `release_tick` (an LL-HLS part or segment becoming
        available), in short slices; gives up after three target durations, as a client would."""
        limit = self.channel.now_ticks() + 3 * self.package.max_segment_duration_seconds_rounded_up * self.package.timescale
        while True:
            now = self.channel.now_ticks()
            remaining = release_tick - now
            if remaining <= 0 or now >= limit:
                return
            time.sleep(min(remaining / self.package.timescale, 0.05))

    def bad_request(self, message: str):
        abort(Response(f"{message}\n", status=400, mimetype="text/plain"))

    def block_for_reload(self, ch: Channel, window, rendition_name: str | None) -> None:
        """LL-HLS blocking playlist reload: `_HLS_msn` (and `_HLS_part`) ask for a playlist that already holds that
        segment (part); hold the request until it exists. Ignored unless this is a low-latency live playlist.
        `_HLS_skip` (delta updates) is accepted and ignored: CAN-SKIP-UNTIL is not advertised."""
        raw_msn, raw_part = request.args.get("_HLS_msn"), request.args.get("_HLS_part")
        if raw_part is not None and raw_msn is None:
            self.bad_request("'_HLS_part' needs '_HLS_msn'")
        if raw_msn is None or not ch.hls_ll_live(window):
            return
        try:
            msn = int(raw_msn)
            part = int(raw_part) if raw_part is not None else None
        except ValueError:
            self.bad_request("'_HLS_msn' and '_HLS_part' must be non-negative integers")
        if msn < 0 or (part is not None and part < 0):
            self.bad_request("'_HLS_msn' and '_HLS_part' must be non-negative integers")
        now = ch.now_ticks()
        if msn > ch.window_range(None, 1, now=now).last + 2:
            self.bad_request("'_HLS_msn' is more than two segments ahead of the live edge")
        spl = self.package.segments_per_loop
        loop_number, local_index = divmod(msn, spl)
        if rendition_name is None:
            boundaries = self.package.audio_rendition.audio_segment_boundary_ticks
        else:
            boundaries = self.package.rendition_by_name(rendition_name).segment_boundary_ticks
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else self.package.total_loop_duration_ticks
        duration = end - boundaries[local_index]
        start = self.epoch_ticks + loop_number * self.package.total_loop_duration_ticks + boundaries[local_index]
        if part is None:
            release = start + duration  # the whole segment
        else:
            chunks = self.package.chunk_table(rendition_name, local_index, duration)
            if part >= len(chunks):
                self.bad_request(f"'_HLS_part' is beyond the last part of segment {msn}")
            release = lowlatency.part_release_tick(chunks, start, part)
        self.wait_until(release)

    def serve_part(self, rendition_name: str | None, physical_index: int, part: int, mode: str, origin_loop: int | None):
        """One LL-HLS part (a baked chunk) of a segment, `rendition_name` None = audio. A part that is not complete
        yet is held until it is (the answer to a preload hint); a finished one is served at once."""
        if not self.channel.ll_capable or self.package.hls_format != "cmaf":
            abort(404)
        local_index, shift_ticks = self.decode_segment_index(physical_index, mode, origin_loop)
        try:
            if rendition_name is None:
                audio = self.package.audio_rendition
                path = self.channel.audio_segment_bytes_path(local_index)
                boundaries, track_timescale, mimetype = audio.audio_segment_boundary_ticks, audio.audio_timescale(self.package.timescale), "audio/iso.segment"
                if shift_ticks is not None:  # tfdt is in the audio track's own timescale
                    shift_ticks = round(shift_ticks * track_timescale / self.package.timescale)
            else:
                rendition = self.package.rendition_by_name(rendition_name)
                path = self.channel.segment_bytes_path(rendition_name, local_index)
                boundaries, track_timescale, mimetype = rendition.segment_boundary_ticks, self.package.video_track_timescale(rendition), "video/iso.segment"
        except (KeyError, IndexError):
            abort(404)
        if path is None:
            abort(404)
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else self.package.total_loop_duration_ticks
        duration = end - boundaries[local_index]
        chunks = self.package.chunk_table(rendition_name, local_index, duration)
        if part >= len(chunks):
            abort(404)
        pace = self.ll_pace(boundaries, local_index, track_timescale, mode, physical_index)
        if pace is not None:
            self.wait_until(lowlatency.part_release_tick(chunks, pace[0], part))
        data = package_io.read_bytes(path)
        if shift_ticks is not None:
            data = continuity.shift_cmaf_fragment(data, shift_ticks, sequence_number=physical_index)
        chunk = chunks[part]
        return Response(data[chunk.byte_start : chunk.byte_end], mimetype=mimetype)
