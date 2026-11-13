"""Tests for generator assets: config validation, command building, caching."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from franken_ts import generated_source
from franken_ts.config import AssetConfig, OutputConfig
from franken_ts.generated_source import build_command, resolve_generated_assets

OUT = OutputConfig(file=Path("o.ts"), resolution="1280x720", framerate=25)


def test_requires_exactly_one_of_file_generator():
    with pytest.raises(ValidationError):
        AssetConfig()
    with pytest.raises(ValidationError):
        AssetConfig(file="a.mp4", generator={"duration": 5})
    a = AssetConfig(generator={"duration": "5s"})
    assert a.file.name.startswith("generator-")


def test_generator_rejects_start():
    with pytest.raises(ValidationError):
        AssetConfig(generator={"duration": 5}, start=1)


def test_non_positive_duration():
    with pytest.raises(ValidationError):
        AssetConfig(generator={"duration": 0})


def test_command_preset_and_params():
    a = AssetConfig(generator={"duration": 10, "preset": "smptebars", "timecode": True})
    cmd = build_command(a.generator, OUT, Path("x.mp4"))
    assert "smptebars=size=1280x720:rate=25" in cmd
    assert cmd[cmd.index("-g") + 1] == "50"
    assert "-t" in cmd and "10.000000" in cmd
    assert "drawtext" in cmd[cmd.index("-vf") + 1]


def test_command_raw_video_gets_size_rate():
    a = AssetConfig(generator={"duration": 5, "video": "mandelbrot", "audio": "silence"})
    cmd = build_command(a.generator, OUT, Path("x.mp4"))
    assert "mandelbrot=size=1280x720:rate=25" in cmd
    assert any(c.startswith("anullsrc") for c in cmd)


def test_resolve_caches(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"x")

    monkeypatch.setattr(generated_source, "run_cmd", fake_run)
    assets = [AssetConfig(generator={"duration": 3}), AssetConfig(file="real.mp4")]
    r = resolve_generated_assets(assets, OUT, tmp_path)
    assert len(r) == 1 and not r[0].cached and assets[0].file.exists()
    assert assets[1].file == Path("real.mp4")
    r2 = resolve_generated_assets([AssetConfig(generator={"duration": 3})], OUT, tmp_path)
    assert r2[0].cached and len(calls) == 1
