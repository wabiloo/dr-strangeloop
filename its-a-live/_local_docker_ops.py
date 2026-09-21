"""local-docker backend implementation for channel.py's commands.

Runs `loop-dee-loop` entirely on the local machine via `docker run` --
no AWS resources at all (no S3, no CloudFormation/CDK stack, no ECS).
Franken-ts output is baked locally with the same Python-venv bake step
`ecs-express` uses (see `_ecs_express_ops.spark`), then served by a
locally-run container bind-mounting the baked loop package. Intended for
local dev/demo/testing of a channel before committing to any AWS spend.

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
import subprocess
import sys

_LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")
_IMAGE_TAG = "loop-dee-loop:local"
_CONTAINER_PREFIX = "its-a-live-"
_DEFAULT_EPOCH = "1970-01-01T00:00:00Z"


def _container_name(channel_name):
    return f"{_CONTAINER_PREFIX}{channel_name}"


def _require_docker():
    if not shutil.which("docker"):
        sys.exit("`docker` was not found on PATH -- the local-docker backend requires "
                  "a local Docker install.")


def _resolve_python_cmd():
    """Same resolution order as _ecs_express_ops -- prefer a local .venv
    next to loop-dee-loop, then `uv run`, then plain python3."""
    venv_python = os.path.join(_LOOP_DEE_LOOP_DIR, ".venv", "bin", "python")
    if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
        return [venv_python]
    if shutil.which("uv"):
        return ["uv", "run", "--project", _LOOP_DEE_LOOP_DIR, "python"]
    return ["python3"]


def _local_output_dir(cfg, channel_name):
    return os.path.abspath(cfg.get("bake", {}).get(
        "local_output_dir", os.path.join(os.path.dirname(__file__), ".local-loop-package", channel_name)
    ))


def _port(cfg):
    return int(cfg.get("docker", {}).get("port", 8080))


def _dvr_window_seconds(cfg):
    return str(cfg.get("packaging", {}).get("dvr_window_seconds", 30))


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
    segment_duration = str(cfg.get("packaging", {}).get("segment_duration", 4.0))
    markers_cfg = cfg.get("markers", {})
    daterange_mode = markers_cfg.get("daterange_mode", "shared")
    cue_tags = markers_cfg.get("cue_tags", "none")
    increment_event_ids = markers_cfg.get("increment_event_ids", False)
    os.makedirs(local_output_dir, exist_ok=True)

    python_cmd = _resolve_python_cmd()
    bake_script = os.path.join(_LOOP_DEE_LOOP_DIR, "bake.py")
    bake_args = python_cmd + [bake_script, source_path, "--output", local_output_dir,
                              "--segment-duration", segment_duration,
                              "--daterange-mode", daterange_mode,
                              "--cue-tags", cue_tags]
    if increment_event_ids:
        bake_args.append("--increment-event-ids")

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
    print(f"==> Building {_IMAGE_TAG} from {_LOOP_DEE_LOOP_DIR} ...")
    result = subprocess.run(["docker", "build", "-t", _IMAGE_TAG, _LOOP_DEE_LOOP_DIR])
    if result.returncode != 0:
        sys.exit(f"docker build failed (exit {result.returncode}) -- see output above.")
    return _image_id(_IMAGE_TAG)


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
    `start`), rather than silently resetting to _DEFAULT_EPOCH."""
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
    print(f"\nHLS:  http://localhost:{port}/master.m3u8")
    print(f"DASH: http://localhost:{port}/manifest.mpd")


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
    preserved rather than reset to _DEFAULT_EPOCH, so a rebuild-triggered
    restart never visibly jumps the stream's playback position. Pass
    `--epoch-utc now|<ISO8601>` to reset it explicitly."""
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    local_output_dir = _local_output_dir(cfg, channel_name)
    port = _port(cfg)

    if not os.path.isdir(local_output_dir) or not os.listdir(local_output_dir):
        sys.exit(f"No baked loop package found at {local_output_dir} -- run "
                  f"`channel.py spark` first.")

    epoch_arg = None
    extra_args = extra_args or []
    if extra_args:
        if extra_args[0] != "--epoch-utc" or len(extra_args) < 2:
            sys.exit("Usage: channel.py start [--epoch-utc now|<ISO8601 UTC timestamp>]")
        epoch_arg = _resolve_epoch_arg(extra_args[1])

    image_id = _build_image()

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

    epoch = epoch_arg or preserved_epoch or _DEFAULT_EPOCH
    run_args = [
        "docker", "run", "-d", "--name", name,
        "-p", f"{port}:{port}",
        "-v", f"{local_output_dir}:/var/loop-package:ro",
        _IMAGE_TAG,
        "serve.py", "/var/loop-package",
        "--epoch-utc", epoch,
        "--port", str(port),
        "--dvr-window-seconds", _dvr_window_seconds(cfg),
    ]
    print(f"==> Starting container {name} (port {port}, epoch {epoch}) ...")
    result = subprocess.run(run_args)
    if result.returncode != 0:
        sys.exit(f"docker run failed (exit {result.returncode}) -- see output above.")

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
    into the bind-mounted loop package directory. Epoch is left as-is
    (whatever the container was last started with is not tracked here --
    pass `channel.py start --epoch-utc ...` directly if you need to reset
    it)."""
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    port = _port(cfg)
    local_output_dir = _local_output_dir(cfg, channel_name)

    if not os.path.isdir(local_output_dir) or not os.listdir(local_output_dir):
        sys.exit(f"No baked loop package found at {local_output_dir} -- run "
                  f"`channel.py spark` first.")

    was_running = _container_status(name) == "running"
    print(f"Recreating container {name} to pick up latest spark ...")
    subprocess.run(["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _build_image()

    run_args = [
        "docker", "run", "-d", "--name", name,
        "-p", f"{port}:{port}",
        "-v", f"{local_output_dir}:/var/loop-package:ro",
        _IMAGE_TAG,
        "serve.py", "/var/loop-package",
        "--epoch-utc", _DEFAULT_EPOCH,
        "--port", str(port),
        "--dvr-window-seconds", _dvr_window_seconds(cfg),
    ]
    result = subprocess.run(run_args)
    if result.returncode != 0:
        sys.exit(f"docker run failed (exit {result.returncode}) -- see output above.")

    if not was_running:
        print("Note: container was not previously running -- started fresh.")
    print("Refreshed.")
    _print_urls(port)


def status(cfg, session, outputs):
    _require_docker()
    channel_name = cfg.get("deploy", {}).get("name", "default")
    name = _container_name(channel_name)
    port = _port(cfg)
    docker_status = _container_status(name)
    result = {
        "backend": "local-docker",
        "container_name": name,
        "status": docker_status or "not created",
        "hls_url": f"http://localhost:{port}/master.m3u8" if docker_status == "running" else None,
        "dash_url": f"http://localhost:{port}/manifest.mpd" if docker_status == "running" else None,
    }
    print(f"Container {name}: status={result['status']}")
    return result
