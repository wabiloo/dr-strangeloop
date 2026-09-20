# push-to-aws-loop

Provisions a **loop-dee-loop** channel on **Amazon ECS Express Mode**,
fronted by **CloudFront** — the AWS-native counterpart to
`push-to-aws-media` (MediaLive/MediaPackage), but for the self-hosted
`loop-dee-loop` packager.

> This stack originally targeted **AWS App Runner**. App Runner stopped
> onboarding new customers on April 30, 2026, and AWS's own migration
> guidance recommends **Amazon ECS Express Mode** as the replacement — so
> that's what this stack now uses. Express Mode is *not* a new compute
> primitive: it's a simplified deployment mode built on standard
> Fargate + ALB that auto-provisions/manages the load balancer, target
> groups, security groups, SSL, and auto-scaling for you, and — per AWS's
> own docs — *shares* Application Load Balancers across multiple Express
> Mode services in the same account/networking config, which directly
> avoids the "N× ALB cost" problem a hand-built Fargate+ALB stack would
> otherwise have per channel.

## Architecture

```
../loop-dee-loop (Dockerfile)
      |
      v  (built + pushed automatically by `cdk deploy`, one shared image)
   ECR asset repo
      |
      v
 AWS::ECS::ExpressGatewayService (long-running `serve.py`,
 auto-scaling 1-20 tasks on avg CPU by default)
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

No VPC lookup, no hand-built ALB/target-group/security-group wiring — the
`AWS::ECS::ExpressGatewayService` resource (CDK: `ecs.CfnExpressGatewayService`)
handles all of that, defaulting to the account's default VPC. `bake` is
deliberately **not** run in AWS at all: it's a one-shot process meant to
run once per schedule change, so for this stack it just runs locally (or
via `docker run` using the same image) and the output is pushed to S3 —
no ECS task definition, IAM role, security group, or subnets needed just
for that.

⚠️ **Security note:** this stack does not restrict who can reach the
Express service's ingress endpoint directly — its `*.ecs.<region>.on.aws`
domain is publicly reachable, same as the CloudFront URL. Fine for
demos/internal use; if you need to guarantee traffic only goes through
CloudFront, add a shared-secret custom header check in `serve.py`
(CloudFront can inject one via an origin request policy).

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
  stack builds, if you don't want to build GPAC locally (mount the input
  **read-write**, not read-only — see Troubleshooting).
- Two IAM managed policies must exist in the account (they're standard AWS
  managed policies, nothing to create): `AmazonECSTaskExecutionRolePolicy`
  and `AmazonECSInfrastructureRoleforExpressGatewayServices` (note: the
  latter lives under the `service-role/` path —
  `arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices`).

## Setup

```bash
uv sync
cdk bootstrap        # once per account/region
cdk deploy LoopSharedStack   # once per account/region -- creates the shared
                              # "loop-dee-loop" ECS cluster every channel
                              # lives in (see shared_stack.py)
```

`LoopSharedStack` exists specifically so channels don't each create their
own `AWS::ECS::Cluster`: ECS cluster names are unique per account/region,
so if every `LoopChannelStack` created one named `loop-dee-loop`, the
second channel's deploy would collide with the first's, and `cdk destroy`
on any one channel would risk deleting the cluster out from under every
other channel. Deploy it once, before any channel; channel stacks just
reference the cluster by name.

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

[express]
cpu    = 256   # 0.25 vCPU
memory = 512   # 0.5 GB
```

The `name` field drives every AWS resource name (`loop-dee-loop-<name>-*`)
and the S3 key prefix (`<folder>/<name>/...`), so config files are fully
independent, parallel deployments.

## Full lifecycle (single channel, default config.toml)

⚠️ **Bake before you deploy, not after.** `serve.py` hard-crashes at
startup (not just a 404) if `LOOP_PACKAGE_S3_URI` is empty when the
container starts, and the Express service requires its container to start
successfully to consider the service created — so deploying the stack
before anything exists at that S3 prefix reliably fails. Push a package to
S3 *first*, then deploy:

```bash
# 1. Bake locally and push the result to S3. No AWS compute involved --
#    this just runs bake.py on your machine (or via `docker run` with the
#    image this stack builds) and `aws s3 sync`s the output up. Works even
#    before the stack exists, since it only needs S3 access.
uv run python channel.py bake

# 2. Provision the stack (builds+pushes the image, creates the Express
#    service + CloudFront distribution -- serve.py finds a real package at
#    LOOP_PACKAGE_S3_URI immediately, so the service starts cleanly).
#    Takes ~9-10 minutes end to end (Express service ~5-6 min including
#    ALB/target-group/cert provisioning on first use in the account,
#    CloudFront ~3 min). Requires LoopSharedStack to already be deployed
#    (see Setup) -- explicitly targets this one channel's stack, since the
#    app also contains LoopSharedStack.
cdk deploy LoopChannelStack-<name>

# 3. Start the channel: scales the Express service to 1 task (if it was
#    stopped), waits for the task to be running, prints the CloudFront
#    playback URLs. Epoch is fixed (see Notes below), so this is a pure
#    scaling operation, not a redeploy.
uv run python channel.py start

# ... channel is live ...

uv run python channel.py stop     # scales to 0 tasks -- no Fargate compute
                                    # cost while stopped (the shared ALB
                                    # itself keeps running for other channels)
cdk destroy LoopChannelStack-<name>
```

### Timing you should expect (measured against a real deployment)

- **First `cdk deploy`**: ~9-10 minutes (Express service creation ~5-6 min,
  CloudFront distribution ~3 min, run in parallel where possible).
- **`channel.py start`/`stop`**: fast (well under a minute) — pure scaling,
  no new task revision.
- **`channel.py refresh`** (see "Updating content" below): the CLI command
  returns once the new task reaches the running count (~1-2 minutes) — but
  **that is not the same as full viewer cutover**. ECS Express Mode uses a
  canary deployment strategy with a fixed ~3 minute bake period during
  which most or all traffic can still be served by the *previous* task
  (observed directly: zero requests landed on the new task for the full
  ~3 minutes, then a hard cutover). There's no fast, reliable API signal
  for "100% of viewers now see the new content" — budget a few extra
  minutes of buffer after `refresh` before assuming every viewer sees it.

## Updating content on a running channel (no image rebuild, no redeploy)

```bash
uv run python channel.py bake      # re-bake locally, push new package to S3
uv run python channel.py refresh   # force the running task to restart and
                                    # re-sync LOOP_PACKAGE_S3_URI
```

`docker-entrypoint.sh` re-syncs `LOOP_PACKAGE_S3_URI` from scratch every
time the container starts, so a restart is all that's needed to pick up a
new bake — the image itself never changes. `refresh` exists specifically
to force that restart on an *already-running* service (`start` alone won't
trigger it if the service is already at its target task count — see
Notes). Remember the canary bake-period
caveat above: give it a few minutes before assuming every viewer sees the
new content.

## Multiple channels

Same pattern as `push-to-aws-media`: one config file per channel, deployed
and managed independently. `LoopSharedStack` (the ECS cluster) only needs
deploying once total, not once per channel:

```bash
# Note: bake each channel BEFORE deploying it (see above).
uv run python channel.py --config ./configs/channel_a.toml bake
cdk deploy -c config=./configs/channel_a.toml LoopChannelStack-channel_a
uv run python channel.py --config ./configs/channel_a.toml start
...
cdk destroy LoopChannelStack-channel_a
```

Each channel gets its own `AWS::ECS::ExpressGatewayService` and its own
CloudFront distribution, but they all share the one cluster from
`LoopSharedStack` — and per AWS's docs, Express Mode services in the same
cluster/networking configuration **share** the underlying ALB, so the ALB
cost itself doesn't multiply per channel the way it did in the earlier
hand-built Fargate+ALB design. Consolidating N channels behind one
shared CloudFront distribution (instead of one per channel) is a further
optimization worth doing once you're running many channels, not
implemented here.

## channel.py reference

| Command | Description |
|---|---|
| `bake` | Run `bake.py` locally against `input.source_path`, push the result to S3 |
| `start` | Scale the Express service to 1 task (if it was stopped), wait for the task to be running, print playback URLs |
| `stop` | Scale the Express service to 0 tasks — this is what actually stops paying for Fargate compute between airings |
| `refresh` | Force a new task launch on an already-running service so it re-syncs `LOOP_PACKAGE_S3_URI` and picks up a fresh `bake` — not needed after `start` from a stopped state |
| `status` | Print the Express service's current status/scaling |
| `outputs` | Print all CloudFormation stack outputs |
| `redeploy` | Delete a broken stack if needed, then `cdk deploy` |

## Notes / gotchas

- **Epoch is fixed** at the Unix epoch (`1970-01-01T00:00:00Z`), permanently
  — never patched or reset. This is looping content simulating live, not a
  real broadcast, so there's no need to force loop position 0 on every
  start; landing mid-ad-break on start/restart is an accepted tradeoff. The
  upside: `start`/`stop` never need to touch the container command, so
  they're pure (fast) scaling operations, and multiple tasks briefly
  running side by side (e.g. during a `refresh`'s canary window, or a
  future multi-task auto-scale-out) always agree on position, since
  nothing about the epoch ever changes between them.
- **Cost**: Fargate compute (0.25 vCPU/0.5GB by default) + a *shared*
  ALB + CloudWatch logs/metrics + data transfer — no separate "Express
  Mode" charge per AWS's pricing page. `channel.py stop` genuinely stops
  the Fargate compute line (scales to 0 tasks); the shared ALB keeps
  running regardless, but its cost is amortized across every Express
  service using it, not paid per-channel.
- **Manifests are never cached** by CloudFront (`TTL=0`); segments under
  `*/seg/*` get a 5-minute default TTL — matches SCOPE.md §8's "CDN in
  front of `/seg/*`" recommendation.
- **Task role** is scoped to read-only on
  `loop-dee-loop/packages/<name>/*`. There's no separate bake IAM role —
  `channel.py bake` uses your own local AWS credentials (via the AWS CLI)
  to push to S3, same as any other `aws s3 sync`.
- **Canary deployments, not instant cutover** — see the timing section
  above. `refresh` forces a new task via `forceNewDeployment`, and ECS
  Express Mode's canary strategy means the *previous* task can still serve
  most/all traffic for ~3 minutes before cutover, with no fast/reliable
  API signal for "100% of viewers now see the new content."
- The pushed loop package is **not** deleted by `cdk destroy` (the stack
  doesn't own the bucket) — clean up manually with
  `aws s3 rm --recursive s3://<bucket>/<folder>/<name>/` if needed.

## Troubleshooting

- **`ServeService CREATE_FAILED`, logs show
  `exec /usr/local/bin/docker-entrypoint.sh: exec format error`**: the
  image was built for the wrong CPU architecture. `loop_channel_stack.py`
  pins `platform=ecr_assets.Platform.LINUX_AMD64` on the `DockerImageAsset`
  specifically to prevent this — on an Apple Silicon (arm64) machine,
  Docker builds arm64 by default unless told otherwise. If you still hit
  this, confirm that pin is in place and that Docker actually rebuilt (not
  reused a stale local arm64 image under the same tag).
- **`ServeService` seems to hang in `CREATE_IN_PROGRESS` for far longer
  than expected (observed: 90+ minutes with zero tasks ever placed and no
  target groups created)**, and `aws ecs describe-service-revisions`
  shows a `statusReason` like
  `ValidationError: Health check path '...' must begin with a '/'
  character ...`: the `health_check_path` given to
  `CfnExpressGatewayService` must be a **bare path** (e.g. `/manifest.mpd`),
  not a `PROTOCOL:PORT/PATH`-style string like `HTTP:8080/manifest.mpd` —
  despite the AWS docs' *default* being quoted as `"HTTP:80/ping"`, which
  reads like that combined format. Get this wrong and the deployment
  doesn't fail cleanly — it just retries the invalid ALB/listener/rule/
  target-group provisioning indefinitely without ever surfacing a
  CloudFormation-visible failure. If a `ServeService` create looks stuck,
  check the *real* underlying resources directly rather than trusting
  CloudFormation's events feed:
  ```bash
  aws ecs describe-service-revisions --service-revision-arns <arn>
  # look at .serviceRevisions[0].ecsManagedResources.ingressPaths[0].*.statusReason
  ```
- **`AWS::IAM::Role ... Policy ... does not exist or is not attachable`**
  for `AmazonECSInfrastructureRoleforExpressGatewayServices`: the managed
  policy lives under the `service-role/` path. Use
  `iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSInfrastructureRoleforExpressGatewayServices")`,
  not the bare name.
- **`Update of resource type is not permitted`** when redeploying after
  changing the compute backend (e.g. App Runner → Express Mode) under the
  same construct ID: CloudFormation refuses in-place resource-type changes
  for the same logical ID. `cdk destroy` then `cdk deploy` (or
  `channel.py redeploy`, which does the delete-then-deploy dance
  automatically for a stack stuck in `ROLLBACK_COMPLETE`) is required.
- **`ServeService CREATE_FAILED`, logs show
  `FileNotFoundError: ... loop_descriptor.json`**: nothing has been baked
  to `LOOP_PACKAGE_S3_URI` yet — see "bake before you deploy" above. Run
  `channel.py bake`, then `channel.py redeploy`.
- **`bake.py` fails with `'gpac' not found on PATH`** when baking via
  `docker run` with this stack's image: this was a real bug in
  `../loop-dee-loop/Dockerfile`'s build stage (fixed) — it assumed GPAC
  installs to `/usr/bin/gpac`, but a from-source `./configure` (no
  `--prefix`) actually installs to `/usr/local/bin`, and a trailing
  `|| true` was silently swallowing the resulting `cp` failure, so the
  runtime image shipped with **no gpac binary at all**. Make sure you have
  the current Dockerfile (resolves the real path via `command -v`, no
  longer masks that `RUN` command's failures).
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
