import aws_cdk as cdk
from aws_cdk import Stack, aws_ecs as ecs
from constructs import Construct

# Fixed, shared cluster name for every loop-dee-loop channel (its-a-live's
# "ecs-express" backend). Deployed once via this stack (`cdk deploy
# ItsALiveSharedStack-ecs-express`); LoopStack instances reference this
# name as a plain string -- they do NOT create their own AWS::ECS::Cluster
# resource, since ECS cluster names must be unique per account/region and
# multiple channel stacks each trying to own a resource with the same name
# would collide (and `cdk destroy` on any one channel would risk deleting
# the cluster out from under the others).
CLUSTER_NAME = "loop-dee-loop"


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
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.CfnCluster(self, "Cluster", cluster_name=CLUSTER_NAME)

        cdk.CfnOutput(self, "ClusterName", value=CLUSTER_NAME)
        cdk.CfnOutput(self, "ClusterArn", value=cluster.attr_arn)
