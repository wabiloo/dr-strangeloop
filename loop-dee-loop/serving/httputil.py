"""Small HTTP helpers."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import package_io
from flask import Response, send_file


def _is_loopback_host(host: str) -> bool:
    """Whether a request's Host header names this machine (localhost / 127.0.0.1 / ::1, any port)."""
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    else:
        name = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
    return name in ("localhost", "127.0.0.1", "::1") or name.endswith(".localhost")


def _send_package_file(path: Path, mimetype: str | None = None):
    """`send_file` for a file inside the loop package. When a custom reader is
    installed (package_io; the aws-lambda backend's S3-backed package) the
    bytes come from it instead of the local path."""
    if not package_io.custom_reader():
        return send_file(path, mimetype=mimetype)
    return Response(package_io.read_bytes(path), mimetype=mimetype or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
