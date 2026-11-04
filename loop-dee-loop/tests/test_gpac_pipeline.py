from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gpac_pipeline  # noqa: E402


def test_run_gpac_dasher_requests_64_bit_tfdt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gpac_pipeline, "check_gpac_available", lambda: Path("/usr/bin/gpac"))

    gpac_pipeline.run_gpac_dasher(
        tmp_path / "input.ts",
        tmp_path / "cues.xml",
        tmp_path / "output",
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert output.count("--mp4mx@tfdt64") == 2


def _cmds(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("[dry-run]")]


def test_run_gpac_dasher_default_is_one_chunk_per_segment(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gpac_pipeline, "check_gpac_available", lambda: Path("/usr/bin/gpac"))
    gpac_pipeline.run_gpac_dasher(tmp_path / "i.ts", tmp_path / "c.xml", tmp_path / "o", dry_run=True)
    for cmd in _cmds(capsys):
        assert ":segdur=4.0:cdur=4.0:" in cmd
        assert "tfdt_traf" not in cmd


def test_run_gpac_dasher_chunked_sets_cdur_and_per_chunk_tfdt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gpac_pipeline, "check_gpac_available", lambda: Path("/usr/bin/gpac"))
    gpac_pipeline.run_gpac_dasher(
        tmp_path / "i.ts", tmp_path / "c.xml", tmp_path / "o", chunk_duration_seconds=0.5, dry_run=True
    )
    cmds = _cmds(capsys)
    assert len(cmds) == 2
    for cmd in cmds:
        assert ":segdur=4.0:cdur=0.5:" in cmd
        assert "--mp4mx@tfdt_traf" in cmd
