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


def test_production_entrypoint_forwards_timeshift_options(tmp_path: Path):
    """SCOPE.md §13: the timeshift CLI flags must reach wsgi.py as env vars."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_path = tmp_path / "gunicorn.json"
    (bin_dir / "aws").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "aws").chmod(0o755)
    keys = [
        "TIMESHIFT", "TIMESHIFT_START_PARAM", "TIMESHIFT_END_PARAM",
        "TIMESHIFT_MAX_SPAN_SECONDS", "QUERY_PREFIX", "QUERY_OVERRIDES",
    ]
    (bin_dir / "gunicorn").write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"json.dump({{k: os.environ.get(k) for k in {keys!r}}}, open(os.environ['CAPTURE_FILE'], 'w'))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    (bin_dir / "gunicorn").chmod(0o755)
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
            "bash", str(ENTRYPOINT), "serve.py", "--epoch-utc", "2026-01-01T00:00:00Z",
            "--timeshift", "--timeshift-start-param", "from", "--timeshift-end-param", "to",
            "--timeshift-max-span-seconds", "600", "--query-prefix", "dsl_", "--query-overrides",
        ],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(capture_path.read_text(encoding="utf-8")) == {
        "TIMESHIFT": "true",
        "TIMESHIFT_START_PARAM": "from",
        "TIMESHIFT_END_PARAM": "to",
        "TIMESHIFT_MAX_SPAN_SECONDS": "600",
        "QUERY_PREFIX": "dsl_",
        "QUERY_OVERRIDES": "true",
    }


def test_dockerfile_copies_every_module_serve_imports():
    """serve.py/wsgi.py import sibling modules; the image copies files by
    name, so a new module that isn't listed fails at container start."""
    import re

    dockerfile = (ENTRYPOINT.parent / "Dockerfile").read_text(encoding="utf-8")
    copied = set(re.search(r"^COPY (bake\.py .*?) \./$", dockerfile, re.M).group(1).split())
    local_modules = {p.stem for p in ENTRYPOINT.parent.glob("*.py")}
    for entry in ("serve.py", "wsgi.py"):
        source = (ENTRYPOINT.parent / entry).read_text(encoding="utf-8")
        imported = set(re.findall(r"^(?:import|from) (\w+)", source, re.M))
        for module in imported & local_modules:
            assert f"{module}.py" in copied, f"{entry} imports {module}.py but the Dockerfile does not COPY it"


def test_s3_store_entrypoint_skips_sync_and_runs_one_threaded_worker(tmp_path: Path):
    """aws-lambda backend: LOOP_PACKAGE_STORE=s3 must not `aws s3 sync` the
    package (wsgi.py reads it lazily) and must run a single gthread worker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_path = tmp_path / "gunicorn.json"
    (bin_dir / "aws").write_text("#!/bin/sh\necho aws-called >&2\nexit 1\n", encoding="utf-8")
    (bin_dir / "aws").chmod(0o755)
    (bin_dir / "gunicorn").write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "json.dump({'argv': sys.argv[1:], 'store': os.environ.get('LOOP_PACKAGE_STORE'),"
        " 'uri': os.environ.get('LOOP_PACKAGE_S3_URI'), 'dir': os.environ.get('LOOP_PACKAGE_DIR')},"
        " open(os.environ['CAPTURE_FILE'], 'w'))\nsys.exit(0)\n",
        encoding="utf-8",
    )
    (bin_dir / "gunicorn").chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "LOOP_PACKAGE_STORE": "s3",
            "LOOP_PACKAGE_S3_URI": "s3://example/channel",
            "LOOP_PACKAGE_LOCAL_DIR": str(tmp_path / "package"),
            "CAPTURE_FILE": str(capture_path),
        }
    )
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "serve.py", "--epoch-utc", "2026-01-01T00:00:00Z", "--continuous-timeline"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "aws-called" not in result.stderr
    got = json.loads(capture_path.read_text(encoding="utf-8"))
    assert got["store"] == "s3" and got["uri"] == "s3://example/channel"
    assert got["dir"] == str(tmp_path / "package")
    argv = got["argv"]
    assert argv[argv.index("--workers") + 1] == "1"
    assert argv[argv.index("--worker-class") + 1] == "gthread"
    assert argv[-1] == "wsgi:app"


def test_s3_store_entrypoint_requires_a_uri(tmp_path: Path):
    env = os.environ.copy()
    env.update({"LOOP_PACKAGE_STORE": "s3", "LOOP_PACKAGE_LOCAL_DIR": str(tmp_path / "p")})
    env.pop("LOOP_PACKAGE_S3_URI", None)
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "serve.py", "--epoch-utc", "2026-01-01T00:00:00Z"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
