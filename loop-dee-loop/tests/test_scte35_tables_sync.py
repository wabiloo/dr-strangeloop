"""Guards against `scte35_table23_data.py` (this tool's generated,
checked-in copy of the shared SCTE-35 Table 23 data -- see that file's own
header) drifting from its canonical source, the `scte35_table23` workspace
package. loop-dee-loop can't import that package at runtime (see
scte35_table23_data.py's header for why), but this test suite runs in the
shared repo-root venv, which does have it installed -- so it's the one
place able to catch "changed scte35_table23 but forgot to regenerate"."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_generated_copy_matches_canonical_source():
    """Runs the generator in --check mode as a fresh subprocess (rather
    than importing it here) so its `scte35_table23` import always resolves
    to the real installed package, never to inspector-krogh's unrelated,
    same-name-adjacent `scte35_tables.py`."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate_scte35_tables.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "loop-dee-loop/scte35_table23_data.py (and/or igor's generated "
        "TS copy) is out of date with scte35_table23 -- run "
        "`uv run python scripts/generate_scte35_tables.py` and commit the "
        f"result.\n{result.stderr}"
    )


def test_derived_lookups_match_generated_copy():
    """The int-keyed views scte35_signaling.py derives from
    scte35_table23_data.py should stay consistent with it (catches a
    hand-edit to either side going out of sync, independent of whether the
    generated copy itself is stale -- that's the test above)."""
    sys.path.insert(0, str(REPO_ROOT / "loop-dee-loop"))
    import scte35_signaling as sig
    import scte35_table23_data as data

    assert sig.SEGMENTATION_TYPE_NAMES == {int(k, 16): v for k, v in data.SEGMENTATION_TYPE_NAME.items()}
    assert sig.SEGMENTATION_TYPE_CODES == {int(k, 16): v for k, v in data.SEGMENTATION_TYPE_CODE.items()}
    assert sig.SEGMENTATION_START_TYPE_IDS == frozenset(data.SEGMENTATION_END_TYPE_ID)
    assert sig.SEGMENTATION_END_TYPE_IDS == frozenset(data.SEGMENTATION_END_TYPE_ID.values())
