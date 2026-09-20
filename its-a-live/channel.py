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
  status    Print the channel's current status
  outputs   Print all CloudFormation stack outputs
  redeploy  Delete a broken stack if needed, then run cdk deploy
  create    First-time, non-interactive setup: (ecs-express only) ensure
            the shared ECS cluster stack is deployed, `spark` (content
            MUST be staged before the channel stack exists), `cdk deploy`
            the channel stack, then `start`. Equivalent to
            galvanise.py's pipeline minus the interactive confirmations --
            intended for programmatic callers (e.g. a management UI).
  list      List channels found under a directory of TOML configs
            (default: the directory containing --config), each with its
            CloudFormation stack status if deployed.

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
    return cfg


def _backend(cfg):
    return cfg["deploy"]["backend"]


def _channel_name(cfg):
    return cfg.get("deploy", {}).get("name", "default")


def _stack_name(cfg):
    return f"ItsALiveStack-{_channel_name(cfg)}-{_backend(cfg)}"


def _shared_stack_name(cfg):
    """Only meaningful for ecs-express (the shared ECS cluster stack)."""
    return "ItsALiveSharedStack-ecs-express"


def _session(cfg):
    region = cfg.get("aws", {}).get("region")
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


def cmd_status(cfg, extra_args, as_json=False):
    if extra_args:
        sys.exit("`status` does not take extra arguments")
    outputs = _outputs_if_needed(cfg)
    result = _ops(cfg).status(cfg, _session(cfg), outputs)
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


def cmd_list(config_path, extra_args, as_json=False):
    """List channels found from TOML configs in the same directory as
    --config (or the directory given as the sole extra arg), each paired
    with its CloudFormation stack status if one exists yet (None if the
    channel has never been deployed). local-docker channels have no
    stack; "stack_status" instead reflects the local container's Docker
    status."""
    if len(extra_args) > 1:
        sys.exit("Usage: channel.py list [directory]")
    directory = extra_args[0] if extra_args else os.path.dirname(os.path.abspath(config_path))

    channels = []
    for path in sorted(glob.glob(os.path.join(directory, "*.toml"))):
        try:
            cfg = _config(path)
        except SystemExit:
            continue
        name = _channel_name(cfg)
        backend = _backend(cfg)
        if backend in _NO_STACK_BACKENDS:
            result = _ops(cfg).status(cfg, _session(cfg), None)
            channels.append({
                "config_path": path,
                "name": name,
                "backend": backend,
                "stack_name": None,
                "stack_status": result.get("status"),
            })
            continue
        stack_name = _stack_name(cfg)
        cf = _session(cfg).client("cloudformation")
        channels.append({
            "config_path": path,
            "name": name,
            "backend": backend,
            "stack_name": stack_name,
            "stack_status": _stack_status(cf, stack_name),
        })

    if as_json:
        print(json.dumps(channels))
        return
    if not channels:
        print(f"No *.toml configs found in {directory}")
        return
    for c in channels:
        print(f"  {c['name']:<20} backend={c['backend']:<12} "
              f"stack_status={c['stack_status'] or 'not deployed':<20} {c['config_path']}")


def _ensure_shared_stack_if_needed(cfg, config_path):
    import subprocess

    if _backend(cfg) != "ecs-express":
        return
    shared_stack_name = _shared_stack_name(cfg)
    print(f"Ensuring {shared_stack_name} is deployed ...")
    result = subprocess.run(
        ["cdk", "deploy", "--require-approval", "never",
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
        ["cdk", "deploy", "--require-approval", "never",
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

    if _backend(cfg) == "ecs-express":
        # The shared ECS cluster stack rarely breaks and isn't config-name
        # dependent -- just make sure it's deployed (idempotent no-op if
        # already up to date) before handling the channel stack itself.
        shared_stack_name = _shared_stack_name(cfg)
        print(f"Ensuring {shared_stack_name} is deployed ...")
        result = subprocess.run(
            ["cdk", "deploy", "--require-approval", "never",
             "-c", f"config={config_path}", shared_stack_name],
            check=False,
        )
        if result.returncode != 0:
            sys.exit(result.returncode)

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
    cdk_cmd = ["cdk", "deploy", "--require-approval", "never",
               "-c", f"config={config_path}", stack_name]
    print(f"Running: {' '.join(cdk_cmd)}")
    result = subprocess.run(cdk_cmd, check=False)
    sys.exit(result.returncode)


COMMANDS = {
    "spark": cmd_spark,
    "start": cmd_start,
    "stop": cmd_stop,
    "refresh": cmd_refresh,
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
}

if __name__ == "__main__":
    config_path, command, extra_args, as_json = _parse_args()

    if command == "list":
        cmd_list(config_path, extra_args, as_json=as_json)
        sys.exit(0)

    all_commands = list(COMMANDS) + list(JSON_COMMANDS) + list(CONFIG_PATH_COMMANDS) + ["list"]
    if command not in COMMANDS and command not in JSON_COMMANDS and command not in CONFIG_PATH_COMMANDS:
        prog = "python channel.py"
        print(f"Usage: {prog} [--config path/to/config.toml] [--json] [{' | '.join(all_commands)}]")
        sys.exit(1)

    cfg = _config(config_path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    if command in CONFIG_PATH_COMMANDS:
        CONFIG_PATH_COMMANDS[command](cfg, config_path, extra_args)
    elif command in JSON_COMMANDS:
        JSON_COMMANDS[command](cfg, extra_args, as_json=as_json)
    else:
        COMMANDS[command](cfg, extra_args)
