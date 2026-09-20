# franken-ts

Stitch video assets together, bolt in ad breaks, and inject SCTE-35 markers — all from a single YAML file.

`franken-ts` automates the full pipeline from a list of source MP4s to a broadcast-ready MPEG-TS file with splice markers at every ad boundary. It uses **ffmpeg** for transcoding and **tsduck** for SCTE-35 injection.

## Prerequisites

- `ffmpeg` + `ffprobe` on your `PATH`
- `tsp` (tsduck) on your `PATH`
- Python environment set up from the repo root (`uv sync --all-packages`)

## Quick start

From the repo root:

```bash
uv run franken-ts franken-ts/playlists/example.yaml
```

With verification (extracts markers after injection and generates an HTML report):

```bash
uv run franken-ts franken-ts/playlists/example.yaml --verify
```

Dry run (prints all commands, executes nothing):

```bash
uv run franken-ts franken-ts/playlists/example.yaml --dry-run
```

Output is written to `outputs/`.

## Configuration

All inputs and options are declared in a YAML file. Put playlists in `franken-ts/playlists/`.

### Minimal example

```yaml
output:
  file: ../outputs/output.ts

assets:
  - file: movie.mp4
    duration: "10 min"

  - file: ad.mp4
    duration: "2 min"
    ad_break:
      event_id: 1
      splice_type: splice_insert

  - file: movie.mp4
    start: "10 min"
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

assets:
  - file: content.mp4
    start: "00:00:00"          # optional — start offset within the file
    duration: "10 min"         # optional — how much of the file to use
    countdown: 5               # optional — countdown overlay in the last 5s
    fade_in: 1.5               # optional — fade in from black (or slate) for 1.5s
    fade_out: 2                # optional — fade out to black (or slate) for 2s
    slate_image: /path/to/slate.png  # optional — cross-dissolve image for fades

  - file: ad.mp4
    duration: "2 min"
    countdown: 3               # optional — countdown in the last 3s of this ad
    ad_break:
      event_id: 1              # unique integer per break
      splice_type: splice_insert  # splice_insert | time_signal

  - file: ad2.mp4
    duration: "3 min"
    ad_break:
      event_id: 2
      splice_type: time_signal
      segmentation:
        type_id: "0x34"
        upid_type: "0x09"
        upid_hex: "53 49 47 4e 41 4c 3a 43 52"
        web_delivery_allowed: true
        no_regional_blackout: false
        archive_allowed: false
        device_restrictions: 1
```

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

- Any asset without `ad_break` is treated as content.
- The same file can appear multiple times with different `start`/`duration` ranges.
- `start` only → from that offset to end of file.
- `duration` only → from the beginning of the file.
- Neither → use the full file.

#### Countdown overlay

Any asset can have a `countdown` field. When set, a corner bug is burned into
the top-right of the video in the last N seconds of that clip, showing a
whole-second ceiling countdown and a label for the next element in the sequence:

```
next: AD        ← label line (ASSET | AD | END)
5               ← ticking countdown below
```

| Value | Behaviour |
|---|---|
| `5` (positive number) | Overlay in the last 5 seconds; clamped to clip duration if the clip is shorter |
| `-1` | Overlay for the entire clip duration |
| absent / `null` | No overlay |

`countdown` accepts the same time formats as `start` and `duration` (see table above).

The overlay requires ffmpeg to be built with `--enable-libfreetype` (the default
on macOS via Homebrew and on standard Linux builds).

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
    duration: "30s"
    fade_out: 1
    slate_image: /path/to/other_slate.png   # overrides the global for this asset
    ad_break:
      event_id: 1
      splice_type: splice_insert
```

- `slate_image` only takes effect when at least one of `fade_in` / `fade_out` is also set.
- Without `slate_image`, fades go to/from black (solid colour).
- With `slate_image`, an `xfade` cross-dissolve is used; the image is looped for the full clip duration.
- Values are clamped to the clip duration.
- If `fade_in + fade_out` would exceed the clip duration, both are scaled
  proportionally so they share the available time without overlapping.
- When combined with `countdown`, the countdown text renders on top of the
  fading video (the text fades out along with the picture during a fade-out).
- `fade_in` / `fade_out` accept the same time formats as `start` and `duration`.

#### SCTE-35 marker types

**`splice_insert`** — classic two-point splice: a splice-out at the start of the ad asset and a splice-in at the end.

**`time_signal`** — time signal with a segmentation descriptor. Requires a `segmentation` block. The `segmentation_duration` is derived from the asset duration unless overridden in the config.

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

