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
physically lives *inside igor's own container*.

Two ways to make those also valid, correct **host** paths were considered:

1. Make the container see its code at *literally the same absolute path*
   it lives at on your host (an earlier version of this doc/image did
   exactly that -- bind-mount the checkout at `${HOST_REPO_ROOT}:${HOST_REPO_ROOT}`,
   no remapping). It works, but it also means `uv` (which bakes the
   absolute path it was run from into each workspace member's editable-
   install metadata) has to be run at that same path at build time too --
   so the image is baked per-checkout-path, and isn't shareable between
   colleagues whose checkouts live at different paths.
2. **What this repo actually does**: keep the container's own view of the
   code at a fixed path (`/repo`, see `igor/Dockerfile`) regardless of
   where a colleague's checkout lives, and translate paths at the exact
   handful of call sites that hand one to a nested `docker build`/`docker
   run` -- `its-a-live/_host_paths.py`'s `to_host_path()`, used by
   `_local_docker_ops.py` (local-docker's own container) and
   `loop_stack.py`'s `DockerImageAsset` (`cdk deploy`'s image build/push
   for `ecs-express`). It reads `HOST_REPO_ROOT` (this checkout's real
   path, from `.env`) and `REPO_ROOT` (always `/repo`) and swaps one
   prefix for the other. Everywhere else -- franken-ts builds, boto3 calls,
   igor's own file browser -- never touches Docker, so never needs this;
   it's a no-op (both env vars simply unset) for a bare-metal/dev
   invocation of `channel.py`, which already sees real host paths directly.

This means the image itself is identical for every colleague and only
needs building once (still cheap to rebuild if it ever does change, since
Docker's layer cache makes a no-source-change rebuild near-instant) --
`HOST_REPO_ROOT` only matters at runtime, not at build time.

One thing that's still needed regardless of which approach above:
**the venvs built at image-build time need putting back at container
start.** The bind mount (source = your checkout, target = the fixed
`/repo`) shadows whatever `uv sync` produced under `/repo` at build time
(`.venv/`, `its-a-live/.venv/`) with your actual (venv-less, freshly
cloned) host checkout. Both venvs are built to `/opt/venv-root` /
`/opt/venv-its-a-live` instead (outside the mounted path) and
`igor/docker-entrypoint.sh` symlinks them back into place on every
container start, so `its-a-live/paths.py`'s existing venv-detection logic
needs no code change at all.

One thing that works for free, precisely *because* of the socket-sharing
approach above: `local-docker` channels publish their HLS/DASH ports
directly on your **host** (the host daemon creates and binds those sibling
containers), so `http://localhost:<port>/index.m3u8` from `channel.py
start`'s output is immediately reachable from your own browser -- no port
plumbing between igor's container and your host was needed for that case.

## What you get vs. what you don't

Included: franken-ts builds (ffmpeg/tsduck/GPAC all in-image), HTTP(S)
stream assets (an `.m3u8`/`.mpd` VOD manifest as a playlist `file`,
resolved via `yt-dlp` -- see `franken-ts/AGENTS.md`'s "assets" section),
all three its-a-live backends (`aws-media`/`ecs-express` need your own AWS
credentials -- see `.env.example` -- `local-docker` needs nothing but your
host Docker install), the asset file-browser/picker scoped to whatever you
set `MEDIA_ROOT` to. The downloaded-stream cache persists in a named
Docker volume (`franken-ts-cache`) across container restarts, so the same
manifest URL isn't re-fetched every time you recreate the container.

Not included, same as running igor any other way (`igor/AGENTS.md`'s
"Known gaps"): no auth (this container binds `0.0.0.0:8090` -- fine on
`localhost`, don't publish that port past your own machine without adding
some), no CloudWatch Logs tailing, in-memory-only job history (a container
restart loses it, same as a bare-metal restart would).
