"""cmaf.py against real ffmpeg output: shared init + rebased bare fragments."""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cmaf  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _fmp4(path: Path) -> bytes:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=2",
            "-c:v", "libx264", "-g", "12", "-video_track_timescale", "90000",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def test_split_and_rebase(tmp_path):
    data = _fmp4(tmp_path / "a.mp4")
    init, fragments = cmaf.split_init_and_fragments(data)

    assert init[4:8] == b"ftyp" and b"moov" in init and cmaf.avcc_config(init)
    assert cmaf.video_track_timescale(init) == 90_000
    assert len(fragments) >= 2

    body, next_seq = cmaf.rebase_fragments(fragments, target_start=540_000, first_sequence_number=5)
    assert next_seq == 5 + len(fragments)
    assert body[4:8] == b"moof" and b"ftyp" not in body and b"moov" not in body

    # first tfdt == target; later fragments keep their original spacing
    tfdts = []
    for box_type, start, end in cmaf.iter_boxes(body):
        if box_type == "moof":
            tfdt = cmaf._find(body, ("traf", "tfdt"), start + 8, end)
            version = body[tfdt[0] + 8]
            fmt = ">Q" if version == 1 else ">I"
            tfdts.append(struct.unpack(fmt, body[tfdt[0] + 12 : tfdt[0] + 12 + struct.calcsize(fmt)])[0])
    assert tfdts[0] == 540_000
    assert tfdts[1] - tfdts[0] == 12 * 3750  # 12 frames at 24 fps in 90 kHz ticks

    # the rebased segments still decode when re-attached to the shared init
    (tmp_path / "joined.mp4").write_bytes(init + body)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(tmp_path / "joined.mp4")],
        capture_output=True, text=True, check=True,
    )
    assert int(probe.stdout.strip().rstrip(",")) == 48
