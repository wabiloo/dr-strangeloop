import os
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as _lambda,
    aws_iam as iam,
)
from constructs import Construct

ITS_A_LIVE_DIR = os.path.dirname(__file__)


class SchedulerStack(Stack):
    """
    Shared prerequisite for scheduled channel start/stop
    (`channel.py schedule add/remove/list`): one Lambda function, reused
    by every channel's EventBridge Scheduler one-time schedules, plus the
    IAM role EventBridge Scheduler assumes to invoke it. Deploy once per
    account/region, independent of any channel:

        cdk deploy ItsALiveSharedStack-scheduler

    Not backend-specific -- covers both aws-media and ecs-express
    channels (local-docker has no AWS presence to schedule against, so
    `channel.py schedule` refuses it outright rather than needing
    anything from this stack).

    The actual per-channel schedules (one EventBridge Scheduler schedule
    per window edge that needs to fire in the future) are created/deleted
    at runtime by _scheduler_ops.py via `channel.py schedule add/remove`
    -- NOT by this stack. This stack only provisions the reusable
    target Lambda + invocation role.

    The Lambda's code asset is this whole its-a-live/ directory (see
    _scheduler_lambda.py): it imports _aws_media_ops.start/stop and
    _ecs_express_ops.start/stop directly -- the exact same functions
    channel.py itself calls -- rather than reimplementing the MediaLive/
    ECS API calls a second time. The CDK/CLI-only files that come along
    for the ride (app.py, *_stack.py, channel.py, ...) are simply never
    imported at runtime; excluded below only to keep the deployed asset
    smaller, not because importing them would break anything.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        function_role = iam.Role(
            self,
            "SchedulerFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        # Stack/channel identity isn't known until the event fires (a
        # given schedule can outlive/predate a channel redeploy), so this
        # can't be scoped down to specific channel stack ARNs at synth
        # time -- see _scheduler_lambda.py's fire-time outputs lookup.
        function_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=["*"],
            )
        )
        function_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "medialive:DescribeChannel",
                    "medialive:StartChannel",
                    "medialive:StopChannel",
                ],
                resources=["*"],
            )
        )
        function_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:DescribeExpressGatewayService",
                    "ecs:UpdateExpressGatewayService",
                ],
                resources=["*"],
            )
        )

        runtime = _lambda.Runtime.PYTHON_3_12
        function = _lambda.Function(
            self,
            "SchedulerFunction",
            function_name="its-a-live-scheduler",
            runtime=runtime,
            handler="_scheduler_lambda.handler",
            role=function_role,
            timeout=Duration.seconds(120),
            code=_lambda.Code.from_asset(
                ITS_A_LIVE_DIR,
                exclude=[
                    ".venv", "cdk.out", "__pycache__", "*.pyc", "node_modules",
                    "configs", "data", ".git", "tests",
                ],
                # `ecs:UpdateExpressGatewayService`/`DescribeExpressGatewayService`
                # are recent enough that the Lambda runtime's own bundled
                # boto3 isn't guaranteed to know them -- pip-install a fresh
                # boto3 alongside the source rather than relying on the
                # runtime's version. Needs Docker at `cdk deploy` time, same
                # prerequisite LoopStack's image_asset already has.
                bundling=cdk.BundlingOptions(
                    image=runtime.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install boto3>=1.28.0 -t /asset-output && cp -au . /asset-output",
                    ],
                ),
            ),
        )

        # What EventBridge Scheduler itself assumes to invoke the Lambda
        # target -- distinct from the Lambda's own execution role above.
        scheduler_invocation_role = iam.Role(
            self,
            "SchedulerInvocationRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        function.grant_invoke(scheduler_invocation_role)

        cdk.CfnOutput(self, "SchedulerFunctionArn", value=function.function_arn)
        cdk.CfnOutput(self, "SchedulerInvocationRoleArn", value=scheduler_invocation_role.role_arn)
