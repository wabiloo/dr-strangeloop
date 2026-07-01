from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import AssetConfig, OutputConfig
from .utils import is_image, run_cmd

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    path: Path
    duration: float
    video_streams: int
    audio_streams: int
    fps: Optional[float]
    is_vfr: bool
    width: int
    height: int
    codec: str


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def print(self) -> None:
        for w in self.warnings:
            logger.warning("%s", w)
        for e in self.errors:
            logger.error("%s", e)


def probe_file(path: Path) -> VideoInfo:
    """Run ffprobe on a file and return structured info.

    For still images (JPEG/PNG) the file has no meaningful duration or frame
    rate — those come from the asset config.  We still call ffprobe to get the
    pixel dimensions and verify the file is readable, but we synthesise sane
    defaults for the other fields so the rest of the pipeline can treat image
    assets uniformly.
    """
    result = run_cmd(
        [
            "ffprobe", "-v", "error",
            "-show_streams", "-show_format",
            "-of", "json",
            str(path),
        ],
        capture=True,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    vs = video[0] if video else {}

    if is_image(path):
        # Still images: synthesise a VideoInfo that satisfies the rest of the
        # pipeline.  Duration is set to 0.0 here; validate_inputs() will patch
        # it with the value from the asset config.  Audio stream count is
        # reported as 1 so the audio-stream validation doesn't fire — the image
        # extraction path generates a silent audio track automatically.
        return VideoInfo(
            path=path,
            duration=0.0,           # overridden from asset.duration in validate_inputs
            video_streams=1,        # the single image frame counts as one video stream
            audio_streams=1,        # synthesised — extraction will produce silence
            fps=None,               # not applicable
            is_vfr=False,
            width=int(vs.get("width", 0)),
            height=int(vs.get("height", 0)),
            codec=vs.get("codec_name", ""),
        )

    fps: Optional[float] = None
    is_vfr = False
    if vs:
        r_frame_rate = vs.get("r_frame_rate", "0/1")
        avg_frame_rate = vs.get("avg_frame_rate", "0/1")

        def parse_ratio(r: str) -> float:
            n, d = r.split("/")
            return float(n) / float(d) if float(d) else 0.0

        rfps = parse_ratio(r_frame_rate)
        afps = parse_ratio(avg_frame_rate)
        fps = rfps
        # VFR: avg and real frame rates differ by more than 0.5fps
        is_vfr = abs(rfps - afps) > 0.5 if rfps > 0 and afps > 0 else False

    # Use the video stream duration when available — it is the authoritative
    # measure of how many video frames the file contains.  The container
    # format.duration reflects the longest stream (often audio), which can
    # exceed the video duration when audio was resampled from a source with
    # a slightly different rate (e.g. 23.976 fps → 25 fps normalization
    # leaves 15.023s of audio while the video is only 14.960s).  Using the
    # longer audio-driven value for outpoint snapping would misplace the
    # forced IDR at an asset boundary.
    video_dur_str = vs.get("duration") if vs else None
    if video_dur_str:
        duration = float(video_dur_str)
    else:
        duration = float(fmt.get("duration", 0.0))

    return VideoInfo(
        path=path,
        duration=duration,
        video_streams=len(video),
        audio_streams=len(audio),
        fps=fps,
        is_vfr=is_vfr,
        width=int(vs.get("width", 0)),
        height=int(vs.get("height", 0)),
        codec=vs.get("codec_name", ""),
    )


def validate_inputs(
    assets: list[AssetConfig],
    output: OutputConfig,
    normalize: bool,
) -> tuple[ValidationReport, dict[Path, VideoInfo]]:
    """Validate all asset files. Returns report and per-file info."""
    report = ValidationReport()
    infos: dict[Path, VideoInfo] = {}

    for asset in assets:
        if not asset.file.exists():
            report.errors.append(f"File not found: {asset.file}")
            continue

        try:
            info = probe_file(asset.file)
        except Exception as exc:
            report.errors.append(f"Cannot probe {asset.file}: {exc}")
            continue

        # ── Still image: special handling ─────────────────────────────────────
        if is_image(asset.file):
            dur_s = asset.duration_seconds()
            if dur_s is None or dur_s <= 0:
                report.errors.append(
                    f"{asset.file.name}: is a still image — 'duration' must be set "
                    "to a positive value (e.g. duration: \"5s\")"
                )
                continue
            # Patch the synthesised VideoInfo with the config-supplied duration so
            # timeline.py sees a meaningful file_duration and doesn't reject it.
            infos[asset.file] = VideoInfo(
                path=info.path,
                duration=dur_s,
                video_streams=info.video_streams,
                audio_streams=info.audio_streams,
                fps=info.fps,
                is_vfr=info.is_vfr,
                width=info.width,
                height=info.height,
                codec=info.codec,
            )
            # Resolution check only (images never need fps/audio-stream checks).
            if info.width and info.height:
                if info.width != output.width or info.height != output.height:
                    msg = (
                        f"{asset.file.name}: resolution {info.width}x{info.height} differs from "
                        f"target {output.resolution}"
                    )
                    report.warnings.append(msg + " (will scale)")
            continue

        infos[asset.file] = info

        # Validate that start/duration are within the file's actual duration.
        start_s = asset.start_seconds() or 0.0
        dur_s = asset.duration_seconds()

        if start_s >= info.duration:
            report.errors.append(
                f"{asset.file.name}: start {start_s:.3f}s exceeds video stream duration "
                f"{info.duration:.3f}s"
            )
        elif dur_s is not None:
            end_s = start_s + dur_s
            if end_s > info.duration:
                report.errors.append(
                    f"{asset.file.name}: start {start_s:.3f}s + duration {dur_s:.3f}s "
                    f"= {end_s:.3f}s exceeds video stream duration {info.duration:.3f}s"
                )

        if info.video_streams != 1:
            report.errors.append(
                f"{asset.file}: expected 1 video stream, found {info.video_streams}"
            )
        if info.audio_streams != 1:
            report.errors.append(
                f"{asset.file}: expected 1 audio stream, found {info.audio_streams}"
            )

        if info.is_vfr:
            report.warnings.append(
                f"{asset.file.name}: variable frame rate detected — "
                "duration estimates may be imprecise"
                + (" (will normalize)" if normalize else " (pass --normalize to fix)")
            )

        if info.fps is not None:
            fps_diff = abs(info.fps - output.framerate)
            if fps_diff > 0.01:
                msg = (
                    f"{asset.file.name}: frame rate {info.fps:.3f} differs from "
                    f"target {output.framerate}"
                )
                if normalize:
                    report.warnings.append(msg + " (will normalize)")
                else:
                    report.errors.append(msg + " (pass --normalize to re-encode)")

        if info.width and info.height:
            if info.width != output.width or info.height != output.height:
                msg = (
                    f"{asset.file.name}: resolution {info.width}x{info.height} differs from "
                    f"target {output.resolution}"
                )
                if normalize:
                    report.warnings.append(msg + " (will normalize)")
                else:
                    report.errors.append(msg + " (pass --normalize to re-encode)")

    return report, infos
