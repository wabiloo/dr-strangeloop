"""S3-backed loop package (aws-lambda backend, its-a-live/LAMBDA_BACKEND.md):
the skeleton built by s3_package.materialize must make serve.py answer
byte-for-byte what it answers from a plain directory, with segment bytes
fetched lazily from S3."""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import package_io  # noqa: E402
import serve  # noqa: E402
from s3_package import S3Package, parse_s3_uri  # noqa: E402
from serve import create_app  # noqa: E402
from test_serve_continuity import _write_continuous_package  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

BUCKET, PREFIX = "bkt", "its-a-live/content/chan"


class FakeS3:
    """The three calls s3_package makes, served from a local directory."""

    def __init__(self, root: Path):
        self.root = root
        self.gets: list[str] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix):
                assert Bucket == BUCKET
                files = sorted(p for p in outer.root.rglob("*") if p.is_file())
                contents = [
                    {"Key": f"{PREFIX}/{p.relative_to(outer.root).as_posix()}", "Size": p.stat().st_size}
                    for p in files
                ]
                # two pages, to exercise pagination
                yield {"Contents": contents[:2]}
                yield {"Contents": contents[2:]}

        return _P()

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        return {"Body": io.BytesIO((self.root / Key[len(PREFIX) + 1:]).read_bytes())}


@pytest.fixture(autouse=True)
def _reset_reader():
    yield
    package_io.install_reader(None)


def _s3_app(tmp_path, source, **kw):
    fake = FakeS3(source)
    pkg = S3Package(f"s3://{BUCKET}/{PREFIX}", tmp_path / "skeleton", client=fake, **kw)
    pkg.materialize()
    pkg.install()
    return create_app(tmp_path / "skeleton", epoch_ticks=0, window_segments=4, continuous=True), fake, pkg


def test_parse_s3_uri():
    assert parse_s3_uri("s3://b/a/b/") == ("b", "a/b")
    with pytest.raises(ValueError):
        parse_s3_uri("http://b/a")


def test_skeleton_has_real_small_files_and_sparse_segment_placeholders(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_continuous_package(src)
    fake = FakeS3(src)
    pkg = S3Package(f"s3://{BUCKET}/{PREFIX}", tmp_path / "skel", client=fake)
    pkg.materialize()
    seg = tmp_path / "skel" / "segments" / "1080p" / "seg_track1_0.m4s"
    assert seg.stat().st_size == (src / "segments" / "1080p" / "seg_track1_0.m4s").stat().st_size
    assert seg.read_bytes() == b"\0" * seg.stat().st_size  # placeholder, not the media
    init = tmp_path / "skel" / "segments" / "1080p" / "seg_track1_init.mp4"
    assert init.read_bytes() == (src / "segments" / "1080p" / "seg_track1_init.mp4").read_bytes()
    assert not any(k.endswith(".m4s") for k in fake.gets)  # no segment downloaded at startup


def test_missing_descriptor_is_an_error(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.json").write_text("{}")
    with pytest.raises(RuntimeError, match="loop_descriptor.json"):
        S3Package(f"s3://{BUCKET}/{PREFIX}", tmp_path / "skel", client=FakeS3(tmp_path / "src")).materialize()


def test_s3_store_is_byte_identical_to_the_filesystem_store(tmp_path, monkeypatch):
    monkeypatch.setattr(serve.time, "time", lambda: 1_800_000_000.0)
    src = tmp_path / "src"
    src.mkdir()
    _write_continuous_package(src)

    fs_client = create_app(src, epoch_ticks=0, window_segments=4, continuous=True).test_client()
    paths = ["/index.m3u8", "/video.m3u8", "/index.mpd", "/timeline.json", "/1080p/init.mp4",
             "/1080p/seg/0.m4s", "/1080p/seg/1.m4s", "/1080p/cseg/0.m4s", "/1080p/cseg/3.m4s",
             "/1080p/cseg/1234567.m4s", "/1080p/cseg/999999999999.m4s"]
    expected = {p: fs_client.get(p) for p in paths}

    app, fake, _ = _s3_app(tmp_path, src)
    client = app.test_client()
    for p in paths:
        got = client.get(p)
        assert got.status_code == expected[p].status_code, p
        assert got.data == expected[p].data, p
        assert got.mimetype == expected[p].mimetype, p
    assert any(k.endswith("seg_track1_0.m4s") for k in fake.gets)


def test_segment_cache_serves_repeats_without_s3_and_evicts_by_size(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_continuous_package(src)
    seg_size = (src / "segments" / "1080p" / "seg_track1_0.m4s").stat().st_size

    app, fake, pkg = _s3_app(tmp_path, src)
    client = app.test_client()
    client.get("/1080p/seg/0.m4s")
    client.get("/1080p/seg/0.m4s")
    assert sum(k.endswith("seg_track1_0.m4s") for k in fake.gets) == 1

    # room for roughly one segment: fetching the other evicts the first
    pkg.cache_bytes = seg_size + 1
    client.get("/1080p/seg/1.m4s")
    fake.gets.clear()
    client.get("/1080p/seg/0.m4s")
    assert any(k.endswith("seg_track1_0.m4s") for k in fake.gets)
