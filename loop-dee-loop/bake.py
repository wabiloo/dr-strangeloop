"""Bake phase entrypoint (SCOPE.md §4.1). Run once per schedule change, never
in the hot serving path.

    franken-ts output (.ts + .markers.json)
            |
            v
       [ bake.py ]  <-- this module
            |
            v
      loop package/  (immutable: segments + init + loop_descriptor.json)

Steps (mirrors SCOPE.md §4.1 numbering):
  1. Validate: demux with GPAC/threefive, hard-fail on any mismatch between
     .markers.json and the actually-embedded SCTE-35.
  2. Build the DASHCues XML (gpac_pipeline.build_cues_xml).
  3. Run GPAC (gpac_pipeline.run_gpac_dasher).
  4. Compute total_loop_duration_ticks from the actual produced segments
     (ground truth, not a nominal value).
  5. Author SCTE-35 signaling ourselves from .markers.json
     (scte35_signaling.py) -- loop-relative tick offsets.
  6. Write the immutable, versioned loop package + loop_descriptor.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from gpac_pipeline import (
    AudioTrackParams,
    build_cues_xml,
    compute_segment_boundary_ticks,
    run_gpac_dasher,
)
logger = logging.getLogger(__name__)

TIMESCALE = 90_000


class ValidationError(RuntimeError):
    """Hard-fail condition per SCOPE.md §4.1 step 1: markers.json and the
    actual embedded SCTE-35 in the .ts disagree. Never silently reconciled."""


@dataclass
class DecodedMarker:
    event_id: str
    pts_time_ticks: int
    splice_command_b64: str


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    logger.debug("$ %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}"
        )
    return result


def probe_scte35_pid(ts_file: Path) -> int:
    """Return the PID carrying SCTE-35 (type SCTE35) in the given .ts, via
    ffprobe. Hard-fails if none is found."""
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=index,id,codec_type,codec_tag_string",
            "-of", "json",
            str(ts_file),
        ]
    )
    data = json.loads(result.stdout)
    for stream in data.get("streams", []):
        # ffprobe surfaces SCTE-35 streams as codec_type "data" with a
        # scte35-ish tag; exact tag string can vary by ffmpeg build, so we
        # look for a couple of known spellings.
        tag = (stream.get("codec_tag_string") or "").lower()
        if "scte" in tag:
            return _pid_from_ffprobe_id(stream["id"])
    raise ValidationError(
        f"No SCTE-35 PID found in {ts_file} -- expected a stream typed "
        f"SCTE35 (franken-ts always injects one). Cannot proceed."
    )


def _pid_from_ffprobe_id(ffprobe_id: str) -> int:
    """Convert ffprobe's stream `id` field (e.g. "0x100") to the actual PID
    / GPAC track ID as an int. ffprobe's array `index` (0, 1, 2, ...) is
    NOT the same thing as the MPEG-TS PID / GPAC trackID -- e.g. video PID
    256 (0x100) is stream index 0. GPAC's `dasher:cues=` XML `trackID`
    attribute expects the real track ID (PID), so every caller building
    cues must use this, not ffprobe's `index`.
    """
    return int(ffprobe_id, 16)


def decode_embedded_scte35(ts_file: Path) -> list[DecodedMarker]:
    """Decode every SCTE-35 splice_info_section actually embedded in the
    .ts using threefive, independent of markers.json, for cross-validation.

    threefive is used here as recommended (SCOPE.md §4.1 step 1: "already
    proven to round-trip franken-ts's cues correctly in prototyping").
    """
    try:
        import threefive  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "threefive is required for bake.py's validation step "
            "(pip install threefive)"
        ) from exc

    decoded: list[DecodedMarker] = []

    def _callback(cue) -> None:  # pragma: no cover - threefive-shaped callback
        # threefive's Cue.command.pts_time is the splice command's own PTS,
        # in *seconds* as a float. Round to ticks in the same 90kHz clock
        # used everywhere else (matches franken-ts's own PTS_CLOCK).
        pts_time = getattr(cue.command, "pts_time", None)
        if pts_time is None:
            raise ValidationError(
                "decoded SCTE-35 message has no pts_time (splice_immediate "
                "or unsupported command type) -- franken-ts only emits "
                "time_signal/splice_insert with an explicit pts_time, so "
                "this indicates unexpected content in the .ts."
            )
        ticks = round(pts_time * TIMESCALE)

        # event_id lives on the segmentation descriptor (segmentation_event_id,
        # a "0x.." string), not on the splice command itself for time_signal.
        event_id = None
        for descriptor in getattr(cue, "descriptors", []):
            seg_event_id = getattr(descriptor, "segmentation_event_id", None)
            if seg_event_id is not None:
                event_id = seg_event_id
                break
        if event_id is None:
            # splice_insert carries its own splice_event_id directly.
            event_id = getattr(cue.command, "splice_event_id", None)
        if event_id is None:
            raise ValidationError(
                "decoded SCTE-35 message has no event_id on either its "
                "segmentation descriptor or splice command -- cannot "
                "cross-validate against markers.json."
            )

        event_id_str = (
            event_id
            if isinstance(event_id, str) and event_id.startswith("0x")
            else f"0x{int(event_id):08X}"
        )
        # Normalize to the same fixed-width 0x%08X form markers.json uses.
        event_id_str = f"0x{int(event_id_str, 16):08X}"

        b64 = cue.base64()
        decoded.append(
            DecodedMarker(
                event_id=event_id_str,
                pts_time_ticks=ticks,
                splice_command_b64=b64,
            )
        )

    stream = threefive.Stream(str(ts_file))
    stream.decode(func=_callback)
    return decoded


def load_markers(markers_path: Path) -> list[dict]:
    if not markers_path.exists():
        raise ValidationError(
            f"{markers_path} does not exist. franken-ts must emit a "
            f".markers.json sidecar alongside its .ts output (SCOPE.md §2); "
            f"loop-dee-loop never re-derives marker timing by re-probing "
            f"the .ts."
        )
    with markers_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_markers_against_ts(
    markers: list[dict],
    decoded: list[DecodedMarker],
) -> list[dict]:
    """Cross-check every entry in markers.json against the actually decoded
    SCTE-35 in the .ts. Hard-fails on any mismatch (missing/extra event, or
    a PTS off by even one tick) -- SCOPE.md §4.1 step 1.

    On success, returns markers.json entries augmented with the original
    base64 splice command captured from the real decoded message (needed by
    scte35_signaling.py, since markers.json itself doesn't carry raw bytes).

    NOTE: `event_id` alone is NOT a unique key -- a start/stop pair (e.g. a
    time_signal splice-out and splice-in) shares the same `event_id` by
    design (franken-ts assigns one event_id per ad break, with two SCTE-35
    messages: start and stop). The key used here is `(event_id, occurrence)`
    where `occurrence` is the 0-based position of that message among all
    messages sharing the same event_id, in ascending PTS order on each side
    independently (start is always occurrence 0, stop is occurrence 1).
    This lets a genuine PTS mismatch on the *same* logical message be
    reported as a mismatch rather than masquerading as "missing" +  "extra"
    on two different keys (which a naive (event_id, pts_time_ticks) key
    would do, since it would fold the actual PTS into the identity of the
    thing being compared).
    """

    def _with_occurrence(items, get_event_id, get_pts):
        by_event: dict[str, list] = {}
        for item in items:
            by_event.setdefault(get_event_id(item), []).append(item)
        keyed = {}
        for event_id, group in by_event.items():
            group.sort(key=get_pts)
            for occurrence, item in enumerate(group):
                keyed[(event_id, occurrence)] = item
        return keyed

    decoded_by_key = _with_occurrence(decoded, lambda d: d.event_id, lambda d: d.pts_time_ticks)
    markers_by_key = _with_occurrence(markers, lambda m: m["event_id"], lambda m: m["pts_time_ticks"])

    missing = set(markers_by_key) - set(decoded_by_key)
    extra = set(decoded_by_key) - set(markers_by_key)

    if missing:
        raise ValidationError(
            f"markers.json declares event(s) {sorted(missing)} that are not "
            f"present in the .ts's decoded SCTE-35. Hard failure per "
            f"SCOPE.md §4.1 step 1 -- not reconciling."
        )
    if extra:
        raise ValidationError(
            f"the .ts contains SCTE-35 event(s) {sorted(extra)} that are "
            f"not declared in markers.json. Hard failure per SCOPE.md §4.1 "
            f"step 1 -- not reconciling."
        )

    augmented: list[dict] = []
    for key, marker in markers_by_key.items():
        decoded_marker = decoded_by_key[key]
        if decoded_marker.pts_time_ticks != marker["pts_time_ticks"]:
            raise ValidationError(
                f"event {marker['event_id']}: markers.json declares "
                f"pts_time_ticks={marker['pts_time_ticks']} but the .ts's "
                f"decoded SCTE-35 has pts_time_ticks="
                f"{decoded_marker.pts_time_ticks} (off by "
                f"{abs(decoded_marker.pts_time_ticks - marker['pts_time_ticks'])} "
                f"tick(s)). Hard failure -- not reconciling."
            )
        augmented_marker = dict(marker)
        augmented_marker["splice_command_b64"] = decoded_marker.splice_command_b64
        augmented.append(augmented_marker)

    logger.info(
        "Validated %d marker(s): markers.json matches decoded SCTE-35 exactly",
        len(augmented),
    )
    return augmented


def read_actual_track_params(ts_file: Path) -> tuple[int, int | None, AudioTrackParams | None]:
    """Return (video_track_id, audio_track_id, audio_params) by probing the
    real .ts -- never hardcode 48kHz/1024-sample AAC (SCOPE.md §4.1 step 2).

    The returned track IDs are the actual MPEG-TS PIDs (== GPAC track IDs
    used by `dasher:cues=`), taken from ffprobe's `id` field (e.g. "0x100"),
    NOT ffprobe's 0-based array `index` -- those are different numbering
    schemes and using `index` here would silently build a cues XML that
    targets the wrong (or a nonexistent) GPAC track.
    """
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=index,id,codec_type,sample_rate",
            "-of", "json",
            str(ts_file),
        ]
    )
    data = json.loads(result.stdout)
    video_track_id = None
    audio_track_id = None
    audio_params = None
    for stream in data.get("streams", []):
        if stream["codec_type"] == "video" and video_track_id is None:
            video_track_id = _pid_from_ffprobe_id(stream["id"])
        elif stream["codec_type"] == "audio" and audio_track_id is None:
            audio_track_id = _pid_from_ffprobe_id(stream["id"])
            sample_rate = int(stream["sample_rate"])
            # AAC-LC: 1024 samples/frame. If a different audio codec is ever
            # used, this must be made codec-aware -- do not assume AAC.
            audio_params = AudioTrackParams(
                sample_rate=sample_rate, samples_per_frame=1024
            )
    if video_track_id is None:
        raise ValidationError(f"No video stream found in {ts_file}")
    return video_track_id, audio_track_id, audio_params


def _numeric_segment_index(path: Path) -> int:
    """Extract the trailing numeric segment index from a GPAC-produced
    fragment filename, e.g. `..._track256_5.m4s` -> 5."""
    stem = path.stem
    tail = stem.rsplit("_", 1)[-1]
    return int(tail)


def compute_total_loop_duration_ticks(
    output_dir: Path,
    video_track_id: int,
    timescale: int = TIMESCALE,
) -> int:
    """Read back the exact end tick of the last baked segment from the
    actual produced output (ground truth) -- never trust a nominal/expected
    duration (SCOPE.md §4.1 step 4).

    Ground truth is obtained by concatenating the video track's init segment
    with its last media segment (valid, since CMAF media segments are
    self-contained fragments referencing the init segment's moov) and
    reading the resulting exact duration back via ffprobe. This was
    cross-checked against manual `MP4Box -diso` box-tree inspection (summing
    each fragment's `tfhd` SampleDuration * `trun` SampleCount) and produced
    an identical result against real franken-ts output -- ffprobe is used
    here because it's robust to internal per-fragment box layout details
    (e.g. whether every fragment repeats a `tfdt`), rather than because it's
    presumed correct.
    """
    init_candidates = sorted(output_dir.glob(f"*track{video_track_id}_init.mp4"))
    if not init_candidates:
        raise RuntimeError(
            f"No init segment found for video track {video_track_id} in {output_dir}"
        )
    init_path = init_candidates[0]

    segments = sorted(
        output_dir.glob(f"*track{video_track_id}_*.m4s"),
        key=_numeric_segment_index,
    )
    if not segments:
        raise RuntimeError(
            f"No .m4s media segments found for video track {video_track_id} in {output_dir}"
        )
    last_segment = segments[-1]

    combined_bytes = init_path.read_bytes() + last_segment.read_bytes()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(combined_bytes)
        tmp_path = Path(tmp.name)

    try:
        result = _run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration",
                "-of", "json",
                str(tmp_path),
            ]
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams or "duration" not in streams[0]:
        raise RuntimeError(
            "Failed to compute total_loop_duration_ticks from produced "
            "segments -- ffprobe could not determine duration of the "
            "concatenated init+last-segment file. This is a hard failure "
            "since §5's drift-freedom argument depends on this value being "
            "exactly correct."
        )

    duration_seconds = float(streams[0]["duration"])
    total_ticks = round(duration_seconds * timescale)

    logger.info("total_loop_duration_ticks (ground truth) = %d", total_ticks)
    return total_ticks


def probe_source_duration_ticks(ts_file: Path, timescale: int = TIMESCALE) -> int:
    """Return the source .ts's total content duration, in ticks, via
    ffprobe. Used only to size the nominal segmentation grid (see
    gpac_pipeline.compute_segment_boundary_ticks) -- NOT trusted for
    total_loop_duration_ticks itself, which always comes from reading back
    the actual produced segments (SCOPE.md §4.1 step 4)."""
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "format=duration",
            "-of", "json",
            str(ts_file),
        ]
    )
    data = json.loads(result.stdout)
    duration_seconds = float(data["format"]["duration"])
    return round(duration_seconds * timescale)


def read_segment_boundary_ticks(segments_dir: Path, video_track_id: int) -> list[int]:
    """Read back the exact start tick of every produced video segment, in
    segment order, from the actual output (ground truth) -- never trust the
    requested cues grid blindly (SCOPE.md §4.1 step 4 applies here too:
    GPAC's cues=...:cts mode can silently snap a *nominal* grid point to the
    nearest available keyframe, which is expected/harmless for grid points,
    but the caller must still know the real resulting boundaries, and must
    separately verify marker ticks snapped with zero offset -- see
    `bake()`)."""
    segments = sorted(
        segments_dir.glob(f"*track{video_track_id}_*.m4s"), key=_numeric_segment_index
    )
    if not segments:
        raise RuntimeError(
            f"No .m4s media segments found for video track {video_track_id} in {segments_dir}"
        )
    if shutil.which("MP4Box") is None:
        raise RuntimeError("MP4Box not found on PATH (needed to read back segment tfdt values)")

    boundaries: list[int] = []
    for seg in segments:
        result = _run(["MP4Box", "-diso", "-std", str(seg)])
        match = re.search(r'baseMediaDecodeTime="(\d+)"', result.stdout)
        if not match:
            raise RuntimeError(
                f"Could not find a tfdt/baseMediaDecodeTime box in {seg} -- "
                f"cannot determine this segment's real start tick."
            )
        boundaries.append(int(match.group(1)))

    if boundaries != sorted(boundaries):
        raise RuntimeError(
            f"Produced video segments are not in increasing tick order: "
            f"{boundaries} -- something is wrong with segment numbering."
        )

    return boundaries


def read_variant_metadata(gpac_mpd_path: Path) -> dict:
    """Extract the RFC 6381 codec string, resolution, frame rate, and
    bandwidth GPAC itself computed for the video (and audio, if present)
    Representation in its own generated manifest.mpd.

    This is the only thing GPAC's own generated DASH manifest is used for
    in this tool -- never its EventStream/marker signaling (SCOPE.md §6) --
    because deriving an exact RFC 6381 codec string (e.g. "avc1.640028"
    from profile_idc/constraint_flags/level_idc) independently via ffprobe
    would just be re-deriving what GPAC already computed correctly while
    building the real segments; this reads that computed value back rather
    than duplicating that logic.

    Needed for the HLS multivariant playlist (#EXT-X-STREAM-INF), which
    requires BANDWIDTH/CODECS/RESOLUTION/FRAME-RATE attributes that a plain
    media playlist doesn't carry.
    """
    from xml.etree import ElementTree as ET

    ns = {"m": "urn:mpeg:dash:schema:mpd:2011"}
    tree = ET.parse(gpac_mpd_path)
    root = tree.getroot()

    video: dict | None = None
    audio: dict | None = None

    for adaptation_set in root.findall(".//m:AdaptationSet", ns):
        mime = adaptation_set.get("mimeType", "")
        representation = adaptation_set.find("m:Representation", ns)
        if representation is None:
            continue

        if mime.startswith("video/"):
            video = {
                "codecs": representation.get("codecs"),
                "width": int(representation.get("width")),
                "height": int(representation.get("height")),
                "frame_rate": float(representation.get("frameRate")),
                "bandwidth": int(representation.get("bandwidth")),
            }
        elif mime.startswith("audio/"):
            audio = {
                "codecs": representation.get("codecs"),
                "bandwidth": int(representation.get("bandwidth")),
            }

    if video is None:
        raise RuntimeError(
            f"Could not find a video Representation in {gpac_mpd_path} -- "
            f"needed for the HLS multivariant playlist's #EXT-X-STREAM-INF "
            f"attributes."
        )

    return {"video": video, "audio": audio}


def discover_renditions(
    input_path: Path, markers_override: Path | None = None
) -> tuple[list[tuple[str, Path]], Path]:
    """Discover the rendition ladder + shared markers.json from a single CLI
    argument, per the loop-dee-loop <-> franken-ts directory contract:

        outputs/mychannel/
          markers.json      <- exactly one, shared
          1080p.ts
          720p.ts
          360p.ts

    `input_path` may be:
      - a directory: every `*.ts` file in it is a rendition (name = filename
        stem, e.g. "1080p.ts" -> "1080p"), sorted alphabetically (first
        becomes the reference rendition -- see bake()). `markers.json` must
        exist directly inside it (fixed name, not pattern-matched).
      - a single `.ts` file (legacy/quick-test escape hatch): one rendition
        named after the file's own stem, markers.json defaults to the same
        franken-ts `.with_suffix(".markers.json")` convention as before.

    No CLI flag is needed to enumerate renditions -- that's the whole point
    (SCOPE.md discussion): the file set on disk *is* the ladder.
    """
    if input_path.is_dir():
        markers_json = markers_override or (input_path / "markers.json")
        ts_files = sorted(input_path.glob("*.ts"))
        if not ts_files:
            raise ValidationError(f"No .ts files found in directory {input_path}")
        renditions = [(f.stem, f) for f in ts_files]
    else:
        markers_json = markers_override or input_path.with_suffix(".markers.json")
        renditions = [(input_path.stem, input_path)]

    if not markers_json.exists():
        raise ValidationError(
            f"{markers_json} does not exist. franken-ts must emit a "
            f"markers.json sidecar (SCOPE.md §2); loop-dee-loop never "
            f"re-derives marker timing by re-probing the .ts."
        )

    logger.info(
        "Discovered %d rendition(s) in %s: %s",
        len(renditions), input_path, [name for name, _ in renditions],
    )
    return renditions, markers_json


def bake_one_rendition(
    name: str,
    ts_file: Path,
    raw_markers: list[dict],
    output_package_dir: Path,
    *,
    segment_duration_seconds: float,
    dry_run: bool,
    include_audio: bool,
) -> dict | None:
    """Bake a single rendition's .ts into its own segment set under
    `<output_package_dir>/segments/<name>/`. Returns a rendition result
    dict for loop_descriptor.json's `video_renditions` list, or None if
    dry_run (nothing real was produced to describe).

    `include_audio`: every rendition's audio track is baked regardless
    (GPAC's dasher errors if asked to process a PID present in the input
    with no cues for it -- there's no clean way to tell it to just ignore
    a track), but only the reference rendition's audio is *recorded* in
    the returned result / loop_descriptor.json. Audio content is identical
    across renditions in practice (franken-ts extracts audio independently
    of the video resolution/bitrate ladder), so non-reference renditions'
    baked audio segments are harmless, unreferenced disk usage -- a known
    storage-deduplication opportunity to revisit later, not a correctness
    issue.
    """
    logger.info("── Rendition '%s' (%s) ──", name, ts_file)

    decoded = decode_embedded_scte35(ts_file)
    validated_markers = validate_markers_against_ts(raw_markers, decoded)

    video_track_id, audio_track_id, audio_params = read_actual_track_params(ts_file)

    rendition_dir = output_package_dir / "segments" / name
    cues_xml_path = output_package_dir / "cues" / f"{name}.xml"
    cues_xml_path.parent.mkdir(parents=True, exist_ok=True)

    nominal_segment_ticks = round(segment_duration_seconds * TIMESCALE)
    total_content_ticks = probe_source_duration_ticks(ts_file)
    marker_ticks = [m["pts_time_ticks"] for m in validated_markers]
    requested_boundary_ticks = compute_segment_boundary_ticks(
        total_content_ticks,
        nominal_segment_ticks,
        marker_ticks,
        end_guard_ticks=TIMESCALE,
    )
    logger.info(
        "Rendition '%s': requesting %d segment boundaries (nominal ~%.1fs "
        "grid + %d marker tick(s) forced in)",
        name, len(requested_boundary_ticks), segment_duration_seconds, len(marker_ticks),
    )

    build_cues_xml(
        requested_boundary_ticks,
        cues_xml_path,
        video_track_id=video_track_id,
        audio_track_id=audio_track_id,
        audio_params=audio_params,
    )

    run_gpac_dasher(
        ts_file,
        cues_xml_path,
        rendition_dir,
        segment_duration_seconds=segment_duration_seconds,
        dry_run=dry_run,
    )

    if dry_run:
        return None

    total_loop_duration_ticks = compute_total_loop_duration_ticks(rendition_dir, video_track_id)
    segment_boundary_ticks = read_segment_boundary_ticks(rendition_dir, video_track_id)

    unmatched_markers = [t for t in marker_ticks if t not in segment_boundary_ticks]
    if unmatched_markers:
        raise RuntimeError(
            f"Rendition '{name}': marker tick(s) {unmatched_markers} did not "
            f"land exactly on a produced segment boundary -- GPAC silently "
            f"snapped a marker cue to a different frame (SCOPE.md §6). This "
            f"is a hard failure: ad signaling must be frame-accurate, never "
            f"approximate. Real boundaries produced: {segment_boundary_ticks}"
        )

    audio_segment_boundary_ticks: list[int] | None = None
    if audio_track_id is not None:
        audio_segment_boundary_ticks = read_segment_boundary_ticks(rendition_dir, audio_track_id)
        if len(audio_segment_boundary_ticks) != len(segment_boundary_ticks):
            raise RuntimeError(
                f"Rendition '{name}': audio track produced "
                f"{len(audio_segment_boundary_ticks)} segment(s) but video "
                f"track produced {len(segment_boundary_ticks)} -- they must "
                f"match 1:1. Real audio boundaries: {audio_segment_boundary_ticks}"
            )

    variant_metadata = read_variant_metadata(rendition_dir / "manifest.mpd")

    logger.info(
        "Rendition '%s': produced %d real segment(s); all %d marker "
        "tick(s) landed exactly on a segment boundary",
        name, len(segment_boundary_ticks), len(marker_ticks),
    )

    return {
        "name": name,
        "video_track_id": video_track_id,
        "audio_track_id": audio_track_id if include_audio else None,
        "total_loop_duration_ticks": total_loop_duration_ticks,
        "segment_boundary_ticks": segment_boundary_ticks,
        "audio_segment_boundary_ticks": audio_segment_boundary_ticks if include_audio else None,
        "video_variant": variant_metadata["video"],
        "audio_variant": variant_metadata["audio"] if include_audio else None,
        "markers": validated_markers,
    }


def bake(
    input_path: Path,
    output_package_dir: Path,
    *,
    segment_duration_seconds: float = 4.0,
    dry_run: bool = False,
    markers_override: Path | None = None,
) -> None:
    """Bake phase entrypoint (SCOPE.md §4.1), generalized to a rendition
    ladder auto-discovered from disk (see discover_renditions()).

    A single-rendition input degenerates naturally into a ladder of one --
    no special-casing needed anywhere below this point.
    """
    logger.info("Bake starting: %s -> %s", input_path, output_package_dir)

    renditions, markers_json = discover_renditions(input_path, markers_override)
    raw_markers = load_markers(markers_json)

    output_package_dir.mkdir(parents=True, exist_ok=True)

    rendition_results: list[dict] = []
    reference: dict | None = None

    for i, (name, ts_file) in enumerate(renditions):
        result = bake_one_rendition(
            name,
            ts_file,
            raw_markers,
            output_package_dir,
            segment_duration_seconds=segment_duration_seconds,
            dry_run=dry_run,
            include_audio=(i == 0),
        )
        if dry_run:
            continue

        if reference is None:
            reference = result
        elif result["total_loop_duration_ticks"] != reference["total_loop_duration_ticks"]:
            raise RuntimeError(
                f"Rendition '{name}' total_loop_duration_ticks="
                f"{result['total_loop_duration_ticks']} does not match "
                f"reference rendition '{reference['name']}'="
                f"{reference['total_loop_duration_ticks']}. All renditions "
                f"in a ladder must share exactly the same loop duration -- "
                f"hard failure, not reconciled."
            )
        elif result["markers"] != reference["markers"]:
            raise RuntimeError(
                f"Rendition '{name}' decoded SCTE-35 markers do not match "
                f"reference rendition '{reference['name']}''s markers "
                f"byte-for-byte. All renditions must carry identical "
                f"marker timing/content -- hard failure, not reconciled."
            )

        rendition_results.append(result)

    if dry_run:
        logger.warning("dry-run: skipping loop_descriptor.json (no real segments produced)")
        return

    assert reference is not None

    # Step 6: write the immutable loop package.
    loop_descriptor = {
        "version": 2,
        "created_at": time.time(),
        "timescale": TIMESCALE,
        "total_loop_duration_ticks": reference["total_loop_duration_ticks"],
        "segment_duration_seconds": segment_duration_seconds,
        "markers": reference["markers"],
        "video_renditions": [
            {k: v for k, v in r.items() if k != "markers"} for r in rendition_results
        ],
        "source_input": str(input_path),
        "source_markers_json": str(markers_json),
    }
    descriptor_path = output_package_dir / "loop_descriptor.json"
    with descriptor_path.open("w", encoding="utf-8") as f:
        json.dump(loop_descriptor, f, indent=2)
        f.write("\n")

    logger.info(
        "Bake complete. %d rendition(s) written to %s",
        len(rendition_results), output_package_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bake franken-ts output (single .ts, or a rendition-ladder "
        "directory) into a loop package"
    )
    parser.add_argument(
        "input_path",
        type=Path,
        help="Path to a franken-ts .ts file, OR a directory containing a "
        "rendition ladder (markers.json + one *.ts per rendition -- see "
        "README.md)",
    )
    parser.add_argument(
        "--markers",
        type=Path,
        default=None,
        help="Override the auto-discovered markers.json path",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output loop package directory")
    parser.add_argument("--segment-duration", type=float, default=4.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        bake(
            args.input_path,
            args.output,
            segment_duration_seconds=args.segment_duration,
            dry_run=args.dry_run,
            markers_override=args.markers,
        )
    except ValidationError as exc:
        logger.error("VALIDATION FAILED: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("bake failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
