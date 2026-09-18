# loop-packager

Self-hosted, non-commercial, cheap alternative to AWS MediaLive + MediaPackage
for looping a `franken-ts`-produced MPEG-TS file (content + real SCTE-35
markers) into a continuous live HLS + DASH channel. See `SCOPE.md` for the
full design rationale and decisions.

No transcoding anywhere. Two phases, communicating only through an immutable
loop package directory on disk:

```
franken-ts output (.ts + .markers.json)
        |
        v
   bake.py   (run once per schedule change)
        |
        v
  loop package/  (segments + init + loop_descriptor.json)
        |
        v
   serve.py  (long-running, stateless per request)
        |
        v
  live-sliding HLS (.m3u8) [+ DASH], served forever
```

## Requirements

- Python 3.11+
- `gpac`/`MP4Box` built from source with `scte35dec` support (no packaged
  release has it yet — see `Dockerfile` for the pinned-commit build with the
  confirmed configure flags; **do not** pass `--disable-svg`).
- `ffmpeg`/`ffprobe` on `PATH`.
- Python deps: `pip install -r requirements.txt` (`threefive`, `flask`).

## Usage

```bash
# Bake + serve in one command (convenience wrapper, see run.sh --help)
./run.sh path/to/output.ts --output /var/loop-packages/2026-01-01 \
         --epoch-utc 2026-01-01T00:00:00Z --port 8080

# Or run the two phases separately:

# One-off bake, given a franken-ts output + its markers.json sidecar
python3 bake.py path/to/output.ts --output /var/loop-packages/2026-01-01

# Long-running serve, stateless per request
python3 serve.py /var/loop-packages/2026-01-01 --epoch-utc 2026-01-01T00:00:00Z
```

`run.sh` also supports `--skip-bake` to (re)start serving an existing loop
package without re-baking, and auto-adds a locally-built `~/gpac-local/bin`
to `PATH` if present (see `Dockerfile`/`SCOPE.md` §6 for why a packaged GPAC
release isn't enough).

### Endpoints

| Path | What it is |
|---|---|
| `/master.m3u8` | HLS **multivariant** playlist — give players this URL, not `/live.m3u8` directly. |
| `/live.m3u8` | HLS media playlist (video). |
| `/audio.m3u8` | HLS media playlist (audio) — only present if the source has an audio track. |
| `/manifest.mpd` | DASH MPD (video + audio `AdaptationSet`s). |
| `/init.mp4` | CMAF init segment (video track). |
| `/seg/<n>.m4s` | CMAF media segments (video track). |
| `/audio/init.mp4` | CMAF init segment (audio track). |
| `/audio/seg/<n>.m4s` | CMAF media segments (audio track). |

`#EXT-X-STREAM-INF`'s `BANDWIDTH`/`CODECS`/`RESOLUTION`/`FRAME-RATE`
attributes in `/master.m3u8` come from the exact values GPAC itself
computed while producing the real segments at bake time (read back from
its own generated `manifest.mpd` — see `bake.py`'s `read_variant_metadata`
— never re-derived/guessed).

### Controlling the DVR window / manifest size

Both `run.sh` and `serve.py` accept `--dvr-window-seconds` (default 30) to
control how much sliding-window history is advertised per manifest response
(HLS `#EXTINF` list / DASH `SegmentTimeline` + `timeShiftBufferDepth`).
This is converted to a segment count using the package's nominal
`--segment-duration` (from bake time), rounded up. For exact control over
the segment count instead, use `--window-segments N` (overrides
`--dvr-window-seconds`):

```bash
./run.sh path/to/output.ts --output /var/loop-packages/ch1 \
         --dvr-window-seconds 60   # ~60s of DVR window

# or, for exact segment-count control:
python3 serve.py /var/loop-packages/ch1 --epoch-utc ... --window-segments 10
```

`bake` hard-fails (exit code 2) on any mismatch between `.markers.json` and
the SCTE-35 actually embedded in the `.ts` — this is a deliberate safety net,
not something to work around; see `SCOPE.md` §2 and §4.1 step 1.

## Module layout

| File | Responsibility |
|---|---|
| `bake.py` | Bake-phase entrypoint: validate, build cues, run GPAC, compute ground-truth loop duration, author signaling, write the loop package. |
| `gpac_pipeline.py` | DASHCues XML construction + GPAC CLI invocation wrapper. |
| `scte35_signaling.py` | Author `EXT-X-DATERANGE` / DASH `<EventStream>` directly from `.markers.json` (never trust GPAC's own aggregation — see `SCOPE.md` §6). |
| `loop_math.py` | The epoch/loop_number/position_in_loop **integer** arithmetic that makes drift structurally impossible (`SCOPE.md` §4.2, §5). |
| `serve.py` | Serve-phase entrypoint: stateless HTTP serving of manifests + segments. |
| `load_test.py` | Concurrent-viewer load generator used for the measurements in `PERFS.md`. |

See `PERFS.md` for measured CPU/memory usage under realistic concurrent
load, and Fargate/EC2 sizing recommendations derived from it.

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
```

`tests/test_loop_math.py` is the required drift regression test from
`SCOPE.md` §5: it asserts the integer method exactly matches an
independently-computed arbitrary-precision ground truth across many
thousands of simulated loop iterations, and separately demonstrates that a
naive float-accumulation implementation *would* fail that same assertion
(measurable drift after 10,000,000 iterations), so the test actually
exercises the failure mode it's meant to prevent.

`tests/test_bake_validation.py` covers the markers.json ↔ decoded-SCTE-35
cross-validation (missing/extra events, one-tick PTS mismatches — all hard
failures, no tolerance).

End-to-end verification performed (real GPAC build from source, real
multi-marker franken-ts output — `outputs/break-and-popos.ts`, 4 markers /
2 events, the exact case GPAC's own EventStream aggregation is known to
mishandle per `SCOPE.md` §6):

- `bake.py` validated all 4 markers against threefive-decoded SCTE-35 with
  zero PTS drift, ran the real `scte35dec:mode=passthrough` → `dasher`
  pipeline, and produced segments whose boundaries land **exactly** on
  every marker tick (verified via `tfdt`/box inspection: 0, 5580000,
  8280000, 13860000, 15660000).
- `total_loop_duration_ticks` was computed as ground truth (18,360,000
  ticks / 204.0s) by reading back the actual produced segments — GPAC's
  own MPD `mediaPresentationDuration` reported a slightly different nominal
  value (204.096s), confirming why `SCOPE.md` §4.1 step 4 insists on never
  trusting a nominal duration.
- GPAC's own `<EventStream>` in the generated `manifest.mpd` indeed only
  contained **1 of 4** events with an incorrect duration — reproducing the
  exact failure mode `SCOPE.md` §6 documents. `scte35_signaling.py`,
  authoring directly from the validated `.markers.json`, correctly produced
  all 4 `EXT-X-DATERANGE` tags / `<Event>` elements.
- `serve.py` was started against the real baked package and its
  `/live.m3u8`, `/init.mp4`, and `/seg/<n>.m4s` endpoints were hit with
  `curl` — segments came back as valid, playable MP4/CMAF fragments (probed
  with `ffprobe`), manifest correctly wrapped across a loop boundary with
  media-sequence numbers in the hundreds of thousands, and DATERANGE tags
  appeared on the correct segments.

Two real bugs were found and fixed during this pass:

1. `bake.py`'s marker/decoded-SCTE-35 matching originally keyed only on
   `event_id`, but franken-ts (by design) reuses the same `event_id` for a
   start/stop pair — this silently dropped one of every pair. Fixed to key
   on `(event_id, occurrence)` (ascending-PTS position within the same
   `event_id`), so a genuine PTS mismatch is still reported as a mismatch
   rather than a false "missing" event.
2. `read_actual_track_params`/`probe_scte35_pid` originally used ffprobe's
   0-based stream `index` as the GPAC track ID passed to `dasher:cues=`.
   These are different numbering schemes (ffprobe `index` 0/1 vs. real PID
   256/257) — GPAC's `<Stream id="...">` cues attribute needs the real PID.
   Fixed to derive the PID from ffprobe's `id` field (`"0x100"` → `256`).
   Also, GPAC's cues XML attribute is `id`/`timescale` on `<Stream>`, not
   `trackID` — fixed to match GPAC's actual documented schema
   (`gpac -ha dasher`).

Segment boundaries are non-uniform in a cue-driven bake (GPAC splits exactly
at marker PTS, ignoring the nominal `segdur`), so `bake.py` now stores the
exact `segment_boundary_ticks` list in `loop_descriptor.json`, and
`serve.py` reads it back rather than assuming uniform segment durations.

## Segmentation

Segments are **not** just cut at marker ticks. `bake.py` requests a
nominal grid at `--segment-duration` seconds (real ABR-sized segments)
UNIONED with the exact marker ticks (forced boundaries), so a normal
channel gets a proper short-segment ABR ladder (e.g. `#EXTINF:4.000,`
throughout) with a forced early cut exactly at each ad marker — not one
giant multi-minute segment per ad break. This mirrors how live
ad-insertion packagers (MediaLive/MediaPackage) actually behave.

`bake.py` reads back the *real* produced segment boundaries from the
output (never trusts the requested grid) and hard-fails if any marker tick
didn't land exactly on a segment boundary (GPAC's `cues=...:cts` mode can
silently snap an imprecise cue to the nearest keyframe — SCOPE.md §6 —
which is fine for nominal grid points but never acceptable for a marker).

## Known limitations in this initial implementation

- `bake.py` runs two full GPAC passes (one for DASH, one for HLS) rather
  than one dual-output invocation — acceptable per `SCOPE.md` §4.1 step 3
  ("either is fine, correctness matters more than invocation count") but
  doubles bake time.
- GPAC was built locally from `master` (no pinned commit yet) to validate
  this implementation — `Dockerfile` still needs a specific commit pinned
  once a version is chosen for production (`SCOPE.md` §7).
