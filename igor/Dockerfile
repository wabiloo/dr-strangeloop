# igor, packaged so a colleague can run the whole console -- UI, franken-ts
# builds, its-a-live deploys (any backend), loop-dee-loop local-docker runs --
# with nothing installed on their own machine except Docker itself.
#
# Build from the REPO ROOT, not this directory, since igor's uv workspace
# spans sibling projects (franken-ts, its-a-live, loop-dee-loop, ...):
#
#   docker compose build     # from repo root -- see ../docker-compose.yml
#   # or by hand:
#   docker build -f igor/Dockerfile -t dr-strangeloop-igor .
#
# See ../docker-compose.yml and ../DOCKER_LOCAL.md for why the running
# container bind-mounts the host checkout back over this same code (so
# paths its-a-live computes internally are also valid *host* paths --
# required because local-docker/ecs-express dispatch nested `docker
# build`/`docker run` calls through the host's own Docker socket) and how
# that interacts with the venvs built below.
#
# REPO_ROOT (build arg, must equal the host checkout's own absolute path --
# docker-compose.yml passes the same value it uses for the bind mount) is
# not just cosmetic: uv bakes the *absolute* path it was run from into each
# workspace member's editable-install metadata. Build with one path, mount a
# different one at runtime, and every `import franken_ts`/`import igor` etc.
# breaks even though the venv is symlinked back correctly (see
# docker-entrypoint.sh) -- the venv would be fine, its recorded source
# location would not. This is also why the image isn't shareable across
# colleagues whose checkouts live at different paths: each colleague builds
# their own (docker compose build), which is cheap since it's cached after
# the first run.

# ---------------------------------------------------------------------------
# Stage: gpac-build -- mirrors loop-dee-loop/Dockerfile's stage exactly
# (same flags, same "do not pass --disable-svg" landmine). Needed here
# because `its-a-live/channel.py spark` (ecs-express/local-docker) runs
# loop-dee-loop's bake.py in-process inside *this* container, not in a
# separate one -- see AGENTS.md's "GPAC is the same problem, smaller."
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS gpac-build

ARG GPAC_COMMIT=master

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
        build-essential pkg-config yasm nasm \
        zlib1g-dev libpng-dev libjpeg-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone https://github.com/gpac/gpac.git gpac
WORKDIR /src/gpac
RUN git checkout "${GPAC_COMMIT}"
RUN ./configure --disable-x11 \
    && make -j"$(nproc)" \
    && make install

RUN mkdir -p /out/lib /out/bin \
    && GPAC_BIN="$(command -v gpac)" && MP4BOX_BIN="$(command -v MP4Box)" \
    && cp "$GPAC_BIN" "$MP4BOX_BIN" /out/bin/ \
    && ldd "$GPAC_BIN" "$MP4BOX_BIN" \
        | awk '{print $3}' | grep '^/' | sort -u \
        | xargs -I{} cp -L {} /out/lib/

# ---------------------------------------------------------------------------
# Stage: runtime
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime

# Must match the bind-mount path docker-compose.yml uses at runtime -- see
# the note above. Default only matters for a bare `docker build` smoke test
# with no compose/mount involved.
ARG REPO_ROOT=/workspace/dr-strangeloop

# UNVERIFIED PLACEHOLDER -- confirm before your first build. This wasn't
# checked against a live download in the environment that wrote this
# Dockerfile (no network egress to github.com there). Go to
# https://github.com/tsduck/tsduck/releases, find the newest release still
# publishing a "debian12"/"bookworm" amd64 .deb asset, and pass its exact
# URL via --build-arg (or edit the default below). This base image is
# debian:bookworm-slim (Debian 12) specifically because GPAC above is built
# from source against it -- a "debian13" tsduck package is very likely
# linked against a newer glibc than bookworm ships and will fail at runtime
# ("version `GLIBC_2.NN' not found") rather than merely warn, so don't
# substitute one as a quick fix if debian12 assets stop being published;
# build tsduck from source instead (same pattern as the gpac-build stage
# above) if that ever happens.
ARG TSDUCK_DEB_URL=https://github.com/tsduck/tsduck/releases/download/v3.41-3977/tsduck_3.41-3977.debian12_amd64.deb

# docker.io gives us the `docker` CLI to talk to the *host* daemon via the
# mounted socket (docker-compose.yml) -- dockerd itself is never started,
# so its presence here costs disk, not attack surface.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip git \
        python3 ffmpeg \
        docker.io \
        nodejs npm \
    && curl -fsSL -o /tmp/tsduck.deb "${TSDUCK_DEB_URL}" \
    && apt-get install -y --no-install-recommends /tmp/tsduck.deb \
    && rm -f /tmp/tsduck.deb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gpac-build /out/bin/ /usr/local/bin/
COPY --from=gpac-build /out/lib/ /usr/local/lib/
RUN ldconfig

# aws CLI v2 (Debian only packages v1) -- required for its-a-live's
# aws-media/ecs-express backends (S3 sync, `cdk bootstrap`'s own checks).
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

# cdk CLI v2 -- needed at *runtime*, not just to build the frontend, since
# `channel.py create/redeploy/terminate` shells out to `cdk deploy`/`cdk
# destroy` for aws-media/ecs-express channels.
RUN npm install -g aws-cdk

# uv, for the repo-root workspace (franken-ts/igor/grave-robber/inspector-krogh/
# scte35-table23 all share one venv -- see repo AGENTS.md) and, separately,
# for its-a-live's own project.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# The running container bind-mounts the colleague's own checkout over this
# same path (docker-compose.yml) -- see DOCKER_LOCAL.md. Build-time copies
# below exist so `docker build` alone (no compose, no mount) also produces a
# working image for CI/smoke-testing.
WORKDIR ${REPO_ROOT}
COPY . .

# Build both venvs OUTSIDE $REPO_ROOT (NOT UV_PROJECT_ENVIRONMENT as a
# persistent ENV -- that would override --project-based venv discovery for
# BOTH `uv run` invocations below, defeating the deliberate root/its-a-live
# split; it's only exported for the one `uv sync` command it belongs to).
# The running container's bind mount shadows everything under $REPO_ROOT
# (including whatever we `uv sync` to right here at build time) -- see
# docker-entrypoint.sh, which symlinks these back into place on every start
# so paths.py's existing venv-detection logic (looks for its-a-live/.venv,
# falls back to repo-root .venv) needs no code change.
RUN UV_PROJECT_ENVIRONMENT=/opt/venv-root uv sync --project "${REPO_ROOT}" --all-packages

RUN UV_PROJECT_ENVIRONMENT=/opt/venv-its-a-live uv sync --project "${REPO_ROOT}/its-a-live"

# Frontend: built once here; the container serves the static bundle, it
# never runs `vite dev` in this shape.
RUN cd igor/frontend && npm ci && npm run build

COPY igor/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV REPO_ROOT=${REPO_ROOT}
EXPOSE 8090
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["sh", "-c", "uv run --project \"$REPO_ROOT\" uvicorn igor.app.main:app --host 0.0.0.0 --port 8090"]
