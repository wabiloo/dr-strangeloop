#!/usr/bin/env python3
"""
Helper script for managing an its-a-live channel (either backend).

Usage:
  python channel.py [--config path/to/config.toml] <command> [args...]

Commands (identical across both backends -- [deploy].backend in the
config file selects "aws-media" (MediaLive + MediaPackage v1) or
"ecs-express" (loop-dee-loop on ECS Express Mode + CloudFront)):

  spark     Stage the franken-ts input for this channel's backend:
              ecs-express -- bake it locally (GPAC, no AWS compute) and
                              push the loop package to S3
              aws-media   -- upload the raw .ts to S3 as-is (MediaLive
                              re-encodes it live, no transformation)
  start     Start the channel:
              ecs-express -- scale to 1 task. Epoch is left untouched by
                              default (fast, no redeploy); pass
                              `--epoch-utc now|<ISO8601>` to explicitly
                              (re)set it (forces a real redeploy)
              aws-media   -- start the MediaLive channel, wait RUNNING
  stop      Stop the channel:
              ecs-express -- scale to 0 tasks (Fargate compute cost stops;
                              the shared ALB keeps running for other
                              channels)
              aws-media   -- stop the MediaLive channel, wait IDLE
  refresh   Pick up newly `spark`ed content on an ALREADY-running channel:
              ecs-express -- force a new task launch (re-syncs S3)
              aws-media   -- no hot-reload exists; this is a full
                              stop -> start cycle (real interruption)
  update    `spark` then `refresh` -- the routine "ship new content to a
            running channel" combo (see README.md's "Updating content on
            a running channel"). `spark` and `refresh` remain available
            separately for finer-grained control.
  status    Print the channel's current status
  outputs   Print all CloudFormation stack outputs
  redeploy  Delete a broken stack if needed, then run cdk deploy
  terminate Tear the channel down for good: `cdk destroy` the stack.
            Not available for local-docker (no stack) -- `stop` alone is
            sufficient teardown there. Content staged in S3 is NOT
            deleted (the stack doesn't own the bucket) -- see README.md.
  create    First-time, non-interactive setup: (ecs-express only) ensure
            the shared ECS cluster stack is deployed, `spark` (content
            MUST be staged before the channel stack exists), `cdk deploy`
            the channel stack, then `start`. Equivalent to
            galvanise.py's pipeline minus the interactive confirmations --
            intended for programmatic callers (e.g. a management UI).
  schedule  Manage scheduled start/stop windows for this channel
            (aws-media/ecs-express only -- local-docker has no AWS
            presence to schedule against). Each window is an on-air
            period with an optional start (omitted = starts immediately;
            this command then also runs `start`) and an optional end
            (omitted = runs until a manual `stop`). Windows may not
            overlap. Backed by one-time EventBridge Scheduler schedules
            targeting a shared Lambda -- see AGENTS.md and
            _scheduler_ops.py. Requires `cdk deploy -c scheduler=true
            ItsALiveSharedStack-scheduler` once per account/region.
              schedule add [--start ISO8601] [--end ISO8601]
              schedule remove <window-id>
              schedule list
  list      List channels found under a directory of TOML configs
            (default: the directory containing --config), each with its
            CloudFormation stack status if deployed, plus a live
            running/stopped signal (min_tasks/max_tasks for ecs-express,
            live_status for aws-media) once the stack has settled --
            stack_status alone can't tell "deployed" apart from "deployed
            but scaled to 0 / IDLE".

Global flag --json (before or after the command) makes `status`,
`outputs`, and `list` print a single JSON document instead of
human-readable text -- for programmatic callers.

The --config flag (short: -c) selects a config file; defaults to
config.toml in the same directory as this script. The stack name is
derived from [deploy].name and [deploy].backend in the config file, so
each config file targets an independent channel deployment.
"""

import glob
import json
import os
import sys
import time
import tomllib
import boto3

from _infra_cfg import infra_table, reject_legacy_tables
from _paths_cfg import resolve_source_path

_DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config.toml")

_BACKENDS = ("aws-media", "ecs-express", "local-docker")

# Backends with no CloudFormation/CDK stack at all -- channel.py must never
# call _cf_outputs() or `cdk deploy`/`cdk destroy` for these.
_NO_STACK_BACKENDS = ("local-docker",)


def _parse_args():
    """Returns (config_path, command, extra_args, as_json) -- extra_args is
    whatever follows the command (e.g. `start --epoch-utc now`). --json may
    appear anywhere in the argument list."""
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    config_path = _DEFAULT_CONFIG
    if args and args[0] in ("--config", "-c"):
        if len(args) < 2:
            sys.exit("--config requires a path argument")
        config_path = args[1]
        args = args[2:]
    if not args:
        return config_path, None, [], as_json
    return config_path, args[0], args[1:], as_json


def _config(config_path):
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    backend = cfg.get("deploy", {}).get("backend")
    if backend not in _BACKENDS:
        sys.exit(f"[deploy].backend must be one of {_BACKENDS!r} in {config_path}, got {backend!r}")
    reject_legacy_tables(cfg, config_path)
    source = cfg.get("input", {})
    if source.get("source_path"):
        source["source_path"] = resolve_source_path(source["source_path"], config_path)
    return cfg


def _backend(cfg):
    return cfg["deploy"]["backend"]


def _channel_name(cfg):
    return cfg.get("deploy", {}).get("name", "default")


def _stack_name(cfg):
    return f"ItsALiveStack-{_channel_name(cfg)}-{_backend(cfg)}"


def _cdk_output(name):
    """`--output` args giving each channel (and the shared stack) its own
    cloud assembly directory. CDK write-locks its output dir for the whole of
    a `deploy`/`destroy`, so with the default shared `cdk.out`, two channels
    could never be deployed or torn down at the same time."""
    return ["--output", f"cdk.out-{name}"]


def _shared_stack_name(cfg):
    """Only meaningful for ecs-express (the shared ECS cluster stack)."""
    return "ItsALiveSharedStack-ecs-express"


def _session(cfg):
    region = infra_table(cfg, "aws").get("region")
    return boto3.session.Session(region_name=region)


def _cf_outputs(cfg, stack_name=None):
    stack_name = stack_name or _stack_name(cfg)
    cf = _session(cfg).client("cloudformation")
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def _ops(cfg):
    """Returns the backend-specific ops module for this config."""
    if _backend(cfg) == "ecs-express":
        import _ecs_express_ops
        return _ecs_express_ops
    if _backend(cfg) == "local-docker":
        import _local_docker_ops
        return _local_docker_ops
    import _aws_media_ops
    return _aws_media_ops


def _outputs_if_needed(cfg):
    """local-docker has no CloudFormation stack -- skip _cf_outputs entirely
    for it and pass None down to the ops module instead."""
    if _backend(cfg) in _NO_STACK_BACKENDS:
        return None
    return _cf_outputs(cfg)


def cmd_spark(cfg, extra_args):
    _ops(cfg).spark(cfg, _session(cfg), _channel_name(cfg), extra_args)


def cmd_start(cfg, extra_args):
    outputs = _outputs_if_needed(cfg)
    _ops(cfg).start(cfg, _session(cfg), outputs, extra_args)


def cmd_stop(cfg, extra_args):
    if extra_args:
        sys.exit("`stop` does not take extra arguments")
    outputs = _outputs_if_needed(cfg)
    _ops(cfg).stop(cfg, _session(cfg), outputs)


def cmd_refresh(cfg, extra_args):
    if extra_args:
        sys.exit("`refresh` does not take extra arguments")
    outputs = _outputs_if_needed(cfg)
    _ops(cfg).refresh(cfg, _session(cfg), outputs)


def cmd_update(cfg, extra_args):
    """`spark` then `refresh`: the routine way to ship new content to an
    already-running channel in one step. Requires the channel to already
    be running -- same precondition as `refresh` alone (see its
    docstring/README.md's "Updating content on a running channel")."""
    if extra_args:
        sys.exit("`update` does not take extra arguments")
    cmd_spark(cfg, [])
    cmd_refresh(cfg, [])


def cmd_status(cfg, extra_args, as_json=False):
    if extra_args:
        sys.exit("`status` does not take extra arguments")
    backend = _backend(cfg)

    # For stacked backends, check CloudFormation's own StackStatus first --
    # same reasoning as cmd_list's _stack_status_is_settled gate. While the
    # stack is mid create/update/delete (or gone/broken), the backend's own
    # live status() call (MediaLive DescribeChannel, ECS DescribeServices,
    # ...) would either read stale/partial Outputs or 404 outright (e.g.
    # MediaLive's channel resource can already be gone mid DELETE_IN_PROGRESS
    # while other stack resources are still being torn down) -- letting that
    # exception propagate used to surface as an opaque 502 to igor's UI
    # instead of the "Dismantling"/"Dismantled" phase the stack status
    # itself already unambiguously answers.
    stack_status = None
    if backend not in _NO_STACK_BACKENDS:
        cf = _session(cfg).client("cloudformation")
        stack_status = _stack_status(cf, _stack_name(cfg))
        if stack_status is None or not _stack_status_is_settled(stack_status):
            result = {
                "backend": backend,
                # Surface the CFN status itself as the human-readable raw
                # status text (igor shows this verbatim) when there's no
                # settled backend-specific status to report.
                "status": stack_status or "DELETE_COMPLETE",
                "stack_status": stack_status,
                "reachable": None,
            }
            if as_json:
                print(json.dumps(result))
            else:
                print(f"Stack status: {result['status']}")
            return

    outputs = _outputs_if_needed(cfg)
    result = _ops(cfg).status(cfg, _session(cfg), outputs)
    result["stack_status"] = stack_status
    if as_json:
        print(json.dumps(result))


def cmd_outputs(cfg, extra_args, as_json=False):
    if extra_args:
        sys.exit("`outputs` does not take extra arguments")
    if _backend(cfg) in _NO_STACK_BACKENDS:
        sys.exit("`outputs` has no meaning for local-docker (no CloudFormation stack) "
                 "-- use `status` instead.")
    outputs = _cf_outputs(cfg)
    if as_json:
        print(json.dumps(outputs))
        return
    max_key = max(len(k) for k in outputs) if outputs else 0
    for k, v in sorted(outputs.items()):
        print(f"  {k:<{max_key}}  {v}")


def _stack_status(cf, stack_name):
    try:
        resp = cf.describe_stacks(StackName=stack_name)
        return resp["Stacks"][0]["StackStatus"]
    except cf.exceptions.ClientError:
        return None


def _stack_status_is_settled(status):
    """True for a stack_status where describe_stacks's Outputs (needed for
    a live status() call) are actually populated and trustworthy: not
    mid-transition (*_IN_PROGRESS), not broken (*_FAILED, *ROLLBACK*), and
    not gone (DELETE_COMPLETE)."""
    return not any(bad in status for bad in ("IN_PROGRESS", "FAILED", "ROLLBACK")) and status != "DELETE_COMPLETE"


def _list_entry(path):
    """One channel's row for `list` (None if the config is invalid). Does the
    real work -- CloudFormation/Docker/ECS/MediaLive calls plus the manifest
    reachability check -- so it is the slow part of `list`."""
    try:
        cfg = _config(path)
    except SystemExit:
        return None
    name = _channel_name(cfg)
    backend = _backend(cfg)
    if backend in _NO_STACK_BACKENDS:
        result = _ops(cfg).status(cfg, _session(cfg), None)
        return {
            "config_path": path,
            "name": name,
            "backend": backend,
            "stack_name": None,
            # No CloudFormation stack for this backend -- the nearest
            # equivalent identifier is the deterministic local Docker
            # container name (see _local_docker_ops.status()).
            "container_name": result.get("container_name"),
            "stack_status": result.get("status"),
            "reachable": result.get("reachable"),
        }
    stack_name = _stack_name(cfg)
    cf = _session(cfg).client("cloudformation")
    status_value = _stack_status(cf, stack_name)
    entry = {
        "config_path": path,
        "name": name,
        "backend": backend,
        "stack_name": stack_name,
        "container_name": None,
        "stack_status": status_value,
    }
    # A healthy stack_status only means the stack is deployed -- it says
    # nothing about whether the service behind it is actually serving
    # (ecs-express scaled to 0, aws-media IDLE) or not, unlike
    # local-docker above (whose Docker status already IS the live
    # signal). Fetch the live status too (including the manifest
    # reachability check above), so the list -- like local-docker's --
    # reflects the real running/stopped/unreachable state, not just
    # "stack exists". Best-effort: swallow failures (e.g. a missing
    # output on a freshly-created stack) and fall back to stack_status
    # alone.
    if status_value and _stack_status_is_settled(status_value):
        try:
            outputs = _cf_outputs(cfg, stack_name)
            live = _ops(cfg).status(cfg, _session(cfg), outputs)
            entry["live_status"] = live.get("status")
            entry["reachable"] = live.get("reachable")
            if backend == "ecs-express":
                entry["min_tasks"] = live.get("min_tasks")
                entry["max_tasks"] = live.get("max_tasks")
        except Exception:
            pass
    return entry


def cmd_list(config_path, extra_args, as_json=False):
    """List channels found from TOML configs in the same directory as
    --config (or the directory given as the sole extra arg), each paired
    with its CloudFormation stack status if one exists yet (None if the
    channel has never been deployed). local-docker channels have no
    stack; "stack_status" instead reflects the local container's Docker
    status, and "container_name" (None for the other two backends) is
    the deterministic `its-a-live-<name>` container name in place of a
    stack name.

    Each settled/running channel also gets a "reachable" field (see
    _reachability.py) -- a network check of whether HlsPlaybackUrl/
    DashPlaybackUrl actually return a manifest, not just whether the
    backend's own infrastructure claims to be up. This adds real latency
    per running channel (a several-second timeout if the check fails), on
    top of the one CloudFormation/ECS/MediaLive API call already made for
    each one."""
    if len(extra_args) > 1:
        sys.exit("Usage: channel.py list [directory | channel.toml]")
    target = extra_args[0] if extra_args else os.path.dirname(os.path.abspath(config_path))
    # A single .toml file lists just that channel (igor fetches rows one at a
    # time so its table can fill in as each channel's live state resolves).
    if os.path.isfile(target):
        directory, paths = os.path.dirname(target), [target]
    else:
        directory, paths = target, sorted(glob.glob(os.path.join(target, "*.toml")))

    channels = [entry for entry in (_list_entry(p) for p in paths) if entry is not None]

    if as_json:
        print(json.dumps(channels))
        return
    if not channels:
        print(f"No *.toml configs found in {directory}")
        return
    for c in channels:
        live = ""
        if c["backend"] == "ecs-express" and c.get("min_tasks") is not None:
            live = f" tasks={c['min_tasks']}/{c['max_tasks']}"
        elif c.get("live_status"):
            live = f" live={c['live_status']}"
        if c.get("reachable") is not None:
            live += f" manifest={'reachable' if c['reachable'] else 'UNREACHABLE'}"
        print(f"  {c['name']:<20} backend={c['backend']:<12} "
              f"stack_status={c['stack_status'] or 'not deployed':<20}{live} {c['config_path']}")


def _ensure_shared_stack_if_needed(cfg, config_path):
    """Deploy the shared ECS cluster stack (ecs-express only) unless it already
    exists and is healthy. It is channel-independent and rarely changes, and a
    no-op `cdk deploy` still pays a full synth + asset publish. To roll out a
    change to loop_shared_stack.py, run `cdk deploy ItsALiveSharedStack-ecs-express`
    by hand."""
    import subprocess

    if _backend(cfg) != "ecs-express":
        return
    shared_stack_name = _shared_stack_name(cfg)
    status = _stack_status(_session(cfg).client("cloudformation"), shared_stack_name)
    if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        print(f"{shared_stack_name} already deployed ({status}) -- skipping.")
        return
    print(f"Ensuring {shared_stack_name} is deployed ...")
    result = subprocess.run(
        ["cdk", "deploy", "--require-approval", "never", *_cdk_output("_shared"),
         "-c", f"config={config_path}", shared_stack_name],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)


def cmd_create(cfg, config_path, extra_args):
    """Non-interactive first-time channel setup: ensure the shared stack
    (ecs-express only), `spark` (stage content), `cdk deploy` the channel
    stack, then `start`. This is `galvanise.py`'s pipeline (steps 3-6)
    with every interactive confirmation removed -- intended for
    programmatic callers (a management API/UI) that already know they
    want to proceed, as opposed to a human running galvanise.py's guided
    terminal flow.

    Content MUST be staged before the channel stack is deployed: on
    ecs-express, the ECS task's entrypoint syncs the loop package from S3
    at container startup and hard-crashes if nothing is there yet, which
    sends the ECS service into an endless crashloop that CloudFormation
    waits on (and eventually times out/rolls back) -- see
    its-a-live/AGENTS.md and README.md.

    For local-docker (no CloudFormation stack at all), this reduces to
    `spark` + `start`."""
    if extra_args:
        sys.exit("`create` does not take extra arguments")

    import subprocess

    if _backend(cfg) in _NO_STACK_BACKENDS:
        print("Sparking (staging content) ...")
        cmd_spark(cfg, [])
        print("Starting the channel ...")
        cmd_start(cfg, [])
        return

    _ensure_shared_stack_if_needed(cfg, config_path)

    print("Sparking (staging content) ...")
    cmd_spark(cfg, [])

    stack_name = _stack_name(cfg)
    print(f"Deploying {stack_name} ...")
    result = subprocess.run(
        ["cdk", "deploy", "--require-approval", "never", *_cdk_output(_channel_name(cfg)),
         "-c", f"config={config_path}", stack_name],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)

    print("Starting the channel ...")
    cmd_start(cfg, [])


def cmd_redeploy(cfg, config_path, extra_args):
    if extra_args:
        sys.exit("`redeploy` does not take extra arguments")

    import subprocess

    if _backend(cfg) in _NO_STACK_BACKENDS:
        # No CloudFormation stack to fix up -- just recreate the local
        # container from whatever was last spark'ed.
        print("local-docker has no CloudFormation stack -- recreating the "
              "local container instead of running `cdk deploy` ...")
        cmd_refresh(cfg, [])
        return

    stack_name = _stack_name(cfg)
    cf = _session(cfg).client("cloudformation")

    _ensure_shared_stack_if_needed(cfg, config_path)

    WAIT_STATES = {
        "DELETE_IN_PROGRESS",
        "ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    }
    DELETE_BEFORE_DEPLOY = {
        "CREATE_IN_PROGRESS",
        "ROLLBACK_COMPLETE",
        "CREATE_FAILED",
        "ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_FAILED",
    }

    try:
        resp = cf.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]
    except cf.exceptions.ClientError:
        status = None

    if status in WAIT_STATES:
        print(f"Stack is {status} -- waiting for it to settle ...")
        while status in WAIT_STATES:
            time.sleep(10)
            resp = cf.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
            print(f"  status: {status}", flush=True)

    if status in DELETE_BEFORE_DEPLOY:
        print(f"Stack is in {status} -- deleting before redeployment ...")
        cf.delete_stack(StackName=stack_name)
        waiter = cf.get_waiter("stack_delete_complete")
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        print("Stack deleted.")
    elif status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        print(f"Stack is already {status} -- redeploying normally.")
    elif status is not None:
        print(f"Stack status: {status}")

    # Explicit stack name: for ecs-express the app also contains
    # ItsALiveSharedStack-ecs-express, so a bare `cdk deploy` with no stack
    # argument would be ambiguous and CDK would refuse it.
    cdk_cmd = ["cdk", "deploy", "--require-approval", "never", *_cdk_output(_channel_name(cfg)),
               "-c", f"config={config_path}", stack_name]
    print(f"Running: {' '.join(cdk_cmd)}")
    result = subprocess.run(cdk_cmd, check=False)
    sys.exit(result.returncode)


def cmd_schedule(cfg, config_path, extra_args, as_json=False):
    if _backend(cfg) == "local-docker":
        sys.exit("Scheduling requires an AWS backend (aws-media or ecs-express) -- "
                  "local-docker channels have no AWS presence to schedule against.")
    if not extra_args:
        sys.exit("Usage: channel.py schedule <add|remove|list> [args...]")

    import _scheduler_ops

    sub, rest = extra_args[0], extra_args[1:]

    if sub == "add":
        start_iso = end_iso = None
        i = 0
        while i < len(rest):
            if rest[i] == "--start" and i + 1 < len(rest):
                start_iso, i = rest[i + 1], i + 2
            elif rest[i] == "--end" and i + 1 < len(rest):
                end_iso, i = rest[i + 1], i + 2
            else:
                sys.exit("Usage: channel.py schedule add [--start ISO8601] [--end ISO8601]")
        window = _scheduler_ops.add_window(cfg, _session(cfg), _channel_name(cfg), config_path, start_iso, end_iso)
        if start_iso is None:
            print("No --start given -- starting the channel now ...")
            cmd_start(cfg, [])
        if as_json:
            print(json.dumps(window))
        else:
            print(f"Scheduled window {window['id']}: start={window['start'] or 'now'} end={window['end'] or 'manual stop'}")
        return

    if sub == "remove":
        if len(rest) != 1:
            sys.exit("Usage: channel.py schedule remove <window-id>")
        window = _scheduler_ops.remove_window(_session(cfg), _channel_name(cfg), config_path, rest[0])
        if as_json:
            print(json.dumps(window))
        else:
            print(f"Removed window {window['id']}.")
        return

    if sub == "list":
        if rest:
            sys.exit("Usage: channel.py schedule list")
        windows = _scheduler_ops.list_windows(config_path)
        if as_json:
            print(json.dumps(windows))
        elif not windows:
            print("No scheduled windows.")
        else:
            for w in windows:
                print(f"  {w['id']}  [{w['status']:<8}]  start={w['start'] or 'now'}  end={w['end'] or 'manual stop'}")
        return

    sys.exit(f"Unknown schedule subcommand: {sub!r} (expected add, remove, or list)")


def cmd_terminate(cfg, config_path, extra_args):
    """Tear the channel down for good: `cdk destroy` the stack. The
    inverse of `create`'s `cdk deploy` -- unlike `redeploy`, this doesn't
    bring anything back up afterward. Content staged in S3 is NOT deleted
    (the stack doesn't own the bucket, see README.md's Notes/gotchas);
    clean that up separately if needed.

    Not available for local-docker: there's no CloudFormation stack, so
    `stop` (which removes the container) is already complete teardown."""
    if extra_args:
        sys.exit("`terminate` does not take extra arguments")

    if _backend(cfg) in _NO_STACK_BACKENDS:
        sys.exit("`terminate` has no meaning for local-docker (no CloudFormation stack) "
                  "-- use `stop` instead.")

    import subprocess

    # Best-effort: drop any scheduled windows for this channel so nothing
    # is left pointing at a stack that's about to stop existing. Never
    # blocks terminate itself (e.g. the scheduler stack was never
    # deployed, or nothing was ever scheduled).
    import _scheduler_ops
    _scheduler_ops.delete_all(_session(cfg), _channel_name(cfg), config_path)

    stack_name = _stack_name(cfg)
    cdk_cmd = ["cdk", "destroy", "--force", *_cdk_output(_channel_name(cfg)),
               "-c", f"config={config_path}", stack_name]
    print(f"Running: {' '.join(cdk_cmd)}")
    result = subprocess.run(cdk_cmd, check=False)
    sys.exit(result.returncode)


COMMANDS = {
    "spark": cmd_spark,
    "start": cmd_start,
    "stop": cmd_stop,
    "refresh": cmd_refresh,
    "update": cmd_update,
}

# Commands taking (cfg, extra_args, as_json) instead of just (cfg, extra_args).
JSON_COMMANDS = {
    "status": cmd_status,
    "outputs": cmd_outputs,
}

# Commands needing config_path in addition to cfg (they shell out to `cdk`).
CONFIG_PATH_COMMANDS = {
    "redeploy": cmd_redeploy,
    "create": cmd_create,
    "terminate": cmd_terminate,
}

if __name__ == "__main__":
    config_path, command, extra_args, as_json = _parse_args()

    if command == "list":
        cmd_list(config_path, extra_args, as_json=as_json)
        sys.exit(0)

    all_commands = list(COMMANDS) + list(JSON_COMMANDS) + list(CONFIG_PATH_COMMANDS) + ["list", "schedule"]
    if (command not in COMMANDS and command not in JSON_COMMANDS
            and command not in CONFIG_PATH_COMMANDS and command != "schedule"):
        prog = "python channel.py"
        print(f"Usage: {prog} [--config path/to/config.toml] [--json] [{' | '.join(all_commands)}]")
        sys.exit(1)

    cfg = _config(config_path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    if command == "schedule":
        cmd_schedule(cfg, config_path, extra_args, as_json=as_json)
    elif command in CONFIG_PATH_COMMANDS:
        CONFIG_PATH_COMMANDS[command](cfg, config_path, extra_args)
    elif command in JSON_COMMANDS:
        JSON_COMMANDS[command](cfg, extra_args, as_json=as_json)
    else:
        COMMANDS[command](cfg, extra_args)
