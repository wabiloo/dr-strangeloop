"""Production Docker entrypoint option forwarding tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


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


def _local_imports(path: Path, local: set[str]) -> set[str]:
    """The sibling modules / packages (names in `local`) a source file imports, at any indentation."""
    import re

    source = path.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*(?:import|from) (\w+)", source, re.M)) & local


@pytest.mark.parametrize("dockerfile_name", ["Dockerfile", "Dockerfile.lambda"])
def test_dockerfile_copies_every_module_serve_imports(dockerfile_name):
    """serve.py/wsgi.py and the `serving/` package import sibling modules; an image copies files by name, so a
    module that isn't listed fails at container start. Walks the whole import graph (a module imported only by
    `serving/` counts) and understands a copied package directory."""
    import re

    root = ENTRYPOINT.parent
    dockerfile = (root / dockerfile_name).read_text(encoding="utf-8")
    copied: set[str] = set()
    for line in re.findall(r"^COPY (.*?) \./(?:\S*)$", dockerfile, re.M):
        copied.update(line.split())
    modules = {p.stem for p in root.glob("*.py")}
    packages = {p.name for p in root.iterdir() if p.is_dir() and (p / "__init__.py").is_file()}
    local = modules | packages

    pending = [root / "serve.py", root / "wsgi.py"]
    seen: set[Path] = set()
    needed: dict[str, Path] = {}
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _local_imports(path, local):
            needed.setdefault(name, path)
            if name in packages:
                pending.extend(sorted((root / name).rglob("*.py")))
            else:
                pending.append(root / f"{name}.py")
    for name, importer in sorted(needed.items()):
        if name in packages:
            assert f"{name}/" in copied, f"{importer.name} imports package {name}/ but {dockerfile_name} does not COPY it"
        else:
            assert f"{name}.py" in copied, f"{importer.name} imports {name}.py but {dockerfile_name} does not COPY it"


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


def test_dockerignore_skips_bytecode_at_any_depth():
    """The serving/ package writes __pycache__ below the top level; the CDK image asset hashes this directory (see
    .dockerignore), so nested bytecode must be ignored or the image tag would change on every local run."""
    patterns = [
        line.strip() for line in (ENTRYPOINT.parent / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "**/__pycache__/" in patterns and "**/*.pyc" in patterns
