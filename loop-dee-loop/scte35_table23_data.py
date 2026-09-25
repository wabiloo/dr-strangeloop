"""GENERATED FILE -- DO NOT EDIT BY HAND.
Source of truth: scte35-table23/scte35_table23/__init__.py
Regenerate with: uv run python scripts/generate_scte35_tables.py

SCTE-35 Table 23 segmentation_type_id reference data -- see
scte35-table23/scte35_table23/__init__.py for the full explanation of why
this is a generated copy rather than an import: loop-dee-loop is deployed
as flat files into a standalone Docker image (see loop-dee-loop/Dockerfile
and its-a-live/loop_stack.py), built from the `loop-dee-loop/` directory
alone, so it cannot depend on a sibling workspace package at runtime.
"""

from __future__ import annotations

SEGMENTATION_TYPE_NAME: dict[str, str] = {
    "0x00": "Not Indicated",
    "0x01": "Content Identification",
    "0x02": "Call Ad Server",
    "0x10": "Program",
    "0x12": "Program Early Termination",
    "0x13": "Program Breakaway",
    "0x14": "Program Resumption",
    "0x15": "Program Runover Planned",
    "0x16": "Program Runover Unplanned",
    "0x17": "Program Overlap Start",
    "0x18": "Program Blackout Override",
    "0x19": "Program Join",
    "0x1A": "Program Immediate Resumption",
    "0x20": "Chapter",
    "0x22": "Break",
    "0x24": "Opening Credit",
    "0x26": "Closing Credit",
    "0x30": "Provider Advertisement",
    "0x32": "Distributor Advertisement",
    "0x34": "Provider Placement Opportunity",
    "0x36": "Distributor Placement Opportunity",
    "0x38": "Provider Overlay Placement Opportunity",
    "0x3A": "Distributor Overlay Placement Opportunity",
    "0x3C": "Provider Promo",
    "0x3E": "Distributor Promo",
    "0x40": "Unscheduled Event",
    "0x42": "Alternate Content Opportunity",
    "0x44": "Provider Ad Block",
    "0x46": "Distributor Ad Block",
    "0x50": "Network",
}

SEGMENTATION_TYPE_CODE: dict[str, str] = {
    "0x00": "NIN",
    "0x01": "CID",
    "0x02": "CAS",
    "0x10": "PRG",
    "0x12": "PET",
    "0x13": "PBA",
    "0x14": "PRS",
    "0x15": "PRP",
    "0x16": "PRU",
    "0x17": "POS",
    "0x18": "PBO",
    "0x19": "PJO",
    "0x1A": "PIR",
    "0x20": "CHP",
    "0x22": "BRK",
    "0x24": "OPN",
    "0x26": "CLC",
    "0x30": "PAD",
    "0x32": "DAD",
    "0x34": "PPO",
    "0x36": "DPO",
    "0x38": "PVO",
    "0x3A": "DVO",
    "0x3C": "PPR",
    "0x3E": "DPR",
    "0x40": "USC",
    "0x42": "ACO",
    "0x44": "PAB",
    "0x46": "DAB",
    "0x50": "NET",
}

SEGMENTATION_END_TYPE_ID: dict[int, int] = {
    0x10: 0x11,
    0x13: 0x14,
    0x17: 0x11,
    0x19: 0x11,
    0x20: 0x21,
    0x22: 0x23,
    0x24: 0x25,
    0x26: 0x27,
    0x30: 0x31,
    0x32: 0x33,
    0x34: 0x35,
    0x36: 0x37,
    0x38: 0x39,
    0x3A: 0x3B,
    0x3C: 0x3D,
    0x3E: 0x3F,
    0x40: 0x41,
    0x42: 0x43,
    0x44: 0x45,
    0x46: 0x47,
    0x50: 0x51,
}

INSTANT_SEGMENTATION_TYPE_IDS: frozenset[str] = frozenset({
    "0x00",
    "0x01",
    "0x02",
    "0x12",
    "0x15",
    "0x16",
    "0x18",
    "0x1A",
})
