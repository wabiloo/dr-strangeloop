"""Render `generator` assets (ffmpeg lavfi sources) to cached local mp4s.

Mirrors stream_source.py: runs before validation/timeline, mutates
`AssetConfig.file` to the rendered file, so everything downstream sees an
ordinary local asset.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import AssetConfig, GeneratorConfig, OutputConfig
from .utils import run_cmd

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000


class GeneratedAssetError(RuntimeError):
    """Raised when a generator asset cannot be rendered."""


@dataclass(frozen=True)
class ResolvedGeneratedAsset:
    label: str
    local_path: Path
    cached: bool


def _escape_drawtext(text: str) -> str:
    for ch in ("\\", ":", "'", "%"):
        text = text.replace(ch, "\\" + ch)
    return text


def _video_source(gen: GeneratorConfig, width: int, height: int, fps: int) -> str:
    if gen.video:
        src = gen.video
        # Append size/rate only where the user didn't set them.
        extra = []
        if "size=" not in src and "s=" not in src:
            extra.append(f"size={width}x{height}")
        if "rate=" not in src and "r=" not in src:
            extra.append(f"rate={fps}")
        if extra:
            src += ("=" if "=" not in src else ":") + ":".join(extra)
        return src
    if gen.preset == "color":
        return f"color=c={gen.color}:size={width}x{height}:rate={fps}"
    return f"{gen.preset}=size={width}x{height}:rate={fps}"


def _audio_source(gen: GeneratorConfig) -> str:
    if gen.audio == "silence":
        return f"anullsrc=r={_SAMPLE_RATE}:cl=stereo"
    return gen.audio or f"sine=frequency=440:sample_rate={_SAMPLE_RATE}"


def build_command(gen: GeneratorConfig, output: OutputConfig, target: Path) -> list[str]:
    width, height = output.width, output.height
    fps = output.framerate
    dur = gen.duration_seconds()

    vf = ["format=yuv420p"]
    if gen.timecode:
        vf.append(
            "drawtext=text='%{pts\\:hms}':fontsize=h/12:fontcolor=white:"
            "box=1:boxcolor=black@0.6:x=(w-text_w)/2:y=h-text_h-h/20"
        )
    if gen.label:
        vf.append(
            f"drawtext=text='{_escape_drawtext(gen.label)}':fontsize=h/16:"
            "fontcolor=white:box=1:boxcolor=black@0.6:x=h/40:y=h/40"
        )

    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", _video_source(gen, width, height, fps),
        "-f", "lavfi", "-i", _audio_source(gen),
        "-t", f"{dur:.6f}",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-profile:v", output.profile, "-preset", "veryfast",
        "-g", str(output.gop), "-keyint_min", str(output.gop), "-sc_threshold", "0",
        "-r", str(fps),
        "-c:a", "aac", "-ar", str(_SAMPLE_RATE), "-ac", "2", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(target),
    ]


def _cache_key(gen: GeneratorConfig, output: OutputConfig) -> str:
    parts = "|".join([
        gen.model_dump_json(),
        f"{output.width}x{output.height}", str(output.framerate),
        str(output.gop), output.profile,
    ])
    return hashlib.sha256(parts.encode()).hexdigest()[:24]


def resolve_generated_asset(
    gen: GeneratorConfig, output: OutputConfig, cache_dir: Path
) -> ResolvedGeneratedAsset:
    """Render one generator spec to `<cache_dir>/<key>.mp4` (cached)."""
    key = _cache_key(gen, output)
    target = cache_dir / f"{key}.mp4"
    label = gen.video or gen.preset
    if target.exists():
        logger.info("Generated asset cache hit: %s", target)
        return ResolvedGeneratedAsset(label, target, cached=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{key}.part.mp4"
    tmp.unlink(missing_ok=True)
    run_cmd(build_command(gen, output, tmp), capture=True)
    if not tmp.exists():
        raise GeneratedAssetError(f"ffmpeg produced no output for generator '{label}'")
    tmp.replace(target)
    return ResolvedGeneratedAsset(label, target, cached=False)


def resolve_generated_assets(
    assets: list[AssetConfig], output: OutputConfig, cache_dir: Path
) -> list[ResolvedGeneratedAsset]:
    """Render every generator asset, setting `AssetConfig.file` to the result."""
    resolved: list[ResolvedGeneratedAsset] = []
    for asset in assets:
        if asset.generator is None:
            continue
        result = resolve_generated_asset(asset.generator, output, cache_dir)
        asset.file = result.local_path
        resolved.append(result)
    return resolved
