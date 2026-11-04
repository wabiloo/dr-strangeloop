from aws_cdk import Duration, Stack
from constructs import Construct

import _cdn_policies as cdn

BACKEND = "aws-lambda"


class LambdaSharedStack(Stack):
    """
    Shared prerequisite for every LambdaLoopStack (aws-lambda backend
    channel): the CloudFront cache policies that are identical across
    channels (an account may only have 20, and each channel used to own
    three). Deploy once per account/region, before any channel -- `channel.py
    create` does it for you:

        cdk deploy ItsALiveSharedStack-aws-lambda

    Holds the segments and status policies and the manifests policy for the
    default `[query]` settings; a channel with different query parameter names
    creates its own manifests policy (lambda_stack.py).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cdn.export_policy(self, BACKEND, "segments",
                          cdn.segment_policy(self, "SegmentCachePolicy", "loop-dee-loop-lambda-segments"))
        cdn.export_policy(self, BACKEND, "status",
                          cdn.status_policy(self, "StatusCachePolicy", "loop-dee-loop-lambda-status"))
        # 1 s so CloudFront collapses concurrent identical requests; a response
        # with its own Cache-Control (time-shift ranges) is honoured, hence the high max.
        cdn.export_policy(
            self, BACKEND, "manifests",
            cdn.manifest_policy(self, "ManifestCachePolicy", "loop-dee-loop-lambda-manifests-default",
                                cdn.default_query_names(),
                                default_ttl=Duration.seconds(1), max_ttl=Duration.days(365)),
        )
