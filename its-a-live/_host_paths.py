"""Translates an in-container path to the equivalent path on the Docker
*host*, for any path about to be handed to a `docker build`/`docker run`
invocation.

Why this exists: local-docker (_local_docker_ops.py) and ecs-express's CDK
stack (loop_stack.py's DockerImageAsset) both shell out to the `docker`
CLI. When channel.py itself is running inside a container that reaches
Docker via a *mounted host socket* rather than its own daemon (igor's
Docker-packaged shape -- see ../DOCKER_LOCAL.md), those commands are
actually talking to the HOST's daemon. That daemon resolves every path it's
given against its own (the host's) filesystem -- it has no notion of this
container's mount namespace. A path this process computes from its own
code's location (e.g. `os.path.dirname(__file__)`) is a *container* path,
and needs translating before it's valid input to such a command.

Outside that shape (bare-metal/dev, or any command that never touches
Docker, e.g. aws-media) HOST_REPO_ROOT is unset and this is a no-op --
every path is already a real host path with nothing to translate.
"""

import os


def to_host_path(path: str) -> str:
    """Translate a path under REPO_ROOT (this container's view of the repo)
    to the equivalent path under HOST_REPO_ROOT (the same repo's real path
    on the Docker host) -- see module docstring. Both env vars are set
    together by docker-compose.yml, so their absence/presence always agree
    in practice; a path outside REPO_ROOT (shouldn't happen for anything
    this module is used on) is returned unchanged rather than guessed at.
    """
    host_root = os.environ.get("HOST_REPO_ROOT")
    container_root = os.environ.get("REPO_ROOT")
    if not host_root or not container_root:
        return path

    abs_path = os.path.abspath(path)
    container_root = os.path.abspath(container_root)
    rel = os.path.relpath(abs_path, container_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return path
    return os.path.normpath(os.path.join(host_root, rel))
