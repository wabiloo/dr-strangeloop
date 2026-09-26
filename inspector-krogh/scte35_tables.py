"""SCTE-35 Table 22/23 reference data (segmentation_type_id names, codes,
and start/end pairing).

This intentionally duplicates the equivalent tables in
``franken_ts/config.py`` rather than importing them. The whole point of
``krogh.py`` is to verify a built ``.ts`` file completely
independently of franken-ts's own bookkeeping (no playlist, no
``markers.json``, no shared build state) -- these values come straight from
the ANSI/SCTE 35 spec, not from franken-ts, so keeping a second, standalone
copy here is what makes that independence real rather than nominal.
"""

from __future__ import annotations

# segmentation_type_id -> human-readable name (Table 22).
SEGMENTATION_TYPE_NAME: dict[str, str] = {
    "0x00": "Not Indicated",
    "0x01": "Content Identification",
    "0x02": "Call Ad Server",
    "0x10": "Program Start",
    "0x11": "Program End",
    "0x12": "Program Early Termination",
    "0x13": "Program Breakaway",
    "0x14": "Program Resumption",
    "0x15": "Program Runover Planned",
    "0x16": "Program Runover Unplanned",
    "0x17": "Program Overlap Start",
    "0x18": "Program Blackout Override",
    "0x19": "Program Join",
    "0x1A": "Program Immediate Resumption",
    "0x20": "Chapter Start",
    "0x21": "Chapter End",
    "0x22": "Break Start",
    "0x23": "Break End",
    "0x24": "Opening Credit Start",
    "0x25": "Opening Credit End",
    "0x26": "Closing Credit Start",
    "0x27": "Closing Credit End",
    "0x30": "Provider Advertisement Start",
    "0x31": "Provider Advertisement End",
    "0x32": "Distributor Advertisement Start",
    "0x33": "Distributor Advertisement End",
    "0x34": "Provider Placement Opportunity Start",
    "0x35": "Provider Placement Opportunity End",
    "0x36": "Distributor Placement Opportunity Start",
    "0x37": "Distributor Placement Opportunity End",
    "0x38": "Provider Overlay Placement Opportunity Start",
    "0x39": "Provider Overlay Placement Opportunity End",
    "0x3A": "Distributor Overlay Placement Opportunity Start",
    "0x3B": "Distributor Overlay Placement Opportunity End",
    "0x3C": "Provider Promo Start",
    "0x3D": "Provider Promo End",
    "0x3E": "Distributor Promo Start",
    "0x3F": "Distributor Promo End",
    "0x40": "Unscheduled Event Start",
    "0x41": "Unscheduled Event End",
    "0x42": "Alternate Content Opportunity Start",
    "0x43": "Alternate Content Opportunity End",
    "0x44": "Provider Ad Block Start",
    "0x45": "Provider Ad Block End",
    "0x46": "Distributor Ad Block Start",
    "0x47": "Distributor Ad Block End",
    "0x50": "Network Start",
    "0x51": "Network End",
}

# Stable three-letter codes, purely for compact timeline-lane labels.
SEGMENTATION_TYPE_CODE: dict[str, str] = {
    "0x00": "NIN", "0x01": "CID", "0x02": "CAS",
    "0x10": "PRG", "0x11": "PRG", "0x12": "PET", "0x13": "PBA", "0x14": "PRS",
    "0x15": "PRP", "0x16": "PRU", "0x17": "POS", "0x18": "PBO", "0x19": "PJO",
    "0x1A": "PIR",
    "0x20": "CHP", "0x21": "CHP",
    "0x22": "BRK", "0x23": "BRK",
    "0x24": "OPN", "0x25": "OPN", "0x26": "CLC", "0x27": "CLC",
    "0x30": "PAD", "0x31": "PAD", "0x32": "DAD", "0x33": "DAD",
    "0x34": "PPO", "0x35": "PPO", "0x36": "DPO", "0x37": "DPO",
    "0x38": "PVO", "0x39": "PVO", "0x3A": "DVO", "0x3B": "DVO",
    "0x3C": "PPR", "0x3D": "PPR", "0x3E": "DPR", "0x3F": "DPR",
    "0x40": "USC", "0x41": "USC", "0x42": "ACO", "0x43": "ACO",
    "0x44": "PAB", "0x45": "PAB", "0x46": "DAB", "0x47": "DAB",
    "0x50": "NET", "0x51": "NET",
}

# start type_id -> end type_id (Table 23). Most pairs are consecutive, but
# Program segmentation has non-consecutive pairs and several start types
# that all close on the same Program End.
SEGMENTATION_END_TYPE_ID: dict[int, int] = {
    0x10: 0x11, 0x13: 0x14, 0x17: 0x11, 0x19: 0x11,
    0x20: 0x21, 0x22: 0x23, 0x24: 0x25, 0x26: 0x27,
    0x30: 0x31, 0x32: 0x33, 0x34: 0x35, 0x36: 0x37,
    0x38: 0x39, 0x3A: 0x3B, 0x3C: 0x3D, 0x3E: 0x3F,
    0x40: 0x41, 0x42: 0x43, 0x44: 0x45, 0x46: 0x47,
    0x50: 0x51,
}
SEGMENTATION_START_TYPE_IDS = frozenset(SEGMENTATION_END_TYPE_ID)
SEGMENTATION_END_TYPE_IDS = frozenset(SEGMENTATION_END_TYPE_ID.values())

# Signals that stand alone (never paired as a start/end span).
INSTANT_SEGMENTATION_TYPE_IDS: frozenset[str] = frozenset({
    "0x00", "0x01", "0x02", "0x12", "0x15", "0x16", "0x18", "0x1A",
})


def type_name(type_id: int) -> str:
    return SEGMENTATION_TYPE_NAME.get(f"0x{type_id:02X}", f"Unknown (0x{type_id:02X})")


def type_code(type_id: int) -> str:
    return SEGMENTATION_TYPE_CODE.get(f"0x{type_id:02X}", "UNK")


def is_instant_type_id(type_id: int) -> bool:
    return f"0x{type_id:02X}" in INSTANT_SEGMENTATION_TYPE_IDS


def is_start_type_id(type_id: int) -> bool:
    """Whether a raw Table 23 segmentation_type_id represents a start (or
    standalone/instant) signal, as opposed to an end signal."""
    if is_instant_type_id(type_id):
        return True
    if type_id in SEGMENTATION_START_TYPE_IDS:
        return True
    if type_id in SEGMENTATION_END_TYPE_IDS:
        return False
    # Unknown pairing: fall back to the spec's usual odd/even convention
    # (start IDs are even, end IDs are the next odd number).
    return type_id % 2 == 0
