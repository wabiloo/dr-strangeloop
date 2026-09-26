"""Tests for stream_source.py: HLS/DASH manifest detection, VOD-only
enforcement, and the raw-source download cache -- all with yt-dlp itself
mocked out (no network, no subprocesses)."""

import json
from pathlib import Path

import pytest

from franken_ts import stream_source
from franken_ts.config import AssetConfig
from franken_ts.stream_source import (
    StreamAssetError,
    is_stream_url,
    resolve_stream_asset,
    resolve_stream_assets,
)


# ── is_stream_url ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://example.com/master.m3u8", True),
    ("https://example.com/master.m3u8?token=abc&x=1", True),
    ("http://example.com/path/manifest.mpd", True),
    ("https://example.com/manifest.mpd#frag", True),
    ("https://example.com/movie.mp4", False),
    ("movie.mp4", False),
    ("../relative/movie.mp4", False),
    ("https://example.com/no_extension", False),
])
def test_is_stream_url(url, expected):
    assert is_stream_url(Path(url)) is expected


# ── resolve_stream_asset ─────────────────────────────────────────────────

def _fake_probe_result(**overrides):
    payload = {"is_live": False, "live_status": "not_live", "duration": 120.0}
    payload.update(overrides)

    class _Result:
        stdout = json.dumps(payload)

    return _Result


def test_resolve_stream_asset_rejects_live_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_source, "check_tool", lambda name: Path("/usr/bin/yt-dlp"))
    monkeypatch.setattr(
        stream_source, "run_cmd", lambda cmd, **kw: _fake_probe_result(is_live=True)()
    )

    with pytest.raises(StreamAssetError, match="live"):
        resolve_stream_asset("https://example.com/live.m3u8", tmp_path)


def test_resolve_stream_asset_downloads_vod_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_source, "check_tool", lambda name: Path("/usr/bin/yt-dlp"))

    calls = []

    def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "-J":
            return _fake_probe_result()()
        # The download call: create the .part.mp4 output the real yt-dlp
        # would have produced, at the -o path.
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"fake mp4 bytes")

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr(stream_source, "run_cmd", fake_run_cmd)

    result = resolve_stream_asset("https://example.com/master.m3u8", tmp_path)
    assert result.cached is False
    assert result.local_path.exists()
    assert result.local_path.read_bytes() == b"fake mp4 bytes"
    assert len(calls) == 2  # one probe (-J), one download

    # Second call for the same URL is a cache hit -- no further subprocesses.
    calls.clear()
    result2 = resolve_stream_asset("https://example.com/master.m3u8", tmp_path)
    assert result2.cached is True
    assert result2.local_path == result.local_path
    assert calls == []


# ── resolve_stream_assets (AssetConfig mutation) ─────────────────────────

def test_resolve_stream_assets_mutates_file_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_source, "check_tool", lambda name: Path("/usr/bin/yt-dlp"))

    def fake_run_cmd(cmd, **kw):
        if cmd[1] == "-J":
            return _fake_probe_result()()
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"fake mp4 bytes")

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr(stream_source, "run_cmd", fake_run_cmd)

    stream_url = "https://example.com/master.m3u8"
    assets = [
        AssetConfig(file=stream_url, id="clip1", start="0s", duration="10s"),
        AssetConfig(file=stream_url, id="clip2", start="10s", duration="10s"),
        AssetConfig(file="local.mp4", id="clip3"),
    ]

    resolved = resolve_stream_assets(assets, tmp_path)

    assert len(resolved) == 1  # same manifest resolved once, reused for clip2
    assert assets[0].file == resolved[0].local_path
    assert assets[1].file == resolved[0].local_path
    assert assets[2].file == Path("local.mp4")  # untouched
