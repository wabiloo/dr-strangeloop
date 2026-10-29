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


def test_manifest_cache_is_one_second_but_honours_origin_cache_control():
    policies = {p["CachePolicyConfig"]["Name"]: p["CachePolicyConfig"] for p in _of_type(_synth(CONFIG), "AWS::CloudFront::CachePolicy")}
    manifests = policies["loop-dee-loop-t-manifests"]
    assert manifests["DefaultTTL"] == 1 and manifests["MinTTL"] == 0 and manifests["MaxTTL"] > 86400


@pytest.mark.parametrize("bad", ["reserved_concurrency = 0", "reserved_concurrency = -5", "architecture = 'mips'", "cache_mb = 99999"])
def test_stack_rejects_bad_lambda_config(bad):
    with pytest.raises(ValueError):
        _synth(CONFIG.replace("reserved_concurrency = 4\ncache_mb = 256", bad))


def test_unreserved_function_has_no_reservation():
    (fn,) = _of_type(_synth(CONFIG.replace("reserved_concurrency = 4", "reserved_concurrency = -1")), "AWS::Lambda::Function")
    assert "ReservedConcurrentExecutions" not in fn


def test_timeline_and_health_forward_every_query_parameter_uncached():
    policies = {p["CachePolicyConfig"]["Name"]: p["CachePolicyConfig"] for p in _of_type(_synth(CONFIG), "AWS::CloudFront::CachePolicy")}
    status = policies["loop-dee-loop-t-status"]
    assert status["ParametersInCacheKeyAndForwardedToOrigin"]["QueryStringsConfig"]["QueryStringBehavior"] == "all"
    assert status["DefaultTTL"] == 0
