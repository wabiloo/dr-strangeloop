#!/usr/bin/env python3
"""
Helper script for managing a LoopChannelStack channel.

Usage:
  python channel.py [--config path/to/config.toml] <command>

Commands:
  bake      Run bake.py LOCALLY against the configured franken-ts source,
            then push the resulting loop package to S3 (no AWS compute
            involved -- bake is a one-shot, run-once-per-schedule-change
            process, so for this stack it just runs on your machine)
  start     (Re)start the App Runner service with epoch-utc=now (resuming
            it first if paused), wait for it to be RUNNING, print playback
            URLs
  stop      Pause the App Runner service (native pause/resume -- this is
            what actually stops paying for compute between airings)
  status    Print the App Runner service's current status
  outputs   Print all CloudFormation stack outputs
  redeploy  Delete a broken stack if needed, then run cdk deploy

The --config flag (short: -c) selects a config file; defaults to
config.toml in the same directory as this script. The stack name is
derived from [deploy].name in the config file, so each config file
targets an independent channel deployment.
"""

import datetime
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import boto3

_DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config.toml")
_LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")


def _parse_args():
    args = sys.argv[1:]
    config_path = _DEFAULT_CONFIG
    if args and args[0] in ("--config", "-c"):
        if len(args) < 2:
            sys.exit("--config requires a path argument")
        config_path = args[1]
        args = args[2:]
    if len(args) != 1:
        return config_path, None
    return config_path, args[0]


def _config(config_path):
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _stack_name(cfg):
    name = cfg.get("deploy", {}).get("name", "default")
    return f"LoopChannelStack-{name}"


def _channel_name(cfg):
    return cfg.get("deploy", {}).get("name", "default")


def _session(cfg):
    region = cfg.get("aws", {}).get("region")
    return boto3.session.Session(region_name=region)


def _cf_outputs(cfg):
    stack_name = _stack_name(cfg)
    cf = _session(cfg).client("cloudformation")
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def _resolve_python_cmd():
    """Same resolution order as loop-dee-loop/run.sh: prefer a local .venv
    next to loop-dee-loop, then `uv run`, then plain python3."""
    venv_python = os.path.join(_LOOP_DEE_LOOP_DIR, ".venv", "bin", "python")
    if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
        return [venv_python]
    if shutil.which("uv"):
        return ["uv", "run", "--project", _LOOP_DEE_LOOP_DIR, "python"]
    return ["python3"]


def cmd_bake(cfg):
    source_path = cfg.get("input", {}).get("source_path", "")
    bucket_name = cfg.get("s3", {}).get("bucket_name", "")
    folder = cfg.get("s3", {}).get("loop_package_folder", "loop-dee-loop/packages").strip("/")
    name = _channel_name(cfg)
    segment_duration = str(cfg.get("channel", {}).get("segment_duration", 4.0))
    local_output_dir = cfg.get("bake", {}).get(
        "local_output_dir", os.path.join(os.path.dirname(__file__), ".local-loop-package", name)
    )

    if not source_path or not bucket_name:
        sys.exit("input.source_path and s3.bucket_name must be set in the config")

    source_path = os.path.abspath(source_path)
    local_output_dir = os.path.abspath(local_output_dir)
    os.makedirs(local_output_dir, exist_ok=True)

    python_cmd = _resolve_python_cmd()
    bake_script = os.path.join(_LOOP_DEE_LOOP_DIR, "bake.py")
    bake_args = python_cmd + [bake_script, source_path, "--output", local_output_dir,
                              "--segment-duration", segment_duration]

    print(f"==> Baking locally: {source_path} -> {local_output_dir}")
    print(f"    {' '.join(bake_args)}")
    result = subprocess.run(bake_args)
    if result.returncode != 0:
        sys.exit(f"bake.py failed (exit {result.returncode}) -- see output above.")

    prefix = f"{folder}/{name}"
    s3_uri = f"s3://{bucket_name}/{prefix}"
    print(f"==> Pushing loop package to {s3_uri} ...")
    sync_args = ["aws", "s3", "sync", local_output_dir, s3_uri, "--delete"]
    if cfg.get("aws", {}).get("region"):
        sync_args += ["--region", cfg["aws"]["region"]]
    result = subprocess.run(sync_args)
    if result.returncode != 0:
        sys.exit(f"aws s3 sync failed (exit {result.returncode}) -- is the AWS CLI installed and configured?")

    print("Bake + push complete.")
    print(f"Loop package published to: {s3_uri}")
    print("Run `channel.py start` to (re)start the channel and pick it up.")


def _wait_for_apprunner_status(ar, service_arn, target_status, max_attempts=60):
    FAILURE_STATUSES = {"CREATE_FAILED", "DELETE_FAILED"}
    for _ in range(max_attempts):
        status = ar.describe_service(ServiceArn=service_arn)["Service"]["Status"]
        print(f"  status: {status}", flush=True)
        if status == target_status:
            return status
        if status in FAILURE_STATUSES:
            sys.exit(f"Service entered failure state: {status}")
        time.sleep(10)
    sys.exit(f"Timed out waiting for status {target_status}")


def cmd_start(cfg):
    outputs = _cf_outputs(cfg)
    service_arn = outputs.get("AppRunnerServiceArn")
    if not service_arn:
        sys.exit("Missing AppRunnerServiceArn output -- has the stack been deployed?")

    ar = _session(cfg).client("apprunner")
    desc = ar.describe_service(ServiceArn=service_arn)["Service"]

    if desc["Status"] == "PAUSED":
        print("Resuming paused service ...")
        ar.resume_service(ServiceArn=service_arn)
        _wait_for_apprunner_status(ar, service_arn, "RUNNING")
        desc = ar.describe_service(ServiceArn=service_arn)["Service"]

    image_repo = dict(desc["SourceConfiguration"]["ImageRepository"])
    image_config = dict(image_repo.get("ImageConfiguration", {}))
    start_command = image_config.get("StartCommand", "")

    epoch_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "--epoch-utc" not in start_command:
        sys.exit("Could not find --epoch-utc in the service's start command -- has the stack drifted?")
    new_start_command = re.sub(r"--epoch-utc\s+\S+", f"--epoch-utc {epoch_utc}", start_command)

    print(f"Updating service with --epoch-utc {epoch_utc} ...")
    image_config["StartCommand"] = new_start_command
    image_repo["ImageConfiguration"] = image_config
    ar.update_service(
        ServiceArn=service_arn,
        SourceConfiguration={
            "ImageRepository": image_repo,
            "AutoDeploymentsEnabled": desc["SourceConfiguration"].get("AutoDeploymentsEnabled", False),
        },
    )

    print("Waiting for the new deployment to become RUNNING ...")
    _wait_for_apprunner_status(ar, service_arn, "RUNNING")

    print(f"\nHLS:  {outputs.get('HlsPlaybackUrl', 'n/a')}")
    print(f"DASH: {outputs.get('DashPlaybackUrl', 'n/a')}")


def cmd_stop(cfg):
    outputs = _cf_outputs(cfg)
    service_arn = outputs.get("AppRunnerServiceArn")
    if not service_arn:
        sys.exit("Missing AppRunnerServiceArn output -- has the stack been deployed?")

    ar = _session(cfg).client("apprunner")
    status = ar.describe_service(ServiceArn=service_arn)["Service"]["Status"]
    if status == "PAUSED":
        print("Service is already PAUSED.")
        return

    print("Pausing service ...")
    ar.pause_service(ServiceArn=service_arn)
    _wait_for_apprunner_status(ar, service_arn, "PAUSED")
    print("Service is paused (no compute charges while paused).")


def cmd_status(cfg):
    outputs = _cf_outputs(cfg)
    service_arn = outputs.get("AppRunnerServiceArn")
    ar = _session(cfg).client("apprunner")
    desc = ar.describe_service(ServiceArn=service_arn)["Service"]
    print(f"Service {desc['ServiceName']}: status={desc['Status']}")


def cmd_outputs(cfg):
    outputs = _cf_outputs(cfg)
    max_key = max(len(k) for k in outputs) if outputs else 0
    for k, v in sorted(outputs.items()):
        print(f"  {k:<{max_key}}  {v}")


def cmd_redeploy(cfg, config_path):
    stack_name = _stack_name(cfg)
    cf = _session(cfg).client("cloudformation")

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

    cdk_cmd = ["cdk", "deploy", "--require-approval", "never",
               "-c", f"config={config_path}"]
    print(f"Running: {' '.join(cdk_cmd)}")
    result = subprocess.run(cdk_cmd, check=False)
    sys.exit(result.returncode)


COMMANDS = {
    "bake": cmd_bake,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "outputs": cmd_outputs,
    "redeploy": cmd_redeploy,
}

if __name__ == "__main__":
    config_path, command = _parse_args()

    if command not in COMMANDS:
        prog = "python channel.py"
        print(f"Usage: {prog} [--config path/to/config.toml] [{' | '.join(COMMANDS)}]")
        sys.exit(1)

    cfg = _config(config_path)

    if command == "redeploy":
        cmd_redeploy(cfg, config_path)
    else:
        COMMANDS[command](cfg)
