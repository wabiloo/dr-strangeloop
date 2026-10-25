"""H.264 profile/level on output + per-rendition overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from franken_ts.config import OutputConfig


def _ladder(**extra):
    return OutputConfig(
        dir="out",
        renditions=[
            {"name": "hd", "resolution": "1920x1080", "bitrate_kbps": 5000},
            {"name": "sd", "resolution": "640x360", "bitrate_kbps": 800, "profile": "baseline", "level": 3.0},
        ],
        **extra,
    )


def test_defaults_match_previous_hardcoded_values():
    cfg = OutputConfig(file="a.ts")
    assert (cfg.profile, cfg.level) == ("high", "4.0")


def test_yaml_numeric_levels_are_normalised():
    assert OutputConfig(file="a.ts", level=4.1).level == "4.1"
    assert OutputConfig(file="a.ts", level=4).level == "4.0"


def test_rendition_overrides_inherit_otherwise():
    cfg = _ladder(profile="main", level="4.1")
    hd, sd = (cfg.for_rendition(r) for r in cfg.renditions)
    assert (hd.profile, hd.level) == ("main", "4.1")
    assert (sd.profile, sd.level) == ("baseline", "3.0")


@pytest.mark.parametrize("kwargs", [{"profile": "extended"}, {"level": "9.9"}, {"level": "high"}])
def test_invalid_values_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        OutputConfig(file="a.ts", **kwargs)
