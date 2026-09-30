# loop-dee-loop

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
- Python deps (`threefive`, `flask`): this package is a member of the
  repo-root uv workspace, not a standalone install -- from the repo root,
  `uv sync --all-packages` (see top-level README.md) gives you everything.
  `run.sh`/`bake.py`/`serve.py` invoked directly from this directory also
  resolve their deps via `uv run --project .` automatically if `uv` is on
  `PATH` (see `run.sh --python`'s default). `requirements.txt` exists only
  for the Docker image's plain `pip install` (see `Dockerfile`) -- keep it
  in sync with `pyproject.toml` by hand if you add a dependency.

## Usage

```bash
# Single rendition (legacy/quick-test mode)
./run.sh path/to/output.ts --output /var/loop-packages/2026-01-01 \
         --epoch-utc 2026-01-01T00:00:00Z --port 8080

# Multi-rendition ABR ladder: point at a directory instead of a .ts file.
# The rendition ladder is auto-discovered from disk -- no need to list
# renditions on the command line (see "Multi-rendition" below).
./run.sh path/to/mychannel/ --output /var/loop-packages/2026-01-01 \
         --epoch-utc 2026-01-01T00:00:00Z --port 8080

# Or run the two phases separately:
python3 bake.py path/to/output.ts --output /var/loop-packages/2026-01-01
python3 serve.py /var/loop-packages/2026-01-01 --epoch-utc 2026-01-01T00:00:00Z
```

`run.sh` also supports `--skip-bake` to (re)start serving an existing loop
package without re-baking, and auto-adds a locally-built `~/gpac-local/bin`
to `PATH` if present (see `Dockerfile`/`SCOPE.md` §6 for why a packaged GPAC
release isn't enough).

### HLS segment format

HLS defaults to CMAF (`.m4s` segments and init files). Select MPEG-TS
with `bake.py` or `run.sh --hls-format ts`. TS includes video and audio
in each rendition by default; `--no-hls-ts-mux-audio` produces a shared,
separate audio TS playlist instead. DASH always uses the CMAF fragments.
The same choices are available in its-a-live's `[packaging]` section as
`hls_format = "cmaf" | "ts"` and `hls_ts_mux_audio = true | false`.

### Multi-rendition (ABR ladder)

`bake.py`/`run.sh` accept either a single `.ts` file (one rendition) or a
**directory** containing a whole rendition ladder, produced by
`franken-ts`'s `output.dir` + `output.renditions` config (see
`franken-ts/README.md`):

```
outputs/mychannel/
  markers.json      <- exactly one, shared across every rendition
  1080p.ts
  720p.ts
  360p.ts
```

Every `*.ts` file in the directory becomes a rendition (name = filename
stem); `markers.json` is a fixed name, not pattern-matched. No CLI flag
enumerates renditions — the file set on disk *is* the ladder. Each
rendition is baked and cross-validated independently (same
`total_loop_duration_ticks`, same decoded SCTE-35 markers, hard failure on
any mismatch), and `serve.py` exposes one `#EXT-X-STREAM-INF`/DASH
`Representation` per rendition, ordered by real encoded bandwidth. Audio
is shared (baked once, from the highest-bandwidth rendition) rather than
duplicated per video rendition.

A single loose `.ts` file remains supported as a lightweight escape hatch
for quick one-off testing — it degenerates naturally into a ladder of one.

### Segment-list ("sparse") input mode (SCOPE.md §11)

Alongside a `.ts` file/rendition directory, `bake.py` also accepts a
**segment-list manifest** — a `.json` file (selected automatically by that
extension, no separate flag/binary) shaped like:

```json
{
  "segments": [
    {"index": 0, "duration_ticks": 540000, "asset_boundary": true,  "media_file": "seg_000.m4s"},
    {"index": 1, "duration_ticks": 540000, "asset_boundary": false, "media_file": null},
    {"index": 2, "duration_ticks": 540000, "asset_boundary": false, "media_file": "seg_002.m4s", "gap_ticks": 45000}
  ],
  "markers": [ /* same .markers.json shape as SCOPE.md §2 */ ]
}
```

`gap_ticks` is optional (absent/0 everywhere is the common case) and only
meaningful on an `asset_boundary: true` segment — a signed declared
dead-time (positive) or overlap (negative) at that join, feeding
`serve.py`'s declared-vs-serving position accumulator (grave-robber's own
`SCOPE.md` §6.2).

This is the input `grave-robber` produces from a captured HTTP archive
(HAR/Proxyman log) of a real HLS/DASH session — an inherently sparse
timeline where some segments' real media bytes were never captured. The
**ledger is always complete** regardless of media completeness:
`total_loop_duration_ticks` and every segment's boundary tick are derived
from the manifest's declared `duration_ticks`, and the served manifest
always looks fully populated (no `#EXT-X-GAP`, no DASH `SegmentTimeline`
gap) — a request for a missing segment's bytes 404s instead.

```bash
# Hard-fails if any segment has media_file: null.
python3 bake.py archive-manifest.json --output /var/loop-packages/archive-channel

# Accept missing segments (manifest-complete, media-optional):
python3 bake.py archive-manifest.json --output /var/loop-packages/archive-channel \
    --allow-missing-segments
```

An ABR ladder is a top-level `"renditions": [{"name", "variant",
"media_files": [...one per segment...]}]` list over the same shared
`segments` timing (SCOPE.md §11.2's ladder extension); `grave-robber
ingest-url` writes this from a VOD multivariant playlist / MPD. Each
rendition is baked into its own `segments/<name>/`, and `serve.py`
advertises them all. The first rendition is the reference that carries
the one shared audio track.

Known limitations of this mode: an archive capture (`grave-robber ingest`)
is still reduced to one reference rendition, only one audio track is kept,
and segments
are remuxed into self-initializing fragments (each carries its own `moov`)
rather than sharing one init segment, since sparse-mode segments come from
independently captured archive entries with no guaranteed common encoder
init.

### SCTE-35 signaling shape

`bake.py` accepts three flags controlling the shape of the HLS/DASH
SCTE-35 signaling `serve.py` later renders (recorded into
`loop_descriptor.json`, not re-decided per request); `its-a-live`
surfaces the same three as `config.toml`'s `[markers]` section (see
`its-a-live/AGENTS.md`):

```bash
python3 bake.py path/to/output.ts --output /var/loop-packages/2026-01-01 \
        --daterange-mode grouped --cue-tags alongside --increment-event-ids
```

- `--daterange-mode {shared,narrowed,grouped}` (default `shared`): how
  coincident segmentation descriptors (e.g. a Break start + nested PPO/Ad
  start, all at one PTS) are packed into `#EXT-X-DATERANGE` tags. `shared`
  emits one tag per descriptor, each carrying the full multi-descriptor
  message (the standard way, and what MediaPackage does too); `narrowed`
  re-encodes each tag's payload down to just that descriptor, for
  consumers that can't cope with more than one segmentation descriptor
  per message; `grouped` collapses a coincident group into a single tag.
- `--cue-tags {none,alongside,only}` (default `none`): whether to also
  emit `#EXT-X-CUE-OUT:DURATION=…` / `#EXT-X-CUE-OUT-CONT:ELAPSED-TIME=…`
  / `#EXT-X-CUE-IN` (no raw SCTE-35 payload) alongside `DATERANGE`, or
  instead of it entirely (`only` — requires every marker in the source
  `markers.json` to be a bare `splice_insert`; `bake.py` hard-fails
  otherwise). Both modes only ever build these tags from bare
  `splice_insert` markers — `alongside` still `DATERANGE`-tags every
  marker regardless of `splice_type`, but silently produces no
  `CUE-OUT`/`-IN` for a non-`splice_insert` one, since nested/overlapping
  `time_signal` segmentation types (e.g. a Break containing a shorter
  PPO containing a shorter Ad) have no single well-formed `CUE-OUT`/`-IN`
  pair to become the way a flat `splice_insert` avail does — layering one
  independent `CUE-OUT`/`-CONT`/`-IN` sequence per nested type instead
  produces multiple simultaneously-open, differently-timed "avails" on
  the same segments, which is a real signal but not one `CUE-OUT`/`-IN`
  (as opposed to `DATERANGE`, which tags each independently) can
  represent.
- `--increment-event-ids` (default off): bump every marker's
  `segmentation_event_id`/`splice_event_id` by `loop_number * step` each
  iteration — re-encoding the actual SCTE-35 bytes, not just the
  `DATERANGE`/DASH `<Event>` wrapper id. `step` is the smallest power of
  10 above the channel's largest base event id (e.g. base ids 100-190 →
  step 1000, so loop 1 emits 1100/1190, loop 2 emits 2100/2190, ...) —
  a single step shared by every marker, so each one's original base id
  stays recognizable as the low-order remainder of its incremented id,
  and the id at any given moment is predictable purely from wall-clock
  time against the channel's epoch (`loop_number` is itself always a
  deterministic function of that, never a runtime counter — see
  `compute_loop_position`), with no need to inspect a running process.
  Wraps the loop-number component back to 0 (i.e. the id back to its
  base) at the 32-bit SCTE-35 ceiling. Off by default (same id every
  loop, easiest to test against); on is more spec-correct.
- `--daterange-id-format` (default `{segcode}-{eventid}-{loop}`): customize
  HLS DATERANGE IDs using static text and placeholders `{loop}`, `{eventid}`,
  `{segid}`, `{seghex}`, `{segcode}`, `{segname}`, `{epoch}`, and `{pd}`.
  `{segname}` is the full lower-case type name with dashes, plus `-start` or
  `-end` (for example, `provider-advertisement-start`).

### Endpoints

| Path | What it is |
|---|---|
| `/index.m3u8` | HLS **multivariant** playlist — give players this URL, not a media playlist directly. |
| `/video[_N].m3u8` | HLS media playlist for one video rendition (video only). `_N` is the 1-based rendition index, present only when there is more than one rendition. |
| `/video_audio[_N].m3u8` | Same, when audio is muxed into the segments (`hls_format=ts` with `hls_ts_mux_audio`). |
| `/audio.m3u8` | HLS media playlist (audio, the one track shared across renditions) — only present if the source has an audio track and it isn't muxed into the video segments. |
| `/stream.mpd` | DASH MPD (one video `AdaptationSet` with one `Representation` per rendition, + one audio `AdaptationSet`). |
| `/<rendition>/init.mp4` | CMAF init segment for one rendition. |
| `/<rendition>/seg/<n>.m4s` | CMAF media segments for one rendition. |
| `/audio/init.mp4` | CMAF init segment (audio track). |
| `/audio/seg/<n>.m4s` | CMAF media segments (audio track). |

`#EXT-X-STREAM-INF`'s `BANDWIDTH`/`CODECS`/`RESOLUTION`/`FRAME-RATE`
attributes in `/index.m3u8` come from the exact values GPAC itself
computed while producing the real segments at bake time (read back from
its own generated `manifest.mpd` — see `bake.py`'s `read_variant_metadata`
— never re-derived/guessed).

### PTS/DTS/PCR continuity across the loop wrap (SCOPE.md §12)

By default, every loop wrap is signaled honestly as a timestamp
discontinuity (`#EXT-X-DISCONTINUITY` / a new DASH `<Period>`), since the
exact same physical segment bytes are reused every iteration. Passing
`--continuous-timeline` to `serve.py` (or `run.sh`) instead rewrites each
served segment's own timestamps per request -- CMAF `tfdt`, or every
MPEG-TS PES PTS/DTS and adaptation-field PCR -- to a genuinely
ever-increasing absolute position, so the whole channel presents as one
continuous timeline with no discontinuity/Period-restart at all:

```bash
python3 serve.py /var/loop-packages/2026-01-01 --epoch-utc ... --continuous-timeline
```

This is a pure header rewrite (`continuity.py`), never a re-mux or
re-encode. It requires a package with no internal asset-boundary
discontinuities (`boundaries == {0}` -- true for every franken-ts-authored
package, and for a §11/grave-robber archive capture that happens to be a
single continuous span with no internal joins, SCOPE.md §12.6) and CMAF
fragments baked with a 64-bit (v1) `tfdt` -- both checked once at startup,
hard-failing otherwise -- and it changes the segment URL scheme to carry
an ever-increasing global index rather than the plain per-loop physical
index. That's not a caching hazard (each URL's bytes are a permanent,
deterministic function of the URL itself, so a CDN can cache any one of
them indefinitely without ever risking stale content) -- it just means a
CDN sees an ever-growing set of cache keys instead of default mode's
small fixed one, aging old entries out via ordinary LRU as they leave the
live/DVR window rather than reusing the same handful of keys forever (see
SCOPE.md §12 for the full design and known limitations).

### Startover & catchup (SCOPE.md §13)

The normal manifest URLs also serve a **past range**, selected by query
parameters (opt in with `serve.py --timeshift`, or `[timeshift]` in an
its-a-live channel config; its-a-live enables it by default):

```bash
# Catchup: a finished VOD of a past range (ENDLIST / static MPD)
curl 'http://localhost:8080/index.m3u8?start=2026-09-30T08:00:00Z&end=2026-09-30T08:10:00Z'
curl 'http://localhost:8080/stream.mpd?start=1790755200&end=1790755800'       # epoch seconds (ms also accepted)

# Startover: plays from the start point, grows to the live edge (end optional)
curl 'http://localhost:8080/index.m3u8?start=2026-09-30T11:55:00Z'

# Whole loops only, and/or pick the timeline mode for this request
curl 'http://localhost:8080/index.m3u8?start=...&end=...&full-loops=true&timeline=periodic'

# The timeline override also works on plain live (no start needed)
curl 'http://localhost:8080/index.m3u8?timeline=continuous'

# Play the live stream as it was an hour ago (or will be in an hour with a positive offset)
curl 'http://localhost:8080/index.m3u8?offset=-PT1H'
```

| Param | Meaning |
|---|---|
| `start` (name configurable) | Range start: epoch seconds, epoch ms (≥ 1e11) or ISO 8601 (no zone = UTC). Snapped down to a segment boundary. Must be ≥ the channel epoch and not in the future. Without it the URL is plain live. |
| `end` (name configurable) | Optional range end (needs `start`). Past → catchup (VOD); future → startover that ends there; absent → startover capped at `start + max_span`. Snapped up to a segment boundary. |
| `full-loops` (fixed name) | Boolean. Widen the range to whole loops: `start` → nearest loop start at or before it, `end` → nearest loop end at or after it. |
| `offset` (fixed name) | Pretend "now" is earlier (negative) or later (positive): signed seconds (`-3600`) or an ISO 8601 duration (`-PT1H`, `P1DT2H`; days and below). The live edge and DVR window sit at `now + offset` and the manifest is what the server would have produced then — the epoch and all timestamps are unchanged. Works on plain live, and start/end are judged against the pretend-now. Dynamic DASH manifests carry a `UTCTiming` element with that time so players use it as their clock. `400` if it would put "now" before the channel epoch or is beyond ±10 years. |
| `timeline` (fixed name) | `default`, `continuous` or `periodic`; absent = `default` = the server's `--continuous-timeline` setting. Overrides it for this request, on live URLs too. `continuous` → `400` if the package can't be served continuously. |

Only the `start` and `end` names are configurable
(`--timeshift-start-param`, `--timeshift-end-param`;
`--timeshift-max-span-seconds`, default 21600); `full-loops`, `timeline` and
`offset` are fixed, and neither configured name may equal them. Bad input is a
plain-text `400`. Child playlists of an HLS master inherit the params
automatically.

Time-shifted HLS playlists carry `#EXT-X-PLAYLIST-TYPE` (`EVENT` while
growing, `VOD` once ended) and `#EXT-X-START:TIME-OFFSET=0`, so a generic
player begins at the requested start instead of joining near the live edge.
DASH has no manifest-side equivalent: a player given a growing (dynamic) MPD
needs to seek to presentation time 0 itself (dash.js: start time `0`).

Segment URLs: `/<r>/seg/<local>` is always the loop-local index;
`/<r>/cseg/<global>` the continuous-timeline (global index) form;
`/<r>/rseg/<origin_loop>/<global>.ts` continuous HLS-TS for a time-shifted
range, shifted relative to the range origin so 33-bit PTS never wraps. Every
one is path-keyed, so they cache forever at a CDN. **Manifests must be cached
per the param values** (its-a-live's CloudFront stack does this from
`[timeshift]`).

**History is derived, not recorded:** it is only right while the epoch and
the baked package are unchanged.

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
| `continuity.py` | `--continuous-timeline` mode's per-request CMAF `tfdt` / MPEG-TS PTS/DTS/PCR rewrite (`SCOPE.md` §12) -- a pure header patch, never a re-mux. |
| `load_test.py` | Concurrent-viewer load generator used for the measurements in `PERFS.md`. |

See `PERFS.md` for measured CPU/memory usage under realistic concurrent
load, and Fargate/EC2 sizing recommendations derived from it.

## Testing

```bash
# From the repo root (recommended -- uses the shared uv workspace venv):
uv run --directory loop-dee-loop pytest tests/ -q

# Or from this directory:
uv run --project . pytest tests/ -q
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

## Deploying to Fargate / ECS Express Mode

The image (see `Dockerfile`) never bakes in channel-specific content — only
code and the GPAC/ffmpeg toolchain. All franken-ts input and baked loop
packages live in S3, synced in/out at container start by
`docker-entrypoint.sh` (the image's `ENTRYPOINT`), driven by two env vars:

- `LOOP_PACKAGE_S3_URI` — used by both `serve.py` (synced down before
  starting) and `bake.py` (synced up after baking).
- `FRANKEN_TS_S3_URI` — used by `bake.py` only (synced down before baking).

When either var is set, **omit the corresponding positional path
argument** (and, for `bake.py`, `--output`) from the container `CMD` — the
shim supplies it. Without the var set, both scripts run exactly as given
(pure passthrough), e.g. for local `docker run` with a bind-mounted volume
— which is exactly how `../push-to-aws-loop/channel.py bake` uses this
image: it runs `bake.py` locally/via `docker run` (no cloud compute for
the one-shot bake step) and pushes the result to S3 with a plain
`aws s3 sync`, since baking once per schedule change doesn't warrant its
own cloud task definition.

This means one shared image + one shared CDK stack serve every
channel/schedule — only S3 prefixes differ. See `../push-to-aws-loop/` for
the full CDK project that provisions the long-running `serve` side on
**Amazon ECS Express Mode** (a simplified deployment mode built on
Fargate + a shared ALB — AWS's recommended replacement for App Runner,
which stopped onboarding new customers April 30, 2026) behind CloudFront.

## Known limitations in this initial implementation

- `bake.py` runs two full GPAC passes (one for DASH, one for HLS) rather
  than one dual-output invocation — acceptable per `SCOPE.md` §4.1 step 3
  ("either is fine, correctness matters more than invocation count") but
  doubles bake time.
- GPAC was built locally from `master` (no pinned commit yet) to validate
  this implementation — `Dockerfile` still needs a specific commit pinned
  once a version is chosen for production (`SCOPE.md` §7).
- Non-reference renditions in a ladder still have their audio track baked
  by GPAC as an unavoidable byproduct (it errors if asked to process a PID
  present in the input with no cues for it), even though only the
  reference rendition's audio is actually served — some redundant disk
  usage per non-reference rendition, not a correctness issue. Worth
  revisiting if storage becomes a concern (e.g. stripping the audio PID
  from non-reference renditions before they reach GPAC).
- `loop_descriptor.json` is versioned (`"version": 2` for multi-rendition
  support) but there is no migration path from `version: 1` packages —
  rebake with the current `bake.py` if you have an old package.
- The segment-list ("sparse") input mode (SCOPE.md §11) derives a
  best-effort RFC 6381 codec string from ffprobe's H.264 profile/level
  (constraint-set flag byte assumed zero) rather than reading a real
  encoder-declared value back from a GPAC dasher pass, since no such pass
  runs in this mode — not yet validated against a real archive capture
  (SCOPE.md's own "not yet spiked" note applies here too).
