"""Serve phase entrypoint (SCOPE.md §4.2). Long-running, stateless per
request -- no in-memory "current position" variable that a timer advances.

    LINT / CODE-REVIEW CHECKLIST ITEM (SCOPE.md §5):
    No persisted or accumulated timing state in this module may be a Python
    `float`. Every persisted tick value is an `int`. Floats appear only as a
    final, one-shot conversion for display/serialization of a single
    response (e.g. an ISO8601 timestamp string) -- never fed back into a
    subsequent computation.

Every request independently derives the current position from `now()`
versus a fixed channel epoch via loop_math.compute_loop_position. Segment
bytes are served completely unmodified (same bytes every loop); only
manifest text differs per request.

Multi-rendition (ABR ladder): `loop_descriptor.json` (v2) carries a list of
`video_renditions`, each independently baked (bake.py) but sharing the same
`segments_per_loop` count and the same marker/timeline data. Exactly one
rendition also carries audio (see bake.py's `include_audio`) -- audio is
never duplicated per video rendition.
"""

from __future__ import annotations

# Everything this module used to define stays importable from here (tests, wsgi.py, the CLI):
# the code lives in the serving/ package (see loop-dee-loop/AGENTS.md, "Module layout").
from serving.app import create_app  # noqa: F401
from serving.channel import Channel  # noqa: F401
from serving.cli import main  # noqa: F401
from serving.constants import DASH_ADDRESSING_CHOICES, DASH_TIMELINE_CHOICES, GENERATOR_COMMENT, GENERATOR_VERSION, LL_HLS_PART_SEGMENTS, LL_UTC_TIMING_CHOICES, _DOCS_HTML, _read_generator_version  # noqa: F401
from serving.dash_render import _dash_frame_rate, _join_events, render_audio_adaptation_set, render_segment_timeline, render_video_representations  # noqa: F401
from serving.hls import _HlsLL, _part_uri  # noqa: F401
from serving.httputil import _is_loopback_host, _send_package_file  # noqa: F401
from serving.markers import _asset_ids_starting_in_segment, _dash_event_comment, _describe_markers, _hls_payload_markers, _marker_covers_segment, _marker_durations_ticks, _marker_type_code, _remap_signaling_markers  # noqa: F401
from serving.package import LoopPackage, VideoRendition, _cmaf_tfdt_version, _first_present_segment, _numeric_segment_index  # noqa: F401
from serving.query import resolve_request_channel  # noqa: F401
from serving.startup import read_package_descriptor, resolve_epoch_ticks, resolve_window_segments  # noqa: F401
from serving.timeline import WindowRange, compute_asset_boundary_set, compute_declared_offset_ticks_by_local_index, compute_discontinuity_sequence, compute_signal_breaks, expand_segmentation_pairs, parse_segmentation_type_ids  # noqa: F401


if __name__ == "__main__":
    raise SystemExit(main())
