# its-a-live

Provisions a live-looping HLS/DASH channel from `franken-ts` output, on
one of three backends selected per-channel via config — one command-line
tool, one config schema, identical commands regardless of which backend
you pick:

- **`ecs-express`** — `loop-dee-loop` (self-hosted remux/serve, no
  transcoding) on **ECS Express Mode** + CloudFront. Cheap, simple, no
  managed media services involved.
- **`aws-media`** — **MediaLive + MediaPackage v1** (fully managed AWS
  media services, real live transcoding).
- **`local-docker`** — `loop-dee-loop` in a `docker run` container on
  your own machine. No AWS resources at all, no CloudFormation stack —
  good for local dev/demo/testing before spending anything on AWS.

> This tool was formerly two separate projects (`push-to-aws-media` and
> `push-to-aws-loop`); they're merged here under one CLI so you don't have
> to learn two different command sets for two different ways of putting
> the same franken-ts content on the air.

## Architecture

```
franken-ts output (.ts + .markers.json)
        |
        v
   channel.py spark    (stage the input for whichever backend you picked)
        |
        +-- ecs-express: bake.py (GPAC, local, no AWS compute)
        |                 -> loop package -> S3
        |
        +-- aws-media:   upload .ts as-is -> S3
        |
        v
   channel.py start
        |
        +-- ecs-express: ECS::ExpressGatewayService (Fargate, shared ALB)
        |                 -> CloudFront -> HLS/DASH
        |
        +-- aws-media:   MediaLive (TS_FILE input, LOOP) -> MediaPackage v1
        |                 -> HLS/DASH (public, no CDN)
```

## Prerequisites

- AWS CLI v2, Node.js (`npm install -g aws-cdk`), [uv](https://docs.astral.sh/uv/)
- An existing S3 bucket (referenced only, never created/destroyed by
  either backend's stack).
- **`ecs-express` only**: Docker (for the CDK-managed `loop-dee-loop`
  image build); either a local Python env with `loop-dee-loop`'s deps +
  a GPAC/MP4Box build with `scte35dec` support on `PATH`, or just use
  `docker run` with the image this stack builds (see `channel.py spark`
  in `_ecs_express_ops.py`). Two standard AWS managed policies must exist
  in the account: `AmazonECSTaskExecutionRolePolicy` and
  `service-role/AmazonECSInfrastructureRoleforExpressGatewayServices`.
- **`aws-media` only**: the source `.ts` must contain *timed* SCTE-35
  cues (`splice_immediate_flag=0`, real `splice_time` PTS, ~2s pre-roll)
  — see `AGENT_BRIEF.md`. `franken-ts`'s default output already satisfies
  this.

## Setup

```bash
uv sync
cdk bootstrap                        # once per account/region
cdk deploy ItsALiveSharedStack-ecs-express   # once per account/region --
                                       # only needed if you'll use the
                                       # ecs-express backend (creates the
                                       # shared ECS cluster every
                                       # ecs-express channel lives in)
```

## Configure

Each channel gets its own config file (see `config.toml` for the full
schema). The key field is `[deploy].backend`:

```toml
# configs/my-channel.toml
[deploy]
name = "my-channel"
backend = "ecs-express"        # or "aws-media"

[aws]
region = "eu-west-1"

[s3]
bucket_name = "my-existing-bucket"
content_folder = "its-a-live/content"

[input]
source_path = "../outputs/mychannel"   # franken-ts output

# ecs-express-only:
[packaging]
segment_duration   = 4.0
dvr_window_seconds = 30

[express]
port   = 8080
cpu    = 256
memory = 512
```

See `AGENTS.md`'s config reference for the full schema, including the
`[markers]` section controlling the shape of the HLS/DASH SCTE-35
signaling `loop-dee-loop` renders, and `local-docker`'s `[docker]`
section (in place of `[express]`, holding just `port`).

`[deploy].name` + `[deploy].backend` together drive the CloudFormation
stack name (`ItsALiveStack-<name>-<backend>`) and every AWS resource name,
so config files are fully independent, parallel deployments — you could
even run the same channel `name` on both backends side by side.

## Full lifecycle

⚠️ **`spark` before you deploy, not after** (`ecs-express` only — the
container hard-crashes at startup if there's no package at
`LOOP_PACKAGE_S3_URI` yet, and Express Mode requires a successful first
start to consider the service created). `aws-media` doesn't have this
restriction (MediaLive starts fine with no input yet), but `spark`-first
is still the recommended order for both.

```bash
uv run python channel.py spark      # stage the franken-ts input (bake or upload)
cdk deploy ItsALiveStack-<name>-<backend>
uv run python channel.py start      # go live, prints playback URLs

# ... channel is live ...

uv run python channel.py stop       # stop paying for compute
cdk destroy ItsALiveStack-<name>-<backend>
```

## Updating content on a running channel

```bash
uv run python channel.py spark      # re-bake/re-upload new content to S3
uv run python channel.py refresh    # pick it up on the running channel
```

- `ecs-express`: `refresh` forces a new ECS task launch, which re-syncs
  `LOOP_PACKAGE_S3_URI` at boot. Fast-ish, but ECS Express Mode's canary
  deployment strategy means the *previous* task can still serve most/all
  traffic for a fixed ~3 minute bake period — no fast/reliable API signal
  for "100% of viewers now see the new content."
- `aws-media`: MediaLive's `TS_FILE` input has no hot-reload, so `refresh`
  is a full stop→start cycle (a real, brief interruption).

## `channel.py` reference

Every command works the same way regardless of backend — `[deploy].backend`
in your config file picks the implementation.

| Command | `ecs-express` | `aws-media` | `local-docker` |
|---|---|---|---|
| `spark` | Bake locally (GPAC via `bake.py`), push loop package to S3 | Upload the raw `.ts` to S3 as-is | Bake locally (GPAC via `bake.py`), no upload — package stays on disk |
| `start` | Scale ECS to 1 task. Epoch left untouched by default (fast, no redeploy) — pass `--epoch-utc now\|<ISO8601>` to explicitly (re)set it (forces a real redeploy, see Notes) | Start the MediaLive channel, wait for `RUNNING` | `docker run` a container bind-mounting the baked package (building the image on first use); epoch defaults to the Unix epoch, `--epoch-utc` works the same as `ecs-express` |
| `stop` | Scale ECS to 0 tasks (shared ALB keeps running for other channels) | Stop the MediaLive channel, wait for `IDLE` | `docker rm -f` the container |
| `refresh` | Force a new ECS task launch to re-sync S3 content | Full stop→start cycle (no hot-reload exists) | Recreate the container (seconds, no canary) |
| `status` | ECS service scaling/status | MediaLive channel state | Docker container status |
| `outputs` | CloudFormation stack outputs | CloudFormation stack outputs | N/A — no stack; use `status` |
| `redeploy` | Delete a broken stack if needed (and ensure the shared cluster stack is deployed), then `cdk deploy` | Delete a broken stack if needed, then `cdk deploy` | N/A — no stack; equivalent to `refresh` |

`local-docker` has no CloudFormation/CDK involvement whatsoever: no
`cdk deploy`/`cdk destroy` step exists for it, and playback URLs are
always `http://localhost:<channel.port>/...`.

## Notes / gotchas

- **Epoch (ecs-express only) defaults to the Unix epoch**
  (`1970-01-01T00:00:00Z`, set in `loop_stack.py`) and is left untouched
  by a plain `channel.py start` — this is looping content simulating
  live, not a real broadcast, so there's no need to force loop position 0
  on every start; landing mid-ad-break on start/restart is an accepted
  tradeoff, and it keeps `start`/`stop` fast (pure scaling, no new task
  revision). Pass `channel.py start --epoch-utc now` or `--epoch-utc
  <ISO8601 UTC timestamp>` to explicitly (re)set it when you want a clean
  restart from position 0.
- **Cost (`ecs-express`)**: Fargate compute (0.25 vCPU/0.5GB by default) +
  a *shared* ALB + CloudWatch logs/metrics + data transfer — no separate
  "Express Mode" charge. `stop` genuinely stops the Fargate compute line;
  the shared ALB keeps running regardless, amortized across every channel
  using it.
- **Cost (`aws-media`)**: MediaLive bills continuously while `RUNNING`
  (~$0.50-0.65/hr for a single-pipeline HD channel) *regardless of
  audience size* — `stop` is the main cost lever here, much more so than
  on `ecs-express`.
- **Manifests are never cached** by CloudFront on `ecs-express` (`TTL=0`);
  segments under `*/seg/*` get a 5-minute default TTL. `aws-media`'s
  MediaPackage v1 endpoints have no CDN in front at all (public, direct).
- **Non-uniform segment durations are fully supported** on `ecs-express`
  — `loop-dee-loop`'s segment/media-sequence math is boundary-list-based,
  not fixed-duration, by design (see `loop-dee-loop/README.md`
  "Segmentation").
- The uploaded/pushed content is **not** deleted by `cdk destroy` (the
  stack doesn't own the bucket) — clean up manually with
  `aws s3 rm --recursive s3://<bucket>/<content_folder>/<name>/` if
  needed.

## Troubleshooting

- **`ItsALiveStack-*-ecs-express` `ServeService CREATE_FAILED`, logs show
  `exec /usr/local/bin/docker-entrypoint.sh: exec format error`**: the
  image was built for the wrong CPU architecture. `loop_stack.py` pins
  `platform=ecr_assets.Platform.LINUX_AMD64` specifically to prevent this
  — on an Apple Silicon (arm64) machine, Docker builds arm64 by default
  unless told otherwise.
- **`ServeService` seems to hang in `CREATE_IN_PROGRESS` for far longer
  than expected (observed: 90+ minutes, zero tasks ever placed, no target
  groups created)**, and `aws ecs describe-service-revisions` shows a
  `statusReason` like `ValidationError: Health check path '...' must
  begin with a '/' character ...`: `health_check_path` must be a **bare
  path** (e.g. `/manifest.mpd`), not a `PROTOCOL:PORT/PATH`-style string
  — despite AWS's docs quoting the *default* as `"HTTP:80/ping"`, which
  reads like that combined format. This doesn't fail cleanly; it retries
  invalid ALB/listener/target-group provisioning indefinitely with no
  CloudFormation-visible failure. Check the real underlying resources
  directly if a create looks stuck:
  ```bash
  aws ecs describe-service-revisions --service-revision-arns <arn>
  # look at .serviceRevisions[0].ecsManagedResources.ingressPaths[0].*.statusReason
  ```
- **`AWS::IAM::Role ... Policy ... does not exist or is not attachable`**
  for `AmazonECSInfrastructureRoleforExpressGatewayServices`: the managed
  policy lives under the `service-role/` path — use
  `"service-role/AmazonECSInfrastructureRoleforExpressGatewayServices"`,
  not the bare name.
- **`Update of resource type is not permitted`** when redeploying after
  changing a stack's resource types under the same construct ID:
  CloudFormation refuses in-place resource-type changes for the same
  logical ID. `cdk destroy` then `cdk deploy` (or `channel.py redeploy`,
  which does the delete-then-deploy dance for a stack stuck in
  `ROLLBACK_COMPLETE`) is required.
- **`ServeService CREATE_FAILED`, logs show `FileNotFoundError: ...
  loop_descriptor.json`**: nothing has been `spark`ed to
  `LOOP_PACKAGE_S3_URI` yet. Run `channel.py spark`, then `channel.py
  redeploy`.
- **`bake.py` fails with `'gpac' not found on PATH`** when sparking via
  `docker run`: this was a real bug in `../loop-dee-loop/Dockerfile`'s
  build stage (fixed) — GPAC's from-source `./configure` (no `--prefix`)
  installs to `/usr/local/bin`, not `/usr/bin`; make sure you have the
  current Dockerfile.
- **Sparking via `docker run` with `-v host/input:/data/input:ro`**:
  don't mount the input read-only — `bake.py`'s SCTE-35 decoder
  (`threefive`) opens the `.ts` file in `r+b` mode even though it only
  reads it, so a read-only mount fails with
  `OSError: [Errno 30] Read-only file system`. Mount it read-write.
- **Stack stuck in `ROLLBACK_COMPLETE`**: normal after any `CREATE_FAILED`
  — CloudFormation can't retry a failed *create* in place. `channel.py
  redeploy` handles this automatically; a plain `cdk deploy` refuses with
  `... is in ROLLBACK_COMPLETE state and can not be updated`.
- **`aws-media` SCTE-35 markers don't appear**: whether markers show up
  depends on how cues are authored in the source `.ts`, not on the AWS
  config — see `AGENT_BRIEF.md`'s "timed cues" requirement.
