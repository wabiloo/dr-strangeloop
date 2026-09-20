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
| `load_test.py` | Concurrent-viewer load generator (see `PERFS.md`). |

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
