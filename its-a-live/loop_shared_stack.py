import aws_cdk as cdk
from aws_cdk import Duration, Stack, aws_ecs as ecs
from constructs import Construct

import _cdn_policies as cdn

# Fixed, shared cluster name for every loop-dee-loop channel (its-a-live's
# "ecs-express" backend). Deployed once via this stack (`cdk deploy
# ItsALiveSharedStack-ecs-express`); LoopStack instances reference this
# name as a plain string -- they do NOT create their own AWS::ECS::Cluster
# resource, since ECS cluster names must be unique per account/region and
# multiple channel stacks each trying to own a resource with the same name
# would collide (and `cdk destroy` on any one channel would risk deleting
# the cluster out from under the others).
CLUSTER_NAME = "loop-dee-loop"

# Prefix of this stack's cache-policy exports (see _cdn_policies.py).
BACKEND = "ecs-express"


class LoopSharedStack(Stack):
    """
    Shared prerequisite for every LoopStack (ecs-express backend channel):
    a single, explicitly named ECS cluster (rather than Express Mode's
    implicit "default" cluster) that all loop-dee-loop channels live in.
    Deploy this once per account/region, before deploying any LoopStack:

        cdk deploy ItsALiveSharedStack-ecs-express

    Per AWS's Express Mode docs, services in the same cluster/networking
    configuration also share the underlying ALB -- so keeping every channel
    in this one named cluster keeps that cost-sharing behavior intact too.

    It also holds the CloudFront cache policies that are identical across
    channels (an account may only have 20; each channel used to own two):
    segments, and manifests for the default `[query]` settings. A channel
    with different query parameter names creates its own manifests policy.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.CfnCluster(self, "Cluster", cluster_name=CLUSTER_NAME)

        cdk.CfnOutput(self, "ClusterName", value=CLUSTER_NAME)
        cdk.CfnOutput(self, "ClusterArn", value=cluster.attr_arn)

        cdn.export_policy(self, BACKEND, "segments",
                          cdn.segment_policy(self, "SegmentCachePolicy", "loop-dee-loop-ecs-segments"))
        # Manifests are dynamic (sliding window) -- never cached (TTL 0), but
        # keyed per value of the query parameters that select their content.
        cdn.export_policy(
            self, BACKEND, "manifests",
            cdn.manifest_policy(self, "ManifestCachePolicy", "loop-dee-loop-ecs-manifests-default",
                                cdn.default_query_names(),
                                default_ttl=Duration.seconds(0), max_ttl=Duration.seconds(1)),
        )
