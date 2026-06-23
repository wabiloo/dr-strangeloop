# frame-extractor

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
