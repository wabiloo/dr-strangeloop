# its-a-live: a generic-Kubernetes backend — scope

Status: scoping only, not implemented, not wired into `channel.py` yet.
This document is self-contained; the implementer should not need the chat
history that produced it, but should ask before deviating from anything
marked **DECIDED** below.

## 1. Problem statement

Every existing its-a-live backend either needs an AWS account (`aws-media`,
`ecs-express`, both CDK/CloudFormation) or runs on exactly one machine
(`local-docker`, `docker run` on whatever box `channel.py` executes on).
None of them is a fit for "hand this project to a colleague/another team
who wants to self-host it on their own Kubernetes cluster" — which may not
even be on AWS (could be GKE, on-prem, a bare k3s/kind cluster).

This adds a fourth backend, `backend = "k8s"`: the `local-docker` idea
("bake + serve entirely on infrastructure you already have, no managed AWS
media services, no CloudFormation stack") retargeted at a Kubernetes
cluster instead of a local Docker daemon. Same non-goal as
`local-docker`: no MediaLive, no MediaPackage, no CloudFront -- just
`loop-dee-loop`'s existing bake/serve code, deployed with Kubernetes-native
primitives.

## 2. Non-goals (explicitly out of scope for v1)

- Not a hosted/multi-tenant service. One Helm release per its-a-live
  installation, operated by whoever owns that cluster -- same "you run it,
  you operate it" model as every other backend here.
- Not cross-cluster or cross-cloud orchestration. Exactly one kubeconfig
  context per `channel.py` invocation, same simplicity as one AWS
  account/region per config file for the other backends.
- Not replacing or merging with `local-docker`. They stay separate
  backends even though the idea is similar -- `local-docker`'s bind-mount
  model and this backend's baked-image model (§4) are different enough
  mechanisms that forcing them into one code path would be a premature
  abstraction over two genuinely different designs.
- Not touching `aws-media`/`ecs-express`. Purely additive; nothing about
  the existing backends changes.
- No autoscaling design. An HPA hook in the chart (§6) is a nice-to-have,
  not a requirement -- ship without it if it adds friction.
- No CI/CD pipeline for building/pushing images. `channel.py spark` does
  the build+push itself (§5), same "bake happens wherever channel.py
  runs" model as `local-docker`. A real CI pipeline is a separate project.
- Not scoping igor's franken-ts build-execution split here (the
  `ecs:RunTask`-vs-Kubernetes-`Job` question from `igor/CLOUD_DEPLOYMENT.md`
  workstream 5). That's igor's build dispatch, a different concern from
  this backend's deploy/serve mechanics -- see §9 for the pointer, but
  it's not this doc's scope.

## 3. Why `local-docker`'s bind-mount doesn't just port over

`local-docker` gets away with a bind-mount (`docker run -v
<local-loop-package>:/var/loop-package`) because bake and serve run on the
*same machine*. On a cluster they usually don't -- the pod that serves a
channel is not guaranteed to land on the node `channel.py spark` ran on,
and even if it did, a plain hostPath mount isn't something you can assume
across arbitrary clusters.

Two ways to bridge that gap:

- **A shared RWX-capable PersistentVolumeClaim** (NFS, EFS-CSI, Longhorn,
  CephFS, ...) mounted by both the bake step and the serving Deployment.
  Closest to `local-docker`'s actual mechanism, but RWX storage isn't
  available on every cluster out of the box -- a colleague running k3s or
  kind on bare metal may not have one. That's exactly the kind of silent
  "assumed prerequisite" gap already flagged elsewhere in this repo
  (unbuilt binaries, ambient AWS profiles) -- avoid reintroducing it here.
- **Bake into a per-channel container image** instead, pushed to a
  registry. No RWX storage needed anywhere -- a container registry is
  universally available even on the smallest cluster, and it's the
  idiomatic way Kubernetes expects a workload to receive its artifact.

**DECIDED: per-channel container image, not a shared volume.** Portability
across arbitrary clusters (including ones with no RWX storage class at
all) matters more here than mirroring `local-docker`'s exact mechanism.
This does add one new prerequisite `local-docker` never needed: a
container registry reachable from wherever `channel.py spark` runs *and*
from the cluster's nodes. Document this explicitly in the chart's README
-- it can be as small as a self-hosted registry, or any of
Docker Hub/ghcr.io/ECR/GCR the colleague already has access to.

## 4. Architecture

```
franken-ts output (.ts + .markers.json)
        │
        ▼
   channel.py spark
        │  1. bake.py (GPAC, local subprocess -- same as local-docker/
        │     ecs-express) -> loop package on local disk
        │  2. docker build: loop-dee-loop base image + COPY the baked
        │     package to /var/loop-package -> per-channel image
        │  3. docker push <registry>/<repo>:<channel>-<tag>
        │  4. record the pushed tag (§7 -- k8s has no CFN outputs to
        │     store this in, unlike the AWS backends)
        ▼
   channel.py start / refresh
        │  helm upgrade --install (idempotent for both) against the
        │  chart in §6, pointing at the tag from spark
        ▼
   Deployment (serve.py under gunicorn, wsgi.py -- same production
   path already added for ecs-express) + Service + optional Ingress
        ▼
   live-sliding HLS/DASH, served from the cluster
```

The served container is **exactly** `loop-dee-loop`'s existing image plus
one `COPY` layer -- no new serving code. `serve.py`/`wsgi.py`/gunicorn are
unchanged from what `ecs-express` already uses.

## 5. `channel.py spark`: bake, build, push

Mirrors `_ecs_express_ops.spark`/`_local_docker_ops.spark`'s existing
shape (`_resolve_python_cmd()` to run `bake.py` locally), then adds a
build+push step neither of those needs:

```
uv run --project loop-dee-loop python bake.py <source> --output <local_output_dir> ...
docker build -f loop-dee-loop/Dockerfile.k8s -t <registry>/<repo>:<channel>-<content-hash> loop-dee-loop/ --build-arg LOOP_PACKAGE_DIR=<local_output_dir>
docker push <registry>/<repo>:<channel>-<content-hash>
```

- **Tag = content hash of the baked package** (e.g. a short hash of
  `loop_descriptor.json` + the segment file list), not a timestamp or
  "latest". Content-addressed tags make `refresh` idempotent (re-sparking
  identical content produces the same tag, so `start`ing twice without an
  intervening content change is a no-op image-wise) and make rollback
  trivial (`helm upgrade` back to a previous tag is just re-pointing the
  Deployment).
- **Where the tag gets recorded**: the AWS backends read this back via
  CloudFormation stack outputs; `local-docker` reads it back via `docker
  inspect` on the running container. Neither exists here. **DECIDED**:
  write a small `.k8s-state.json` next to the channel's config (mirroring
  `local-docker`'s `.local-loop-package/<name>/` convention) recording the
  last-pushed tag. `start`/`refresh` read it from there.
- `Dockerfile.k8s` is a tiny second-stage Dockerfile
  (`FROM loop-dee-loop:latest` + `COPY <package> /var/loop-package/`), not
  a full rebuild of the GPAC/ffmpeg toolchain per channel -- keeps `spark`
  fast and keeps the heavy build stage cached.

## 6. Helm chart shape

One chart (`its-a-live/k8s-chart/` or similar), one `helm upgrade
--install <release-name> ... -n <namespace>` per channel, release name
`its-a-live-<channel-name>` -- same deterministic per-channel naming
convention `local-docker` already uses for its container name.

Resources per release:

- **Deployment**: 1 replica by default (configurable), container image =
  the tag from `.k8s-state.json`, command mirrors what `loop_stack.py`
  already passes to the `ecs-express` container (`serve.py --host 0.0.0.0
  --port <port> --dvr-window-seconds <n> --epoch-utc <epoch>`) -- **but**
  see §7, this needs one small `docker-entrypoint.sh` change to route
  through gunicorn without an S3 sync.
- **Service**: `ClusterIP`, targeting the Deployment's port.
- **Ingress** (optional, off by default): host + path from values, for
  clusters with an ingress controller already set up. Without it,
  `channel.py status` should print a `kubectl port-forward` hint instead
  of a URL, the same way `local-docker` prints `http://localhost:<port>`
  when there's nothing else to print.
- **HorizontalPodAutoscaler** (optional, off by default): nice-to-have per
  §2, not required for v1.
- **ServiceAccount + Role + RoleBinding** for whatever identity
  `channel.py`/igor uses to run `helm`/`kubectl` against the cluster --
  scoped to exactly this namespace and exactly the verbs needed (get/list/
  watch/update/patch on Deployments and Services, get/list on Pods for
  `status`). **DECIDED**: no cluster-admin, ever -- same "prefer a scoped
  role over broad access" principle `igor/CLOUD_DEPLOYMENT.md`'s IAM
  section already applies to AWS; apply it here too.

Values file covers: image repository/tag (set by `channel.py`, not
hand-edited), replica count, resource requests/limits, port,
`dvr_window_seconds`, `epoch_utc`, ingress host/enabled, namespace.

## 7. One small required change: `docker-entrypoint.sh`

Today's production path (added for `ecs-express`, see `PERFS.md`) only
knows how to reach gunicorn via an S3 sync:

```
if LOOP_PACKAGE_S3_URI is set:
    sync_down, then exec gunicorn ... wsgi:app
else:
    exec python3 serve.py ...   # dev server -- local-docker uses this today
```

This backend's images have the package **already baked in** at
`/var/loop-package` (§5) -- there's nothing to sync, but it still needs
gunicorn, not the dev server. Add a third condition: if
`LOOP_PACKAGE_S3_URI` is unset but `LOOP_PACKAGE_LOCAL_DIR` (default
`/var/loop-package`) already contains a `loop_descriptor.json`, skip
`sync_down` and go straight to the same gunicorn/`wsgi:app` path used for
the S3 case. `EPOCH_UTC`/`DVR_WINDOW_SECONDS`/`WINDOW_SEGMENTS` env vars
(already how `wsgi.py` is configured) come from the Deployment's env,
set by the Helm chart from values.

This is the only change needed to `loop-dee-loop/` itself -- `bake.py`,
`serve.py`, `wsgi.py`, and the Dockerfile's build stage are all reused
unmodified.

## 8. `channel.py` integration

`channel.py`'s dispatch (`_BACKENDS`, `_NO_STACK_BACKENDS`, `_ops()`)
already generalizes cleanly to a fourth no-stack backend -- confirmed by
reading the current source, not assumed:

```python
_BACKENDS = ("aws-media", "ecs-express", "local-docker", "k8s")
_NO_STACK_BACKENDS = ("local-docker", "k8s")   # no CloudFormation, ever

def _ops(cfg):
    if _backend(cfg) == "ecs-express": import _ecs_express_ops; return _ecs_express_ops
    if _backend(cfg) == "local-docker": import _local_docker_ops; return _local_docker_ops
    if _backend(cfg) == "k8s": import _k8s_ops; return _k8s_ops
    import _aws_media_ops; return _aws_media_ops
```

`cmd_create`/`cmd_redeploy`/`cmd_list` already branch on
`_NO_STACK_BACKENDS` generically -- adding `"k8s"` to that tuple is the
*entire* change needed in `channel.py` itself. `create` becomes `spark` +
`start` (no shared-cluster-stack step, since there's no CDK involved at
all); `redeploy` becomes `refresh`. Nothing else in `channel.py` needs to
change; only a new `_k8s_ops.py` module (mirroring `_local_docker_ops.py`'s
shape: `spark`/`start`/`stop`/`refresh`/`status`, same function
signatures) and the `_session(cfg)` boto3 Session igor always constructs
is simply never used by it (harmless -- constructing a boto3 `Session`
object makes no network call and needs no credentials on its own).

`_k8s_ops.py` responsibilities, one function each:

| Command | Behavior |
|---|---|
| `spark` | §5: bake, build, push, record tag in `.k8s-state.json` |
| `start` | `helm upgrade --install` (idempotent -- also covers first create), then `kubectl rollout status` to confirm it actually came up before returning, then print the Ingress URL or a port-forward hint |
| `stop` | `kubectl scale deployment/its-a-live-<name> --replicas=0 -n <namespace>` -- mirrors `ecs-express`'s "scale to 0, don't delete" |
| `refresh` | Re-run `helm upgrade` pointing at the latest tag from `.k8s-state.json`, then `kubectl rollout status` -- a real "fully rolled out" signal, unlike `ecs-express`'s canary window (`its-a-live/README.md`'s own documented gap: "no fast/reliable API signal for '100% of viewers now see the new content'") |
| `status` | `kubectl get deployment ... -o json`, report replica/availability counts the same shape `local-docker`'s `status()` already returns (`{"status": ...}`) so `channel.py list`'s existing generic handling of `_NO_STACK_BACKENDS` needs no changes |

## 9. Config schema addition

New `[deploy].backend = "k8s"` value; `[aws]`/`[s3]` sections ignored
(same convention `local-docker` already uses -- comment them as such). New
section in place of `[express]`/`[docker]`:

```toml
[kubernetes]
namespace         = "its-a-live"
registry          = "registry.example.com/its-a-live"  # <registry>/<repo> prefix images get pushed/pulled from
kubeconfig_context = ""   # empty = current context
replicas          = 1
port              = 8080
cpu_request       = "250m"
memory_request    = "512Mi"
cpu_limit         = "1"
memory_limit      = "1Gi"

[kubernetes.ingress]
enabled = false
host    = ""
```

`[bake]`, `[markers]`, `[packaging]` sections are unchanged and fully
reused -- this backend bakes exactly like `ecs-express`/`local-docker` do
today.

## 10. Related but out of scope here: running igor itself on the cluster

This doc only covers `channel.py`/`_k8s_ops.py` -- it says nothing about
where `channel.py` itself runs. If igor is what's meant to run `spark`
(the natural shape when a colleague wants a management UI, not just a
CLI), igor needs to run somewhere too, and its own build/bake dispatch has
the same "isolate heavy toolchain work from a steady-state process"
question `igor/CLOUD_DEPLOYMENT.md` workstream 5 scopes as an
`ecs:RunTask` for the AWS-hosted case. On a generic Kubernetes cluster the
equivalent primitive is a Kubernetes `Job` -- arguably a cleaner fit than
`ecs:RunTask`, since `Job` is built exactly for "run this to completion
once," including the `spark` build+push step this doc's §5 describes.
That's scoped in [`igor/K8S_DEPLOYMENT.md`](../igor/K8S_DEPLOYMENT.md),
which builds directly on this doc's RBAC (§6) and treats §5's `docker
build`/`push` as something that needs a daemonless builder (Kaniko/
rootless BuildKit) once it's running inside a pod rather than on an
operator's own machine.

## 11. Acceptance checklist

- [ ] `docker-entrypoint.sh`'s "baked-in package, no S3 sync" branch (§7)
      added and covered by a test alongside the existing S3-sync-path
      coverage.
- [ ] `_k8s_ops.py` implements `spark`/`start`/`stop`/`refresh`/`status`
      with the same signatures `_local_docker_ops.py` uses.
- [ ] `channel.py`'s `_BACKENDS`/`_NO_STACK_BACKENDS`/`_ops()` updated
      (§8) -- `create`/`redeploy`/`list` need zero further changes beyond
      that, confirmed against the current source in §8.
- [ ] Helm chart (§6) renders a working Deployment/Service from values
      alone, with RBAC scoped to the release's own namespace -- no
      cluster-admin anywhere.
- [ ] `spark` produces a content-addressed tag; re-sparking identical
      content produces the same tag (idempotent).
- [ ] `refresh` gives a real "fully rolled out" completion signal via
      `kubectl rollout status`, not a fire-and-forget dispatch.
- [ ] End-to-end manual test on at least one non-EKS cluster (e.g. kind
      or k3s) with no RWX storage class available at all, to prove the
      per-channel-image approach (§3/§4) doesn't quietly depend on
      storage the design claims it doesn't need.
- [ ] `its-a-live/README.md`'s backend table and `config.toml` updated to
      mention `k8s` once implemented (not before -- this doc stays
      "scoped, not wired up" per its Status line until then, same
      convention `MEDIAPACKAGE_V2.md` uses for the v2 MediaPackage design).
