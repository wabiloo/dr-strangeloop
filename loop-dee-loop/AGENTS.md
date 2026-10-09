# Agent reference — loop-dee-loop

Phase 3a of the pipeline (see repo-root [`AGENTS.md`](../AGENTS.md)):
self-hosted `ecs-express` backend for `its-a-live`. Bakes a `franken-ts`
`.ts` (+ `.markers.json`) into an immutable loop package once, then serves
it forever as live-sliding HLS/DASH with zero drift. See
[`README.md`](./README.md) for usage/module-layout and
[`SCOPE.md`](./SCOPE.md) for full design rationale (read `SCOPE.md`
before changing anything in `bake.py`/`serve.py`/`loop_math.py` — the
drift-freedom and GPAC-quirk constraints there are load-bearing, not
stylistic).

## Do you need to touch this tool directly?

**Usually no.** If you're deploying a channel with `ecs-express` backend,
`its-a-live/channel.py spark` calls `bake.py` for you and the deployed ECS
task calls `serve.py` for you — see `its-a-live/AGENTS.md`. Run this
tool's scripts directly only for:

- local dev/testing of a franken-ts output before deploying anything,
- debugging a baked package or a serving bug in isolation,
- anything explicitly `--skip-bake`/local-only per the README.

## Commands

```bash
# Both phases via the wrapper:
./run.sh <path/to/output.ts | path/to/rendition-dir/> \
         --output /var/loop-packages/<name> \
         --epoch-utc <ISO8601> --port 8080

# Or separately:
python3 bake.py <input.ts | rendition-dir> --output /var/loop-packages/<name>
# Optional: --hls-format ts --no-hls-ts-mux-audio (default: CMAF HLS; TS muxed audio)
python3 serve.py /var/loop-packages/<name> --epoch-utc <ISO8601>
```

- Input is either a single `.ts` file (one rendition) or a directory
  produced by franken-ts's `output.dir` + `output.renditions` (a
  `markers.json` + one `.ts` per rendition — auto-discovered, no CLI
  enumeration needed).
- `--epoch-utc` fixes the wall-clock instant the loop's `position 0`
  corresponds to; changing it forces viewers to a different point in the
  loop (this is what `its-a-live channel.py start --epoch-utc ...` sets).
- `--dvr-window-seconds` (default 30) or `--window-segments N` controls
  manifest sliding-window size.
- `run.sh --skip-bake` restarts serving an existing package without
  re-baking.

`bake.py` hard-fails (exit 2) on any mismatch between `.markers.json` and
the SCTE-35 actually embedded in the `.ts` — this is intentional (see
`SCOPE.md` §2/§4.1); never work around it by editing the markers file to
match, fix the upstream franken-ts input instead.

## Module layout

| File | Responsibility |
|---|---|
| `bake.py` | Validate input, build cues, run GPAC, compute ground-truth loop duration, author SCTE-35 signaling, write the loop package. |
| `gpac_pipeline.py` | DASHCues XML construction + GPAC CLI invocation. |
| `scte35_signaling.py` | Authors `EXT-X-DATERANGE`/DASH `<EventStream>` directly from `.markers.json` — never trust GPAC's own aggregation (unreliable for multi-marker content, see `SCOPE.md` §6). |
| `loop_math.py` | Integer epoch/loop_number/position_in_loop arithmetic — the drift-freedom guarantee (`SCOPE.md` §4.2/§5). No floats in persisted timing state, ever. |
| `serve.py` | Stateless HTTP serving of manifests + segments per request. |
| `continuity.py` | Opt-in `--continuous-timeline` mode (`SCOPE.md` §12): per-request CMAF `tfdt` / MPEG-TS PTS/DTS/PCR rewrite so the channel has no discontinuity/Period-restart at the loop wrap — header patch only, never a re-mux. |
| `timeshift.py` | Startover/catchup (`SCOPE.md` §13): query-param parsing (epoch s/ms or ISO8601), `TimeshiftConfig`, and integer-tick `resolve_window` (segment snapping, `full-loops` widening, max-span cap). Pure ints, no Flask. |
| `load_test.py` | Concurrent-viewer load generator (see `PERFS.md`). |

## Window JSON (`/timeline.json`)

`serve.py` exposes `/timeline.json`, documented in
[`openapi.yaml`](./openapi.yaml) (served at `/openapi.yaml`, rendered at
`/docs`). `Channel.build_window_json` shares its timeline maths with the
HLS/DASH builders through `Channel` helpers (window range, segment times,
discontinuities, Period grouping/ids, marker anchors -- see "shared timeline
plan" in `serve.py`): change those in one place, not in a builder. Only the
marker/asset *span* is a JSON-vs-manifest difference. `tests/test_serve_window_json.py` (schema drift + HLS DATERANGE /
DASH Period+Event consistency, both directions, with and without
`increment_event_ids`, periodic and continuous) and
`tests/test_serve_golden_manifests.py` (byte-exact hashes of 864 HLS/DASH
manifests -- regenerate with `UPDATE_GOLDEN=1` only for an intended manifest
change) guard it. Igor's Timeline panel draws it. Design/glossary: `SCOPE.md` §15.

## Forced Periods on SCTE-35 types

`serve.py --period-on-segmentation 0x22,0x30,...` (its-a-live `[markers]
period_on_segmentation`) opens a new Period / `#EXT-X-DISCONTINUITY` at
markers with those `segmentation_type_id`s, signal-only (timestamps stay
continuous under `--continuous-timeline`). Design: `SCOPE.md` §14; tests:
`tests/test_serve_signal_periods.py`.

## Startover & catchup

`serve.py --timeshift` (its-a-live: `[timeshift]`, on by default) makes the
normal `index.m3u8` / `stream.mpd` accept `start`, `end`, `full-loops`, `offset` (pretend "now" is earlier/later; `SCOPE.md` §13.7) and
`continuous_timeline` query params (names configurable) — catchup = VOD of a
past range, startover = live-style from a past point. Full reference and
examples: [`README.md`](./README.md) "Startover & catchup"; design:
`SCOPE.md` §13. Things an agent must not get wrong:

- **Segment URL forms are part of the contract**: `/seg/<local>` (loop-local),
  `/cseg/<global>` (continuous), `/rseg/<origin_loop>/<global>.ts`
  (continuous HLS-TS range). Continuous live URLs moved from `/seg/` to
  `/cseg/` — anything that assumed global indices under `/seg/` is stale.
- A CDN must key **manifests** on exactly the configured param names
  (its-a-live's `loop_stack.py` does), or viewers share each other's ranges.
- History is re-derived from the epoch + baked package, never recorded; a new
  `--epoch-utc` or a re-bake changes what past times contain.
- `tests/test_timeshift.py` covers parsing, range math and served manifests.

## Requirements

Python 3.11+, GPAC/`MP4Box` built from source with `scte35dec` (no
packaged release has it — see `Dockerfile` for pinned-commit build flags,
and **do not** pass `--disable-svg`), `ffmpeg`/`ffprobe` on `PATH`.

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

`tests/test_loop_math.py` is the required drift regression test; if you
change `loop_math.py`, this must still pass. `tests/test_bake_validation.py`
covers the markers.json ↔ decoded-SCTE-35 cross-validation.

## Known limitations

See `README.md` → "Known limitations in this initial implementation" for
the current list (double GPAC pass, unpinned GPAC commit, redundant
per-rendition audio, no `loop_descriptor.json` v1→v2 migration).
