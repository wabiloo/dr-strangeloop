import os
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    Size,
    aws_s3 as s3,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct
from _host_paths import to_host_path
from _epoch_cfg import config_epoch_utc
from _infra_cfg import infra_table
from _markers_cfg import split_on_serve_args
from _packaging_cfg import dash_serve_args, dvr_window_seconds as config_dvr_window_seconds
import _cdn_policies as cdn
from _query_cfg import query_param_names, query_serve_args

LOOP_DEE_LOOP_DIR = os.path.join(os.path.dirname(__file__), "..", "loop-dee-loop")

# Stack that owns the shared cache policies (lambda_shared_stack.py).
SHARED_BACKEND = "aws-lambda"

# Lambda's ephemeral storage is 512..10240 MB.
_MIN_EPHEMERAL_MB, _MAX_EPHEMERAL_MB = 512, 10240


class LambdaLoopStack(Stack):
    """
    Provisions one loop-dee-loop channel (its-a-live's "aws-lambda" backend):
    `serve.py` in a Lambda function, fronted by CloudFront. See
    LAMBDA_BACKEND.md for the full design.

      CloudFront (caches /seg /cseg /rseg + init segments; manifests 1 s)
        |  Origin Access Control (SigV4)
        v
      Lambda Function URL (AWS_IAM, RESPONSE_STREAM)
        |  container image = ../loop-dee-loop/Dockerfile.lambda
        |  (Lambda Web Adapter + gunicorn, unchanged serve.py)
        v
      S3 loop package (written by `channel.py spark`, listed at cold start,
      segment bytes fetched on demand -- loop-dee-loop/s3_package.py)

    No cluster, no ALB, no idle cost. Unlike the ecs-express service, the
    origin is not publicly reachable: only this distribution may invoke the
    function URL. Every request (manifests AND segments) goes through
    Lambda, because continuous-timeline mode rewrites segment timestamps per
    request; CloudFront caching keeps that to ~one invocation per unique
    segment per cache.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        name = config.get("deploy", {}).get("name", "default")
        bucket_name = infra_table(config, "s3").get("bucket_name", "")

        if not bucket_name:
            # During cdk bootstrap no config values are required -- empty stack.
            return

        loop_package_folder = infra_table(config, "s3").get("content_folder", "its-a-live/content").strip("/")
        loop_package_s3_uri = f"s3://{bucket_name}/{loop_package_folder}/{name}"

        lambda_cfg = infra_table(config, "lambda")
        memory = int(lambda_cfg.get("memory", 1024))
        timeout = int(lambda_cfg.get("timeout", 30))
        # -1 = no reservation (accounts whose concurrency quota is too small to reserve any).
        reserved_concurrency = int(lambda_cfg.get("reserved_concurrency", 10))
        cache_mb = int(lambda_cfg.get("cache_mb", 512))
        architecture = str(lambda_cfg.get("architecture", "arm64"))
        if architecture not in ("arm64", "x86_64"):
            raise ValueError(f"[infrastructure.lambda] architecture must be 'arm64' or 'x86_64', got {architecture!r}")
        if reserved_concurrency == 0 or reserved_concurrency < -1:
            raise ValueError(
                "[infrastructure.lambda] reserved_concurrency must be >= 1 (or -1 for none); "
                "0 is what `channel.py stop` sets, not a configuration"
            )
        ephemeral_mb = max(_MIN_EPHEMERAL_MB, cache_mb + 128)
        if ephemeral_mb > _MAX_EPHEMERAL_MB:
            raise ValueError(f"[infrastructure.lambda] cache_mb {cache_mb} exceeds Lambda's ephemeral storage limit")

        dvr_window_seconds = config_dvr_window_seconds(config)
        # See loop_stack.py / loop-dee-loop/SCOPE.md §12.
        continuous = bool(config.get("timeline", {}).get("continuous", True))
        port = 8080

        bucket = s3.Bucket.from_bucket_name(self, "ExistingBucket", bucket_name)

        # ── Image ──────────────────────────────────────────────────────────────
        # Serve-only (no GPAC/ffmpeg), same build context as the ECS image.
        # `directory` must be a path the docker daemon doing the build can see
        # (see loop_stack.py / _host_paths.py).
        image_code = lambda_.DockerImageCode.from_image_asset(
            to_host_path(os.path.abspath(LOOP_DEE_LOOP_DIR)),
            file="Dockerfile.lambda",
            platform=ecr_assets.Platform.LINUX_ARM64 if architecture == "arm64" else ecr_assets.Platform.LINUX_AMD64,
            cmd=[
                "serve.py",
                "--host", "0.0.0.0",
                "--port", str(port),
                "--dvr-window-seconds", dvr_window_seconds,
                "--epoch-utc", config_epoch_utc(config),
                "--channel-name", name,
                *(["--continuous-timeline"] if continuous else []),
                *split_on_serve_args(config),
                *dash_serve_args(config),
                *query_serve_args(config),
            ],
        )

        # ── IAM ────────────────────────────────────────────────────────────────
        # Read-only S3 access scoped to this channel's loop-package prefix.
        # Inlined in the role (not add_to_policy) so no separate AWS::IAM::Policy
        # sits on the create path, as in loop_stack.py.
        role = iam.Role(
            self,
            "ServeRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
            inline_policies={
                "LoopPackageRead": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["s3:GetObject"],
                            resources=[f"{bucket.bucket_arn}/{loop_package_folder}/{name}/*"],
                        ),
                        iam.PolicyStatement(
                            actions=["s3:ListBucket"],
                            resources=[bucket.bucket_arn],
                            conditions={"StringLike": {"s3:prefix": [f"{loop_package_folder}/{name}/*"]}},
                        ),
                    ]
                )
            },
        )

        # ── Function ───────────────────────────────────────────────────────────
        function = lambda_.DockerImageFunction(
            self,
            "ServeFunction",
            function_name=f"loop-dee-loop-{name}-serve",
            code=image_code,
            architecture=lambda_.Architecture.ARM_64 if architecture == "arm64" else lambda_.Architecture.X86_64,
            memory_size=memory,
            timeout=Duration.seconds(timeout),
            ephemeral_storage_size=Size.mebibytes(ephemeral_mb),
            reserved_concurrent_executions=reserved_concurrency if reserved_concurrency > 0 else None,
            role=role,
            log_group=logs.LogGroup(
                self,
                "ServeLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
            environment={
                "LOOP_PACKAGE_S3_URI": loop_package_s3_uri,
                "LOOP_PACKAGE_STORE": "s3",
                "LOOP_PACKAGE_LOCAL_DIR": "/tmp/loop-package",
                "SEGMENT_CACHE_MB": str(cache_mb),
                # What `channel.py start` (also run by the scheduler Lambda,
                # without a config) restores after `stop` set it to 0.
                "RESERVED_CONCURRENCY": str(reserved_concurrency),
                # `channel.py refresh` bumps this to recycle every execution
                # environment after a re-spark (they cache the S3 listing).
                "LOOP_PACKAGE_REVISION": "0",
            },
        )
        function_url = function.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )

        # ── CloudFront: the only public entrypoint ─────────────────────────────
        # Cache policies come from ItsALiveSharedStack-aws-lambda (an account may
        # only have 20): segments and status are the same for every channel; the
        # manifests policy is shared too unless this channel's `[query]` parameter
        # names (the manifest cache key) differ from the default.
        segment_cache_policy = cdn.import_policy(self, "SegmentCachePolicy", SHARED_BACKEND, "segments")
        status_cache_policy = cdn.import_policy(self, "StatusCachePolicy", SHARED_BACKEND, "status")
        if cdn.uses_default_query(config):
            manifest_cache_policy = cdn.import_policy(self, "ManifestCachePolicy", SHARED_BACKEND, "manifests")
        else:
            manifest_cache_policy = cdn.manifest_policy(
                self, "ManifestCachePolicy", f"loop-dee-loop-{name}-manifests", query_param_names(config),
                default_ttl=Duration.seconds(1), max_ttl=Duration.days(365),
            )

        origin = origins.FunctionUrlOrigin.with_origin_access_control(function_url)

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"loop-dee-loop channel (lambda): {name}",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=manifest_cache_policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            ),
            # seg = loop-local, cseg = continuous (global index), rseg =
            # range-relative TS: all immutable per URL. Init segments are
            # immutable too (the ECS stack leaves them uncached).
            additional_behaviors={
                pattern: cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=segment_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                )
                for pattern in ("*/seg/*", "*/cseg/*", "*/rseg/*", "*/init*.mp4")
            } | {
                pattern: cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=status_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                )
                for pattern in ("/timeline.json", "/health")
            },
        )
        playback_host = distribution.distribution_domain_name

        # The OAC origin grants lambda:InvokeFunctionUrl; function URLs also
        # need lambda:InvokeFunction for the same principal (AWS requires
        # both for URLs created from late 2025). Redundant where the CDK
        # version already adds it.
        function.add_permission(
            "CloudFrontInvokeFunction",
            principal=iam.ServicePrincipal("cloudfront.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=f"arn:{self.partition}:cloudfront::{self.account}:distribution/{distribution.distribution_id}",
        )

        # ── CloudFormation outputs ─────────────────────────────────────────────
        cdk.CfnOutput(self, "S3BucketName", value=bucket_name)
        cdk.CfnOutput(self, "LoopPackageS3Uri", value=loop_package_s3_uri)
        cdk.CfnOutput(self, "LambdaFunctionName", value=function.function_name)
        cdk.CfnOutput(self, "LambdaFunctionUrl", value=function_url.url)
        cdk.CfnOutput(self, "CloudFrontDomainName", value=distribution.distribution_domain_name)
        cdk.CfnOutput(self, "CloudFrontDistributionId", value=distribution.distribution_id)
        cdk.CfnOutput(self, "HlsPlaybackUrl", value=f"https://{playback_host}/index.m3u8")
        cdk.CfnOutput(self, "DashPlaybackUrl", value=f"https://{playback_host}/index.mpd")
