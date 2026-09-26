# inspector-krogh

Extracts every frame from a video, overlays frame number, type (I/P/B), and timestamp, then generates a self-contained interactive HTML timeline viewer.

Useful for visually inspecting MPEG-TS files produced by `franken-ts` — verifying IDR placement, GOP structure, and splice boundaries.

## Prerequisites

- `ffmpeg` + `ffprobe` on your `PATH`
- Python environment set up from the repo root (`uv sync --all-packages`)

## Usage

From the repo root:

```bash
uv run frame-extractor outputs/my_stream.ts
```

Output is written to `outputs/my_stream_timeline/` by default.

To redirect output elsewhere:

```bash
uv run frame-extractor outputs/my_stream.ts --output outputs/my_stream_timeline
```

## Options

```
usage: frame-extractor [-h] [--width WIDTH] [--output OUTPUT]
                       [--workers WORKERS] [--no-overlay] video

positional arguments:
  video              Input video file

options:
  --width WIDTH      Thumbnail width in pixels (default: 320)
  --output OUTPUT    Output directory (default: <video_stem>_timeline/ next to input)
  --workers WORKERS  Parallel workers for overlay step (default: 4)
  --no-overlay       Skip PIL overlay (faster, no text burned into frames)
```

## HTML viewer features

The generated `index.html` is fully self-contained (no server needed). Open it in a browser to:

- Filter frames by type: I / P / B
- Jump to GOP boundaries only
- Search by frame number or timestamp (`HH:MM:SS` or plain seconds)
- Click any frame for a lightbox with prev/next navigation
- Keyboard shortcuts: `j`/`k` to step frames, `[`/`]` to jump between I-frames, `g` to go to a timestamp

## `krogh` — independent SCTE-35 marker verification

A second tool in this package, completely independent of franken-ts: it
scans a built `.ts` for the SCTE-35 markers *actually present in the
file* (via `tsduck`, filtered by table id `0xFC` so it doesn't need to
know which PID they're on), pairs them into start/stop markers per
ANSI/SCTE 35 Table 22/23, infers nesting purely from PTS-span overlap
(so concurrent segmentation types -- e.g. a Break containing a PPO
containing an Ad -- show up with no playlist/`markers.json` involved),
and extracts frames around every splice boundary so you can eyeball
exactly what's before/after each splice.

The scan only ever looks at the `.ts` itself -- it's the tool for "does
this file really carry the markers it claims to." **Optionally**, if the
build's `markers.json` is available (auto-discovered as
`<stem>.markers.json` or `markers.json` next to the `.ts`, or given with
`--expected`; disable with `--no-expected`), each scanned marker is also
checked against what was *intended*: time, segmentation type, declared
duration, UPID, segment numbers and flags, plus markers that are missing
from or unexpected in the stream. This comparison only adds checks and
asset names to the report -- it never influences the scan.

Similarly, franken-ts writes a `<stem>.timeline.json` sidecar (asset
boundaries in output time; also refreshable without rebuilding via
`franken-ts playlist.yaml --report-only`). When krogh finds it (or is given
`--timeline`), the detailed report also shows every **asset join that has
no SCTE-35 marker** -- frames around it, an IDR check, and an assets row in
the timeline. Builds made before this sidecar existed just don't show
joins until reassembled or re-run with `--report-only`.

### Prerequisites

- `ffmpeg` + `ffprobe` and `tsduck` (`tsp`) on your `PATH`

### Usage

```bash
uv run krogh outputs/my_stream.ts
uv run krogh outputs/my_stream.ts --output outputs/my_stream_scte
uv run krogh outputs/my_stream.ts --skip-frames        # metadata-only scan
uv run krogh outputs/my_stream.ts --expected outputs/my_stream.markers.json
uv run krogh outputs/my_stream.ts --no-expected        # pure independent scan
uv run krogh --render-only outputs/my_stream_scte/scte-report.json
```

Writes `scte-report.json` (the machine-readable source of truth --
markers, inferred nesting, per-boundary frame references, and pass/fail
checks like "splice lands on an IDR frame" / "declared duration matches
actual" / "matches markers.json"), plus two self-contained HTML views
rendered from that JSON into the output directory: `scte-filmstrip.html`
(every extracted frame in time order with marker spans above) and
`scte-report.html` (detailed per-marker cards, grouped by start time). The JSON → HTML rendering step
(`scte35_report_html.render_html`) is a pure function with no
ffmpeg/tsduck dependency, so it can be re-run standalone (`--render-only`)
or imported directly by another tool (igor's Assemble tab uses this to
show the same report without an iframe, or by iframing the generated
HTML directly).

### Options

```
usage: krogh [-h] [--output OUTPUT] [--pid PID] [--before BEFORE]
                      [--after AFTER] [--width WIDTH] [--skip-frames]
                      [--skip-html] [--render-only JSON_PATH]
                      [--idr-tolerance-frames N] [--duration-tolerance-frames N]
                      [--expected MARKERS_JSON] [--timeline TIMELINE_JSON]
                      [--no-expected]
                      [ts]
```
