import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dr_strangeloop_config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv(cfg.ENV_VAR, raising=False)
    monkeypatch.setattr(cfg, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)


def _write(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / cfg.CONFIG_FILENAME
    f.write_text(body)
    return f


def test_defaults_without_config(tmp_path):
    p = cfg.load_paths(cfg.find_config_file())
    assert p.config_file is None
    assert p.channels_dir == tmp_path / "repo" / "data" / "channels"
    assert p.outputs_dir == tmp_path / "repo" / "outputs"
    assert p.archive_imports_dir == tmp_path / "repo" / "outputs" / "archives"


def test_relative_paths_resolve_against_config_dir(tmp_path):
    f = _write(tmp_path / "work" / cfg.CONFIG_DIRNAME, '[paths]\ndata_dir = "../store"\n')
    p = cfg.load_paths(f)
    assert p.data_dir == tmp_path / "work" / "store"
    assert p.playlists_dir == tmp_path / "work" / "store" / "playlists"
    assert p.outputs_dir == tmp_path / "work" / ".dr-strangeloop" / "outputs"


def test_empty_config_defaults_inside_its_own_dir(tmp_path):
    f = _write(tmp_path / "home" / cfg.CONFIG_DIRNAME, "")
    p = cfg.load_paths(f)
    d = tmp_path / "home" / cfg.CONFIG_DIRNAME
    assert p.data_dir == d / "data"
    assert p.channels_dir == d / "data" / "channels"
    assert p.outputs_dir == d / "outputs"
    assert p.manifest_imports_dir == d / "outputs" / "manifests"
    assert p.local_package_dir == d / "packages"


def test_specific_dir_overrides_derived(tmp_path):
    f = _write(tmp_path / cfg.CONFIG_DIRNAME, '[paths]\ndata_dir = "/d"\nchannels_dir = "/c"\n')
    p = cfg.load_paths(f)
    assert p.channels_dir == Path("/c")
    assert p.playlists_dir == Path("/d/playlists")


def test_unknown_key_rejected(tmp_path):
    f = _write(tmp_path / cfg.CONFIG_DIRNAME, '[paths]\ndata_dri = "x"\n')
    with pytest.raises(ValueError, match="data_dri"):
        cfg.load_paths(f)


def test_lookup_order(tmp_path, monkeypatch):
    assert cfg.find_config_file() is None
    _write(tmp_path / "repo" / cfg.CONFIG_DIRNAME, "")  # repo-level dirs are not searched
    _write(tmp_path / cfg.CONFIG_DIRNAME, "")  # nor is the current directory
    assert cfg.find_config_file() is None
    home = _write(tmp_path / "home" / cfg.CONFIG_DIRNAME, "")
    assert cfg.find_config_file() == home
    explicit = _write(tmp_path / "x", "")
    monkeypatch.setenv(cfg.ENV_VAR, str(tmp_path / "x"))
    assert cfg.find_config_file() == explicit


def test_env_var_pointing_nowhere_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv(cfg.ENV_VAR, str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError):
        cfg.find_config_file()


def test_source_path_resolution(tmp_path, monkeypatch):
    toml = tmp_path / "channels" / "c.toml"
    toml.parent.mkdir()
    (tmp_path / "channels" / "x.ts").write_text("")
    assert cfg.resolve_source_path("x.ts", toml) == str(tmp_path / "channels" / "x.ts")
    assert cfg.resolve_source_path("/abs/y.ts", toml) == "/abs/y.ts"
    # legacy: only exists relative to the working directory
    (tmp_path / "legacy.ts").write_text("")
    assert cfg.resolve_source_path("legacy.ts", toml) == str(tmp_path / "legacy.ts")
    # nothing exists anywhere: TOML-relative
    assert cfg.resolve_source_path("new.ts", toml) == str(tmp_path / "channels" / "new.ts")
