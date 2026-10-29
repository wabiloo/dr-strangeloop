"""aws-lambda backend implementation for channel.py's commands.

Same content pipeline as ecs-express (franken-ts output baked locally by
loop-dee-loop/bake.py, pushed to S3), served by a Lambda function behind
CloudFront (see lambda_stack.py and LAMBDA_BACKEND.md). There is nothing to
"start" for the channel to be playable -- an idle function costs nothing and
the first viewer wakes it up -- so start/stop are an access control: `stop`
sets the function's reserved concurrency to 0 (every request is throttled),
`start` restores the configured cap.
"""

import datetime
import sys
import time

from _infra_cfg import infra_table
from _reachability import check_manifest_reachable

DEFAULT_RESERVED_CONCURRENCY = 10


def spark(cfg, session, channel_name, extra_args=None):
    """Identical to ecs-express: bake locally, push the loop package to S3.
    Imported lazily so the scheduler Lambda (which only runs start/stop) does
    not need the bake-side config modules."""
    from _ecs_express_ops import spark as _spark

    return _spark(cfg, session, channel_name, extra_args)


def _function_name(outputs):
    name = (outputs or {}).get("LambdaFunctionName")
    if not name:
        sys.exit("Missing LambdaFunctionName output -- has the stack been deployed?")
    return name


def _configured_concurrency(cfg, function_environment):
    """The cap `start` restores. The scheduler Lambda calls start() without a
    config, so the stack also records it in the function's environment."""
    if "reserved_concurrency" in infra_table(cfg, "lambda"):
        return int(infra_table(cfg, "lambda")["reserved_concurrency"])
    return int(function_environment.get("RESERVED_CONCURRENCY", DEFAULT_RESERVED_CONCURRENCY))


def _wait_updated(client, function_name):
    client.get_waiter("function_updated_v2").wait(
        FunctionName=function_name, WaiterConfig={"Delay": 3, "MaxAttempts": 100}
    )


def _invalidate_cdn(session, outputs):
    """Segments and init segments are cached at the edge for up to an hour;
    after new content is sparked the same URLs must not keep returning the
    old bytes. Best effort -- never fails the command."""
    distribution_id = (outputs or {}).get("CloudFrontDistributionId")
    if not distribution_id:
        return
    try:
        session.client("cloudfront").create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": f"its-a-live-{time.time_ns()}",
            },
        )
        print("Invalidated the CloudFront cache.")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not invalidate the CloudFront cache ({exc}); edge copies expire within the hour.")


def _print_urls(outputs):
    print(f"\nHLS:  {outputs.get('HlsPlaybackUrl', 'n/a')}")
    print(f"DASH: {outputs.get('DashPlaybackUrl', 'n/a')}")


def start(cfg, session, outputs, extra_args=None):
    """Restore the reserved-concurrency cap (removed by `stop`).

    `--epoch-utc now|<ISO8601>` rewrites the function's serve command, which
    recycles every execution environment (a configuration change), and
    invalidates the CDN."""
    function_name = _function_name(outputs)
    epoch_arg = None
    extra_args = extra_args or []
    if extra_args:
        if extra_args[0] != "--epoch-utc" or len(extra_args) < 2:
            sys.exit("Usage: channel.py start [--epoch-utc now|<ISO8601 UTC timestamp>]")
        from _ecs_express_ops import _resolve_epoch_arg

        epoch_arg = _resolve_epoch_arg(extra_args[1])

    client = session.client("lambda")
    function = client.get_function_configuration(FunctionName=function_name)
    if epoch_arg is not None:
        from _ecs_express_ops import _patch_epoch_utc

        image_config = dict(function.get("ImageConfigResponse", {}).get("ImageConfig", {}))
        image_config["Command"] = _patch_epoch_utc(image_config.get("Command", []), epoch_arg)
        print(f"Setting --epoch-utc {epoch_arg} ...")
        client.update_function_configuration(FunctionName=function_name, ImageConfig=image_config)
        _wait_updated(client, function_name)
        _invalidate_cdn(session, outputs)

    cap = _configured_concurrency(cfg, function.get("Environment", {}).get("Variables", {}))
    if cap > 0:
        print(f"Allowing up to {cap} concurrent invocations ...")
        client.put_function_concurrency(FunctionName=function_name, ReservedConcurrentExecutions=cap)
    else:
        print("Removing the concurrency reservation (reserved_concurrency = -1) ...")
        client.delete_function_concurrency(FunctionName=function_name)
    _print_urls(outputs)


def stop(cfg, session, outputs):
    function_name = _function_name(outputs)
    client = session.client("lambda")
    print("Setting reserved concurrency to 0: every request is now throttled."
          " (An idle function costs nothing either way -- this is an access"
          " control, not a cost saving.)")
    client.put_function_concurrency(FunctionName=function_name, ReservedConcurrentExecutions=0)


def refresh(cfg, session, outputs):
    """Make already-warm execution environments re-read the S3 package after
    a new `spark` (they cache the listing): bump LOOP_PACKAGE_REVISION, which
    recycles them, and invalidate the CDN so edges drop the old segments."""
    function_name = _function_name(outputs)
    client = session.client("lambda")
    variables = dict(client.get_function_configuration(FunctionName=function_name).get("Environment", {}).get("Variables", {}))
    variables["LOOP_PACKAGE_REVISION"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"Recycling execution environments (LOOP_PACKAGE_REVISION={variables['LOOP_PACKAGE_REVISION']}) ...")
    client.update_function_configuration(FunctionName=function_name, Environment={"Variables": variables})
    _wait_updated(client, function_name)
    _invalidate_cdn(session, outputs)
    _print_urls(outputs)


def status(cfg, session, outputs):
    """Prints a human-readable summary and returns a structured dict (see
    channel.py `status --json`). `status` is one of ACTIVE (serving),
    STOPPED (reserved concurrency 0), PENDING (function being created or
    updated), FAILED."""
    function_name = _function_name(outputs)
    client = session.client("lambda")
    function = client.get_function_configuration(FunctionName=function_name)
    reserved = client.get_function_concurrency(FunctionName=function_name).get("ReservedConcurrentExecutions")

    state, last_update = function.get("State"), function.get("LastUpdateStatus")
    if state == "Failed" or last_update == "Failed":
        live = "FAILED"
    elif reserved == 0:
        live = "STOPPED"
    elif state != "Active" or last_update == "InProgress":
        live = "PENDING"
    else:
        live = "ACTIVE"

    hls_url = outputs.get("HlsPlaybackUrl")
    dash_url = outputs.get("DashPlaybackUrl")
    result = {
        "backend": "aws-lambda",
        "function_name": function_name,
        "status": live,
        "reserved_concurrency": reserved,
        "memory": function.get("MemorySize"),
        "hls_url": hls_url,
        "dash_url": dash_url,
        # Only worth network-checking while serving -- see _reachability.py.
        # (Wakes an idle function: one cold start.)
        "reachable": check_manifest_reachable(hls_url, dash_url) if live == "ACTIVE" else None,
    }
    print(f"Function {function_name}: status={live} reservedConcurrency={reserved}"
          + (f" (manifest {'reachable' if result['reachable'] else 'UNREACHABLE'})" if result["reachable"] is not None else ""))
    return result
