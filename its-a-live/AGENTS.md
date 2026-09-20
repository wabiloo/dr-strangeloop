# Agent reference — its-a-live

Phase 3 of the pipeline (see repo-root [`AGENTS.md`](../AGENTS.md)):
deploys a `franken-ts` output as a live-looping HLS/DASH channel in AWS,
on one of two backends chosen per-channel. One CLI (`channel.py`), one
config schema (`config.toml`), identical commands regardless of backend.

For architecture/CDK internals see [`AGENT_BRIEF.md`](./AGENT_BRIEF.md)
(currently `aws-media`-focused; `ecs-express` internals live in
[`loop-dee-loop/AGENTS.md`](../loop-dee-loop/AGENTS.md)) and
[`README.md`](./README.md) for full setup/lifecycle prose. This file is
the condensed reference + the questions to resolve before writing a
config.

## The one decision that shapes everything else: `[deploy].backend`

| `backend` | What it deploys | Chosen when |
|---|---|---|
| `"ecs-express"` | `loop-dee-loop` (self-hosted remux, no transcode) on ECS Express Mode + CloudFront | cheap/simple, content is already in final delivery format, no need for AWS-managed live transcoding |
| `"aws-media"` | MediaLive + MediaPackage v1 (real live transcoding) | need MediaLive/MediaPackage-specific features, or transcoding from a non-final format |

This must be decided **before** writing the franken-ts config, because it
determines whether `output.file` (single-rendition) or `output.dir` +
`output.renditions` (ABR ladder) is required there — see
`franken-ts/AGENTS.md`.

## Config (`config.toml`) — required fields to gather from the user

```toml
[deploy]
name = "..."          # required — drives stack name + all AWS resource names
backend = "..."        # required — "ecs-express" | "aws-media"

[aws]
region = "..."         # required

[s3]
bucket_name = "..."    # required — must already exist, never created/destroyed by the stack
content_folder = "..." # required — content lives under <content_folder>/<name>/

[input]
source_path = "..."    # required — franken-ts output (.ts file, or ladder dir for ecs-express)

# ecs-express only (ignored by aws-media):
[bake]
local_output_dir = "..."   # optional, defaults to ./.local-loop-package/<name>
[channel]
segment_duration = 4.0
dvr_window_seconds = 30
port = 8080
[express]
cpu = 256      # 0.25 vCPU units, Fargate convention
memory = 512   # MB
```

## Questions to ask before deploying (if not already answered)

1. Backend (`ecs-express` vs `aws-media`) — see table above.
2. Channel `name` and target AWS `region`.
3. Existing S3 bucket name + content folder prefix (never create a new
   bucket for this).
4. `input.source_path` — the franken-ts output path (confirm it matches
   what `franken-ts/AGENTS.md`'s output-mode decision produced).
5. For `aws-media`: confirm the source `.ts` has *timed* SCTE-35 cues
   (`franken-ts`'s default output already satisfies this — see
   `AGENT_BRIEF.md`).

## Commands (identical across both backends)

```bash
uv sync                                        # once, its-a-live has its own venv
cdk bootstrap                                  # once per account/region
cdk deploy ItsALiveSharedStack-ecs-express      # once per account/region, ecs-express only

uv run python channel.py -c <config.toml> spark    # stage input (bake or upload) -- do this BEFORE first deploy
cdk deploy ItsALiveStack-<name>-<backend> -c config=<config.toml>
uv run python channel.py -c <config.toml> start    # go live, prints playback URLs

uv run python channel.py -c <config.toml> stop     # stop paying for compute
cdk destroy ItsALiveStack-<name>-<backend> -c config=<config.toml>

# updating content on a running channel:
uv run python channel.py -c <config.toml> spark
uv run python channel.py -c <config.toml> refresh
```

`spark` before the first deploy, not after — `ecs-express`'s container
hard-crashes at startup if there's no package staged yet. `refresh` has
different cost/interruption characteristics per backend (fast re-sync for
`ecs-express`, full stop/start cycle for `aws-media`) — see `README.md`
→ "Updating content on a running channel" before promising a hot reload.
