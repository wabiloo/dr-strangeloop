# Scoping: running igor off localhost

igor is currently built and run as a trusted, single-operator tool on
someone's own machine (see `AGENTS.md`'s "No auth -- do not expose this
off localhost without adding some"). This document scopes what changes
before it's safe/durable to run somewhere shared (a cloud VM, ECS/Fargate,
etc.) instead. It's a scoping pass, not a plan that's been agreed to yet --
each numbered item below is a real workstream, not a checkbox.

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
broad IAM directly.

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

### 5. Toolchain / base image
Needs `aws` CLI v2, `cdk` CLI v2 (Node.js), GPAC/ffmpeg/tsduck, igor's own
`.venv` (`uv sync --all-packages`), and its-a-live's separate `.venv`
(`cd its-a-live && uv sync`) baked into a custom image -- none of this can
be "assumed already there" the way local dev does. **Docker itself is the
odd one out**: the `local-docker` its-a-live backend shells out to `docker
run`, which needs a real Docker daemon/socket -- not available on Fargate
without privileged mode. Recommendation: drop `local-docker` from the cloud
deployment (keep it dev-machine-only; it's also redundant with
`ecs-express` for anything that isn't local iteration) rather than solve
Docker-in-Docker for one backend option.

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

- one container (FastAPI + built SPA, same as `Running (prod)` in
  `README.md`) on Fargate or a small EC2 box, behind Tailscale/VPN-only
  ingress or an authenticating proxy -- no app-level auth built
- `local-docker` backend dropped from this deployment; `ecs-express`/
  `aws-media` only
- an EFS mount (or S3 migration) for `data/` and `outputs/`
- a scoped IAM task role instead of an ambient profile, using CDK's
  bootstrap-role pattern for the deploy step
- `/api/v1/files/browse` restricted to a configured asset root
- a custom image with the full toolchain baked in, built from this repo
  rather than assembled by hand on the host

## Explicitly out of scope here

- A multi-tenant/multi-user auth model (per-user roles, channel ownership,
  audit log) -- the recommendation above is "trust boundary via network/
  proxy," not "build a user system."
- A durable, multi-instance job queue (would need a real DB-backed queue,
  not just swapping `JobRunner`'s dict for SQLite) -- only worth it past a
  single instance.
- CI/CD for igor itself (image build/publish pipeline).
