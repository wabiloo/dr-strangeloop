Fixtures for loop-packager tests.

For unit-level tests (test_loop_math.py, test_bake_validation.py) no real
media files are needed -- they exercise pure-Python arithmetic and
validation logic with fabricated data.

For a full end-to-end test against real franken-ts output (SCOPE.md §10
acceptance checklist item: "checked against real franken-ts output, not
just synthetic test clips"), place here:

  - a small real franken-ts `.ts` output (ideally multi-marker, since
    GPAC's own aggregation was confirmed to drop markers under exactly
    that condition -- SCOPE.md §6)
  - its corresponding `.markers.json` sidecar

and add a fixture-driven integration test that runs bake.py against them
(requires a real `gpac`/`MP4Box` build on PATH, and `ffprobe`/`threefive`
installed). These binary fixtures are intentionally not checked in by this
change -- add them alongside a real franken-ts run when available.
