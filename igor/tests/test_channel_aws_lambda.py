"""aws-lambda channel config: form payload -> TOML (`[infrastructure.lambda]`)."""

import tomllib

import pytest

from igor.app.routes.channels import ChannelCreatePayload
from igor.integrations.its_a_live import generate_toml


def _payload(**kw):
    return ChannelCreatePayload(
        name="lam", backend="aws-lambda", region="eu-west-1", bucket_name="b",
        content_folder="c", source_path="outputs/t.ts", **kw,
    )


def test_defaults_roundtrip_into_infrastructure_lambda():
    config = tomllib.loads(generate_toml(**_payload().model_dump()))
    assert config["deploy"]["backend"] == "aws-lambda"
    assert config["infrastructure"]["lambda"] == {
        "memory": 1024, "timeout": 30, "reserved_concurrency": 10, "cache_mb": 512, "architecture": "arm64",
    }
    assert "express" not in config["infrastructure"] and "docker" not in config["infrastructure"]
    # same shared sections as the other serve.py backends
    assert {"markers", "packaging", "timeline", "query"} <= set(config)


def test_custom_values_roundtrip():
    payload = _payload(lambda_memory=1769, lambda_timeout=60, lambda_reserved_concurrency=-1,
                       lambda_cache_mb=2048, lambda_architecture="x86_64")
    lam = tomllib.loads(generate_toml(**payload.model_dump()))["infrastructure"]["lambda"]
    assert lam == {"memory": 1769, "timeout": 60, "reserved_concurrency": -1, "cache_mb": 2048, "architecture": "x86_64"}


@pytest.mark.parametrize("bad", [0, -2])
def test_reserved_concurrency_zero_is_not_a_configuration(bad):
    with pytest.raises(ValueError, match="reserved_concurrency"):
        _payload(lambda_reserved_concurrency=bad)


def test_port_auto_is_still_local_docker_only():
    with pytest.raises(ValueError, match="auto"):
        _payload(port="auto")

