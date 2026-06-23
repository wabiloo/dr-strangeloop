from __future__ import annotations

import logging
from pathlib import Path

from .config import OutputConfig
from .timeline import TimelineEntry
from .utils import run_cmd

logger = logging.getLogger(__name__)


def write_concat_playlist(entries: list[TimelineEntry], playlist_path: Path) -> None:
    """Write an ffconcat demuxer playlist."""
    lines = ["ffconcat version 1.0\n"]
    for entry in entries:
        lines.append(f"file '{entry.source_file.resolve()}'\n")
        lines.append(f"inpoint {entry.inpoint:.6f}\n")
        lines.append(f"outpoint {entry.outpoint:.6f}\n")
    playlist_path.write_text("".join(lines))
    logger.debug("Wrote concat playlist to %s (%d entries)", playlist_path, len(entries))


def transcode_to_ts(
    playlist_path: Path,
    output_path: Path,
    output_cfg: OutputConfig,
    forced_keyframe_times: list[float],
    dry_run: bool = False,
) -> None:
    """Run ffmpeg to concat + transcode to MPEG-TS with IDR frames at boundaries."""
    keyframes_expr = ",".join(f"{t:.6f}" for t in forced_keyframe_times)

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(playlist_path),
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.0",
        # bframes=0: no B-frames → no encoder reordering buffer → DTS never goes
        # negative → muxer adds no PTS offset → forced keyframes land at exactly
        # the timestamps we specify.  This is also the correct approach for
        # broadcast TS intended for SCTE-35 clean-cut splicing.
        # no-open-gop: forces every regular GOP keyframe to be a true H.264 IDR
        # (NAL unit type 5) rather than a non-IDR I-frame.  Without this flag x264
        # uses open-gop by default, where only forced keyframes are IDRs.  Non-IDR
        # I-frames do not reset the reference picture buffer, so seeking to them
        # and decoding forward causes "missing reference picture" errors.
        "-x264opts", f"keyint={output_cfg.gop}:min-keyint={output_cfg.gop}:no-scenecut:bframes=0:no-open-gop",
        "-x264-params", "nal-hrd=cbr",
        # setpts=PTS-STARTPTS resets the concatenated stream to start at t=0
        # so our forced-keyframe timestamps map 1:1 to output frames.
        #
        # IMPORTANT: do NOT run an `fps=` filter here.  Every input has already
        # been conformed to the target framerate (validation hard-errors on a
        # framerate mismatch unless --normalize re-encodes the asset to the
        # output fps), so a second resample is redundant.  Worse, running fps=
        # once over the *whole concatenated* stream re-times frames across clip
        # joins and shifts the cut by one frame at some boundaries (observed at
        # ad→content transitions): the first content frame lands one frame
        # *before* the forced IDR instead of on it, breaking clean-cut splicing.
        # Concatenating frame-exact 25fps inputs without resampling keeps every
        # clip boundary aligned to an integer output-frame index, so the forced
        # IDR sits exactly on the first frame of each clip.  scale= is retained
        # as a defensive no-op (inputs are already at the target resolution).
        "-filter:v", f"setpts=PTS-STARTPTS,scale={output_cfg.width}:{output_cfg.height}",
        "-force_key_frames", keyframes_expr,
        "-b:v", f"{output_cfg.bitrate_kbps}k",
        "-minrate", f"{output_cfg.bitrate_kbps}k",
        "-maxrate", f"{output_cfg.bitrate_kbps}k",
        "-bufsize", f"{output_cfg.bitrate_kbps}k",
        "-preset", "fast",
        "-c:a", "aac",
        "-ar", "48000",
        "-af", "aresample=48000:async=1",
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-metadata", f"service_provider={output_cfg.service_provider}",
        "-metadata", f"service_name={output_cfg.service_name}",
        # Suppress the MPEG-TS muxer's default ~1.4s initial PTS offset so that
        # forced-keyframe times align with the YAML-specified boundaries rather
        # than being shifted by the muxer's buffer-fill delay.
        # The residual ~21ms offset (AAC encoder delay) is handled by
        # muxer_offset detection in pts.py.
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-f", "mpegts",
        str(output_path),
    ]

    logger.info("Transcoding %d clips → %s", len(forced_keyframe_times), output_path)
    run_cmd(cmd, dry_run=dry_run)
