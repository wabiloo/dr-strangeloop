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

### Full pipeline (recommended)

[`deploy.py`](./deploy.py) runs the entire pipeline from a single franken-ts YAML config,
prompting for confirmation at each step:

```bash
uv run python deploy.py franken-ts/configs/my-stream.yaml
```

Steps performed:
1. Build the `.ts` file with SCTE-35 markers (`franken-ts`)
2. Generate `configs/my-stream.toml` (push-to-aws settings)
3. Deploy the AWS stack (`cdk deploy`)
4. Upload the `.ts` to S3
5. Start the MediaLive channel

At the end it prints the exact commands to stop the channel and destroy the stack.

### Step by step

#### Build a TS file

```bash
uv run franken-ts franken-ts/configs/example.yaml
```

#### Inspect a TS file

```bash
uv run frame-extractor outputs/my_stream.ts
```

Frame timeline is written to `outputs/my_stream_timeline/`.

#### Deploy to AWS manually

TOML configs for push-to-aws live in `configs/`. Generate one or copy an existing example, then:

```bash
# Deploy the stack
cd push-to-aws-media
cdk deploy --require-approval never -c config=../configs/my-stream.toml

# All subsequent commands can be run from the repo root
uv run python push-to-aws-media/channel.py -c configs/my-stream.toml upload
uv run python push-to-aws-media/channel.py -c configs/my-stream.toml start

# When done
uv run python push-to-aws-media/channel.py -c configs/my-stream.toml stop

# Tear down (stops billing)
cd push-to-aws-media
cdk destroy ScteLoopStack-my-stream -c config=../configs/my-stream.toml
```

See each tool's README for full details.
