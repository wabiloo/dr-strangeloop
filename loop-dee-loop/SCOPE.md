# loop-dee-loop — scope for implementation

Status: ready for implementation. This document is self-contained; the implementer
should not need the chat history that produced it, but should ask before deviating
from any decision marked **DECIDED** below.

## 1. Problem statement

We want a self-hosted, non-commercial, cheap alternative to AWS MediaLive +
MediaPackage for looping a `franken-ts`-produced MPEG-TS file (content + real
SCTE-35 markers) into a continuous live HLS + DASH channel, deployable as a small
Docker container on Fargate or a single EC2 instance. No transcoding anywhere in
this tool. Real SCTE-35 signaling (`EXT-X-DATERANGE` for HLS, `<EventStream>` for
DASH) must appear at the exact, frame-accurate segment boundaries corresponding to
the markers `franken-ts` already authored — not a generic ad-marker workaround.

This tool is **new** and lives in its own sibling directory, `loop-dee-loop/`,
next to `franken-ts/`, `inspector-krogh/`, `push-to-aws-media/`. It does not
modify `franken-ts` except for one small, optional addition described in §2.
It is a self-hosted replacement for what `push-to-aws-media` does today
(MediaLive/MediaPackage); `push-to-aws-media` is left as-is for anyone who still
wants the AWS-hosted path.

## 2. Prerequisite: a clean marker-metadata export from franken-ts

`franken-ts` already computes everything we need — the *real, verified* PTS of
every SCTE-35 event (its own step 4, "PTS detection"), and the tsduck XML used to
inject them (step 5). Today that XML is a temp file only kept with `--debug`, and
the human-readable marker list only exists baked into the `--verify` HTML report.

**Add one small feature to `franken-ts`:** alongside the `.ts` output, always
write a `<output>.markers.json` sidecar (not gated behind `--debug` or
`--verify`) with one entry per injected SCTE-35 event:

```json
[
  {
    "event_id": "0x00000001",
    "splice_type": "time_signal",
    "pts_time_ticks": 5400000,
    "pts_time_seconds": 60.0,
    "segmentation_type_id": "0x34",
    "segmentation_duration_ticks": 2700000,
    "upid_type": "0x0C",
    "upid_hex": "5349474E414C3A4C494E454152",
    "flags": {
      "web_delivery_allowed": true,
      "no_regional_blackout": false,
      "archive_allowed": false,
      "device_restrictions": 1
    }
  }
]
```

`pts_time_ticks` is in the 90kHz clock, matching the `.ts`'s own timescale — this
is the single source of truth for every downstream timing decision in this tool.
Do **not** re-derive marker timing by re-probing the `.ts` in `loop-dee-loop`;
always consume this sidecar. If it's missing or its PTS values don't match what's
actually embedded in PID 600 of the `.ts` (see §4, step 1 validation), that's a
hard error, not something to silently reconcile.

## 3. Non-goals (explicitly out of scope for v1)

- No live transcoding, no live re-encoding, anywhere.
- No support for a multi-asset YAML playlist of remote HTTPS VOD assets stitched
  live. v1 takes exactly one already-baked `franken-ts` `.ts` file (+ its
  `.markers.json`) as input to the bake step. A future version may want to
  concatenate multiple franken-ts outputs or remote assets before baking — if
  that's wanted later, treat it as a new phase-0 step that produces a single
  conformed `.ts` + merged `.markers.json`, upstream of everything below. Don't
  design that in now; flag it as a known future direction only.
- No live per-loop SCTE-35 injection or live per-loop re-segmentation. The
  design is bake-once, loop-forever (see §4). If this constraint is ever
  relaxed, treat it as a different project — the drift and frame-accuracy
  guarantees below depend on it.
- No reliance on GPAC's own `scte35dec` → DATERANGE/EventStream aggregation.
  Confirmed unreliable (see §6) — this tool authors that signaling itself from
  `.markers.json`.
- No multi-channel orchestration UI, no database, no admin API in v1. One loop
  package, one serving process, one channel. Multiple channels = multiple
  deployments of the same container with different inputs.

## 4. Architecture: bake once, loop forever

Two phases, cleanly separated, communicating only through a versioned,
immutable **loop package** directory on disk.

```
franken-ts output (.ts + .markers.json)
        │
        ▼
   [ BAKE  — bake.py, run once per schedule change ]
        │
        ▼
  loop package/  (immutable: segments + init + loop_descriptor.json)
        │
        ▼
   [ SERVE — serve.py, long-running, stateless per request ]
        │
        ▼
  live-sliding HLS (.m3u8) + DASH (.mpd), served forever
```

### 4.1 Bake phase (`bake.py`)

Input: path to a `franken-ts` `.ts` file and its `.markers.json` sidecar (§2).
Runs offline / as a one-shot job (e.g. a Fargate task run on demand, or a local
script) — never in the hot serving path.

Steps:

1. **Validate.** Demux the `.ts` with GPAC, confirm PID 600 (or whatever PID
   franken-ts used) is present and typed `SCTE35`. Decode the embedded SCTE-35
   messages directly (`threefive` is a fine Python dependency for this — already
   proven to round-trip franken-ts's cues correctly in prototyping) and compare
   every decoded `pts_time` against `.markers.json`. Any mismatch (missing event,
   extra event, PTS off by even one tick) is a **hard failure** — stop, do not
   guess or proceed with a "close enough" value. This is the safety net for the
   whole pipeline: everything downstream trusts `.markers.json` blindly, so this
   is the one place that earns that trust.

2. **Build the DASHCues XML.** For each verified marker, emit a `<Cue>` for the
   video track at its exact `pts_time_ticks` (as `cts`, since GPAC's `dts`-mode
   cues correctly hard-fail on a non-keyframe target — see §6 — which is a
   useful safety property, but only if the target actually needs to resolve
   against decode order; `cts` matched exactly against a real franken-ts
   keyframe in prototyping with zero offset). For the audio track, do **not**
   reuse the video tick verbatim — snap to the nearest exact multiple of
   `90000 * samples_per_frame / sample_rate` (e.g. 1920 ticks for 48kHz/1024-sample
   AAC). Compute this from the actual audio track's real parameters, don't
   hardcode 48kHz/1024. Confirmed in prototyping: an unsnapped audio cue causes
   GPAC to spam "buggy source cues" and effectively hang scanning the rest of
   the file rather than failing fast — so snapping is mandatory, not an
   optimization.

3. **Run GPAC.** `scte35dec:mode=passthrough` (never `evte_agg` — see §6) piped
   into the `dasher` filter with the cues file from step 2, producing fragmented
   CMAF segments + init segment(s) for both HLS and DASH from a single pass (or
   two passes, one per format, if a single dasher invocation proves awkward in
   practice — either is fine, correctness matters more than invocation count).
   Confirmed in prototyping on real franken-ts output: segment boundaries land
   exactly on the cue ticks with zero drift when the source markers are
   genuinely on real IDRs (which franken-ts already guarantees).

4. **Compute `total_loop_duration_ticks`.** This is the single most important
   number in the whole system — the exact end tick of the last baked segment,
   as an integer, in the same 90kHz timescale as everything else. Get it by
   reading it back out of the actual produced segments (e.g. via MP4Box box
   inspection of the last fragment's `tfdt` + sample durations), not by trusting
   a nominal/expected duration — this number must be ground truth, because §5's
   entire drift-freedom argument depends on it being exactly correct.

5. **Author the SCTE-35 signaling ourselves.** Do not use GPAC's own
   EventStream/DATERANGE generation. From `.markers.json`, directly write:
   - HLS: `EXT-X-DATERANGE` attributes per SCTE-35/HLS mapping conventions
     (`ID`, `START-DATE`, `PLANNED-DURATION` from `segmentation_duration_ticks`,
     `SCTE35-OUT`/`SCTE35-IN` or `SCTE35-CMD` carrying the original base64/hex
     splice command — implementer should follow the standard SCTE-35-in-HLS
     mapping (CUE-OUT/CUE-IN + DATERANGE with SCTE35-* attributes), not invent
     one).
   - DASH: `<EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" timescale="90000">`
     with one `<Event presentationTime="..." duration="...">` per marker,
     containing `<Signal><Binary>` = the original base64 SCTE-35 message. This
     exact shape was confirmed working end-to-end in prototyping (GPAC produced
     it correctly for a single marker before its own aggregation logic broke
     down on multiple markers) — reuse that shape, generate it from
     `.markers.json` instead of trusting GPAC to generate it.
   These go into the **loop-relative** manifest fragments described in §4.2 —
   i.e. don't bake absolute wall-clock times here, bake tick-offsets relative to
   the start of one loop iteration (0 to `total_loop_duration_ticks`).

6. **Write the loop package.** A versioned output directory containing:
   - the segment + init files from step 3 (physical, immutable, reused every
     loop iteration — never regenerated by the serve phase),
   - `loop_descriptor.json`: `total_loop_duration_ticks`, `timescale`, per-track
     segment boundary tick lists, and the list of markers with their loop-relative
     tick offsets and pre-rendered DATERANGE/EventStream fragments from step 5.
   Treat this directory as immutable and atomically swappable (e.g. a
   timestamped or content-hashed directory name, with the serve phase pointed at
   "current" via a symlink or config value) so a schedule change never involves
   mutating a package the server is actively reading.

### 4.2 Serve phase (`serve.py`)

Long-running process. Must be **stateless across requests** — no in-memory
"current position" variable that a timer advances. Every request independently
derives the current position from `now()` versus a fixed channel epoch. This is
not a style preference — it's what makes drift structurally impossible (see §5).

Per request (manifest or segment):

```
elapsed_ticks       = now_ticks() - epoch_ticks           # both integers, same timescale
loop_number         = elapsed_ticks // total_loop_duration_ticks     # exact int division
position_in_loop    = elapsed_ticks %  total_loop_duration_ticks     # exact int modulo
```

`loop_number` and `position_in_loop` are then pure, deterministic lookups
against `loop_descriptor.json` (no further arithmetic that could accumulate
error) to produce:

- the current HLS media sequence number / DASH `$Number$` (
  `loop_number * segments_per_loop + segment_index_within_loop`),
- which physical segment file to serve (segment identity repeats every loop —
  URL scheme should map a physical segment file to every loop_number that uses
  it, e.g. `/seg/{physical_index}.m4s?` or a redirect, so segment bytes are
  never duplicated on disk and are trivially CDN-cacheable),
- `EXT-X-PROGRAM-DATE-TIME` / DASH `availabilityStartTime`, computed as
  `epoch_wall_clock + (loop_number * total_loop_duration_ticks + position_in_loop) / timescale`
  — one division, done once per request, never accumulated,
- which markers (from `loop_descriptor.json`) fall within the currently
  advertised manifest window, with their DATERANGE/EventStream fragments'
  tick-offsets shifted by `loop_number * total_loop_duration_ticks` before
  being placed in the outgoing manifest.

**Hard rule: no floating-point seconds in any value that could be reused or
accumulated across requests.** Floating point is fine as a one-time, final
conversion for display/serialization (e.g. converting a computed tick value to
an ISO8601 timestamp string for one response), never as a running total. See §5
for why this matters and a concrete regression test to enforce it.

Segment bytes themselves are served completely unmodified — same bytes every
loop, every time. Only the manifest text differs per request. This split means
segment serving can be as simple as static file serving (nginx, S3, or a trivial
handler) and can sit behind a CDN; only manifest generation needs to be dynamic,
and it's cheap (string/XML templating over a small precomputed structure, no
media parsing at request time).

## 5. Drift-freedom: required design property, with a regression test

The naive failure mode is a server that maintains a running "current time" or
"current loop start" variable and advances it with `sleep()` or repeated
addition of a nominal segment/loop duration in floating-point seconds. That
drifts, because float addition error compounds with the number of additions —
concretely measured in prototyping: accumulating a 188.2658333...s period
10,000,000 times in float64 drifts ~307ms versus the exact integer-tick
result. The correct design (§4.2) never accumulates: every request computes
`loop_number` and `position_in_loop` fresh via one integer multiplication/
division against a fixed epoch, so the result for loop 10 is exactly as exact
as loop 10,000,000,000 — there's no state to drift.

**Required unit test** (implementer must write this, not skip it): given a
fixed `epoch_ticks`, a `total_loop_duration_ticks` that is deliberately *not* a
round number of seconds, and a range of simulated `now_ticks` values spanning
many thousands of loop iterations, assert that `loop_number`/`position_in_loop`
computed via the required integer method exactly match values computed
independently via Python's arbitrary-precision integers (i.e. the test *is* the
mathematical ground truth, not a tolerance-based comparison) — and separately,
as a negative test, assert that a naive float-accumulation implementation
*would* fail this same assertion, so the test actually exercises the failure
mode it's meant to prevent, not just the happy path.

Also required: a lint/code-review checklist item (a comment at the top of
`serve.py` is sufficient, doesn't need tooling) stating explicitly that no
persisted or accumulated timing state may be a Python `float` — every persisted
tick value is an `int`.

## 6. Known GPAC risks and constraints (from prototyping — do not rediscover these)

- **No packaged GPAC release has `scte35dec`.** It's new (Motion Spell,
  2024–2026). Must build GPAC from source. A plain default `./configure` build
  works; do **not** pass `--disable-svg` — it triggers a real upstream compile
  bug (`base_scenegraph.c`, unbalanced `#ifndef GPAC_DISABLE_SVG` block) that
  cascades into unrelated link failures. Minimal working configure flags
  confirmed in prototyping: `--static-mp4box --disable-x11 --disable-atsc
  --disable-mpegts-xevc --disable-avi`, with `libfreetype6-dev libpng-dev
  libjpeg-dev zlib1g-dev` installed. Pin an exact commit once validated against
  real franken-ts output — do not float on `master` in the Dockerfile.
- **`dasher:evte_agg` is unstable — do not use it.** In prototyping it either
  triggered `[Dasher] evte_agg option used with HLS is likely not supported by
  your player`, or produced real internal errors (`Unaligned segment`, DTS
  patching warnings, `dasher not responding properly ... Internal Service
  Error`) on both single- and multi-marker real content. `scte35dec:mode=
  passthrough` is the stable mode and is what §4.1 step 3 specifies.
- **GPAC's own EventStream generation under `passthrough` is unreliable for
  multi-marker content.** Tested against the real `break-and-popos.ts` (four
  real markers): only one `<Event>` was produced, and its `presentationTime`
  was off by 2 seconds from the verified true value — reproduced identically
  with and without an explicit cues file, so it's not a cues-file interaction
  bug, it's in `scte35dec`'s event aggregation itself. This is exactly why §4.1
  step 5 requires authoring the signaling from `.markers.json` directly instead
  of trusting GPAC's output — do not revisit trusting GPAC here without new
  evidence that a newer commit has fixed it.
- **`dasher:cues=` with `dts` fails loudly on a non-keyframe target**
  (`packet N is not a RAP nor a switch frame!`) — treat this as a feature (it
  means your marker PTS is wrong or the source isn't IDR-clean at that point;
  don't work around it, fix the input). **`cues=` with `cts` can silently snap**
  to the nearest available frame without erroring — in one non-production test
  this was ~2 frames (80ms) off; prefer validating the exact match distance in
  the bake step and hard-failing if it's non-zero, rather than trusting a silent
  snap. Against real, correctly-authored franken-ts output in this session, the
  `cts` match was exact (zero offset) — the earlier 80ms case only showed up
  against a hand-built synthetic test clip with its own encoding-pipeline
  imprecision, not against real franken-ts output. Still: verify, don't assume.

## 7. Docker packaging

Multi-stage build:

- **Build stage**: full build toolchain (gcc, make, pkg-config, the dev
  libraries listed in §6) plus GPAC source at the pinned commit, built with the
  confirmed configure flags. Produces the `gpac` (and `MP4Box`, if needed)
  binaries.
- **Runtime stage**: slim base image (e.g. `debian-slim` or similar), copy only
  the built `gpac`/`MP4Box` binaries and their runtime shared-library
  dependencies (`ldd` them in the build stage to get the list), plus the
  `bake.py`/`serve.py` Python code and its dependencies (`threefive` for
  SCTE-35 handling, whatever's chosen for the HTTP server). No compiler, no dev
  headers, no source tree in the final image.

Two entrypoints from the same image: `bake` (run once, e.g. as a one-off ECS
task or local invocation, given a franken-ts `.ts`+`.markers.json` and an output
loop-package location) and `serve` (long-running, given a loop-package location
and a channel epoch, exposes HTTP).

## 8. Deployment sizing (informational, not a task)

No GPU anywhere in this design — nothing decodes or renders video at any point
after the one-time bake, and the bake step itself is stream-copy (demux/remux),
not encode/decode. Runtime CPU/RAM needs for the serve phase are small (it's
manifest-text generation plus static file serving); the smallest Fargate task
sizes should be more than sufficient for a single channel. If real viewer
counts get large, put a CDN (CloudFront/S3, or any CDN) in front of the segment
files rather than serving every viewer directly from the task's network
interface — the origin only needs to sustain the CDN's cache-fill rate, not the
full audience.

## 9. Suggested repo layout

```
loop-dee-loop/
  README.md              # usage, mirrors franken-ts/README.md style
  SCOPE.md                # this file
  bake.py
  serve.py
  gpac_pipeline.py        # the DASHCues XML build + gpac invocation wrapper
  scte35_signaling.py     # .markers.json -> EXT-X-DATERANGE / EventStream authoring
  loop_math.py            # the epoch/loop_number/position_in_loop integer arithmetic (§4.2, §5)
  Dockerfile
  tests/
    test_loop_math.py     # the drift regression test from §5
    test_bake_validation.py
    fixtures/             # small real or realistic .ts + .markers.json for tests
```

## 10. Acceptance checklist

- [ ] `franken-ts` emits `.markers.json` alongside its `.ts` output (§2).
- [ ] `bake.py` hard-fails on any mismatch between `.markers.json` and the
      actual decoded SCTE-35 in the `.ts` (§4.1 step 1).
- [ ] Baked segment boundaries land exactly (zero-tick tolerance) on every
      marker's verified PTS, checked against real franken-ts output, not just
      synthetic test clips.
- [ ] The authored HLS/DASH signaling contains **all** markers from
      `.markers.json`, each at the correct offset — explicitly tested against a
      multi-marker real file, since GPAC's own aggregation was confirmed to
      drop markers under exactly this condition.
- [ ] `loop_math.py`'s drift regression test (§5) passes, and demonstrably fails
      against a naive float-accumulation implementation (i.e. the test actually
      tests something).
- [ ] `serve.py` holds no persisted floating-point timing state.
- [ ] Docker image builds GPAC from a pinned commit in a build stage and ships
      only the runtime binaries in the final image.
- [ ] End-to-end manual test: run `bake` against a real franken-ts output, run
      `serve`, and confirm with a real HLS/DASH player that playback loops
      seamlessly and ad markers fire at the right point on at least two
      consecutive loop iterations.
