import os
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    aws_s3 as s3,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_apprunner as apprunner,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct

LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")


class LoopChannelStack(Stack):
    """
    Provisions one loop-dee-loop channel on App Runner, fronted by CloudFront:

      loop-dee-loop image (built from ../loop-dee-loop/Dockerfile, pushed to
      a CDK-managed ECR asset repo -- same image serves every channel)
        |
        v
      App Runner service (long-running, auto-scaling `serve.py`)
        |  LOOP_PACKAGE_S3_URI synced down at container start by
        |  docker-entrypoint.sh (see ../loop-dee-loop)
        v
      CloudFront distribution (public entrypoint, caches /seg/*)

    No ALB, no VPC, no ECS cluster -- App Runner's own price bundles the
    ingress/load-balancing layer (see cost discussion in chat history), and
    it has native pause/resume for stopping a channel between airings.

    `bake` is NOT run in AWS at all: it's a one-shot process meant to run
    once per schedule change, so for this (demo-oriented) stack it's simply
    run locally/via `docker run` and the output pushed to S3 with
    `channel.py bake` (see channel.py) -- no ECS task definition, IAM role,
    security group, or subnets needed just for that.

    NOTE: this stack does not restrict who can reach the App Runner service
    directly (its default *.awsapprunner.com domain is publicly reachable,
    same as CloudFront) -- acceptable for demos; see README.md for how to
    close that gap with a shared-secret origin header if needed later.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        name = config.get("deploy", {}).get("name", "default")
        bucket_name = config.get("s3", {}).get("bucket_name", "")

        if not bucket_name:
            # During cdk bootstrap no config values are required -- empty stack.
            return

        loop_package_folder = config.get("s3", {}).get("loop_package_folder", "loop-dee-loop/packages").strip("/")

        channel_cfg = config.get("channel", {})
        dvr_window_seconds = str(channel_cfg.get("dvr_window_seconds", 30))
        port = int(channel_cfg.get("port", 8080))

        apprunner_cfg = config.get("apprunner", {})
        # App Runner CPU/memory use the same numeric codes as Fargate
        # (256=0.25 vCPU, 512 MiB, etc.) -- see AWS App Runner docs for the
        # full supported-configuration list.
        serve_cpu = str(apprunner_cfg.get("cpu", 256))
        serve_memory = str(apprunner_cfg.get("memory", 512))

        loop_package_s3_uri = f"s3://{bucket_name}/{loop_package_folder}/{name}"

        # ── S3 (existing bucket, referenced only) ─────────────────────────────
        bucket = s3.Bucket.from_bucket_name(self, "ExistingBucket", bucket_name)

        # ── Shared image ───────────────────────────────────────────────────────
        # Force linux/amd64: App Runner (and this stack's InstanceConfiguration)
        # assume x86_64; without this, building on an ARM host (e.g. Apple
        # Silicon) produces an arm64 image that fails at container start with
        # "exec format error".
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "LoopDeeLoopImage",
            platform=ecr_assets.Platform.LINUX_AMD64,
            directory=os.path.abspath(LOOP_DEE_LOOP_DIR),
        )

        # ── IAM: App Runner's own ECR-pull role + the container's instance role ─
        access_role = iam.Role(
            self,
            "AppRunnerEcrAccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
        )
        image_asset.repository.grant_pull(access_role)

        instance_role = iam.Role(
            self,
            "AppRunnerInstanceRole",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )
        instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/{loop_package_folder}/{name}/*"],
            )
        )

        # ── App Runner service (long-running, auto-scaling `serve.py`) ────────
        # epoch-utc is a placeholder here -- `channel.py start` updates the
        # service with the current UTC timestamp each time the channel is
        # (re)started, same idea as the previous ECS task-def-revision trick.
        service = apprunner.CfnService(
            self,
            "ServeService",
            service_name=f"loop-dee-loop-{name}-serve",
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                auto_deployments_enabled=False,
                authentication_configuration=apprunner.CfnService.AuthenticationConfigurationProperty(
                    access_role_arn=access_role.role_arn,
                ),
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=image_asset.image_uri,
                    image_repository_type="ECR",
                    image_configuration=apprunner.CfnService.ImageConfigurationProperty(
                        port=str(port),
                        start_command=(
                            "serve.py --host 0.0.0.0 "
                            f"--port {port} "
                            f"--dvr-window-seconds {dvr_window_seconds} "
                            "--epoch-utc 1970-01-01T00:00:00Z"
                        ),
                        runtime_environment_variables=[
                            apprunner.CfnService.KeyValuePairProperty(
                                name="LOOP_PACKAGE_S3_URI", value=loop_package_s3_uri,
                            ),
                        ],
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu=serve_cpu,
                memory=serve_memory,
                instance_role_arn=instance_role.role_arn,
            ),
        )

        # ── CloudFront: public entrypoint, caches /seg/* aggressively ────────
        # Manifests are dynamic (sliding window) -- never cached. Segments are
        # immutable within a loop package version -- cache them (SCOPE.md §8).
        manifest_cache_policy = cloudfront.CachePolicy(
            self,
            "ManifestCachePolicy",
            cache_policy_name=f"loop-dee-loop-{name}-manifests",
            default_ttl=Duration.seconds(0),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(1),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )
        segment_cache_policy = cloudfront.CachePolicy(
            self,
            "SegmentCachePolicy",
            cache_policy_name=f"loop-dee-loop-{name}-segments",
            default_ttl=Duration.minutes(5),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.hours(1),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )

        origin = origins.HttpOrigin(
            service.attr_service_url,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"loop-dee-loop channel: {name}",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=manifest_cache_policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            ),
            additional_behaviors={
                "*/seg/*": cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=segment_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                ),
            },
        )

        # ── CloudFormation outputs ─────────────────────────────────────────────
        cdk.CfnOutput(self, "S3BucketName", value=bucket_name)
        cdk.CfnOutput(self, "LoopPackageS3Uri", value=loop_package_s3_uri)
        cdk.CfnOutput(self, "AppRunnerServiceArn", value=service.attr_service_arn)
        cdk.CfnOutput(self, "AppRunnerServiceUrl", value=f"https://{service.attr_service_url}")
        cdk.CfnOutput(self, "CloudFrontDomainName", value=distribution.distribution_domain_name)
        cdk.CfnOutput(self, "HlsPlaybackUrl", value=f"https://{distribution.distribution_domain_name}/master.m3u8")
        cdk.CfnOutput(self, "DashPlaybackUrl", value=f"https://{distribution.distribution_domain_name}/manifest.mpd")
