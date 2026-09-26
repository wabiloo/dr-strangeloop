"""The HTML is re-rendered from the saved JSON when it is missing or stale."""

from __future__ import annotations

import json
import os
import time

from igor.integrations import scte_verify

REPORT = {
    "tool": "krogh", "report_version": 1, "generated_at": "2026-01-01T00:00:00+00:00",
    "source": {"path": "x.ts", "name": "x.ts", "codec": "h264", "width": 640, "height": 360,
               "fps": 25.0, "duration": 10.0, "scte35_table_id": "0xFC", "scte35_pid": None},
    "markers": [], "frames": {}, "checks": [],
    "summary": {"marker_count": 0, "checks_total": 0, "checks_failed": 0},
}


def test_ensure_html_renders_when_missing_and_skips_when_fresh(tmp_path, monkeypatch):
    ts = tmp_path / "x.ts"
    ts.write_bytes(b"")
    monkeypatch.setattr(scte_verify.franken_ts, "output_ts_path", lambda name: ts)
    out = tmp_path / "x_scte"
    out.mkdir()
    (out / "scte-report.json").write_text(json.dumps(REPORT))

    html = scte_verify.ensure_html("x", "filmstrip")
    assert html.is_file() and "Inspector Krogh" in html.read_text()

    first = html.stat().st_mtime
    time.sleep(0.05)
    assert scte_verify.ensure_html("x", "filmstrip") == html
    assert html.stat().st_mtime == first  # fresh -> not re-rendered

    # An older HTML than the JSON (e.g. after a re-scan) is regenerated.
    os.utime(html, (first - 100, first - 100))
    scte_verify.ensure_html("x", "filmstrip")
    assert html.stat().st_mtime > first - 100
