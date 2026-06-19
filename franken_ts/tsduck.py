from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree as ET

from .timeline import AdBoundary
from .utils import run_cmd

logger = logging.getLogger(__name__)


def inject_markers(
    intermediate_ts: Path,
    xml_path: Path,
    output_path: Path,
    dry_run: bool = False,
) -> None:
    """Inject SCTE-35 markers from xml_path into intermediate_ts → output_path."""
    xml_size = xml_path.stat().st_size if xml_path.exists() else 25000
    max_file_size = max(25000, xml_size + 4096)

    cmd = [
        "tsp",
        "--add-input-stuffing", "1/10",
        "--verbose",
        "-I", "file", str(intermediate_ts),
        "-P", "pmt",
            "--service", "1",
            "--add-programinfo-id", "0x43554549",
            "--add-pid", "600/0x86",
        "-P", "spliceinject",
            "--service", "1",
            "--inject-count", "1",
            "--files", str(xml_path),
            "--max-file-size", str(max_file_size),
            "--wait-first-batch",
        "-P", "filter",
            "--negate",
            "--pid", "0x1FFF",
        "-O", "file", str(output_path),
    ]

    logger.info("Injecting SCTE-35 markers → %s", output_path)
    run_cmd(cmd, dry_run=dry_run)


def verify_markers(
    output_path: Path,
    expected_boundaries: list[AdBoundary],
    temp_dir: Path,
    dry_run: bool = False,
) -> bool:
    """Extract SCTE-35 tables from the output TS and verify expected events are present.

    Returns True if all expected events are found.
    """
    txt_path = temp_dir / "splice-info-tables.txt"
    xml_path = temp_dir / "splice-info-tables.xml"

    cmd = [
        "tsp",
        "-I", "file", str(output_path),
        "-P", "tables",
            "--pid", "0x0258",
            "--text", str(txt_path),
            "--xml", str(xml_path),
        "-O", "drop",
    ]

    logger.info("Verifying SCTE-35 markers in %s", output_path)
    run_cmd(cmd, dry_run=dry_run)

    if dry_run:
        return True

    if not xml_path.exists():
        logger.error("Verification failed: %s was not produced", xml_path)
        return False

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        logger.error("Verification failed: cannot parse %s: %s", xml_path, exc)
        return False

    root = tree.getroot()
    found_ids: set[int] = set()

    for sit in root.iter("splice_information_table"):
        for si in sit.iter("splice_insert"):
            raw = si.get("splice_event_id", "")
            try:
                eid = int(raw, 16) if raw.startswith("0x") else int(raw)
                found_ids.add(eid)
            except (ValueError, TypeError):
                pass
        for ts in sit.iter("splice_segmentation_descriptor"):
            raw = ts.get("segmentation_event_id", "")
            try:
                eid = int(raw, 16) if raw.startswith("0x") else int(raw)
                found_ids.add(eid)
            except (ValueError, TypeError):
                pass

    expected_ids = {b.event_id for b in expected_boundaries}
    missing = expected_ids - found_ids
    extra = found_ids - expected_ids

    print(f"\nVerification results for {output_path.name}:")
    print(f"  Expected event IDs : {sorted(expected_ids)}")
    print(f"  Found event IDs    : {sorted(found_ids)}")

    if missing:
        logger.error("Missing event IDs in output: %s", sorted(missing))
    if extra:
        logger.warning("Unexpected event IDs in output: %s", sorted(extra))

    success = not missing
    if success:
        logger.info("Verification passed: all %d event(s) found", len(expected_ids))
    else:
        logger.error("Verification FAILED: %d event(s) missing", len(missing))

    return success
