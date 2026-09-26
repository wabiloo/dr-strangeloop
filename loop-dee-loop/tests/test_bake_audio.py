"""Sparse bake with audio, against real ffmpeg: audio muxed into the video
segments, and audio delivered as a separate playlist (per-entry audio_media_file)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmaf  # noqa: E402
from bake import bake_segment_list  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TICKS_2S = 180_000


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _video_ts(path: Path, with_audio: bool):
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"] if with_audio else ["-an"]
    _ff("-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=2", *audio,
        "-c:v", "libx264", "-g", "24", "-f", "mpegts", str(path))


def _audio_ts(path: Path):
    _ff("-f", "lavfi", "-i", "sine=frequency=880:duration=2", "-c:a", "aac", "-f", "mpegts", str(path))


def _manifest(tmp_path: Path, segments: list[dict], audio: dict | None = None) -> Path:
    data = {"segments": segments, "markers": []}
    if audio:
        data["audio"] = audio
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    return path


def _seg(i, media, **extra):
    return {"index": i, "duration_ticks": TICKS_2S, "asset_boundary": False, "media_file": str(media), **extra}


def _rendition(out: Path):
    return json.loads((out / "loop_descriptor.json").read_text())["video_renditions"][0]


def test_muxed_audio_is_detected_and_baked(tmp_path):
    for i in range(2):
        _video_ts(tmp_path / f"v{i}.ts", with_audio=True)
    manifest = _manifest(tmp_path, [_seg(i, tmp_path / f"v{i}.ts") for i in range(2)])
    out = tmp_path / "out"

    bake_segment_list(manifest, out)

    r = _rendition(out)
    assert r["audio_sparse"] is True and r["audio_variant"]["codecs"] == "mp4a.40.2"
    assert r["audio_segment_boundary_ticks"] == [0, TICKS_2S]
    assert r["audio_segment_present"] == [True, True] and r["audio_init_files"] == ["audio_init_0.mp4"]
    seg_dir = out / "segments" / r["name"]
    init = (seg_dir / "audio_init_0.mp4").read_bytes()
    assert cmaf.avcc_config(init) is None and b"esds" in init
    # segment 1's audio starts at its ledger position (2s at the 48 kHz track timescale)
    ts = cmaf.track_timescale(init)
    seg1 = (seg_dir / "seg_a_000001.m4s").read_bytes()
    tfdt = cmaf._find(seg1, ("traf", "tfdt"), 8, len(seg1))
    width = 8 if seg1[tfdt[0] + 8] == 1 else 4  # tfdt version 1 = 64-bit value
    assert int.from_bytes(seg1[tfdt[0] + 12 : tfdt[0] + 12 + width], "big") == 2 * ts


def test_video_only_source_has_no_audio(tmp_path):
    _video_ts(tmp_path / "v0.ts", with_audio=False)
    manifest = _manifest(tmp_path, [_seg(0, tmp_path / "v0.ts")])
    out = tmp_path / "out"

    bake_segment_list(manifest, out)

    r = _rendition(out)
    assert r["audio_variant"] is None and "audio_sparse" not in r


def test_separate_audio_playlist_with_a_hole(tmp_path):
    for i in range(2):
        _video_ts(tmp_path / f"v{i}.ts", with_audio=False)
    _audio_ts(tmp_path / "a0.ts")
    manifest = _manifest(
        tmp_path,
        [
            _seg(0, tmp_path / "v0.ts", audio_media_file=str(tmp_path / "a0.ts"), audio_duration_ticks=TICKS_2S),
            _seg(1, tmp_path / "v1.ts", audio_media_file=None, audio_duration_ticks=TICKS_2S),
        ],
        audio={"separate": True},
    )
    out = tmp_path / "out"

    bake_segment_list(manifest, out, allow_missing_segments=True)

    r = _rendition(out)
    assert r["audio_segment_present"] == [True, False]
    assert (out / "segments" / r["name"] / "seg_a_000000.m4s").exists()
    assert not (out / "segments" / r["name"] / "seg_a_000001.m4s").exists()
