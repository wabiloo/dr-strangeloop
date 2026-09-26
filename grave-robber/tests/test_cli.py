"""Smoke tests for cli.py's subcommands against the real HAR fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.cli import main  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "trace-shrink"
MEDIA_PLAYLIST_URL = (
    "https://stream.broadpeak.io/hls/live/2003678/ndtv24x7/masterp_720p@3.m3u8"
    "?bpkio_serviceid=300d1539c3b6aa17a79a8fd9f1e45448"
    "&bpkio_sessionid=10a0d36d4-f64af804-55e5-4e25-8b94-119d626d0bf2"
    "&category=all&mm_sp"
)


def test_ingest_subcommand_writes_manifest(tmp_path):
    archive_path = FIXTURES_DIR / "hls1-chrome.har"
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")
    output_dir = tmp_path / "out"

    exit_code = main(["ingest", str(archive_path), MEDIA_PLAYLIST_URL, "--output", str(output_dir)])

    assert exit_code == 0
    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert len(manifest["segments"]) > 0


def test_ingest_subcommand_explicit_format_override(tmp_path):
    archive_path = FIXTURES_DIR / "hls1-chrome.har"
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")
    output_dir = tmp_path / "out"

    exit_code = main(
        ["ingest", str(archive_path), MEDIA_PLAYLIST_URL, "--output", str(output_dir), "--format", "hls"]
    )

    assert exit_code == 0


def test_coverage_subcommand_reports_variants(tmp_path, capsys):
    archive_path = FIXTURES_DIR / "hls1-chrome.har"
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")

    exit_code = main(["coverage", str(archive_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "HLS" in out


def test_ingest_subcommand_returns_nonzero_on_bad_manifest_url(tmp_path):
    archive_path = FIXTURES_DIR / "hls1-chrome.har"
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")

    exit_code = main(
        ["ingest", str(archive_path), "https://nowhere.example.com/does-not-exist.m3u8", "--output", str(tmp_path / "out")]
    )

    assert exit_code == 1
