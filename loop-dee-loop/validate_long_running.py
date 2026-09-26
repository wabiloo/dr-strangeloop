"""Long-running soak test: validate that HLS DATERANGE and DASH EventStream
markers appear at the correct absolute time, sampled repeatedly over real
wall-clock time spanning multiple loop iterations (>= 5 full loops of the
baked package).

This is intentionally independent of serve.py's own arithmetic: for each
poll, it derives its own ground-truth expectation using loop_math
(the same integer primitives, but invoked fresh here) and cross-checks
against what the running server actually returned, over HTTP, for both
manifests.

Usage:
    python3 validate_long_running.py --base-url http://127.0.0.1:8099 \
        --epoch-utc 2026-01-01T00:00:00Z --package /tmp/loop-package-e2e \
        --min-loops 5 --poll-interval 15
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_math import compute_loop_position  # noqa: E402


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode("utf-8")


def parse_hls_daterange_ids(body: str) -> list[int]:
    """Extract each DATERANGE tag's event_id as a plain decimal int, from
    its `ID="<segmentation_type_id>-<event_id>-<loop_number>"` attribute
    (see loop-dee-loop/scte35_signaling.py's build_daterange_tags) -- the
    middle field, not the whole ID string."""
    raw_ids = re.findall(r'#EXT-X-DATERANGE:ID="([^"]+)"', body)
    return [int(raw_id.split("-")[1]) for raw_id in raw_ids]


def parse_hls_daterange_and_next_pdt(body: str) -> list[tuple[str, str, str | None]]:
    """Return (event_id, START-DATE, next PROGRAM-DATE-TIME) for every
    DATERANGE tag in manifest order. The third element is the
    EXT-X-PROGRAM-DATE-TIME of the segment line immediately following the
    tag (None if the manifest ends before one is found) -- this is what
    lets us check the "START-DATE must equal the PDT of the segment it
    precedes" invariant directly against the real serialized manifest, not
    just against our own recomputation.
    """
    lines = body.splitlines()
    results: list[tuple[str, str, str | None]] = []
    for i, line in enumerate(lines):
        m = re.match(r'#EXT-X-DATERANGE:ID="([^"]+)",START-DATE="([^"]+)"', line)
        if not m:
            continue
        event_id, start_date = m.group(1), m.group(2)
        next_pdt = None
        for later_line in lines[i + 1:]:
            pdt_m = re.match(r"#EXT-X-PROGRAM-DATE-TIME:(.+)", later_line)
            if pdt_m:
                next_pdt = pdt_m.group(1)
                break
            if later_line.startswith("#EXT-X-DATERANGE"):
                # another marker before the next segment -- keep scanning,
                # the PDT still belongs to the upcoming segment line.
                continue
        results.append((event_id, start_date, next_pdt))
    return results


def parse_hls_media_sequence(body: str) -> int:
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body)
    assert m, "no EXT-X-MEDIA-SEQUENCE in HLS manifest"
    return int(m.group(1))


def parse_dash_event_ids_and_times(body: str) -> list[tuple[int, int]]:
    """`id` is a synthetic per-direction value, `event_id * 4 +
    direction_code` (direction_code: out=0, in=1, instant=2 -- see
    serve.py's DASH <Event> authoring), not the real event_id directly --
    Start/End sharing one event_id would otherwise collide in the single
    <EventStream> every marker now lands in. Undo the encoding here so
    this function returns the same real event_ids as HLS's DATERANGE
    parsing, for an apples-to-apples multiset comparison."""
    return [
        (int(synthetic_id) // 4, int(pts))
        for pts, synthetic_id in re.findall(
            r'<Event presentationTime="(\d+)"[^>]*id="([^"]+)"', body
        )
    ]


def parse_dash_availability_start(body: str) -> str:
    m = re.search(r'availabilityStartTime="([^"]+)"', body)
    assert m, "no availabilityStartTime in DASH manifest"
    return m.group(1)


def iso_to_ticks(iso_str: str, timescale: int) -> int:
    dt = _dt.datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=_dt.timezone.utc
    )
    return round(dt.timestamp() * timescale)


def ticks_to_iso(ticks: int, timescale: int) -> str:
    """One-shot, final conversion for display/comparison only -- mirrors
    loop_math.ticks_to_wall_clock_seconds's contract (never fed back into
    further arithmetic)."""
    seconds = ticks / timescale
    return _dt.datetime.utcfromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--epoch-utc", required=True)
    parser.add_argument("--min-loops", type=int, default=5)
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--max-polls", type=int, default=200)
    args = parser.parse_args()

    descriptor = json.loads((args.package / "loop_descriptor.json").read_text())
    timescale = int(descriptor["timescale"])
    total_loop_duration_ticks = int(descriptor["total_loop_duration_ticks"])
    markers = descriptor["markers"]
    segment_boundary_ticks = [int(t) for t in descriptor["segment_boundary_ticks"]]
    segments_per_loop = len(segment_boundary_ticks)

    epoch_dt = _dt.datetime.strptime(args.epoch_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )
    epoch_ticks = round(epoch_dt.timestamp() * timescale)

    print(
        f"Soak test starting. total_loop_duration_ticks={total_loop_duration_ticks} "
        f"({total_loop_duration_ticks/timescale:.3f}s/loop), "
        f"target: >= {args.min_loops} loops, epoch_ticks={epoch_ticks}"
    )

    def expected_window_events(loop_number: int, seg_index: int, window_segments: int = 6):
        """Independently recompute (mirroring serve.py's own windowing logic,
        re-implemented here from scratch rather than imported, so this is a
        genuine independent cross-check) the exact multiset of
        (event_id, period_relative_presentation_time) pairs that SHOULD
        appear in a manifest whose sliding window starts at the given
        (loop_number, seg_index)."""
        media_sequence = loop_number * segments_per_loop + seg_index
        expected: list[tuple[int, int]] = []
        for i in range(window_segments):
            global_index = media_sequence + i
            local_index = global_index % segments_per_loop
            local_loop_number = global_index // segments_per_loop
            seg_start_local = segment_boundary_ticks[local_index]
            seg_end_local = (
                segment_boundary_ticks[local_index + 1]
                if local_index + 1 < segments_per_loop
                else total_loop_duration_ticks
            )
            for marker in markers:
                if seg_start_local <= marker["pts_time_ticks"] < seg_end_local:
                    expected.append(
                        (
                            int(marker["event_id"], 16),
                            local_loop_number * total_loop_duration_ticks
                            + marker["pts_time_ticks"],
                        )
                    )
        return expected

    loops_observed: set[int] = set()
    poll_count = 0
    failures: list[str] = []

    while len(loops_observed) < args.min_loops + 1 and poll_count < args.max_polls:
        poll_count += 1
        now_ticks = round(time.time() * timescale)
        expected_pos = compute_loop_position(now_ticks, epoch_ticks, total_loop_duration_ticks)
        loops_observed.add(expected_pos.loop_number)

        hls_body = fetch(f"{args.base_url}/video.m3u8")
        dash_body = fetch(f"{args.base_url}/stream.mpd")

        hls_ids = parse_hls_daterange_ids(hls_body)
        hls_daterange_triples = parse_hls_daterange_and_next_pdt(hls_body)
        dash_events = parse_dash_event_ids_and_times(dash_body)
        dash_avail_start_ticks = iso_to_ticks(
            parse_dash_availability_start(dash_body), timescale
        )

        media_sequence = parse_hls_media_sequence(hls_body)
        seg_index = media_sequence % segments_per_loop
        loop_number = media_sequence // segments_per_loop

        ts_label = _dt.datetime.utcfromtimestamp(now_ticks / timescale).strftime(
            "%H:%M:%S"
        )
        print(
            f"[poll {poll_count:3d}] now={ts_label} loop={expected_pos.loop_number} "
            f"media_seq={media_sequence} hls_markers={len(hls_ids)} "
            f"dash_markers={len(dash_events)}"
        )

        # DASH availabilityStartTime must exactly equal our epoch, every poll.
        if dash_avail_start_ticks != epoch_ticks:
            failures.append(
                f"poll {poll_count}: DASH availabilityStartTime tick "
                f"{dash_avail_start_ticks} != epoch_ticks {epoch_ticks}"
            )

        expected = expected_window_events(loop_number, seg_index)
        expected_event_ids_sorted = sorted(eid for eid, _ in expected)
        expected_pts_multiset = sorted(pts for _, pts in expected)

        actual_hls_ids_sorted = sorted(hls_ids)
        actual_dash_ids_sorted = sorted(eid for eid, _ in dash_events)
        actual_dash_pts_multiset = sorted(pts for _, pts in dash_events)

        if actual_hls_ids_sorted != expected_event_ids_sorted:
            failures.append(
                f"poll {poll_count}: HLS DATERANGE event_id multiset "
                f"{actual_hls_ids_sorted} != expected {expected_event_ids_sorted}"
            )
        if actual_dash_ids_sorted != expected_event_ids_sorted:
            failures.append(
                f"poll {poll_count}: DASH Event event_id multiset "
                f"{actual_dash_ids_sorted} != expected {expected_event_ids_sorted}"
            )
        if actual_dash_pts_multiset != expected_pts_multiset:
            failures.append(
                f"poll {poll_count}: DASH Event presentationTime multiset "
                f"{actual_dash_pts_multiset} != expected {expected_pts_multiset}"
            )

        # HLS START-DATE checks (the fix requested: START-DATE must be the
        # real absolute wall-clock time of the marker, and -- since markers
        # sit exactly on segment boundaries by construction -- must equal
        # the PROGRAM-DATE-TIME of the segment line it immediately precedes).
        expected_iso_by_pts = {
            pts: ticks_to_iso(pts + epoch_ticks, timescale) for _, pts in expected
        }
        expected_start_dates_multiset = sorted(expected_iso_by_pts.values())
        actual_start_dates_multiset = sorted(sd for _, sd, _ in hls_daterange_triples)

        if actual_start_dates_multiset != expected_start_dates_multiset:
            failures.append(
                f"poll {poll_count}: HLS START-DATE multiset "
                f"{actual_start_dates_multiset} != expected "
                f"{expected_start_dates_multiset}"
            )

        for event_id, start_date, next_pdt in hls_daterange_triples:
            if next_pdt is None:
                failures.append(
                    f"poll {poll_count}: HLS DATERANGE {event_id} "
                    f"(START-DATE={start_date}) has no following "
                    f"EXT-X-PROGRAM-DATE-TIME in the manifest"
                )
            elif start_date != next_pdt:
                failures.append(
                    f"poll {poll_count}: HLS DATERANGE {event_id} "
                    f"START-DATE={start_date} != following segment's "
                    f"PROGRAM-DATE-TIME={next_pdt} "
                    f"(marker is not exactly on this segment's boundary)"
                )

        time.sleep(args.poll_interval)

    print()
    print(f"Loops observed: {sorted(loops_observed)} ({len(loops_observed)} distinct)")
    print(f"Total polls: {poll_count}")

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1

    if len(loops_observed) < args.min_loops + 1:
        print(
            f"\nFAILED: only observed {len(loops_observed)} distinct loop "
            f"number(s), needed >= {args.min_loops + 1} (loops 0..{args.min_loops})"
        )
        return 1

    print("\nAll checks passed across every observed loop iteration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
