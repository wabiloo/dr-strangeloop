Small real HAR/Proxyman captures, copied from `wabiloo/trace-shrink`'s own
`tests/archives/` test fixtures (per this branch's implementation
instructions — real HAR captures aren't otherwise available in this
environment). Used by `test_fixtures_smoke.py`/`test_cli.py` for
pipeline-shape smoke tests against genuine archive formats.

These are small (2-4 entries each) and manifest-only (no segment bodies,
no SCTE-35 markers) — they exercise real HAR/Proxyman parsing and
multi-snapshot merging, not multi-variant coverage or marker decoding
(covered instead by targeted unit tests against synthetic manifest text
elsewhere in this test suite). Real end-to-end validation against a
richer capture (real markers, multiple variants, real playable media)
happens separately, per this branch's own instructions.

Files:
- `hls1-chrome.har` / `hls1-proxyman.har` / `hls1-proxyman.proxymanlogv2`:
  a live HLS session (broadpeak.io), captured via Chrome DevTools and via
  Proxyman respectively.
- `export-proxyman.har` / `export-proxyman.proxymanlogv2`: a larger
  Proxyman export, not currently used by any test here but kept alongside
  its siblings for future use.
