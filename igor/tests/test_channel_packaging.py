"""Channel form packaging choices persist in the config used by spark."""

import tomllib

import pytest

from igor.app.routes.channels import ChannelCreatePayload
from igor.integrations.its_a_live import generate_toml


@pytest.mark.parametrize("backend", ["ecs-express", "local-docker"])
@pytest.mark.parametrize("hls_format,mux_audio", [("cmaf", True), ("ts", True), ("ts", False)])
def test_channel_hls_packaging_roundtrip(backend, hls_format, mux_audio):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        hls_format=hls_format, hls_ts_mux_audio=mux_audio,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["packaging"]["hls_format"] == hls_format
    assert config["packaging"]["hls_ts_mux_audio"] is mux_audio


def test_invalid_hls_format_rejected():
    with pytest.raises(ValueError, match="hls_format"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            hls_format="invalid",
        )


@pytest.mark.parametrize("backend", ["ecs-express", "local-docker"])
@pytest.mark.parametrize("dash_signal_format", ["binary", "xml"])
def test_channel_dash_signal_format_roundtrip(backend, dash_signal_format):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        dash_signal_format=dash_signal_format,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["markers"]["dash_signal_format"] == dash_signal_format


def test_invalid_dash_signal_format_rejected():
    with pytest.raises(ValueError, match="dash_signal_format"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            dash_signal_format="invalid",
        )


@pytest.mark.parametrize("backend", ["ecs-express", "local-docker"])
@pytest.mark.parametrize("dash_descriptor_mode", ["shared", "narrowed"])
def test_channel_dash_descriptor_mode_roundtrip(backend, dash_descriptor_mode):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        dash_descriptor_mode=dash_descriptor_mode,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["markers"]["dash_descriptor_mode"] == dash_descriptor_mode


def test_invalid_dash_descriptor_mode_rejected():
    with pytest.raises(ValueError, match="dash_descriptor_mode"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            dash_descriptor_mode="invalid",
        )
