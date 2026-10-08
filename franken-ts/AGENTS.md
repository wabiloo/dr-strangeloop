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

`file` may also be an `http(s)://` URL to an **HLS (`.m3u8`) or DASH
(`.mpd`) manifest**, in addition to a local path or a flat remote mp4. The
manifest must be VOD (a closed/finite manifest — `#EXT-X-ENDLIST`/static
DASH `@type`), never live/open-ended: franken-ts needs a fixed duration to
trim and stitch, the same as any other asset. On first use, the highest
available video+audio rendition is downloaded and muxed into a local mp4
via `yt-dlp` (which must be on `PATH` — same failure mode as a missing
`ffmpeg`/`tsp`), cached by manifest URL under `<cache-dir>/streams/` so
repeat builds don't re-fetch it; `start`/`duration` then trim the
downloaded file exactly like a local/remote mp4 asset, and the same
manifest URL referenced by more than one asset entry is only downloaded
once. This resolution step always runs for real, even under `--dry-run`
(same as the existing ffprobe validation step) — a live manifest fails
the build immediately with a clear error rather than hanging.

Some CDN-hosted manifests reject requests that don't carry a matching
`Referer`/`User-Agent` (confirmed against a real public Bitmovin DASH test
stream, which 403s without one) — set per-asset `headers` (a string→string
map, e.g. `headers: {Referer: "https://example.com/"}`) to pass those
through to yt-dlp. Only meaningful for stream (`.m3u8`/`.mpd`) assets;
ignored for local files and flat remote mp4s.

Fragments are fetched `--stream-concurrency` at a time (default `4`;
yt-dlp's `-N`, sequential-only default is `1`) — 4 is deliberately
conservative for a small container (e.g. an ECS Fargate task running
franken-ts/loop-dee-loop's bake step): bandwidth-bound rather than CPU/
memory-bound, and most CDNs start rate-limiting/resetting well above
single-digit concurrent requests per client. Raise it if the source CDN
tolerates more and downloads are the bottleneck.

Optional per-asset: `fade_in`/`fade_out` (seconds), `slate_image`
(per-asset override), `no_osd` (bool, suppresses the OSD entirely for
this asset regardless of the playlist-level `osd` settings), `osd_label`
(free text, shown by a corner configured with the `osd_label` content
type; nothing shown if unset).

### `osd` — top-level (playlist-wide, not per-asset), on-screen display

`enabled` (bool, default `false`) is the master switch. When on: a
`progress_bar` (`mode`: `asset` (default) | `loop` | `none`; `height_pct`, %
of output height, default 3). `asset` grows a bar 0→100% width over each
asset's playback; `loop` draws a static whole-loop map of `height_pct`-high
rows (bottom: slate asset row alternating two shades per asset, with dividers; above it a single row of
non-instant SCTE-35 spans colored like Igor's lanes, outermost painted first,
dividers at every span edge, no labels) plus a playhead — the cache key then
includes the whole-loop layout, so any asset/span change re-extracts every
clip; plus up to 4
`corners` (`top_left`/`top_right`/`bottom_left`/`bottom_right`, default
`bottom_left: asset_id`/`bottom_right: time`, others unset), each one of
`asset_id`/`time` (elapsed/total seconds in the current asset, sub-second
with 2 decimal places, e.g. `12.32/34.60`)/`loop_time` (elapsed/total
seconds in the whole playlist loop, e.g. `83.45/754.00`)/`transition`
(5-second countdown before the next asset or loop boundary; uses the
asset's role or `Asset`, and `Loop End` for the last asset)/
`next_asset_id` (`next: {id}`, always resolves — playlist loops)/
`scte35_spans` (non-instant covering spans, shown as stable three-letter
codes and `/`-joined outermost-first, e.g. `BRK / PPO / PAD`; bare
`splice_insert` uses `SPI`)/`is_adbreak` (`ad_break_label` text
when covered by a break/ppo/ad-lane span)/`osd_label` (the asset's own
`osd_label` free text, nothing if unset). `text_size_pct`/`text_color`
apply uniformly to all corner text. `corner_box` (`enabled`/`color`,
default off) draws a fixed dark-gray box behind each corner's text plus a
`color`-accented vertical border on the box's outer edge, auto-sized from
the rendered text plus padding.

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
- `type` (`break`/`ppo`/`ad`/custom) is derived from
  `segmentation.type_id` for timeline grouping; do not author it separately.
- **Nested markers** (e.g. a `break` spanning a jingle + several `ad`s,
  with a `ppo` spanning just the ads): give each nesting level its own
  marker entry over the appropriate sub-range of asset ids. Nesting is
  expressed by span *containment*, not an authored tree. With semantic
  enforcement on, the type-aware hierarchy and profile determine which
  overlaps are valid and how all numbering fields are computed.
- Every `event_id` across all markers must be unique when semantic
  enforcement is on.

### Numbering and semantic enforcement

Playlist-level `enforce_scte35_marker_semantics` defaults to `true` and computes the four segmentation number fields. Set `scte35_numbering_scheme` to `SCTE35_2023R1` (default), `SCTE35_2019A`, or `AF2M_SNPTV`; `break_numbering_supported: true` opts into one-based Break numbering per Program or, without Program markers, per provider-defined interval. A Break's `break_interval` (positive integer, default 1) groups Breaks when no Program is present. An asset's optional **`role`** is `advert` or `jingle`, independent of the scheme; af2m numbers a Jingle Provider Advertisement `0x30` at the Break's opening as `0/n` and at its closing as `0/0`. Legacy `jingle_role` on assets or markers migrates to `role: jingle` on load. See root `SCTE35_MARKER_RULES.md` for detailed profile behavior. With enforcement off, explicit `segmentation.segment_num`, `segments_expected`, `sub_segment_num`, and `sub_segments_expected` pass through.

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
- `<name>.timeline.json` is also written alongside `.ts` (per rendition for
  ladders): where each source asset sits in the output. Not consumed by
  loop-dee-loop; `inspector-krogh` uses it to show asset joins that carry no
  marker. `--report-only` refreshes it without rebuilding.
