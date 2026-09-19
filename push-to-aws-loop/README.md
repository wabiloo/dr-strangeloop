# push-to-aws-loop

Provisions a **loop-dee-loop** channel on **AWS App Runner**, fronted by
**CloudFront** — the AWS-native counterpart to `push-to-aws-media`
(MediaLive/MediaPackage), but for the self-hosted `loop-dee-loop` packager.

## Architecture

```
../loop-dee-loop (Dockerfile)
      |
      v  (built + pushed automatically by `cdk deploy`, one shared image)
   ECR asset repo
      |
      v
 App Runner service (long-running, auto-scaling `serve.py`)
      |  LOOP_PACKAGE_S3_URI synced down at container start by
      |  docker-entrypoint.sh (see ../loop-dee-loop)
      v
 CloudFront distribution (public entrypoint; caches /seg/* and
 /audio/seg/*, never caches manifests)


 bake.py runs LOCALLY (your machine / CI), no AWS compute involved:

  local franken-ts output --(bake.py)--> local loop package --(aws s3 sync)--> S3
                                                                                  ^
                                                                                  |
                                                            LOOP_PACKAGE_S3_URI --+
```

No ALB, no VPC, no ECS cluster — App Runner's own price bundles the
ingress/load-balancing layer, and it has native pause/resume for stopping
a channel between airings (see cost discussion below). `bake` is
deliberately **not** run in AWS at all: it's a one-shot process meant to
run once per schedule change, so for this stack it just runs locally (or
via `docker run` using the same image) and the output is pushed to S3 —
no ECS task definition, IAM role, security group, or subnets needed just
for that.

⚠️ **Security note:** this stack does not restrict who can reach the App
Runner service directly — its default `*.awsapprunner.com` domain is
publicly reachable, same as the CloudFront URL. Fine for demos/internal
use; if you need to guarantee traffic only goes through CloudFront, add a
shared-secret custom header check in `serve.py` (CloudFront can inject one
via an origin request policy) or switch to App Runner VPC ingress + a
CloudFront VPC origin.

## Prerequisites

- AWS CLI v2, Docker (for the CDK-managed image build), Node.js (`npm
  install -g aws-cdk`), [uv](https://docs.astral.sh/uv/)
- An existing S3 bucket (referenced only, never created/destroyed by this
  stack).
- For `channel.py bake`: either a local Python environment with
  loop-dee-loop's deps installed (`.venv` next to `../loop-dee-loop`, or
  `uv`), **and** a GPAC/MP4Box build with `scte35dec` support on `PATH`
  (see `../loop-dee-loop/README.md` prerequisites) — or just run
  `../loop-dee-loop`'s `bake.py` via `docker run` using the image this
  stack builds, if you don't want to build GPAC locally.

No VPC lookup is required (unlike the earlier Fargate+ALB design) — App
Runner doesn't need to run inside your VPC for this setup.

## Setup

```bash
uv sync
cdk bootstrap   # once per account/region
```

## Configure

Each channel gets its own config file (see `config.toml` for the full
schema):

```toml
# configs/my-channel.toml
[deploy]
name = "my-channel"          # stack becomes LoopChannelStack-my-channel

[aws]
region = "eu-west-1"

[s3]
bucket_name = "my-existing-bucket"
loop_package_folder = "loop-dee-loop/packages"

[input]
source_path = "../outputs/mychannel"   # .ts file or rendition directory

[channel]
segment_duration   = 4.0
dvr_window_seconds = 30
port               = 8080

[apprunner]
cpu    = 256   # 0.25 vCPU
memory = 512   # 0.5 GB
```

The `name` field drives every AWS resource name (`loop-dee-loop-<name>-*`)
and the S3 key prefix (`<folder>/<name>/...`), so config files are fully
independent, parallel deployments.

## Full lifecycle (single channel, default config.toml)

⚠️ **Bake before you deploy, not after.** `serve.py` hard-crashes at
startup (not just a 404) if `LOOP_PACKAGE_S3_URI` is empty when the
container starts, and App Runner requires the container to start
successfully to consider the service created — so deploying the stack
before anything exists at that S3 prefix reliably fails with
`CREATE_FAILED (NotStabilized)` after ~3 minutes. Push a package to S3
*first*, then deploy:

```bash
# 1. Bake locally and push the result to S3. No AWS compute involved --
#    this just runs bake.py on your machine (or via `docker run` with the
#    image this stack builds) and `aws s3 sync`s the output up. Works even
#    before the stack exists, since it only needs S3 access.
uv run python channel.py bake

# 2. Provision the stack (builds+pushes the image, creates the App Runner
#    service + CloudFront distribution -- serve.py finds a real package at
#    LOOP_PACKAGE_S3_URI immediately, so the service starts cleanly).
cdk deploy

# 3. (Re)start the channel: resumes the App Runner service if paused,
#    updates it with --epoch-utc=now (so the loop's epoch always starts
#    "now" relative to when you actually go live), waits for it to be
#    RUNNING, prints the CloudFront playback URLs.
uv run python channel.py start

# ... channel is live ...

uv run python channel.py stop     # pauses the service -- no compute charges while paused
cdk destroy LoopChannelStack-<name>
```

Both `cdk deploy` (image build + App Runner service + CloudFront
distribution) and `channel.py start` (redeploying the App Runner service
with a new start command) reliably take a few minutes each — that's
inherent AWS latency (App Runner: ~3 min to pull/start/health-check a
container and land in `RUNNING`; CloudFront: ~3 min to propagate a new
distribution to edge locations), not something wrong with the setup.

## Updating content on a running channel (no image rebuild, no redeploy)

```bash
uv run python channel.py bake     # re-bake locally, push new package to S3
uv run python channel.py start    # restart with a fresh epoch so the new
                                   # package's loop/markers line up
```

`docker-entrypoint.sh` re-syncs `LOOP_PACKAGE_S3_URI` from scratch every
time the App Runner container starts, so a restart is all that's needed to
pick up a new bake — the image itself never changes.

## Multiple channels

Same pattern as `push-to-aws-media`: one config file per channel, deployed
and managed independently.

```bash
cdk deploy -c config=./configs/channel_a.toml
uv run python channel.py --config ./configs/channel_a.toml bake
uv run python channel.py --config ./configs/channel_a.toml start
...
cdk destroy LoopChannelStack-channel_a
```

Note: each channel still gets its own App Runner service + CloudFront
distribution (full isolation, same tradeoff discussed for the earlier
ALB-based design) — consolidating N channels behind one shared CloudFront
distribution with per-channel behaviors/origins is a further optimization
worth doing once you're running many channels, not implemented here.

## channel.py reference

| Command | Description |
|---|---|
| `bake` | Run `bake.py` locally against `input.source_path`, push the result to S3 |
| `start` | Resume (if paused) + update the App Runner service with `--epoch-utc=now`, wait for RUNNING, print playback URLs |
| `stop` | Pause the App Runner service (native pause/resume — actually stops compute billing) |
| `status` | Print the App Runner service's current status |
| `outputs` | Print all CloudFormation stack outputs |
| `redeploy` | Delete a broken stack if needed, then `cdk deploy` |

## Notes / gotchas

- **Cost**: see chat history for the full breakdown, but roughly:
  App Runner at 0.25 vCPU/0.5GB costs ~$0.08/day paused (memory-only) and
  ~$0.47/day if actively serving requests continuously all day — no
  separate ALB or public-IPv4 line items, unlike the earlier Fargate+ALB
  design.
- **`channel.py stop` genuinely stops compute billing** (App Runner's
  native pause), unlike the earlier ECS+ALB design where scaling the
  service to 0 tasks still left the ALB (and its public IPs) running and
  billing.
- **Manifests are never cached** by CloudFront (`TTL=0`); segments under
  `*/seg/*` get a 5-minute default TTL — matches SCOPE.md §8's "CDN in
  front of `/seg/*`" recommendation.
- **Instance role** is scoped to read-only on
  `loop-dee-loop/packages/<name>/*`. There's no separate bake IAM role
  anymore — `channel.py bake` uses your own local AWS credentials (via the
  AWS CLI) to push to S3, same as any other `aws s3 sync`.
- The pushed loop package is **not** deleted by `cdk destroy` (the stack
  doesn't own the bucket) — clean up manually with
  `aws s3 rm --recursive s3://<bucket>/<folder>/<name>/` if needed.

## Troubleshooting

- **`ServeService ... CREATE_FAILED (NotStabilized)`, logs show
  `exec /usr/local/bin/docker-entrypoint.sh: exec format error`**: the
  image was built for the wrong CPU architecture. `loop_channel_stack.py`
  pins `platform=ecr_assets.Platform.LINUX_AMD64` on the `DockerImageAsset`
  specifically to prevent this — on an Apple Silicon (arm64) machine,
  Docker builds arm64 by default unless told otherwise, and App Runner
  here expects x86_64. If you still hit this, confirm that pin is in place
  and that Docker actually rebuilt (not reused a stale local arm64 image
  under the same tag).
- **`ServeService ... CREATE_FAILED (NotStabilized)`, logs show
  `FileNotFoundError: ... loop_descriptor.json`**: nothing has been baked
  to `LOOP_PACKAGE_S3_URI` yet — see "bake before you deploy" above. Run
  `channel.py bake`, then `channel.py redeploy` (it deletes the failed
  `ROLLBACK_COMPLETE` stack and retries `cdk deploy`).
- **`bake.py` fails with `'gpac' not found on PATH`** when baking via
  `docker run` with this stack's image: this was a real bug in
  `../loop-dee-loop/Dockerfile`'s build stage (fixed) — it assumed GPAC
  installs to `/usr/bin/gpac`, but a from-source `./configure` (no
  `--prefix`) actually installs to `/usr/local/bin`, and a trailing
  `|| true` was silently swallowing the resulting `cp` failure, so the
  runtime image shipped with **no gpac binary at all**. If you see this on
  a fresh `cdk deploy`/local build, make sure you have the current
  Dockerfile (it resolves the real path via `command -v` and no longer
  masks that `RUN` command's failures).
- **Baking via `docker run` and passing `-v host/input:/data/input:ro`**:
  don't mount the input read-only — `bake.py`'s SCTE-35 decoder
  (`threefive`) opens the `.ts` file in `r+b` mode even though it only
  reads it, so a read-only mount fails with
  `OSError: [Errno 30] Read-only file system`. Mount it read-write instead.
- **Stack stuck in `ROLLBACK_COMPLETE`**: this is normal after any
  `CREATE_FAILED` — CloudFormation can't retry a failed *create* in place.
  `channel.py redeploy` handles this automatically (deletes the stack,
  then runs `cdk deploy`); a plain `cdk deploy` will refuse with
  `... is in ROLLBACK_COMPLETE state and can not be updated`.
