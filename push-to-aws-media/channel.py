#!/usr/bin/env python3
"""
Helper script for managing a ScteLoopStack channel.

Usage:
  python channel.py [--config path/to/config.toml] <command>

Commands:
  upload    Upload the .ts file from the config to S3
  start     Start the channel, wait for RUNNING, print playback URLs
  stop      Stop the channel, wait for IDLE
  status    Print current channel state
  outputs   Print all CloudFormation stack outputs
  policy    No-op (MediaPackage v1 endpoints are public)
  redeploy  Delete a broken stack if needed, then run cdk deploy

The --config flag (short: -c) selects a config file; defaults to
config.toml in the same directory as this script. The stack name is
derived from [deploy].name in the config file, so each config file
targets an independent deployment.
"""

import os
import subprocess
import sys
import time
import tomllib
import boto3

_DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config.toml")


def _parse_args():
    """Return (config_path, command), consuming --config/-c from sys.argv."""
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
    return f"ScteLoopStack-{name}"


def _session(cfg):
    region = cfg.get("aws", {}).get("region")
    return boto3.session.Session(region_name=region)


def _cf_outputs(cfg):
    stack_name = _stack_name(cfg)
    cf = _session(cfg).client("cloudformation")
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def _channel_id(outputs):
    cid = outputs.get("MediaLiveChannelId")
    if not cid:
        sys.exit("MediaLiveChannelId not found in stack outputs.")
    return cid


def _wait_for_state(ml, channel_id, target_state, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = ml.describe_channel(ChannelId=channel_id)
        state = resp["State"]
        print(f"  state: {state}", flush=True)
        if state == target_state:
            return state
        if state in ("CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"):
            sys.exit(f"Channel entered error state: {state}")
        time.sleep(5)
    sys.exit(f"Timed out waiting for {target_state}")


def cmd_start(cfg):
    outputs = _cf_outputs(cfg)
    channel_id = _channel_id(outputs)
    ml = _session(cfg).client("medialive")

    current = ml.describe_channel(ChannelId=channel_id)["State"]
    if current == "RUNNING":
        print("Channel is already RUNNING.")
    else:
        print(f"Starting channel {channel_id} …")
        ml.start_channel(ChannelId=channel_id)
        _wait_for_state(ml, channel_id, "RUNNING")
        print("Channel is RUNNING.")

    print(f"\nHLS:  {outputs.get('HlsPlaybackUrl', 'n/a')}")
    print(f"DASH: {outputs.get('DashPlaybackUrl', 'n/a')}")


def cmd_stop(cfg):
    outputs = _cf_outputs(cfg)
    channel_id = _channel_id(outputs)
    ml = _session(cfg).client("medialive")

    current = ml.describe_channel(ChannelId=channel_id)["State"]
    if current == "IDLE":
        print("Channel is already IDLE.")
        return

    print(f"Stopping channel {channel_id} …")
    ml.stop_channel(ChannelId=channel_id)
    _wait_for_state(ml, channel_id, "IDLE")
    print("Channel is IDLE.")


def cmd_status(cfg):
    outputs = _cf_outputs(cfg)
    channel_id = _channel_id(outputs)
    ml = _session(cfg).client("medialive")
    state = ml.describe_channel(ChannelId=channel_id)["State"]
    print(f"Channel {channel_id}: {state}")


def cmd_outputs(cfg):
    outputs = _cf_outputs(cfg)
    max_key = max(len(k) for k in outputs) if outputs else 0
    for k, v in sorted(outputs.items()):
        print(f"  {k:<{max_key}}  {v}")


def cmd_upload(cfg):
    ts_file     = cfg.get("input", {}).get("ts_file", "")
    bucket_name = cfg.get("s3", {}).get("bucket_name", "")
    s3_folder   = cfg.get("s3", {}).get("folder", "").strip("/")

    if not ts_file or not bucket_name:
        sys.exit("ts_file and bucket_name must be set in the config")

    ts_path     = os.path.abspath(ts_file)
    ts_filename = os.path.basename(ts_path)
    s3_key      = f"{s3_folder}/{ts_filename}" if s3_folder else ts_filename

    print(f"Uploading {ts_path} → s3://{bucket_name}/{s3_key} …")
    _session(cfg).client("s3").upload_file(ts_path, bucket_name, s3_key)
    print("Upload complete.")


def cmd_policy(cfg):
    """No-op: MediaPackage v1 origin endpoints are publicly reachable."""
    print("MediaPackage v1 endpoints are public — no policy needed.")


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
        print(f"Stack is {status} — waiting for it to settle …")
        while status in WAIT_STATES:
            time.sleep(10)
            resp = cf.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
            print(f"  status: {status}", flush=True)

    if status in DELETE_BEFORE_DEPLOY:
        print(f"Stack is in {status} — deleting before redeployment …")
        cf.delete_stack(StackName=stack_name)
        waiter = cf.get_waiter("stack_delete_complete")
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        print("Stack deleted.")
    elif status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        print(f"Stack is already {status} — redeploying normally.")
    elif status is not None:
        print(f"Stack status: {status}")

    cdk_cmd = ["cdk", "deploy", "--require-approval", "never",
               "-c", f"config={config_path}"]
    print(f"Running: {' '.join(cdk_cmd)}")
    result = subprocess.run(cdk_cmd, check=False)
    sys.exit(result.returncode)


COMMANDS = {
    "upload": cmd_upload,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "outputs": cmd_outputs,
    "policy": cmd_policy,
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
