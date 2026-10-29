"""CloudFront cache policies shared by every channel of a backend.

An account is limited to 20 cache policies (per region-less CloudFront), so
channels must not each own the ones that are identical for all of them. The
segment and status policies never vary; the manifest policy varies only with
the channel's `[query]` settings (its parameter names are the cache key), so
the shared stack holds the one for the DEFAULT `[query]` and a channel whose
names differ creates its own. Shared stacks: lambda_shared_stack.py
(aws-lambda) and loop_shared_stack.py (ecs-express); channel stacks import by
CloudFormation export.
"""

import aws_cdk as cdk
from aws_cdk import Duration, Fn, aws_cloudfront as cloudfront
from constructs import Construct

from _query_cfg import CLOUDFRONT_MAX_ALLOW_LIST, query_param_names

KINDS = ("segments", "status", "manifests")


def export_name(backend: str, kind: str) -> str:
    return f"ItsALive-{backend}-{kind}-CachePolicyId"


def default_query_names() -> list[str]:
    """Names a channel with no `[query]` table keys on."""
    return query_param_names({})


def uses_default_query(config: dict) -> bool:
    return sorted(query_param_names(config)) == sorted(default_query_names())


def _query_strings(names: list[str]) -> cloudfront.CacheQueryStringBehavior:
    if not names:
        return cloudfront.CacheQueryStringBehavior.none()
    if len(names) > CLOUDFRONT_MAX_ALLOW_LIST:
        return cloudfront.CacheQueryStringBehavior.all()
    return cloudfront.CacheQueryStringBehavior.allow_list(*names)


def segment_policy(scope: Construct, construct_id: str, policy_name: str) -> cloudfront.CachePolicy:
    """Segments are immutable per URL within a loop package version."""
    return cloudfront.CachePolicy(
        scope,
        construct_id,
        cache_policy_name=policy_name,
        default_ttl=Duration.minutes(5),
        min_ttl=Duration.seconds(0),
        max_ttl=Duration.hours(1),
        enable_accept_encoding_gzip=True,
        enable_accept_encoding_brotli=True,
    )


def manifest_policy(
    scope: Construct,
    construct_id: str,
    policy_name: str,
    names: list[str],
    *,
    default_ttl: Duration,
    max_ttl: Duration,
) -> cloudfront.CachePolicy:
    """Manifests are cached per value of exactly the query parameters that
    select their content (startover/catchup and the per-request overrides,
    loop-dee-loop/SCOPE.md §13); nothing else is keyed or forwarded."""
    return cloudfront.CachePolicy(
        scope,
        construct_id,
        cache_policy_name=policy_name,
        query_string_behavior=_query_strings(names),
        default_ttl=default_ttl,
        min_ttl=Duration.seconds(0),
        max_ttl=max_ttl,
        enable_accept_encoding_gzip=True,
        enable_accept_encoding_brotli=True,
    )


def status_policy(scope: Construct, construct_id: str, policy_name: str) -> cloudfront.CachePolicy:
    """/timeline.json and /health take free-form query parameters that the
    manifest allow-list would drop: forward every one, cache nothing."""
    return cloudfront.CachePolicy(
        scope,
        construct_id,
        cache_policy_name=policy_name,
        query_string_behavior=cloudfront.CacheQueryStringBehavior.all(),
        default_ttl=Duration.seconds(0),
        min_ttl=Duration.seconds(0),
        max_ttl=Duration.seconds(1),
    )


def export_policy(scope: Construct, backend: str, kind: str, policy: cloudfront.CachePolicy) -> None:
    cdk.CfnOutput(
        scope,
        f"{kind.capitalize()}CachePolicyId",
        value=policy.cache_policy_id,
        export_name=export_name(backend, kind),
    )


def import_policy(scope: Construct, construct_id: str, backend: str, kind: str) -> cloudfront.ICachePolicy:
    return cloudfront.CachePolicy.from_cache_policy_id(
        scope, construct_id, Fn.import_value(export_name(backend, kind))
    )
