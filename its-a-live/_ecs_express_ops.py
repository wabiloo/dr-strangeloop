"""ECS Express Mode backend implementation for channel.py's commands.

Franken-ts output is baked locally (GPAC, via loop-dee-loop/bake.py -- no
AWS compute involved) into a loop package, pushed to S3, and served by an
AWS::ECS::ExpressGatewayService (see loop_stack.py). See README.md for the
full architecture writeup and hard-won operational notes (health-check
path format, canary deployment timing, etc).
"""

import datetime
import os
import shutil
import subprocess
import sys
import time

from _infra_cfg import infra_table
from _reachability import check_manifest_reachable

_LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")


def _resolve_python_cmd():
    """Same resolution order as loop-dee-loop/run.sh: loop-dee-loop is a
    workspace member of the repo-root uv project (shares its .venv, not
    its own), so `uv run --project` resolves its deps without a manually
    created venv. Falls back to plain python3 if uv itself isn't on PATH."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", _LOOP_DEE_LOOP_DIR, "python"]
    return ["python3"]


def spark(cfg, session, channel_name, extra_args=None):
    """Run bake.py LOCALLY against the configured franken-ts source, then
    push the resulting loop package to S3. No AWS compute involved -- this
    is a one-shot, run-once-per-schedule-change process."""
    source_path = cfg.get("input", {}).get("source_path", "")
    bucket_name = infra_table(cfg, "s3").get("bucket_name", "")
    folder = infra_table(cfg, "s3").get("content_folder", "its-a-live/content").strip("/")
    segment_duration = str(cfg.get("packaging", {}).get("segment_duration", 4.0))
    packaging = cfg.get("packaging", {})
    local_output_dir = cfg.get("bake", {}).get(
        "local_output_dir", os.path.join(os.path.dirname(__file__), ".local-loop-package", channel_name)
    )
    markers_cfg = cfg.get("markers", {})
    daterange_mode = markers_cfg.get("daterange_mode", "shared")
    cue_tags = markers_cfg.get("cue_tags", "none")
    increment_event_ids = markers_cfg.get("increment_event_ids", False)
    daterange_id_format = markers_cfg.get(
        "daterange_id_format", "{segcode}-{eventid}-{loop}"
    )
    dash_signal_format = markers_cfg.get("dash_signal_format", "binary")
    dash_descriptor_mode = markers_cfg.get("dash_descriptor_mode", "shared")

    if not source_path or not bucket_name:
        sys.exit("input.source_path and s3.bucket_name must be set in the config")

    source_path = os.path.abspath(source_path)
    local_output_dir = os.path.abspath(local_output_dir)
    os.makedirs(local_output_dir, exist_ok=True)

    python_cmd = _resolve_python_cmd()
    bake_script = os.path.join(_LOOP_DEE_LOOP_DIR, "bake.py")
    bake_args = python_cmd + [bake_script, source_path, "--output", local_output_dir,
                               "--segment-duration", segment_duration,
                               "--hls-format", packaging.get("hls_format", "cmaf"),
                               "--hls-ts-mux-audio" if packaging.get("hls_ts_mux_audio", True) else "--no-hls-ts-mux-audio",
                              "--daterange-mode", daterange_mode,
                              "--cue-tags", cue_tags,
                              "--daterange-id-format", daterange_id_format,
                              "--dash-signal-format", dash_signal_format,
                              "--dash-descriptor-mode", dash_descriptor_mode]
    if increment_event_ids:
        bake_args.append("--increment-event-ids")
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

    prefix = f"{folder}/{channel_name}"
    s3_uri = f"s3://{bucket_name}/{prefix}"
    print(f"==> Pushing loop package to {s3_uri} ...")
    sync_args = ["aws", "s3", "sync", local_output_dir, s3_uri, "--delete"]
    if infra_table(cfg, "aws").get("region"):
        sync_args += ["--region", infra_table(cfg, "aws")["region"]]
    result = subprocess.run(sync_args)
    if result.returncode != 0:
        sys.exit(f"aws s3 sync failed (exit {result.returncode}) -- is the AWS CLI installed and configured?")

    print("Spark complete.")
    print(f"Loop package published to: {s3_uri}")
    print("Run `channel.py start` to (re)start the channel and pick it up.")


def _parse_cluster_and_service(service_arn):
    # arn:aws:ecs:region:account:service/CLUSTER/SERVICE_NAME
    resource = service_arn.split(":service/", 1)[1]
    cluster, service_name = resource.split("/", 1)
    return cluster, service_name


def _wait_for_primary_deployment_healthy(ecs_client, service_arn, expected_desired_count, max_attempts=60):
    """Wait for the real ECS deployment behind the Express service to reach
    runningCount == desiredCount on its PRIMARY deployment.

    NOTE: this is deliberately NOT the same as waiting for rolloutState to
    reach COMPLETED -- in practice that ECS-level bookkeeping (traffic-shift
    finalization, old-task cleanup) can take several more minutes after the
    new task is already up, healthy, and actually serving the new content.
    For a single-task, stateless service like this one, "task running" is
    what matters for `channel.py start`/`stop`'s purposes.
    """
    cluster, service_name = _parse_cluster_and_service(service_arn)
    for _ in range(max_attempts):
        resp = ecs_client.describe_services(cluster=cluster, services=[service_name])
        deployments = resp["services"][0].get("deployments", [])
        primary = next((d for d in deployments if d["status"] == "PRIMARY"), None)
        if primary is None:
            time.sleep(10)
            continue
        running = primary.get("runningCount", 0)
        desired = primary.get("desiredCount", 0)
        print(f"  primary deployment: desired={desired} running={running} rolloutState={primary.get('rolloutState')}", flush=True)
        if desired == expected_desired_count and running == expected_desired_count:
            return primary
        time.sleep(10)
    sys.exit("Timed out waiting for the primary deployment to reach the expected running count.")


def _patch_epoch_utc(command, epoch_utc):
    command = list(command)
    for i, arg in enumerate(command):
        if arg == "--epoch-utc" and i + 1 < len(command):
            command[i + 1] = epoch_utc
            return command
    sys.exit("Could not find --epoch-utc in the service's command -- has the stack drifted?")


def _resolve_epoch_arg(raw):
    """`raw` is whatever followed --epoch-utc on the command line: the
    literal string "now", or an ISO8601 UTC timestamp (e.g.
    2026-01-01T00:00:00Z). Returns the resolved timestamp string."""
    if raw == "now":
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        sys.exit(f"--epoch-utc value {raw!r} must be 'now' or an ISO8601 UTC "
                  f"timestamp like 2026-01-01T00:00:00Z")
    return raw


def start(cfg, session, outputs, extra_args=None):
    """Scale the Express service to 1 task (if it was stopped).

    Epoch defaults to whatever is already baked into the service (from
    `[timeline] epoch_utc`, default 2026-01-01T00:00:00Z -- see
    loop_stack.py) and is left untouched, making this a pure scaling
    operation: no primaryContainer change, no new task revision, no canary
    deployment. Pass `--epoch-utc now` or `--epoch-utc <ISO8601>` to
    explicitly (re)set the loop's epoch instead -- that forces a real
    redeploy (new task revision, ~3 minute canary bake before full
    cutover, per README) since it does change primaryContainer.
    """
    service_arn = outputs.get("ExpressServiceArn")
    if not service_arn:
        sys.exit("Missing ExpressServiceArn output -- has the stack been deployed?")

    epoch_arg = None
    extra_args = extra_args or []
    if extra_args:
        if extra_args[0] != "--epoch-utc" or len(extra_args) < 2:
            sys.exit("Usage: channel.py start [--epoch-utc now|<ISO8601 UTC timestamp>]")
        epoch_arg = _resolve_epoch_arg(extra_args[1])

    ecs_client = session.client("ecs")

    if epoch_arg is None:
        print("Scaling service to 1 task (if stopped); epoch left as-is ...")
        ecs_client.update_express_gateway_service(
            serviceArn=service_arn,
            scalingTarget={"minTaskCount": 1, "maxTaskCount": 1},
        )
    else:
        service = ecs_client.describe_express_gateway_service(serviceArn=service_arn)["service"]
        primary_container = dict(service["activeConfigurations"][0]["primaryContainer"])
        primary_container["command"] = _patch_epoch_utc(primary_container.get("command", []), epoch_arg)

        print(f"Setting --epoch-utc {epoch_arg} and scaling to 1 task ...")
        ecs_client.update_express_gateway_service(
            serviceArn=service_arn,
            primaryContainer=primary_container,
            scalingTarget={"minTaskCount": 1, "maxTaskCount": 1},
        )

    print("Waiting for the task to be up and running"
          + ("" if epoch_arg is None else
             " (this confirms the task is healthy, NOT that all viewer"
             " traffic has cut over to the new epoch yet -- see README's"
             " canary deployment notes)") + " ...")
    _wait_for_primary_deployment_healthy(ecs_client, service_arn, expected_desired_count=1)

    print(f"\nHLS:  {outputs.get('HlsPlaybackUrl', 'n/a')}")
    print(f"DASH: {outputs.get('DashPlaybackUrl', 'n/a')}")


def stop(cfg, session, outputs):
    service_arn = outputs.get("ExpressServiceArn")
    if not service_arn:
        sys.exit("Missing ExpressServiceArn output -- has the stack been deployed?")

    ecs_client = session.client("ecs")
    service = ecs_client.describe_express_gateway_service(serviceArn=service_arn)["service"]
    active = service["activeConfigurations"][0]
    if active.get("scalingTarget", {}).get("maxTaskCount") == 0:
        print("Service is already scaled to 0 tasks.")
        return

    print("Scaling service to 0 tasks (no Fargate compute cost while stopped;"
          " the shared ALB itself keeps running for other channels) ...")
    ecs_client.update_express_gateway_service(
        serviceArn=service_arn,
        scalingTarget={"minTaskCount": 0, "maxTaskCount": 0},
    )
    _wait_for_primary_deployment_healthy(ecs_client, service_arn, expected_desired_count=0)
    print("Service is scaled to 0.")


def refresh(cfg, session, outputs):
    """Force a fresh task launch on an already-running service, so it
    re-syncs LOOP_PACKAGE_S3_URI and picks up a new spark/bake. Not needed
    after `channel.py start` from a stopped state -- that already launches
    a new task. Only needed when you `spark` new content while the channel
    is already running and want it to take effect without a stop/start
    cycle."""
    service_arn = outputs.get("ExpressServiceArn")
    if not service_arn:
        sys.exit("Missing ExpressServiceArn output -- has the stack been deployed?")

    ecs_client = session.client("ecs")
    cluster, service_name = _parse_cluster_and_service(service_arn)

    print("Forcing a new deployment so the running task re-syncs"
          " LOOP_PACKAGE_S3_URI and picks up the latest spark ...")
    ecs_client.update_service(cluster=cluster, service=service_name, forceNewDeployment=True)

    print("Waiting for the new task to be up and running (ECS Express Mode's"
          " canary deployment strategy means the OLD task can still serve"
          " most/all traffic for a ~3 minute bake period -- see README for"
          " details) ...")
    _wait_for_primary_deployment_healthy(ecs_client, service_arn, expected_desired_count=1)

    print(f"\nHLS:  {outputs.get('HlsPlaybackUrl', 'n/a')}")
    print(f"DASH: {outputs.get('DashPlaybackUrl', 'n/a')}")


def status(cfg, session, outputs):
    """Prints a human-readable summary and returns a structured dict (used
    by `channel.py status --json` and, longer-term, by any programmatic
    caller such as a management API/UI)."""
    service_arn = outputs.get("ExpressServiceArn")
    ecs_client = session.client("ecs")
    service = ecs_client.describe_express_gateway_service(serviceArn=service_arn)["service"]
    active = service["activeConfigurations"][0]
    scaling = active.get("scalingTarget", {})
    min_tasks = scaling.get("minTaskCount")
    hls_url = outputs.get("HlsPlaybackUrl")
    dash_url = outputs.get("DashPlaybackUrl")
    result = {
        "backend": "ecs-express",
        "service_name": service["serviceName"],
        "status": service["status"]["statusCode"],
        "min_tasks": min_tasks,
        "max_tasks": scaling.get("maxTaskCount"),
        "hls_url": hls_url,
        "dash_url": dash_url,
        # Only worth network-checking once actually scaled up -- see
        # _reachability.py.
        "reachable": check_manifest_reachable(hls_url, dash_url) if min_tasks else None,
    }
    print(f"Service {result['service_name']}: status={result['status']} "
          f"minTasks={result['min_tasks']} maxTasks={result['max_tasks']}"
          + (f" (manifest {'reachable' if result['reachable'] else 'UNREACHABLE'})" if result["reachable"] is not None else ""))
    return result
