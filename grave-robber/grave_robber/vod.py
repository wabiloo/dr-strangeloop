"""Ingest a VOD manifest URL (HLS or DASH) instead of a captured archive.

Same output as `pipeline.ingest` -- a segment-list manifest (+ downloaded
`media/`) for loop-dee-loop's `bake.py` -- but the manifest and every segment
are fetched over HTTP, and a multivariant playlist / MPD becomes a rendition
ladder rather than one reference variant.

A VOD ladder is segment-aligned (every rendition has the same segments), so
the timeline, asset boundaries and markers are read off the reference
(highest-bandwidth) rendition once and every other rendition only contributes
its segment bytes. `check_alignment` hard-fails on a ladder that isn't aligned:
`bake.py`/`serve.py` share one segment ledger across renditions.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import m3u8

from .audio import align_audio_segments, align_audio_segments_by_ticks
from .extract_dash import dash_is_static, extract_dash, has_dash_audio, list_dash_representations
from .extract_hls import extract_hls
from .manifest_writer import build_ladder_segment_list_manifest, write_segment_list_manifest
from .models import TimingSegment
from .multivariant import is_multivariant_playlist
from .scte35_decode import decode_marker

logger = logging.getLogger(__name__)

# (url, byte_range=(offset, length) | None) -> body
Fetch = Callable[[str, "tuple[int, int] | None"], bytes]

# Durations of aligned renditions may differ by container rounding (EXTINF is
# decimal seconds); 10 ms at 90 kHz.
ALIGNMENT_TOLERANCE_TICKS = 900


class VodIngestError(ValueError):
    pass


def http_fetch(url: str, byte_range: tuple[int, int] | None = None, *, retries: int = 3, timeout: float = 30) -> bytes:
    headers = {"User-Agent": "grave-robber"}
    if byte_range is not None:
        offset, length = byte_range
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 -- retried, then re-raised below
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise VodIngestError(f"GET {url} failed: {last_error}")


def _text(fetch: Fetch, url: str) -> str:
    return fetch(url, None).decode("utf-8", errors="replace")


# ── variant selection ────────────────────────────────────────────────────


def _rendition_name(variant: dict, position: int, taken: set[str]) -> str:
    resolution = variant.get("resolution") or ""
    height = resolution.split("x")[-1] if "x" in resolution else ""
    base = f"{height}p" if height else f"v{position + 1}"
    name = base
    if name in taken and variant.get("bandwidth"):
        name = f"{base}_{variant['bandwidth'] // 1000}k"
    while name in taken:
        name += "_"
    taken.add(name)
    return name


def select_variants(variants: list[dict], selector: str | None) -> list[dict]:
    """`selector`: None/"all" keeps every variant; otherwise a comma-separated list
    of heights (`720,360` or `720p`), 1-based positions in bandwidth-descending
    order (`#1,#3`), or `best`. Result is best-bandwidth first."""
    ordered = sorted(variants, key=lambda v: v.get("bandwidth") or 0, reverse=True)
    if selector is None or selector.strip().lower() == "all":
        return ordered
    if selector.strip().lower() == "best":
        return ordered[:1]
    chosen: list[dict] = []
    for token in (t.strip() for t in selector.split(",") if t.strip()):
        if token.startswith("#"):
            matches = [ordered[int(token[1:]) - 1]] if token[1:].isdigit() and 0 < int(token[1:]) <= len(ordered) else []
        else:
            height = token.rstrip("pP")
            matches = [v for v in ordered if (v.get("resolution") or "").endswith(f"x{height}")]
        if not matches:
            raise VodIngestError(
                f"--renditions {token!r} matches none of: "
                + ", ".join(f"{v.get('resolution') or '?'}@{v.get('bandwidth')}" for v in ordered)
            )
        chosen.extend(m for m in matches if m not in chosen)
    return sorted(chosen, key=lambda v: ordered.index(v))


# ── alignment ────────────────────────────────────────────────────────────


def check_alignment(reference: list[TimingSegment], other: list[TimingSegment], name: str) -> None:
    if len(other) != len(reference):
        raise VodIngestError(
            f"Rendition '{name}' has {len(other)} segment(s) but the reference rendition has "
            f"{len(reference)} -- a ladder must be segment-aligned to loop as one channel."
        )
    for ref, seg in zip(reference, other):
        if ref.asset_boundary != seg.asset_boundary:
            raise VodIngestError(f"Rendition '{name}': discontinuity mismatch at segment {ref.index}")
        if abs(ref.duration_ticks - seg.duration_ticks) > ALIGNMENT_TOLERANCE_TICKS:
            raise VodIngestError(
                f"Rendition '{name}': segment {ref.index} lasts {seg.duration_ticks / 90_000:.3f}s but the "
                f"reference rendition's lasts {ref.duration_ticks / 90_000:.3f}s -- not segment-aligned."
            )


# ── download ─────────────────────────────────────────────────────────────


def download_segments(
    segments: list[TimingSegment],
    output_dir: Path,
    fetch: Fetch,
    *,
    filename_template: str,
    allow_missing: bool,
    workers: int = 8,
    init_cache: dict | None = None,
) -> dict[int, Path | None]:
    """Fetch every segment (init segment prepended for fMP4, like the archive
    path does) into `output_dir`. A failed download raises unless `allow_missing`,
    in which case that segment is `None` (bake's `media_file: null`)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    init_cache = {} if init_cache is None else init_cache

    def _init(segment: TimingSegment) -> bytes:
        key = (segment.init_uri, segment.init_byte_range)
        if key not in init_cache:
            init_cache[key] = fetch(segment.init_uri, segment.init_byte_range)
        return init_cache[key]

    # Resolve inits up front, serially: they are shared, and few.
    for segment in segments:
        if segment.init_uri is not None and segment.source_uri is not None:
            try:
                _init(segment)
            except VodIngestError:
                if not allow_missing:
                    raise

    def _one(segment: TimingSegment) -> tuple[int, Path | None]:
        if segment.source_uri is None:
            return segment.index, None
        try:
            body = fetch(segment.source_uri, segment.byte_range)
            if segment.init_uri is not None:
                body = _init(segment) + body
        except (VodIngestError, KeyError):
            if allow_missing:
                logger.warning("Segment %d (%s) could not be downloaded; recording it as missing", segment.index, segment.source_uri)
                return segment.index, None
            raise
        if not body:
            return segment.index, None
        dest = output_dir / filename_template.format(index=segment.index)
        dest.write_bytes(body)
        return segment.index, dest

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(_one, segments))


# ── HLS / DASH front-ends ────────────────────────────────────────────────


def _hls_audio_playlist(playlist: m3u8.M3U8, multivariant_url: str, variant_audio_group: str | None) -> str | None:
    if not variant_audio_group:
        return None
    group = [m for m in playlist.media if m.type == "AUDIO" and m.group_id == variant_audio_group and m.uri]
    if not group:
        return None  # audio muxed into the video segments
    chosen = next((m for m in group if (m.default or "").upper() == "YES"), group[0])
    return urljoin(multivariant_url, chosen.uri)


def _hls_ladder(manifest_url: str, text: str, fetch: Fetch, selector: str | None) -> tuple[list[dict], str | None]:
    """[{"name", "variant", "manifest_url", "manifest_text"}] best first, plus the reference's audio playlist URL."""
    from .multivariant import parse_multivariant_playlist

    if not is_multivariant_playlist(text):
        return [{"name": "archive", "variant": None, "manifest_url": manifest_url, "manifest_text": text}], None
    multivariant = m3u8.loads(text)
    variants = [
        v for v in parse_multivariant_playlist(text, manifest_url)
        if v["resolution"] or (v["codecs"] and re.search(r"avc|hvc|hev|av01|vp0?9", v["codecs"]))
    ]
    if not variants:
        raise VodIngestError(f"{manifest_url} is a multivariant playlist with no video variants")
    chosen = select_variants(variants, selector)
    taken: set[str] = set()
    ladder = [
        {
            "name": _rendition_name(v, i, taken),
            "variant": v,
            "manifest_url": v["manifest_url"],
            "manifest_text": _text(fetch, v["manifest_url"]),
        }
        for i, v in enumerate(chosen)
    ]
    return ladder, _hls_audio_playlist(multivariant, manifest_url, chosen[0]["audio_group"])


def _require_vod(playlist_url: str, text: str) -> None:
    if not m3u8.loads(text).is_endlist:
        raise VodIngestError(
            f"{playlist_url} has no #EXT-X-ENDLIST: it is a live/event playlist, not a VOD. "
            f"Capture it to an archive and use `grave-robber ingest` instead."
        )


def ingest_url(
    manifest_url: str,
    output_dir: str | Path,
    *,
    fetch: Fetch = http_fetch,
    renditions: str | None = None,
    audio: bool = True,
    audio_manifest_url: str | None = None,
    allow_missing_segments: bool = False,
    workers: int = 8,
) -> dict:
    """Fetch a VOD HLS multivariant/media playlist or DASH MPD and write a
    (possibly multi-rendition) segment-list manifest + media to `output_dir`.
    Returns the manifest dict (also written to `output_dir/manifest.json`)."""
    output_dir = Path(output_dir)
    text = _text(fetch, manifest_url)
    is_dash = "<MPD" in text[:2048]

    audio_url: str | None = audio_manifest_url
    audio_extract: Callable[[], list[TimingSegment]] | None = None
    if is_dash:
        if not dash_is_static(text):
            raise VodIngestError(f"{manifest_url} is a dynamic (live) MPD, not a VOD")
        reps = select_variants(list_dash_representations(text, manifest_url), renditions)
        if not reps:
            raise VodIngestError(f"{manifest_url} has no video Representations")
        taken: set[str] = set()
        ladder = [
            {"name": _rendition_name(r, i, taken), "variant": r, "representation_id": r["id"]}
            for i, r in enumerate(reps)
        ]
        extract = lambda entry: extract_dash(text, manifest_url, entry["representation_id"])  # noqa: E731
        if audio and has_dash_audio(text):
            audio_extract = lambda: extract_dash(text, manifest_url, audio=True)[0]  # noqa: E731
    else:
        ladder, discovered_audio = _hls_ladder(manifest_url, text, fetch, renditions)
        for entry in ladder:
            _require_vod(entry["manifest_url"], entry["manifest_text"])
        extract = lambda entry: extract_hls(entry["manifest_text"], entry["manifest_url"])  # noqa: E731
        audio_url = audio_manifest_url or discovered_audio
        if audio and audio_url:
            def audio_extract() -> list[TimingSegment]:
                audio_text = _text(fetch, audio_url)
                _require_vod(audio_url, audio_text)
                return extract_hls(audio_text, audio_url)[0]

    extracted = [extract(entry) for entry in ladder]
    reference_segments, raw_markers, boundaries = extracted[0]
    if not reference_segments:
        raise VodIngestError(f"{ladder[0].get('manifest_url', manifest_url)} yielded no media segments")
    for entry, (segments, markers, _b) in zip(ladder[1:], extracted[1:]):
        check_alignment(reference_segments, segments, entry["name"])
        if len(markers) != len(raw_markers):
            logger.warning("Rendition '%s' declares %d marker(s), the reference %d; using the reference's",
                           entry["name"], len(markers), len(raw_markers))

    decoded_markers = [decode_marker(m) for m in raw_markers]

    init_cache: dict = {}
    built = []
    for entry, (segments, _m, _b) in zip(ladder, extracted):
        media_paths = download_segments(
            segments, output_dir / "media" / entry["name"], fetch,
            filename_template="seg_{index:06d}.bin", allow_missing=allow_missing_segments,
            workers=workers, init_cache=init_cache,
        )
        variant = entry["variant"]
        built.append({"name": entry["name"], "variant": variant, "media_paths": media_paths})
        logger.info("Rendition '%s': %d segment(s) downloaded", entry["name"],
                    sum(1 for p in media_paths.values() if p is not None))

    aligned_audio = audio_media_paths = None
    if audio and audio_extract is not None:
        audio_segments = audio_extract()
        align = align_audio_segments_by_ticks if is_dash else align_audio_segments
        aligned_audio = align(reference_segments, audio_segments)
        audio_media_paths = download_segments(
            [a for a in aligned_audio if a is not None], output_dir / "media" / "audio", fetch,
            filename_template="audio_{index:06d}.bin", allow_missing=allow_missing_segments,
            workers=workers, init_cache=init_cache,
        )

    manifest = build_ladder_segment_list_manifest(
        reference_segments, boundaries, decoded_markers, built, aligned_audio, audio_media_paths
    )
    write_segment_list_manifest(manifest, output_dir / "manifest.json")
    return manifest
