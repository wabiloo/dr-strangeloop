# Scoping: running igor off localhost

igor is currently built and run as a trusted, single-operator tool on
someone's own machine (see `AGENTS.md`'s "No auth -- do not expose this
off localhost without adding some"). This document scopes what changes
before it's safe/durable to run somewhere shared (a cloud VM, ECS/Fargate,
etc.) instead. It's a scoping pass, not a plan that's been agreed to yet --
each numbered item below is a real workstream, not a checkbox.

This doc is specifically the AWS-hosted shape. For running igor on a
colleague's own Kubernetes cluster instead (no AWS account involved at
all), see [`K8S_DEPLOYMENT.md`](./K8S_DEPLOYMENT.md) -- same underlying
concerns (auth, persistent state, toolchain, splitting build execution
out), Kubernetes-native answers throughout.

## Local dev and cloud deliberately use different container shapes

See [`../DOCKER_LOCAL.md`](../DOCKER_LOCAL.md) for the local-bundling shape
described in this section, already built (`docker-compose.yml` at the repo
root) -- handing a colleague a working console with nothing installed
locally, against their own files, is a solved problem for the
single-operator case; everything below in this document is about the
different, harder problem of making igor reachable by a *team* over a
network.

For local Docker use (a single operator on one machine), bundle igor +
franken-ts + the ffmpeg/tsduck toolchain into one image: CPU is shared and
effectively free on your own box, builds are interactive, and the added
complexity of a second image buys nothing there (see the "Would I gain
from doing that?" reasoning: igor's build dispatch already runs a build as
an isolated OS subprocess, not a thread, so bundling doesn't cost you
crash-isolation either). `loop-dee-loop` stays a separate container even
locally, but only because `local-docker` launches it as a sibling
container via the Docker socket -- that split is forced by the existing
design, not chosen for resource isolation.

In the cloud, the calculus flips, because you pay for provisioned capacity
continuously rather than sharing a box's idle CPU: igor's own FastAPI+SPA
backend needs about what `loop-dee-loop`'s `serve.py` needs (~0.25 vCPU /
0.5 GB, see `PERFS.md`), but a real `ffmpeg` build can spike to 1+ vCPU and
several GB depending on source resolution/duration. Sizing one always-on
container for ffmpeg's worst case just to serve a small web app 24/7 is
wasted spend. Workstream 5 below splits build execution out for this
reason -- the same "one-shot heavy task, separate from the steady-state
service" pattern `its-a-live`'s `ecs-express` backend already uses for
`bake.py` vs `serve.py`. **Don't try to make the local and cloud shapes
match** -- they're solving different problems (interactivity vs.
continuous billing).

## Why this isn't just "put the container somewhere"

The FastAPI app + built SPA is trivially containerizable. What makes this
non-trivial is everything igor currently assumes about *where* it runs:

- it has no authentication of its own (anyone who can reach the API can
  deploy/stop channels and spend AWS money -- `channels.py:AGENTS.md`)
- `GET /api/v1/files/browse` and `/probe` (`igor/src/igor/integrations/files.py`)
  browse and ffprobe **any path on the backend's filesystem**, unauthenticated
  and unjailed -- `Path(path).expanduser()`, no root restriction
- AWS calls go through whatever ambient credentials `boto3.session.Session()`
  picks up in `its-a-live/channel.py` -- today that's the operator's own
  local AWS profile, not a scoped service identity
- all state (channel TOML configs, franken-ts playlists, job history) lives
  on local disk or in an in-process Python dict -- see the "How igor knows
  about channels" answer from this conversation: `data/channels/*.toml`,
  `data/playlists/*.yaml`, and `JobRunner._jobs` (in-memory, lost on restart)
- the toolchain it shells out to (`aws` CLI v2, `cdk` CLI v2/Node, Docker,
  GPAC/ffmpeg/tsduck, its-a-live's separate `.venv`) is assumed already
  installed on the box, per `README.md`'s Requirements section

## Workstreams

### 1. AuthN/AuthZ
No user/session concept exists in the FastAPI app today (`app/main.py` has
zero auth middleware). Building real app-level auth is disproportionate for
what is fundamentally an internal ops console for a small team. Cheaper and
more appropriate: put it behind a trust boundary instead of building one in
-- a private network (Tailscale/VPN-only ingress) or an authenticating
reverse proxy (oauth2-proxy, or an ALB + Cognito/OIDC in front of it).
Revisit real in-app auth only if this needs to be reachable from the open
internet or needs per-user audit trails.

### 2. Filesystem browsing (`/api/v1/files/browse`, `/probe`)
This is the sharpest edge: today it's an open directory listing of
wherever the backend process can see, with no root restriction, gated only
by "you're on localhost". Before this is reachable by anyone but the
operator at their own keyboard, it needs to be scoped to an explicit asset
root (e.g. an EFS/S3-backed media directory passed in via config), not the
container's whole filesystem. If cloud asset storage moves to S3 instead of
local disk, this endpoint's whole model (local path picker) should probably
be rebuilt as an S3 key browser rather than jailed-and-reused as-is.

### 3. AWS credentials / IAM
Replace the ambient local AWS profile with a scoped IAM role (task role for
Fargate, instance profile for EC2) granting exactly what `channel.py`'s ops
modules call: `cloudformation:DescribeStacks`, the ECS Express Gateway
Service actions (`_ecs_express_ops.py`), `medialive:*Channel*`
(`_aws_media_ops.py`), S3 read/write for the content bucket, plus whatever
`cdk deploy`/`cdk bootstrap` needs (CFN execution role, staging bucket,
ECR). Prefer CDK's own bootstrap-role pattern (igor's identity only needs
`sts:AssumeRole` into the CDK deploy role) over granting igor's identity
broad IAM directly. If franken-ts build execution moves to a separate
`RunTask` (workstream 5), igor's role also needs `ecs:RunTask`,
`ecs:DescribeTasks`, and `iam:PassRole` scoped to just the assembler task's
own execution/task roles -- not broad `ecs:*`.

### 4. Persistent state
`data/channels/*.toml`, `data/playlists/*.yaml`, `outputs/`, and
`its-a-live/.local-loop-package/*` are all plain local-disk paths
(`igor/src/igor/paths.py`). A stateless/replaceable container loses the
entire channel registry on redeploy. Needs either a persistent volume (EFS
mount survives container replacement) or moving this storage into S3/a
small DB. Job history (`JobRunner`, `igor/src/igor/jobs/runner.py`) is
explicitly documented as in-memory-only and already flags its own fix
("swap `_JOBS`/lock for a small SQLite table") -- decide whether orphaned
jobs on restart are tolerable for a shared deployment or whether that
durability work needs to happen first.

### 5. Toolchain / base image, and splitting build execution out

Needs `aws` CLI v2, `cdk` CLI v2 (Node.js), igor's own `.venv` (`uv sync
--all-packages`), and its-a-live's separate `.venv` (`cd its-a-live && uv
sync`) baked into a custom image -- none of this can be "assumed already
there" the way local dev does. **Docker itself is the odd one out**: the
`local-docker` its-a-live backend shells out to `docker run`, which needs a
real Docker daemon/socket -- not available on Fargate without privileged
mode. Recommendation: drop `local-docker` from the cloud deployment (keep
it dev-machine-only; it's also redundant with `ecs-express` for anything
that isn't local iteration) rather than solve Docker-in-Docker for one
backend option.

**ffmpeg/tsduck do NOT belong in igor's own image for a cloud deployment**
(unlike the local Docker shape, which does bundle them -- see above).
igor's in-process use of `franken_ts` (schema generation, config
validation, marker/timeline preview -- `igor/src/igor/integrations/
franken_ts.py`) only needs `ffprobe`, which is cheap; keep that in igor's
image. The actual build (`franken-ts <playlist>`, today dispatched by
`spawn_build_job` as a local subprocess) is the rare, heavy step that uses
`ffmpeg` + `tsp`/tsduck, and belongs in its own image + ECS task
definition instead, sized like `bake.py` (`PERFS.md`: ~1 vCPU / 2 GB,
one-shot, run only when triggered):

- A second image (`franken-ts-assembler`, or similar): `ffmpeg`, `tsp`
  (tsduck), and the `franken_ts` package -- nothing else. Built from this
  repo's `franken-ts/` alongside igor's own image, not assembled by hand.
- `spawn_build_job` changes from "run a local subprocess" to "call
  `ecs:RunTask` against the assembler task definition, then poll
  `ecs:DescribeTasks`/read a completion marker" -- a real change to
  `igor/src/igor/integrations/franken_ts.py` and `igor/src/igor/jobs/
  runner.py`, not just infra. Expect real cold-start latency (tens of
  seconds for a fresh Fargate task to launch) that doesn't exist locally.
- The assembler task needs the same shared storage as igor for
  `data/playlists/*.yaml` (input) and `outputs/` (result) -- see
  workstream 4; this is the same S3/EFS migration that workstream needs
  anyway, not new scope.

**GPAC is the same problem, smaller.** `channel.py spark` for `ecs-express`
channels also runs a subprocess (`loop-dee-loop/bake.py`, GPAC-based) --
dispatched by igor exactly like a franken-ts build (`igor/src/igor/
integrations/its_a_live.py`'s `spark` job), so it would also run inside
igor's own cloud container unless treated the same way. Per `PERFS.md`'s
own measurements, `bake.py` is small and fast (~288 MB peak RSS, ~3.5s for
a 204s clip) -- much lighter than a real `ffmpeg` transcode -- so it's a
reasonable, deliberate call to leave GPAC in igor's own image rather than
split it too; that's a judgment call based on today's measured numbers,
not a hard rule. If a fully build-work-free igor container is wanted
later, the exact same `RunTask` treatment applies to `spark` as to
franken-ts builds.

### 6. Account/network placement
`cdk deploy`/`channel.py create`/`redeploy` need outbound AWS API access
and a CDK-bootstrapped target account. Decide whether igor runs in the same
AWS account as the channels it manages (simpler) or a separate ops account
assuming a cross-account role into each target account (more isolation,
more setup). Simpler is the reasonable default unless there's already a
multi-account boundary in place for other reasons.

### 7. Gaps `AGENTS.md` already flags that get worse with multiple users
- No `cdk destroy` wiring -- cleaning up a broken/abandoned channel still
  needs direct CLI access, which a shared deployment's users may not have.
- No CloudWatch Logs tailing -- job logs only cover the orchestrating
  subprocess's stdout, not the deployed ECS task's/MediaLive channel's
  runtime logs, making remote debugging harder without shell access to AWS.

Both are pre-existing known gaps, not new problems from going to the
cloud, but they're more painful once the person hitting them can't just
drop into the CLI locally.

## A lightweight target shape

For a small-team internal tool (not a multi-tenant product), the
proportionate version of all this is:

- one steady-state container (FastAPI + built SPA, `aws` CLI v2, `cdk`
  CLI v2, `ffprobe`, GPAC, igor's + its-a-live's `.venv`s -- **not**
  `ffmpeg`/tsduck) on Fargate sized like `loop-dee-loop/serve.py`
  (~0.25 vCPU / 0.5 GB), behind Tailscale/VPN-only ingress or an
  authenticating proxy -- no app-level auth built
- a second image/task definition (`ffmpeg` + `tsp`/tsduck + the
  `franken_ts` package, nothing else) that igor dispatches franken-ts
  builds to via `ecs:RunTask`, sized like `bake.py`'s recommendation
  (~1 vCPU / 2 GB) but only billed while a build actually runs -- see
  workstream 5
- `local-docker` backend dropped from this deployment; `ecs-express`/
  `aws-media` only
- an EFS mount (or S3 migration) for `data/` and `outputs/`, shared
  between the steady-state container and the on-demand assembler task
- a scoped IAM task role instead of an ambient profile, using CDK's
  bootstrap-role pattern for the deploy step, plus `ecs:RunTask`/
  `ecs:DescribeTasks`/scoped `iam:PassRole` for dispatching builds
- `/api/v1/files/browse` restricted to a configured asset root
- both images built from this repo (not assembled by hand on the host);
  this is a deliberate departure from the local Docker Compose shape,
  which bundles everything with igor into one image for simplicity -- see
  "Local dev and cloud deliberately use different container shapes" above

## Explicitly out of scope here

- A multi-tenant/multi-user auth model (per-user roles, channel ownership,
  audit log) -- the recommendation above is "trust boundary via network/
  proxy," not "build a user system."
- A durable, multi-instance job queue (would need a real DB-backed queue,
  not just swapping `JobRunner`'s dict for SQLite) -- only worth it past a
  single instance.
- CI/CD for igor itself (image build/publish pipeline).
