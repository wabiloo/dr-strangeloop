"""Production Docker entrypoint option forwarding tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


def test_production_entrypoint_forwards_continuous_timeline(tmp_path: Path):
    """The S3-backed production path must translate the serve flag for WSGI."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_path = tmp_path / "gunicorn.json"

    aws_stub = bin_dir / "aws"
    aws_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aws_stub.chmod(0o755)

    gunicorn_stub = bin_dir / "gunicorn"
    gunicorn_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE_FILE'], 'w') as f:\n"
        "    json.dump({k: os.environ.get(k) for k in ("
        "'LOOP_PACKAGE_DIR', 'EPOCH_UTC', 'DVR_WINDOW_SECONDS', "
        "'WINDOW_SEGMENTS', 'CONTINUOUS_TIMELINE')}, f)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    gunicorn_stub.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "LOOP_PACKAGE_S3_URI": "s3://example/channel",
            "LOOP_PACKAGE_LOCAL_DIR": str(tmp_path / "package"),
            "CAPTURE_FILE": str(capture_path),
        }
    )
    result = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT),
            "serve.py",
            "--epoch-utc",
            "2026-01-01T00:00:00Z",
            "--port",
            "8080",
            "--dvr-window-seconds",
            "30",
            "--continuous-timeline",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    forwarded = json.loads(capture_path.read_text(encoding="utf-8"))
    assert forwarded == {
        "LOOP_PACKAGE_DIR": str(tmp_path / "package"),
        "EPOCH_UTC": "2026-01-01T00:00:00Z",
        "DVR_WINDOW_SECONDS": "30",
        "WINDOW_SEGMENTS": "",
        "CONTINUOUS_TIMELINE": "true",
    }
