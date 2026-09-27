#!/usr/bin/env bash
# Runs once per container start, before the real CMD (uvicorn).
#
# The running container bind-mounts the colleague's own repo checkout over
# /repo (docker-compose.yml) -- deliberately, so paths its-a-live computes
# from __file__ (loop-dee-loop's directory, its-a-live/.local-loop-package/
# <channel>, ...) are also valid paths on the *host*, which is required
# because local-docker/ecs-express hand those paths to `docker run`/`docker
# build`/cdk's DockerImageAsset -- all executed against the HOST daemon via
# the mounted socket, which has no idea this container's mount namespace
# exists and resolves every path it's given against its own (the host's)
# filesystem. See DOCKER_LOCAL.md for the full explanation.
#
# That bind mount shadows /repo/.venv and /repo/its-a-live/.venv, which the
# image built at /opt/venv-root and /opt/venv-its-a-live specifically to
# survive this. Re-link them on every start so paths.py's existing
# venv-detection logic (its_a_live_python(), franken_ts_python()) keeps
# working unmodified.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/repo}"

link_venv() {
    local target="$1" real="$2"
    if [ -L "$target" ]; then
        ln -sfn "$real" "$target"
    elif [ -e "$target" ]; then
        echo "docker-entrypoint: $target already exists and isn't a symlink" \
             "this image manages -- leaving it alone. If franken-ts/its-a-live" \
             "commands fail with import errors, remove it (it's probably a" \
             "venv from running this checkout outside Docker) and restart." >&2
    else
        ln -s "$real" "$target"
    fi
}

link_venv "${REPO_ROOT}/.venv" /opt/venv-root
link_venv "${REPO_ROOT}/its-a-live/.venv" /opt/venv-its-a-live

exec "$@"
