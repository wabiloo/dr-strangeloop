from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Optional

from .config import OutputConfig

logger = logging.getLogger(__name__)


def _key(
    source: Path,
    output: OutputConfig,
    start: Optional[float] = None,
    duration: Optional[float] = None,
) -> str:
    stat = source.stat()
    parts = (
        f"{source.resolve()}|{stat.st_mtime}|{stat.st_size}"
        f"|{output.resolution}|{output.framerate}|48000|setpts_bf0_shortest_v4"
        f"|start={start}|duration={duration}"
    )
    return hashlib.sha256(parts.encode()).hexdigest()[:24]


def lookup(
    source: Path,
    output: OutputConfig,
    cache_dir: Path,
    start: Optional[float] = None,
    duration: Optional[float] = None,
) -> Optional[Path]:
    """Return a cached normalized file path if it exists, otherwise None."""
    path = cache_dir / f"{_key(source, output, start, duration)}.mp4"
    if path.exists():
        logger.info("Cache hit for %s → %s", source.name, path.name)
        return path
    return None


def store(
    normalized: Path,
    source: Path,
    output: OutputConfig,
    cache_dir: Path,
    start: Optional[float] = None,
    duration: Optional[float] = None,
) -> Path:
    """Copy a freshly-normalized file into the cache. Returns the cache path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{_key(source, output, start, duration)}.mp4"
    shutil.copy2(normalized, dest)
    logger.info("Cached %s → %s", source.name, dest.name)
    return dest
