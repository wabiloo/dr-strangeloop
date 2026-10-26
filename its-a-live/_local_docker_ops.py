"""local-docker backend implementation for channel.py's commands.

Runs `loop-dee-loop` entirely on the local machine via `docker run` --
no AWS resources at all (no S3, no CloudFormation/CDK stack, no ECS).
Franken-ts output is baked locally with the same Python-venv bake step
`ecs-express` uses (see `_ecs_express_ops.spark`), then served by a
locally-run container bind-mounting the baked loop package. Intended for
local dev/demo/testing of a channel before committing to any AWS spend.

Content delivery has two modes, chosen per channel by `[infrastructure.docker]
bundle_content` (default false):

  - volume (default): the shared `loop-dee-loop:local` image holds only
    code; the baked package is bind-mounted read-only at run time. Fast to
    update (re-`spark` + `refresh`), but the image alone is useless to
    anyone else -- the content lives on this machine.
  - bundle: the loop package is copied INTO a per-channel image
    (`its-a-live-<name>:baked`, see loop-dee-loop/Dockerfile.baked) that is
    then run with no mount at all. The image is fully self-contained and can
    be shared (`channel.py image --save ...` -> `docker load`), at the cost
    of a rebuild per content change and an image as large as the loop.

Because there is no CloudFormation stack, channel.py skips
`_cf_outputs()`/`cdk deploy` entirely for this backend -- state across
separate `channel.py` invocations is tracked by Docker itself via a
deterministic container name (`its-a-live-<channel-name>`), not by stack
outputs or a local state file.
"""

import datetime
import json
import os
import shutil
import socket
import subprocess
import sys

from _host_paths import to_host_path
from _infra_cfg import infra_table
from _paths_cfg import get_paths
from _markers_cfg import markers_bake_args, split_on_serve_args
from _packaging_cfg import dash_serve_args, dvr_window_seconds, packaging_bake_args
from _reachability import check_manifest_reachable
from _epoch_cfg import config_epoch_utc
from _query_cfg import query_serve_args

_LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")
_IMAGE_TAG = "loop-dee-loop:local"
_BAKED_DOCKERFILE = os.path.join(_LOOP_DEE_LOOP_DIR, "Dockerfile.baked")
_CONTAINER_PREFIX = "its-a-live-"


def _container_name(channel_name):
    return f"{_CONTAINER_PREFIX}{channel_name}"


def _require_docker():
    if not shutil.which("docker"):
        sys.exit("`docker` was not found on PATH -- the local-docker backend requires "
                  "a local Docker install.")


def _resolve_python_cmd():
    """Same resolution order as _ecs_express_ops: loop-dee-loop is a
    workspace member of the repo-root uv project (shares its .venv, not
    its own), so `uv run --project` resolves its deps without a manually
    created venv. Falls back to plain python3 if uv itself isn't on PATH."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", _LOOP_DEE_LOOP_DIR, "python"]
    return ["python3"]


def _baked_image_tag(channel_name):
    return f"{_CONTAINER_PREFIX}{channel_name}:baked"


def _bundle_content(cfg):
    """`[infrastructure.docker] bundle_content = true`: serve from a per-channel image with
    the loop package copied into it, instead of bind-mounting it."""
    return bool(infra_table(cfg, "docker").get("bundle_content", False))


def _image_export_dir():
    """Where `image --save` writes tarballs by default: <outputs_dir>/images
    (igor's download endpoint reads the same place)."""
    return os.path.join(get_paths().outputs_dir, "images")


def _exported_image_bytes(channel_name):
    path = os.path.join(_image_export_dir(), f"{channel_name}.tar")
    return os.path.getsize(path) if os.path.isfile(path) else None


def _local_output_dir(cfg, channel_name):
    return os.path.abspath(cfg.get("bake", {}).get(
        "local_output_dir", os.path.join(get_paths().local_package_dir, channel_name)
    ))


_AUTO_PORT_RANGE = range(8080, 8180)


def _port_is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _find_free_port():
    for candidate in _AUTO_PORT_RANGE:
        if _port_is_free(candidate):
            return candidate
    sys.exit(f"Could not find a free port in {_AUTO_PORT_RANGE.start}-{_AUTO_PORT_RANGE.stop - 1} "
              f"for local-docker auto port selection -- set infrastructure.docker.port explicitly in the config.")


def _running_port(name):
    """The --port value a running (or stopped-but-not-removed) container
    was actually launched with, read back from its own launch command --
    mirrors _running_epoch. Used so an "auto"-selected port stays stable
    across start/refresh/status calls instead of being re-picked (and
    potentially landing on a different free port) every time."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Config.Cmd}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        cmd = json.loads(result.stdout)
    except ValueError:
        return None
    if "--port" in cmd:
        idx = cmd.index("--port")
        if idx + 1 < len(cmd):
            try:
                return int(cmd[idx + 1])
            except ValueError:
                return None
    return None


def _resolve_port(cfg, name):
    """Explicit docker.port wins outright. In "auto" mode (the default),
    reuse whatever port an existing container for this channel (running
    or stopped, as long as it still exists) was last launched with, so
    repeated start/refresh/status calls never disagree about a channel's
    port. Only picks a brand new free port -- skipping any port already
    bound on this host, which naturally avoids other running channels'
    containers too -- when there's truly no prior container to recover
    one from. A recorded port is only reused if that container is actually
    running (so it legitimately owns the port) or the port is still free; a
    stale, non-running container whose old port has since been taken by
    something else is ignored and a new free port is picked."""
    raw = infra_table(cfg, "docker").get("port", "auto")
    if raw != "auto":
        return int(raw)
    running_port = _running_port(name)
    if running_port is not None and (
        _container_status(name) == "running" or _port_is_free(running_port)
    ):
        return running_port
    return _find_free_port()


def _continuous_timeline_args(cfg):
    """loop-dee-loop/SCOPE.md §12: default on -- `[timeline]
    continuous = false` opts back out to the honestly-signaled
    #EXT-X-DISCONTINUITY/Period-restart default."""
    if cfg.get("timeline", {}).get("continuous", True):
        return ["--continuous-timeline"]
    return []


def spark(cfg, session, channel_name, extra_args=None):
    """Bake the franken-ts input locally (GPAC, no AWS/Docker involved --
    identical bake step to ecs-express's spark), writing straight into the
    directory that `start` will bind-mount into the container. No S3
    push, no upload of any kind."""
    source_path = cfg.get("input", {}).get("source_path", "")
    if not source_path:
        sys.exit("input.source_path must be set in the config")

    source_path = os.path.abspath(source_path)
    local_output_dir = _local_output_dir(cfg, channel_name)
    os.makedirs(local_output_dir, exist_ok=True)

    python_cmd = _resolve_python_cmd()
    bake_script = os.path.join(_LOOP_DEE_LOOP_DIR, "bake.py")
    bake_args = python_cmd + [bake_script, source_path, "--output", local_output_dir,
                              *packaging_bake_args(cfg), *markers_bake_args(cfg)]
    # grave-robber/SCOPE.md §10: only meaningful when [input].source_path
    # points at a segment-list manifest (.json) rather than a franken-ts
    # .ts/rendition dir -- bake.py itself ignores this flag for the normal
    # input shape, so it's harmless to always pass through when set.
    if cfg.get("input", {}).get("allow_missing_segments", False):
        bake_args.append("--allow-missing-segments")

    print(f"==> Baking locally: {source_path} -> {local_output_dir}")
    print(f"    {' '.join(bake_args)}")
    result = subprocess.run(bake_args)
    if result.returncode != 0:
        sys.exit(f"bake.py failed (exit {result.returncode}) -- see output above.")

    print("Spark complete.")
    print(f"Loop package baked to: {local_output_dir}")
    print("Run `channel.py start` to (re)start the local container and serve it.")


def _image_id(tag):
    result = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Id}}", tag],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _build_image():
    """Always rebuild, never just build-if-missing: `docker build`'s own
    layer cache makes this a fast no-op whenever loop-dee-loop's source
    hasn't changed, and a real rebuild whenever it has -- so `start`
    (and `refresh`) never again silently keep serving a container built
    from stale source the way build-once-ever used to (that's exactly
    what happened when serve.py's DASH signaling was fixed earlier today
    -- the container kept running the OLD code across multiple
    stop/start cycles because the image was only built on its very first
    run). Returns the freshly built image's id, so callers can tell
    whether a currently-running container is already using it."""
    build_context = to_host_path(_LOOP_DEE_LOOP_DIR)
    print(f"==> Building {_IMAGE_TAG} from {build_context} ...")
    result = subprocess.run(["docker", "build", "-t", _IMAGE_TAG, build_context])
    if result.returncode != 0:
        sys.exit(f"docker build failed (exit {result.returncode}) -- see output above.")
    return _image_id(_IMAGE_TAG)


def _dir_size_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _require_package(local_output_dir):
    if not os.path.isdir(local_output_dir) or not os.listdir(local_output_dir):
        sys.exit(f"No baked loop package found at {local_output_dir} -- run "
                  f"`channel.py spark` first.")


def _print_bundle_warnings(package_bytes):
    print("")
    print("  NOTE -- bundling content into the image:")
    print(f"   * The image will contain the ENTIRE loop package (~{_human_size(package_bytes)}). "
          f"Anyone with the image can extract the video.")
    print("   * It is a snapshot: later `spark` runs do NOT change it. Rebuild "
          "(`start`/`refresh`/`image`) to ship new content.")
    print("   * Each rebuild leaves the previous image layers behind until you "
          "`docker image prune`.")
    print("   * Don't push it to a public registry if the content isn't yours to publish.")
    print("")


def _serve_args(cfg, channel_name, port, epoch):
    """The serve.py flags for this channel (everything after the package
    dir). Single source of truth for both the local `docker run` command and
    the defaults baked into a bundled image, so a new serve flag can't be
    added to one and forgotten in the other."""
    return [
        "--epoch-utc", epoch,
        "--channel-name", channel_name,
        "--port", str(port),
        "--dvr-window-seconds", dvr_window_seconds(cfg),
        *_continuous_timeline_args(cfg),
        *split_on_serve_args(cfg),
        *dash_serve_args(cfg),
        *query_serve_args(cfg),
    ]


def _build_baked_image(cfg, channel_name, local_output_dir, epoch=None):
    """Build the per-channel self-contained image (base image + loop
    package copied in). Returns (tag, image_id). The base image
    (`_IMAGE_TAG`) must already be built -- callers run `_build_image()`
    first. The channel's serve.py flags (epoch, DVR window, timeline,
    markers, ...) are baked in as the image's defaults so a recipient
    running it bare gets the same stream this channel serves; any serve.py
    flag given at `docker run` time still overrides them."""
    _require_package(local_output_dir)
    if not os.path.isfile(_BAKED_DOCKERFILE):
        sys.exit(f"{_BAKED_DOCKERFILE} not found -- cannot build a bundled image.")

    tag = _baked_image_tag(channel_name)
    _print_bundle_warnings(_dir_size_bytes(local_output_dir))
    # Defaults for whoever runs the image bare: the channel's own flags, on
    # the image's fixed container port (8080). Flags given at `docker run`
    # time override these (see docker-entrypoint.sh).
    serve_defaults = _serve_args(cfg, channel_name, 8080, epoch or config_epoch_utc(cfg))
    build_cmd = [
        "docker", "build",
        "-f", to_host_path(_BAKED_DOCKERFILE),
        "--build-arg", f"BASE_IMAGE={_IMAGE_TAG}",
        "--build-arg", f"SERVE_ARGS_JSON={json.dumps(serve_defaults)}",
        "-t", tag,
        to_host_path(local_output_dir),
    ]
    print(f"==> Building self-contained image {tag} from {local_output_dir} ...")
    result = subprocess.run(build_cmd)
    if result.returncode != 0:
        sys.exit(f"docker build (bundled image) failed (exit {result.returncode}) -- see output above.")
    return tag, _image_id(tag)


def _prepare_image(cfg, channel_name, local_output_dir, epoch=None):
    """Build whatever image `start`/`refresh` will run: the shared code-only
    image (volume mode) or the per-channel bundled image on top of it.
    Returns (tag, image_id, bundled)."""
    base_id = _build_image()
    if _bundle_content(cfg):
        tag, image_id = _build_baked_image(cfg, channel_name, local_output_dir, epoch)
        return tag, image_id, True
    return _IMAGE_TAG, base_id, False


def _run_container(cfg, name, port, epoch, image_tag, bundled, local_output_dir):
    """`docker run` the channel's container. Volume mode bind-mounts the
    loop package and runs serve.py on it directly; bundled mode runs the
    self-contained image with no mount (the entrypoint finds the package at
    /var/loop-package itself -- note the missing positional)."""
    run_args = ["docker", "run", "-d", "--name", name, "-p", f"{port}:{port}"]
    serve_args = ["serve.py"]
    if not bundled:
        run_args += ["-v", f"{to_host_path(local_output_dir)}:/var/loop-package:ro"]
        serve_args.append("/var/loop-package")
    run_args += [image_tag, *serve_args, *_serve_args(cfg, cfg.get("deploy", {}).get("name", "default"), port, epoch)]
    result = subprocess.run(run_args)
    if result.returncode != 0:
        sys.exit(f"docker run failed (exit {result.returncode}) -- see output above.")


def _running_image_id(name):
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.Image}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _running_epoch(name):
    """The --epoch-utc value a running container was actually launched
    with, read back from its own launch command -- used to preserve
    playback timing continuity across a rebuild-triggered recreate (see
    `start`), rather than silently resetting to the configured one."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Config.Cmd}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        cmd = json.loads(result.stdout)
    except ValueError:
        return None
    if "--epoch-utc" in cmd:
        idx = cmd.index("--epoch-utc")
        if idx + 1 < len(cmd):
            return cmd[idx + 1]
    return None


def _container_status(name):
    """Returns the container's docker state (e.g. "running", "exited"), or
    None if no container with this name exists."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _resolve_epoch_arg(raw):
    if raw == "now":
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        sys.exit(f"--epoch-utc value {raw!r} must be 'now' or an ISO8601 UTC "
                  f"timestamp like 2026-01-01T00:00:00Z")
    return raw


def _print_urls(port):
    print(f"\nHLS:  http://localhost:{port}/index.m3u8")
    print(f"DASH: http://localhost:{port}/index.mpd")


def start(cfg, session, outputs, extra_args=None):
    """Start (or recreate) the local container serving the baked loop
    package. Always rebuilds the loop-dee-loop image first (see
    _build_image -- cheap when source hasn't changed), so a plain `start`
    can never again leave a channel silently serving stale code.

    If a container is already running the freshly-built image and no
    explicit epoch was requested, it's left alone untouched (unchanged
    behavior). Otherwise it's recreated -- if that recreate is happening
    ONLY because the image changed underneath it (not because of an
    explicit --epoch-utc), the container's own currently-running epoch is
    preserved rather than reset to the configured `[timeline] epoch`, so a rebuild-triggered
    restart never visibly jumps the stream's playback position. Pass
    `--epoch-utc now|<ISO8601>` to reset it explicitly."""
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    local_output_dir = _local_output_dir(cfg, channel_name)
    port = _resolve_port(cfg, name)

    _require_package(local_output_dir)

    epoch_arg = None
    extra_args = extra_args or []
    if extra_args:
        if extra_args[0] != "--epoch-utc" or len(extra_args) < 2:
            sys.exit("Usage: channel.py start [--epoch-utc now|<ISO8601 UTC timestamp>]")
        epoch_arg = _resolve_epoch_arg(extra_args[1])

    # In bundle mode this also rebuilds the per-channel image from the
    # current package on disk, so a new image id (and hence a recreate)
    # falls out naturally whenever either the code or the content changed.
    image_tag, image_id, bundled = _prepare_image(cfg, channel_name, local_output_dir, epoch_arg)

    status = _container_status(name)
    if status == "running" and epoch_arg is None and _running_image_id(name) == image_id:
        print(f"Container {name} is already running the current image.")
        _print_urls(port)
        return

    preserved_epoch = _running_epoch(name) if status == "running" and epoch_arg is None else None

    if status is not None:
        print(f"Removing existing container {name} (status={status}) ...")
        subprocess.run(["docker", "rm", "-f", name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    epoch = epoch_arg or preserved_epoch or config_epoch_utc(cfg)
    print(f"==> Starting container {name} (port {port}, epoch {epoch}, "
          f"content: {'bundled in image ' + image_tag if bundled else 'bind-mounted volume'}) ...")
    _run_container(cfg, name, port, epoch, image_tag, bundled, local_output_dir)

    print("Container started.")
    _print_urls(port)


def stop(cfg, session, outputs):
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    status = _container_status(name)
    if status is None:
        print(f"No container named {name} -- already stopped.")
        return
    print(f"Stopping and removing container {name} ...")
    subprocess.run(["docker", "rm", "-f", name])
    print("Stopped.")


def refresh(cfg, session, outputs):
    """Recreate the container so it picks up whatever was last `spark`ed
    into the bind-mounted loop package directory. The container is
    recreated with the configured `[timeline] epoch` (default
    2026-01-01T00:00:00Z) -- this is also how a changed epoch is applied
    (`redeploy` runs this). Pass `channel.py start --epoch-utc ...`
    directly for a one-off epoch."""
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    port = _resolve_port(cfg, name)
    local_output_dir = _local_output_dir(cfg, channel_name)

    _require_package(local_output_dir)

    was_running = _container_status(name) == "running"
    print(f"Recreating container {name} to pick up latest spark ...")
    subprocess.run(["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    image_tag, _, bundled = _prepare_image(cfg, channel_name, local_output_dir)

    _run_container(cfg, name, port, config_epoch_utc(cfg), image_tag, bundled, local_output_dir)

    if not was_running:
        print("Note: container was not previously running -- started fresh.")
    print("Refreshed.")
    _print_urls(port)


def image(cfg, session, channel_name, extra_args=None):
    """Build the self-contained, shareable image for this channel
    (`its-a-live-<name>:baked`: loop-dee-loop code + the baked loop package)
    and optionally export it as a tarball another machine can `docker load`.
    Independent of `[infrastructure.docker] bundle_content` -- that setting only decides
    what `start`/`refresh` run locally; this always produces the bundle.

      channel.py image [--epoch-utc now|<ISO8601>] [--save [PATH]]

    --epoch-utc sets the default epoch baked into the image (default: the
    configured `[timeline] epoch`). --save writes `docker save` output to PATH
    (default: <outputs_dir>/images/<name>.tar)."""
    _require_docker()
    local_output_dir = _local_output_dir(cfg, channel_name)
    _require_package(local_output_dir)

    epoch = None
    save_path = None
    args = list(extra_args or [])
    i = 0
    while i < len(args):
        if args[i] == "--epoch-utc" and i + 1 < len(args):
            epoch = _resolve_epoch_arg(args[i + 1])
            i += 2
        elif args[i] == "--save":
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                save_path = os.path.abspath(args[i + 1])
                i += 2
            else:
                save_path = os.path.join(_image_export_dir(), f"{channel_name}.tar")
                i += 1
        else:
            sys.exit("Usage: channel.py image [--epoch-utc now|<ISO8601 UTC timestamp>] [--save [PATH]]")

    _build_image()
    tag, image_id = _build_baked_image(cfg, channel_name, local_output_dir, epoch)

    size = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Size}}", tag],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ).stdout.strip()
    print(f"Built {tag}" + (f" ({_human_size(int(size))})" if size.isdigit() else "") + ".")

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        print(f"==> Exporting {tag} -> {save_path} ...")
        result = subprocess.run(["docker", "save", "-o", save_path, tag])
        if result.returncode != 0:
            sys.exit(f"docker save failed (exit {result.returncode}) -- see output above.")
        print(f"Exported {_human_size(os.path.getsize(save_path))} to {save_path}")
        print(f"IMAGE_FILE={save_path}")
        print("\nOn the receiving machine:")
        print(f"  docker load -i {os.path.basename(save_path)}")
    print("\nRun it anywhere Docker is available -- no S3, AWS credentials or volume needed:")
    print(f"  docker run -p 8080:8080 {tag}")
    print("  HLS:  http://localhost:8080/index.m3u8")
    print("  DASH: http://localhost:8080/index.mpd")


def status(cfg, session, outputs):
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    docker_status = _container_status(name)
    # Unlike start/refresh, never picks a brand new port here -- status is
    # read-only and shouldn't claim a free port that nothing is bound to.
    # In "auto" mode with no container ever created for this channel yet,
    # there simply isn't a port to report.
    raw_port = infra_table(cfg, "docker").get("port", "auto")
    port = int(raw_port) if raw_port != "auto" else _running_port(name)
    hls_url = f"http://localhost:{port}/index.m3u8" if docker_status == "running" and port else None
    dash_url = f"http://localhost:{port}/index.mpd" if docker_status == "running" and port else None
    result = {
        "backend": "local-docker",
        "container_name": name,
        "bundle_content": _bundle_content(cfg),
        "image": _baked_image_tag(channel_name) if _bundle_content(cfg) else _IMAGE_TAG,
        # Size of the tarball `channel.py image --save` last exported for
        # this channel (None if there isn't one) -- igor offers it for
        # download.
        "exported_image_bytes": _exported_image_bytes(channel_name),
        "status": docker_status or "not created",
        "port": port,
        "hls_url": hls_url,
        "dash_url": dash_url,
        # Only worth network-checking once the container itself claims to
        # be running -- see _reachability.py.
        "reachable": check_manifest_reachable(hls_url, dash_url) if docker_status == "running" else None,
    }
    print(f"Container {name}: status={result['status']}"
          + (f", port={port}" if port else ", port=not yet assigned")
          + (f", manifest {'reachable' if result['reachable'] else 'UNREACHABLE'}" if result["reachable"] is not None else ""))
    return result
