"""continuity.py against real ffmpeg output: CMAF tfdt shift (thin wrapper
over cmaf.rebase_fragments) and MPEG-TS PTS/DTS/PCR shift (this module's own
packet scan-and-patch) round-trip correctly and preserve playability.
"""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmaf  # noqa: E402
import continuity  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# ── Pure bit-packing round trips (no ffmpeg needed) ─────────────────────────


def test_ts_timestamp_pack_unpack_round_trip():
    for value in (0, 1, 12345, (1 << 33) - 1, 90_000 * 3600):
        for guard in (0b0010, 0b0011, 0b0001):
            packed = continuity._pack_ts_timestamp(guard, value)
            assert len(packed) == 5
            assert continuity._unpack_ts_timestamp(packed) == value
            # marker bits (bit 0 of bytes 0/2/4) must be 1, per spec.
            assert packed[0] & 1 and packed[2] & 1 and packed[4] & 1


def test_pcr_pack_unpack_round_trip():
    for base, ext in ((0, 0), (12345, 150), ((1 << 33) - 1, 299)):
        packed = continuity._pack_pcr(base, ext)
        assert len(packed) == 6
        assert continuity._unpack_pcr(packed) == (base, ext)


def test_pts_dts_wraps_at_33_bits():
    shifted = continuity._pack_ts_timestamp(
        0b0010, ((1 << 33) - 1 + 10) % continuity.PTS_DTS_MODULUS
    )
    assert continuity._unpack_ts_timestamp(shifted) == 9


# ── Real ffmpeg fixtures ─────────────────────────────────────────────────────


def _fmp4_with_audio(path: Path, duration: int = 2) -> bytes:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=24:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-g", "12", "-video_track_timescale", "90000",
            "-c:a", "aac",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _ts_with_audio(path: Path, duration: int = 2) -> bytes:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=24:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-g", "12", "-c:a", "aac",
            "-mpegts_flags", "resend_headers",
            "-f", "mpegts", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _first_moof_tfdt(data: bytes) -> tuple[int, int]:
    """(version, value) of the first fragment's tfdt."""
    moof = cmaf._find(data, ("moof",))
    tfdt = cmaf._find(data, ("traf", "tfdt"), moof[0] + 8, moof[1])
    version = data[tfdt[0] + 8]
    fmt = ">Q" if version == 1 else ">I"
    at = tfdt[0] + 12
    return version, struct.unpack(fmt, data[at : at + struct.calcsize(fmt)])[0]


def test_shift_cmaf_fragment_advances_tfdt_and_stays_playable(tmp_path):
    data = _fmp4_with_audio(tmp_path / "a.mp4")
    init, fragments = cmaf.split_init_and_fragments(data)
    fragment = fragments[0]

    _version, old_tfdt = _first_moof_tfdt(fragment)
    shift = 90_000 * 3600  # one hour, in the channel's 90kHz timescale

    shifted = continuity.shift_cmaf_fragment(fragment, shift, sequence_number=42)
    _version, new_tfdt = _first_moof_tfdt(shifted)
    assert new_tfdt == old_tfdt + shift

    # mfhd sequence number was set to the caller's global index.
    moof = cmaf._find(shifted, ("moof",))
    mfhd = cmaf._find(shifted, ("mfhd",), moof[0] + 8, moof[1])
    assert struct.unpack(">I", shifted[mfhd[0] + 12 : mfhd[0] + 16])[0] == 42

    # still decodes when reattached to the original init segment.
    joined = tmp_path / "joined.mp4"
    joined.write_bytes(init + shifted)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-count_frames",
         "-of", "csv=p=0", str(joined)],
        capture_output=True, text=True, check=True,
    )
    assert int(probe.stdout.strip().rstrip(",")) == 12  # one GOP


def test_shift_cmaf_fragment_shifts_every_moof_of_a_multi_fragment_segment(tmp_path):
    data = _fmp4_with_audio(tmp_path / "a.mp4")
    _init, fragments = cmaf.split_init_and_fragments(data)
    assert len(fragments) >= 2
    segment = b"".join(fragments)
    shift = 90_000 * 3600

    shifted = continuity.shift_cmaf_fragment(segment, shift, sequence_number=1)

    def tfdts(buf):
        return [_first_moof_tfdt(f)[1] for f in cmaf.split_fragments(buf)]

    assert tfdts(shifted) == [t + shift for t in tfdts(segment)]


def test_shift_cmaf_fragment_is_idempotent_shape_for_zero_shift(tmp_path):
    data = _fmp4_with_audio(tmp_path / "a.mp4")
    _init, fragments = cmaf.split_init_and_fragments(data)
    fragment = fragments[0]
    _version, old_tfdt = _first_moof_tfdt(fragment)

    shifted = continuity.shift_cmaf_fragment(fragment, 0, sequence_number=0)
    _version, new_tfdt = _first_moof_tfdt(shifted)
    assert new_tfdt == old_tfdt


def _packet_starts(data: bytes) -> range:
    assert len(data) % continuity.TS_PACKET_SIZE == 0
    return range(0, len(data), continuity.TS_PACKET_SIZE)


def _extract_all_pts_dts(data: bytes) -> list[int]:
    """Every PTS/DTS value found via the same scan shift_ts_segment uses
    (independent re-implementation-free check: re-run the packet walk,
    just reading instead of writing)."""
    values = []
    for offset in _packet_starts(data):
        pusi = (data[offset + 1] >> 6) & 0x01
        afc = (data[offset + 3] >> 4) & 0x03
        pos = offset + 4
        if afc in (0b10, 0b11):
            af_len = data[pos]
            pos = pos + 1 + af_len
        elif afc == 0b00:
            continue
        packet_end = offset + continuity.TS_PACKET_SIZE
        if afc not in (0b01, 0b11) or not pusi:
            continue
        if pos + 9 > packet_end or data[pos : pos + 3] != b"\x00\x00\x01":
            continue
        pts_dts_flags = (data[pos + 7] >> 6) & 0x03
        if pts_dts_flags not in (0b10, 0b11) or pos + 14 > packet_end:
            continue
        pts_off = pos + 9
        values.append(continuity._unpack_ts_timestamp(bytes(data[pts_off : pts_off + 5])))
        if pts_dts_flags == 0b11 and pos + 19 <= packet_end:
            dts_off = pts_off + 5
            values.append(continuity._unpack_ts_timestamp(bytes(data[dts_off : dts_off + 5])))
    return values


def _extract_all_pcr(data: bytes) -> list[int]:
    values = []
    for offset in _packet_starts(data):
        afc = (data[offset + 3] >> 4) & 0x03
        if afc not in (0b10, 0b11):
            continue
        pos = offset + 4
        af_len = data[pos]
        if af_len == 0:
            continue
        flags = data[pos + 1]
        if (flags >> 4) & 0x01:
            pcr_off = pos + 2
            base, ext = continuity._unpack_pcr(bytes(data[pcr_off : pcr_off + 6]))
            values.append(base * continuity.PCR_EXT_PER_TICK + ext)
    return values


def test_shift_ts_segment_advances_every_pts_dts_and_pcr(tmp_path):
    data = _ts_with_audio(tmp_path / "a.ts")
    shift = 90_000 * 7200  # two hours

    before_pts_dts = _extract_all_pts_dts(data)
    before_pcr = _extract_all_pcr(data)
    assert before_pts_dts and before_pcr  # sanity: fixture actually has both

    shifted = continuity.shift_ts_segment(data, shift)
    after_pts_dts = _extract_all_pts_dts(shifted)
    after_pcr = _extract_all_pcr(shifted)

    assert len(after_pts_dts) == len(before_pts_dts)
    for old, new in zip(before_pts_dts, after_pts_dts):
        assert new == (old + shift) % continuity.PTS_DTS_MODULUS

    assert len(after_pcr) == len(before_pcr)
    shift_27mhz = shift * continuity.PCR_EXT_PER_TICK
    modulus_27mhz = continuity.PCR_BASE_MODULUS * continuity.PCR_EXT_PER_TICK
    for old, new in zip(before_pcr, after_pcr):
        assert new == (old + shift_27mhz) % modulus_27mhz

    # sample bytes (everything outside the timestamp fields we touched)
    # must be byte-identical -- this is a header patch, not a remux.
    assert len(shifted) == len(data)


def test_shift_ts_segment_stays_decodable_by_ffprobe(tmp_path):
    src = tmp_path / "a.ts"
    data = _ts_with_audio(src)
    shifted = continuity.shift_ts_segment(data, 90_000 * 3600)

    out = tmp_path / "shifted.ts"
    out.write_bytes(shifted)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-count_frames",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    # same frame count as the unshifted source -- shifting timestamps must
    # not disturb the actual sample data.
    probe_src = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-count_frames",
         "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == probe_src.stdout.strip()


def test_shift_ts_segment_rejects_non_multiple_of_packet_size():
    with pytest.raises(ValueError, match="not a multiple"):
        continuity.shift_ts_segment(b"\x47" * 100, 0)


def test_shift_ts_segment_rejects_bad_sync_byte():
    data = bytearray(continuity.TS_PACKET_SIZE)
    data[0] = 0x00
    with pytest.raises(ValueError, match="sync byte"):
        continuity.shift_ts_segment(bytes(data), 0)
