"""The Channel: a fixed channel epoch plus a loop package. Everything here is stateless per request."""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from loop_math import compute_loop_position, ticks_to_wall_clock_seconds
from timeshift import ANNOUNCE_MAX_SECONDS

from serving.constants import (
    DASH_ADDRESSING_CHOICES,
    DASH_TIMELINE_CHOICES,
    LL_UTC_TIMING_CHOICES,
)
from serving.dash import DashMixin
from serving.hls import HlsMixin
from serving.markers import _marker_type_id
from serving.package import LoopPackage, _cmaf_tfdt_version, _first_present_segment
from serving.plan import TimelinePlanMixin
from serving.timeline import (
    compute_signal_breaks,
    expand_segmentation_pairs,
    utc_naive_datetime,
)
from serving.variants import VariantsMixin
from serving.window_json import WindowJsonMixin


class Channel(VariantsMixin, TimelinePlanMixin, HlsMixin, DashMixin, WindowJsonMixin):
    """Fixed channel epoch + loop package. All request handling goes through
    this class's stateless methods -- no attribute here is ever mutated
    after construction.

    `continuous` (SCOPE.md §12): when True, every physical segment's
    internal timestamps are rewritten per request (continuity.py) to a
    genuinely ever-increasing absolute position instead of restarting from
    the same loop-relative values every iteration, so no
    #EXT-X-DISCONTINUITY / new DASH Period is needed at the loop wrap. Only
    supported for a package with no internal asset-boundary discontinuities
    (`boundaries == {0}`) -- i.e. no discontinuity/multiple-Period join
    anywhere in the source, whether that source is a single franken-ts
    encode (always true there) or a grave-robber/archive capture that
    happens to be a single continuous span (SCOPE.md §12.6) -- and CMAF
    fragments baked with a 64-bit (v1) tfdt. Both checked once here, at
    startup, not per request.
    """

    def __init__(
        self,
        package: LoopPackage,
        epoch_ticks: int,
        window_segments: int = 6,
        continuous: bool = False,
        period_on_segmentation: frozenset[int] = frozenset(),
        channel_name: str = "",
        dash_addressing: str = "number",
        dash_timeline: str = "full",
        ll_dash: bool = False,
        ll_target_latency: float | None = None,
        ll_utc_timing_url: str = "",
        ll_utc_timing: str = "direct",
        ll_optin: bool = False,
        ll_hls: bool = False,
        announce_ahead: float = 0.0,
        announce_on: frozenset[int] = frozenset(),
        announce_splice_insert: bool = False,
    ):
        if ll_utc_timing not in LL_UTC_TIMING_CHOICES:
            raise ValueError(f"ll_utc_timing must be one of {LL_UTC_TIMING_CHOICES}, got {ll_utc_timing!r}")
        package_chunk = getattr(package, "chunk_duration_seconds", None)
        if ll_hls and getattr(package, "hls_format", "cmaf") != "cmaf":
            raise ValueError("low-latency HLS needs CMAF segments (hls_format 'cmaf'): MPEG-TS cannot carry parts")
        if (ll_dash or ll_optin or ll_hls) and package_chunk is None:
            raise ValueError(
                "low-latency DASH needs a package baked with `bake.py --chunk-duration` "
                "(this one has a single chunk per segment)"
            )
        if dash_timeline not in DASH_TIMELINE_CHOICES:
            raise ValueError(f"dash_timeline must be one of {DASH_TIMELINE_CHOICES}, got {dash_timeline!r}")
        if dash_addressing not in DASH_ADDRESSING_CHOICES:
            raise ValueError(f"dash_addressing must be one of {DASH_ADDRESSING_CHOICES}, got {dash_addressing!r}")
        if not isinstance(epoch_ticks, int):
            # ValueError (not TypeError) is this constructor's existing contract for every bad argument.
            raise ValueError("epoch_ticks must be int")  # noqa: TRY004
        if window_segments < 1:
            raise ValueError("window_segments must be >= 1")
        self.continuous_error: str | None = None
        if continuous:
            self._validate_continuous(package)
        else:
            # SCOPE.md §13.5: a request may opt in to continuity per-request,
            # so learn once, at startup, whether the package supports it.
            try:
                self._validate_continuous(package)
            except Exception as exc:  # noqa: BLE001 -- any reason means 'unsupported'
                self.continuous_error = str(exc)
        self.package = package
        self.epoch_ticks = epoch_ticks
        self.channel_name = channel_name
        # DASH SegmentTemplate addressing: "number" ($Number$ + startNumber,
        # the default) or "time" ($Time$ = the <S t=...> value). HLS is unaffected.
        self.dash_addressing = dash_addressing
        # "full": every <S> has @t and @d. "compact": implicit @t where
        # contiguous + @r for runs of equal @d.
        self.dash_timeline = dash_timeline
        # Local index where each real asset span starts (the Period origin of
        # the default, loop-relative timeline); $Time$ URLs name the span.
        self.real_span_starts = sorted(package.boundaries)
        # Channel-level: segmentation_type_ids whose markers force a new
        # Period / #EXT-X-DISCONTINUITY (signal only -- timestamps stay
        # continuous across it). Resolved once to local segment indices.
        self.period_on_segmentation = expand_segmentation_pairs(frozenset(period_on_segmentation))
        self.signal_breaks: frozenset[int] = compute_signal_breaks(
            package.markers, package.segment_boundary_ticks, self.period_on_segmentation
        )
        # Controls the DVR window / manifest size: how many segments ahead
        # of the live edge are advertised in each manifest response (HLS
        # sliding window, DASH SegmentTimeline + timeShiftBufferDepth).
        # Larger = more seekable-back history for players, larger manifest
        # responses. See --dvr-window-seconds / --window-segments in
        # serve.py's CLI.
        self.window_segments = window_segments
        self.continuous = continuous
        # Set only on a per-request with_offset() variant (SCOPE.md §13.7).
        self.offset_ticks = 0
        # Low-latency DASH (lowlatency.py): chunks of an in-progress segment are
        # released at their own wall-clock end, and the MPD lets players ask
        # for a segment `ll_ato_seconds` early. ATO = segment - chunk, so the
        # first chunk is due right when it is requested (a little before for
        # uneven or marker-shortened segments, where the server simply holds).
        # `ll_dash`: this MPD advertises low latency. `ll_capable`: the server delivers in-progress segments paced
        # (a server property -- segment URLs carry no query string). `ll_optin`: capable, but an MPD only
        # advertises LL when asked (`?low_latency=true`).
        self.ll_dash = ll_dash
        # `ll_hls`: this HLS media playlist advertises low latency (parts, blocking reload, preload hint).
        self.ll_hls = ll_hls
        self.ll_capable = ll_dash or ll_optin or ll_hls
        chunk = package_chunk or 0.0
        self.ll_ato_seconds = round(max(0.0, package.segment_duration_seconds - chunk), 3) if (ll_dash or ll_optin or ll_hls) else 0.0
        self.ll_target_latency_seconds = ll_target_latency or max(4 * chunk, 1.0)
        self.ll_utc_timing_url = ll_utc_timing_url
        # How a low-latency MPD lets the player sync its clock: "http" (a polled
        # /time endpoint), "direct" (the time written into the MPD, which must
        # then never be cached) or "none" (trust the player's own clock).
        self.ll_utc_timing = ll_utc_timing
        # Markers announced ahead of the segment they fall in (see `_marker_covers_segment`): `announce_ahead`
        # seconds early, for markers of these segmentation_type_ids (and plain splice_insert markers if
        # `announce_splice_insert`); neither given = every marker. 0 = off, the default.
        self.announce_ahead_ticks = 0
        self.announce_on: frozenset[int] = frozenset()
        self.announce_splice_insert = False
        self._set_announce(announce_ahead, announce_on, announce_splice_insert)

    def _set_announce(self, announce_ahead: float, announce_on: frozenset[int], announce_splice_insert: bool) -> None:
        if announce_ahead < 0 or announce_ahead > ANNOUNCE_MAX_SECONDS:
            raise ValueError(f"announce_ahead must be between 0 and {ANNOUNCE_MAX_SECONDS} seconds")
        ticks = int((Decimal(str(announce_ahead)) * self.package.timescale).to_integral_value())
        self.announce_ahead_ticks = ticks
        self.announce_on = expand_segmentation_pairs(frozenset(announce_on))
        self.announce_splice_insert = announce_splice_insert

    def marker_announce_ticks(self, marker: dict) -> int:
        """How many ticks before its own start this marker is announced (0: at its own segment, as always)."""
        if not self.announce_ahead_ticks:
            return 0
        if not self.announce_on and not self.announce_splice_insert:
            return self.announce_ahead_ticks
        type_id = _marker_type_id(marker)
        if type_id is None:
            return self.announce_ahead_ticks if self.announce_splice_insert else 0
        return self.announce_ahead_ticks if type_id in self.announce_on else 0

    def _period_starts(self, signal_breaks: frozenset[int]) -> frozenset[int]:
        """Local segment indices that open a new Period / discontinuity in
        the current mode. Default mode: every real boundary (always incl. 0,
        the loop wrap) plus the forced signal breaks. Continuous mode: the
        loop wrap is seamless, so only the forced signal breaks (which may
        include 0, a marker right at the loop start)."""
        if self.continuous:
            return signal_breaks
        return frozenset(self.package.boundaries) | signal_breaks

    @property
    def hls_period_starts(self) -> frozenset[int]:
        return self._period_starts(self.signal_breaks)

    @property
    def dash_period_starts(self) -> frozenset[int]:
        return self._period_starts(self.signal_breaks)

    @staticmethod
    def _validate_continuous(package: LoopPackage) -> None:
        if package.boundaries != {0}:
            raise RuntimeError(
                f"continuity mode (--continuous-timeline) does not support "
                f"internal asset-boundary discontinuities (grave-robber/"
                f"SCOPE.md §6.1) -- only the plain loop-wrap boundary. This "
                f"package declares boundaries={sorted(package.boundaries)} "
                f"(i.e. the source itself has a discontinuity/multiple-"
                f"Period join, not just the loop wrap -- SCOPE.md §12.6)."
            )
        # SCOPE.md §12.6: sparse/self-initializing renditions (grave-robber
        # archive input, §11) are allowed here -- the check above is the
        # real gate ("is there any discontinuity in the source at all"),
        # not `.sparse` itself. A single-span archive capture (boundaries
        # == {0}) is mechanically the same shape as a franken-ts encode as
        # far as continuity is concerned: one span of segments to shift by
        # a constant per loop iteration. Segments with no physical media
        # (a hole, SCOPE.md §11.1) stay a 404 either way -- shift_ticks is
        # never applied to bytes that don't exist.
        for rendition in package.video_renditions:
            representative = _first_present_segment(rendition.segment_files, rendition.name)
            version = _cmaf_tfdt_version(representative)
            if version != 1:
                raise RuntimeError(
                    f"continuity mode requires a 64-bit (v1) tfdt, so the "
                    f"per-loop tick offset (loop_number * "
                    f"total_loop_duration_ticks) never overflows over a "
                    f"long-running channel's lifetime -- rendition "
                    f"'{rendition.name}' was baked with a 32-bit (v0) tfdt. "
                    f"Rebake with a GPAC/ffmpeg invocation that emits v1 "
                    f"tfdt boxes (SCOPE.md §12.4)."
                )
        audio = package.audio_rendition
        if audio is not None and audio.audio_segment_files:
            representative = _first_present_segment(audio.audio_segment_files, "audio")
            version = _cmaf_tfdt_version(representative)
            if version != 1:
                raise RuntimeError(
                    "continuity mode requires a 64-bit (v1) tfdt for the "
                    "shared audio track too, same reasoning as the video "
                    "check above -- rebake with a GPAC/ffmpeg invocation "
                    "that emits v1 tfdt boxes (SCOPE.md §12.4)."
                )

    def now_ticks(self) -> int:
        """The only place wall-clock time is sampled. Converted to an
        integer tick count exactly once, at the point of measurement."""
        return round(time.time() * self.package.timescale) + self.offset_ticks

    def current_position(self):
        return compute_loop_position(
            self.now_ticks(), self.epoch_ticks, self.package.total_loop_duration_ticks
        )

    def _iso_ticks(self, ticks: int) -> str:
        seconds = ticks_to_wall_clock_seconds(ticks, self.package.timescale)
        return utc_naive_datetime(seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def segment_bytes_path(self, rendition_name: str, physical_index: int) -> Path:
        return self.package.rendition_by_name(rendition_name).segment_path_for_index(physical_index)

    def audio_segment_bytes_path(self, physical_index: int) -> Path:
        assert self.package.audio_rendition is not None
        return self.package.audio_rendition.audio_segment_path_for_index(physical_index)

    def hls_ts_segment_path(self, rendition_name: str, physical_index: int) -> Path | None:
        """Returns None for a sparse-mode index with no physical TS file
        (SCOPE.md §11.3's 404 guard, applied to the hls_format='ts' store
        as well as the CMAF one)."""
        rendition = self.package.rendition_by_name(rendition_name)
        if physical_index >= self.package.segments_per_loop:
            raise IndexError(physical_index)
        path = self.package.package_dir / "hls-ts" / rendition.name / f"{physical_index}.ts"
        if rendition.sparse and not path.exists():
            return None
        return path
