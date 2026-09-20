# Agent reference — franken-ts

Phase 1+2 of the pipeline (see repo-root [`AGENTS.md`](../AGENTS.md)):
turns a YAML content/ad-break definition into a broadcast-ready `.ts` with
real SCTE-35 markers, plus a `.markers.json` sidecar consumed downstream
by `loop-dee-loop` (never re-derived by re-probing the `.ts`).

This file is a condensed reference for driving the tool programmatically.
For prose/examples, see [`README.md`](./README.md) — this doc exists so
an agent doesn't have to re-derive config semantics from source.

## What this tool does NOT decide

`franken-ts` only produces content; it has no opinion on how the channel
is deployed. The `[deploy].backend` choice (`aws-media` vs `ecs-express`)
lives in `its-a-live`'s config, not here — but it **does** determine which
`output` mode you must use in the YAML (see below), so get that decision
from the user (or `its-a-live/AGENTS.md`) before writing the config.

## Command

```bash
uv run franken-ts <config.yaml> [--verify] [--dry-run]
```

- `--dry-run` — print every ffmpeg/tsduck command without executing;
  use this first to sanity-check a config an agent just wrote.
- `--verify` — re-extract markers after injection and generate an HTML
  report; cheap correctness check before handing output downstream.
- Full flag reference: see `README.md` → "CLI reference".

## Config schema (YAML)

### `output` — exactly one of two mutually exclusive modes

| Mode | Fields | When to use |
|---|---|---|
| Single-rendition | `output.file` (required path) | `aws-media` backend (MediaLive transcodes live from one file), or `ecs-express` with no ABR ladder needed |
| Multi-rendition (ABR ladder) | `output.dir` (required) + `output.renditions: [{name, resolution, bitrate_kbps}, ...]` | `ecs-express` backend when you want an adaptive-bitrate ladder — writes `<dir>/<rendition.name>.ts` per rendition + one shared `<dir>/markers.json` |

Shared `output` fields (either mode): `resolution` (default `1920x1080`,
single-rendition only — renditions set their own), `framerate` (default
25), `bitrate_kbps` (default 10000, single-rendition only), `gop`
(default `framerate * 2`), `service_provider`, `service_name`.

### `normalize` (bool, default `false`)

Set `true` if source assets don't already share the target
resolution/framerate/codec — auto-transcodes mismatched inputs instead of
hard-failing.

### `slate_image` (optional, top-level)

Global cross-dissolve image used by any asset's `fade_in`/`fade_out` that
doesn't set its own `slate_image`. Requires an actual fade to have effect.

### `assets` — ordered list, each item one of:

Every asset: `file` (path, required), `start` (optional offset),
`duration` (optional length) — omit both to use the whole file. Time
values accept `HH:MM:SS[.mmm]`, plain seconds, or human phrases like
`"10 min"` / `"1 hour 30 min"`.

Optional per-asset: `countdown` (seconds of corner-bug overlay before the
next element, or `-1` for the whole clip), `fade_in`/`fade_out`
(seconds), `slate_image` (per-asset override).

**Ad markers** — add an `ad_break` block to mark an asset as an ad
(anything without `ad_break` is content):

```yaml
ad_break:
  event_id: 1                    # unique int per break (start+stop pair reuse the same id)
  splice_type: splice_insert     # splice_insert | time_signal
  # time_signal only, optional:
  segmentation:
    type_id: "0x34"               # SCTE-35 segmentation_type_id, e.g. 0x34 = provider ad
    upid_type: "0x09"
    upid_hex: "53 49 47 4e 41 4c 3a 43 52"
    web_delivery_allowed: true
    no_regional_blackout: false
    archive_allowed: false
    device_restrictions: 1
```

- `splice_insert` — two-point out/in splice, no segmentation block needed.
- `time_signal` — needs `segmentation`; `segmentation_duration` derives
  from the asset's duration unless overridden.

## What to ask the user before writing a config (if not already specified)

1. **Source assets** — file paths, and for each: is it content or an ad
   (and if ad, `event_id`/`splice_type`/segmentation UPID)?
2. **Single file vs ABR ladder output** — depends on the chosen `its-a-live`
   backend (see table above); ask if unknown, don't assume.
3. **Output location** — defaults to `../outputs/<name>.ts` (or
   `../outputs/<name>/` for ladders) relative to `franken-ts/`; confirm if
   the user wants a specific name/path (this feeds `its-a-live`'s
   `[input].source_path`).
4. **Visual polish** — countdowns, fades, slates: only ask if the user
   mentioned wanting them; otherwise omit (no defaults imposed).
5. **`normalize`** — only needed if source assets have mismatched
   resolution/framerate/codec; leave `false` by default.

## Output

- `outputs/<name>.ts` (or `outputs/<name>/*.ts` + shared `markers.json`
  for ladders) — feed this path into `its-a-live`'s config
  `[input].source_path` (see `../AGENTS.md` and `its-a-live/AGENTS.md`).
- `.markers.json` is always written alongside `.ts` (or once per ladder
  dir) — the single source of truth for SCTE-35 timing downstream; never
  regenerate it by hand.
