# franken-ts

Stitch video assets together, bolt in ad breaks, and inject SCTE-35 markers — all from a single YAML file.

`franken-ts` automates the full pipeline from a list of source MP4s to a broadcast-ready MPEG-TS file with splice markers at every ad boundary. It uses **ffmpeg** for transcoding and **tsduck** for SCTE-35 injection.

## Prerequisites

- `ffmpeg` + `ffprobe` on your `PATH` — for the OSD overlay's `drawtext` filter,
  this must be a build with `--enable-libfreetype`; see [OSD](#on-screen-display-osd) below
- `tsp` (tsduck) on your `PATH`
- `yt-dlp` on your `PATH` — only needed if a playlist uses an HLS/DASH stream
  as an asset source (see below); installed automatically as a Python
  dependency, but the CLI itself must resolve on `PATH`
- Python environment set up from the repo root (`uv sync --all-packages`)

## Quick start

From the repo root:

```bash
uv run franken-ts data/playlists/example.yaml
```

With verification (extracts markers after injection and generates an HTML report):

```bash
uv run franken-ts data/playlists/example.yaml --verify
```

Dry run (prints all commands, executes nothing):

```bash
uv run franken-ts data/playlists/example.yaml --dry-run
```

Output is written to `outputs/`.

## Configuration

All inputs and options are declared in a YAML file. Put playlists in `data/playlists/` (repo root).

### Minimal example

```yaml
output:
  file: ../outputs/output.ts

assets:
  - file: movie.mp4
    duration: "10 min"

  - file: ad.mp4
    id: ad1
    duration: "2 min"

  - file: movie.mp4
    start: "10 min"

markers:
  - event_id: 1
    type: ad
    splice_type: splice_insert
    assets: [ad1]
```

### Full reference

```yaml
output:
  file: ../outputs/output.ts   # required — path relative to franken-ts/
  resolution: "1920x1080"      # default: 1920x1080
  framerate: 25                # default: 25
  bitrate_kbps: 10000          # default: 10000 (CBR)
  gop: 50                      # default: framerate × 2
  service_provider: "broadpeak"
  service_name: "broadpeak.io"

normalize: false               # set true to auto-fix mismatched inputs
slate_image: /path/to/slate.png  # optional — global cross-dissolve image for all fades

osd:                            # optional — on-screen display, see "On-screen display (OSD)" below
  enabled: true                 # default: false
  countdown:
    enabled: true                # default: true
    height_pct: 3                 # default: 3 (% of transcoded output height)
  text_size_pct: 3               # default: 3 (% of transcoded output height)
  text_color: "#FFFFFF"          # default: "#FFFFFF"
  ad_break_label: "ad break"     # default: "ad break"
  corner_box:                    # optional — dark box behind each corner's text
    enabled: true                # default: false
    color: "#000000"             # default: "#000000" — border accent color only, not the fill (see below)
  corners:
    top_left: asset_id           # default: null
    top_right: scte35_spans      # default: null
    bottom_left: asset_id        # default: asset_id
    bottom_right: time           # default: time

assets:
  - file: content.mp4
    start: "00:00:00"          # optional — start offset within the file
    duration: "10 min"         # optional — how much of the file to use
    fade_in: 1.5               # optional — fade in from black (or slate) for 1.5s
    fade_out: 2                # optional — fade out to black (or slate) for 2s
    slate_image: /path/to/slate.png  # optional — cross-dissolve image for fades
    no_osd: false               # optional — suppress the OSD entirely for this asset
    osd_label: "Weather"        # optional — free text shown by the osd_label corner content

  - file: ad.mp4
    id: ad1                    # required if referenced by a `markers` entry
    duration: "2 min"

  - file: ad2.mp4
    id: ad2
    duration: "3 min"

markers:
  - event_id: 1                # unique integer per marker
    type: ad
    splice_type: splice_insert  # splice_insert | time_signal
    assets: [ad1]

  - event_id: 2
    type: ad
    splice_type: time_signal
    assets: [ad2]
    segmentation:
      type_id: "0x34"
      upid_type: "0x09"
      upid_hex: "53 49 47 4e 41 4c 3a 43 52"
      web_delivery_allowed: true
      no_regional_blackout: false
      archive_allowed: false
      device_restrictions: 1
```

### Remote and stream (HLS/DASH) asset sources

`file` accepts more than a local path:

```yaml
assets:
  - file: https://example.com/movie.mp4          # flat remote mp4 — read directly by ffmpeg
    duration: "10 min"

  - file: https://example.com/vod/master.m3u8    # HLS manifest
    id: promo
    duration: "30s"

  - file: https://example.com/vod/manifest.mpd   # DASH manifest
    start: "5s"
```

A `.m3u8`/`.mpd` URL must be a **VOD (closed/finite) manifest** — a live or
open-ended stream has no fixed duration, so it can't be trimmed/stitched
like every other asset, and franken-ts refuses it with a clear error rather
than trying. On first use, the highest-bitrate rendition is downloaded and
muxed into a local mp4 via `yt-dlp` (must be on `PATH`), cached by manifest
URL so repeat builds/renditions don't re-fetch it; `start`/`duration` then
trim the downloaded file exactly like any other asset. This download always
runs for real, even under `--dry-run` (same as the ffprobe validation step
that already probes remote mp4s).

### Nested markers (breaks, placements, ads)

`markers` is a flat list — nesting (e.g. a `break` spanning a jingle plus
several `ad`s, with a `ppo` spanning just the ads) is expressed by having
each level's marker name the sub-range of asset `id`s it covers, not by
an authored tree:

```yaml
assets:
  - file: content1.mp4
  - file: jingle.mp4
    id: jingle
  - file: ad1.mp4
    id: ad1
  - file: ad2.mp4
    id: ad2
  - file: content2.mp4

markers:
  - event_id: 100
    type: break                  # BreakStart/End (0x22/0x23)
    splice_type: time_signal
    assets: [jingle, ad1, ad2]
    segmentation: { upid_hex: "aa" }

  - event_id: 101
    type: ppo                    # ProviderPlacementOpportunity (0x34/0x35)
    splice_type: time_signal
    assets: [ad1, ad2]
    segmentation: { upid_hex: "bb" }

  - event_id: 102
    type: ad                     # ProviderAdvertisement (0x30/0x31)
    splice_type: time_signal
    assets: [ad1]
    segmentation: { upid_hex: "cc" }

  - event_id: 103
    type: ad
    splice_type: time_signal
    assets: [ad2]
    segmentation: { upid_hex: "dd" }
```

Every marker's start/end are *derived* from its assets' resolved timeline
positions — never authored directly — so trimming/reordering assets moves
every enclosing marker automatically; there's nothing to desync. Two
marker spans must be disjoint or one must strictly contain the other
(validated at load time); `segmentation.segment_num`/`segments_expected`
auto-fill from sibling position/count under the same immediate parent
(e.g. the jingle is the break's first child, so it gets `segment_num: 0`
automatically) unless set explicitly. `type` (`break`/`ppo`/`ad`/anything
else) also supplies a default `segmentation.type_id` per the mapping
above, always overridable.

### Multi-rendition (ABR ladder) output

For a self-hosted ABR channel via `loop-dee-loop`, replace the single
`output.file` with `output.dir` + `output.renditions` — the same asset
timeline/ad-break schedule is encoded once per rendition, all sharing the
same forced-keyframe schedule (so segment boundaries and SCTE-35 marker
PTS come out byte-identical across renditions):

```yaml
output:
  dir: ../outputs/mychannel     # required in multi-rendition mode
  framerate: 25
  gop: 50
  service_provider: "broadpeak"
  service_name: "broadpeak.io"
  renditions:
    - name: "1080p"              # becomes <dir>/1080p.ts
      resolution: "1920x1080"
      bitrate_kbps: 10000
    - name: "720p"
      resolution: "1280x720"
      bitrate_kbps: 4500
    - name: "360p"
      resolution: "640x360"
      bitrate_kbps: 800

assets:
  - file: content.mp4
    duration: "10 min"
  # ...same as single-rendition mode
```

This writes `<dir>/<rendition.name>.ts` per rendition plus one shared
`<dir>/markers.json` (SCTE-35 timing is identical across renditions by
construction, so there's no need for — and it would be a hard failure if
there ever were — a different markers.json per rendition). `loop-dee-loop`
auto-discovers the whole ladder from this directory — no need to list
renditions again on its command line (see `loop-dee-loop/README.md`).

`output.file` and `output.dir`+`output.renditions` are mutually exclusive;
use exactly one.

#### Asset time formats

`start` and `duration` accept any of:

| Format | Example |
|---|---|
| `HH:MM:SS` | `"00:10:00"` |
| `HH:MM:SS.mmm` | `"00:10:00.500"` |
| Plain seconds | `600` or `600.5` |
| Human-readable | `"10 min"`, `"12 min 20 sec"`, `"1 hour 30 min"` |

#### Asset rules

- Any asset not covered by a `markers` entry is treated as content.
- The same file can appear multiple times with different `start`/`duration` ranges.
- `start` only → from that offset to end of file.
- `duration` only → from the beginning of the file.
- Neither → use the full file.

#### On-screen display (OSD)

`osd` (top-level, playlist-wide — not per-asset) configures a burned-in
overlay shown on every asset in the playlist, except those with `no_osd:
true`. It has two independent parts: a countdown progress bar, and up to 4
corner text slots. Both are sized as a percentage of the transcoded output
height, so they scale correctly across a multi-rendition ABR ladder.

```yaml
osd:
  enabled: true                 # master on/off switch — default: false
  countdown:
    enabled: true                # default: true
    height_pct: 3                 # default: 3
  text_size_pct: 3               # default: 3 — applies to all 4 corners
  text_color: "#FFFFFF"          # default: "#FFFFFF" — applies to all corner text
  ad_break_label: "ad break"     # default: "ad break" — text for the is_adbreak corner
  corner_box:                    # optional dark box behind each corner's text
    enabled: true                # default: false
    color: "#000000"             # default: "#000000" — border accent color only (see below), same for all 4 corners
  corners:
    top_left: asset_id
    top_right: scte35_spans
    bottom_left: asset_id        # default: asset_id
    bottom_right: time           # default: time
```

**Countdown bar**: a semi-transparent black horizontal bar at the bottom of
the frame, growing from 0% to 100% width over the current asset's playback
(it resets at the start of each asset).

**Corner text**: each of `top_left`/`top_right`/`bottom_left`/`bottom_right`
can independently show one of:

| Value | Shows |
|---|---|
| `asset_id` | the current asset's `id` |
| `time` | elapsed/total seconds within the current asset, sub-second with 2 decimal places, e.g. `12.32/34.60` |
| `next_asset_id` | `next: {id}` — the next real (non-still-image) asset; the playlist loops, so this always resolves to something |
| `scte35_spans` | the non-instant SCTE-35 spans currently covering this asset, shown as stable three-letter codes and `/`-joined outermost-first, e.g. `BRK / PPO / PAD` (`SPI` for a bare `splice_insert`) |
| `is_adbreak` | `osd.ad_break_label` when the asset is covered by an ad-related SCTE-35 span (break/placement-opportunity/advertisement/promo/ad-block lanes), otherwise nothing |
| `osd_label` | the asset's own `osd_label` free-text field, or nothing if unset |
| `null` (or omitted) | nothing shown in that corner |

`no_osd: true` on an asset suppresses the OSD entirely for that asset (no
bar, no corner text), regardless of the playlist-level `osd` settings.
`osd_label` is a free-text per-asset field with no effect unless a corner
is configured to show `osd_label`.

`corner_box` draws a dark background box (fixed color, not configurable)
behind each corner's text, plus a thin vertical border accent in
`corner_box.color` flush with the box's outer edge — left edge for left
corners, right edge for right corners — only where a corner actually has
something to show. Its size is derived automatically from the rendered
text plus a small padding, so there's nothing to size by hand.

The overlay requires ffmpeg to be built with `--enable-libfreetype` (for the
`drawtext` filter). Homebrew's default `ffmpeg` formula on macOS does **not**
include this — install `ffmpeg-full` instead (`brew install ffmpeg-full`,
bottled, no compile needed) and `brew link --force ffmpeg-full` so `ffmpeg`
on `PATH` resolves to it; standard Linux distro builds (e.g. `apt install
ffmpeg`) do include it.

#### Fade in / fade out

Any asset can have `fade_in` and/or `fade_out` fields. When set, the video fades
from black at the start and/or to black at the end for the given duration. Audio
fades from/to silence in sync.

```yaml
- file: content.mp4
  duration: "10 min"
  fade_in: 1.5      # 1.5-second fade in from black
  fade_out: 2       # 2-second fade out to black
```

To cross-dissolve from/to a static image (slate) instead of black, set
`slate_image` — either globally at the top level (applies to all assets) or
per-asset (overrides the global value for that clip):

```yaml
slate_image: /path/to/slate.png   # global default for all assets

assets:
  - file: content.mp4
    duration: "10 min"
    fade_in: 1.5
    fade_out: 2
    # uses the global slate_image above

  - file: ad.mp4
    id: ad1
    duration: "30s"
    fade_out: 1
    slate_image: /path/to/other_slate.png   # overrides the global for this asset

markers:
  - event_id: 1
    type: ad
    splice_type: splice_insert
    assets: [ad1]
```

- `slate_image` only takes effect when at least one of `fade_in` / `fade_out` is also set.
- Without `slate_image`, fades go to/from black (solid colour).
- With `slate_image`, an `xfade` cross-dissolve is used; the image is looped for the full clip duration.
- Values are clamped to the clip duration.
- If `fade_in + fade_out` would exceed the clip duration, both are scaled
  proportionally so they share the available time without overlapping.
- When combined with `osd`, the OSD renders on top of the fading video (it
  fades out along with the picture during a fade-out).
- `fade_in` / `fade_out` accept the same time formats as `start` and `duration`.

#### SCTE-35 marker types

**`splice_insert`** — classic two-point splice: a splice-out at the start of the marker's span and a splice-in at the end.

**`time_signal`** — time signal with a segmentation descriptor. Requires a `segmentation` block. The `segmentation_duration` is derived from the marker's span duration unless overridden in the config. Required if the marker is part of a nested group (break/ppo/ad) that needs coincident boundaries merged into shared SCTE-35 messages.

## CLI reference

```
Usage: franken-ts [OPTIONS] CONFIG

Options:
  -o, --output FILE     Override output file path from config.
  --temp-dir DIRECTORY  Directory for temporary files (default: system temp).
  --normalize           Pre-transcode non-conforming inputs to match output spec.
  --debug               Keep all temporary files; enable verbose logging.
  --dry-run             Print commands without executing them.
  --skip-transcode      Skip ffmpeg step (use existing TS at output path).
  --skip-inject         Stop after ffmpeg transcode, before tsduck injection.
  --verify              Run tsduck extraction after injection and generate an HTML report.
  -v, --verbose         Increase log verbosity (-v INFO, -vv DEBUG).
  -h, --help            Show this message and exit.
```

### Useful combinations

| Goal | Command |
|---|---|
| Iterate on markers without re-encoding | `franken-ts config.yaml --skip-transcode --verify` |
| Inspect intermediate files | `franken-ts config.yaml --debug` |
| Just transcode, no injection | `franken-ts config.yaml --skip-inject` |
| Check what would run | `franken-ts config.yaml --dry-run` |

## How it works

1. **Validate** — ffprobe checks every input for track count, frame rate, and resolution. Track-count mismatches are hard errors; frame-rate/resolution mismatches are warnings (they are normalized away in the next step).
2. **Extract + normalize** — *every* clip (content and ads) is cut into its own keyframe-clean segment that starts on an IDR, conformed to the output fps/resolution and locked to an exact frame count. Video and audio are extracted into **separate** segment files. Results are cached (keyed on source + output spec + exact cut range).
3. **Assemble** — the video-only segments are concatenated and re-encoded in a single CBR H.264 pass with IDR frames forced at every clip boundary; the audio segments are concatenated and muxed back in. The result is strictly CFR with frame-exact splice points.
4. **PTS detection** — ffprobe identifies the actual IDR frame PTS at each ad boundary in the output TS.
5. **XML generation** — a tsduck-compatible SCTE-35 XML file is built from the detected PTS values and the per-marker config.
6. **Inject** — `tsp` injects the SCTE-35 tables into PID 600 of the TS.
7. **Verify** *(optional)* — `tsp` extracts the splice tables back out and confirms all expected event IDs are present. An HTML report is written alongside the output TS.

> **Why extract per clip instead of trimming inside one big concat?**
> The concat demuxer cannot start a segment mid-GOP, so any clip whose in-point
> is not on a source keyframe gets pre-roll frames with overlapping timestamps,
> which causes mass frame drops and missing boundary IDRs. Cutting each clip
> into its own IDR-aligned segment first avoids this. Audio is kept on a
> separate track because AAC's frame granularity never matches the video frame
> grid, and muxing them per-segment punches one-frame holes at the joins
> (breaking strict CFR). See the module docstring in `franken_ts/extract.py`
> for the full findings.

## Input requirements

Source files should have exactly one video track and one audio track (track-count
mismatches are hard errors). Frame rate, resolution, and aspect ratio are
normalized automatically during extraction — no flags required.
