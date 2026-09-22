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

from serve import create_app, read_package_descriptor, resolve_epoch_ticks, resolve_window_segments

package_dir = Path(os.environ["LOOP_PACKAGE_DIR"])
descriptor = read_package_descriptor(package_dir)

epoch_ticks = resolve_epoch_ticks(os.environ["EPOCH_UTC"], int(descriptor["timescale"]))

_window_segments_env = os.environ.get("WINDOW_SEGMENTS", "")
window_segments = resolve_window_segments(
    descriptor,
    dvr_window_seconds=float(os.environ.get("DVR_WINDOW_SECONDS") or 30.0),
    window_segments=int(_window_segments_env) if _window_segments_env else None,
)

app = create_app(package_dir, epoch_ticks, window_segments=window_segments)
