# Dr Strangeloop

Dr. Strangeloop builds a looping live HLS/DASH channel from source video and ad-break definitions. Its output is a stream that repeats a fixed sequence of content, with SCTE-35 markers (ad breaks, PPOs and similar events) at defined positions.

## Overview

### Why it exists

- To make it much simpler to create live demos without the cost of AWS Media services.
- To allow testing how a manifest manipulation engine handles specific scenarios.
- To make it much easier to troubleshoot issues that depend on a particular stream setup.
- To potentially enable creating environments for automation (regression testing, unit tests etc), by establishing predictive outcome (in particular with ) that can even enable automated checks based on the visual output (frame analysis)

### Sources

There are three ways to supply content:

- Local or remote video assets (MP4) listed in a YAML playlist, with markers defined per playlist. This can be single-rendition or a multi-rendition ladder.
- A captured HAR or Proxyman session, from which a loop is derived.
- A VOD HLS or DASH manifest URL, which is ingested with its full rendition ladder.

### Signalling options

The markers are configurable. You can choose the format of the markers and whether the stream uses a continuous or non-continuous timeline, among other options. This makes it possible to produce streams that follow different signalling conventions, not just one fixed layout.

### Deployment targets

Each channel picks one of three backends:

- `local-docker`: runs the remux in a Docker container on your own machine, with no AWS involved.
- `ecs-express`: ECS Express Mode behind CloudFront, deployed with CDK. This remuxes with GPAC and does no transcoding.
- `aws-media`: MediaLive and MediaPackage v1, deployed with CDK. This does real live transcoding.

### Modes of work

There are three ways to drive it, all operating on the same objects (playlists, channel configs and outputs):

- **CLI**: `franken-ts` builds the content, `its-a-live/channel.py` manages channels (create, spark, start, stop, refresh), and `galvanise.py` chains the whole pipeline. Suited to scripting and to working directly with files.
- **API**: Igor exposes a REST API under `/api/v1/` (playlists, archives, manifests, channels, jobs, files), with interactive docs at `/docs`. Suited to integrating with other tools or automation. It has no authentication, so keep it on localhost.
- **Igor**: the web UI, served at `/admin/` by the same process as the API. It covers defining playlists, importing archives and manifests, launching channels and monitoring them.

## Tools

| Tool | Role | Description |
|---|---|---|
| [`franken-ts/`](./franken-ts/README.md) | Define + build | Stitch MP4 assets together and inject SCTE-35 markers from a YAML playlist; outputs a `.ts` (or a rendition ladder) plus a `.markers.json` |
| [`grave-robber/`](./grave-robber/README.md) | Define + build (alternative source) | Derive a loop from a captured HAR/Proxyman session, or from a VOD HLS/DASH manifest URL (`ingest-url`, full rendition ladder), instead of authoring one. Feeds `loop-dee-loop` rather than `franken-ts` |
| [`loop-dee-loop/`](./loop-dee-loop/README.md) | Packaging engine | Self-hosted remux (GPAC, no transcoding) of a looped source into a continuous live HLS/DASH channel with SCTE-35 signalling: `bake.py` builds an immutable loop package, `serve.py` serves it. Used by the `ecs-express` and `local-docker` backends |
| [`its-a-live/`](./its-a-live/README.md) | Deploy + run | One CLI (`channel.py`) to create, spark, start, stop and refresh a channel on one of three backends selected per channel: `local-docker`, `ecs-express` or `aws-media` (see [Deployment targets](#deployment-targets)) |
| [`inspector-krogh/`](./inspector-krogh/README.md) | Inspect + verify | `frame-extractor` (every frame, GOP/I-P-B timeline, interactive HTML) and `krogh` (independent SCTE-35 examiner: JSON, filmstrip and report, optionally compared against `markers.json`) |
| [`igor/`](./igor/README.md) | UI + API | Web console and REST API (branded "Dr. Strangeloop", codenamed Igor) to define playlists, import archives and manifests, launch channels and monitor them |
| [`galvanise.py`](./galvanise.py) | Orchestration | Runs build, config generation, deploy, spark and start from a single playlist, prompting at each step (AWS backends only) |
| [`scte35-table23/`](./scte35-table23) | Shared library | SCTE-35 Table 23 `segmentation_type_id` reference data, shared by `franken-ts`, `loop-dee-loop` and Igor's frontend (regenerated into copies by `scripts/generate_scte35_tables.py`) |

Outputs from `franken-ts` and `inspector-krogh` (`.ts` files, HTML reports, frame timeline directories) land in [`outputs/`](./outputs/).

## Workflows

There are two families of workflow, depending on the backend. What you can use as a source, and how much control you get over signalling, depends on which one you pick.

| | `local-docker` / `ecs-express` (loop-dee-loop) | `aws-media` |
|---|---|---|
| Playlist, single rendition (`franken-ts`) | yes | yes (a single `.ts` only) |
| Playlist, multi-rendition ladder (`franken-ts`) | yes | no |
| HAR / Proxyman capture (`grave-robber ingest`) | yes | no |
| VOD manifest URL (`grave-robber ingest-url`) | yes | no |
| Marker format and timeline options | yes | no (defined by MediaLive/MediaPackage) |
| Transcoding | no (remux only) | yes |
| Cost | none locally; low on ECS | MediaLive + MediaPackage charges |

### loop-dee-loop workflows (`local-docker`, `ecs-express`)

The content is remuxed with GPAC, never transcoded. It is baked once into an immutable loop package (`spark`), then served as a continuous live channel (`start`). Whichever source you use, the last two steps are identical:

```
source  →  (inspect)  →  its-a-live  spark  →  its-a-live  start
```

The three sources:

1. **Playlist**: `franken-ts` builds the content and SCTE-35 markers from a YAML playlist. `inspector-krogh` can then check the result frame by frame and marker by marker.
   ```
   data/playlists/x.yaml → franken-ts → outputs/x.ts + x.markers.json  (or outputs/x/ for a ladder)
   ```
2. **HAR / Proxyman capture**: `grave-robber ingest` derives the timing and markers from a recorded session. Media is recovered on a best-effort basis, so the result may be a manifest-only loop, which is still useful for testing manifest manipulation. One variant is kept.
   ```
   capture.har → grave-robber ingest → outputs/x/manifest.json
   ```
3. **VOD manifest URL**: `grave-robber ingest-url` downloads a VOD HLS/DASH manifest and its segments, keeping the rendition ladder.
   ```
   https://.../master.m3u8 → grave-robber ingest-url → outputs/x/manifest.json
   ```

Signalling and timeline options are set per channel in the its-a-live TOML config (`[markers]`, `[packaging]`) and fixed at bake time. They include:
- The HLS marker shape (`EXT-X-DATERANGE` layout, optionally `EXT-X-CUE-OUT/-IN` alongside or instead).
- Event ID behaviour across loops, and the DATERANGE ID format.
- HLS as CMAF or TS.
- A continuous timeline (no discontinuity or Period restart at the loop point) or an honestly signalled discontinuity.

At the marker level, `franken-ts` supports `splice_insert` and `time_signal` (with nested break, PPO and ad groups).

Each channel is a TOML file in `data/channels/`, whose `[input].source_path` points at the source output. The backend is chosen with `[deploy].backend`:
- `local-docker` needs no AWS account and has no stack; `channel.py` runs a container named after the channel.
- `ecs-express` runs the same engine on ECS behind CloudFront, deployed with CDK.

### AWS Media workflow (`aws-media`)

This path is for when you need real live transcoding or MediaLive/MediaPackage behaviour. The source must be a single-rendition `.ts` built by `franken-ts` (the default output already carries the timed SCTE-35 cues MediaLive needs). HAR captures, manifest URLs and rendition ladders are not supported, and the `[markers]` / `[packaging]` options above do not apply.

```
data/playlists/x.yaml → franken-ts → outputs/x.ts
   → cdk deploy (once) → its-a-live spark (upload .ts to S3) → start (MediaLive loops it → MediaPackage → HLS/DASH)
```

`galvanise.py` runs this whole sequence, prompting at each step, and can also do it for `ecs-express`.

### Driving a workflow

All workflows work through any of the three modes described in [Modes of work](#modes-of-work). In Igor, playlists, archives (HAR/Proxyman) and manifests (VOD URLs) each have their own page, and a channel references one of them as its source. `galvanise.py` covers only the playlist source on the two AWS backends. Everything else goes through `channel.py`, Igor or the API.

## Prerequisites

All tools share the same Python environment. External binaries needed:

| Binary | Needed for | Install |
|---|---|---|
| `ffmpeg` + `ffprobe` | `franken-ts`, `inspector-krogh`, and `loop-dee-loop` baking | [ffmpeg.org](https://ffmpeg.org/) |
| `tsp` (tsduck) | `franken-ts` (SCTE-35 injection) | [tsduck.io](https://tsduck.io/) |
| `gpac` / `MP4Box` with `scte35dec` support | baking for `local-docker` and `ecs-express` (built from source; the Docker images bundle it) | [gpac.io](https://gpac.io/) |
| `docker` | `local-docker` backend, `ecs-express` image build, and running Igor via Docker | [docker.com](https://www.docker.com/) |
| `aws` (AWS CLI v2) | `aws-media` and `ecs-express` | [AWS docs](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| `cdk` (AWS CDK) | `aws-media` and `ecs-express` | `npm install -g aws-cdk` |
| Node.js / npm | AWS CDK, and Igor's frontend build | [nodejs.org](https://nodejs.org/) |

Local-only use (`local-docker`) needs no AWS CLI or CDK. To run Igor without installing any of this, see [`DOCKER_LOCAL.md`](./DOCKER_LOCAL.md).

## Installation

```bash
git clone <repo>
cd dr-strangeloop
uv sync --all-packages
```

This creates a single `.venv` at the root with the dependencies for `franken-ts`, `inspector-krogh`, `loop-dee-loop`, `grave-robber` and `igor`. `its-a-live` manages its own separate environment (its own `pyproject.toml`/`uv.lock`/`.venv`, since `aws-cdk-lib`/`boto3` don't need to be resolved together with the media-processing tooling): run `cd its-a-live && uv sync` once before using it. All commands below are run from the repo root regardless.

## Quick start

Where things live: playlists in `data/playlists/`, channel configs in `data/channels/`, build outputs in `outputs/`, and imported captures/manifests in `data/archives/` and `data/manifests/`.

### Run locally (no AWS)

Set `[deploy].backend = "local-docker"` in a channel config (copy one from `data/channels/`), then:

```bash
# Playlist source: build the content first
uv run franken-ts data/playlists/short-loop.yaml
uv run frame-extractor outputs/short-loop.ts        # optional: inspect

# Bake and start the channel (create = spark + start for local-docker)
uv run --project its-a-live python its-a-live/channel.py -c data/channels/my-channel.toml create
uv run --project its-a-live python its-a-live/channel.py -c data/channels/my-channel.toml status
uv run --project its-a-live python its-a-live/channel.py -c data/channels/my-channel.toml stop
```

For an HAR or VOD manifest source, run `grave-robber ingest` / `ingest-url` instead of `franken-ts` and point `[input].source_path` at the resulting `manifest.json`:

```bash
uv run --project grave-robber grave-robber ingest data/archives/session.har <manifest-url> --output outputs/my-archive/
uv run --project grave-robber grave-robber ingest-url https://cdn.example/vod/master.m3u8 --output outputs/my-vod/
```

### Full pipeline on AWS

[`galvanise.py`](./galvanise.py) runs the entire pipeline from a single franken-ts YAML playlist, prompting for confirmation at each step. `--backend` selects the backend (default `aws-media`):

```bash
# MediaLive + MediaPackage (default)
uv run python galvanise.py data/playlists/my-stream.yaml

# loop-dee-loop on ECS Express Mode + CloudFront
uv run python galvanise.py data/playlists/my-stream.yaml --backend ecs-express
```

Steps performed:
1. Build the `.ts` file with SCTE-35 markers (`franken-ts`)
2. Generate `data/channels/my-stream.toml` (its-a-live settings, `backend = <chosen backend>`)
3. *(`ecs-express` only)* Ensure the shared ECS cluster stack is deployed
4. Deploy the channel stack (`cdk deploy`)
5. Spark: stage the input for the chosen backend (upload the raw `.ts` for `aws-media`; bake it locally via GPAC and push to S3 for `ecs-express`)
6. Start the channel

At the end it prints the exact commands to update content, stop the channel, and destroy the stack.

### AWS step by step

```bash
# ecs-express only, once per account/region: deploy the shared ECS cluster stack
cd its-a-live
cdk deploy ItsALiveSharedStack-ecs-express -c config=../data/channels/my-stream.toml

# Deploy the channel stack (name includes the backend: ItsALiveStack-<name>-<backend>)
cdk deploy ItsALiveStack-my-stream-aws-media -c config=../data/channels/my-stream.toml
cd ..

# Identical for every backend. its-a-live has its own venv, so use `uv run --project`:
uv run --project its-a-live python its-a-live/channel.py -c data/channels/my-stream.toml spark
uv run --project its-a-live python its-a-live/channel.py -c data/channels/my-stream.toml start
uv run --project its-a-live python its-a-live/channel.py -c data/channels/my-stream.toml stop

# Tear down (stops billing)
cd its-a-live
cdk destroy ItsALiveStack-my-stream-aws-media -c config=../data/channels/my-stream.toml
```

`channel.py create` does the first-time bootstrap (spark, deploy, start) in one command. `aws-media` and `ecs-express` channels can also be given scheduled on-air windows with `channel.py schedule` (see [`its-a-live/README.md`](./its-a-live/README.md#scheduling)).

### Web UI

```bash
./igor/dev.sh          # backend on :8090 plus the Vite dev server
```

See [`igor/README.md`](./igor/README.md) for the production build, and [`DOCKER_LOCAL.md`](./DOCKER_LOCAL.md) to run it from a container.

See each tool's README for full details.
