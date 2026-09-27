# Running igor locally via Docker (no toolchain install)

This is for handing `igor` to a colleague who has Docker but doesn't want
to install Python/uv/Node/ffmpeg/tsduck/GPAC/aws-cli/cdk by hand just to
use the console on their own machine, against their own local (and http)
media files. It is **not** the shared/cloud deployment described in
`igor/CLOUD_DEPLOYMENT.md` -- that doc is about putting igor somewhere
reachable by a team over a network; this one is about one person running
the whole thing locally, in a container, as a drop-in replacement for a
manual toolchain install. `igor/K8S_DEPLOYMENT.md` is a third, unrelated
shape (a shared team deployment on someone's own Kubernetes cluster). All
three inherit the same "no auth built in" posture from `igor/AGENTS.md` --
this shape doesn't change that, it just removes install friction for a
single operator.

## Quickstart

```bash
cp .env.example .env
# edit .env: set HOST_REPO_ROOT to `pwd` (this directory), and MEDIA_ROOT
# to wherever your source video/slate assets live.

docker compose up --build
# first build takes a while (GPAC compiles from source, same as
# loop-dee-loop/Dockerfile) -- cached after that.

open http://localhost:8090/admin/
```

Everything igor normally shells out to -- `franken-ts`, `its-a-live/channel.py`
(any backend, including `cdk deploy`), `loop-dee-loop`'s `bake.py` -- runs
inside this one container. `local-docker` channels and `cdk deploy`'s image
build/push run as sibling containers on your own Docker install (see
below), not nested inside this one.

## Why this needed more than "wrap it in a Dockerfile"

The sharp edge is that several operations igor dispatches aren't pure
Python -- they shell out to `docker build`/`docker run` themselves
(`its-a-live/_local_docker_ops.py` for the `local-docker` backend, and
`ecs-express`'s CDK stack via `aws_ecr_assets.DockerImageAsset` when you
`cdk deploy`). To make those work from inside a container without Docker-
in-Docker/privileged mode, `docker-compose.yml` mounts the **host's**
Docker socket (`/var/run/docker.sock`) into igor's container, and igor's
image includes just the `docker` CLI (no daemon runs inside the
container). That makes every `docker ...` command igor's Python code
issues actually run against your **host's** Docker install -- the
containerized igor process is really just a remote control for it.

That has one consequence that shaped everything else here: the host
daemon resolves every path it's given (a bind-mount source, a build
context directory) against **its own (the host's) filesystem** -- it has
no idea igor is running inside a container, or what that container's
mount namespace looks like. Paths that its-a-live computes internally
(`its-a-live/.local-loop-package/<channel>`, `loop-dee-loop/` as a build
context) are derived from `__file__`, i.e. from wherever the code
physically lives *inside igor's own container*. For those to also be
valid, correct **host** paths, the container has to see the code at
*literally the same absolute path* it lives at on your host -- so
`docker-compose.yml` bind-mounts your whole checkout at `HOST_REPO_ROOT`
inside the container too (`${HOST_REPO_ROOT}:${HOST_REPO_ROOT}`, not
remapped to some fixed `/repo`).

Two things fall out of that one constraint:

- **The image is built per-checkout-path, not shared.** `uv` bakes the
  absolute path it was run from into each workspace member's editable-
  install metadata (see `igor/Dockerfile`'s comments). If the build path
  and the runtime mount path don't match byte-for-byte, `import franken_ts`
  etc. breaks at runtime even though the venv itself is intact. So
  `REPO_ROOT` is threaded through as a build arg equal to `HOST_REPO_ROOT`,
  and each colleague runs `docker compose build` once, for their own
  machine -- there's no single image to hand around.
- **The venvs built at image-build time need putting back at container
  start.** The bind mount above shadows whatever `uv sync` produced under
  the checkout at build time (`.venv/`, `its-a-live/.venv/`) with your
  actual (venv-less, freshly cloned) host checkout. Both venvs are built
  to `/opt/venv-root` / `/opt/venv-its-a-live` instead (outside the mounted
  path) and `igor/docker-entrypoint.sh` symlinks them back into place on
  every container start, so `its-a-live/paths.py`'s existing venv-detection
  logic needs no code change at all.

One thing that works for free, precisely *because* of the socket-sharing
approach above: `local-docker` channels publish their HLS/DASH ports
directly on your **host** (the host daemon creates and binds those sibling
containers), so `http://localhost:<port>/index.m3u8` from `channel.py
start`'s output is immediately reachable from your own browser -- no port
plumbing between igor's container and your host was needed for that case.

## What you get vs. what you don't

Included: franken-ts builds (ffmpeg/tsduck/GPAC all in-image), all three
its-a-live backends (`aws-media`/`ecs-express` need your own AWS
credentials -- see `.env.example` -- `local-docker` needs nothing but your
host Docker install), the asset file-browser/picker scoped to whatever you
set `MEDIA_ROOT` to.

Not included, same as running igor any other way (`igor/AGENTS.md`'s
"Known gaps"): no auth (this container binds `0.0.0.0:8090` -- fine on
`localhost`, don't publish that port past your own machine without adding
some), no CloudWatch Logs tailing, in-memory-only job history (a container
restart loses it, same as a bare-metal restart would).
