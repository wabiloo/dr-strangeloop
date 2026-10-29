"""its-a-live's aws-lambda backend (its-a-live/LAMBDA_BACKEND.md): the ops
module against a fake boto session, and -- when aws-cdk-lib is installed
(its-a-live's own venv) -- the synthesized stack."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ITS_A_LIVE = REPO / "its-a-live"
sys.path.insert(0, str(ITS_A_LIVE))

import _aws_lambda_ops as ops  # noqa: E402

OUTPUTS = {
    "LambdaFunctionName": "loop-dee-loop-x-serve",
    "CloudFrontDistributionId": "EDIST",
    "HlsPlaybackUrl": "https://d.example/index.m3u8",
    "DashPlaybackUrl": "https://d.example/index.mpd",
}


class FakeLambda:
    def __init__(self, *, state="Active", last_update="Successful", reserved=10, env=None, command=None):
        self.config = {
            "State": state,
            "LastUpdateStatus": last_update,
            "MemorySize": 1024,
            "Environment": {"Variables": dict(env or {"RESERVED_CONCURRENCY": "7", "LOOP_PACKAGE_REVISION": "0"})},
            "ImageConfigResponse": {"ImageConfig": {"Command": command or ["serve.py", "--epoch-utc", "2026-01-01T00:00:00Z"]}},
        }
        self.reserved = reserved
        self.calls: list[tuple[str, dict]] = []

    def get_function_configuration(self, FunctionName):
        return self.config

    def get_function_concurrency(self, FunctionName):
        return {} if self.reserved is None else {"ReservedConcurrentExecutions": self.reserved}

    def put_function_concurrency(self, **kw):
        self.calls.append(("put_function_concurrency", kw))
        self.reserved = kw["ReservedConcurrentExecutions"]

    def delete_function_concurrency(self, **kw):
        self.calls.append(("delete_function_concurrency", kw))
        self.reserved = None

    def update_function_configuration(self, **kw):
        self.calls.append(("update_function_configuration", kw))

    def get_waiter(self, name):
        assert name == "function_updated_v2"
        return type("W", (), {"wait": lambda self, **kw: None})()


class FakeCloudFront:
    def __init__(self):
        self.invalidations = []

    def create_invalidation(self, **kw):
        self.invalidations.append(kw)


class FakeSession:
    def __init__(self, fn, cf=None):
        self.clients = {"lambda": fn, "cloudfront": cf or FakeCloudFront()}

    def client(self, name):
        return self.clients[name]


def test_stop_throttles_everything():
    fn = FakeLambda()
    ops.stop({}, FakeSession(fn), OUTPUTS)
    assert fn.calls == [("put_function_concurrency", {"FunctionName": "loop-dee-loop-x-serve", "ReservedConcurrentExecutions": 0})]


def test_start_restores_the_cap_recorded_on_the_function_when_called_without_a_config():
    """The scheduler Lambda calls start({}, ...)."""
    fn = FakeLambda(reserved=0)
    ops.start({}, FakeSession(fn), OUTPUTS)
    assert fn.calls[-1][1]["ReservedConcurrentExecutions"] == 7


def test_start_prefers_the_config_and_minus_one_removes_the_reservation():
    fn = FakeLambda(reserved=0)
    ops.start({"infrastructure": {"lambda": {"reserved_concurrency": 3}}}, FakeSession(fn), OUTPUTS)
    assert fn.calls[-1][1]["ReservedConcurrentExecutions"] == 3
    ops.start({"infrastructure": {"lambda": {"reserved_concurrency": -1}}}, FakeSession(fn), OUTPUTS)
    assert fn.calls[-1][0] == "delete_function_concurrency"


def test_start_with_epoch_patches_the_serve_command_and_invalidates_the_cdn():
    fn, cf = FakeLambda(), FakeCloudFront()
    ops.start({}, FakeSession(fn, cf), OUTPUTS, ["--epoch-utc", "2027-02-03T04:05:06Z"])
    update = next(kw for name, kw in fn.calls if name == "update_function_configuration")
    assert update["ImageConfig"]["Command"] == ["serve.py", "--epoch-utc", "2027-02-03T04:05:06Z"]
    assert cf.invalidations and cf.invalidations[0]["DistributionId"] == "EDIST"


def test_start_rejects_other_arguments():
    with pytest.raises(SystemExit):
        ops.start({}, FakeSession(FakeLambda()), OUTPUTS, ["--bogus"])


def test_refresh_bumps_the_revision_keeping_the_rest_of_the_environment():
    fn, cf = FakeLambda(), FakeCloudFront()
    ops.refresh({}, FakeSession(fn, cf), OUTPUTS)
    env = next(kw for name, kw in fn.calls if name == "update_function_configuration")["Environment"]["Variables"]
    assert env["LOOP_PACKAGE_REVISION"] != "0" and env["RESERVED_CONCURRENCY"] == "7"
    assert len(cf.invalidations) == 1


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, "ACTIVE"),
        ({"reserved": None}, "ACTIVE"),
        ({"reserved": 0}, "STOPPED"),
        ({"state": "Pending"}, "PENDING"),
        ({"last_update": "InProgress"}, "PENDING"),
        ({"last_update": "Failed"}, "FAILED"),
        ({"state": "Failed", "reserved": 0}, "FAILED"),
    ],
)
def test_status_vocabulary(monkeypatch, kwargs, expected):
    checked = []
    monkeypatch.setattr(ops, "check_manifest_reachable", lambda h, d: checked.append((h, d)) or True)
    result = ops.status({}, FakeSession(FakeLambda(**kwargs)), OUTPUTS)
    assert result["status"] == expected and result["backend"] == "aws-lambda"
    # only a serving channel is network-checked (that would wake an idle function)
    assert (result["reachable"] is not None) == (expected == "ACTIVE") == bool(checked)


def test_missing_output_is_a_clear_error():
    with pytest.raises(SystemExit, match="LambdaFunctionName"):
        ops.stop({}, FakeSession(FakeLambda()), {})


# ── stack ────────────────────────────────────────────────────────────────────

CONFIG = """
config_version = 2
[deploy]
name = "t"
backend = "aws-lambda"
[infrastructure.aws]
region = "eu-west-1"
[infrastructure.s3]
bucket_name = "bkt"
content_folder = "f"
[infrastructure.lambda]
reserved_concurrency = 4
cache_mb = 256
"""


def _synth(config_text: str) -> dict:
    cdk = pytest.importorskip("aws_cdk")
    import aws_cdk.assertions as assertions
    from lambda_stack import LambdaLoopStack

    app = cdk.App()
    stack = LambdaLoopStack(
        app, "S", config=tomllib.loads(config_text),
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )
    return assertions.Template.from_stack(stack).to_json()["Resources"]


def _of_type(resources, type_):
    return [r["Properties"] for r in resources.values() if r["Type"] == type_]


def test_stack_shape():
    res = _synth(CONFIG)
    (fn,) = _of_type(res, "AWS::Lambda::Function")
    assert fn["PackageType"] == "Image" and fn["Architectures"] == ["arm64"]
    assert fn["ReservedConcurrentExecutions"] == 4
    assert fn["EphemeralStorage"]["Size"] == 512  # cache_mb 256 + headroom, floored at 512
    env = fn["Environment"]["Variables"]
    assert env["LOOP_PACKAGE_S3_URI"] == "s3://bkt/f/t" and env["LOOP_PACKAGE_STORE"] == "s3"
    assert env["RESERVED_CONCURRENCY"] == "4" and env["SEGMENT_CACHE_MB"] == "256"
    assert "--continuous-timeline" in fn["ImageConfig"]["Command"]
    (url,) = _of_type(res, "AWS::Lambda::Url")
    assert url["AuthType"] == "AWS_IAM" and url["InvokeMode"] == "RESPONSE_STREAM"
    actions = {p["Action"] for p in _of_type(res, "AWS::Lambda::Permission")}
    assert {"lambda:InvokeFunctionUrl", "lambda:InvokeFunction"} <= actions
    (oac,) = _of_type(res, "AWS::CloudFront::OriginAccessControl")
    assert oac["OriginAccessControlConfig"]["OriginAccessControlOriginType"] == "lambda"
    (dist,) = _of_type(res, "AWS::CloudFront::Distribution")
    patterns = {b["PathPattern"] for b in dist["DistributionConfig"]["CacheBehaviors"]}
    assert patterns == {"*/seg/*", "*/cseg/*", "*/rseg/*", "*/init*.mp4", "/timeline.json", "/health"}


def test_stack_has_no_cluster_or_load_balancer():
    types = {r["Type"] for r in _synth(CONFIG).values()}
    assert not any(t.startswith(("AWS::ECS", "AWS::ElasticLoadBalancing")) for t in types)


def _shared(stack_cls_module: str, stack_cls: str) -> dict:
    cdk = pytest.importorskip("aws_cdk")
    import importlib

    import aws_cdk.assertions as assertions

    stack = getattr(importlib.import_module(stack_cls_module), stack_cls)(
        cdk.App(), "Shared", env=cdk.Environment(account="123456789012", region="eu-west-1")
    )
    return assertions.Template.from_stack(stack).to_json()


def _policies(resources) -> dict:
    return {p["CachePolicyConfig"]["Name"]: p["CachePolicyConfig"] for p in _of_type(resources, "AWS::CloudFront::CachePolicy")}


def test_shared_stack_holds_the_cache_policies_and_exports_their_ids():
    template = _shared("lambda_shared_stack", "LambdaSharedStack")
    policies = _policies(template["Resources"])
    assert set(policies) == {
        "loop-dee-loop-lambda-segments", "loop-dee-loop-lambda-status", "loop-dee-loop-lambda-manifests-default",
    }
    manifests = policies["loop-dee-loop-lambda-manifests-default"]
    assert manifests["DefaultTTL"] == 1 and manifests["MinTTL"] == 0 and manifests["MaxTTL"] > 86400
    status = policies["loop-dee-loop-lambda-status"]
    assert status["ParametersInCacheKeyAndForwardedToOrigin"]["QueryStringsConfig"]["QueryStringBehavior"] == "all"
    assert status["DefaultTTL"] == 0
    exports = {o["Export"]["Name"] for o in template["Outputs"].values() if "Export" in o}
    assert exports == {f"ItsALive-aws-lambda-{k}-CachePolicyId" for k in ("segments", "status", "manifests")}


def test_a_default_query_channel_owns_no_cache_policy_and_imports_all_three():
    res = _synth(CONFIG)
    assert _policies(res) == {}
    (dist,) = _of_type(res, "AWS::CloudFront::Distribution")
    config = dist["DistributionConfig"]
    imported = str([config["DefaultCacheBehavior"]["CachePolicyId"]] + [b["CachePolicyId"] for b in config["CacheBehaviors"]])
    for kind in ("segments", "status", "manifests"):
        assert f"ItsALive-aws-lambda-{kind}-CachePolicyId" in imported


def test_a_channel_with_custom_query_names_owns_only_its_manifests_policy():
    custom = CONFIG + "\n[query]\nprefix = 'dsl_'\nstart_param = 'from'\nend_param = 'to'\n"
    res = _synth(custom)
    assert set(_policies(res)) == {"loop-dee-loop-t-manifests"}
    (dist,) = _of_type(res, "AWS::CloudFront::Distribution")
    imported = str([b["CachePolicyId"] for b in dist["DistributionConfig"]["CacheBehaviors"]])
    assert "ItsALive-aws-lambda-segments-CachePolicyId" in imported and "ItsALive-aws-lambda-status-CachePolicyId" in imported
    assert "manifests-CachePolicyId" not in str(dist["DistributionConfig"])


def test_ecs_express_shares_its_policies_the_same_way():
    template = _shared("loop_shared_stack", "LoopSharedStack")
    assert set(_policies(template["Resources"])) == {"loop-dee-loop-ecs-segments", "loop-dee-loop-ecs-manifests-default"}
    manifests = _policies(template["Resources"])["loop-dee-loop-ecs-manifests-default"]
    assert manifests["DefaultTTL"] == 0 and manifests["MaxTTL"] == 1  # manifests are never cached on ecs-express
    exports = {o["Export"]["Name"] for o in template["Outputs"].values() if "Export" in o}
    assert exports == {"ItsALive-ecs-express-segments-CachePolicyId", "ItsALive-ecs-express-manifests-CachePolicyId"}

    cdk = pytest.importorskip("aws_cdk")
    import aws_cdk.assertions as assertions
    from loop_stack import LoopStack

    stack = LoopStack(cdk.App(), "C", config=tomllib.loads(CONFIG.replace("aws-lambda", "ecs-express")),
                      env=cdk.Environment(account="123456789012", region="eu-west-1"))
    assert _policies(assertions.Template.from_stack(stack).to_json()["Resources"]) == {}


@pytest.mark.parametrize("bad", ["reserved_concurrency = 0", "reserved_concurrency = -5", "architecture = 'mips'", "cache_mb = 99999"])
def test_stack_rejects_bad_lambda_config(bad):
    with pytest.raises(ValueError):
        _synth(CONFIG.replace("reserved_concurrency = 4\ncache_mb = 256", bad))


def test_unreserved_function_has_no_reservation():
    (fn,) = _of_type(_synth(CONFIG.replace("reserved_concurrency = 4", "reserved_concurrency = -1")), "AWS::Lambda::Function")
    assert "ReservedConcurrentExecutions" not in fn
