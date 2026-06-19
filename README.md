# 🧟 franken-ts

Stitch video assets together, bolt in ad breaks, and inject SCTE-35 markers — all from a single YAML file.

`franken-ts` automates the full pipeline from a list of source MP4s to a broadcast-ready MPEG-TS file with splice markers at every ad boundary. It uses **ffmpeg** for transcoding and **tsduck** for SCTE-35 injection.

---

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) (with `ffprobe`)
- [tsduck](https://tsduck.io/) (provides the `tsp` command)

All three external tools must be on your `PATH`.

## Installation

```bash
git clone <repo>
cd ts-scte-maker
uv sync
```

The `franken-ts` command is then available via `uv run franken-ts`.

---

## Quick start

```bash
uv run franken-ts example.yaml
```

With verification:

```bash
uv run franken-ts example.yaml --verify
```

Dry run (prints all commands, executes nothing):

```bash
uv run franken-ts example.yaml --dry-run
```

---

## Configuration

All inputs and options are declared in a YAML file.

### Minimal example

```yaml
output:
  file: output_with_markers.ts

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
  file: output_with_markers.ts   # required — final output path
  resolution: "1920x1080"        # default: 1920x1080
  framerate: 25                  # default: 25
  bitrate_kbps: 10000            # default: 10000 (CBR)
  gop: 50                        # default: framerate × 2
  service_provider: "broadpeak"  # TS metadata
  service_name: "broadpeak.io"   # TS metadata

normalize: false                 # set true to auto-fix mismatched inputs

assets:
  - file: content.mp4
    start: "00:00:00"            # optional — start offset within the file
    duration: "10 min"           # optional — how much of the file to use

  - file: ad.mp4
    duration: "2 min"
    ad_break:
      event_id: 1                # unique integer per break
      splice_type: splice_insert # splice_insert | time_signal

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

#### Asset time formats

`start` and `duration` accept any of:

| Format | Example |
|---|---|
| `HH:MM:SS` | `"00:10:00"` |
| `HH:MM:SS.mmm` | `"00:10:00.500"` |
| Plain seconds | `600` or `600.5` |
| Human-readable | `"10 min"`, `"12 min 20 sec"`, `"1 hour 30 min"` |

#### Asset rules

- Any asset without `ad_break` is treated as content (transition jingles included — just add them as regular assets).
- The same file can appear multiple times with different `start`/`duration` ranges.
- `start` only → from that offset to end of file.
- `duration` only → from the beginning of the file.
- Neither → use the full file.

#### SCTE-35 marker types

**`splice_insert`** — classic two-point splice: a splice-out at the start of the ad asset and a splice-in at the end.

**`time_signal`** — time signal with a segmentation descriptor. Requires a `segmentation` block. The `segmentation_duration` is derived from the asset duration unless overridden in the config.

---

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
  --verify              Run tsduck extraction after injection to confirm markers.
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

---

## How it works

1. **Validate** — ffprobe checks every input for track count, frame rate, and resolution. Track count mismatches are hard errors; format mismatches require `--normalize`.
2. **Normalize** *(optional)* — non-conforming inputs are pre-transcoded to match the output spec so durations are accurate.
3. **Transcode** — a single ffmpeg pass concatenates all assets (content and ads) and transcodes to MPEG-TS with CBR H.264. IDR frames are forced at every asset boundary, making splice points exact.
4. **PTS detection** — ffprobe identifies the actual IDR frame PTS at each ad boundary in the output TS.
5. **XML generation** — a tsduck-compatible SCTE-35 XML file is built from the detected PTS values and the per-marker config.
6. **Inject** — `tsp` injects the SCTE-35 tables into PID 600 of the TS.
7. **Verify** *(optional)* — `tsp` extracts the splice tables back out and confirms all expected event IDs are present.

---

## Input requirements

For best results, all source files should have:
- Exactly one video track
- Exactly one audio track
- Matching aspect ratio

If they don't, pass `--normalize` and franken-ts will handle it.
