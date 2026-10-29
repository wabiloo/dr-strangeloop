"""Byte-reading seam between serve.py and wherever the loop package lives.

Default: the package is a directory on the local filesystem and everything
reads straight from it (`read_bytes` is `Path.read_bytes`, `custom_reader()`
is False, serve.py keeps using `send_file`). The `aws-lambda` backend
installs a reader (s3_package.S3Package.read_bytes) that fetches segment
bytes from S3 on demand; see its-a-live/LAMBDA_BACKEND.md.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

_reader: Callable[[Path], bytes] | None = None


def install_reader(reader: Callable[[Path], bytes] | None) -> None:
    """Route every package read through `reader` (None = back to the
    filesystem). Process-wide: one loop package per process."""
    global _reader
    _reader = reader


def custom_reader() -> bool:
    return _reader is not None


def read_bytes(path: Path) -> bytes:
    return path.read_bytes() if _reader is None else _reader(path)
