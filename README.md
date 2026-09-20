# ts-scte-maker

A suite of tools for building broadcast-ready MPEG-TS files with SCTE-35 ad markers, inspecting them frame-by-frame, and deploying them as live looping streams on AWS.

## Tools

| Tool | Description |
|---|---|
| [`franken-ts/`](./franken-ts/README.md) | Stitch MP4 assets together and inject SCTE-35 markers from a YAML config |
| [`frame-extractor/`](./frame-extractor/README.md) | Extract every frame from a video and build an interactive HTML timeline viewer |
| [`its-a-live/`](./its-a-live/README.md) | Deploy a live looping HLS/DASH stream to AWS, on either of two backends selected per-channel: **MediaLive + MediaPackage** (`aws-media`) or **loop-dee-loop on ECS Express Mode + CloudFront** (`ecs-express`) |

Outputs from all three tools — `.ts` files, HTML reports, frame timeline directories — land in [`outputs/`](./outputs/).

## Typical workflow

```
1. franken-ts        →  build outputs/my_stream.ts  (with SCTE-35 markers)
2. frame-extractor   →  inspect outputs/my_stream.ts frame-by-frame
3. its-a-live        →  spark (upload/bake), deploy, start, verify playback
```

## Prerequisites

All tools share the same Python environment. External binaries needed:

| Binary | Used by | Install |
|---|---|---|
| `ffmpeg` + `ffprobe` | franken-ts, frame-extractor | [ffmpeg.org](https://ffmpeg.org/) |
| `tsp` (tsduck) | franken-ts | [tsduck.io](https://tsduck.io/) |
| `aws` (AWS CLI v2) | its-a-live | [AWS docs](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| `cdk` (AWS CDK) | its-a-live | `npm install -g aws-cdk` |
| `docker` | its-a-live (`ecs-express` backend only) | [docker.com](https://www.docker.com/) |

## Installation

```bash
git clone <repo>
cd ts-scte-maker
uv sync --all-packages
```

This creates a single `.venv` at the root with all dependencies for
`franken-ts`, `frame-extractor`, and `loop-dee-loop`. `its-a-live` manages
its own separate environment (its own `pyproject.toml`/`uv.lock`/`.venv`,
since `aws-cdk-lib`/`boto3` don't need to be resolved together with the
media-processing tooling) — run `cd its-a-live && uv sync` once before
using it. All commands below are run from the repo root regardless.

## Quick start

### Full pipeline (recommended)

[`deploy.py`](./deploy.py) runs the entire pipeline from a single franken-ts YAML config,
prompting for confirmation at each step. `--backend` selects which its-a-live
backend to deploy to (default `aws-media`):

```bash
# MediaLive + MediaPackage (default)
uv run python deploy.py franken-ts/configs/my-stream.yaml

# loop-dee-loop on ECS Express Mode + CloudFront
uv run python deploy.py franken-ts/configs/my-stream.yaml --backend ecs-express
```

Steps performed:
1. Build the `.ts` file with SCTE-35 markers (`franken-ts`)
2. Generate `configs/my-stream.toml` (its-a-live settings, `backend = <chosen backend>`)
3. *(`ecs-express` only)* Ensure the shared ECS cluster stack is deployed
4. Deploy the channel stack (`cdk deploy`)
5. Spark: stage the input for the chosen backend (upload the raw `.ts`
   for `aws-media`; bake it locally via GPAC + push to S3 for `ecs-express`)
6. Start the channel

At the end it prints the exact commands to update content, stop the channel, and destroy the stack.

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

TOML configs for its-a-live live in `configs/`. Generate one or copy an existing example
(remember to set `[deploy].backend` to `"aws-media"` or `"ecs-express"`), then:

```bash
# ecs-express only, once per account/region: deploy the shared ECS cluster
# stack before any channel (not needed for aws-media)
cd its-a-live
cdk deploy ItsALiveSharedStack-ecs-express -c config=../configs/my-stream.toml
cd ..

# Deploy the channel stack (stack name includes the backend: ItsALiveStack-<name>-<backend>)
cd its-a-live
cdk deploy ItsALiveStack-my-stream-aws-media -c config=../configs/my-stream.toml
cd ..

# All subsequent commands can be run from the repo root -- identical
# regardless of which backend the config targets. its-a-live manages its
# own separate venv (see Installation above), so use `uv run --project`:
uv run --project its-a-live python its-a-live/channel.py -c configs/my-stream.toml spark
uv run --project its-a-live python its-a-live/channel.py -c configs/my-stream.toml start

# When done
uv run --project its-a-live python its-a-live/channel.py -c configs/my-stream.toml stop

# Tear down (stops billing)
cd its-a-live
cdk destroy ItsALiveStack-my-stream-aws-media -c config=../configs/my-stream.toml
```

See each tool's README for full details.
