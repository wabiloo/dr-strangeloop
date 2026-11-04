"""Per-request overrides: the query parameters of a manifest request -> the Channel variant they select."""

from __future__ import annotations

import math
from decimal import Decimal

from timeshift import (
    ADDRESSING_PARAM,
    ADDRESSING_VALUES,
    CUE_TAGS_PARAM,
    CUE_TAGS_VALUES,
    DASH_DESCRIPTOR_GROUPING_VALUES,
    DESCRIPTOR_GROUPING_PARAM,
    DVR_WINDOW_PARAM,
    EPOCH_PARAM,
    HLS_DESCRIPTOR_GROUPING_VALUES,
    ANNOUNCE_AHEAD_PARAM,
    ANNOUNCE_ON_PARAM,
    ID_TEMPLATE_PARAM,
    INCREMENT_EVENT_IDS_PARAM,
    LL_MAX_TARGET_LATENCY_SECONDS,
    LL_TARGET_LATENCY_PARAM,
    LL_UTC_TIMING_PARAM,
    LL_UTC_TIMING_VALUES,
    LOW_LATENCY_PARAM,
    MAX_BITRATE_PARAM,
    MAX_HEIGHT_PARAM,
    OFFSET_PARAM,
    SIGNAL_FORMAT_PARAM,
    SIGNAL_FORMAT_VALUES,
    SPLIT_ON_PARAM,
    TIMELINE_PARAM,
    TIMELINE_STYLE_PARAM,
    TIMELINE_STYLE_VALUES,
    TimeshiftConfig,
    TimeshiftError,
    parse_announce_ahead,
    parse_announce_on,
    parse_bool,
    parse_choice,
    parse_duration_seconds,
    parse_instant_ticks,
    parse_offset_ticks,
    parse_positive_int,
    parse_split_on,
    parse_timeline,
)

from serving.channel import Channel


def resolve_request_channel(channel: Channel, args, cfg: TimeshiftConfig, fmt: str = "hls") -> Channel:
    """The per-request `Channel` variant selected by the query parameters in
    `args` (any mapping with `.get`). `fmt` is the format being answered --
    `hls`, `dash` or `json` (/timeline.json) -- because some settings differ
    per format (`descriptor_grouping` has no `grouped` value for DASH).
    Every parameter is optional; an absent one keeps the channel's own
    setting. Each kind is read only while its switch is on: `offset` (startover/
    catchup's clock) with `cfg.timeshift`, every other parameter here with
    `cfg.overrides`; the rest is ignored. Raises TimeshiftError (HTTP 400) on
    any bad value."""
    if fmt not in ("hls", "dash", "json"):
        raise ValueError(f"unknown format {fmt!r}")

    def arg(base: str) -> str | None:
        return args.get(cfg.name(base))

    def name(base: str) -> str:
        return cfg.name(base)

    ch = channel
    ts = channel.package.timescale
    overrides = cfg.overrides
    raw = arg(TIMELINE_PARAM) if overrides else None
    if raw is not None:
        want_continuous = parse_timeline(raw, name(TIMELINE_PARAM))
        if want_continuous is not None:
            ch = ch.for_mode(want_continuous)
    raw = arg(EPOCH_PARAM) if overrides else None
    if raw is not None:
        ch = ch.with_epoch(parse_instant_ticks(raw, ts, name(EPOCH_PARAM)))
    raw = arg(OFFSET_PARAM) if cfg.timeshift else None  # startover/catchup's clock
    if raw is not None:
        ch = ch.with_offset(parse_offset_ticks(raw, ts, name(OFFSET_PARAM)))
    ch.check_clock()
    if not overrides:
        return ch

    raw = arg(DVR_WINDOW_PARAM)
    if raw is not None:
        seconds = parse_duration_seconds(raw, name(DVR_WINDOW_PARAM))
        if seconds > cfg.max_span_seconds:
            raise TimeshiftError(f"'{name(DVR_WINDOW_PARAM)}' is limited to {cfg.max_span_seconds} seconds")
        nominal = Decimal(str(ch.package.segment_duration_seconds))
        ch = ch.with_window_segments(max(1, math.ceil(seconds / nominal)))

    raw_addressing, raw_style = arg(ADDRESSING_PARAM), arg(TIMELINE_STYLE_PARAM)
    addressing = parse_choice(raw_addressing, name(ADDRESSING_PARAM), ADDRESSING_VALUES) if raw_addressing is not None else None
    style = parse_choice(raw_style, name(TIMELINE_STYLE_PARAM), TIMELINE_STYLE_VALUES) if raw_style is not None else None
    ch = ch.with_dash_style(addressing, style)

    raw = arg(SPLIT_ON_PARAM)
    if raw is not None:
        ch = ch.with_split_on(parse_split_on(raw, name(SPLIT_ON_PARAM)))

    raw_ahead, raw_on = arg(ANNOUNCE_AHEAD_PARAM), arg(ANNOUNCE_ON_PARAM)
    if raw_ahead is not None or raw_on is not None:
        ch = ch.with_announce(
            float(parse_announce_ahead(raw_ahead, name(ANNOUNCE_AHEAD_PARAM))) if raw_ahead is not None else None,
            parse_announce_on(raw_on, name(ANNOUNCE_ON_PARAM)) if raw_on is not None else None,
        )

    raw_ll, raw_target, raw_clock = arg(LOW_LATENCY_PARAM), arg(LL_TARGET_LATENCY_PARAM), arg(LL_UTC_TIMING_PARAM)
    target = None
    if raw_target is not None:
        target = float(parse_duration_seconds(raw_target, name(LL_TARGET_LATENCY_PARAM)))
        if target > LL_MAX_TARGET_LATENCY_SECONDS:
            raise TimeshiftError(f"'{name(LL_TARGET_LATENCY_PARAM)}' is limited to {LL_MAX_TARGET_LATENCY_SECONDS} seconds")
    ch = ch.with_ll(
        parse_bool(raw_ll, name(LOW_LATENCY_PARAM)) if raw_ll is not None else None,
        target,
        parse_choice(raw_clock, name(LL_UTC_TIMING_PARAM), LL_UTC_TIMING_VALUES) if raw_clock is not None else None,
        fmt=fmt,
    )

    overrides: dict = {}
    raw = arg(CUE_TAGS_PARAM)
    if raw is not None:
        overrides["cue_tags"] = parse_choice(raw, name(CUE_TAGS_PARAM), CUE_TAGS_VALUES)
    raw = arg(INCREMENT_EVENT_IDS_PARAM)
    if raw is not None:
        overrides["increment_event_ids"] = parse_bool(raw, name(INCREMENT_EVENT_IDS_PARAM))
    raw = arg(ID_TEMPLATE_PARAM)
    if raw is not None:
        overrides["id_template"] = raw
    raw = arg(SIGNAL_FORMAT_PARAM)
    if raw is not None:
        overrides["signal_format"] = parse_choice(raw, name(SIGNAL_FORMAT_PARAM), SIGNAL_FORMAT_VALUES)
    raw = arg(DESCRIPTOR_GROUPING_PARAM)
    if raw is not None:
        if fmt == "dash":
            overrides["dash_grouping"] = parse_choice(raw, name(DESCRIPTOR_GROUPING_PARAM), DASH_DESCRIPTOR_GROUPING_VALUES)
        else:
            overrides["hls_grouping"] = parse_choice(raw, name(DESCRIPTOR_GROUPING_PARAM), HLS_DESCRIPTOR_GROUPING_VALUES)
    raw = arg(MAX_HEIGHT_PARAM)
    if raw is not None:
        overrides["max_height"] = parse_positive_int(raw, name(MAX_HEIGHT_PARAM))
    raw = arg(MAX_BITRATE_PARAM)
    if raw is not None:
        overrides["max_bitrate"] = parse_positive_int(raw, name(MAX_BITRATE_PARAM))
    return ch.with_package_overrides(**overrides)
