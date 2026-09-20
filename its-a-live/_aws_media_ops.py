"""MediaLive + MediaPackage v1 backend implementation for channel.py's
commands.

Franken-ts output (.ts, real SCTE-35 markers) is uploaded as-is to S3 --
no baking/transformation happens (MediaLive re-encodes it live). See
media_stack.py for the MediaLive/MediaPackage wiring and
AGENT_BRIEF.md/MEDIAPACKAGE_V2.md for background.
"""

import os
import sys
import time


def spark(cfg, session, channel_name, extra_args=None):
    """Upload the franken-ts .ts file to S3 as MediaLive's TS_FILE input
    source. No transformation happens -- MediaLive re-encodes it live."""
    source_path = cfg.get("input", {}).get("source_path", "")
    bucket_name = cfg.get("s3", {}).get("bucket_name", "")
    folder = cfg.get("s3", {}).get("content_folder", "").strip("/")

    if not source_path or not bucket_name:
        sys.exit("input.source_path and s3.bucket_name must be set in the config")

    ts_path = os.path.abspath(source_path)
    ts_filename = os.path.basename(ts_path)
    s3_key = f"{folder}/{ts_filename}" if folder else ts_filename

    print(f"Uploading {ts_path} -> s3://{bucket_name}/{s3_key} ...")
    session.client("s3").upload_file(ts_path, bucket_name, s3_key)
    print("Spark complete (uploaded).")


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


def start(cfg, session, outputs, extra_args=None):
    if extra_args:
        sys.exit("aws-media backend's `start` does not take extra arguments "
                 "(no --epoch-utc concept -- MediaLive has no epoch)")

    channel_id = _channel_id(outputs)
    ml = session.client("medialive")

    current = ml.describe_channel(ChannelId=channel_id)["State"]
    if current == "RUNNING":
        print("Channel is already RUNNING.")
    else:
        print(f"Starting channel {channel_id} ...")
        ml.start_channel(ChannelId=channel_id)
        _wait_for_state(ml, channel_id, "RUNNING")
        print("Channel is RUNNING.")

    print(f"\nHLS:  {outputs.get('HlsPlaybackUrl', 'n/a')}")
    print(f"DASH: {outputs.get('DashPlaybackUrl', 'n/a')}")


def stop(cfg, session, outputs):
    channel_id = _channel_id(outputs)
    ml = session.client("medialive")

    current = ml.describe_channel(ChannelId=channel_id)["State"]
    if current == "IDLE":
        print("Channel is already IDLE.")
        return

    print(f"Stopping channel {channel_id} ...")
    ml.stop_channel(ChannelId=channel_id)
    _wait_for_state(ml, channel_id, "IDLE")
    print("Channel is IDLE.")


def refresh(cfg, session, outputs):
    """MediaLive's TS_FILE input doesn't hot-reload -- there's no lighter
    option than a real restart to pick up a new upload. Full stop -> start
    cycle (a real, brief channel interruption)."""
    print("aws-media backend has no hot-reload -- refreshing via a full "
          "stop -> start cycle ...")
    stop(cfg, session, outputs)
    start(cfg, session, outputs)


def status(cfg, session, outputs):
    channel_id = _channel_id(outputs)
    ml = session.client("medialive")
    state = ml.describe_channel(ChannelId=channel_id)["State"]
    print(f"Channel {channel_id}: {state}")
