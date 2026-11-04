"""Per-request variants of a Channel (`with_*`): each returns a shallow copy with one setting changed,
never mutating the channel."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from scte35_signaling import (
    build_cue_breaks,
    event_id_increment_problem,
    non_splice_insert_event_ids,
    validate_daterange_id_format,
)
from timeshift import TimeshiftError

from serving.timeline import compute_signal_breaks, expand_segmentation_pairs

if TYPE_CHECKING:
    from serving.channel import Channel


class VariantsMixin:
    """Per-request variants of a Channel (`with_*`): each returns a shallow copy with one setting changed,"""

    def for_mode(self, continuous: bool) -> Channel:
        """This channel, but serving in the requested continuity mode
        (SCOPE.md §13.5). Channel is immutable after construction, so a
        shallow copy with the flag flipped is a safe per-request variant.
        Raises TimeshiftError if the package can't be served continuously."""
        if continuous == self.continuous:
            return self
        if continuous and self.continuous_error is not None:
            raise TimeshiftError(f"continuous timeline is not supported: {self.continuous_error}")
        variant = copy.copy(self)
        variant.continuous = continuous
        variant.continuous_error = None if continuous else self.continuous_error
        return variant

    def with_offset(self, offset_ticks: int) -> Channel:
        """This channel as if "now" were `offset_ticks` later (negative:
        earlier) -- SCOPE.md §13.7. The epoch is untouched and every
        timestamp stays true content time: the live edge (and the DVR window
        behind it) simply sits at now+offset, and "is the end in the past",
        "start in the future" etc. are judged against that pretend-now.
        Raises TimeshiftError if that would put "now" before the epoch."""
        if offset_ticks == 0:
            return self
        variant = copy.copy(self)
        variant.offset_ticks = offset_ticks
        if variant.now_ticks() < self.epoch_ticks:
            raise TimeshiftError("offset puts 'now' before the channel epoch (nothing existed yet)")
        return variant

    def with_epoch(self, epoch_ticks: int) -> Channel:
        """This channel as if its epoch (loop 0's start) were `epoch_ticks`:
        loop numbers, PROGRAM-DATE-TIME, Period starts, availabilityStartTime
        and global segment numbers all derive from it. Unlike `with_offset`
        (which only moves "now"), the whole timeline moves. Segment bytes are
        a function of the segment URL alone, so nothing else changes. Call
        `check_clock` once every clock override has been applied."""
        if epoch_ticks == self.epoch_ticks:
            return self
        variant = copy.copy(self)
        variant.epoch_ticks = epoch_ticks
        return variant

    def check_clock(self) -> None:
        """Raises TimeshiftError when "now" (offset included) is before the
        epoch -- nothing existed yet."""
        if self.now_ticks() < self.epoch_ticks:
            raise TimeshiftError("epoch is in the future (nothing existed yet)")

    def with_window_segments(self, window_segments: int) -> Channel:
        if window_segments < 1:
            raise TimeshiftError("the DVR window must cover at least one segment")
        if window_segments == self.window_segments:
            return self
        variant = copy.copy(self)
        variant.window_segments = window_segments
        return variant

    def with_dash_style(self, addressing: str | None = None, timeline_style: str | None = None) -> Channel:
        """DASH SegmentTemplate addressing (`number` | `time`) and
        SegmentTimeline style (`full` | `compact`) for this request."""
        addressing = addressing or self.dash_addressing
        timeline_style = timeline_style or self.dash_timeline
        if (addressing, timeline_style) == (self.dash_addressing, self.dash_timeline):
            return self
        variant = copy.copy(self)
        variant.dash_addressing = addressing
        variant.dash_timeline = timeline_style
        return variant

    def with_ll(
        self,
        low_latency: bool | None = None,
        target_latency: float | None = None,
        utc_timing: str | None = None,
        fmt: str = "dash",
    ) -> Channel:
        """Per-request low-latency settings. `low_latency` is whether THIS response (the MPD for fmt "dash", an HLS
        playlist for "hls") advertises it; the target and the clock source are DASH-only. `low_latency=True` needs
        a channel that delivers low latency at all (`--ll-dash` / `--ll-hls` / `--ll-optin`, on a package baked
        with `--chunk-duration`; for HLS also CMAF segments)."""
        if low_latency is None and target_latency is None and utc_timing is None:
            return self
        if low_latency and not self.ll_capable:
            raise TimeshiftError(
                "low latency is not available on this channel (not enabled, or the package was baked without chunks)"
            )
        if low_latency and fmt == "hls" and self.package.hls_format != "cmaf":
            raise TimeshiftError("low-latency HLS needs CMAF segments; this channel's HLS is MPEG-TS")
        variant = copy.copy(self)
        if low_latency is not None and fmt == "hls":
            variant.ll_hls = low_latency
        elif low_latency is not None and fmt == "dash":
            variant.ll_dash = low_latency
        if target_latency is not None:
            variant.ll_target_latency_seconds = target_latency
        if utc_timing is not None:
            variant.ll_utc_timing = utc_timing
        return variant

    def with_ll_utc_timing_url(self, url: str) -> Channel:
        """Low-latency MPD's UTCTiming endpoint for `http` mode, when none was configured."""
        if self.ll_utc_timing_url or not self.ll_dash or self.ll_utc_timing != "http":
            return self
        variant = copy.copy(self)
        variant.ll_utc_timing_url = url
        return variant

    def with_announce(
        self,
        ahead: float | None = None,
        on: tuple[frozenset[int], bool] | None = None,
    ) -> Channel:
        """Announce markers `ahead` seconds before their own segment for this request (0 = off), for the markers
        `on` = (segmentation_type_ids, splice_insert) selects (empty = every marker). None keeps the channel's own."""
        if ahead is None and on is None:
            return self
        variant = copy.copy(self)
        new_on, new_splice = on if on is not None else (self.announce_on, self.announce_splice_insert)
        if ahead is not None:
            variant._set_announce(ahead, new_on, new_splice)
        else:
            variant.announce_on = expand_segmentation_pairs(frozenset(new_on))
            variant.announce_splice_insert = new_splice
        return variant

    def with_split_on(self, type_ids: frozenset[int]) -> Channel:
        """Force a Period / #EXT-X-DISCONTINUITY at markers of these
        segmentation_type_ids for this request (replaces the channel's
        configured set; empty = none)."""
        expanded = expand_segmentation_pairs(frozenset(type_ids))
        if expanded == self.period_on_segmentation:
            return self
        variant = copy.copy(self)
        variant.period_on_segmentation = expanded
        variant.signal_breaks = compute_signal_breaks(
            self.package.markers, self.package.segment_boundary_ticks, expanded
        )
        return variant

    def _require_both_scte35_payloads(self, fmt: str) -> None:
        """`descriptor_grouping` can only switch between the shared and the narrowed
        message when the package stores both (bake.py >= 2.0); otherwise honoring it
        would silently serve the wrong payload, or fail on a missing one."""
        if not getattr(self.package, "has_both_scte35_payloads", True):
            raise TimeshiftError(
                f"this package was baked before it stored both SCTE-35 message forms, so "
                f"descriptor_grouping cannot change the {fmt} payload; re-bake the channel"
            )

    def with_package_overrides(
        self,
        *,
        cue_tags: str | None = None,
        increment_event_ids: bool | None = None,
        id_template: str | None = None,
        hls_grouping: str | None = None,
        dash_grouping: str | None = None,
        signal_format: str | None = None,
        max_height: int | None = None,
        max_bitrate: int | None = None,
    ) -> Channel:
        """This channel with per-request SCTE-35 signaling / ladder settings
        replacing the package's baked defaults. Everything here is derived
        from data the package already carries (markers.json plus both the
        shared and narrowed SCTE-35 payloads), so no bytes change -- only the
        manifests. Raises TimeshiftError for a combination the markers cannot
        support (e.g. `cue_tags=only` with a non-splice_insert marker)."""
        pkg = self.package
        changes: dict = {}
        if cue_tags is not None and cue_tags != pkg.cue_tags:
            if cue_tags == "only":
                bad = non_splice_insert_event_ids(pkg.markers)
                if bad:
                    raise TimeshiftError(
                        f"cue_tags=only requires every marker to be a bare splice_insert, but {bad} are not"
                    )
            changes["cue_tags"] = cue_tags
            changes["cue_breaks"] = build_cue_breaks(pkg.markers) if cue_tags in ("alongside", "only") else []
        if increment_event_ids is not None and increment_event_ids != pkg.increment_event_ids:
            if increment_event_ids:
                problem = event_id_increment_problem(pkg.markers)
                if problem:
                    raise TimeshiftError(problem)
            changes["increment_event_ids"] = increment_event_ids
        if id_template is not None and id_template != pkg.daterange_id_format:
            try:
                validate_daterange_id_format(id_template)
            except ValueError as exc:
                raise TimeshiftError(str(exc)) from None
            changes["daterange_id_format"] = id_template
        if hls_grouping is not None and hls_grouping != pkg.daterange_mode:
            # `shared` and `grouped` carry the same (whole-message) payload, `narrowed` another
            if (hls_grouping == "narrowed") != (pkg.daterange_mode == "narrowed"):
                self._require_both_scte35_payloads("HLS")
            changes["daterange_mode"] = hls_grouping
        if dash_grouping is not None and dash_grouping != pkg.dash_descriptor_mode:
            self._require_both_scte35_payloads("DASH")
            changes["dash_descriptor_mode"] = dash_grouping
        if signal_format is not None and signal_format != pkg.dash_signal_format:
            changes["dash_signal_format"] = signal_format
        if max_height is not None or max_bitrate is not None:
            kept = [
                r for r in pkg.video_renditions
                if (max_height is None or int(r.video_variant["height"]) <= max_height)
                and (max_bitrate is None or int(r.video_variant["bandwidth"]) <= max_bitrate)
            ]
            if not kept:
                raise TimeshiftError(
                    "no rendition satisfies the requested limits; the ladder is "
                    + ", ".join(
                        f"{r.name} ({r.video_variant['height']}p, {r.video_variant['bandwidth']} bps)"
                        for r in pkg.video_renditions
                    )
                )
            if len(kept) != len(pkg.video_renditions):
                changes["video_renditions"] = kept
        if not changes:
            return self
        package = copy.copy(pkg)
        for key, value in changes.items():
            setattr(package, key, value)
        variant = copy.copy(self)
        variant.package = package
        return variant
