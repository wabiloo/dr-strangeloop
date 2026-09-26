"""HLS multivariant (multivariant) playlist handling.

A multivariant playlist carries no segments, so it never contributes to the loop
timeline -- but it's still useful metadata: which renditions exist and their
specs (bandwidth, resolution, codecs, ...). `parse_multivariant_playlist` returns
that info; callers use `is_multivariant_playlist` to keep multivariant URLs out of the
segment/coverage pipeline.
"""

from __future__ import annotations

from urllib.parse import urljoin

import m3u8


def is_multivariant_playlist(manifest_text: str) -> bool:
    return bool(m3u8.loads(manifest_text).is_variant)


def find_audio_playlist(trace, manifest_url: str) -> dict | None:
    """How the audio of the variant at `manifest_url` is delivered, per the
    archive's multivariant playlist: `{"manifest_url": <abs url>, ...}` for a
    separate audio playlist (`#EXT-X-MEDIA:TYPE=AUDIO,URI=...`, DEFAULT
    preferred), `{"manifest_url": None}` when its audio group declares no URI
    (audio muxed into the video segments), or None when nothing is declared."""
    for decorated_url in trace.get_abr_manifest_urls():
        multivariant_url = str(decorated_url.url)
        entries = trace.get_entries_for_url(multivariant_url)
        if not entries:
            continue
        text = entries[-1].content_bytes.decode("utf-8", errors="replace")
        if not is_multivariant_playlist(text):
            continue
        playlist = m3u8.loads(text)
        for rendition in parse_multivariant_playlist(text, multivariant_url):
            if rendition["manifest_url"] != manifest_url or not rendition["audio_group"]:
                continue
            group = [
                m for m in playlist.media if m.type == "AUDIO" and m.group_id == rendition["audio_group"]
            ]
            with_uri = [m for m in group if m.uri]
            if with_uri:
                chosen = next((m for m in with_uri if (m.default or "").upper() == "YES"), with_uri[0])
                return {"manifest_url": urljoin(multivariant_url, chosen.uri), "name": chosen.name}
            return {"manifest_url": None}
    return None


def find_declared_variant(trace, manifest_url: str) -> dict | None:
    """The rendition entry (codecs, bandwidth, resolution, ...) that some
    multivariant playlist in the archive declares for `manifest_url`, or
    None if the archive never captured one that lists it."""
    for decorated_url in trace.get_abr_manifest_urls():
        entries = trace.get_entries_for_url(str(decorated_url.url))
        if not entries:
            continue
        text = entries[-1].content_bytes.decode("utf-8", errors="replace")
        if not is_multivariant_playlist(text):
            continue
        for rendition in parse_multivariant_playlist(text, str(decorated_url.url)):
            if rendition["manifest_url"] == manifest_url:
                return rendition
    return None


def parse_multivariant_playlist(manifest_text: str, manifest_url: str) -> list[dict]:
    """One dict per `#EXT-X-STREAM-INF` variant (absolute `manifest_url`)."""
    playlist = m3u8.loads(manifest_text)
    variants = []
    for p in playlist.playlists:
        info = p.stream_info
        resolution = info.resolution
        variants.append(
            {
                "manifest_url": urljoin(manifest_url, p.uri),
                "bandwidth": info.bandwidth,
                "average_bandwidth": info.average_bandwidth,
                "resolution": f"{resolution[0]}x{resolution[1]}" if resolution else None,
                "frame_rate": info.frame_rate,
                "codecs": info.codecs,
                "audio_group": info.audio,
            }
        )
    return variants
