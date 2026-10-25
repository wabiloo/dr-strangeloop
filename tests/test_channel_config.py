"""its-a-live's channel-config layer: the `[markers]`, `[packaging]`, `[query]`,
`[timeline]` tables -> bake.py / serve.py flags and CDN cache-key names, the
migration of configs that still use the old key names, and parity of the
parameter names with loop-dee-loop. All stdlib-only modules, no AWS needed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ITS_A_LIVE = REPO / "its-a-live"
LOOP_DEE_LOOP = REPO / "loop-dee-loop"
sys.path.insert(0, str(ITS_A_LIVE))
sys.path.insert(0, str(LOOP_DEE_LOOP))  # timeshift.py imports loop_math

import _epoch_cfg  # noqa: E402
import _legacy_cfg  # noqa: E402
import _markers_cfg  # noqa: E402
import _packaging_cfg  # noqa: E402
import _query_cfg  # noqa: E402
import migrate_config  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


loop_timeshift = _load(LOOP_DEE_LOOP / "timeshift.py", "_loop_dee_loop_timeshift")


def _flags(args: list[str]) -> dict[str, str | None]:
    """['--a', '1', '--b'] -> {'--a': '1', '--b': None}"""
    out: dict[str, str | None] = {}
    for i, arg in enumerate(args):
        if arg.startswith("--"):
            nxt = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else None
            out[arg] = nxt
    return out


# ── [markers] ───────────────────────────────────────────────────────────────


def test_markers_defaults():
    m = _markers_cfg.markers_settings({})
    assert m == {
        "increment_event_ids": False,
        "split_on": [],
        "hls": {"cue_tags": "none", "descriptor_grouping": "shared", "id_template": "{segcode}-{eventid}-{loop}"},
        "dash": {"descriptor_grouping": "shared", "signal_format": "binary"},
    }
    assert _markers_cfg.split_on_serve_args({}) == []


def test_markers_bake_args_default_to_the_bake_defaults():
    flags = _flags(_markers_cfg.markers_bake_args({}))
    assert flags == {
        "--daterange-mode": "shared",
        "--cue-tags": "none",
        "--daterange-id-format": "{segcode}-{eventid}-{loop}",
        "--dash-signal-format": "binary",
        "--dash-descriptor-mode": "shared",
    }


def test_markers_bake_args_follow_the_nested_tables():
    cfg = {
        "markers": {
            "increment_event_ids": True,
            "hls": {"cue_tags": "alongside", "descriptor_grouping": "grouped", "id_template": "x-{eventid}"},
            "dash": {"descriptor_grouping": "narrowed", "signal_format": "xml"},
        }
    }
    flags = _flags(_markers_cfg.markers_bake_args(cfg))
    assert flags["--daterange-mode"] == "grouped"
    assert flags["--cue-tags"] == "alongside"
    assert flags["--daterange-id-format"] == "x-{eventid}"
    assert flags["--dash-signal-format"] == "xml"
    assert flags["--dash-descriptor-mode"] == "narrowed"
    assert "--increment-event-ids" in flags


def test_hls_and_dash_descriptor_grouping_are_independent():
    cfg = {"markers": {"hls": {"descriptor_grouping": "grouped"}}}
    flags = _flags(_markers_cfg.markers_bake_args(cfg))
    assert flags["--daterange-mode"] == "grouped" and flags["--dash-descriptor-mode"] == "shared"


@pytest.mark.parametrize(
    "table,key,value,message",
    [
        ("hls", "cue_tags", "sometimes", r"\[markers.hls\] cue_tags"),
        ("hls", "descriptor_grouping", "merged", r"\[markers.hls\] descriptor_grouping"),
        ("dash", "descriptor_grouping", "grouped", r"\[markers.dash\] descriptor_grouping"),  # HLS-only value
        ("dash", "signal_format", "json", r"\[markers.dash\] signal_format"),
    ],
)
def test_markers_values_are_validated_with_the_toml_key_in_the_message(table, key, value, message):
    with pytest.raises(ValueError, match=message):
        _markers_cfg.markers_settings({"markers": {table: {key: value}}})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ([0x22, 0x30], ["0x22", "0x30"]),
        (["0x22", "48"], ["0x22", "0x30"]),
        (34, ["0x22"]),
        ("0x10", ["0x10"]),
        ([], []),
    ],
)
def test_split_on_normalises_ids(raw, expected):
    args = _markers_cfg.split_on_serve_args({"markers": {"split_on": raw}})
    assert args == (["--period-on-segmentation", ",".join(expected)] if expected else [])


@pytest.mark.parametrize("bad", [[256], [-1], ["abc"]])
def test_split_on_rejects_non_segmentation_type_ids(bad):
    with pytest.raises(ValueError):
        _markers_cfg.split_on_serve_args({"markers": {"split_on": bad}})


# ── [packaging] ─────────────────────────────────────────────────────────────


def test_packaging_defaults():
    p = _packaging_cfg.packaging_settings({})
    assert p == {
        "segment_duration": 4.0,
        "dvr_window": 30.0,
        "hls": {"container": "cmaf", "mux_audio": True},
        "dash": {"timeline_style": "full", "addressing": "number"},
    }
    assert _flags(_packaging_cfg.packaging_bake_args({})) == {
        "--segment-duration": "4.0", "--hls-format": "cmaf", "--hls-ts-mux-audio": None,
    }
    assert _packaging_cfg.dvr_window_seconds({}) == "30"
    assert _packaging_cfg.dash_serve_args({}) == []


def test_packaging_nested_tables_drive_the_flags():
    cfg = {
        "packaging": {
            "segment_duration": 2,
            "dvr_window": 12.5,
            "hls": {"container": "ts", "mux_audio": False},
            "dash": {"timeline_style": "compact", "addressing": "time"},
        }
    }
    assert _flags(_packaging_cfg.packaging_bake_args(cfg)) == {
        "--segment-duration": "2", "--hls-format": "ts", "--no-hls-ts-mux-audio": None,
    }
    assert _packaging_cfg.dvr_window_seconds(cfg) == "12.5"
    assert _flags(_packaging_cfg.dash_serve_args(cfg)) == {"--dash-addressing": "time", "--dash-timeline": "compact"}


def test_dash_flags_are_only_emitted_when_not_default():
    assert _packaging_cfg.dash_serve_args({"packaging": {"dash": {"addressing": "time"}}}) == ["--dash-addressing", "time"]
    assert _packaging_cfg.dash_serve_args({"packaging": {"dash": {"timeline_style": "compact"}}}) == [
        "--dash-timeline", "compact",
    ]


@pytest.mark.parametrize(
    "packaging,message",
    [
        ({"hls": {"container": "mp4"}}, r"\[packaging.hls\] container"),
        ({"dash": {"timeline_style": "tiny"}}, r"\[packaging.dash\] timeline_style"),
        ({"dash": {"addressing": "index"}}, r"\[packaging.dash\] addressing"),
        ({"dvr_window": 0}, "dvr_window"),
        ({"segment_duration": -1}, "segment_duration"),
    ],
)
def test_packaging_values_are_validated(packaging, message):
    with pytest.raises(ValueError, match=message):
        _packaging_cfg.packaging_settings({"packaging": packaging})


# ── [timeline] ──────────────────────────────────────────────────────────────


def test_epoch_reads_the_renamed_key():
    assert _epoch_cfg.config_epoch_utc({}) == "2026-01-01T00:00:00Z"
    assert _epoch_cfg.config_epoch_utc({"timeline": {"epoch": "2027-02-03T04:05:06Z"}}) == "2027-02-03T04:05:06Z"
    with pytest.raises(ValueError, match="epoch"):
        _epoch_cfg.config_epoch_utc({"timeline": {"epoch": "yesterday"}})
    # the old name is no longer read (and is rejected by reject_legacy_keys)
    assert _epoch_cfg.config_epoch_utc({"timeline": {"epoch_utc": "2027-02-03T04:05:06Z"}}) == "2026-01-01T00:00:00Z"


# ── [query] / [timeshift] ───────────────────────────────────────────────────


def test_query_defaults_and_names():
    s = _query_cfg.query_settings({})
    assert s == {
        "timeshift": True, "overrides": True, "prefix": "", "start_param": "start", "end_param": "end",
        "max_span_seconds": 21600,
    }
    assert _query_cfg.query_param_names({}) == ["start", "end", *_query_cfg.FIXED_PARAMS]
    assert _flags(_query_cfg.query_serve_args({})) == {
        "--timeshift": None, "--timeshift-start-param": "start", "--timeshift-end-param": "end",
        "--query-overrides": None, "--timeshift-max-span-seconds": "21600",
    }


def test_there_is_no_master_enabled_switch():
    assert "enabled" not in _query_cfg.query_settings({})
    assert "enabled" not in _query_cfg.DEFAULTS
    # the old key is not read: it neither turns anything off nor on
    assert _query_cfg.query_settings({"query": {"enabled": False}})["timeshift"] is True


def test_both_switches_off_has_no_flags_and_no_cache_key_names():
    cfg = {"query": {"timeshift": False, "overrides": False}}
    assert _query_cfg.query_serve_args(cfg) == [] and _query_cfg.query_param_names(cfg) == []


def test_timeshift_off_overrides_on():
    cfg = {"query": {"timeshift": False}}
    assert _query_cfg.query_param_names(cfg) == list(_query_cfg.OVERRIDE_PARAMS)
    flags = _flags(_query_cfg.query_serve_args(cfg))
    assert "--query-overrides" in flags and "--timeshift" not in flags and "--timeshift-start-param" not in flags
    assert flags["--timeshift-max-span-seconds"] == "21600"  # still caps dvr_window


def test_overrides_off_timeshift_on_keeps_only_the_timeshift_names():
    cfg = {"query": {"overrides": False}}
    assert _query_cfg.query_param_names(cfg) == ["start", "end", "full_loops", "offset"]
    flags = _flags(_query_cfg.query_serve_args(cfg))
    assert "--timeshift" in flags and "--query-overrides" not in flags


def test_names_keep_the_documented_order_whichever_switches_are_on():
    fixed = list(_query_cfg.FIXED_PARAMS)
    for ts in (True, False):
        for ov in (True, False):
            names = _query_cfg.query_param_names({"query": {"timeshift": ts, "overrides": ov}})
            expected = (["start", "end"] if ts else []) + [
                p for p in fixed if (p in _query_cfg.TIMESHIFT_FIXED_PARAMS if ts else False)
                or (p in _query_cfg.OVERRIDE_PARAMS and ov)
            ]
            assert names == expected


def test_the_switches_are_toml_only():
    """They gate the query parameters, so they must not be parameters themselves."""
    for flag in ("timeshift", "overrides", "enabled"):
        assert flag not in _query_cfg.FIXED_PARAMS
        assert flag not in _query_cfg.query_param_names({})


def test_the_switches_must_be_booleans_in_their_effect():
    assert _query_cfg.query_settings({"query": {"timeshift": 0}})["timeshift"] is False
    assert _query_cfg.query_settings({"query": {"overrides": 1}})["overrides"] is True


def test_prefix_reaches_the_flags_and_every_cache_key_name():
    cfg = {"query": {"prefix": "dsl_", "start_param": "from", "end_param": "to"}}
    names = _query_cfg.query_param_names(cfg)
    assert names == ["dsl_from", "dsl_to", *(f"dsl_{n}" for n in _query_cfg.FIXED_PARAMS)]
    flags = _flags(_query_cfg.query_serve_args(cfg))
    assert flags["--query-prefix"] == "dsl_"
    assert flags["--timeshift-start-param"] == "from"  # the unprefixed names: serve.py applies the prefix
    assert "--query-prefix" not in _flags(_query_cfg.query_serve_args({}))


def test_prefix_applies_with_a_single_switch_too():
    cfg = {"query": {"prefix": "dsl_", "overrides": False}}
    assert _query_cfg.query_param_names(cfg) == ["dsl_start", "dsl_end", "dsl_full_loops", "dsl_offset"]


def test_max_span_lives_in_timeshift_but_start_and_end_in_query():
    cfg = {"timeshift": {"max_span_seconds": 600}, "query": {"start_param": "from"}}
    s = _query_cfg.query_settings(cfg)
    assert (s["max_span_seconds"], s["start_param"]) == (600, "from")
    # the pre-[query] spellings in [timeshift] are not read
    legacy = _query_cfg.query_settings({"timeshift": {"start_param": "from", "enabled": False}})
    assert legacy["start_param"] == "start" and legacy["timeshift"] is True


@pytest.mark.parametrize(
    "query,message",
    [
        ({"prefix": "a b"}, "prefix"),
        ({"prefix": "x&"}, "prefix"),
        ({"start_param": "end"}, "distinct"),
        ({"start_param": "cue_tags"}, "distinct"),
        ({"end_param": "offset"}, "distinct"),
        ({"start_param": ""}, "non-empty"),
        ({"overrides": False, "start_param": "cue_tags"}, "distinct"),  # distinct whichever switches are on
        ({"timeshift": False, "end_param": "max_height"}, "distinct"),
    ],
)
def test_query_names_are_validated(query, message):
    with pytest.raises(ValueError, match=message):
        _query_cfg.query_settings({"query": query})


def test_query_max_span_must_be_positive():
    with pytest.raises(ValueError, match="max_span_seconds"):
        _query_cfg.query_settings({"timeshift": {"max_span_seconds": 0}})


def test_cloudfront_allow_list_threshold():
    """17 names do not fit CloudFront's 10-name allow-list, so loop_stack.py
    keys the manifest cache on the whole query string instead -- unless
    only one switch is on, when few enough names remain for the allow-list."""
    assert len(_query_cfg.query_param_names({})) > _query_cfg.CLOUDFRONT_MAX_ALLOW_LIST
    assert len(_query_cfg.query_param_names({"query": {"overrides": False}})) <= _query_cfg.CLOUDFRONT_MAX_ALLOW_LIST
    assert len(_query_cfg.query_param_names({"query": {"timeshift": False}})) > 0


# ── parity with loop-dee-loop ───────────────────────────────────────────────


def test_fixed_params_match_loop_dee_loop():
    assert _query_cfg.FIXED_PARAMS == loop_timeshift.FIXED_PARAMS
    assert _query_cfg.TIMESHIFT_FIXED_PARAMS == loop_timeshift.TIMESHIFT_FIXED_PARAMS
    assert _query_cfg.OVERRIDE_PARAMS == loop_timeshift.OVERRIDE_PARAMS


@pytest.mark.parametrize(
    "query",
    [
        {}, {"prefix": "dsl_"}, {"prefix": "x.", "start_param": "from", "end_param": "to"},
        {"overrides": False}, {"timeshift": False}, {"timeshift": False, "overrides": False},
        {"overrides": False, "prefix": "dsl_", "start_param": "from"},
    ],
)
def test_wire_names_match_what_serve_py_reads(query):
    cfg = {"query": query}
    s = _query_cfg.query_settings(cfg)
    served = loop_timeshift.TimeshiftConfig(
        timeshift=s["timeshift"], overrides=s["overrides"], start_param=s["start_param"],
        end_param=s["end_param"], prefix=s["prefix"],
    )
    assert _query_cfg.query_param_names(cfg) == list(served.param_names)


def _help(script: str) -> str:
    return subprocess.run(
        [sys.executable, str(LOOP_DEE_LOOP / script), "--help"], capture_output=True, text=True, check=True, cwd=LOOP_DEE_LOOP
    ).stdout


def test_every_flag_its_a_live_emits_exists_in_serve_and_bake():
    cfg = {
        "markers": {
            "increment_event_ids": True, "split_on": [0x22],
            "hls": {"cue_tags": "alongside"}, "dash": {"signal_format": "xml"},
        },
        "packaging": {"hls": {"container": "ts", "mux_audio": False}, "dash": {"addressing": "time", "timeline_style": "compact"}},
        "query": {"prefix": "dsl_"},
    }
    serve_flags = {
        *_flags(_markers_cfg.split_on_serve_args(cfg)), *_flags(_packaging_cfg.dash_serve_args(cfg)),
        *_flags(_query_cfg.query_serve_args(cfg)), "--dvr-window-seconds",
    }
    assert {"--timeshift", "--query-overrides", "--query-prefix"} <= serve_flags
    bake_flags = {*_flags(_markers_cfg.markers_bake_args(cfg)), *_flags(_packaging_cfg.packaging_bake_args(cfg))}
    serve_help, bake_help = _help("serve.py"), _help("bake.py")
    for flag in serve_flags:
        assert flag in serve_help, f"serve.py has no {flag}"
    for flag in bake_flags:
        assert flag in bake_help, f"bake.py has no {flag}"
    for gone in ("--no-query-overrides", "--startover", "--period-on-segmentation-apply"):
        assert gone not in serve_help


# ── config_version ──────────────────────────────────────────────────────────

OLD_FULL = """\
[deploy]
name = "demo"
backend = "local-docker"

[markers]
# which DATERANGE shape
daterange_mode      = "narrowed"
cue_tags            = "alongside"
increment_event_ids = true
daterange_id_format = "{segcode}-{eventid}"
dash_signal_format  = "xml"
dash_descriptor_mode = "narrowed"
period_on_segmentation = [0x22, 0x30]
period_on_segmentation_apply = "both"

[packaging]
segment_duration   = 4.0
dvr_window_seconds = 45.0
hls_format = "ts"
hls_ts_mux_audio = false
dash_addressing = "time"
dash_timeline = "compact"

[timeline]
epoch_utc = "2026-03-04T05:06:07Z"
continuous = false

[timeshift]
enabled          = false
start_param      = "from"
end_param        = "to"
max_span_seconds = 1800
"""

NEW_FULL = {
    "config_version": 2,
    "deploy": {"name": "demo", "backend": "local-docker"},
    "markers": {
        "increment_event_ids": True,
        "split_on": [0x22, 0x30],
        "hls": {"descriptor_grouping": "narrowed", "cue_tags": "alongside", "id_template": "{segcode}-{eventid}"},
        "dash": {"signal_format": "xml", "descriptor_grouping": "narrowed"},
    },
    "packaging": {
        "segment_duration": 4.0,
        "dvr_window": 45.0,
        "hls": {"container": "ts", "mux_audio": False},
        "dash": {"addressing": "time", "timeline_style": "compact"},
    },
    "timeline": {"epoch": "2026-03-04T05:06:07Z", "continuous": False},
    # the old single `enabled = false` switched off every query parameter: both new switches inherit it
    "query": {"timeshift": False, "overrides": False, "start_param": "from", "end_param": "to"},
    "timeshift": {"max_span_seconds": 1800},
}


def test_config_version_defaults_to_1_and_validates():
    assert _legacy_cfg.config_version({}) == 1
    assert _legacy_cfg.config_version({"config_version": 2}) == 2
    for bad in (0, -1, "2", 2.0, True):
        with pytest.raises(SystemExit, match="config_version"):
            _legacy_cfg.config_version({"config_version": bad})


def test_current_version_is_2():
    assert _legacy_cfg.CONFIG_VERSION == 2


def test_check_accepts_a_current_config():
    _legacy_cfg.check_config_version(NEW_FULL, "ok.toml")
    _legacy_cfg.check_config_version({"config_version": 2}, "ok.toml")


def test_check_refuses_a_config_without_a_version_even_if_its_keys_are_current():
    with pytest.raises(SystemExit) as exc:
        _legacy_cfg.check_config_version({"deploy": {}}, "my/channel.toml")
    message = str(exc.value)
    assert "config_version 1, current is 2" in message and "only the version marker is missing" in message
    assert "migrate_config.py my/channel.toml" in message


def test_check_refuses_an_old_version_naming_every_change():
    with pytest.raises(SystemExit) as exc:
        _legacy_cfg.check_config_version(tomllib.loads(OLD_FULL), "my/channel.toml")
    message = str(exc.value)
    assert "[packaging] hls_format -> [packaging.hls] container" in message
    assert "[timeshift] enabled -> [query] timeshift and [query] overrides" in message
    assert "migrate_config.py my/channel.toml" in message


def test_check_refuses_a_current_version_that_still_has_an_old_key():
    with pytest.raises(SystemExit, match="dvr_window_seconds"):
        _legacy_cfg.check_config_version({"config_version": 2, "packaging": {"dvr_window_seconds": 5}}, "x.toml")


def test_check_refuses_a_newer_version():
    with pytest.raises(SystemExit, match="newer release"):
        _legacy_cfg.check_config_version({"config_version": 3}, "x.toml")


def test_migrate_config_refuses_a_newer_version():
    with pytest.raises(ValueError, match="newer"):
        _legacy_cfg.migrate_config({"config_version": 3})


# ── migration: dict ─────────────────────────────────────────────────────────


def test_migrate_config_moves_every_renamed_key_and_sets_the_version():
    migrated, warnings = _legacy_cfg.migrate_config(tomllib.loads(OLD_FULL))
    assert migrated == NEW_FULL
    assert warnings == []


def test_every_renamed_key_is_covered_by_the_full_example():
    old = tomllib.loads(OLD_FULL)
    present = {(t, k) for t, body in old.items() if isinstance(body, dict) for k in body}
    # `[query] enabled` only existed in development builds of 2.0: covered separately below
    assert set(_legacy_cfg.RENAMES) - {("query", "enabled")} <= present


def test_migrate_config_is_idempotent_and_leaves_current_configs_alone():
    once, _ = _legacy_cfg.migrate_config(tomllib.loads(OLD_FULL))
    twice, warnings = _legacy_cfg.migrate_config(once)
    assert twice == once and warnings == []
    assert _legacy_cfg.find_legacy_keys(once) == []


def test_migrate_config_does_not_mutate_its_input():
    cfg = tomllib.loads(OLD_FULL)
    before = tomllib.loads(OLD_FULL)
    _legacy_cfg.migrate_config(cfg)
    assert cfg == before


def test_a_config_with_nothing_old_only_gains_the_version():
    migrated, warnings = _legacy_cfg.migrate_config({"deploy": {"name": "x"}, "markers": {"hls": {"cue_tags": "only"}}})
    assert migrated == {"config_version": 2, "deploy": {"name": "x"}, "markers": {"hls": {"cue_tags": "only"}}}
    assert warnings == []


@pytest.mark.parametrize("table", ["timeshift", "query"])
@pytest.mark.parametrize("value", [True, False])
def test_the_old_enabled_switch_sets_both_new_switches(table, value):
    migrated, _ = _legacy_cfg.migrate_config({table: {"enabled": value}})
    assert migrated["query"] == {"timeshift": value, "overrides": value}
    assert "enabled" not in migrated["query"]
    assert "timeshift" not in migrated or "enabled" not in migrated["timeshift"]


def test_dropped_apply_warns_when_it_was_not_both():
    cfg = {"markers": {"period_on_segmentation": [0x22], "period_on_segmentation_apply": "dash"}}
    migrated, warnings = _legacy_cfg.migrate_config(cfg)
    assert migrated == {"config_version": 2, "markers": {"split_on": [0x22]}}
    assert len(warnings) == 1 and "'dash'" in warnings[0] and "both HLS and DASH" in warnings[0]


def test_find_legacy_keys_lists_old_and_new_names():
    found = _legacy_cfg.find_legacy_keys(tomllib.loads(OLD_FULL))
    assert "[markers] daterange_mode -> [markers.hls] descriptor_grouping" in found
    assert "[timeshift] start_param -> [query] start_param" in found
    assert "[timeshift] enabled -> [query] timeshift and [query] overrides" in found
    assert any("period_on_segmentation_apply -> removed" in line for line in found)
    assert len(found) == len(_legacy_cfg.RENAMES) - 1  # `[query] enabled` is not in the example


def test_migrations_form_a_complete_chain():
    assert sorted(_legacy_cfg._MIGRATIONS) == list(range(1, _legacy_cfg.CONFIG_VERSION))


# ── migration: text ─────────────────────────────────────────────────────────


def test_migrate_toml_text_matches_the_dict_migration():
    text, warnings = _legacy_cfg.migrate_toml_text(OLD_FULL)
    assert tomllib.loads(text) == NEW_FULL and warnings == []


def test_migrate_toml_text_puts_the_version_first():
    text, _ = _legacy_cfg.migrate_toml_text(OLD_FULL)
    assert text.startswith("config_version = 2\n\n[deploy]")
    # a top-level key after a table header would belong to that table
    assert tomllib.loads(text)["config_version"] == 2 and "config_version" not in tomllib.loads(text)["deploy"]


def test_migrate_toml_text_adds_only_the_version_to_an_unversioned_current_file():
    plain = '[deploy]\nname = "x"\n# a comment\n\n[markers.hls]\ncue_tags = "only"\n'
    out, _ = _legacy_cfg.migrate_toml_text(plain)
    assert out == "config_version = 2\n\n" + plain


def test_migrate_toml_text_replaces_an_older_explicit_version_in_place():
    out, _ = _legacy_cfg.migrate_toml_text('# header\nconfig_version = 1\n\n[packaging]\ndvr_window_seconds = 9\n')
    assert out.count("config_version") == 1 and out.startswith("# header\nconfig_version = 2\n")
    assert tomllib.loads(out) == {"config_version": 2, "packaging": {"dvr_window": 9}}


def test_migrate_toml_text_is_byte_stable_once_current():
    text, _ = _legacy_cfg.migrate_toml_text(OLD_FULL)
    again, _ = _legacy_cfg.migrate_toml_text(text)
    assert again == text


def test_migrate_toml_text_keeps_comments_with_their_keys():
    text = """\
[markers]
# keep the wire format readable
# (two comment lines)
dash_signal_format = "xml"  # inline note
cue_tags = "none"

[packaging]
# window of the DVR
dvr_window_seconds = 60  # seconds
"""
    out, _ = _legacy_cfg.migrate_toml_text(text)
    assert '# keep the wire format readable\n# (two comment lines)\nsignal_format = "xml"  # inline note' in out
    assert "# window of the DVR\ndvr_window = 60  # seconds" in out
    assert out.index("[markers.dash]") < out.index("# keep the wire format readable")
    assert tomllib.loads(out) == {
        "config_version": 2,
        "markers": {"dash": {"signal_format": "xml"}, "hls": {"cue_tags": "none"}},
        "packaging": {"dvr_window": 60},
    }


def test_migrate_toml_text_handles_multiline_arrays_and_strings():
    text = '''\
[markers]
period_on_segmentation = [
  0x22,   # break start
  0x30,
]
daterange_id_format = "a]b[c"
cue_tags = "none"
'''
    out, _ = _legacy_cfg.migrate_toml_text(text)
    assert tomllib.loads(out) == {
        "config_version": 2,
        "markers": {"split_on": [0x22, 0x30], "hls": {"id_template": "a]b[c", "cue_tags": "none"}},
    }
    assert "  0x22,   # break start" in out


def test_migrate_toml_text_places_new_tables_next_to_their_parent():
    out, _ = _legacy_cfg.migrate_toml_text(OLD_FULL)
    order = [line for line in out.splitlines() if line.startswith("[")]
    assert order == [
        "[deploy]", "[markers]", "[markers.hls]", "[markers.dash]",
        "[packaging]", "[packaging.hls]", "[packaging.dash]",
        "[timeline]", "[query]", "[timeshift]",
    ]


def test_migrate_toml_text_drops_tables_that_lose_every_key():
    out, _ = _legacy_cfg.migrate_toml_text('[timeshift]\nenabled = false\nstart_param = "s"\n')
    assert tomllib.loads(out) == {
        "config_version": 2, "query": {"timeshift": False, "overrides": False, "start_param": "s"},
    }
    assert "[timeshift]" not in out and "enabled" not in out


def test_migrate_toml_text_writes_a_value_under_both_new_switches():
    out, _ = _legacy_cfg.migrate_toml_text("[timeshift]\nenabled = true # on\nmax_span_seconds = 60\n")
    assert tomllib.loads(out)["query"] == {"timeshift": True, "overrides": True}
    assert tomllib.loads(out)["timeshift"] == {"max_span_seconds": 60}
    assert out.count("# on") == 2  # the inline note goes with each copy


def test_migrate_toml_text_migrates_the_development_query_enabled():
    out, _ = _legacy_cfg.migrate_toml_text('[query]\nenabled = false\nprefix = "dsl_"\n')
    assert tomllib.loads(out)["query"] == {"prefix": "dsl_", "timeshift": False, "overrides": False}


def test_migrate_toml_text_appends_to_an_existing_new_table():
    text = '[markers.hls]\nid_template = "x"\n\n[markers]\ncue_tags = "only"\ndaterange_mode = "grouped"\n'
    out, _ = _legacy_cfg.migrate_toml_text(text)
    assert tomllib.loads(out) == {
        "config_version": 2,
        "markers": {"hls": {"id_template": "x", "cue_tags": "only", "descriptor_grouping": "grouped"}},
    }


def test_migrate_toml_text_reports_dropped_settings():
    _, warnings = _legacy_cfg.migrate_toml_text('[markers]\nperiod_on_segmentation_apply = "hls"\n')
    assert warnings and "'hls'" in warnings[0]


def test_migrate_toml_text_rejects_invalid_toml():
    with pytest.raises(tomllib.TOMLDecodeError):
        _legacy_cfg.migrate_toml_text("[markers\n")


def test_migrate_toml_text_refuses_a_newer_version():
    with pytest.raises(ValueError, match="newer"):
        _legacy_cfg.migrate_toml_text("config_version = 9\n[deploy]\nname = 'x'\n")


# ── the migration CLI ───────────────────────────────────────────────────────


def test_cli_rewrites_files_and_directories(tmp_path, capsys):
    (tmp_path / "sub").mkdir()
    a, b = tmp_path / "a.toml", tmp_path / "sub" / "b.toml"
    a.write_text(OLD_FULL)
    b.write_text('[deploy]\nname = "b"\n[packaging]\ndvr_window_seconds = 10\n')
    (tmp_path / "current.toml").write_text('config_version = 2\n[deploy]\nname = "x"\n')
    (tmp_path / "not-a-channel.toml").write_text('[tool]\nname = "x"\n')
    assert migrate_config.main([str(tmp_path)]) == 0
    assert tomllib.loads(a.read_text()) == NEW_FULL
    assert tomllib.loads(b.read_text()) == {
        "config_version": 2, "deploy": {"name": "b"}, "packaging": {"dvr_window": 10},
    }
    out = capsys.readouterr().out
    assert "migrated:" in out and "config_version 1 -> 2" in out
    assert "current.toml" not in out and "not-a-channel.toml" not in out
    assert (tmp_path / "not-a-channel.toml").read_text() == '[tool]\nname = "x"\n'
    assert migrate_config.main([str(tmp_path)]) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_cli_adds_the_version_to_an_unversioned_but_current_file(tmp_path, capsys):
    f = tmp_path / "c.toml"
    f.write_text('[deploy]\nname = "x"\n\n[packaging]\ndvr_window = 30\n')
    assert migrate_config.main([str(f)]) == 0
    assert f.read_text().startswith("config_version = 2\n\n[deploy]")
    assert "config_version 1 -> 2" in capsys.readouterr().out


def test_cli_check_reports_without_writing(tmp_path, capsys):
    f = tmp_path / "old.toml"
    f.write_text(OLD_FULL)
    assert migrate_config.main(["--check", str(f)]) == 1
    assert f.read_text() == OLD_FULL
    assert "would migrate" in capsys.readouterr().out
    f.write_text('config_version = 2\n[deploy]\nname = "x"\n')
    assert migrate_config.main(["--check", str(f)]) == 0


def test_cli_check_flags_a_file_that_only_lacks_the_version(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[deploy]\nname = "x"\n')
    assert migrate_config.main(["--check", str(f)]) == 1


def test_cli_dry_run_prints_a_diff_and_writes_nothing(tmp_path, capsys):
    f = tmp_path / "old.toml"
    original = '[deploy]\nname = "x"\n[packaging]\ndvr_window_seconds = 10\n'
    f.write_text(original)
    assert migrate_config.main(["--dry-run", str(f)]) == 0
    assert f.read_text() == original
    out = capsys.readouterr().out
    assert "-dvr_window_seconds = 10" in out and "+dvr_window = 10" in out and "+config_version = 2" in out


def test_cli_warns_about_dropped_settings_on_stderr(tmp_path, capsys):
    f = tmp_path / "old.toml"
    f.write_text('[deploy]\nname = "x"\n[markers]\nperiod_on_segmentation = [0x22]\nperiod_on_segmentation_apply = "dash"\n')
    migrate_config.main([str(f)])
    assert "WARNING" in capsys.readouterr().err


def test_cli_skips_invalid_toml_and_missing_paths(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[oops\n")
    assert migrate_config.main([str(bad)]) == 0
    assert "skipped" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="no such file"):
        migrate_config.main([str(tmp_path / "missing.toml")])


# ── the configs shipped in the repo ─────────────────────────────────────────

REPO_CONFIGS = sorted([*(REPO / "data" / "channels").glob("*.toml"), *(ITS_A_LIVE / "configs").glob("*.toml"), ITS_A_LIVE / "config.toml"])


@pytest.mark.parametrize("path", REPO_CONFIGS, ids=lambda p: str(p.relative_to(REPO)))
def test_repo_configs_use_the_current_keys_and_validate(path):
    cfg = tomllib.loads(path.read_text())
    assert _legacy_cfg.find_legacy_keys(cfg) == [], f"{path}: run {_legacy_cfg.MIGRATE_COMMAND} {path}"
    assert cfg.get("config_version") == _legacy_cfg.CONFIG_VERSION, f"{path}: missing config_version"
    _legacy_cfg.check_config_version(cfg, str(path))
    _markers_cfg.markers_settings(cfg)
    _packaging_cfg.packaging_settings(cfg)
    _query_cfg.query_settings(cfg)
    _epoch_cfg.config_epoch_utc(cfg)


# ── galvanise.py's generated channel configs ────────────────────────────────


@pytest.mark.parametrize("backend", ["ecs-express", "aws-media"])
def test_galvanise_generated_config_uses_the_current_keys(backend):
    sys.path.insert(0, str(REPO))
    import galvanise

    cfg = tomllib.loads(galvanise._generate_toml(backend, "demo", "/tmp/demo.ts"))
    assert _legacy_cfg.find_legacy_keys(cfg) == []
    assert cfg["config_version"] == _legacy_cfg.CONFIG_VERSION
    _packaging_cfg.packaging_settings(cfg)
    if backend == "ecs-express":
        assert cfg["packaging"]["dvr_window"] == 30
