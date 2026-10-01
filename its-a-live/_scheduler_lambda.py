"""EventBridge Scheduler target for scheduled channel start/stop.

Deployed as the Lambda function in scheduler_stack.py (`cdk deploy -c scheduler=true
ItsALiveSharedStack-scheduler`, once per account/region). One shared
function, reused by every channel's one-time EventBridge Scheduler
schedules created via `channel.py schedule add` (see _scheduler_ops.py).

Resolves the channel's current CloudFormation stack outputs at FIRE time
(not baked into the schedule's target input) so a channel redeploy that
happens between scheduling and firing doesn't invoke a stale
MediaLiveChannelId/ExpressServiceArn -- then calls the exact same
start()/stop() functions channel.py itself calls (_aws_media_ops.py /
_ecs_express_ops.py), so there is exactly one implementation of what
"start"/"stop" means per backend, not a second one reimplemented here.
"""

import boto3

import _aws_media_ops
import _ecs_express_ops

_OPS = {
    "aws-media": _aws_media_ops,
    "ecs-express": _ecs_express_ops,
}


def handler(event, context):
    """event: {"channel_name": ..., "backend": "aws-media"|"ecs-express",
    "action": "start"|"stop", "region": ...} -- see _scheduler_ops.py's
    add_window() for who sets this as the EventBridge Scheduler target
    input."""
    channel_name = event["channel_name"]
    backend = event["backend"]
    action = event["action"]
    region = event["region"]

    ops = _OPS.get(backend)
    if ops is None:
        raise ValueError(f"Unsupported backend for scheduled start/stop: {backend!r}")
    if action not in ("start", "stop"):
        raise ValueError(f"Unsupported scheduled action: {action!r}")

    session = boto3.session.Session(region_name=region)
    cf = session.client("cloudformation")
    stack_name = f"ItsALiveStack-{channel_name}-{backend}"
    resp = cf.describe_stacks(StackName=stack_name)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}

    fn = ops.start if action == "start" else ops.stop
    print(f"Scheduled {action} firing for channel {channel_name!r} ({backend}) ...")
    fn({}, session, outputs)
    print(f"Scheduled {action} for channel {channel_name!r} complete.")
