# ts-scte-maker

A suite of tools for building broadcast-ready MPEG-TS files with SCTE-35 ad markers, inspecting them frame-by-frame, and deploying them as live looping streams on AWS.

## Tools

| Tool | Description |
|---|---|
| [`franken-ts/`](./franken-ts/README.md) | Stitch MP4 assets together and inject SCTE-35 markers from a YAML config |
| [`frame-extractor/`](./frame-extractor/README.md) | Extract every frame from a video and build an interactive HTML timeline viewer |
| [`push-to-aws-media/`](./push-to-aws-media/README.md) | Deploy a live looping HLS/DASH stream to AWS (MediaLive + MediaPackage) |

Outputs from all three tools — `.ts` files, HTML reports, frame timeline directories — land in [`outputs/`](./outputs/).

## Typical workflow

```
1. franken-ts        →  build outputs/my_stream.ts  (with SCTE-35 markers)
2. frame-extractor   →  inspect outputs/my_stream.ts frame-by-frame
3. push-to-aws-media →  upload to S3, start MediaLive, verify playback
```

## Prerequisites

All tools share the same Python environment. External binaries needed:

| Binary | Used by | Install |
|---|---|---|
| `ffmpeg` + `ffprobe` | franken-ts, frame-extractor | [ffmpeg.org](https://ffmpeg.org/) |
| `tsp` (tsduck) | franken-ts | [tsduck.io](https://tsduck.io/) |
| `aws` (AWS CLI v2) | push-to-aws-media | [AWS docs](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| `cdk` (AWS CDK) | push-to-aws-media | `npm install -g aws-cdk` |

## Installation

```bash
git clone <repo>
cd ts-scte-maker
uv sync --all-packages
```

This creates a single `.venv` at the root with all dependencies for all three tools. All commands below are run from the repo root.

## Quick start

### Build a TS file

```bash
uv run franken-ts franken-ts/configs/example.yaml
```

### Inspect a TS file

```bash
uv run frame-extractor outputs/my_stream.ts
```

Frame timeline is written to `outputs/my_stream_timeline/`.

### Deploy to AWS

```bash
cd push-to-aws-media
uv run python channel.py upload
cdk deploy
uv run python channel.py start
```

See each tool's README for full details.
