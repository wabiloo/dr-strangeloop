from __future__ import annotations

import logging
from pathlib import Path

from .config import OutputConfig
from .utils import run_cmd

logger = logging.getLogger(__name__)


def write_concat_playlist(segments: list[Path], playlist_path: Path) -> None:
    """Write an ffconcat demuxer playlist of whole, pre-trimmed segments.

    NOTE: there is deliberately NO `inpoint`/`outpoint` here.  Each segment has
    already been cut to its exact range by the extraction stage and starts on a
    frame-0 IDR.  Using the concat demuxer to trim mid-GOP is exactly the bug
    this pipeline was rewritten to avoid (see extract.py).
    """
    lines = ["ffconcat version 1.0\n"]
    for seg in segments:
        lines.append(f"file '{seg.resolve()}'\n")
    playlist_path.write_text("".join(lines))
    logger.debug("Wrote concat playlist %s (%d entries)", playlist_path, len(segments))


def assemble_ts(
    video_playlist: Path,
    audio_playlist: Path,
    output_path: Path,
    output_cfg: OutputConfig,
    forced_keyframe_times: list[float],
    dry_run: bool = False,
) -> None:
    """Assemble the final MPEG-TS from per-clip video/audio segment playlists.

    Two SEPARATE concat-demuxer inputs are used on purpose:

        input 0 = video-only segments  → mapped to the output video
        input 1 = audio-only segments  → mapped to the output audio

    Why separate inputs instead of one concat of muxed segments?  Because a
    single segment carrying both streams has an audio length that never matches
    its video length to the frame (AAC's ~21 ms frame grid vs the 40 ms video
    grid).  The concat step pads the short stream and punches a one-frame video
    hole at some joins, so the result is not strictly CFR.  Reading video from a
    video-only concat removes the mismatch entirely → the assembled video is
    exactly `frames / fps` long with zero PTS gaps.  (Full findings in
    extract.py.)

    The video is re-encoded once here so that:
      • `-force_key_frames` plants a true IDR exactly on every clip boundary
        (the cumulative, frame-snapped segment lengths), giving clean-cut
        splice points; and
      • the whole programme gets a uniform CBR / GOP / profile.

    There is intentionally NO `fps=` filter on this concatenated stream: all
    rate conversion happened per-clip during extraction, and re-gridding here
    shifts the cut by a frame at boundaries (confirmed experimentally).
    """
    keyframes_expr = ",".join(f"{t:.6f}" for t in forced_keyframe_times)

    cmd = [
        "ffmpeg", "-y",
        # input 0: video-only segments
        "-f", "concat", "-safe", "0", "-i", str(video_playlist),
        # input 1: audio-only segments
        "-f", "concat", "-safe", "0", "-i", str(audio_playlist),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.0",
        # bframes=0: no reorder buffer → DTS never negative → muxer adds no PTS
        # offset beyond the small AAC one (handled by pts.find_idr_pts).
        # no-open-gop: every GOP keyframe is a true IDR, required for clean-cut
        # seeking/splicing.
        "-x264opts",
        f"keyint={output_cfg.gop}:min-keyint={output_cfg.gop}:no-scenecut:bframes=0:no-open-gop",
        "-x264-params", "nal-hrd=cbr",
        # scale is a defensive no-op (segments are already at target resolution).
        # IMPORTANT: do NOT add setpts=PTS-STARTPTS here.  Each segment already
        # starts at PTS 0, so the concat demuxer emits a monotonically increasing
        # PTS sequence starting from 0.  Applying setpts=PTS-STARTPTS resets PTS
        # to 0 again at every segment join (because each segment's own PTS starts
        # at 0), causing the encoder to see backwards timestamps and drop frames.
        # There is also NO fps= filter here — see the docstring.
        "-filter:v", f"scale={output_cfg.width}:{output_cfg.height}",
        "-force_key_frames", keyframes_expr,
        "-b:v", f"{output_cfg.bitrate_kbps}k",
        "-minrate", f"{output_cfg.bitrate_kbps}k",
        "-maxrate", f"{output_cfg.bitrate_kbps}k",
        "-bufsize", f"{output_cfg.bitrate_kbps}k",
        "-preset", "fast",
        "-c:a", "aac",
        "-ar", "48000",
        "-af", "aresample=48000:async=1",
        "-metadata", f"service_provider={output_cfg.service_provider}",
        "-metadata", f"service_name={output_cfg.service_name}",
        # Suppress the MPEG-TS muxer's default ~1.4s initial PTS offset so forced
        # keyframe times align with the YAML boundaries.  The residual ~21ms AAC
        # offset is detected in pts.find_idr_pts.
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts",
        str(output_path),
    ]

    logger.info("Assembling final TS → %s (%d forced keyframes)",
                output_path, len(forced_keyframe_times))
    run_cmd(cmd, dry_run=dry_run)


def generate_preview_mp4(
    source_path: Path,
    output_path: Path,
    dry_run: bool = False,
) -> None:
    """Transcode the final .ts (or any rendition of it) into a tiny, fast,
    browser-playable .mp4 for the igor "Assemble" tab's preview player.

    This is NOT a broadcast artifact -- no SCTE-35, no bitrate fidelity, no
    care about GOP/keyframe alignment. It exists purely so a human can
    eyeball the assembled programme (fades, OSD, ad-break placement) without
    downloading/opening the multi-GB .ts in an external player. Optimized
    entirely for turnaround speed:
      - downscaled to 540p (quality doesn't matter for a sanity-check preview)
      - `-preset ultrafast` (fastest x264 preset; quality/size are irrelevant here)
      - `-crf 32` (deliberately low quality to keep encode time and file size down)
      - faststart so the browser can start playback before the full file
        downloads
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-vf", "scale=-2:540",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "32",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Generating preview MP4 → %s", output_path)
    run_cmd(cmd, dry_run=dry_run)
