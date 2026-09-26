"""RFC 6381 avc1 codec string: 'avc1.' + profile_idc + constraint flags + level_idc, 6 hex digits."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bake import _rfc6381_avc1_codec_string  # noqa: E402


def test_main_profile_level_3_1():
    assert _rfc6381_avc1_codec_string("Main", 3.1) == "avc1.4D001F"


def test_high_profile_level_4_0():
    assert _rfc6381_avc1_codec_string("High", 4.0) == "avc1.640028"


def test_declared_variant_overrides_probed_values():
    from bake import apply_declared_variant

    video = {"codecs": "avc1.4D001F", "bandwidth": 1}
    apply_declared_variant(video, {"codecs": "avc1.4D401F,mp4a.40.2", "bandwidth": 4161542})
    assert video == {"codecs": "avc1.4D401F", "bandwidth": 4161542}

    untouched = {"codecs": "avc1.4D001F", "bandwidth": 1}
    apply_declared_variant(untouched, None)
    assert untouched == {"codecs": "avc1.4D001F", "bandwidth": 1}


def test_avcc_codec_string_read_from_file(tmp_path):
    from bake import read_avcc_codec_string

    f = tmp_path / "x.m4s"
    f.write_bytes(b"\x00\x00\x00\x20avcC\x01\x4d\x40\x1f\xff\xe1")
    assert read_avcc_codec_string(f) == "avc1.4D401F"
    g = tmp_path / "y.m4s"
    g.write_bytes(b"no box here")
    assert read_avcc_codec_string(g) is None
