from __future__ import annotations

"""
Per-clip extraction & normalization.

────────────────────────────────────────────────────────────────────────────
WHY THIS MODULE EXISTS  (read before changing anything here — hard-won findings)
────────────────────────────────────────────────────────────────────────────
The job of the pipeline is to splice several source clips into one MPEG-TS and
drop SCTE-35 markers exactly on the clip boundaries.  For a clean-cut splice,
every boundary MUST fall on a true H.264 IDR frame, and the boundary timestamp
must be frame-accurate.

The original implementation fed the WHOLE normalized assets to a single
`ffmpeg -f concat` (concat *demuxer*) pass and trimmed each clip with the
demuxer's `inpoint`/`outpoint` directives.  That is broken for any clip whose
inpoint is not exactly on a source keyframe:

  • The concat demuxer cannot start a segment mid-GOP.  Given `inpoint 60`
    when the nearest keyframes are at 56.12s and 60.72s, it seeks back to
    56.12s and emits the pre-roll frames.  After the demuxer's per-segment
    offset those frames carry timestamps that OVERLAP the previous clip, so
    the concatenated PTS jump *backwards* at every join (observed:
    89.96s → 86.12s).
  • `setpts=PTS-STARTPTS` cannot fix this — it subtracts one global constant,
    not per-clip overlaps.
  • ffmpeg's frame-rate sync then DROPS the overlapping frames (thousands of
    them), leaving holes/gaps in the output.  Forced keyframes drift earlier
    and earlier until the IDR the splice logic expects at 150s simply does not
    exist → "No IDR frame found within 2.0s of 150.021s".

The fix is to stop cutting mid-GOP entirely: extract EACH clip into its own
keyframe-clean segment that starts at PTS 0 (hence on an IDR), then concatenate
those whole segments.  Findings that shaped the exact recipe below:

  1. EVERYTHING is normalized/extracted — including ads.  A clip used "as-is"
     can still need a mid-GOP outpoint trim, and mixing raw + normalized
     inputs in the concat is what created the timestamp hazards in the first
     place.  Uniform segments = uniform, predictable timestamps.

  2. VIDEO AND AUDIO ARE EXTRACTED INTO SEPARATE FILES.  This is the single
     most important and least obvious finding.  If a segment carries both
     streams, AAC's 1024-sample (~21.3 ms) frame granularity never lines up
     with the 40 ms (@25fps) video-frame grid, so each segment ends up with
     audio a few ms longer/shorter than its video.  At concat time ffmpeg pads
     the short stream, which inserts a ONE-FRAME VIDEO HOLE at some joins
     (measured: 4500 frames but 180.16s instead of 180.00s — not strictly
     CFR).  Video-only segments have no such mismatch, so the assembled video
     is perfectly CFR (duration == frames / fps, zero PTS gaps).  Audio is
     concatenated on its own track and muxed back in at assembly time.

  3. The segment is forced to an EXACT frame count via `-frames:v N` where
     N = round(clip_duration * fps).  Because clip_duration is already snapped
     to the frame grid (see timeline.py), the cumulative clip boundaries are
     then exact integer frame indices, which is what lets the assembly pass
     place a forced IDR on the precise boundary with no drift.

  4. Fast input seek (`-ss` BEFORE `-i`) is frame-accurate when re-encoding
     (ffmpeg decodes from the preceding keyframe and discards up to the seek
     point) and is dramatically faster than output seek on long sources
     (e.g. inpoint 120s into a 596s asset).  The `fps` filter re-grids the
     decoded frames, and `setpts=PTS-STARTPTS` zeroes the segment so it begins
     on a frame-0 IDR.

  5. Do NOT apply an `fps=` re-grid to the *concatenated* stream during
     assembly — it shifts the cut by a frame at boundaries (confirmed).  All
     rate conversion must happen here, per-clip, before concatenation.

See ffmpeg.py (assemble_ts) for the second half of the story.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cache import ClipSegments
from .config import OutputConfig
from .timeline import TimelineEntry
from .utils import run_cmd

logger = logging.getLogger(__name__)

# Mezzanine quality for the per-clip video segment.  The assembly pass re-encodes
# to the final CBR bitrate, so this only needs to be visually near-lossless to
# avoid generation loss; CRF 16 is plenty while keeping the segments small.
_MEZZANINE_CRF = "16"


@dataclass
class ExtractResult:
    segments: ClipSegments
    frame_count: int          # actual frames in the extracted video segment


def _probe_video_frame_count(path: Path) -> int:
    """Return the exact number of video frames in a file (decoded count)."""
    result = run_cmd(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture=True,
    )
    text = result.stdout.strip().splitlines()
    return int(text[0]) if text and text[0].isdigit() else 0


def _build_drawtext_filters(
    clip_dur: float,
    countdown_seconds: float,
    next_label: str,
) -> list[str]:
    """Return the two drawtext filter strings for the countdown overlay.

    Returns a list of two strings: the label filter and the countdown filter.
    These can be appended to a -vf chain (joined with commas) or embedded into
    a -filter_complex graph (each applied as a separate node).
    """
    start_t = clip_dur - countdown_seconds
    label_escaped = next_label.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    label_expr = (
        f"drawtext="
        f"text='next\\: {label_escaped}':"
        f"fontsize=48:"
        f"fontcolor=white:"
        f"borderw=3:"
        f"bordercolor=black:"
        f"x=w-tw-20:"
        f"y=20:"
        f"enable='gte(t,{start_t:.6f})'"
    )
    countdown_expr = (
        f"drawtext="
        f"text='%{{eif\\:ceil(max(0\\,({clip_dur:.6f}-t)))\\:d}}':"
        f"fontsize=48:"
        f"fontcolor=white:"
        f"borderw=3:"
        f"bordercolor=black:"
        f"x=w-tw-20:"
        f"y=76:"
        f"enable='gte(t,{start_t:.6f})'"
    )
    return [label_expr, countdown_expr]


def _extract_video(
    src: Path, out: Path, inpoint: float, n_frames: int, output: OutputConfig,
    dry_run: bool,
    countdown_seconds: Optional[float] = None,
    next_label: Optional[str] = None,
    fade_in: Optional[float] = None,
    fade_out: Optional[float] = None,
    slate_image: Optional[Path] = None,
) -> None:
    """Extract a VIDEO-ONLY, frame-exact, normalized MPEG-TS segment.

    Starts at PTS 0 (→ first frame is an IDR), runs at the target fps/resolution,
    and contains exactly `n_frames` frames.  No audio (see module docstring).

    Always uses -filter_complex regardless of whether a slate image or overlays
    are present.  filter_complex is a strict superset of -vf, so the simple case
    is just a trivial one-node graph.  This removes conditional branching and
    keeps a single code path.

    Filter graph (nodes added only when the corresponding feature is active):
      [0:v] fps/scale/setpts          → [norm]
      [1:v] scale+setsar              → [slate]   (if slate_image)
      [slate] [norm] xfade(fade-in)   → [fi]      (if fade_in + slate_image)
      [norm/fi] fade=in               → [fi]      (if fade_in, no slate)
      [current] [slate] xfade(fo)     → [fo]      (if fade_out + slate_image)
      [current] fade=out              → [fo]      (if fade_out, no slate)
      [current] drawtext(label)       → [dt1]     (if countdown)
      [dt1]     drawtext(digits)      → [out]     (if countdown)
    """
    clip_dur = n_frames / output.framerate
    has_countdown = countdown_seconds is not None and next_label is not None
    has_slate = slate_image is not None

    graph: list[str] = []

    # ── Normalise the clip stream ──────────────────────────────────────────────
    graph.append(
        f"[0:v] fps=fps={output.framerate},"
        f"scale={output.width}:{output.height},"
        f"setpts=PTS-STARTPTS [norm]"
    )

    current = "[norm]"

    # ── Prepare slate image ────────────────────────────────────────────────────
    if has_slate:
        graph.append(
            f"[1:v] scale={output.width}:{output.height},setsar=1 [slate]"
        )

    # ── Fade in ────────────────────────────────────────────────────────────────
    if fade_in is not None:
        if has_slate:
            graph.append(
                f"[slate] {current} "
                f"xfade=transition=fade:duration={fade_in:.6f}:offset=0 [fi]"
            )
        else:
            graph.append(f"{current} fade=t=in:st=0:d={fade_in:.6f} [fi]")
        current = "[fi]"

    # ── Fade out ───────────────────────────────────────────────────────────────
    if fade_out is not None:
        fo_offset = clip_dur - fade_out
        if has_slate:
            graph.append(
                f"{current} [slate] "
                f"xfade=transition=fade:duration={fade_out:.6f}:offset={fo_offset:.6f} [fo]"
            )
        else:
            graph.append(
                f"{current} fade=t=out:st={fo_offset:.6f}:d={fade_out:.6f} [fo]"
            )
        current = "[fo]"

    # ── Countdown overlay ──────────────────────────────────────────────────────
    if has_countdown:
        dt = _build_drawtext_filters(clip_dur, countdown_seconds, next_label)
        graph.append(f"{current} {dt[0]} [dt1]")
        graph.append(f"[dt1] {dt[1]} [out]")
    else:
        # Rename the last stream to [out] for the -map argument.
        last = graph[-1]
        bracket = last.rfind("[")
        graph[-1] = last[:bracket] + "[out]"

    filter_complex = "; ".join(graph)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{inpoint:.6f}",            # fast input seek
        "-i", str(src),                     # input 0: the clip
    ]
    if has_slate:
        cmd += ["-loop", "1", "-framerate", str(output.framerate), "-i", str(slate_image)]
    cmd += [
        "-an",                              # NO AUDIO — handled separately
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-frames:v", str(n_frames),         # lock to an exact frame count
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", _MEZZANINE_CRF,
        "-pix_fmt", "yuv420p",
        "-bf", "0",                         # no B-frames → DTS==PTS, no reorder delay
        # Closed GOP of IDR frames so the segment starts on a true IDR.
        "-x264opts",
        f"keyint={output.gop}:min-keyint={output.gop}:no-scenecut:no-open-gop",
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts",
        str(out),
    ]

    run_cmd(cmd, dry_run=dry_run)


def _extract_audio(
    src: Path, out: Path, inpoint: float, duration: float, output: OutputConfig,
    dry_run: bool,
    fade_in: Optional[float] = None,
    fade_out: Optional[float] = None,
) -> None:
    """Extract an AUDIO-ONLY segment covering the same clip range."""
    af_parts = ["aresample=48000", "asetpts=PTS-STARTPTS"]

    if fade_in is not None:
        af_parts.append(f"afade=t=in:st=0:d={fade_in:.6f}")

    if fade_out is not None:
        fade_out_start = duration - fade_out
        af_parts.append(f"afade=t=out:st={fade_out_start:.6f}:d={fade_out:.6f}")

    run_cmd(
        [
            "ffmpeg", "-y",
            "-ss", f"{inpoint:.6f}",
            "-i", str(src),
            "-vn",                          # NO VIDEO
            "-t", f"{duration:.6f}",
            "-af", ",".join(af_parts),
            "-c:a", "aac",
            "-ar", "48000",
            str(out),
        ],
        dry_run=dry_run,
    )


def extract_clip(
    entry: TimelineEntry,
    output: OutputConfig,
    temp_dir: Path,
    index: int,
    *,
    cache_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> ExtractResult:
    """Extract+normalize one timeline clip into separate video/audio segments.

    Checks the cache first (keyed on source + output spec + exact cut range).
    On a miss it extracts both segments and stores them.  Returns the segment
    paths plus the actual video frame count (for boundary verification).
    """
    src = entry.source_file
    inpoint = entry.inpoint
    outpoint = entry.outpoint
    duration = entry.clip_duration
    n_frames = round(duration * output.framerate)

    if cache_dir is not None and not dry_run:
        from . import cache as _cache
        cached = _cache.lookup(src, output, cache_dir, inpoint, outpoint,
                               entry.countdown, entry.next_label,
                               entry.fade_in, entry.fade_out,
                               entry.slate_image)
        if cached is not None:
            return ExtractResult(cached, _probe_video_frame_count(cached.video))

    video_tmp = temp_dir / f"clip_{index:03d}.v.ts"
    audio_tmp = temp_dir / f"clip_{index:03d}.a.m4a"

    logger.info(
        "Extracting %s [%.3f–%.3f] (%d frames) → %s + %s",
        src.name, inpoint, outpoint, n_frames, video_tmp.name, audio_tmp.name,
    )
    _extract_video(
        src, video_tmp, inpoint, n_frames, output, dry_run,
        countdown_seconds=entry.countdown,
        next_label=entry.next_label,
        fade_in=entry.fade_in,
        fade_out=entry.fade_out,
        slate_image=entry.slate_image,
    )
    _extract_audio(
        src, audio_tmp, inpoint, duration, output, dry_run,
        fade_in=entry.fade_in,
        fade_out=entry.fade_out,
    )

    if dry_run:
        return ExtractResult(ClipSegments(video_tmp, audio_tmp), n_frames)

    actual = _probe_video_frame_count(video_tmp)
    if actual != n_frames:
        # Off-by-N here would shift every downstream clip boundary, so surface it.
        logger.warning(
            "%s: extracted %d video frames but expected %d "
            "(clip range may exceed available source frames)",
            src.name, actual, n_frames,
        )

    segments = ClipSegments(video_tmp, audio_tmp)
    if cache_dir is not None:
        from . import cache as _cache
        segments = _cache.store(video_tmp, audio_tmp, src, output, cache_dir, inpoint, outpoint,
                                entry.countdown, entry.next_label,
                                entry.fade_in, entry.fade_out,
                                entry.slate_image)

    return ExtractResult(segments, actual)
