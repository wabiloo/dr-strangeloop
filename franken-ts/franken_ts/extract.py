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
from .config import OsdConfig, OutputConfig
from .osd import build_osd_filters
from .timeline import TimelineEntry
from .utils import is_image, run_cmd, source_str

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


def _extract_video(
    src: Path, out: Path, inpoint: float, n_frames: int, output: OutputConfig,
    dry_run: bool,
    entry: Optional[TimelineEntry] = None,
    osd: Optional[OsdConfig] = None,
    fade_in: Optional[float] = None,
    fade_out: Optional[float] = None,
    slate_image: Optional[Path] = None,
) -> None:
    """Extract a VIDEO-ONLY, frame-exact, normalized MPEG-TS segment.

    Starts at PTS 0 (→ first frame is an IDR), runs at the target fps/resolution,
    and contains exactly `n_frames` frames.  No audio (see module docstring).

    Still images (JPEG/PNG) are handled transparently: ``-loop 1`` keeps the
    single image frame alive for the entire duration, and ``-frames:v N`` cuts
    it to the exact requested length.  No ``-ss`` seek is needed for images.

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
      [current] osd(bar, corners...)  → [out]     (if OSD, see osd.py)
    """
    src_is_image = is_image(src)
    clip_dur = n_frames / output.framerate
    has_slate = slate_image is not None

    graph: list[str] = []

    # ── Normalise the clip stream ──────────────────────────────────────────────
    # The filter order matters for two competing requirements:
    #
    # 1. Frame-accurate boundaries (no-slate path):
    #    fps= must come FIRST, before scale and setpts=PTS-STARTPTS.  Putting
    #    fps= last re-timestamps frames via its own counter, which can cause
    #    sub-frame PTS drift that shifts clip boundaries in the assembly pass
    #    (confirmed in the original pipeline work — see ffmpeg.py docstring).
    #    The safe order is: fps= → scale → setpts=PTS-STARTPTS.
    #
    # 2. xfade frame rate declaration (slate path):
    #    xfade checks r_frame_rate on both input links at graph configuration
    #    time.  fps= is the only filter that sets r_frame_rate, but only when
    #    it is the LAST filter before xfade — any filter after fps= (scale,
    #    setpts) strips r_frame_rate back to 1/0.  On the slate path we
    #    therefore put fps= last, accepting the minor PTS re-timestamping
    #    because -frames:v N still enforces the exact frame count and the
    #    assembly pass re-encodes everything with -force_key_frames anyway.
    if has_slate:
        # fps= last: satisfies xfade's r_frame_rate check.
        graph.append(
            f"[0:v] scale={output.width}:{output.height},"
            f"setpts=PTS-STARTPTS,"
            f"fps=fps={output.framerate} [norm]"
        )
    else:
        # fps= first: preserves clean PTS-STARTPTS zeroing for frame-accurate
        # boundaries in the no-xfade path.
        graph.append(
            f"[0:v] fps=fps={output.framerate},"
            f"scale={output.width}:{output.height},"
            f"setpts=PTS-STARTPTS [norm]"
        )

    current = "[norm]"

    # ── Prepare slate image ────────────────────────────────────────────────────
    if has_slate:
        # Same rule: fps= last so xfade sees a declared r_frame_rate.
        # -loop 1 on the input keeps the single PNG frame alive indefinitely;
        # the graph is bounded by -frames:v on the output.
        #
        # IMPORTANT: a filter output label can only be consumed ONCE in a
        # filter_complex graph.  If both fade_in and fade_out are set, [slate]
        # would be used by two xfade nodes, causing ffmpeg to silently fall back
        # to the raw [1:v] stream for the second use (wrong resolution/rate).
        # We create two independent slate labels — [slate_fi] and [slate_fo] —
        # so each xfade node gets its own dedicated, properly prepared input.
        #
        # When the source itself is a still image it is already supplied as
        # input 0 with -loop 1.  The slate then becomes input 1, which is the
        # same index as the normal slate case, so no index adjustment is needed.
        n_slate_uses = (1 if fade_in is not None else 0) + (1 if fade_out is not None else 0)
        for idx in range(n_slate_uses):
            label = f"slate_{idx}"
            graph.append(
                f"[1:v] scale={output.width}:{output.height},"
                f"setsar=1,"
                f"fps=fps={output.framerate} [{label}]"
            )
        slate_labels = iter([f"slate_{i}" for i in range(n_slate_uses)])

    # ── Fade in ────────────────────────────────────────────────────────────────
    if fade_in is not None:
        if has_slate:
            sl = next(slate_labels)
            graph.append(
                f"[{sl}] {current} "
                f"xfade=transition=fade:duration={fade_in:.6f}:offset=0 [fi]"
            )
        else:
            graph.append(f"{current} fade=t=in:st=0:d={fade_in:.6f} [fi]")
        current = "[fi]"

    # ── Fade out ───────────────────────────────────────────────────────────────
    if fade_out is not None:
        fo_offset = clip_dur - fade_out
        if has_slate:
            sl = next(slate_labels)
            graph.append(
                f"{current} [{sl}] "
                f"xfade=transition=fade:duration={fade_out:.6f}:offset={fo_offset:.6f} [fo]"
            )
        else:
            graph.append(
                f"{current} fade=t=out:st={fo_offset:.6f}:d={fade_out:.6f} [fo]"
            )
        current = "[fo]"

    # ── OSD (countdown bar + corner text) ─────────────────────────────────────
    osd_lines, current = build_osd_filters(entry, output, osd, current) if entry is not None else ([], current)
    graph.extend(osd_lines)

    # Rename the last stream to [out] for the -map argument.
    last = graph[-1]
    bracket = last.rfind("[")
    graph[-1] = last[:bracket] + "[out]"

    filter_complex = "; ".join(graph)

    cmd = ["ffmpeg", "-y"]

    if src_is_image:
        # Still image: loop the single frame indefinitely; -frames:v N cuts it.
        # No -ss seek — images have no timeline to seek within.
        cmd += ["-loop", "1", "-i", source_str(src)]
    else:
        cmd += ["-ss", f"{inpoint:.6f}", "-i", source_str(src)]

    if has_slate:
        cmd += ["-loop", "1", "-i", source_str(slate_image)]

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
    """Extract an AUDIO-ONLY segment covering the same clip range.

    For still images there is no audio track, so a silent AAC segment of the
    exact duration is generated instead.  Fade-in/out are intentionally not
    applied to the silent track (nothing to fade).
    """
    if is_image(src):
        # Generate silence for the image duration.  anullsrc produces infinite
        # PCM silence; -t limits it to the exact clip duration.
        run_cmd(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", f"{duration:.6f}",
                "-c:a", "aac",
                "-ar", "48000",
                str(out),
            ],
            dry_run=dry_run,
        )
        return

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
            "-i", source_str(src),
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
    osd: Optional[OsdConfig] = None,
    cache_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> ExtractResult:
    """Extract+normalize one timeline clip into separate video/audio segments.

    Does NOT check the cache — the caller (cli.py) handles lookup before
    calling this function.  If cache_dir is provided, the result is stored
    after extraction so future runs can find it.  Returns the segment paths
    plus the actual video frame count (for boundary verification).
    """
    src = entry.source_file
    inpoint = entry.inpoint
    outpoint = entry.outpoint
    duration = entry.clip_duration
    n_frames = round(duration * output.framerate)

    video_tmp = temp_dir / f"clip_{index:03d}.v.ts"
    audio_tmp = temp_dir / f"clip_{index:03d}.a.m4a"

    logger.info(
        "Extracting %s [%.3f–%.3f] (%d frames) → %s + %s",
        src.name, inpoint, outpoint, n_frames, video_tmp.name, audio_tmp.name,
    )
    _extract_video(
        src, video_tmp, inpoint, n_frames, output, dry_run,
        entry=entry,
        osd=osd,
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
        segments = _cache.store(video_tmp, audio_tmp, entry, output, osd, cache_dir)

    return ExtractResult(segments, actual)
