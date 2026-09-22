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

Every asset: `file` (path, required), `id` (string, optional but required
if any `markers` entry references it — see below), `start` (optional
offset), `duration` (optional length) — omit both to use the whole file.
Time values accept `HH:MM:SS[.mmm]`, plain seconds, or human phrases like
`"10 min"` / `"1 hour 30 min"`.

Optional per-asset: `fade_in`/`fade_out` (seconds), `slate_image`
(per-asset override), `no_osd` (bool, suppresses the OSD entirely for
this asset regardless of the playlist-level `osd` settings), `osd_label`
(free text, shown by a corner configured with the `osd_label` content
type; nothing shown if unset).

### `osd` — top-level (playlist-wide, not per-asset), on-screen display

`enabled` (bool, default `false`) is the master switch. When on: an
optional `countdown` progress bar (`enabled`/`height_pct`, % of output
height, grows 0→100% width over each asset's playback) plus up to 4
`corners` (`top_left`/`top_right`/`bottom_left`/`bottom_right`, default
`bottom_left: asset_id`/`bottom_right: time`, others unset), each one of
`asset_id`/`time` (elapsed/total seconds in the current asset, sub-second
with 2 decimal places, e.g. `12.32/34.60`)/
`next_asset_id` (`next: {id}`, always resolves — playlist loops)/
`scte35_spans` (non-instant covering spans, abbreviated and `/`-joined
outermost-first, e.g. `b / ppo / pa`)/`is_adbreak` (`ad_break_label` text
when covered by a break/ppo/ad-lane span)/`osd_label` (the asset's own
`osd_label` free text, nothing if unset). `text_size_pct`/`text_color`
apply uniformly to all corner text. `corner_box` (`enabled`/`color`,
default off) draws a same-color semi-transparent box behind each
corner's text, auto-sized from the rendered text plus padding.

### `markers` — top-level list (sibling of `assets`), the only way to signal ad breaks

Each marker names the contiguous run of asset `id`s it covers; its
start/end are always *derived* from those assets' resolved positions in
the built timeline, never authored directly — trimming/re-ordering
assets moves every marker over it automatically, nothing to desync:

```yaml
assets:
  - file: content1.mp4
  - file: ad1.mp4
    id: ad1                        # required: markers reference assets by id
  - file: content2.mp4

markers:
  - event_id: 1                    # unique int per marker (start+stop pair reuse the same id)
    type: ad                       # ad | ppo | break | any custom label
    splice_type: splice_insert     # splice_insert | time_signal
    assets: [ad1]                  # contiguous run of asset ids this marker covers
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
  from the marker's span duration unless overridden.
- `type` (`break`/`ppo`/`ad`/custom) is a label that also supplies a
  default `segmentation.type_id` (break→`0x22`, ppo→`0x34`, ad→`0x30`) —
  always overridable via an explicit `segmentation.type_id`.
- **Nested markers** (e.g. a `break` spanning a jingle + several `ad`s,
  with a `ppo` spanning just the ads): give each nesting level its own
  marker entry over the appropriate sub-range of asset ids. Nesting is
  expressed by span *containment*, not an authored tree — every pair of
  marker spans must be disjoint or one must strictly contain the other
  (validated at load time). `segmentation.segment_num`/`segments_expected`
  auto-fill from sibling position/count under the same immediate parent
  (e.g. the Nth of M placements in a break) unless set explicitly.
- Every `event_id` across all markers must be unique.

## What to ask the user before writing a config (if not already specified)

1. **Source assets** — file paths, and for each: is it content or an ad
   (and if ad, `event_id`/`splice_type`/segmentation UPID, and whether it's
   part of a larger nested break — see `markers` above)?
2. **Single file vs ABR ladder output** — depends on the chosen `its-a-live`
   backend (see table above); ask if unknown, don't assume.
3. **Output location** — defaults to `outputs/<name>.ts` (or
   `outputs/<name>/` for ladders), relative to the repo root (playlists
   live in `data/playlists/`, `franken-ts` is invoked from the repo root);
   confirm if the user wants a specific name/path (this feeds
   `its-a-live`'s `[input].source_path`).
4. **Visual polish** — OSD overlays, fades, slates: only ask if the user
   mentioned wanting them; otherwise omit (`osd.enabled` defaults `false`).
5. **`normalize`** — only needed if source assets have mismatched
   resolution/framerate/codec; leave `false` by default.

## Output

- `outputs/<name>.ts` (or `outputs/<name>/*.ts` + shared `markers.json`
  for ladders) — feed this path into `its-a-live`'s config
  `[input].source_path` (see `../AGENTS.md` and `its-a-live/AGENTS.md`).
- `.markers.json` is always written alongside `.ts` (or once per ladder
  dir) — the single source of truth for SCTE-35 timing downstream; never
  regenerate it by hand.
