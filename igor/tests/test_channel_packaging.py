"""Channel form packaging choices persist in the config used by spark."""

import tomllib

import pytest

from igor.app.routes.channels import ChannelCreatePayload
from igor.integrations.its_a_live import generate_toml


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("hls_format,mux_audio", [("cmaf", True), ("ts", True), ("ts", False)])
def test_channel_hls_packaging_roundtrip(backend, hls_format, mux_audio):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        hls_format=hls_format, hls_ts_mux_audio=mux_audio,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["packaging"]["hls"] == {"container": hls_format, "mux_audio": mux_audio}


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("continuous", [True, False])
def test_channel_continuous_roundtrip(backend, continuous):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        continuous=continuous,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["timeline"]["continuous"] is continuous


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("dash_addressing", ["number", "time"])
def test_channel_dash_addressing_roundtrip(backend, dash_addressing):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        dash_addressing=dash_addressing,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["packaging"]["dash"]["addressing"] == dash_addressing


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("dash_timeline", ["full", "compact"])
def test_channel_dash_timeline_roundtrip(backend, dash_timeline):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        dash_timeline=dash_timeline,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["packaging"]["dash"]["timeline_style"] == dash_timeline


def test_invalid_dash_timeline_rejected():
    with pytest.raises(ValueError, match="dash_timeline"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            dash_timeline="bogus",
        )


def test_invalid_dash_addressing_rejected():
    with pytest.raises(ValueError, match="dash_addressing"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            dash_addressing="bogus",
        )


def test_channel_continuous_defaults_false():
    payload = ChannelCreatePayload(
        name="test-channel", backend="ecs-express", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["timeline"]["continuous"] is False


@pytest.mark.parametrize("source_kind", ["playlist", "archive"])
def test_channel_source_kind_roundtrip(source_kind):
    payload = ChannelCreatePayload(
        name="test-channel", backend="local-docker", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/manifest.json",
        source_kind=source_kind,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["input"]["source_kind"] == source_kind


def test_invalid_channel_source_kind_rejected():
    with pytest.raises(ValueError, match="source_kind"):
        ChannelCreatePayload(
            name="test-channel", backend="local-docker", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/manifest.json",
            source_kind="unknown",
        )


def test_invalid_hls_format_rejected():
    with pytest.raises(ValueError, match="hls_format"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            hls_format="invalid",
        )


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("dash_signal_format", ["binary", "xml"])
def test_channel_dash_signal_format_roundtrip(backend, dash_signal_format):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        dash_signal_format=dash_signal_format,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["markers"]["dash"]["signal_format"] == dash_signal_format


def test_invalid_dash_signal_format_rejected():
    with pytest.raises(ValueError, match="dash_signal_format"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            dash_signal_format="invalid",
        )


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("dash_descriptor_mode", ["shared", "narrowed"])
def test_channel_dash_descriptor_mode_roundtrip(backend, dash_descriptor_mode):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        dash_descriptor_mode=dash_descriptor_mode,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["markers"]["dash"]["descriptor_grouping"] == dash_descriptor_mode


def test_invalid_dash_descriptor_mode_rejected():
    with pytest.raises(ValueError, match="dash_descriptor_mode"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            dash_descriptor_mode="invalid",
        )


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
def test_channel_timeshift_roundtrip(backend):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        timeshift_start_param="from", timeshift_end_param="to",
        timeshift_max_span_seconds=3600,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["query"] == {
        "timeshift": True, "overrides": True, "prefix": "", "start_param": "from", "end_param": "to",
    }
    assert config["timeshift"] == {"max_span_seconds": 3600}


def test_channel_timeshift_defaults_match_loop_dee_loop():
    payload = ChannelCreatePayload(
        name="test-channel", backend="ecs-express", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["query"]["timeshift"] is True and "enabled" not in config["query"]
    assert (config["query"]["start_param"], config["query"]["end_param"]) == ("start", "end")
    assert config["query"]["prefix"] == ""
    assert config["timeshift"]["max_span_seconds"] == 21600


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeshift_start_param": "a b"},
        {"timeshift_start_param": "1start"},
        {"timeshift_end_param": "x&y=1"},
        {"timeshift_start_param": "timeline_mode"},  # reserved: fixed query params
        {"timeshift_end_param": "full_loops"},
        {"timeshift_end_param": "offset"},
        {"timeshift_start_param": "cue_tags"},
        {"timeshift_end_param": "max_height"},
        {"query_prefix": "a b"},
        {"query_prefix": "x&"},
        {"timeshift_start_param": "same", "timeshift_end_param": "same"},
        {"timeshift_max_span_seconds": 0},
    ],
)
def test_invalid_timeshift_config_rejected(kwargs):
    with pytest.raises(ValueError):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            **kwargs,
        )


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("epoch_utc", ["1970-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-10-01T09:15:30Z"])
def test_channel_epoch_utc_roundtrip(backend, epoch_utc):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        epoch_utc=epoch_utc,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["timeline"]["epoch"] == epoch_utc
    assert "epoch_utc" not in config["timeline"]


def test_channel_epoch_utc_defaults_to_2026():
    payload = ChannelCreatePayload(
        name="test-channel", backend="ecs-express", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["timeline"]["epoch"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("bad", ["now", "2026-01-01", "2026-01-01T00:00:00", "2026-01-01 00:00:00Z", "2026-13-01T00:00:00Z", ""])
def test_channel_epoch_utc_rejects_anything_but_the_exact_utc_form(bad):
    with pytest.raises(ValueError, match="epoch_utc"):
        ChannelCreatePayload(
            name="test-channel", backend="ecs-express", region="eu-west-1",
            bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
            epoch_utc=bad,
        )


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
def test_channel_period_on_segmentation_roundtrip(backend):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        continuous=True,
        period_on_segmentation=["0x22", "0x30", "0x22"],
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["markers"]["split_on"] == [0x22, 0x30]
    assert "period_on_segmentation" not in config["markers"]
    assert "period_on_segmentation_apply" not in config["markers"]
    assert config["timeline"]["continuous"] is True


def test_channel_period_on_segmentation_omitted_by_default():
    payload = ChannelCreatePayload(
        name="test-channel", backend="local-docker", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert "split_on" not in config["markers"]


def test_channel_period_on_segmentation_rejects_bad_values():
    base = dict(
        name="test-channel", backend="local-docker", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    with pytest.raises(ValueError):
        ChannelCreatePayload(**base, period_on_segmentation=["nope"])
    with pytest.raises(ValueError):
        ChannelCreatePayload(**base, period_on_segmentation=["0x100"])
    # the HLS/DASH "apply" setting no longer exists: an old client's value is ignored, never written
    stale = ChannelCreatePayload(**base, period_on_segmentation=["0x22"], period_on_segmentation_apply="dash")
    assert "period_on_segmentation_apply" not in stale.model_dump()
    assert "apply" not in generate_toml(**stale.model_dump())


@pytest.mark.parametrize("backend,table", [("ecs-express", "express"), ("local-docker", "docker")])
def test_generated_toml_groups_infrastructure_tables(backend, table):
    payload = ChannelCreatePayload(
        name="test-channel", backend=backend, region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    infra = config["infrastructure"]
    assert infra["aws"]["region"] == "eu-west-1"
    assert infra["s3"]["bucket_name"] == "test-bucket"
    assert "port" in infra[table]
    for legacy in ("aws", "s3", "express", "docker"):
        assert legacy not in config


@pytest.mark.parametrize("cdn", [True, False])
def test_channel_cdn_roundtrip(cdn):
    payload = ChannelCreatePayload(
        name="test-channel", backend="ecs-express", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
        cdn=cdn,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["infrastructure"]["express"]["cdn"] is cdn


def test_channel_cdn_defaults_true():
    payload = ChannelCreatePayload(
        name="test-channel", backend="ecs-express", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["infrastructure"]["express"]["cdn"] is True


def _payload(**overrides):
    base = dict(
        name="test-channel", backend="local-docker", region="eu-west-1",
        bucket_name="test-bucket", content_folder="content", source_path="outputs/test.ts",
    )
    base.update(overrides)
    return ChannelCreatePayload(**base)


def test_bundle_content_defaults_false():
    config = tomllib.loads(generate_toml(**_payload().model_dump()))
    assert config["infrastructure"]["docker"]["bundle_content"] is False


@pytest.mark.parametrize("bundle_content", [True, False])
def test_local_docker_bundle_content_roundtrip(bundle_content):
    config = tomllib.loads(generate_toml(**_payload(bundle_content=bundle_content).model_dump()))
    assert config["infrastructure"]["docker"]["bundle_content"] is bundle_content


@pytest.mark.parametrize("backend", ["ecs-express", "aws-media"])
def test_bundle_content_rejected_outside_local_docker(backend):
    with pytest.raises(ValueError, match="bundle_content"):
        _payload(backend=backend, bundle_content=True)
    with pytest.raises(ValueError, match="bundle_content"):
        generate_toml(
            name="x", backend=backend, region="r", bucket_name="b", content_folder="c",
            source_path="p", bundle_content=True,
        )


# ── the new TOML layout ─────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
def test_generated_toml_uses_the_nested_marker_and_packaging_tables(backend):
    payload = _payload(
        backend=backend, daterange_mode="grouped", cue_tags="alongside", daterange_id_format="x-{eventid}",
        dash_descriptor_mode="narrowed", dash_signal_format="xml", increment_event_ids=True,
        period_on_segmentation=["0x22"], dvr_window_seconds=45, hls_format="ts", hls_ts_mux_audio=False,
        dash_addressing="time", dash_timeline="compact", epoch_utc="2026-03-04T05:06:07Z", continuous=True,
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    assert config["markers"] == {
        "increment_event_ids": True,
        "split_on": [0x22],
        "hls": {"descriptor_grouping": "grouped", "cue_tags": "alongside", "id_template": "x-{eventid}"},
        "dash": {"descriptor_grouping": "narrowed", "signal_format": "xml"},
    }
    assert config["packaging"] == {
        "segment_duration": 4.0,
        "dvr_window": 45,
        "hls": {"container": "ts", "mux_audio": False},
        "dash": {"timeline_style": "compact", "addressing": "time"},
    }
    assert config["timeline"] == {"epoch": "2026-03-04T05:06:07Z", "continuous": True}


def test_generated_toml_has_no_pre_rename_keys():
    config = tomllib.loads(generate_toml(**_payload().model_dump()))
    flat = {(t, k) for t, body in config.items() if isinstance(body, dict) for k in body}
    for old in ("daterange_mode", "dash_descriptor_mode", "cue_tags", "daterange_id_format", "dash_signal_format",
                "period_on_segmentation", "period_on_segmentation_apply", "dvr_window_seconds", "hls_format",
                "hls_ts_mux_audio", "dash_timeline", "dash_addressing", "epoch_utc", "enabled", "start_param"):
        assert ("markers", old) not in flat and ("packaging", old) not in flat and ("timeline", old) not in flat
        assert ("timeshift", old) not in flat


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
def test_query_prefix_roundtrip(backend):
    config = tomllib.loads(generate_toml(**_payload(backend=backend, query_prefix="dsl_").model_dump()))
    assert config["query"]["prefix"] == "dsl_"


def test_query_prefix_defaults_to_none():
    assert tomllib.loads(generate_toml(**_payload().model_dump()))["query"]["prefix"] == ""


@pytest.mark.parametrize("prefix", ["dsl_", "x.", "a-b", "A1", "~t"])
def test_url_safe_prefixes_accepted(prefix):
    assert _payload(query_prefix=prefix).query_prefix == prefix


def test_start_and_end_names_are_checked_against_every_fixed_parameter():
    from igor.integrations.its_a_live import QUERY_FIXED_PARAMS

    for name in QUERY_FIXED_PARAMS:
        with pytest.raises(ValueError, match="distinct"):
            _payload(timeshift_start_param=name)
        with pytest.raises(ValueError, match="distinct"):
            _payload(timeshift_end_param=name)
    # an old name is an ordinary custom name now
    assert _payload(timeshift_start_param="full-loops").timeshift_start_param == "full-loops"


def test_fixed_query_params_match_its_a_live_and_loop_dee_loop():
    """Igor, its-a-live and loop-dee-loop keep separate copies of the parameter
    names (separate venvs); they must never drift apart."""
    import importlib.util
    import sys
    from pathlib import Path

    from igor.integrations.its_a_live import QUERY_FIXED_PARAMS

    repo = Path(__file__).resolve().parents[2]

    def load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    its_a_live_cfg = load(repo / "its-a-live" / "_query_cfg.py", "_igor_parity_query_cfg")
    sys.path.insert(0, str(repo / "loop-dee-loop"))
    try:
        loop_timeshift = load(repo / "loop-dee-loop" / "timeshift.py", "_igor_parity_timeshift")
    finally:
        sys.path.remove(str(repo / "loop-dee-loop"))
    assert QUERY_FIXED_PARAMS == its_a_live_cfg.FIXED_PARAMS == loop_timeshift.FIXED_PARAMS


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
def test_generated_toml_is_accepted_by_its_a_live(backend):
    """The file Igor writes must pass its-a-live's own loader checks and
    settings validation (no legacy keys, every value valid)."""
    import importlib.util
    import sys
    from pathlib import Path

    its_a_live_dir = Path(__file__).resolve().parents[2] / "its-a-live"
    sys.path.insert(0, str(its_a_live_dir))
    try:
        import _legacy_cfg
        import _markers_cfg
        import _packaging_cfg
        import _query_cfg
    finally:
        sys.path.remove(str(its_a_live_dir))
    payload = _payload(
        backend=backend, cue_tags="only", daterange_mode="narrowed", dash_signal_format="xml",
        period_on_segmentation=["0x22"], hls_format="ts", dash_addressing="time", query_prefix="dsl_",
        timeshift_start_param="from",
    )
    config = tomllib.loads(generate_toml(**payload.model_dump()))
    _legacy_cfg.check_config_version(config)
    markers = _markers_cfg.markers_settings(config)
    assert markers["hls"]["cue_tags"] == "only" and markers["dash"]["signal_format"] == "xml"
    assert markers["hls"]["descriptor_grouping"] == "narrowed" and markers["split_on"] == [0x22]
    packaging = _packaging_cfg.packaging_settings(config)
    assert packaging["hls"]["container"] == "ts" and packaging["dash"]["addressing"] == "time"
    query = _query_cfg.query_settings(config)
    assert query["prefix"] == "dsl_" and query["start_param"] == "from"
    assert _query_cfg.query_param_names(config)[0] == "dsl_from"


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("overrides", [True, False])
def test_query_overrides_roundtrip(backend, overrides):
    config = tomllib.loads(generate_toml(**_payload(backend=backend, query_overrides=overrides).model_dump()))
    assert config["query"]["overrides"] is overrides


def test_query_overrides_default_to_on():
    assert _payload().query_overrides is True
    assert tomllib.loads(generate_toml(**_payload().model_dump()))["query"]["overrides"] is True


def test_generated_toml_with_overrides_off_is_read_back_by_its_a_live():
    import sys
    from pathlib import Path

    its_a_live_dir = Path(__file__).resolve().parents[2] / "its-a-live"
    sys.path.insert(0, str(its_a_live_dir))
    try:
        import _query_cfg
    finally:
        sys.path.remove(str(its_a_live_dir))
    config = tomllib.loads(generate_toml(**_payload(query_overrides=False, query_prefix="dsl_").model_dump()))
    assert _query_cfg.query_param_names(config) == ["dsl_start", "dsl_end", "dsl_full_loops", "dsl_offset"]
    flags = _query_cfg.query_serve_args(config)
    assert "--timeshift" in flags and "--query-overrides" not in flags and "--no-query-overrides" not in flags


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
@pytest.mark.parametrize("timeshift", [True, False])
def test_timeshift_switch_roundtrip(backend, timeshift):
    config = tomllib.loads(generate_toml(**_payload(backend=backend, timeshift_enabled=timeshift).model_dump()))
    assert config["query"]["timeshift"] is timeshift
    assert "enabled" not in config["query"]


@pytest.mark.parametrize("timeshift", [True, False])
@pytest.mark.parametrize("overrides", [True, False])
def test_the_two_switches_are_independent_in_the_toml(timeshift, overrides):
    config = tomllib.loads(generate_toml(
        **_payload(timeshift_enabled=timeshift, query_overrides=overrides).model_dump()
    ))
    assert (config["query"]["timeshift"], config["query"]["overrides"]) == (timeshift, overrides)


def test_the_switches_are_not_fixed_query_parameter_names():
    from igor.integrations.its_a_live import QUERY_FIXED_PARAMS

    assert not {"timeshift", "overrides", "enabled"} & set(QUERY_FIXED_PARAMS)
    # ... so a custom start/end name may even be "timeshift"/"overrides"
    assert _payload(timeshift_start_param="overrides").timeshift_start_param == "overrides"


# ── config_version ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", ["ecs-express", "aws-lambda", "local-docker"])
def test_generated_toml_starts_with_the_config_version(backend):
    text = generate_toml(**_payload(backend=backend).model_dump())
    assert text.startswith("config_version = 2\n\n[deploy]")
    assert tomllib.loads(text)["config_version"] == 2


def test_igor_its_a_live_and_the_frontend_agree_on_the_config_version():
    import re
    import sys
    from pathlib import Path

    from igor.integrations.its_a_live import CONFIG_VERSION

    repo = Path(__file__).resolve().parents[2]
    its_a_live_dir = repo / "its-a-live"
    sys.path.insert(0, str(its_a_live_dir))
    try:
        import _legacy_cfg
    finally:
        sys.path.remove(str(its_a_live_dir))
    frontend = (repo / "igor" / "frontend" / "src" / "utils" / "channelConfigLayout.ts").read_text()
    frontend_version = int(re.search(r"export const CONFIG_VERSION = (\d+)", frontend).group(1))
    assert CONFIG_VERSION == _legacy_cfg.CONFIG_VERSION == frontend_version


def test_generated_toml_passes_its_a_live_version_check():
    import sys
    from pathlib import Path

    its_a_live_dir = Path(__file__).resolve().parents[2] / "its-a-live"
    sys.path.insert(0, str(its_a_live_dir))
    try:
        import _legacy_cfg
    finally:
        sys.path.remove(str(its_a_live_dir))
    for backend in ("ecs-express", "local-docker"):
        config = tomllib.loads(generate_toml(**_payload(backend=backend).model_dump()))
        _legacy_cfg.check_config_version(config, "igor.toml")  # does not raise
