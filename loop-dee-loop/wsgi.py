"""Gunicorn entrypoint for serve.py's Flask app (see PERFS.md: the built-in
Flask dev server is single-threaded and not production-grade -- it
serializes concurrent requests). Used only by docker-entrypoint.sh's
production path (LOOP_PACKAGE_S3_URI set); local/dev usage still runs
`python3 serve.py ...` directly via its own argparse CLI, unaffected.

Configuration arrives as env vars, set by docker-entrypoint.sh from the
same flags serve.py's CLI accepts, since gunicorn imports this module
(`wsgi:app`) rather than invoking serve.py's argparse-based main().
"""

import os
from pathlib import Path

from serve import (
    TimeshiftConfig,
    create_app,
    parse_segmentation_type_ids,
    read_package_descriptor,
    resolve_epoch_ticks,
    resolve_window_segments,
)

package_dir = Path(os.environ["LOOP_PACKAGE_DIR"])

# aws-lambda backend (its-a-live/LAMBDA_BACKEND.md): the package stays in S3;
# build a local skeleton and fetch segment bytes on demand. Everything below
# is then identical to the ECS path.
if os.environ.get("LOOP_PACKAGE_STORE", "").lower() == "s3":
    from s3_package import S3Package

    _s3 = S3Package(
        os.environ["LOOP_PACKAGE_S3_URI"],
        package_dir,
        cache_bytes=int(os.environ.get("SEGMENT_CACHE_MB") or 512) * 1024 * 1024,
    )
    _s3.materialize()
    _s3.install()

descriptor = read_package_descriptor(package_dir)

epoch_ticks = resolve_epoch_ticks(os.environ["EPOCH_UTC"], int(descriptor["timescale"]))

_window_segments_env = os.environ.get("WINDOW_SEGMENTS", "")
window_segments = resolve_window_segments(
    descriptor,
    dvr_window_seconds=float(os.environ.get("DVR_WINDOW_SECONDS") or 30.0),
    window_segments=int(_window_segments_env) if _window_segments_env else None,
)

continuous = os.environ.get("CONTINUOUS_TIMELINE", "").lower() in ("1", "true", "yes")

timeshift = TimeshiftConfig(
    timeshift=os.environ.get("TIMESHIFT", "").lower() in ("1", "true", "yes"),
    overrides=os.environ.get("QUERY_OVERRIDES", "").lower() in ("1", "true", "yes"),
    start_param=os.environ.get("TIMESHIFT_START_PARAM") or "start",
    end_param=os.environ.get("TIMESHIFT_END_PARAM") or "end",
    max_span_seconds=int(os.environ.get("TIMESHIFT_MAX_SPAN_SECONDS") or 21600),
    prefix=os.environ.get("QUERY_PREFIX", ""),
)

period_on_segmentation = parse_segmentation_type_ids(os.environ.get("PERIOD_ON_SEGMENTATION"))

app = create_app(
    package_dir,
    epoch_ticks,
    window_segments=window_segments,
    continuous=continuous,
    timeshift=timeshift,
    period_on_segmentation=period_on_segmentation,
    channel_name=os.environ.get("CHANNEL_NAME", ""),
    dash_addressing=os.environ.get("DASH_ADDRESSING") or "number",
    dash_timeline=os.environ.get("DASH_TIMELINE") or "full",
)
