"""Serve a loop package that lives in S3 without syncing it (aws-lambda
backend, its-a-live/LAMBDA_BACKEND.md §3).

`materialize` builds a *skeleton* of the package under a local directory:
every small file (descriptor, cues, init segments, ...) is downloaded for
real, every media segment (`*.m4s`, `*.ts`) becomes a sparse placeholder of
the right size. `LoopPackage` only globs, counts and `.exists()`-checks
those, so it loads the skeleton unchanged. The bytes of a segment are then
fetched on demand by `S3Package.read_bytes` (installed through package_io)
and kept in a bounded on-disk LRU.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

import package_io

logger = logging.getLogger(__name__)

# Media segments are the only large objects in a package; everything else
# (loop_descriptor.json, cues/*.xml, *_init.mp4, hls-ts playlists) is read at
# startup and must be real.
PLACEHOLDER_SUFFIXES = (".m4s", ".ts")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    if not bucket:
        raise ValueError(f"no bucket in {uri!r}")
    return bucket, prefix.strip("/")


def _default_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=3, read_timeout=15),
    )


class S3Package:
    def __init__(
        self,
        s3_uri: str,
        local_dir: Path,
        *,
        cache_dir: Path | None = None,
        cache_bytes: int = 512 * 1024 * 1024,
        client=None,
    ):
        self.bucket, self.prefix = parse_s3_uri(s3_uri)
        self.local_dir = Path(local_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else self.local_dir.parent / (self.local_dir.name + "-cache")
        self.cache_bytes = cache_bytes
        self._client = client
        # local placeholder path (as str) -> (S3 key, size)
        self._placeholders: dict[str, tuple[str, int]] = {}
        self._lru: OrderedDict[str, int] = OrderedDict()  # cache file name -> size
        self._lru_total = 0
        self._lock = threading.Lock()

    @property
    def client(self):
        if self._client is None:
            self._client = _default_client()
        return self._client

    def materialize(self) -> Path:
        """Build the skeleton; returns the package directory."""
        if self.local_dir.exists():
            shutil.rmtree(self.local_dir)
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.local_dir.mkdir(parents=True)
        self.cache_dir.mkdir(parents=True)
        self._placeholders.clear()
        self._lru.clear()
        self._lru_total = 0

        list_prefix = f"{self.prefix}/" if self.prefix else ""
        n_real = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=list_prefix):
            for obj in page.get("Contents", []):
                key, size = obj["Key"], int(obj["Size"])
                rel = key[len(list_prefix):]
                if not rel or rel.endswith("/"):
                    continue
                dest = self.local_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if rel.endswith(PLACEHOLDER_SUFFIXES):
                    with dest.open("wb") as f:
                        f.truncate(size)
                    self._placeholders[str(dest)] = (key, size)
                else:
                    dest.write_bytes(self._get(key))
                    n_real += 1
        if not (self.local_dir / "loop_descriptor.json").is_file():
            raise RuntimeError(f"no loop_descriptor.json under s3://{self.bucket}/{list_prefix}")
        logger.info(
            "materialized s3://%s/%s: %d real file(s), %d segment placeholder(s)",
            self.bucket, list_prefix, n_real, len(self._placeholders),
        )
        return self.local_dir

    def _get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def read_bytes(self, path: Path) -> bytes:
        entry = self._placeholders.get(str(path))
        if entry is None:
            return Path(path).read_bytes()
        key, size = entry
        cached = self.cache_dir / key.replace("/", "__")
        with self._lock:
            if cached.name in self._lru:
                self._lru.move_to_end(cached.name)
                try:
                    return cached.read_bytes()
                except OSError:
                    self._lru_total -= self._lru.pop(cached.name)
        data = self._get(key)
        if self.cache_bytes >= len(data):
            tmp = cached.with_name(f"{cached.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cached)
            with self._lock:
                if cached.name not in self._lru:
                    self._lru[cached.name] = len(data)
                    self._lru_total += len(data)
                while self._lru_total > self.cache_bytes and len(self._lru) > 1:
                    old, old_size = self._lru.popitem(last=False)
                    self._lru_total -= old_size
                    (self.cache_dir / old).unlink(missing_ok=True)
        return data

    def install(self) -> None:
        package_io.install_reader(self.read_bytes)
