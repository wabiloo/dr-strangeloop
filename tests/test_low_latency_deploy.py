"""Low-latency DASH at deploy time: how an ecs-express channel learns its own public address for
`utc_timing = "http"` (a second deploy with `-c public_host=`), and what reaches the container command.
Needs boto3 / aws_cdk (the its-a-live venv); skipped in the root venv."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ITS_A_LIVE = Path(__file__).resolve().parents[1] / "its-a-live"
sys.path.insert(0, str(ITS_A_LIVE))

pytest.importorskip("boto3")
import botocore.exceptions  # noqa: E402
import channel  # noqa: E402

CFG = {
    "deploy": {"name": "t", "backend": "ecs-express"},
    "packaging": {"segment_duration": 2, "dash": {"low_latency": "on", "utc_timing": "http"}},
}
OUTPUTS = {"CloudFrontDomainName": "d111.cloudfront.net", "ExpressServiceEndpoint": "https://svc.ecs.example"}
NO_STACK = botocore.exceptions.ClientError({"Error": {"Code": "ValidationError", "Message": "does not exist"}}, "DescribeStacks")


def test_public_host_prefers_the_cdn_domain_then_the_service_endpoint():
    assert channel._public_host(OUTPUTS) == "d111.cloudfront.net"
    assert channel._public_host({"ExpressServiceEndpoint": "https://svc.ecs.example"}) == "svc.ecs.example"
    assert channel._public_host({}) is None


@pytest.fixture
def deploys(monkeypatch):
    """Record every `cdk deploy`; `outputs` is what the stack reports (or raises) each time it is read."""
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda cmd, check=False: (calls.append(cmd), Result())[1])
    return calls


def _context(cmd):
    return [a for a in cmd if a.startswith("public_host=")]


def test_existing_stack_is_deployed_once_with_its_public_host(monkeypatch, deploys):
    monkeypatch.setattr(channel, "_cf_outputs", lambda cfg, stack_name=None: OUTPUTS)
    assert channel._cdk_deploy(CFG, "/x/c.toml", "Stack") == 0
    assert len(deploys) == 1 and _context(deploys[0]) == ["public_host=d111.cloudfront.net"]


def test_new_stack_is_deployed_twice_the_second_time_with_its_public_host(monkeypatch, deploys):
    reads = iter([NO_STACK, OUTPUTS])

    def outputs(cfg, stack_name=None):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(channel, "_cf_outputs", outputs)
    assert channel._cdk_deploy(CFG, "/x/c.toml", "Stack") == 0
    assert len(deploys) == 2
    assert _context(deploys[0]) == [] and _context(deploys[1]) == ["public_host=d111.cloudfront.net"]


def test_no_second_deploy_when_nothing_needs_the_public_address(monkeypatch, deploys):
    monkeypatch.setattr(channel, "_cf_outputs", lambda cfg, stack_name=None: pytest.fail("must not read the stack"))
    for packaging in (
        {"segment_duration": 2, "dash": {"low_latency": "on"}},  # direct (default)
        {"segment_duration": 2, "dash": {"low_latency": "on", "utc_timing": "none"}},
        {"segment_duration": 2, "dash": {"low_latency": "on", "utc_timing": "http", "utc_timing_url": "https://t.example/time"}},
        {},  # low latency off
    ):
        deploys.clear()
        cfg = {**CFG, "packaging": packaging}
        assert channel._cdk_deploy(cfg, "/x/c.toml", "Stack") == 0
        assert len(deploys) == 1 and _context(deploys[0]) == [], packaging


def test_failed_first_deploy_stops_and_an_unreadable_address_only_warns(monkeypatch, deploys, capsys):
    monkeypatch.setattr(channel, "_cf_outputs", lambda cfg, stack_name=None: {})
    assert channel._cdk_deploy(CFG, "/x/c.toml", "Stack") == 0
    assert len(deploys) == 1 and "utc_timing_url" in capsys.readouterr().out

    class Failed:
        returncode = 3

    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda cmd, check=False: Failed())
    assert channel._cdk_deploy(CFG, "/x/c.toml", "Stack") == 3


def test_other_backends_never_get_the_context(monkeypatch, deploys):
    monkeypatch.setattr(channel, "_cf_outputs", lambda cfg, stack_name=None: pytest.fail("must not read the stack"))
    cfg = {**CFG, "deploy": {"name": "t", "backend": "aws-lambda"}}
    assert channel._public_host_context(cfg) == []


# ── what reaches the container ──────────────────────────────────────────────

STACK_CONFIG = """
config_version = 2
[deploy]
name = "t"
backend = "ecs-express"
[infrastructure.aws]
region = "eu-west-1"
[infrastructure.s3]
bucket_name = "bkt"
content_folder = "f"
[packaging]
segment_duration = 2
[packaging.dash]
{dash}
"""


def _command(dash: str, public_host: str | None) -> list[str]:
    cdk = pytest.importorskip("aws_cdk")
    from aws_cdk import assertions
    from loop_stack import LoopStack

    stack = LoopStack(
        cdk.App(), "S", config=tomllib.loads(STACK_CONFIG.format(dash=dash)), public_host=public_host,
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )
    res = assertions.Template.from_stack(stack).to_json()["Resources"]
    (svc,) = [r["Properties"] for r in res.values() if r["Type"] == "AWS::ECS::ExpressGatewayService"]
    return svc["PrimaryContainer"]["Command"]


def test_container_command_has_the_low_latency_flags_and_the_public_time_url():
    cmd = _command('low_latency = "on"\nutc_timing = "http"\ntarget_latency = 2', "d111.cloudfront.net")
    assert "--ll-dash" in cmd and cmd[cmd.index("--ll-utc-timing") + 1] == "http"
    assert cmd[cmd.index("--ll-utc-timing-url") + 1] == "https://d111.cloudfront.net/time"
    assert cmd[cmd.index("--ll-target-latency") + 1] == "2"


def test_container_command_before_the_address_is_known_has_no_url_flag():
    assert "--ll-utc-timing-url" not in _command('low_latency = "on"\nutc_timing = "http"', None)


def test_container_command_defaults_to_direct_and_off_adds_nothing():
    cmd = _command('low_latency = "on"', None)
    assert "--ll-dash" in cmd and cmd[cmd.index("--ll-utc-timing") + 1] == "direct" and "--ll-utc-timing-url" not in cmd
    assert not [a for a in _command("", "d111.cloudfront.net") if a.startswith("--ll")]
