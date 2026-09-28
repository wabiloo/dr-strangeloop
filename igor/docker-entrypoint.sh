#!/usr/bin/env bash
# Runs once per container start, before the real CMD (uvicorn).
#
# The running container bind-mounts each colleague's own repo checkout over
# this same fixed path, /repo (docker-compose.yml) -- deliberately, so the
# image itself stays identical/shareable across colleagues regardless of
# where their checkout actually lives on their own host. (A separate
# concern, handled elsewhere: local-docker/cdk's DockerImageAsset hand
# paths to `docker run`/`docker build`, executed against the HOST daemon
# via the mounted socket -- its-a-live/_host_paths.py translates this
# container's /repo-relative paths to their real host equivalent at those
# specific call sites. See DOCKER_LOCAL.md for the full explanation.)
#
# The bind mount above shadows /repo/.venv and /repo/its-a-live/.venv, which
# the image built at /opt/venv-root and /opt/venv-its-a-live specifically to
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
