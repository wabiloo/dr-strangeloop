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
  **Superseded in part by §11**: the archive-import tool (`grave-robber`,
  formerly scoped under the placeholder name `archive-loop-import`) needs
  a second, sparse input mode that does not fit "exactly one already-baked
  `.ts`" at all — §11 defines it as an addition, not a relaxation of this
  v1 constraint.
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

## 11. Extension: sparse segment input (manifest-complete, media-optional)

**DECIDED.** Added to support `grave-robber` (formerly scoped under the
placeholder name `archive-loop-import`; see its own `SCOPE.md`), which
derives loop timing + SCTE-35 markers from a
captured HTTP archive (HAR/Proxyman log) of a real HLS/DASH session
instead of from a `franken-ts` build. Unlike v1's input (§2/§4.1: exactly
one continuous, already-baked `.ts`), an archive-derived source is
inherently a set of **independently captured, already-segmented files
with holes** — the archive may simply never have recorded the response
body for some referenced segments. There is no continuous stream to
re-cut, and no guarantee every segment's bytes exist at all.

### 11.1 Governing decision: manifests are always complete

**The served manifest must always look exactly like a normal, fully
populated manifest** — every segment in `loop_descriptor.json`'s boundary
list gets a normal `EXTINF`/segment URI (HLS) or `<S>` entry (DASH),
whether or not real media backs it. **No `#EXT-X-GAP`, no DASH
`SegmentTimeline` coverage hole, no other manifest-visible signal that a
segment might not be playable.** A real player may fail to play through a
missing span; that is accepted. The primary consumer this mode is built
for is not necessarily a playback client — it's tooling (an SSAI engine,
for instance) that inspects/manipulates manifest structure (segment
timing, discontinuities, SCTE-35 cues) and doesn't require every
referenced segment to actually resolve.

This means **no change to manifest generation** in `serve.py` at all: it
already builds every manifest field purely from `loop_descriptor.json`'s
segment/marker lists, never by checking whether a physical file exists.
The only things that need to change are input (bake) and one narrow
serving-time guard (below).

### 11.2 New `bake.py` input mode: segment-list manifest

Alongside the existing "one `.ts` + `.markers.json`" input (§4.1), accept
a second input shape: a **segment-list manifest** — one entry per output
segment, in order:

```json
{
  "segments": [
    {"index": 0, "duration_ticks": 540000, "asset_boundary": true,  "media_file": "seg_000.m4s"},
    {"index": 1, "duration_ticks": 540000, "asset_boundary": false, "media_file": null},
    {"index": 2, "duration_ticks": 540000, "asset_boundary": false, "media_file": "seg_002.m4s"}
  ],
  "markers": [ /* same .markers.json shape as §2 */ ]
}
```

Bake behavior:

- For every entry with a real `media_file`, GPAC-remux/concat it into the
  loop package's segment store exactly as today (container-only, no
  transcode — §1's non-goal still holds).
- For every entry with `media_file: null`, write **no physical segment**
  for that index — but its `duration_ticks` and `asset_boundary` still
  contribute to `loop_descriptor.json`'s segment boundary list and to
  `total_loop_duration_ticks` (computed the normal way, §4.1 step 4's
  ground-truth requirement is unaffected: the *ledger* is always complete,
  only the *media* is sometimes absent).
- **Default: hard-fail** on any `media_file: null` entry, same
  fail-loud-by-default posture as every other `bake.py` validation (§4.1
  step 1). A new flag, `--allow-missing-segments` (or the equivalent
  its-a-live channel-config toggle, e.g. `[input] allow_missing_segments =
  true`), is required to accept them. This mode is opt-in, never the
  silent default.
- `asset_boundary` entries feed the same discontinuity/Period-restart
  mechanism `grave-robber/SCOPE.md` §6.1 asks `serve.py` to extend
  to fire at internal boundaries, not just the loop wrap.

**Rendition ladder extension.** A segment-list manifest may carry an ABR
ladder over one shared timeline (a VOD's variants are segment-aligned). A
top-level `renditions` list replaces the per-segment `media_file`:

```json
{
  "segments": [{"index": 0, "duration_ticks": 540000, "asset_boundary": true}, ...],
  "renditions": [
    {"name": "720p", "variant": {"bandwidth": 3000000, "codecs": "avc1.64001f"}, "media_files": ["a0.bin", null, ...]},
    {"name": "360p", "variant": {"bandwidth": 800000}, "media_files": ["b0.bin", "b1.bin", ...]}
  ],
  "markers": [ ... ]
}
```

Each `media_files` list has exactly one entry per segment; `name` is a
`[A-Za-z0-9_.-]+` directory/URL component, unique in the ladder. The
timing (`duration_ticks`, `asset_boundary`, `gap_ticks`) and `markers` are
shared, so every rendition gets the same boundary ticks. Holes (`null`)
and `--allow-missing-segments` apply per rendition, and every rendition
needs at least one real segment to probe. The first rendition is the
reference: the one shared audio track (a separate `audio_media_file`
playlist, or audio muxed into its video segments) is baked from it and
served under the ladder's single audio Representation/Rendition. Without
`renditions` the manifest is the classic single-rendition shape (rendition
name `archive`), which stays valid.

### 11.3 `serve.py` change: 404 on a missing segment, nothing else

The only `serve.py` change this mode needs: the segment **byte-serving**
handler, on a request for an index that has no physical file in the loop
package, returns a plain 404 instead of raising/crashing. Manifest
generation (§11.1) takes no part of this — it hands out the same segment
URI whether or not that lookup will succeed. This is intentionally the
smallest possible change to `serve.py`'s serving path.

### 11.4 Acceptance checklist for this extension

- [ ] `bake.py` accepts the segment-list input shape (§11.2) as an
      alternative to the single-`.ts` input, selected by input shape
      (directory/file vs. segment-list JSON), not a separate binary.
- [ ] `bake.py` hard-fails on any `media_file: null` entry unless
      `--allow-missing-segments` is passed.
- [ ] With `--allow-missing-segments`, `total_loop_duration_ticks` and
      every segment's position in `loop_descriptor.json` are correct and
      complete, including for indices with no physical media — verified
      against a segment-list input with at least one `null` entry in the
      middle of the sequence (not just at the end).
- [ ] The served manifest for such a package is indistinguishable in
      structure from one with full media (same segment count, `EXTINF`/`<S>`
      entries, discontinuities, SCTE-35) — no `#EXT-X-GAP`, no DASH
      timeline gap, anywhere.
- [ ] Requesting a segment with no physical file returns 404; requesting
      any other segment in the same package still succeeds normally.

## 12. Extension: PTS/DTS/PCR continuity across the loop wrap (`--continuous-timeline`)

**DECIDED.** §4.1 step 6 and §4.2 both rely on serving the exact same
physical segment bytes every loop iteration (immutable, byte-identical,
trivially CDN-cacheable forever) and signaling the resulting timestamp
restart with `#EXT-X-DISCONTINUITY` / a new DASH `<Period>` at every wrap
(see the comments on that in `serve.py`'s `_build_hls_media_playlist` and
`build_dash_manifest`). This section adds an **opt-in, mutually exclusive**
alternative, `--continuous-timeline`: instead of signaling the restart,
rewrite the served bytes' own timestamps per request so the wrap never
happens from the player's point of view. Both modes read the exact same
baked loop package -- this is a `serve.py`-only behavior switch, `bake.py`
is unchanged.

### 12.1 Why this is possible without transcoding

Every timestamp a container carries is a small number of fixed-width
integer fields (CMAF: one `tfdt` per fragment; MPEG-TS: PTS/DTS in every
PES header, PCR in scattered adaptation fields) that describe *when* a
sample plays, never *what* it is. Advancing all of them by the same
`shift_ticks = loop_number * total_loop_duration_ticks` for a given
loop iteration is a pure header rewrite -- no decode, no re-encode, no
re-mux -- implemented in `continuity.py`:

- `shift_cmaf_fragment`: a thin wrapper over `cmaf.rebase_fragments`
  (already used today for sparse/archive-mode rebasing, §11) -- O(1),
  since a fragment has exactly one `tfdt` anchor.
- `shift_ts_segment`: a full packet scan patching every PES PTS/DTS and
  adaptation-field PCR -- O(packet count), still header-only. PTS/DTS/PCR
  all wrap at their spec-defined 33-bit modulus (~26.5h at 90kHz) exactly
  as any sufficiently long-running real MPEG-TS stream already must --
  expected client-side behavior, not a new failure mode.

Both were validated against real ffmpeg-produced fmp4/TS fixtures in
`tests/test_continuity.py`: shifted output stays byte-length-identical
(sample data untouched) and still decodes/ffprobes correctly after a
multi-hour shift.

### 12.2 URL scheme: the global index, not the physical/local one

The default mode's segment URL intentionally encodes only the
loop-relative physical/local index (0..`segments_per_loop`-1) -- the whole
point being that one URL is reused, byte-identical, across every loop
iteration forever (§4.2). Continuity mode needs the opposite: the server
must know *which* loop iteration a given request's bytes should be shifted
into, and that can't be inferred purely from wall-clock time at request
time (a stale manifest, a slow client, or a CDN prefetch could land a
request for local index 0 on either side of the exact instant the server's
own "current loop" flips). So in continuity mode, `serve.py` puts the
**ever-increasing global segment number** in the segment URI instead
(`_build_hls_media_playlist`'s `global_index if self.continuous else
local_index`) -- DASH already did this by construction (`$Number$` /
`startNumber` was always the global number, see `build_dash_manifest`),
only HLS's own URI needed to change. The byte-serving routes then decode
it back (`Channel.loop_number_and_local_index`) to get both the physical
file to read and the exact shift to apply.

This is not the caching hazard it first looks like. A continuity-mode
segment response is a pure, deterministic, *permanent* function of its
URL alone (same physical file on disk, same `loop_number` arithmetic
derived straight from the URL's own global index, every time) -- there is
no staleness case, ever, so a CDN can cache a given URL indefinitely
without risk, exactly like the default mode's segments, and for the same
underlying reason: URL identity is content identity. Nothing here depends
on the CDN doing anything special -- it only needs the one thing every
CDN already does by default, include the full path in the cache key
(CloudFront/Cloudflare/Fastly all do), which `loop_stack.py`'s existing
segment cache policy already satisfies unmodified.

What *does* differ from default mode is cardinality, not correctness: a
fixed, perpetually-hot set of `segments_per_loop` keys becomes an
ever-growing set (one new key roughly every segment duration, forever).
A CDN's cache therefore holds a rolling window of recent global indices
and lets old ones fall out via ordinary LRU eviction once nothing is
requesting them any more (they've aged out of the live/DVR window) --
this is normal live-CDN behavior, not a new failure mode, and it's why a
short-ish `max_ttl` is still sensible (avoid holding dead weight
indefinitely), not because a longer one would ever serve something wrong.

### 12.3 Manifest shape: no discontinuity left to signal

- **HLS**: `#EXT-X-DISCONTINUITY` is simply never emitted at the loop wrap
  (`_build_hls_media_playlist`'s `and not self.continuous` guard), and
  `#EXT-X-DISCONTINUITY-SEQUENCE` is always `0` (there is never a
  discontinuity to count).
- **DASH**: one single, never-restarted `<Period id="continuous"
  start="PT0S">` whose `<SegmentTimeline>` `t` values are each segment's
  real ABSOLUTE tick position (`loop_number * total_loop_duration_ticks +`
  its loop-relative tick) -- continuously increasing across every wrap,
  matching what the byte-serving routes actually rewrite the segments'
  `tfdt` to (`Channel._build_dash_manifest_continuous`). This is the direct
  DASH analog of dropping the HLS discontinuity tag: the default mode's
  one-`<Period>`-per-loop restart (`build_dash_manifest`'s own docstring)
  has nothing left to restart *for* once the underlying media is genuinely
  continuous.

### 12.4 Preconditions, checked once at startup (`Channel._validate_continuous`)

Continuity mode hard-fails at `Channel`/`create_app` construction, never
partway through serving, if either holds:

- **Internal asset-boundary discontinuities** are present
  (`package.boundaries != {0}`) -- this first cut only makes the loop-wrap
  boundary continuous; an internal join (grave-robber/§6.1) is a different,
  harder problem (the gap/overlap math of §6.2's declared-position split
  would need an equivalent tick shift derived from `gap_ticks`, not
  attempted here). This is the *only* gate -- see §12.6 for why a sparse
  package with no internal joins (`boundaries == {0}`) is allowed through,
  not rejected outright just for being sparse.
- **A CMAF fragment was baked with a 32-bit (v0) `tfdt`** instead of
  64-bit (v1). The GPAC bake now explicitly requests `tfdt64` on the
  `mp4mx` muxer, so newly baked CMAF packages satisfy this requirement.
  This is a hard requirement, not a preference: continuity mode adds
  `loop_number * total_loop_duration_ticks` to a `tfdt` on every
  request for the lifetime of a channel meant to run forever, and a v0
  `tfdt` (max ~47,721s ≈ 13.25h of ticks) **will** eventually overflow no
  matter how large `total_loop_duration_ticks` is -- checked once per
  rendition (+ shared audio) against the real baked segment on disk, not
  assumed. ffmpeg's own `frag_keyframe` fmp4 muxer already defaults to v1
  (confirmed while building `tests/test_continuity.py`'s fixtures); GPAC's
  default is not relied upon; bake.py forces 64-bit timestamps explicitly.

### 12.5 Known limitations of this first cut

- **DASH `EventStream` id uniqueness assumes a DVR window never spans more
  than two loop iterations at once.** A recurring marker needs a distinct
  `id` per loop iteration it's re-emitted at within the single, ever-open
  Period (unlike the default mode's one-id-namespace-per-`<Period>`), or
  dash.js's `EventController` silently drops the second occurrence as a
  duplicate. `_build_dash_manifest_continuous` folds in only
  `loop_number & 1` (one bit) for this -- correct for any realistic
  `--window-segments`/`--dvr-window-seconds` vs. loop-duration
  combination, but not a general solution for a pathologically short loop
  relative to the window. A full fix (e.g. folding in more of
  `loop_number`, bounded by `xs:unsignedInt`'s 32-bit ceiling the same way
  `increment_event_ids` already has to reason about it, §"increment_event_ids"
  above) is a follow-up, not attempted here.
- **its-a-live's `channel.py`/`config.toml` wiring is a follow-up.** This
  section only adds `serve.py`'s own `--continuous-timeline` CLI flag (and
  `wsgi.py`'s `CONTINUOUS_TIMELINE` env var) -- surfacing it as an
  its-a-live `[serving]`-style config key (parallel to `[markers]`'s own
  flags) is mechanical but not done in this pass.
- **Existing packages must be rebaked** after upgrading loop-dee-loop to
  get 64-bit `tfdt` boxes. The startup check (§12.4) remains as a guard for
  older packages and any other unsupported input.

### 12.6 DECIDED: sparse (grave-robber/archive) input is allowed when the source itself has no discontinuity

§12.4's original cut rejected every sparse rendition outright, on the
theory that "archive input" and "has internal discontinuities" were the
same thing. They aren't: `package.boundaries != {0}` -- already computed
from the segment-list manifest's own declared `asset_boundary` entries
(§11.2), independent of `.sparse` -- is the actual, precise answer to "does
the *source* have a discontinuity/multiple-Period join anywhere in it".
A grave-robber capture that happens to be one continuous span end to end
(no internal `asset_boundary: true` entries beyond the always-implicit
index 0) is, as far as continuity is concerned, mechanically identical to
a franken-ts encode: one span of segments to shift by a constant
`loop_number * total_loop_duration_ticks` per iteration. So
`Channel._validate_continuous` now only checks `boundaries != {0}` --
`.sparse` itself is no longer a blanket rejection.

What this does *not* change: an archive capture with real internal joins
(the common case -- HAR/Proxyman captures routinely have gaps) is still
rejected exactly as before, for exactly the reason §12.4 originally gave
(the `gap_ticks`/declared-position math would need its own per-boundary
shift, and -- separately, see the conversation that led here -- even with
that math, independently-captured segments across a real join aren't
guaranteed to be decode-continuous the way one continuous encode wrapping
on itself provably is; that's a different, harder problem than a missing
formula).

Mechanical consequences of allowing a sparse-but-single-span package
through, all handled in `serve.py`:

- `shift_cmaf_fragment` needed no change: `cmaf.rebase_fragments`/`_find`
  walk top-level boxes by type, so a self-initializing segment's leading
  `ftyp`+`moov` (absent from a normal shared-init fragment) is simply
  carried through untouched while the `moof`/`tfdt` inside is patched --
  verified in `tests/test_serve_continuity.py` against a real,
  independently-encoded ffmpeg fixture.
- `_build_dash_manifest_continuous`'s video/audio `initialization=`
  handling, previously hardcoded to a shared `init.mp4` (safe only because
  every package that reached it was non-sparse), now makes the same
  three-way choice (`self_initializing` -> no `initialization=` attribute
  at all; `shared_init` -> per-span `init_0.mp4`, span always 0 since
  `boundaries == {0}` means exactly one span; else -> plain `init.mp4`)
  `build_dash_manifest`'s own per-Period builder already made.
- The 64-bit `tfdt` startup check (§12.4) now looks past a possible hole at
  segment index 0 (a sparse package's first declared segment may have no
  physical media) to the first *present* segment, rather than assuming
  index 0 exists.
- A hole stays a 404 in continuity mode exactly as in the default mode
  (§11.3) -- `shift_ticks` is only ever applied to bytes that were
  actually read from disk, never synthesized for a missing index.

Still out of scope, unchanged from §12.4: a package with any internal
`asset_boundary` beyond index 0. Extending continuity across those is the
harder problem described above and in §12.4's original bullet, not
attempted here.

## 13. Extension: startover + catchup (time-shifted playback via query params)

**DECIDED** (design agreed in interview; implemented in `serve.py` / `timeshift.py`). Adds two
playback modes next to the live DVR window, served from the *same*
manifest URLs, selected purely by query parameters. No bake changes, no
segment-byte changes, no new stored state.

### 13.1 Why this is cheap

§4.2's serve path is a pure function of `(now, epoch, package)`. Live
builds a window ending at `now`; time-shifted playback builds a window
over an explicit `[start, end]` instead. `loop_math.compute_loop_position`
already resolves any instant >= epoch, and segment bytes for a past
instant are identical to what was served then (default mode: unmodified
file; continuous mode: a pure function of the global index, §12.2).

### 13.2 DECIDED: scope and assumptions

- **The epoch never changes.** History is *derived*, not recorded. A
  re-`start --epoch-utc` or a re-bake silently changes what the past
  contained; this is a documented caveat, not something serve.py detects.
  (Mitigation only: expose `epoch` in `/health` and as a manifest comment.)
- **Any time back** to the epoch; `start < epoch` -> 400.
- **Max span** `max_span_seconds` (default 21600 = 6h, channel TOML);
  `end - start` above it -> 400. `start > now` or `end <= start` -> 400.
- **Open, same as live.** No auth, no signing. Params are plain query strings.
- **Snap to segment boundaries.** `start` snaps down, `end` snaps up to the
  enclosing segment edge. No partial segments, no re-mux. An exact in-point
  may be hinted (`EXT-X-START:TIME-OFFSET` / DASH `presentationTimeOffset`)
  but that is optional polish, not required for v1.
- **SCTE-35 markers carry through** for every range, same DATERANGE /
  EventStream authoring as live (`scte35_signaling.py`), extended to a
  multi-loop span (see 13.6).
- **Same URL as live.** `master.m3u8` / `manifest.mpd` (and each child
  playlist) behave as live when no params are present.

### 13.3 Parameters

Names are configurable per channel; values accept epoch (seconds, or
milliseconds when > 1e11) or ISO8601 (`Z` or numeric offset).

```toml
[timeshift]
enabled            = true
start_param        = "start"
end_param          = "end"
continuous_param   = "continuous_timeline"   # per-request override, bool
full_loop_param    = "full_loop"             # bool: widen range to whole loops (13.5b)
max_span_seconds   = 21600
```

Plumbing: `its-a-live` TOML -> `serve.py` CLI flags and `wsgi.py` env vars
(same route as `CONTINUOUS_TIMELINE` today, `loop_stack.py` /
`_local_docker_ops.py`). Unknown/garbled values -> 400 with a message.

### 13.4 Behavior matrix

| `start` | `end` | HLS | DASH |
|---|---|---|---|
| - | - | live DVR window (unchanged) | unchanged |
| set | set, `<= now` | VOD: full range, `#EXT-X-ENDLIST`, `PLAYLIST-TYPE:VOD` | `type="static"`, `mediaPresentationDuration` |
| set | set, `> now` | EVENT-style: from `start`, grows with live edge, no trimming; `ENDLIST` appended once `end` passes | `dynamic`, fixed `availabilityStartTime`=start, no `timeShiftBufferDepth` trim; `static` once `end` passes |
| set | - | as above, ends at `start + max_span_seconds` | as above |
| - | set | 400 (`end` requires `start`) | 400 |

"Live edge" for a still-growing range is the same `now`-derived edge live
uses; nothing else about segment availability changes.

### 13.5 DECIDED: per-request `continuous_timeline` override

`--continuous-timeline` (§12) is a startup flag today. It becomes the
*default* for the new per-request `continuous_param`; the param overrides
it for that request. Startup preconditions (§12.4: `boundaries == {0}`,
64-bit `tfdt`) are computed once and cached; `continuous=true` on a
package that fails them -> 400, never a silent fallback.

This forces a URL-scheme decision, because the segment URL means
different things in the two modes (§12.2: local index vs. global index)
and one server must serve both:

- `/<rendition>/seg/<local>.{m4s,ts}` -> always the *local* physical index
  (default-mode semantics, byte-identical, discontinuity-signaled).
- `/<rendition>/cseg/<global>.{m4s,ts}` -> always the *global* index
  (continuous semantics, timestamps rewritten per §12.2).
- A manifest emits whichever form matches the mode it was built in. The
  choice is therefore encoded in the path, so CDN cache keys stay correct
  with no query-string dependence for segments.
- **Breaking change to note:** continuous-mode live today serves global
  indices under `/seg/`. Moving them to `/cseg/` invalidates warm CDN
  caches once and breaks any player holding an old manifest across the
  deploy. Acceptable at a deploy boundary; call it out in the changelog.
  (Alternative: keep `/seg/` = channel-default mode and add the *other*
  form under a new prefix, avoiding the break but making URL meaning depend
  on deployment config. Rejected as harder to reason about.)

Non-continuous ranges spanning several loop wraps emit one
`#EXT-X-DISCONTINUITY` (HLS) / one `<Period>` (DASH) per wrap. A 6h span
over a 10-minute loop is ~36. This is accepted (see interview); players
that dislike it can request `continuous_timeline=true`.

Existing limit that catchup makes reachable: MPEG-TS PTS/DTS/PCR wrap at
33 bits (~26.5h), and continuous mode's shift of `loop_number *
total_loop_duration_ticks` grows without bound since the epoch. DECIDED
(not a 400): time-shifted continuous **HLS-TS** requests shift timestamps
relative to the *range origin* instead of the epoch, via a third segment
path:

- `/<rendition>/rseg/<origin_loop>/<global>.ts` -> shift =
  `(loop(global) - origin_loop) * total_loop_duration_ticks`, where
  `origin_loop` is the loop number containing the snapped range start.
  Timestamps stay within (range span + one loop) of zero however long after
  the epoch the range is. The origin is in the path, so URL identity is
  still content identity and CDN caching is unaffected.
- Used only for continuous + HLS-TS + a `start` param. Live and CMAF/DASH
  keep `/cseg/` (64-bit `tfdt` never wraps; live TS wraps like any
  long-running TS stream, as before).

Open-ended startover (`start` only): capped at `start + max_span_seconds`;
the manifest becomes an ended VOD after that. An explicit `end` further than
`max_span_seconds` from `start` (measured after any `full_loop` snapping) is
a 400.

Time-shifted DASH: `availabilityStartTime` = wall clock of the snapped range
start, Period/timeline rebased so the range begins at presentation time 0
(non-continuous: Period `start` rebased; continuous: `presentationTimeOffset`).
Ended ranges are `type="static"` with `mediaPresentationDuration` and no
`availabilityStartTime`.

### 13.5b DECIDED: `full_loop` (whole-loop snapping)

A fourth, boolean, configurable parameter (`full_loop_param`, default
`full_loop`). When true, the range is widened to whole loop iterations
*before* segment snapping:

- `start` -> the start of the loop iteration containing it (nearest loop
  start at or before `start`);
- `end` (if given) -> the end of the loop iteration containing `end - 1`
  (nearest loop end at or after `end`; an `end` exactly on a loop boundary
  is unchanged);
- no `end` -> the implicit `start + max_span_seconds` cap is rounded *down*
  to whole loops (minimum one loop; a single loop longer than
  `max_span_seconds` -> 400).

`max_span_seconds` is enforced on the widened range. `full_loop` without
`start` is ignored (live is unaffected).

### 13.6 Implementation notes (serve.py / scte35_signaling.py)

1. Introduce a `Window(start_ticks, end_ticks|None, growing: bool)` value
   and make `_build_hls_media_playlist`, `build_dash_manifest` and
   `_build_dash_manifest_continuous` take it; live becomes
   `Window(now - dvr, now, growing=True)`. `window_segments` becomes a
   derived quantity, not an input, for time-shifted requests.
2. The master playlist / HLS child-playlist links must **propagate** the
   request's timeshift query params (start/end/continuous) onto every
   variant, audio and iframe URI, otherwise players fetch live child
   playlists. DASH is one MPD, so segment templates need nothing.
3. Replace the DASH-continuous `loop_number & 1` EventStream id trick
   (§12.5 first bullet) with an id that folds in the full loop number,
   bounded to `xs:unsignedInt`, since a range spans arbitrarily many loops.
4. `scte35_signaling` already remaps markers per window; extend it to
   iterate every loop iteration intersecting `[start, end]`.
5. Cache headers: VOD/ended ranges `Cache-Control: public, max-age=31536000,
   immutable`; growing ranges and live `max-age` ~ one segment duration.
   `loop_stack.py` must add the configured start/end/continuous param names
   to the CloudFront manifest cache key (and only manifests; segments are
   path-keyed). **Missing this serves the wrong range to other viewers.**
6. `/health`: add `epoch_utc`, `timeshift` config, `max_span_seconds`.
7. Sparse packages: holes 404 exactly as today (§11.3).

### 13.7 Acceptance checklist

- [ ] No params -> byte-identical manifests to today (regression).
- [ ] VOD range, single loop and multi-loop, HLS + DASH, both modes: manifest
      valid, `ENDLIST`/`static`, segment count == snapped-range / segment dur.
- [ ] Growing range with future `end` flips to ended form after `end` (inject
      clock).
- [ ] start/end snap to boundaries; ISO8601 and epoch (s and ms) parse to the
      same instant; bad values / `start < epoch` / span > max -> 400.
- [ ] `continuous_timeline` override: both forms resolve through
      `/seg/` vs `/cseg/`; unsupported package -> 400; PTS-wrap guard.
- [ ] Discontinuity count == number of loop wraps in range (non-continuous).
- [ ] SCTE-35 markers present and id-unique across a multi-loop range
      (ffprobe / dash.js EventController duplicate check).
- [ ] Child playlist URIs carry the timeshift params; CloudFront cache key
      includes them (verify in `loop_stack.py` synth output).
- [ ] `tests/test_loop_math.py` still passes; no float tick state introduced.
