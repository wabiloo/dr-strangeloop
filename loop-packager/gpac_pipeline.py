"""DASHCues XML construction and GPAC invocation wrapper (SCOPE.md §4.1 steps 2-3).

Two responsibilities:
  1. Build the `cues.xml` file GPAC's `dasher` filter consumes via `cues=`,
     one `<Cue>` per marker, per track -- video cues at the exact
     `pts_time_ticks` (as `cts`), audio cues snapped to the nearest exact
     multiple of the audio frame duration in ticks (SCOPE.md §4.1 step 2).
  2. Wrap the actual `gpac` CLI invocation: `scte35dec:mode=passthrough`
     piped into `dasher` with that cues file, producing fragmented CMAF
     segments + init segment(s) for HLS and DASH.

GPAC risks and constraints from prototyping that this module must respect
(SCOPE.md §6):
  - Never use `dasher:evte_agg` -- unstable, drops markers / wrong offsets.
  - `scte35dec:mode=passthrough` is the stable mode.
  - An unsnapped audio cue tick can hang GPAC scanning the rest of the file
    ("buggy source cues") rather than failing fast -- so snapping audio cues
    to `90000 * samples_per_frame / sample_rate` is mandatory, not optional.
  - `cues=` with `cts` can silently snap to the nearest available frame
    without erroring if the target isn't an exact match -- so after running
    GPAC, bake.py must independently verify (via box inspection) that
    produced segment boundaries land exactly on the requested cue ticks and
    hard-fail if not (SCOPE.md §4.1 step 3 note, §6 last bullet).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


class GpacNotFoundError(RuntimeError):
    pass


class GpacExecutionError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"gpac exited with code {returncode} running: {' '.join(cmd)}\n"
            f"stderr:\n{stderr}"
        )


@dataclass(frozen=True)
class AudioTrackParams:
    sample_rate: int
    samples_per_frame: int  # e.g. 1024 for AAC

    def cue_tick_granularity(self, timescale: int = 90_000) -> int:
        """Exact tick step between consecutive audio frames.

        e.g. 90000 * 1024 / 48000 = 1920 ticks for 48kHz/1024-sample AAC.
        Must divide evenly -- if it doesn't, the audio track's parameters
        are incompatible with the channel timescale and that is a hard
        error at the caller (do not round silently).
        """
        numerator = timescale * self.samples_per_frame
        if numerator % self.sample_rate != 0:
            raise ValueError(
                f"audio cue granularity is not an exact integer number of "
                f"ticks: {timescale} * {self.samples_per_frame} / "
                f"{self.sample_rate} is not a whole number. Refusing to "
                f"round -- verify the real audio track parameters."
            )
        return numerator // self.sample_rate


def snap_audio_tick(pts_time_ticks: int, audio: AudioTrackParams, timescale: int = 90_000) -> int:
    """Snap a marker's video-track tick to the nearest exact multiple of the
    audio track's frame-duration-in-ticks (SCOPE.md §4.1 step 2)."""
    granularity = audio.cue_tick_granularity(timescale)
    return round(pts_time_ticks / granularity) * granularity


def check_gpac_available() -> Path:
    path = shutil.which("gpac")
    if path is None:
        raise GpacNotFoundError(
            "'gpac' not found on PATH -- must be built from the pinned "
            "commit per SCOPE.md §6/§7 (no packaged release ships scte35dec)."
        )
    return Path(path)


def compute_segment_boundary_ticks(
    total_content_ticks: int,
    nominal_segment_ticks: int,
    marker_ticks: list[int],
    *,
    end_guard_ticks: int = 0,
) -> list[int]:
    """Compute the full set of desired segment boundary ticks: a regular
    nominal grid (every `nominal_segment_ticks`, for real ABR-sized
    segments) UNIONED with the exact marker ticks (forced boundaries so
    every SCTE-35 event lands exactly on a segment start).

    This mirrors how live ad-insertion packagers actually behave: normal
    short segments throughout, with a forced (possibly short/long) cut
    exactly at each ad marker -- NOT "one giant segment per ad break", which
    is what naively using only marker ticks as GPAC dasher cues produces
    (GPAC's `cues=` docs: "only these are used to derive segment
    boundaries" -- there is no way to additionally honor `segdur`/`cdur`
    once any cues are given, so the full desired grid must be provided
    explicitly here).

    Nominal grid points are NOT guaranteed to land on real IDR frames --
    GPAC's cues="...":cts mode will silently snap each one to the nearest
    available frame (SCOPE.md §6), which is normal/expected here (exactly
    like any keyframe-aligned segmenter). Only marker ticks must match
    exactly with zero snap; bake.py is responsible for verifying that
    separately against the real produced output.

    `end_guard_ticks`: `total_content_ticks` is typically an approximate
    probe of the source container's duration (e.g. ffprobe's
    `format=duration`), which can slightly OVERESTIMATE the true usable
    content length. A nominal grid point that lands in that small
    overestimated tail has no real content after it: on a track with fine
    keyframe-snapping (video) GPAC silently drops it (no real segment
    created there), but on a track with coarser, independent snapping
    (audio, snapped to its own frame-duration grid) that same requested
    tick can land on a genuinely different, real tick -- producing an
    extra, spurious tail segment on one track but not the other, breaking
    serve.py's 1:1 video/audio segment pairing. Excluding any nominal grid
    point within `end_guard_ticks` of the estimated end avoids requesting
    that ambiguous tail point in the first place. Marker ticks are never
    excluded by this guard -- a real ad marker must never be dropped.
    """
    if nominal_segment_ticks <= 0:
        raise ValueError("nominal_segment_ticks must be strictly positive")
    grid_end = max(0, total_content_ticks - end_guard_ticks)
    grid = [t for t in range(0, total_content_ticks, nominal_segment_ticks) if t < grid_end]
    return sorted(set(grid) | {0} | set(marker_ticks))


def build_cues_xml(
    boundary_ticks: list[int],
    cues_path: Path,
    *,
    video_track_id: int,
    audio_track_id: int | None,
    audio_params: AudioTrackParams | None,
    timescale: int = 90_000,
) -> None:
    """Write GPAC's `dasher:cues=` XML: one `<Cue>` per desired segment
    boundary tick, per track (see compute_segment_boundary_ticks -- this is
    normally the nominal grid unioned with exact marker ticks, not just the
    markers alone).

    Video cues use `cts` at the exact tick. Audio cues (if an audio track
    is present) use `cts` snapped to the nearest exact audio-frame-boundary
    tick.

    Per GPAC's own dasher docs (`gpac -ha dasher`), each `<Stream>` element
    takes an `id` attribute (the actual PID/track ID -- NOT `trackID`) and a
    `timescale` attribute for the units its children's `cts`/`dts` are in.
    Cues must be listed in decoding order (ascending).
    """
    sorted_ticks = sorted(set(boundary_ticks))

    root = ET.Element("DASHCues")
    stream_video = ET.SubElement(
        root, "Stream", id=str(video_track_id), timescale=str(timescale)
    )

    for tick in sorted_ticks:
        ET.SubElement(stream_video, "Cue", cts=str(tick))

    if audio_track_id is not None:
        if audio_params is None:
            raise ValueError("audio_track_id given without audio_params")
        stream_audio = ET.SubElement(
            root, "Stream", id=str(audio_track_id), timescale=str(timescale)
        )
        for tick in sorted_ticks:
            snapped = snap_audio_tick(tick, audio_params, timescale)
            ET.SubElement(stream_audio, "Cue", cts=str(snapped))

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    with cues_path.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)

    logger.info(
        "Wrote DASHCues XML to %s (%d segment boundaries)", cues_path, len(sorted_ticks)
    )


def run_gpac_dasher(
    input_ts: Path,
    cues_xml: Path,
    output_dir: Path,
    *,
    segment_duration_seconds: float = 4.0,
    hls: bool = True,
    dash: bool = True,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    """Invoke gpac with scte35dec:mode=passthrough piped into dasher.

    Produces fragmented CMAF segments + init segment(s) for HLS and/or DASH.
    Never pass `evte_agg` -- see module docstring / SCOPE.md §6.

    Confirmed working invocation shape against real franken-ts output:

        gpac -i in.ts scte35dec:mode=passthrough @ \\
             dasher:cues=cues.xml:segdur=4:cdur=4:profile=live \\
             -o out_dir/manifest.mpd

    Segments land next to the manifest (in `output_dir`); there is no
    separate `out_dir=` dasher option -- it's derived from `-o`'s path.
    A second pass with `-o .../manifest.m3u8` produces the HLS variant from
    the same cue-driven segmentation (kept as a second invocation rather
    than trying to force both outputs from one dasher call, per SCOPE.md
    §4.1 step 3: "either is fine, correctness matters more than invocation
    count").
    """
    check_gpac_available()
    output_dir.mkdir(parents=True, exist_ok=True)

    def _build_cmd(manifest_path: Path) -> list[str]:
        return [
            "gpac",
            "-i", str(input_ts),
            "scte35dec:mode=passthrough",
            "@",
            f"dasher:cues={cues_xml}:segdur={segment_duration_seconds}"
            f":cdur={segment_duration_seconds}:profile=live",
            "-o", str(manifest_path),
            *(extra_args or []),
        ]

    last_result: subprocess.CompletedProcess | None = None

    if dash:
        cmd = _build_cmd(output_dir / "manifest.mpd")
        logger.info("Running gpac (DASH): %s", " ".join(cmd))
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise GpacExecutionError(cmd, result.returncode, result.stderr)
            last_result = result

    if hls:
        cmd = _build_cmd(output_dir / "manifest.m3u8")
        logger.info("Running gpac (HLS): %s", " ".join(cmd))
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise GpacExecutionError(cmd, result.returncode, result.stderr)
            last_result = result

    logger.info("gpac completed successfully")
    return last_result if last_result is not None else subprocess.CompletedProcess([], 0, "", "")
