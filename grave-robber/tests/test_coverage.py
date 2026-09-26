"""Tests for coverage.py (SCOPE.md §8): multi-variant coverage map,
full-coverage filtering, and reference pick -- against synthetic manifest
text (the trace-shrink fixtures are too small, ~4 entries, to exercise
multi-variant scenarios meaningfully, per this branch's own instructions)."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.coverage import (  # noqa: E402
    VariantCoverage,
    build_hls_variant_coverage,
    filter_variants_covering_range,
    pick_reference_variant,
)


def _playlist(pdt: str, durations: list[float]) -> str:
    lines = ["#EXTM3U", f"#EXT-X-PROGRAM-DATE-TIME:{pdt}"]
    for i, d in enumerate(durations):
        lines.append(f"#EXTINF:{d},")
        lines.append(f"seg{i}.ts")
    return "\n".join(lines) + "\n"


def _dt_(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── build_hls_variant_coverage ────────────────────────────────────────────


def test_coverage_from_single_snapshot():
    text = _playlist("2024-01-01T00:00:00.000Z", [6.0, 6.0])
    coverage = build_hls_variant_coverage("1080p", [(text, "https://cdn/1080p.m3u8")])

    assert coverage.name == "1080p"
    assert coverage.covered_ranges == [(_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:12Z"))]


def test_coverage_merges_overlapping_snapshots():
    snap1 = _playlist("2024-01-01T00:00:00.000Z", [6.0, 6.0])
    snap2 = _playlist("2024-01-01T00:00:06.000Z", [6.0, 6.0])  # overlaps snap1's second half

    coverage = build_hls_variant_coverage(
        "1080p", [(snap1, "https://cdn/1080p.m3u8"), (snap2, "https://cdn/1080p.m3u8")]
    )

    assert coverage.covered_ranges == [(_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:18Z"))]


def test_coverage_shows_a_hole_between_non_contiguous_snapshots():
    snap1 = _playlist("2024-01-01T00:00:00.000Z", [6.0])
    snap2 = _playlist("2024-01-01T00:01:00.000Z", [6.0])  # a real gap: this variant wasn't active

    coverage = build_hls_variant_coverage(
        "1080p", [(snap1, "https://cdn/1080p.m3u8"), (snap2, "https://cdn/1080p.m3u8")]
    )

    assert len(coverage.covered_ranges) == 2


# ── filter_variants_covering_range ────────────────────────────────────────


def test_filter_keeps_only_full_coverage_survivors():
    target = (_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:10Z"))
    full = VariantCoverage("1080p", [(_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:20Z"))])
    partial = VariantCoverage("720p", [(_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:05Z"))])

    survivors, dropped = filter_variants_covering_range([full, partial], target)

    assert [v.name for v in survivors] == ["1080p"]
    assert [v.name for v in dropped] == ["720p"]


def test_filter_excludes_a_variant_with_an_internal_gap_across_the_range():
    target = (_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:10Z"))
    gappy = VariantCoverage(
        "1080p",
        [
            (_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:04Z")),
            (_dt_("2024-01-01T00:00:06Z"), _dt_("2024-01-01T00:00:12Z")),
        ],
    )

    survivors, dropped = filter_variants_covering_range([gappy], target)

    assert survivors == []
    assert dropped == [gappy]


def test_filter_exact_boundary_match_is_a_survivor():
    target = (_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:10Z"))
    exact = VariantCoverage("1080p", [(_dt_("2024-01-01T00:00:00Z"), _dt_("2024-01-01T00:00:10Z"))])

    survivors, _ = filter_variants_covering_range([exact], target)

    assert survivors == [exact]


# ── pick_reference_variant ────────────────────────────────────────────────


def test_pick_reference_variant_returns_first():
    a = VariantCoverage("a", [])
    b = VariantCoverage("b", [])

    assert pick_reference_variant([a, b]) is a


def test_pick_reference_variant_raises_on_empty():
    import pytest

    with pytest.raises(ValueError):
        pick_reference_variant([])
