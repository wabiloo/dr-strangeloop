# Scoping: running igor itself on Kubernetes

Status: scoping only, not implemented. This is the Kubernetes-native
sibling of [`CLOUD_DEPLOYMENT.md`](./CLOUD_DEPLOYMENT.md) (which scopes
running igor on AWS/Fargate) and assumes
[`its-a-live/K8S_BACKEND.md`](../its-a-live/K8S_BACKEND.md)'s `k8s`
backend as the *only* its-a-live backend this deployment uses — this is
specifically the "hand dr-strangeloop to a colleague/another team so they
can self-host it on their own cluster, no AWS account needed at all"
shape, not a generic "igor on any infra" doc. Read `K8S_BACKEND.md` first;
this doc builds on its RBAC and Job-dispatch design rather than repeating
it.

Same discipline as `CLOUD_DEPLOYMENT.md`: each numbered workstream below
is real, not a checkbox, and the reader should ask before deviating from
anything marked **DECIDED**.

## Why this is simpler than the AWS case, and where it isn't

Simpler: no IAM, no ambient AWS profile, no cross-account role dance.
A pod gets its cluster identity for free via its mounted ServiceAccount
token -- if this igor only ever manages `k8s`-backend channels, it needs
**zero cloud credentials of any kind**.

Not simpler: `K8S_BACKEND.md` §5's `spark` step (bake + build + push a
per-channel image) assumed a Docker daemon is available wherever
`channel.py` runs. That was fine on a laptop; it's not available inside a
pod by default, and privileged Docker-in-Docker is the wrong answer here
-- the same lesson `CLOUD_DEPLOYMENT.md` already drew for `local-docker`
on Fargate ("Docker itself is the odd one out"). See workstream 4 for the
fix (a daemonless builder, dispatched as its own Job) and workstream 5 for
how igor's own state survives that split.

## Workstreams

### 1. AuthN/AuthZ

Same answer as `CLOUD_DEPLOYMENT.md` workstream 1, no AWS-specific parts
to it: building real app-level auth is disproportionate for an internal
ops console, so put igor behind a trust boundary instead --
Tailscale/VPN-only Service (no public Ingress at all), or an
authenticating Ingress (oauth2-proxy as a sidecar/companion Deployment, or
whatever OIDC-aware ingress controller the cluster already has). Revisit
in-app auth only if this needs to be reachable from the open internet or
needs per-user audit trails.

### 2. Filesystem browsing (`/api/v1/files/browse`, `/probe`)

Same sharp edge as before: today it's an unjailed listing of wherever the
backend process can see. In this shape, "wherever the backend process can
see" is exactly one PVC mount (workstream 5) -- scope the endpoint to that
mount path explicitly, not the pod's whole filesystem (which also
contains the image's own code/venvs -- no reason those should ever be
browsable).

### 3. Identity / RBAC

No IAM anywhere in this shape -- see above. igor's pod needs a
ServiceAccount bound to a Role scoped to its own namespace, extending
`K8S_BACKEND.md` §6's RBAC (which only covers Deployments/Services/Pods
for the *serving* side) with what igor itself additionally needs to
dispatch and observe build/spark work (workstream 4):

- `batch/jobs`: create, get, list, watch, delete
- `pods`: get, list, watch (to find a Job's pod)
- `pods/log`: get (to read a completed Job's output -- see workstream 5)

No cluster-scoped permissions anywhere, same principle `K8S_BACKEND.md`
already commits to: least-privilege, namespace-scoped, no cluster-admin.

### 4. Toolchain / base image, and where builds actually run

igor's own image needs: `ffprobe` (cheap, for the live schema/validation/
preview features -- see `igor/src/igor/integrations/franken_ts.py`), the
`franken_ts` package, its-a-live's separate venv (for `channel.py`/
`_k8s_ops.py`), and a `kubectl`/`helm` client. It does **not** need
`ffmpeg`, `tsp`/tsduck, GPAC, or a Docker daemon/socket -- all build work
moves into on-demand Kubernetes `Job`s that igor's ServiceAccount creates
(workstream 3), the same idea `CLOUD_DEPLOYMENT.md` workstream 5 scoped as
`ecs:RunTask` for AWS, but a cleaner fit here since `Job` is a first-class
"run this to completion once" primitive with built-in retry/backoff.

Two kinds of Job, both created by igor, both using a daemonless image
builder (**DECIDED: Kaniko or rootless BuildKit, not `docker build`** --
no privileged pods, for the reason above):

- **franken-ts build Job**: `ffmpeg` + `tsp`/tsduck + `franken_ts`,
  produces `outputs/<name>.ts` (+ `.markers.json`). Replaces today's local
  subprocess (`spawn_build_job`).
- **spark Job**: GPAC (`bake.py`) + the daemonless builder, produces and
  pushes the per-channel image `K8S_BACKEND.md` §5 describes. Replaces
  `_k8s_ops.py`'s local `docker build`/`push` for this deployment shape
  specifically -- `K8S_BACKEND.md` itself stays correct for a colleague
  running `channel.py` from their own machine against a cluster; this is
  the variant for when igor *itself* is the thing dispatching `spark` from
  inside the cluster.

Both Job pods need `outputs/` (workstream 5) mounted; neither needs
`data/playlists`/`data/channels` mounted directly -- igor passes what a
Job needs as env vars / a per-invocation ConfigMap instead of sharing that
volume, which is what keeps the answer to "where do the YAML/TOML files
live" simple (next section).

### 5. Persistent state -- where the YAML/TOML files actually live

Three different things, three different answers, deliberately not one
shared volume for everything:

**`data/playlists/*.yaml` (franken-ts playlists) and `data/channels/*.toml`
(its-a-live channel configs), plus `K8S_BACKEND.md`'s per-channel
`.k8s-state.json`, plus job history (today's in-memory `JobRunner._jobs`,
already flagged in `CLOUD_DEPLOYMENT.md` as needing to become a real
SQLite table):**

**DECIDED: one small `ReadWriteOnce` PersistentVolumeClaim, mounted only
by igor's own pod.** These are small, low-volume, text/SQLite files with
exactly one writer (igor itself, single replica -- see workstream 6).
There is no reason to need a shared/RWX volume for these at all, and RWO
is available on every cluster's default StorageClass, including a bare
kind/k3s cluster with no NFS/EFS-CSI/Longhorn set up -- unlike the
serving-side artifact question `K8S_BACKEND.md` §3 had to solve around,
this one has no hard portability constraint pushing away from the
simplest option.

Layout under the mount (e.g. `/data`, configurable):

```
/data
  playlists/*.yaml
  channels/*.toml
  channels/<name>.k8s-state.json   # sibling to that channel's .toml
  jobs.db                          # replaces JobRunner's in-memory dict
```

This requires one real code change: `igor/src/igor/paths.py` currently
computes `FRANKEN_TS_PLAYLISTS_DIR`/`ITS_A_LIVE_CONFIGS_DIR`/`OUTPUTS_DIR`
as fixed offsets from `REPO_ROOT` (`__file__`'s location) -- fine for a
git checkout, meaningless inside a container where "the repo" is baked
into an immutable image layer you don't want mutable state written into
anyway. Add an `IGOR_DATA_DIR` (and `IGOR_OUTPUTS_DIR`) environment
variable that overrides these, defaulting to today's `REPO_ROOT`-relative
paths so native/local runs are unaffected. The Helm chart points it at
the PVC mount.

**Build/spark Jobs never mount this PVC.** When igor dispatches a
franken-ts build or a spark, it injects exactly what that Job needs --
the playlist's YAML content, or the channel config values it needs -- as
Job env vars or a ConfigMap created for that one invocation, rather than
giving the Job filesystem access to igor's own state. Results come back
the same way igor already consumes them today: by watching the Job to
completion via the Kubernetes API and reading its pod's log output.
`igor/src/igor/integrations/franken_ts.py`'s `spawn_build_job` already
treats "the last non-empty stdout line" as the result contract for a
dispatched build -- reuse that same convention for a Job's log tail
instead of a local subprocess's stdout, and the same pattern gives
`.k8s-state.json`'s tag to igor without either Job ever touching igor's
PVC: igor parses the spark Job's log output for the pushed tag and writes
`channels/<name>.k8s-state.json` itself, in its own process.

**`outputs/` (built `.ts` files, potentially large, multi-rendition
ladders bigger still):** this is the one genuine hand-off between a
writer (the franken-ts build Job) and readers (the spark Job, and igor
itself for playback/preview) that a "pass it as env vars" trick can't
solve -- these are real media files, not small text. Two supported
options, a values.yaml toggle rather than a hard choice, mirroring
`CLOUD_DEPLOYMENT.md`'s own "EFS mount (or S3 migration)" flexibility for
the AWS case:

- A second PVC with an `ReadWriteMany`-capable StorageClass (NFS,
  EFS-CSI, Longhorn, CephFS, ...) mounted by igor and both Job types. Zero
  new infrastructure *if* the cluster already has one -- but, unlike the
  RWO case above, this is a real prerequisite the colleague's cluster
  might not satisfy, and should be checked/documented up front rather
  than discovered at first `spark`.
- Reuse `ecs-express`'s existing upload/download pattern (stage the
  output to object storage, download it back) against a self-hostable
  S3-compatible endpoint (MinIO is the standard choice) instead of real
  AWS S3. Slightly more moving parts (one more service to run) but sidesteps
  the RWX-availability question entirely, and there's already a proven
  code path in this repo doing exactly this hand-off shape for a
  different backend.

No default recommendation between these two -- it genuinely depends on
what the colleague's cluster already has. Document both, let the chart's
values pick.

### 6. Account/cluster placement

Run igor in the **same** cluster it manages, as the default. It's
simplest, it's what makes workstream 3's in-cluster ServiceAccount auth
free (no external kubeconfig Secret to provision/rotate), and it matches
this doc's whole framing: a colleague self-hosting on infrastructure they
already own, not a separate ops cluster targeting someone else's. A
separate ops-cluster shape (igor elsewhere, holding a kubeconfig Secret
for a *different* target cluster) is possible later if wanted, but adds
real complexity (credential rotation, network reachability between
clusters) for no benefit in the target scenario this doc is written for.

Single igor replica, not multiple -- workstream 5's RWO PVC assumption
depends on this. If multiple-replica igor is ever wanted (e.g. for
availability), the persistent-state design needs revisiting first
(RWX or a real database), not treated as a free upgrade.

### 7. Gaps that get worse with multiple users

Same two pre-existing gaps `CLOUD_DEPLOYMENT.md` flags for the AWS case,
translated:

- No teardown wiring beyond what `channel.py`/`_k8s_ops.py` already do --
  cleaning up an abandoned channel (Helm release + pushed images) still
  needs direct cluster access.
- No log tailing for the deployed workload from within igor -- job logs
  only cover the dispatching Job's own output (workstream 5), not a
  running channel's serve pod logs. `kubectl logs -f`/`stern` still needed
  for that, same as CloudWatch was for the AWS case.

Both are pre-existing, not new from this deployment shape, but more
painful once the person hitting them isn't the operator with direct
cluster access.

## A lightweight target shape

- one igor Deployment (light image per workstream 4: `ffprobe` +
  `franken_ts` + `kubectl`/`helm`, no ffmpeg/tsduck/GPAC/Docker), single
  replica, behind a VPN-only Service or an authenticating Ingress
- a ServiceAccount + namespace-scoped Role/RoleBinding covering
  Deployments/Services/Pods (`K8S_BACKEND.md` §6) plus Jobs/pod-logs
  (workstream 3) -- no IAM, no cluster-admin, anywhere
- one small `ReadWriteOnce` PVC for `data/playlists`, `data/channels`,
  `.k8s-state.json` files, and `jobs.db` (workstream 5)
- a second volume for `outputs/` -- an RWX PVC if the cluster has one, or
  a self-hosted MinIO if it doesn't (workstream 5, values-toggle)
- two Job templates (franken-ts build, spark) carrying the heavy
  toolchain + a daemonless builder, created on demand and never long-lived
  (workstream 4)
- `IGOR_DATA_DIR`/`IGOR_OUTPUTS_DIR` env vars added to `igor/src/igor/
  paths.py` (workstream 5), defaulting to today's behavior so native/local
  runs are unaffected by this deployment shape existing

## Explicitly out of scope here

- Multi-replica igor / a real shared database for its state -- workstream
  6's single-replica assumption is load-bearing; revisit together, not
  piecemeal.
- A durable, multi-instance job queue -- same call `CLOUD_DEPLOYMENT.md`
  makes for the AWS case; SQLite (workstream 5) is enough for one replica.
- CI/CD for building/publishing igor's own image, or the Job templates'
  images.
- Anything about `aws-media`/`ecs-express` -- this doc is specifically the
  `k8s`-backend-only, no-AWS-account shape. An igor deployment that also
  needs to manage AWS-backed channels is `CLOUD_DEPLOYMENT.md`'s scope,
  not this one, and mixing the two isn't addressed here.
