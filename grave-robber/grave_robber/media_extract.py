"""Media body extraction, best-effort (SCOPE.md §5.3). For each
`TimingSegment.source_uri`, look it up directly in the archive
(`trace.get_entries_for_url`) and take the first matching entry's
`TraceEntry.content_bytes`, writing it to disk under this tool's control.
No match -> `None` (expected, not an error -- SCOPE.md §1's revised
stance: this tool's timing/markers never depend on real segment bytes
being present)."""

from __future__ import annotations

from pathlib import Path

from trace_shrink import Trace

from .models import TimingSegment


def _pick_best_entry(entries: list):
    """Duplicate entries for the same URL (a segment re-requested across
    manifest refreshes, or on the live edge) are resolved by picking the
    response with the largest `raw_size`/most complete body -- a live-edge
    capture sometimes catches a segment mid-download on its first request
    and completes it on a later one (SCOPE.md §5.3)."""

    def _size(entry) -> int:
        raw_size = entry.response.body.raw_size
        if raw_size is not None:
            return raw_size
        return len(entry.content_bytes)

    return max(entries, key=_size)


def _body(trace: Trace, uri: str | None) -> bytes:
    """Best-matching non-empty body for `uri`, or b"" (never raises)."""
    if uri is None:
        return b""
    entries = trace.get_entries_for_url(uri)
    return _pick_best_entry(entries).content_bytes if entries else b""


def has_media(trace: Trace, source_uri: str | None, init_uri: str | None = None) -> bool:
    """Whether `extract_media_for_segments` would recover a usable file for
    this segment (same lookup, without writing anything). A CMAF segment
    (`init_uri` set) is only usable if its init segment is in the archive too."""
    if not _body(trace, source_uri):
        return False
    return init_uri is None or bool(_body(trace, init_uri))


def extract_media_for_segments(
    trace: Trace,
    segments: list[TimingSegment],
    output_dir: Path,
    *,
    filename_template: str = "seg_{index:06d}.bin",
) -> dict[int, Path | None]:
    """For every segment with a `source_uri`, look it up in `trace` and
    write the best-matching entry's bytes to `output_dir`. Returns
    {segment_index: path_or_None}, one entry per input segment (None for
    no match, no source_uri, or an empty body -- never a KeyError/crash).

    Byte-level validation is intentionally NOT performed here (no demux/
    probe of the segment content) -- SCOPE.md §5.3: "matches this tool's
    whole posture of trusting manifest text over media inspection."
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    media_paths: dict[int, Path | None] = {}

    for segment in segments:
        if segment.source_uri is None:
            media_paths[segment.index] = None
            continue

        content = _body(trace, segment.source_uri)
        if not content:
            media_paths[segment.index] = None
            continue

        if segment.init_uri is not None:
            # fMP4/CMAF media segments have no moov of their own: prepend the
            # init segment so each file is a complete, independently
            # decodable fragmented MP4. No init in the archive -> the
            # segment is unrecoverable (media_file: null).
            init = _body(trace, segment.init_uri)
            if not init:
                media_paths[segment.index] = None
                continue
            content = init + content

        dest = output_dir / filename_template.format(index=segment.index)
        dest.write_bytes(content)
        media_paths[segment.index] = dest

    return media_paths
