"""grave-robber: derive loop-dee-loop timing + SCTE-35 markers from a
captured HTTP archive (HAR/Proxyman log) of a real HLS/DASH session.

See SCOPE.md for the full design. Pipeline (SCOPE.md §4):

    HAR/Proxyman archive
            |  trace-shrink: open_trace -> Trace -> ManifestStream
            v
      per-format extractor        HLS: m3u8   /   DASH: mpd-inspector
            |
            v
      TimingSegment[] + RawMarker[]                      (this module, §5)
            |
            v
      SCTE-35 binary decode (threefive)                   (§5.2)
            |
            v
      multi-variant coverage + selection + filter          (§8, HLS only)
            |
            v
      media body extraction, best-effort                   (§5.3)
            |
            v
      segment-list manifest + markers.json-shaped list
            |    (loop-dee-loop's new bake.py sparse input mode, SCOPE.md §11)
            v
      scte35_signaling.markers_to_signaling() / loop_math.py
"""

from .models import AssetBoundary, AssetSpan, RawMarker, TimingSegment

__all__ = ["TimingSegment", "AssetSpan", "AssetBoundary", "RawMarker"]

TIMESCALE = 90_000
