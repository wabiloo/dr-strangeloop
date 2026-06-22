#!/usr/bin/env python3
"""
Helper script for managing the MediaLive loop channel.

Usage:
  python channel.py start    – start channel, wait for RUNNING, print URLs
  python channel.py stop     – stop channel, wait for IDLE
  python channel.py status   – print current channel state
  python channel.py outputs  – print all CloudFormation stack outputs
"""

import os
import sys
import time
import tomllib
import boto3

STACK_NAME = "MediaLiveLoopStack"
_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.toml")


def _config():
    with open(_CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def _session():
    region = _config().get("aws", {}).get("region")
    return boto3.session.Session(region_name=region)


def _cf_outputs():
    cf = _session().client("cloudformation")
    resp = cf.describe_stacks(StackName=STACK_NAME)
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


def cmd_start():
    outputs = _cf_outputs()
    channel_id = _channel_id(outputs)
    ml = _session().client("medialive")

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


def cmd_stop():
    outputs = _cf_outputs()
    channel_id = _channel_id(outputs)
    ml = _session().client("medialive")

    current = ml.describe_channel(ChannelId=channel_id)["State"]
    if current == "IDLE":
        print("Channel is already IDLE.")
        return

    print(f"Stopping channel {channel_id} …")
    ml.stop_channel(ChannelId=channel_id)
    _wait_for_state(ml, channel_id, "IDLE")
    print("Channel is IDLE.")


def cmd_status():
    outputs = _cf_outputs()
    channel_id = _channel_id(outputs)
    ml = _session().client("medialive")
    state = ml.describe_channel(ChannelId=channel_id)["State"]
    print(f"Channel {channel_id}: {state}")


def cmd_outputs():
    outputs = _cf_outputs()
    max_key = max(len(k) for k in outputs) if outputs else 0
    for k, v in sorted(outputs.items()):
        print(f"  {k:<{max_key}}  {v}")


def cmd_upload():
    cfg = _config()
    ts_file     = cfg.get("input", {}).get("ts_file", "")
    bucket_name = cfg.get("s3", {}).get("bucket_name", "")
    s3_folder   = cfg.get("s3", {}).get("folder", "").strip("/")

    if not ts_file or not bucket_name:
        sys.exit("ts_file and bucket_name must be set in config.toml")

    ts_path     = os.path.abspath(ts_file)
    ts_filename = os.path.basename(ts_path)
    s3_key      = f"{s3_folder}/{ts_filename}" if s3_folder else ts_filename

    print(f"Uploading {ts_path} → s3://{bucket_name}/{s3_key} …")
    s3 = _session().client("s3")
    s3.upload_file(ts_path, bucket_name, s3_key)
    print("Upload complete.")


def cmd_policy():
    """No-op: MediaPackage v1 origin endpoints are publicly reachable, so no
    endpoint policy or CloudFront OAC is required."""
    print("MediaPackage v1 endpoints are public — no policy needed.")


def cmd_redeploy():
    import subprocess
    cf = _session().client("cloudformation")

    # States where we wait (recoverable in-progress transitions)
    WAIT_STATES = {
        "DELETE_IN_PROGRESS",
        "ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    }
    # States where the stack must be deleted before redeploying
    DELETE_BEFORE_DEPLOY = {
        "CREATE_IN_PROGRESS",   # force-delete stuck creations immediately
        "ROLLBACK_COMPLETE",
        "CREATE_FAILED",
        "ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_FAILED",
    }

    try:
        resp = cf.describe_stacks(StackName=STACK_NAME)
        status = resp["Stacks"][0]["StackStatus"]
    except cf.exceptions.ClientError:
        status = None  # stack doesn't exist yet

    if status in WAIT_STATES:
        print(f"Stack is {status} — waiting for it to settle …")
        while status in WAIT_STATES:
            time.sleep(10)
            resp = cf.describe_stacks(StackName=STACK_NAME)
            status = resp["Stacks"][0]["StackStatus"]
            print(f"  status: {status}", flush=True)

    if status in DELETE_BEFORE_DEPLOY:
        print(f"Stack is in {status} — deleting before redeployment …")
        cf.delete_stack(StackName=STACK_NAME)
        waiter = cf.get_waiter("stack_delete_complete")
        waiter.wait(StackName=STACK_NAME, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        print("Stack deleted.")
    elif status == "CREATE_COMPLETE" or status == "UPDATE_COMPLETE":
        print(f"Stack is already {status} — redeploying normally.")
    elif status is not None:
        print(f"Stack status: {status}")

    print("Running cdk deploy …")
    result = subprocess.run(["cdk", "deploy", "--require-approval", "never"], check=False)
    if result.returncode == 0:
        print("Applying public-read policies to MediaPackage endpoints …")
        cmd_policy()
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
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python channel.py [{' | '.join(COMMANDS)}]")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
