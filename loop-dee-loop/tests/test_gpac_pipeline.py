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
