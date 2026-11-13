# Playlist format (franken-ts YAML)

A **playlist** is the YAML file franken-ts builds into a `.ts` with SCTE-35
markers. It lives in `data/playlists/` and has four parts: where the output goes
(`output`), the clips that make up the loop (`assets`), the ad breaks and other
SCTE-35 events (`markers`), and optional polish (`osd`, `slate_image`). You can
write it by hand or use Igor's playlist editor, which edits the same file.

```bash
uv run franken-ts data/playlists/my-loop.yaml --verify
```

This page is a guide. The exhaustive reference, written for agents, is
[`franken-ts/AGENTS.md`](../franken-ts/AGENTS.md); the marker rules per numbering scheme
are in `SCTE35_MARKER_RULES.md` at the repo root.

## A complete example

```yaml
output:
  file: outputs/my-loop.ts
  resolution: 1280x720
  framerate: 25
  bitrate_kbps: 4000

assets:
  - id: content1
    file: /media/show-part1.mp4
  - id: ad1
    file: /media/ad-30s.mp4
    role: advert
  - id: content2
    file: /media/show-part2.mp4
    start: "00:01:00"
    duration: "10 min"
    fade_in: 1.5

markers:
  - event_id: 1
    type: ad
    splice_type: splice_insert
    assets: [ad1]
```

The loop is the assets in order: `content1`, then `ad1`, then ten minutes of
`content2`, then back to the start. The marker signals `ad1` as an ad break.

## `output`: where the result goes

Exactly one of two modes:

| Mode | Keys | Result |
|---|---|---|
| Single rendition | `file` | `<file>` plus `<file>.markers.json` |
| ABR ladder | `dir` and `renditions` | one `<dir>/<name>.ts` per rendition plus a shared `<dir>/markers.json` |

Which one to use depends on the channel backend: `aws-media` takes a single file
(MediaLive transcodes live); `ecs-express`, `aws-lambda` and `local-docker` can
serve a ladder.

```yaml
output:                       # ladder
  dir: outputs/my-loop
  resolution: 1920x1080       # top-level size: used for generated assets
  framerate: 25
  renditions:
    - {name: r1080, resolution: 1920x1080, bitrate_kbps: 5000}
    - {name: r720,  resolution: 1280x720,  bitrate_kbps: 2500, profile: main}
    - {name: r360,  resolution: 640x360,   bitrate_kbps: 800,  profile: baseline, level: "3.0"}
```

| Key | Default | Notes |
|---|---|---|
| `file` | | Single-rendition output path. |
| `dir`, `renditions` | | Ladder. Each rendition needs `name`, `resolution`, `bitrate_kbps`, and may set its own `profile`/`level`. |
| `resolution` | `1920x1080` | Single-rendition size. In a ladder each rendition has its own; the top-level one is only used to render generated assets. |
| `framerate` | `25` | |
| `bitrate_kbps` | `10000` | Single-rendition only. |
| `gop` | `framerate * 2` | Keyframe interval in frames. |
| `profile` | `high` | H.264 `baseline`, `main` or `high`; renditions may override. |
| `level` | `4.0` | H.264 level, e.g. `3.1`; renditions may override. |
| `service_provider`, `service_name` | `broadpeak`, `broadpeak.io` | MPEG-TS service descriptor. |

Relative paths are relative to where you run franken-ts, normally the repo root.

## `assets`: the loop's clips

An ordered list; the playlist loops back to the first asset after the last.

```yaml
assets:
  - id: content1                  # needed if a marker refers to it
    file: /media/show.mp4         # local path, https URL, or an HLS/DASH VOD manifest
    start: "00:01:00"             # trim: where to start (HH:MM:SS, seconds, or "1 min 30 sec")
    duration: "10 min"            # trim: how long. Omit start and duration for the whole file
```

| Key | Meaning |
|---|---|
| `file` | Source: a local path, a remote mp4 URL, or an HLS (`.m3u8`) / DASH (`.mpd`) **VOD** manifest URL (downloaded once and cached; add `headers:` if the CDN needs a Referer). |
| `generator` | Instead of `file`: render the clip with ffmpeg (test patterns, colours). See [Generated assets](./generator-assets.md). Exactly one of `file` / `generator`. |
| `id` | Name used by `markers` to refer to the asset. Required if any marker uses it. |
| `role` | `advert` or `jingle`. Only affects numbering under the `AF2M_SNPTV` scheme. |
| `start`, `duration` | Trim the source. Time values accept `HH:MM:SS[.mmm]`, plain seconds, or phrases like `10 min`. Not allowed with `generator`. |
| `fade_in`, `fade_out` | Cross-dissolve length in seconds, using the slate image. |
| `slate_image` | Per-asset override of the global `slate_image`. |
| `no_osd`, `osd_label` | Hide the on-screen display for this asset; or set the text shown by an `osd_label` corner. |

Still images (jpg/png) work as assets too, but need an explicit `duration`.
Every video must have one video and one audio stream.

## `markers`: ad breaks and other SCTE-35 events

A marker lists the **contiguous assets** it covers. Its start and end are
derived from where those assets sit in the built timeline, so reordering or
trimming assets moves the markers with them and nothing can fall out of sync.

```yaml
markers:
  - event_id: 1                  # unique number per marker
    type: ad                     # a label for grouping: ad | ppo | break | your own
    splice_type: splice_insert   # splice_insert | time_signal
    assets: [ad1]
```

| Key | Meaning |
|---|---|
| `event_id` | Unique integer per marker. |
| `splice_type` | `splice_insert`: a simple out/in pair. `time_signal`: needs a `segmentation` block. |
| `assets` | Ids of the contiguous run of assets it spans. |
| `unique_program_id` | `splice_insert` only. 16-bit integer or hex string (default `0x0001`). |
| `segmentation` | `time_signal` only: `type_id` (e.g. `"0x34"` provider advertisement start), `upid_type`, `upid_hex`, and the delivery restriction flags. |

**Nested markers**, such as a break containing a jingle and several ads, with a
placement opportunity over just the ads, are separate marker entries over the
appropriate sub-ranges. Nesting is expressed by one span containing another.

With `enforce_scte35_marker_semantics: true` (the default), franken-ts computes
the segment-number fields for you, following the scheme in
`scte35_numbering_scheme` (`SCTE35_2023R1` by default, `SCTE35_2019A` or
`AF2M_SNPTV`). Set it to `false` to pass your own `segmentation.segment_num` and
related values through unchanged. `break_numbering_supported: true` turns on
one-based break numbering.

## Optional polish

| Section | What it does |
|---|---|
| `slate_image` | Image used by `fade_in` / `fade_out` cross-dissolves. |
| `osd` | On-screen display (off by default): a progress bar (`mode`: `asset`, `loop` or `none`) and up to four text corners (`top_left` ... `bottom_right`), each showing e.g. `asset_id`, `time`, `loop_time`, `scte35_spans` or `is_adbreak`. Useful when checking a built loop by eye. |

```yaml
osd:
  enabled: true
  progress_bar: {mode: loop}
  corners: {top_left: asset_id, top_right: scte35_spans, bottom_left: time, bottom_right: loop_time}
```

## Check it before building

- `uv run franken-ts my-loop.yaml --dry-run` prints every ffmpeg and tsduck
  command without running them.
- `--verify` rebuilds nothing but re-reads the SCTE-35 markers from the result and
  writes an HTML report.
- Igor's playlist editor shows the timeline and resolved marker positions without
  building (**Resolve** in the Timeline & markers section), and Igor's `/api/v1/playlists/schema`
  returns the full JSON Schema.

## Where it goes next

The `.ts` (or the ladder folder) is the input of a channel: set the channel
config's `[input] source_path` to it. See the [overview](./overview.md) and
[CLI reference](./cli-reference.md).
