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

import cmaf
from gpac_pipeline import (
    AudioTrackParams,
    build_cues_xml,
    compute_segment_boundary_ticks,
    run_gpac_dasher,
)
from scte35_signaling import (
    DATERANGE_ID_FORMAT_DEFAULT,
    SCTE35_EVENT_ID_MAX,
    compute_event_id_step,
    validate_daterange_id_format,
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
    segmentation_type_id: int | None = None


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


def decode_embedded_scte35(ts_file: Path, *, narrow_descriptors: bool = False) -> list[DecodedMarker]:
    """Decode every SCTE-35 splice_info_section actually embedded in the
    .ts using threefive, independent of markers.json, for cross-validation.

    threefive is used here as recommended (SCOPE.md §4.1 step 1: "already
    proven to round-trip franken-ts's cues correctly in prototyping").

    `narrow_descriptors` (default False, set when `--daterange-mode narrowed`
    / channel config `[markers] daterange_mode = "narrowed"`): whether each
    event's DecodedMarker gets the raw *shared* multi-descriptor message
    (default -- the standard way coincident SCTE-35 events are signaled on
    the wire, and what other systems such as MediaPackage do for
    co-located DATERANGEs), or a re-encoded, single-descriptor payload
    containing only that event's own descriptor. Narrowing is useful when
    a downstream consumer can't cope with more than one segmentation
    descriptor per message, but it is not the default behavior.
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
        b64 = cue.base64()

        # A single splice_information_table message can carry MULTIPLE
        # segmentation descriptors -- franken-ts merges every event
        # coincident at the same PTS into one message (e.g. a Break start +
        # a nested PPO start + a nested Ad start, all at the same instant)
        # instead of emitting one message per event. Every descriptor's
        # event_id is therefore a real, independent marker that must be
        # cross-validated -- NOT just the first one found. By default the
        # same raw `splice_command_b64` -- the whole message's bytes -- is
        # shared by all events from that message: downstream SCTE35-OUT/IN
        # and DASH <Binary> signaling embed the full message regardless of
        # which of its descriptors a given marker corresponds to, which is
        # the standard way coincident events are signaled on the wire (and
        # what other systems, e.g. MediaPackage, do for co-located
        # DATERANGEs too). Passing `narrow_descriptors=True` instead
        # re-encodes a single-descriptor splice_info_section per event_id,
        # keeping only that event's own segmentation descriptor (plus any
        # non-segmentation descriptors, e.g. the avail descriptor) -- an
        # opt-in for downstream consumers that can't cope with more than
        # one segmentation descriptor per message.
        descriptors = list(getattr(cue, "descriptors", []))
        seg_descriptors = [
            descriptor for descriptor in descriptors
            if getattr(descriptor, "segmentation_event_id", None) is not None
        ]
        seg_event_ids = [getattr(descriptor, "segmentation_event_id") for descriptor in seg_descriptors]
        segmentation_type_by_event = {}
        for descriptor in seg_descriptors:
            descriptor_event_id = getattr(descriptor, "segmentation_event_id")
            descriptor_type_id = getattr(descriptor, "segmentation_type_id", None)
            if descriptor_type_id is not None:
                segmentation_type_by_event[descriptor_event_id] = descriptor_type_id

        if not seg_event_ids:
            # splice_insert carries its own splice_event_id directly on the
            # command (no segmentation descriptors at all) -- already a
            # single-event message, nothing to split.
            event_id = getattr(cue.command, "splice_event_id", None)
            if event_id is None:
                raise ValidationError(
                    "decoded SCTE-35 message has no event_id on either its "
                    "segmentation descriptor(s) or splice command -- cannot "
                    "cross-validate against markers.json."
                )
            seg_event_ids = [event_id]
            per_event_b64 = {event_id: b64}
        elif not narrow_descriptors or len(seg_event_ids) == 1:
            # Default: every event sharing this message gets the same raw,
            # shared bytes (or there's only one descriptor to begin with,
            # so the shared message already is that event's own payload).
            per_event_b64 = {eid: b64 for eid in seg_event_ids}
        else:
            per_event_b64 = {}
            for eid in seg_event_ids:
                narrowed = threefive.Cue(b64)
                narrowed.decode()
                narrowed.descriptors = [
                    d
                    for d in narrowed.descriptors
                    if getattr(d, "segmentation_event_id", None) in (None, eid)
                ]
                per_event_b64[eid] = narrowed.encode()

        for event_id in seg_event_ids:
            event_id_str = (
                event_id
                if isinstance(event_id, str) and event_id.startswith("0x")
                else f"0x{int(event_id):08X}"
            )
            # Normalize to the same fixed-width 0x%08X form markers.json uses.
            event_id_str = f"0x{int(event_id_str, 16):08X}"

            decoded.append(
                DecodedMarker(
                    event_id=event_id_str,
                    pts_time_ticks=ticks,
                    splice_command_b64=per_event_b64[event_id],
                    segmentation_type_id=(
                        int(segmentation_type_by_event[event_id], 16)
                        if isinstance(segmentation_type_by_event.get(event_id), str)
                        else segmentation_type_by_event.get(event_id)
                    ),
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


def validate_cue_tags_only(raw_markers: list[dict]) -> None:
    """`[markers] cue_tags = "only"` (see its-a-live/AGENTS.md) drops
    DATERANGE entirely in favor of #EXT-X-CUE-OUT/-CONT/-IN -- there is no
    DATERANGE fallback in that mode, so every marker must be a bare
    `splice_insert` (the only splice_type serve.py's cue-tag path knows
    how to derive a duration/pairing for without a segmentation
    descriptor). Hard-fails otherwise, same style as
    `validate_markers_against_ts`."""
    non_splice_insert = sorted(
        m["event_id"] for m in raw_markers if m.get("splice_type") != "splice_insert"
    )
    if non_splice_insert:
        raise ValidationError(
            f"cue_tags='only' requires every marker to be a bare "
            f"splice_insert (no DATERANGE fallback exists in this mode), "
            f"but event(s) {non_splice_insert} use a different splice_type. "
            f"Hard failure -- not reconciled."
        )


def validate_increment_event_ids(raw_markers: list[dict]) -> None:
    """`[markers] increment_event_ids = true` derives one shared step
    (`scte35_signaling.compute_event_id_step`) -- the smallest power of
    10 above the channel's largest base event id -- and bumps every
    marker's id by `loop_number * step` at serve time.

    If that largest base id already sits close enough to the 32-bit
    SCTE-35 ceiling that even its own next decade exceeds it, there is
    no room left for a single real increment:
    `compute_incremented_event_id`'s wraparound math (`max_loop_number =
    (SCTE35_EVENT_ID_MAX - base) // step`) degenerates to 0, so `loop_number
    % (max_loop_number + 1)` is always 0 and every loop silently emits the
    exact same id as loop 0 -- i.e. the setting would quietly do nothing.
    Hard-fail instead of shipping a loop package where that's the case,
    same style as `validate_cue_tags_only`."""
    event_ids = [m["event_id"] for m in raw_markers]
    if not event_ids:
        return
    step = compute_event_id_step(event_ids)
    max_base_id = max(int(eid, 16) for eid in event_ids)
    if step > SCTE35_EVENT_ID_MAX - max_base_id:
        raise ValidationError(
            f"increment_event_ids=true derives step={step} (smallest power "
            f"of 10 above the largest base event id, "
            f"0x{max_base_id:08X}={max_base_id}), which leaves no room "
            f"under the 32-bit SCTE-35 ceiling "
            f"(0x{SCTE35_EVENT_ID_MAX:08X}={SCTE35_EVENT_ID_MAX}) for even "
            f"one real increment -- every loop would silently emit the "
            f"exact same id as loop 0. Base event ids are too close to the "
            f"32-bit ceiling for this feature. Hard failure -- not "
            f"silently ignored."
        )


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
    markers_by_key = _with_occurrence(
        markers, lambda m: m["event_id"], lambda m: m["pts_time_ticks"],
    )

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
        expected_type_id = marker.get("segmentation_type_id")
        if expected_type_id is not None:
            expected_type_id_int = (
                int(expected_type_id, 16) if isinstance(expected_type_id, str) else expected_type_id
            )
            if expected_type_id_int != decoded_marker.segmentation_type_id:
                actual_type = (
                    f"0x{decoded_marker.segmentation_type_id:02X}"
                    if decoded_marker.segmentation_type_id is not None
                    else "missing"
                )
                raise ValidationError(
                    f"event {marker['event_id']}: markers.json declares "
                    f"segmentation_type_id=0x{expected_type_id_int:02X} but the .ts's "
                    f"decoded SCTE-35 has segmentation_type_id={actual_type}. "
                    "Hard failure -- not reconciling."
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


def _iter_top_level_boxes(data: bytes) -> list[tuple[str, int, int]]:
    """Return [(box_type, start_offset, size), ...] for every top-level
    ISOBMFF box in `data`. Only handles the standard 32-bit size field
    (with size==0 meaning "rest of data") -- sufficient for the small
    fragmented-mp4 structures GPAC produces here; a 64-bit largesize box
    would be unexpected in a single ~4s CMAF segment and is not handled."""
    boxes: list[tuple[str, int, int]] = []
    i = 0
    n = len(data)
    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8].decode("latin1", errors="replace")
        if size == 0:
            size = n - i
        elif size == 1:
            raise RuntimeError(
                f"64-bit largesize box ({typ!r} at offset {i}) not supported "
                f"by the malformed-fragment repair scan"
            )
        if size < 8 or i + size > n:
            break
        boxes.append((typ, i, size))
        i += size
    return boxes


def _moof_has_tfdt(moof_bytes: bytes) -> bool:
    """Whether a `moof` box's (only) `traf` contains a `tfdt` box."""
    payload = moof_bytes[8:]
    for typ, start, size in _iter_top_level_boxes(payload):
        if typ == "traf":
            traf_payload = payload[start + 8:start + size]
            for t2, _s2, _sz2 in _iter_top_level_boxes(traf_payload):
                if t2 == "tfdt":
                    return True
    return False


def repair_trailing_malformed_fragment(segment_path: Path) -> bool:
    """Detect and strip a spurious trailing movie-fragment (`moof`+`mdat`
    pair) from a GPAC-produced CMAF media segment, if present. Returns True
    if the file was rewritten.

    Observed defect (confirmed by hand -- see chat history for the full
    investigation, reproduced independently of any Period/manifest/HLS
    logic via a bare `SourceBuffer.appendBuffer()` test): the very LAST
    segment GPAC produces for a *finite* input file sometimes contains not
    one but TWO `moof`+`mdat` pairs concatenated in the same `.m4s` file --
    a normal, fully-formed first fragment (188 audio samples, matching
    every other segment) immediately followed by a second, malformed
    fragment (observed: only 2 samples, and critically, missing its own
    `tfdt`/`TrackFragmentBaseMediaDecodeTimeBox` entirely). This looks like
    an end-of-stream flush artifact in GPAC's live-profile dasher when the
    input stream genuinely ends (as opposed to a real live encoder feed,
    which never does) -- not something introduced by this tool's own
    manifest/Period authoring.

    A `traf` without a `tfdt` is invalid per the fragmented-MP4/CMAF spec,
    and Chrome's MSE demuxer rejects the whole segment outright on append
    (`MEDIA_ERR_DECODE`) rather than merely warning -- which is fatal for a
    looping channel, since every loop iteration re-serves this exact same
    broken byte sequence forever. Trimming the file back to just its
    well-formed leading fragment(s) removes a couple of stray audio samples
    (a few tens of ms) that were never valid content instead of poisoning
    the entire segment.

    Scans strictly forward through the top-level boxes: the first `moof`
    lacking a `tfdt` in its `traf`, and everything from that point on, is
    dropped. If the file only ever contained one `moof`+`mdat` pair (the
    normal case), this is a no-op.
    """
    data = segment_path.read_bytes()
    boxes = _iter_top_level_boxes(data)
    moof_indices = [i for i, (typ, _s, _sz) in enumerate(boxes) if typ == "moof"]
    if len(moof_indices) <= 1:
        return False

    good_end: int | None = None
    for idx in moof_indices:
        typ, start, size = boxes[idx]
        moof_bytes = data[start:start + size]
        if not _moof_has_tfdt(moof_bytes):
            break
        frag_end = start + size
        if idx + 1 < len(boxes) and boxes[idx + 1][0] == "mdat":
            frag_end = boxes[idx + 1][1] + boxes[idx + 1][2]
        good_end = frag_end
    else:
        return False  # every fragment was well-formed; nothing to repair

    if good_end is None:
        raise RuntimeError(
            f"{segment_path}: the FIRST movie fragment in this segment is "
            f"missing its own tfdt box -- refusing to guess a safe repair "
            f"(there is no well-formed content left to keep)."
        )
    if good_end >= len(data):
        return False

    dropped_bytes = len(data) - good_end
    fragments_kept = sum(1 for i in moof_indices if boxes[i][1] < good_end)
    fragments_dropped = len(moof_indices) - fragments_kept
    logger.warning(
        "Repaired malformed trailing movie fragment in %s: dropped %d "
        "trailing byte(s) (%d fragment(s) removed, %d fragment(s) kept) -- "
        "see repair_trailing_malformed_fragment docstring for why this "
        "happens and why it's safe to drop.",
        segment_path, dropped_bytes, fragments_dropped, fragments_kept,
    )
    segment_path.write_bytes(data[:good_end])
    return True


def repair_malformed_segments_in_dir(rendition_dir: Path) -> int:
    """Run `repair_trailing_malformed_fragment` over every `.m4s` media
    segment in `rendition_dir` (both video and audio tracks -- filenames
    are `*_track<id>_<index>.m4s`; init segments (`*_init.mp4`) are
    untouched, they carry no `moof`/`mdat`). Returns the number of segment
    files that needed repair. Must run once, right after GPAC produces the
    segments and before anything reads them back (tfdt boundaries, ffprobe
    duration, etc.) -- a malformed trailing fragment would otherwise
    corrupt those readings too, not just downstream playback."""
    repaired = 0
    for seg_path in sorted(rendition_dir.glob("*.m4s")):
        if repair_trailing_malformed_fragment(seg_path):
            repaired += 1
    if repaired:
        logger.info(
            "Repaired %d segment(s) with a malformed trailing movie "
            "fragment in %s", repaired, rendition_dir,
        )
    return repaired


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


def bake_hls_ts_segments(output_package_dir: Path, renditions: list[dict], *, mux_audio: bool) -> None:
    """Remux the validated CMAF fragments into HLS MPEG-TS segments.

    DASH keeps the original CMAF fragments. Using those same video samples
    ensures HLS TS has precisely the already-verified marker boundaries.
    """
    ts_dir = output_package_dir / "hls-ts"
    if ts_dir.exists():
        shutil.rmtree(ts_dir)
    ts_dir.mkdir(parents=True)

    reference = renditions[0]
    audio_id = reference["audio_track_id"]
    audio_source = output_package_dir / "segments" / reference["name"]
    audio_init = None
    audio_segments = []
    if audio_id is not None:
        audio_init = next(iter(sorted(audio_source.glob(f"*track{audio_id}_init.mp4"))))
        audio_segments = sorted(audio_source.glob(f"*track{audio_id}_*.m4s"), key=_numeric_segment_index)

    audio_init_bytes = audio_init.read_bytes() if audio_init is not None else b""
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        for rendition in renditions:
            name = rendition["name"]
            source = output_package_dir / "segments" / name
            video_id = rendition["video_track_id"]
            video_init = next(iter(sorted(source.glob(f"*track{video_id}_init.mp4"))))
            video_init_bytes = video_init.read_bytes()
            video_segments = sorted(source.glob(f"*track{video_id}_*.m4s"), key=_numeric_segment_index)
            if audio_id is not None and len(video_segments) != len(audio_segments):
                raise RuntimeError(f"Rendition {name!r} has a different segment count from the shared audio")
            destination = ts_dir / name
            destination.mkdir()
            for index, video_segment in enumerate(video_segments):
                video_input = temp_dir / "video.mp4"
                video_input.write_bytes(video_init_bytes + video_segment.read_bytes())
                cmd = ["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(video_input)]
                if mux_audio and audio_id is not None:
                    audio_input = temp_dir / "audio.mp4"
                    audio_input.write_bytes(audio_init_bytes + audio_segments[index].read_bytes())
                    cmd += ["-i", str(audio_input)]
                cmd += ["-map", "0:v:0"]
                if mux_audio and audio_id is not None:
                    cmd += ["-map", "1:a:0"]
                cmd += ["-c", "copy", "-f", "mpegts", str(destination / f"{index}.ts")]
                _run(cmd)

        if not mux_audio and audio_id is not None:
            destination = ts_dir / "audio"
            destination.mkdir()
            for index, audio_segment in enumerate(audio_segments):
                audio_input = temp_dir / "audio.mp4"
                audio_input.write_bytes(audio_init_bytes + audio_segment.read_bytes())
                _run(["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(audio_input),
                      "-map", "0:a:0", "-c", "copy", "-f", "mpegts",
                      str(destination / f"{index}.ts")])


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


SPARSE_RENDITION_NAME = "archive"
"""Fixed single-rendition name used by the segment-list ('sparse') bake
mode (SCOPE.md §11). An archive-derived source has already been reduced to
one canonical reference rendition upstream (grave-robber/SCOPE.md §8 step
5, "residual reference pick") before it ever reaches bake.py -- there is
no ABR ladder concept in this input mode, unlike the normal franken-ts
rendition-directory path. ABR ladder support for sparse input is a known
future extension, not attempted here."""


def load_segment_list_manifest(path: Path) -> dict:
    """Parse + validate the segment-list manifest shape (SCOPE.md §11.2):

        {"segments": [{"index", "duration_ticks", "asset_boundary",
                       "media_file"}, ...],
         "markers": [...]}   # same .markers.json shape as §2

    Hard-fails (ValidationError) on any structural problem -- same
    fail-loud-by-default posture as every other bake.py input check. Does
    NOT check media_file presence/absence here -- that's
    validate_segment_list_missing_media's job (needs the
    --allow-missing-segments flag to decide the outcome).
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data.get("segments"), list) or not data["segments"]:
        raise ValidationError(
            f"{path}: segment-list manifest must have a non-empty top-level "
            f"'segments' list (SCOPE.md §11.2)"
        )
    if not isinstance(data.get("markers"), list):
        raise ValidationError(
            f"{path}: segment-list manifest must have a top-level 'markers' "
            f"list (may be empty) -- same .markers.json shape as SCOPE.md §2"
        )

    segments = data["segments"]
    required_fields = ("index", "duration_ticks", "asset_boundary", "media_file")
    for i, seg in enumerate(segments):
        missing_fields = [f for f in required_fields if f not in seg]
        if missing_fields:
            raise ValidationError(
                f"{path}: segments[{i}] is missing required field(s) "
                f"{missing_fields} (SCOPE.md §11.2)"
            )
        if seg["index"] != i:
            raise ValidationError(
                f"{path}: segments[{i}]['index']={seg['index']!r}, expected "
                f"{i} -- entries must be ordered 0..N-1 with no gaps or "
                f"duplicates in the index sequence itself (a missing SEGMENT "
                f"is expressed via media_file=null, never by skipping an "
                f"index)."
            )
        if not isinstance(seg["duration_ticks"], int) or seg["duration_ticks"] <= 0:
            raise ValidationError(
                f"{path}: segments[{i}]['duration_ticks'] must be a strictly "
                f"positive int, got {seg['duration_ticks']!r}"
            )
        if not isinstance(seg["asset_boundary"], bool):
            raise ValidationError(
                f"{path}: segments[{i}]['asset_boundary'] must be a bool, "
                f"got {seg['asset_boundary']!r}"
            )
        media_file = seg["media_file"]
        if media_file is not None and not isinstance(media_file, str):
            raise ValidationError(
                f"{path}: segments[{i}]['media_file'] must be a string path "
                f"or null, got {media_file!r}"
            )
        # Optional field, additive to §11.2's documented shape (needed by
        # grave-robber/SCOPE.md §6.2's declared-vs-serving position split --
        # a signed tick offset recorded at an asset-boundary segment, dead
        # time if positive / overlap if negative). Absent or 0 everywhere
        # is the common case (no internal asset joins at all, e.g. a plain
        # franken-ts-authored source), which round-trips as a no-op.
        gap_ticks = seg.get("gap_ticks", 0)
        if not isinstance(gap_ticks, int):
            raise ValidationError(
                f"{path}: segments[{i}]['gap_ticks'] must be an int, got {gap_ticks!r}"
            )
        if gap_ticks != 0 and not seg["asset_boundary"]:
            raise ValidationError(
                f"{path}: segments[{i}] declares gap_ticks={gap_ticks} but "
                f"asset_boundary=False -- a gap/overlap only means anything "
                f"at an asset boundary."
            )

    return data


def validate_segment_list_missing_media(
    segments: list[dict], *, allow_missing_segments: bool
) -> list[int]:
    """Returns the list of segment indices with no media_file. Hard-fails
    unless `allow_missing_segments` is set (SCOPE.md §11.2: "Default:
    hard-fail... opt-in, never the silent default")."""
    missing = [s["index"] for s in segments if s["media_file"] is None]
    if missing and not allow_missing_segments:
        raise ValidationError(
            f"{len(missing)} segment(s) have no media_file (index(es): "
            f"{missing}) -- pass --allow-missing-segments to bake a "
            f"manifest-complete, media-optional package (SCOPE.md §11.1). "
            f"Hard failure by default, same fail-loud posture as every "
            f"other bake.py validation."
        )
    return missing


def compute_segment_list_boundary_ticks(segments: list[dict]) -> list[int]:
    """Exclusive-prefix-sum of duration_ticks -- the start tick of each
    segment, in the same shape VideoRendition.segment_boundary_ticks
    expects. Ground truth here is the declared ledger itself (there is no
    "read it back from produced media" step to cross-check against, unlike
    the normal .ts path's compute_total_loop_duration_ticks -- SCOPE.md
    §11.2 is explicit that the ledger is always complete regardless of
    which segments have real media)."""
    boundaries = []
    running = 0
    for seg in segments:
        boundaries.append(running)
        running += seg["duration_ticks"]
    return boundaries


def compute_asset_boundary_indices(segments: list[dict]) -> list[int]:
    return [s["index"] for s in segments if s["asset_boundary"]]


def compute_asset_boundary_gap_ticks(segments: list[dict]) -> dict[str, int]:
    """{str(index): gap_ticks} for every asset-boundary segment with a
    nonzero declared gap/overlap -- feeds serve.py's declared-position
    accumulator (grave-robber/SCOPE.md §6.2). String keys because this is
    serialized straight into loop_descriptor.json (JSON object keys are
    always strings; serve.py's LoopPackage.__init__ converts them back to
    int on load)."""
    return {
        str(s["index"]): s["gap_ticks"]
        for s in segments
        if s["asset_boundary"] and s.get("gap_ticks", 0) != 0
    }


def remux_segment_to_fragmented_mp4(src: Path, dest: Path, *, stream: str = "v") -> None:
    """Container-only remux (ffmpeg stream copy -- no transcode) of one
    archive-extracted segment into a fragmented MP4 with a 90 kHz video track
    timescale (so `tfdt` values are directly loop-ledger ticks). Video only:
    sparse mode has no audio Representation (SCOPE.md §11 open item)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(src),
            "-map", f"0:{stream}:0",
            "-c", "copy",
            *(["-video_track_timescale", str(TIMESCALE)] if stream == "v" else ["-bsf:a", "aac_adtstoasc"]),
            # delay_moov (audio): with a plain empty_moov the moov is written before
            # aac_adtstoasc has produced the AudioSpecificConfig, so the esds ends up
            # without it and browsers reject the init segment.
            "-movflags", "frag_keyframe+empty_moov+default_base_moof" + ("" if stream == "v" else "+delay_moov"),
            "-f", "mp4",
            str(dest),
        ]
    )


def remux_segment_to_self_initializing_fragment(src: Path, dest: Path) -> None:
    """Container-only remux (ffmpeg stream copy -- no transcode, per
    SCOPE.md §1's non-goal) of one archive-extracted segment file into a
    standalone fragmented-MP4 file carrying its own moov -- i.e. playable
    without a separate shared init segment.

    This is needed because sparse-mode segments come from independently
    captured archive entries with no guarantee they share one common
    encoder init the way a single continuous franken-ts encode does (the
    normal bake path always has exactly one shared init per rendition,
    produced once by GPAC's dasher over the whole file). Each sparse
    segment is therefore made self-describing instead.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(src),
            # Video only: sparse mode has no separate audio track/
            # Representation concept (SCOPE.md §11 open item -- a known,
            # documented v1 limitation, not attempted here). A muxed
            # TS-sourced segment would otherwise carry an audio stream this
            # package never declares in its manifest.
            "-map", "0:v:0",
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            str(dest),
        ]
    )


TS_BASE_SECONDS = 1.4  # PTS of a loop's first segment (ffmpeg's usual mpegts mux delay)


def _container_start_seconds(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", str(path)])
    return float(out.stdout.strip().splitlines()[0])


def remux_segment_to_ts(
    src: Path, dest: Path, *, stream: str | None = None, start_seconds: float | None = None
) -> None:
    """Container-only remux to self-contained MPEG-TS (no init-segment
    concept at all) -- for hls_format='ts' output, same ffmpeg stream-copy
    style bake_hls_ts_segments already uses for the normal path's CMAF->TS
    derivation."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # stream="v"/"a": keep only that stream (unmuxed HLS TS: video-only and audio-only files).
    select = ["-map", f"0:{stream}:0"] if stream else []
    # start_seconds: place the segment's first timestamp at its position on the loop
    # timeline (independently captured segments otherwise each restart near 0, which
    # HLS players read as an unsignalled discontinuity). -copyts keeps the source's
    # timestamps so one offset moves every stream together, preserving A/V sync.
    timing = []
    if start_seconds is not None:
        timing = ["-copyts", "-muxdelay", "0", "-muxpreload", "0"]
    _run(
        ["ffmpeg", "-v", "error", "-y", *timing[:1], "-i", str(src), *select, "-c", "copy", *timing[1:],
         *(["-output_ts_offset", str(start_seconds - _container_start_seconds(src))] if start_seconds is not None else []),
         "-f", "mpegts", str(dest)]
    )


# RFC 6381 profile_idc byte for the H.264 profile names ffprobe reports.
# Best-effort only -- see probe_segment_variant_metadata's docstring.
_AVC_PROFILE_IDC = {
    "Constrained Baseline": 0x42,
    "Baseline": 0x42,
    "Main": 0x4D,
    "Extended": 0x58,
    "High": 0x64,
    "High 10": 0x6E,
    "High 4:2:2": 0x7A,
    "High 4:4:4 Predictive": 0xF4,
}


def _rfc6381_avc1_codec_string(profile: str, level: float) -> str:
    """Best-effort RFC 6381 'avc1.PPCCLL' codec string from ffprobe's
    reported profile name + level. Constraint-set flag byte is assumed 0
    (ffprobe doesn't expose the individual constraint_set flags) -- this is
    a known simplification, not a guaranteed byte-exact match to the
    source encoder's real SPS (SCOPE.md §11's "not yet spiked against a
    real archive" applies here: the normal bake path avoids this whole
    problem by reading GPAC's own computed value back instead of
    re-deriving it, which isn't available in sparse mode since no dasher
    pass runs over the source)."""
    profile_idc = _AVC_PROFILE_IDC.get(profile)
    if profile_idc is None:
        raise RuntimeError(
            f"Unrecognized H.264 profile {profile!r} for RFC 6381 codec "
            f"string derivation -- sparse-mode metadata probing only "
            f"supports H.264 today."
        )
    level_idc = round(level * 10)
    return f"avc1.{profile_idc:02X}00{level_idc:02X}"


def apply_declared_variant(video: dict, declared: dict | None) -> None:
    """Prefer the exact video codec string / bandwidth the source's own
    multivariant playlist declared (grave-robber's optional `variant` key)
    over the ffprobe-derived approximation. The declared `codecs` may also
    list audio (e.g. "avc1.4D401F,mp4a.40.2"); sparse mode is video-only, so
    only the video entry is taken."""
    if not declared:
        return
    codecs = [c.strip() for c in (declared.get("codecs") or "").split(",")]
    video_codec = next((c for c in codecs if c.startswith(("avc1", "avc3", "hvc1", "hev1"))), None)
    if video_codec:
        video["codecs"] = video_codec
    if declared.get("bandwidth"):
        video["bandwidth"] = int(declared["bandwidth"])


def probe_has_audio(path: Path) -> bool:
    result = _run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)]
    )
    return bool(result.stdout.strip())


def probe_audio_variant(path: Path) -> dict:
    """{"codecs", "bandwidth"} of the first audio stream (best effort: AAC
    profile -> RFC 6381 object type; AC-3/E-AC-3 by name)."""
    data = json.loads(
        _run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,profile,bit_rate", "-show_entries", "format=bit_rate",
             "-of", "json", str(path)]
        ).stdout
    )
    stream = (data.get("streams") or [{}])[0]
    name, profile = stream.get("codec_name"), stream.get("profile") or ""
    if name == "aac":
        codecs = {"HE-AAC": "mp4a.40.5", "HE-AACv2": "mp4a.40.29"}.get(profile, "mp4a.40.2")
    elif name in ("ac3", "eac3"):
        codecs = "ac-3" if name == "ac3" else "ec-3"
    else:
        raise RuntimeError(f"Unsupported audio codec {name!r} in {path} for sparse-mode CMAF audio")
    bandwidth = int(stream.get("bit_rate") or data.get("format", {}).get("bit_rate") or 128_000)
    return {"codecs": codecs, "bandwidth": bandwidth}


def read_avcc_codec_string(path: Path) -> str | None:
    """Exact RFC 6381 'avc1.PPCCLL' string read from the fMP4's own `avcC`
    box (profile_idc, constraint flags, level_idc as the encoder wrote them)
    -- no guessing. None if the file has no avcC (e.g. not H.264)."""
    data = path.read_bytes()
    pos = data.find(b"avcC")
    if pos < 0 or pos + 8 > len(data):
        return None
    profile, constraints, level = data[pos + 5], data[pos + 6], data[pos + 7]
    return f"avc1.{profile:02X}{constraints:02X}{level:02X}"


def probe_segment_variant_metadata(path: Path, init_path: Path | None = None) -> dict:
    """ffprobe-based equivalent of read_variant_metadata for sparse mode,
    which has no GPAC-generated manifest.mpd to read codec/resolution/
    frame_rate/bandwidth back from (no dasher pass runs in this mode).
    Probes one representative present segment file. `bandwidth` is
    estimated from this one segment's own bitrate (ffprobe stream
    `bit_rate`, falling back to format-level bit_rate) -- a coarser
    estimate than the normal path's real encoder-declared value, adequate
    for the HLS #EXT-X-STREAM-INF / DASH @bandwidth attributes' informational
    role."""
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,profile,level,bit_rate",
            "-show_entries", "format=bit_rate",
            "-of", "json",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {path} for variant metadata probing")
    stream = streams[0]

    num, den = (int(x) for x in stream["r_frame_rate"].split("/"))
    frame_rate = num / den if den else 0.0

    bandwidth = stream.get("bit_rate") or data.get("format", {}).get("bit_rate")
    if bandwidth is None:
        raise RuntimeError(
            f"Could not determine a bitrate for {path} (needed for "
            f"#EXT-X-STREAM-INF/@bandwidth) -- neither the stream nor the "
            f"format reported bit_rate."
        )

    return {
        "video": {
            "codecs": read_avcc_codec_string(init_path or path)
            or _rfc6381_avc1_codec_string(stream["profile"], float(stream["level"]) / 10),
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "frame_rate": frame_rate,
            "bandwidth": int(bandwidth),
        },
        "audio": None,
    }


def bake_segment_list(
    manifest_path: Path,
    output_package_dir: Path,
    *,
    allow_missing_segments: bool = False,
    dry_run: bool = False,
    hls_format: str = "cmaf",
    hls_ts_mux_audio: bool = True,
) -> None:
    """Bake phase entrypoint for the segment-list ('sparse') input mode
    (SCOPE.md §11), an alternative to bake()'s single-.ts/rendition-ladder
    input. Consumes the grave-robber-produced segment-list manifest (§11.2)
    instead of a franken-ts .ts + markers.json.

    Unlike bake(), there is no GPAC dasher pass and no cues-file/PTS
    cross-validation against embedded SCTE-35 -- an archive-derived
    source's markers are already fully decoded (grave-robber/SCOPE.md §5.2)
    and there is no continuous .ts to re-probe. The "ledger is always
    complete" governing decision (§11.1) means total_loop_duration_ticks
    and every segment's boundary tick are derived directly from the
    manifest's declared duration_ticks, never from reading back produced
    media -- there may be no media at all for some/most segments.
    """
    logger.info(
        "Sparse bake starting: %s -> %s (allow_missing_segments=%s)",
        manifest_path, output_package_dir, allow_missing_segments,
    )

    if hls_format not in ("cmaf", "ts"):
        raise ValidationError("hls_format must be 'cmaf' or 'ts'")

    manifest = load_segment_list_manifest(manifest_path)
    segments = manifest["segments"]
    raw_markers = manifest["markers"]

    missing_indices = validate_segment_list_missing_media(
        segments, allow_missing_segments=allow_missing_segments
    )

    segment_boundary_ticks = compute_segment_list_boundary_ticks(segments)
    total_loop_duration_ticks = sum(s["duration_ticks"] for s in segments)
    asset_boundaries = compute_asset_boundary_indices(segments)
    asset_boundary_gap_ticks = compute_asset_boundary_gap_ticks(segments)
    nominal_segment_duration_seconds = (total_loop_duration_ticks / TIMESCALE) / len(segments)

    logger.info(
        "Sparse bake: %d segment(s) declared, %d missing media, "
        "%d asset boundary/boundaries, total_loop_duration_ticks=%d",
        len(segments), len(missing_indices), len(asset_boundaries),
        total_loop_duration_ticks,
    )

    output_package_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = output_package_dir / "segments" / SPARSE_RENDITION_NAME
    segments_dir.mkdir(parents=True, exist_ok=True)
    if hls_format == "ts":
        ts_dir = output_package_dir / "hls-ts" / SPARSE_RENDITION_NAME
        ts_dir.mkdir(parents=True, exist_ok=True)

    segment_present: list[bool] = []
    reference_segment_path: Path | None = None
    # Proper CMAF: ONE init per output period / discontinuity (= per asset
    # span: a span starts at index 0 and at every asset boundary) + bare
    # moof+mdat segments whose tfdt is relative to their span's start (what
    # DASH's Period-relative SegmentTimeline and HLS's per-discontinuity
    # timestamp reset both expect). Every segment in a span must share one
    # decoder config (avcC) or a single init can't describe the span; a
    # config change needs a real discontinuity (asset boundary) to carry it.
    span_starts = sorted({0} | set(asset_boundaries))
    span_of = {i: max(k for k, s in enumerate(span_starts) if s <= i) for i in range(len(segments))}
    span_init_avcc: dict[int, bytes | None] = {}
    init_files: list[str | None] = [None] * len(span_starts)
    next_sequence_number = 1
    segment_starts = compute_segment_list_boundary_ticks(segments)

    # Audio: a separate playlist's segments (per-entry `audio_media_file`), or
    # audio muxed into the video segments (detected by probing). Same span/init
    # scheme as video; audio has its own ledger only when separately sourced.
    audio_separate = bool((manifest.get("audio") or {}).get("separate"))
    if audio_separate and hls_format == "ts" and hls_ts_mux_audio:
        raise ValidationError(
            "A separate audio playlist can't be muxed into HLS TS video segments; use "
            "--no-hls-ts-mux-audio (separate TS audio playlist) or hls_format='cmaf'."
        )
    ts_unmuxed = hls_format == "ts" and not hls_ts_mux_audio
    audio_durations = [
        s.get("audio_duration_ticks", s["duration_ticks"]) if audio_separate else s["duration_ticks"]
        for s in segments
    ]
    audio_starts = [sum(audio_durations[:i]) for i in range(len(segments))]
    audio_dir = segments_dir  # audio files live beside video, distinguished by name
    audio_present: list[bool] = [False] * len(segments)
    audio_span_avcc: dict[int, bytes | None] = {}
    audio_init_files: list[str | None] = [None] * len(span_starts)
    audio_next_sequence_number = 1
    audio_reference: Path | None = None
    audio_muxed: bool | None = None if not audio_separate else False

    def _bake_audio(index: int, audio_src: Path) -> None:
        nonlocal audio_next_sequence_number, audio_reference
        tmp = audio_dir / f"seg_a_{index:06d}.tmp.mp4"
        remux_segment_to_fragmented_mp4(audio_src, tmp, stream="a")
        a_init, a_fragments = cmaf.split_init_and_fragments(tmp.read_bytes())
        tmp.unlink()
        config = cmaf.stsd_config(a_init)
        span = span_of[index]
        if span not in audio_span_avcc:
            audio_span_avcc[span] = config
            audio_init_files[span] = f"audio_init_{span}.mp4"
            (audio_dir / audio_init_files[span]).write_bytes(a_init)
        elif config != audio_span_avcc[span]:
            raise ValidationError(
                f"Audio of segment {index} has a different codec config from the rest of its span "
                f"with no discontinuity between them. Narrow the imported range."
            )
        timescale = cmaf.track_timescale(a_init) or TIMESCALE
        relative_ticks = audio_starts[index] - audio_starts[span_starts[span]]
        body, audio_next_sequence_number = cmaf.rebase_fragments(
            a_fragments, round(relative_ticks * timescale / TIMESCALE), audio_next_sequence_number
        )
        (audio_dir / f"seg_a_{index:06d}.m4s").write_bytes(body)
        if ts_unmuxed:
            remux_segment_to_ts(
                audio_src, output_package_dir / "hls-ts" / "audio" / f"{index}.ts", stream="a",
                start_seconds=TS_BASE_SECONDS + audio_starts[index] / TIMESCALE,
            )
        audio_present[index] = True
        if audio_reference is None:
            audio_reference = audio_src
    for seg in segments:
        index = seg["index"]
        media_file = seg["media_file"]
        present = media_file is not None
        segment_present.append(present)
        if audio_separate and not dry_run and seg.get("audio_media_file"):
            _bake_audio(index, Path(seg["audio_media_file"]))  # independent of the video's presence
        if not present:
            continue

        src = Path(media_file)
        cmaf_dest = segments_dir / f"seg_{index:06d}.m4s"
        if not dry_run:
            tmp_dest = cmaf_dest.with_suffix(".tmp.mp4")
            remux_segment_to_fragmented_mp4(src, tmp_dest)
            init, fragments = cmaf.split_init_and_fragments(tmp_dest.read_bytes())
            tmp_dest.unlink()
            avcc = cmaf.avcc_config(init)
            span = span_of[index]
            if span not in span_init_avcc:
                span_init_avcc[span] = avcc
                init_files[span] = f"init_{span}.mp4"
                (segments_dir / init_files[span]).write_bytes(init)
            elif avcc != span_init_avcc[span]:
                raise ValidationError(
                    f"Segment {index} ({src}) has a different decoder config (SPS/PPS) from the "
                    f"other segments of its span (segments {span_starts[span]}..), but there is no "
                    f"discontinuity between them, so one CMAF init can't describe both. Narrow the "
                    f"imported range to a stretch with a single encoder config."
                )
            body, next_sequence_number = cmaf.rebase_fragments(
                fragments, segment_starts[index] - segment_starts[span_starts[span]], next_sequence_number
            )
            cmaf_dest.write_bytes(body)
            if reference_segment_path is None:
                reference_segment_path = src
            # Audio muxed into this video segment.
            if not audio_separate:
                if audio_muxed is None:
                    audio_muxed = probe_has_audio(src)
                if audio_muxed:
                    _bake_audio(index, src)
        if hls_format == "ts":
            ts_dest = output_package_dir / "hls-ts" / SPARSE_RENDITION_NAME / f"{index}.ts"
            if not dry_run:
                remux_segment_to_ts(
                    src, ts_dest, stream="v" if ts_unmuxed else None,
                    start_seconds=TS_BASE_SECONDS + segment_starts[index] / TIMESCALE,
                )

    if dry_run:
        logger.warning("dry-run: skipping loop_descriptor.json (no real segments produced)")
        return

    if reference_segment_path is None:
        raise ValidationError(
            "Every segment is missing media_file -- cannot probe codec/"
            "resolution/bandwidth metadata with nothing to probe. At least "
            "one real segment is required even with --allow-missing-segments."
        )
    first_init = next(f for f in init_files if f)
    variant_metadata = probe_segment_variant_metadata(reference_segment_path, segments_dir / first_init)
    apply_declared_variant(variant_metadata["video"], manifest.get("variant"))

    audio_variant = None
    if audio_reference is not None:
        audio_variant = probe_audio_variant(audio_reference)
        declared_codecs = [c.strip() for c in ((manifest.get("variant") or {}).get("codecs") or "").split(",")]
        declared_audio = next((c for c in declared_codecs if c.startswith(("mp4a", "ac-3", "ec-3"))), None)
        if declared_audio:
            audio_variant["codecs"] = declared_audio
        # A declared BANDWIDTH covers video + audio; serve.py adds audio's back on.
        variant_metadata["video"]["bandwidth"] = max(
            1, variant_metadata["video"]["bandwidth"] - audio_variant["bandwidth"]
        )

    rendition_result = {
        "name": SPARSE_RENDITION_NAME,
        "sparse": True,
        "init_span_starts": span_starts,
        "init_files": init_files,
        "video_track_id": None,
        "audio_track_id": None,
        "total_loop_duration_ticks": total_loop_duration_ticks,
        "segment_boundary_ticks": segment_boundary_ticks,
        "segment_present": segment_present,
        "audio_segment_boundary_ticks": audio_starts if audio_variant else None,
        "video_variant": variant_metadata["video"],
        "audio_variant": audio_variant,
    }
    if audio_variant:
        rendition_result.update(
            audio_sparse=True,
            audio_init_files=audio_init_files,
            audio_segment_present=audio_present,
        )

    loop_descriptor = {
        "version": 2,
        "created_at": time.time(),
        "timescale": TIMESCALE,
        "total_loop_duration_ticks": total_loop_duration_ticks,
        "segment_duration_seconds": nominal_segment_duration_seconds,
        "hls_format": hls_format,
        "hls_ts_mux_audio": hls_ts_mux_audio,
        "daterange_mode": "shared",
        "cue_tags": "none",
        "increment_event_ids": False,
        "daterange_id_format": DATERANGE_ID_FORMAT_DEFAULT,
        "markers": raw_markers,
        "asset_boundaries": asset_boundaries,
        "asset_boundary_gap_ticks": asset_boundary_gap_ticks,
        "video_renditions": [rendition_result],
        "source_input": str(manifest_path),
        "source_markers_json": str(manifest_path),
    }
    descriptor_path = output_package_dir / "loop_descriptor.json"
    with descriptor_path.open("w", encoding="utf-8") as f:
        json.dump(loop_descriptor, f, indent=2)
        f.write("\n")

    logger.info(
        "Sparse bake complete. %d/%d segment(s) have real media, written to %s",
        len(segments) - len(missing_indices), len(segments), output_package_dir,
    )


def bake_one_rendition(
    name: str,
    ts_file: Path,
    raw_markers: list[dict],
    output_package_dir: Path,
    *,
    segment_duration_seconds: float,
    dry_run: bool,
    include_audio: bool,
    daterange_mode: str = "shared",
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

    decoded = decode_embedded_scte35(ts_file, narrow_descriptors=(daterange_mode == "narrowed"))
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

    # GPAC's dasher runs with profile=live, which is designed to
    # incrementally APPEND into an existing output directory rather than
    # start a fresh one -- and bake.py never otherwise clears
    # `rendition_dir` between runs. Re-sparking the same channel with
    # shorter content (fewer/smaller segments than a previous bake) would
    # otherwise leave the previous, longer run's now-orphaned trailing
    # segment files lying around, silently glob-picked-up as real data by
    # read_segment_boundary_ticks/compute_total_loop_duration_ticks below
    # (both just glob *every* `*track{id}_*.m4s` file present, with no way
    # to tell "produced by this run" from "leftover from a previous one")
    # -- inflating the reported total_loop_duration_ticks past the actual,
    # current content's real length. Every bake is supposed to produce a
    # self-contained, immutable loop package (SCOPE.md §4.1) -- starting
    # from a genuinely empty directory every time is what actually makes
    # that true, rather than an incremental accumulation GPAC's live
    # profile would otherwise turn it into.
    if rendition_dir.exists():
        shutil.rmtree(rendition_dir)

    run_gpac_dasher(
        ts_file,
        cues_xml_path,
        rendition_dir,
        segment_duration_seconds=segment_duration_seconds,
        dry_run=dry_run,
    )

    if dry_run:
        return None

    # Must run before anything reads the produced segments back (tfdt
    # boundaries, ffprobe duration, etc.) -- see
    # repair_malformed_segments_in_dir's docstring.
    repair_malformed_segments_in_dir(rendition_dir)

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
    daterange_mode: str = "shared",
    cue_tags: str = "none",
    increment_event_ids: bool = False,
    daterange_id_format: str | None = DATERANGE_ID_FORMAT_DEFAULT,
    hls_format: str = "cmaf",
    hls_ts_mux_audio: bool = True,
) -> None:
    """Bake phase entrypoint (SCOPE.md §4.1), generalized to a rendition
    ladder auto-discovered from disk (see discover_renditions()).

    A single-rendition input degenerates naturally into a ladder of one --
    no special-casing needed anywhere below this point.

    `daterange_mode`/`cue_tags`/`increment_event_ids` (see
    `its-a-live/AGENTS.md`'s `[markers]` config section) control the
    *shape* of the HLS/DASH SCTE-35 signaling serve.py later renders from
    this bake -- they're recorded as-is into loop_descriptor.json and
    otherwise only consulted here for `daterange_mode`'s effect on the
    embedded payload (via `decode_embedded_scte35`'s `narrow_descriptors`)
    and `cue_tags="only"`'s validation below.
    """
    logger.info("Bake starting: %s -> %s", input_path, output_package_dir)

    if hls_format not in ("cmaf", "ts"):
        raise ValidationError("hls_format must be 'cmaf' or 'ts'")

    try:
        validate_daterange_id_format(daterange_id_format)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    renditions, markers_json = discover_renditions(input_path, markers_override)
    raw_markers = load_markers(markers_json)

    if cue_tags == "only":
        validate_cue_tags_only(raw_markers)
    if increment_event_ids:
        validate_increment_event_ids(raw_markers)

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
            daterange_mode=daterange_mode,
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

    if hls_format == "ts":
        bake_hls_ts_segments(output_package_dir, rendition_results, mux_audio=hls_ts_mux_audio)
    else:
        # Re-baking into an existing package must not leave stale TS files.
        shutil.rmtree(output_package_dir / "hls-ts", ignore_errors=True)

    # Step 6: write the immutable loop package.
    loop_descriptor = {
        "version": 2,
        "created_at": time.time(),
        "timescale": TIMESCALE,
        "total_loop_duration_ticks": reference["total_loop_duration_ticks"],
        "segment_duration_seconds": segment_duration_seconds,
        "hls_format": hls_format,
        "hls_ts_mux_audio": hls_ts_mux_audio,
        "daterange_mode": daterange_mode,
        "cue_tags": cue_tags,
        "increment_event_ids": increment_event_ids,
        # None marks packages baked before configurable ID formatting and
        # tells serve.py to preserve their original per-marker ID scheme.
        "daterange_id_format": daterange_id_format,
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
        help="Path to a franken-ts .ts file, a directory containing a "
        "rendition ladder (markers.json + one *.ts per rendition -- see "
        "README.md), OR a segment-list manifest .json (SCOPE.md §11 -- "
        "selected automatically by this .json extension, not a separate "
        "flag/binary)",
    )
    parser.add_argument(
        "--allow-missing-segments",
        action="store_true",
        help="Segment-list manifest input only (SCOPE.md §11.2): accept "
        "segments whose media_file is null (no physical media recovered), "
        "producing a manifest-complete, media-optional package -- a "
        "request for a missing segment's bytes 404s, but the served "
        "manifest is otherwise indistinguishable from a fully-populated "
        "one. Default: hard-fail on any such segment.",
    )
    parser.add_argument(
        "--markers",
        type=Path,
        default=None,
        help="Override the auto-discovered markers.json path",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output loop package directory")
    parser.add_argument("--segment-duration", type=float, default=4.0)
    parser.add_argument("--hls-format", choices=("cmaf", "ts"), default="cmaf")
    parser.add_argument("--hls-ts-mux-audio", action=argparse.BooleanOptionalAction, default=True,
                        help="For HLS TS, mux audio with video (default) or serve a separate TS audio playlist")
    parser.add_argument(
        "--daterange-mode",
        choices=("grouped", "shared", "narrowed"),
        default="shared",
        help="Shape of the HLS DATERANGE/DASH <Binary> SCTE-35 payload for "
        "coincident descriptors (e.g. a Break start + nested PPO/Ad start, "
        "all at the same PTS). 'shared' (default): one tag per descriptor, "
        "each carrying the full shared multi-descriptor message (the "
        "standard way coincident events are signaled -- what MediaPackage "
        "does too). 'narrowed': one tag per descriptor, each re-encoded to "
        "carry only that event's own descriptor -- for downstream "
        "consumers that can't cope with more than one segmentation "
        "descriptor per message. 'grouped': one tag per group of "
        "coincident descriptors, carrying the shared payload once.",
    )
    parser.add_argument(
        "--cue-tags",
        choices=("none", "alongside", "only"),
        default="none",
        help="Whether to also emit #EXT-X-CUE-OUT/-CONT/-IN (no raw SCTE-35 "
        "payload) alongside DATERANGE ('alongside'), or instead of it "
        "entirely ('only' -- requires every marker to be a bare "
        "splice_insert, hard-fails otherwise). Both modes only ever "
        "consider bare splice_insert markers for the CUE-OUT/-CONT/-IN "
        "tags themselves -- 'alongside' still tags every marker's "
        "DATERANGE regardless of splice_type, splice_insert or not, but "
        "silently emits no CUE-OUT/-IN for any non-splice_insert one, "
        "since nested/overlapping time_signal segmentation types (e.g. a "
        "Break containing a shorter PPO containing a shorter Ad) have no "
        "way to become well-formed CUE-OUT/-IN pairs (unlike DATERANGE, "
        "which tags each independently) -- splice_insert markers are "
        "always flat, non-overlapping avails, which is what this tag "
        "pair actually models. 'none' (default): DATERANGE only, today's "
        "behavior.",
    )
    parser.add_argument(
        "--increment-event-ids",
        action="store_true",
        help="Bump every marker's segmentation_event_id/splice_event_id by "
        "the current loop number times a shared step -- the smallest "
        "power of 10 above the channel's largest base event id (e.g. "
        "base ids 100-190 -> step 1000, so loop 1 emits 1100/1190, loop "
        "2 emits 2100/2190, ...), keeping each id's original base "
        "recognizable as its low-order remainder and predictable purely "
        "from wall-clock time against the channel's epoch (no runtime "
        "counter) -- instead of repeating the same id every loop. Wraps "
        "the loop-number component back to 0 at the 32-bit SCTE-35 "
        "ceiling. Default off (same id every loop -- easiest to test "
        "against); this is a serve-time behavior read from "
        "loop_descriptor.json, recorded here at bake time.",
    )
    parser.add_argument(
        "--daterange-id-format",
        default=DATERANGE_ID_FORMAT_DEFAULT,
        help="Python-style HLS DATERANGE ID template. Supported fields: "
        "{loop}, {eventid}, {segid}, {seghex}, {segcode}, {segname}, {epoch} "
        "(Unix milliseconds), {pd} (ISO-8601 program date-time). "
        f"Default: {DATERANGE_ID_FORMAT_DEFAULT!r}.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.input_path.is_file() and args.input_path.suffix == ".json":
            bake_segment_list(
                args.input_path,
                args.output,
                allow_missing_segments=args.allow_missing_segments,
                dry_run=args.dry_run,
                hls_format=args.hls_format,
                hls_ts_mux_audio=args.hls_ts_mux_audio,
            )
        else:
            bake(
                args.input_path,
                args.output,
                segment_duration_seconds=args.segment_duration,
                dry_run=args.dry_run,
                markers_override=args.markers,
                daterange_mode=args.daterange_mode,
                cue_tags=args.cue_tags,
                increment_event_ids=args.increment_event_ids,
                daterange_id_format=args.daterange_id_format,
                hls_format=args.hls_format,
                hls_ts_mux_audio=args.hls_ts_mux_audio,
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
