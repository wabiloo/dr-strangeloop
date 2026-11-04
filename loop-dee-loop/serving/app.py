"""The Flask application: a ServeContext built from the options, then the routes registered on it."""

from __future__ import annotations

from pathlib import Path

from flask import Flask
from timeshift import TimeshiftConfig

from serving.context import ServeContext
from serving.routes import register_all


def create_app(
    package_dir: Path,
    epoch_ticks: int,
    window_segments: int = 6,
    continuous: bool = False,
    timeshift: TimeshiftConfig | None = None,
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
) -> Flask:
    ctx = ServeContext(
        package_dir=package_dir,
        epoch_ticks=epoch_ticks,
        window_segments=window_segments,
        continuous=continuous,
        timeshift=timeshift,
        period_on_segmentation=period_on_segmentation,
        channel_name=channel_name,
        dash_addressing=dash_addressing,
        dash_timeline=dash_timeline,
        ll_dash=ll_dash,
        ll_target_latency=ll_target_latency,
        ll_utc_timing_url=ll_utc_timing_url,
        ll_utc_timing=ll_utc_timing,
        ll_optin=ll_optin,
        ll_hls=ll_hls,
        announce_ahead=announce_ahead,
        announce_on=announce_on,
        announce_splice_insert=announce_splice_insert,
    )
    app = Flask(__name__)
    register_all(app, ctx)
    return app

