import os
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    aws_s3 as s3,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_ecs as ecs,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct
from loop_shared_stack import CLUSTER_NAME
from _host_paths import to_host_path
from _epoch_cfg import config_epoch_utc
from _packaging_cfg import period_on_segmentation_serve_args
from _timeshift_cfg import timeshift_param_names, timeshift_serve_args

LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")


class LoopStack(Stack):
    """
    Provisions one loop-dee-loop channel (its-a-live's "ecs-express"
    backend) on ECS Express Mode, fronted by CloudFront:

      loop-dee-loop image (built from ../loop-dee-loop/Dockerfile, pushed to
      a CDK-managed ECR asset repo -- same image serves every channel)
        |
        v
      AWS::ECS::ExpressGatewayService (long-running, auto-scaling `serve.py`)
        |  LOOP_PACKAGE_S3_URI synced down at container start by
        |  docker-entrypoint.sh (see ../loop-dee-loop)
        v
      CloudFront distribution (public entrypoint, caches /seg/*)

    Express Mode is AWS's replacement recommendation for App Runner (which
    stopped onboarding new customers April 30, 2026): it's a simplified
    deployment mode built on standard Fargate + ALB, not a new compute
    primitive -- but it auto-provisions/manages that ALB, target group,
    security groups, SSL, and auto-scaling for you, and (per AWS docs)
    *shares* Application Load Balancers across multiple Express Mode
    services in the same account/networking config to reduce the
    per-channel ALB cost that a hand-built Fargate+ALB stack would otherwise
    multiply by N channels. No VPC lookup is needed here either -- Express
    Mode defaults to the account's default VPC unless network_configuration
    is given.

    `spark` is NOT run in AWS at all: it's a one-shot process meant to run
    once per schedule change, so for this (demo-oriented) stack it's simply
    run locally/via `docker run` and the output pushed to S3 with
    `channel.py spark` (see _ecs_express_ops.py) -- no ECS task
    definition, IAM role, security group, or subnets needed just for that.

    NOTE: this stack does not restrict who can reach the Express service's
    ingress endpoint directly (its ALB DNS name is publicly reachable, same
    as CloudFront) -- acceptable for demos; see README.md for how to close
    that gap with a shared-secret origin header if needed later.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        name = config.get("deploy", {}).get("name", "default")
        bucket_name = config.get("s3", {}).get("bucket_name", "")

        if not bucket_name:
            # During cdk bootstrap no config values are required -- empty stack.
            return

        loop_package_folder = config.get("s3", {}).get("content_folder", "its-a-live/content").strip("/")

        packaging_cfg = config.get("packaging", {})
        dvr_window_seconds = str(packaging_cfg.get("dvr_window_seconds", 30))
        # loop-dee-loop/SCOPE.md §12: default on -- rewrites each segment's
        # own timestamps per request (header patch only, never a re-mux) so
        # the channel has no #EXT-X-DISCONTINUITY / DASH Period restart at
        # the loop wrap. `false` falls back to the honestly-signaled
        # default serve.py behavior (e.g. if a package was baked with a
        # 32-bit tfdt and continuity mode's startup check would refuse it).
        continuous = bool(config.get("timeline", {}).get("continuous", True))

        express_cfg = config.get("express", {})
        port = int(express_cfg.get("port", 8080))
        # Same numeric codes as Fargate (256=0.25 vCPU, 512 MiB, etc.).
        serve_cpu = str(express_cfg.get("cpu", 256))
        serve_memory = str(express_cfg.get("memory", 512))

        loop_package_s3_uri = f"s3://{bucket_name}/{loop_package_folder}/{name}"

        # ── S3 (existing bucket, referenced only) ─────────────────────────────
        bucket = s3.Bucket.from_bucket_name(self, "ExistingBucket", bucket_name)

        # ── Shared image ───────────────────────────────────────────────────────
        # Force linux/amd64: Fargate (and this stack's cpu/memory config)
        # assume x86_64; without this, building on an ARM host (e.g. Apple
        # Silicon) produces an arm64 image that fails at container start with
        # "exec format error".
        #
        # `directory` must be a real path on whatever machine actually runs
        # `docker build` for this asset -- normally that's just wherever `cdk
        # deploy` itself runs, but when `cdk deploy` runs inside igor's
        # Docker-packaged shape (../DOCKER_LOCAL.md), the *host's* daemon
        # does the building via a mounted socket, so this needs the host's
        # own path, not this process's in-container one. to_host_path() is a
        # no-op outside that shape. See _host_paths.py.
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "LoopDeeLoopImage",
            platform=ecr_assets.Platform.LINUX_AMD64,
            directory=to_host_path(os.path.abspath(LOOP_DEE_LOOP_DIR)),
        )

        # ── IAM ────────────────────────────────────────────────────────────────
        # Infrastructure role: what Express Mode itself assumes to provision/
        # manage the ALB, target group, security groups, SSL, and auto-scaling
        # on your behalf (see AWS docs "Amazon ECS infrastructure IAM role").
        infrastructure_role = iam.Role(
            self,
            "ExpressInfrastructureRole",
            assumed_by=iam.ServicePrincipal("ecs.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSInfrastructureRoleforExpressGatewayServices"
                ),
            ],
        )

        # Execution role: standard ECS task execution role (ECR pull, logs).
        execution_role = iam.Role(
            self,
            "ExpressExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )
        image_asset.repository.grant_pull(execution_role)

        # Task role: what the container itself assumes -- read-only S3 access
        # scoped to this channel's loop-package prefix.
        task_role = iam.Role(
            self,
            "ExpressTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/{loop_package_folder}/{name}/*"],
            )
        )

        # ── ECS cluster ────────────────────────────────────────────────────────
        # Referenced by name only -- NOT created here. Every channel shares the
        # one cluster created once by LoopSharedStack (`cdk deploy
        # ItsALiveSharedStack-ecs-express`, before deploying any channel).
        # Creating a cluster per-channel-stack would collide (ECS cluster
        # names are unique per account/region) and would make `cdk destroy`
        # on one channel risk deleting the cluster out from under every
        # other channel.

        # ── Express service (long-running, auto-scaling `serve.py`) ──────────
        # --epoch-utc comes from [timeline] epoch_utc (default
        # 2026-01-01T00:00:00Z -- recent enough to keep loop numbers small;
        # the Unix epoch gave ~1.8e9-sized ones) and is then left alone --
        # this is looping content simulating live, not a real broadcast start
        # time, so there's no need to reset loop position to 0 on every
        # (re)start; landing mid-ad-break on start/restart is acceptable.
        # This also means `channel.py start` never needs to touch
        # primaryContainer (just scalingTarget), so it's a fast scale-only
        # operation rather than a new task revision/canary deployment.
        service = ecs.CfnExpressGatewayService(
            self,
            "ServeService",
            service_name=f"loop-dee-loop-{name}-serve",
            cluster=CLUSTER_NAME,
            infrastructure_role_arn=infrastructure_role.role_arn,
            execution_role_arn=execution_role.role_arn,
            task_role_arn=task_role.role_arn,
            cpu=serve_cpu,
            memory=serve_memory,
            health_check_path="/stream.mpd",
            primary_container=ecs.CfnExpressGatewayService.ExpressGatewayContainerProperty(
                image=image_asset.image_uri,
                container_port=port,
                command=[
                    "serve.py",
                    "--host", "0.0.0.0",
                    "--port", str(port),
                    "--dvr-window-seconds", dvr_window_seconds,
                    "--epoch-utc", config_epoch_utc(config),
                    *(["--continuous-timeline"] if continuous else []),
                    *period_on_segmentation_serve_args(config),
                    *timeshift_serve_args(config),
                ],
                environment=[
                    ecs.CfnExpressGatewayService.KeyValuePairProperty(
                        name="LOOP_PACKAGE_S3_URI", value=loop_package_s3_uri,
                    ),
                ],
            ),
        )

        # ── CloudFront: public entrypoint, caches /seg/* aggressively ────────
        # Manifests are dynamic (sliding window) -- never cached. Segments are
        # immutable within a loop package version -- cache them (SCOPE.md §8).
        # Startover/catchup (loop-dee-loop/SCOPE.md §13) selects the range via
        # query parameters on the manifest URLs, so manifests MUST be cached
        # per value of exactly those parameters -- CloudFront's default is to
        # drop the query string entirely, which would serve one viewer's range
        # to everyone. Nothing else in the query string is keyed or forwarded.
        timeshift_params = timeshift_param_names(config)
        manifest_query_strings = (
            cloudfront.CacheQueryStringBehavior.allow_list(*timeshift_params)
            if timeshift_params
            else cloudfront.CacheQueryStringBehavior.none()
        )
        manifest_cache_policy = cloudfront.CachePolicy(
            self,
            "ManifestCachePolicy",
            query_string_behavior=manifest_query_strings,
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
            service.attr_endpoint,
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
            # seg = loop-local, cseg = continuous (global index), rseg =
            # range-relative TS (SCOPE.md §13.5): all immutable per URL.
            additional_behaviors={
                pattern: cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=segment_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                )
                for pattern in ("*/seg/*", "*/cseg/*", "*/rseg/*")
            },
        )

        # ── CloudFormation outputs ─────────────────────────────────────────────
        cdk.CfnOutput(self, "S3BucketName", value=bucket_name)
        cdk.CfnOutput(self, "LoopPackageS3Uri", value=loop_package_s3_uri)
        cdk.CfnOutput(self, "ExpressServiceArn", value=service.attr_service_arn)
        cdk.CfnOutput(self, "ExpressServiceEndpoint", value=f"https://{service.attr_endpoint}")
        cdk.CfnOutput(self, "CloudFrontDomainName", value=distribution.distribution_domain_name)
        cdk.CfnOutput(self, "HlsPlaybackUrl", value=f"https://{distribution.distribution_domain_name}/index.m3u8")
        cdk.CfnOutput(self, "DashPlaybackUrl", value=f"https://{distribution.distribution_domain_name}/stream.mpd")
