"""`[timeline] epoch_utc` channel config -> loop-dee-loop serve.py's
`--epoch-utc` (loop 0's start, and the DASH availabilityStartTime).

The epoch only needs to be a stable instant in the past: looping content
simulating live has no real broadcast start. A recent one keeps loop numbers
(and the media sequence / Period ids derived from them) small.
"""

from __future__ import annotations

import datetime

DEFAULT_EPOCH_UTC = "2026-01-01T00:00:00Z"
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def validate_epoch_utc(raw: str) -> str:
    """`raw` unchanged if it is an ISO 8601 UTC timestamp of the exact form
    serve.py's --epoch-utc accepts (2026-01-01T00:00:00Z), else ValueError."""
    try:
        datetime.datetime.strptime(raw, _FORMAT)
    except (TypeError, ValueError):
        raise ValueError(
            f"epoch_utc {raw!r} must be an ISO8601 UTC timestamp like {DEFAULT_EPOCH_UTC}"
        ) from None
    return raw


def config_epoch_utc(cfg: dict) -> str:
    """The channel's configured `[timeline] epoch_utc`, else the default."""
    raw = cfg.get("timeline", {}).get("epoch_utc")
    return DEFAULT_EPOCH_UTC if raw is None else validate_epoch_utc(raw)
