"""Startover / catchup range resolution (SCOPE.md §13).

Pure integer-tick helpers -- no Flask, no I/O -- so the drift-freedom rule
(loop_math.py: no floats in timing state) holds here too. Seconds given as
a decimal string are converted with `Decimal`/`Fraction`, never a float.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal

from loop_math import compute_loop_position, global_segment_number, segment_index_for_position

# Numeric instants above this are milliseconds, below are seconds
# (1e11 s is year 5138; 1e11 ms is 1973).
_MS_THRESHOLD = 10**11
_NUMERIC = re.compile(r"^\d+(\.\d+)?$")
# Fixed (not configurable) names of the per-request query parameters. Only
# `start`/`end` are configurable (TimeshiftConfig); every name can be given a
# common prefix (TimeshiftConfig.prefix) so a caller that already uses some of
# these names for its own purposes (an SSAI service, say) cannot clash.
# `timeline_mode`: `default` = the server's --continuous-timeline setting.
TIMELINE_PARAM = "timeline_mode"
# `full_loops=true` widens the range to whole loops.
FULL_LOOPS_PARAM = "full_loops"
# `offset=` pretends "now" is earlier (negative) or later (positive).
OFFSET_PARAM = "offset"
# `epoch=` replaces the channel epoch (loop 0's start) for this request.
EPOCH_PARAM = "epoch"
# `dvr_window=` sets the size of the sliding window (seconds or ISO 8601 duration).
DVR_WINDOW_PARAM = "dvr_window"
# DASH manifest style.
TIMELINE_STYLE_PARAM = "timeline_style"
ADDRESSING_PARAM = "addressing"
# SCTE-35 signaling.
CUE_TAGS_PARAM = "cue_tags"
DESCRIPTOR_GROUPING_PARAM = "descriptor_grouping"
INCREMENT_EVENT_IDS_PARAM = "increment_event_ids"
SIGNAL_FORMAT_PARAM = "signal_format"
ID_TEMPLATE_PARAM = "id_template"
SPLIT_ON_PARAM = "split_on"
# Ladder filtering.
MAX_HEIGHT_PARAM = "max_height"
MAX_BITRATE_PARAM = "max_bitrate"
# Every fixed-name parameter, in documentation order.
FIXED_PARAMS = (
    FULL_LOOPS_PARAM, TIMELINE_PARAM, OFFSET_PARAM, EPOCH_PARAM, DVR_WINDOW_PARAM,
    TIMELINE_STYLE_PARAM, ADDRESSING_PARAM, CUE_TAGS_PARAM, DESCRIPTOR_GROUPING_PARAM,
    INCREMENT_EVENT_IDS_PARAM, SIGNAL_FORMAT_PARAM, ID_TEMPLATE_PARAM, SPLIT_ON_PARAM,
    MAX_HEIGHT_PARAM, MAX_BITRATE_PARAM,
)
# The two kinds, each behind its own TOML-only switch: startover/catchup
# (`start`/`end` plus these two) behind `timeshift`, and the overrides of how a
# manifest is rendered (everything else) behind `overrides`.
TIMESHIFT_FIXED_PARAMS = (FULL_LOOPS_PARAM, OFFSET_PARAM)
OVERRIDE_PARAMS = tuple(p for p in FIXED_PARAMS if p not in TIMESHIFT_FIXED_PARAMS)
# Sanity bound so absurd values fail cleanly instead of overflowing datetimes.
MAX_ABS_OFFSET_SECONDS = 10 * 365 * 86400
TIMELINE_VALUES = ("default", "continuous", "periodic")
TIMELINE_STYLE_VALUES = ("full", "compact")
ADDRESSING_VALUES = ("number", "time")
CUE_TAGS_VALUES = ("none", "alongside", "only")
HLS_DESCRIPTOR_GROUPING_VALUES = ("shared", "narrowed", "grouped")
DASH_DESCRIPTOR_GROUPING_VALUES = ("shared", "narrowed")
SIGNAL_FORMAT_VALUES = ("binary", "xml")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_PREFIX = re.compile(r"^[A-Za-z0-9_.~-]*$")


class TimeshiftError(ValueError):
    """A bad startover/catchup/override request (maps to HTTP 400)."""


@dataclass(frozen=True)
class TimeshiftConfig:
    """Which query parameters are honoured and how they are named.

    Two independent switches (TOML only -- they gate the parameters, so they
    are not parameters themselves):

    - `timeshift`: startover/catchup (time-shifted playback) -- `start`, `end`,
      `full_loops`, `offset`;
    - `overrides`: the per-request overrides of how a manifest is rendered
      (`OVERRIDE_PARAMS`).

    A switch that is off means its parameters are ignored (not rejected) and
    not forwarded to child playlists; with both off the query string is inert.
    `prefix` is prepended to every name -- including the configurable
    `start_param`/`end_param`."""

    timeshift: bool = False
    overrides: bool = False
    start_param: str = "start"
    end_param: str = "end"
    max_span_seconds: int = 21600
    prefix: str = ""

    def __post_init__(self):
        if not _PREFIX.match(self.prefix):
            raise ValueError(f"query parameter prefix must be URL-safe (letters, digits, _ . ~ -), got {self.prefix!r}")
        names = self.all_param_names
        if any(not n for n in (self.start_param, self.end_param)) or len(set(names)) != len(names):
            raise ValueError(f"query parameter names must be non-empty and distinct: {names}")
        if self.max_span_seconds < 1:
            raise ValueError("max_span_seconds must be >= 1")

    def name(self, base: str) -> str:
        """The on-the-wire name of the parameter `base` (prefix applied)."""
        return f"{self.prefix}{base}"

    @property
    def start_name(self) -> str:
        return self.name(self.start_param)

    @property
    def end_name(self) -> str:
        return self.name(self.end_param)

    @property
    def active(self) -> bool:
        """Whether any query parameter is honoured at all (either switch on)."""
        return self.timeshift or self.overrides

    @property
    def all_param_names(self) -> tuple[str, ...]:
        """Every name, whether or not the overrides are enabled (used to check they are distinct)."""
        return tuple(self.name(n) for n in (self.start_param, self.end_param, *FIXED_PARAMS))

    @property
    def param_names(self) -> tuple[str, ...]:
        """Every on-the-wire name a manifest request may carry, i.e. what a
        CDN must key manifest caching on and what child playlists inherit."""
        names = [self.start_param, self.end_param] if self.timeshift else []
        names += [
            p for p in FIXED_PARAMS
            if (self.timeshift if p in TIMESHIFT_FIXED_PARAMS else self.overrides)
        ]
        return tuple(self.name(n) for n in names)


def parse_bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise TimeshiftError(f"'{name}' must be a boolean (true/false/1/0), got {value!r}")


def parse_timeline(value: str, name: str = TIMELINE_PARAM) -> bool | None:
    """`timeline=` -> True (continuous), False (periodic) or None (use the
    server default). Case-insensitive; anything else is a 400."""
    v = value.strip().lower()
    if v == "default":
        return None
    if v == "continuous":
        return True
    if v == "periodic":
        return False
    raise TimeshiftError(f"'{name}' must be one of {', '.join(TIMELINE_VALUES)}, got {value!r}")


_SIGNED_SECONDS = re.compile(r"^[+-]?\d+(\.\d+)?$")
_ISO_DURATION = re.compile(
    r"^([+-])?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$", re.IGNORECASE
)


def parse_offset_ticks(value: str, timescale: int, name: str = OFFSET_PARAM) -> int:
    """`offset=` -> signed integer ticks. Signed seconds (`-3600`, `90.5`) or
    an ISO 8601 duration with optional sign (`-PT1H`, `P1DT2H30M`; days and
    below only -- months/years have no fixed length). A leading `+` may
    arrive as a space (URL decoding), which `strip()` removes."""
    raw = value.strip()
    sign = 1
    if _SIGNED_SECONDS.match(raw):
        seconds = Decimal(raw)
    else:
        m = _ISO_DURATION.match(raw)
        if not m or not any(m.group(i) for i in range(2, 6)):
            raise TimeshiftError(
                f"'{name}' must be signed seconds (-3600) or an ISO 8601 duration (-PT1H), got {value!r}"
            )
        sign = -1 if m.group(1) == "-" else 1
        d, h, mi, s = (Decimal(g) if g else Decimal(0) for g in m.groups()[1:])
        seconds = sign * (d * 86400 + h * 3600 + mi * 60 + s)
    if abs(seconds) > MAX_ABS_OFFSET_SECONDS:
        raise TimeshiftError(f"'{name}' is limited to +/-{MAX_ABS_OFFSET_SECONDS} seconds")
    return int((seconds * timescale).to_integral_value())


def parse_choice(value: str, name: str, choices: tuple[str, ...]) -> str:
    """One of `choices`, case-insensitive; anything else is a 400."""
    v = value.strip().lower()
    if v not in choices:
        raise TimeshiftError(f"'{name}' must be one of {', '.join(choices)}, got {value!r}")
    return v


def parse_positive_int(value: str, name: str) -> int:
    raw = value.strip()
    if not raw.isdigit() or int(raw) < 1:
        raise TimeshiftError(f"'{name}' must be a positive integer, got {value!r}")
    return int(raw)


def parse_duration_seconds(value: str, name: str) -> Decimal:
    """A positive length of time: plain seconds (`30`, `7.5`) or an ISO 8601
    duration (`PT30S`, `PT1H30M`, `P1D`; days and below only)."""
    raw = value.strip()
    if _NUMERIC.match(raw):
        seconds = Decimal(raw)
    else:
        m = _ISO_DURATION.match(raw)
        if not m or m.group(1) or not any(m.group(i) for i in range(2, 6)):
            raise TimeshiftError(
                f"'{name}' must be a number of seconds (30) or an ISO 8601 duration (PT30S), got {value!r}"
            )
        d, h, mi, s = (Decimal(g) if g else Decimal(0) for g in m.groups()[1:])
        seconds = d * 86400 + h * 3600 + mi * 60 + s
    if seconds <= 0:
        raise TimeshiftError(f"'{name}' must be greater than zero, got {value!r}")
    return seconds


def parse_split_on(value: str, name: str) -> frozenset[int]:
    """`split_on=0x22,0x30` -> segmentation_type_ids; empty or `none` -> none."""
    ids = set()
    for token in value.split(","):
        token = token.strip()
        if not token or token.lower() == "none":
            continue
        try:
            type_id = int(token, 0)
        except ValueError:
            raise TimeshiftError(f"'{name}' must be a comma-separated list of segmentation_type_ids (e.g. 0x22,0x30) or none, got {value!r}") from None
        if not 0 <= type_id <= 0xFF:
            raise TimeshiftError(f"'{name}': segmentation_type_id {token!r} is out of range 0..255")
        ids.add(type_id)
    return frozenset(ids)


def parse_instant_ticks(value: str, timescale: int, name: str = "time") -> int:
    """Epoch seconds, epoch milliseconds (value >= 1e11) or ISO8601 -> an
    integer tick count since the Unix epoch. A naive ISO8601 string is UTC."""
    raw = value.strip()
    if _NUMERIC.match(raw):
        number = Decimal(raw)
        if number >= _MS_THRESHOLD:
            number = number / 1000
        return int((number * timescale).to_integral_value())
    try:
        dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise TimeshiftError(
            f"'{name}' must be epoch seconds/milliseconds or ISO8601, got {value!r}"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    delta = dt - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return (micros * timescale + 500_000) // 1_000_000


@dataclass(frozen=True)
class TimeWindow:
    """A resolved, segment-snapped startover/catchup range (inclusive of
    both global indices). `ended` -> the whole range is in the past, so the
    manifest is a finished VOD; otherwise it is still growing."""

    first_global: int
    last_global: int
    ended: bool
    origin_loop: int  # loop number containing first_global (rseg origin)


def global_index_at(
    abs_ticks: int,
    epoch_ticks: int,
    total_loop_duration_ticks: int,
    segment_boundary_ticks: list[int],
    segments_per_loop: int,
) -> int:
    pos = compute_loop_position(abs_ticks, epoch_ticks, total_loop_duration_ticks)
    seg = segment_index_for_position(pos.position_in_loop_ticks, segment_boundary_ticks)
    return global_segment_number(pos.loop_number, seg, segments_per_loop)


def resolve_window(
    *,
    start_ticks: int,
    end_ticks: int | None,
    now_ticks: int,
    epoch_ticks: int,
    total_loop_duration_ticks: int,
    segment_boundary_ticks: list[int],
    segments_per_loop: int,
    max_span_ticks: int,
    full_loop: bool = False,
) -> TimeWindow:
    D = total_loop_duration_ticks
    if start_ticks < epoch_ticks:
        raise TimeshiftError("start is before the channel epoch")
    if start_ticks > now_ticks:
        raise TimeshiftError("start is in the future")
    if end_ticks is not None and end_ticks <= start_ticks:
        raise TimeshiftError("end must be after start")

    if full_loop:
        start_ticks = epoch_ticks + ((start_ticks - epoch_ticks) // D) * D
        if end_ticks is not None:
            end_ticks = epoch_ticks + -(-(end_ticks - epoch_ticks) // D) * D
    if end_ticks is None:
        end_ticks = start_ticks + max_span_ticks
        if full_loop:
            loops = max_span_ticks // D
            if loops < 1:
                raise TimeshiftError("a single loop is longer than the maximum span")
            end_ticks = start_ticks + loops * D
    if end_ticks - start_ticks > max_span_ticks:
        raise TimeshiftError(
            f"requested range is {(end_ticks - start_ticks) // 90000}s (after snapping), "
            f"longer than the maximum span of {max_span_ticks // 90000}s"
        )

    def at(t: int) -> int:
        return global_index_at(t, epoch_ticks, D, segment_boundary_ticks, segments_per_loop)

    first = at(start_ticks)
    end_global = at(end_ticks - 1)  # segment containing the last tick of the range
    now_global = at(now_ticks)
    return TimeWindow(
        first_global=first,
        last_global=min(end_global, now_global),
        ended=end_ticks <= now_ticks,
        origin_loop=first // segments_per_loop,
    )
