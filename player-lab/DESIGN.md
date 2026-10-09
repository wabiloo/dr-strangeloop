# player-lab — design

Status: proposal, not built. Reviewed in conversation 2026-10-09.

## Purpose

Validate that a channel plays correctly in the mainstream players, for HLS and
DASH (periodic and continuous), live and startover/catchup. Detect stalls and
rebuffering, count Period/discontinuity transitions and compare them with what
the channel says it is doing. Manual use (scheduling later), plus a "Playback test"
panel in Igor. Not part of the unit-test suite or merge-request CI.

Works on any channel with a playable URL. Expectation checks (below) need
`/timeline.json`, so they apply to `ecs-express` and `local-docker`; for
`aws-media` only the generic stall/error checks apply.

## Architecture

```
            ┌────────────── harness page (HTML + JS) ──────────────┐
 channel →  │ adapters/<player>.js   normalised events             │ → results JSON
 manifest   │ observer.js            <video>-level ground truth    │
            └──────────────────────────────────────────────────────┘
                 ▲ driven by                          ▲ driven by
        Playwright + installed Chrome          the user's own browser
        (CLI)                                  (Igor panel)
```

One harness page, two drivers. Both produce the same result JSON, so an Igor
run and a CLI run are comparable.

### Components

- **Runner** (Python, Playwright for Python): resolves the channel's manifest
  and `/timeline.json` URLs (channel TOML, `get_paths()`), starts Chrome, runs
  scenarios, applies the profile, writes the report, sets the exit code.
- **Harness page**: one `<video>` + one player per case. Served by the runner
  (headless) or by Igor (in-browser mode).
- **Adapters**: one file per player implementing `load(url)`, `destroy()` and
  emitting normalised events: `started`, `error`, `periodChange` /
  `discontinuity`, `qualityChange`, `marker`. Adding a player = adding a file.
- **Observer** (player-independent, on the `<video>` element): `waiting`,
  `stalled`, `seeking`; playhead progress sampled every second (frozen
  seconds, longest stall); buffer ahead; dropped frames
  (`getVideoPlaybackQuality()`); startup time. Stalls are defined here, not by
  the player, so players are compared on the same definition.
- **Network observer** (Playwright request log, headless mode only): manifest
  refresh cadence, segment 404/5xx, fetch latency.
- **Expectations**: before and during the run, read `/timeline.json` and derive
  the discontinuities, Period boundaries and markers expected in the run
  window. Players' counts are compared with it (tolerance in the profile).
- **Profile** (TOML): thresholds (startup, total stall seconds, stall count,
  errors, boundary-count tolerance) with per-player overrides for known quirks.
  The report always shows the raw numbers; the profile only turns them into
  pass/fail.

### Scenarios

- Phase 1: `live-boundaries` — play live until N boundaries (loop wrap, asset
  boundary, signal break) have passed, found from `/timeline.json`, with a
  timeout. No `--epoch-utc` fiddling.
- Later: startover/catchup (`?start=&end=`), pause/resume, seek in window,
  long soak.

### Output

JSON report, terminal table, non-zero exit on failure, optional HTML report
with playhead/buffer traces, under `outputs/` (via `get_paths()`). For failed
runs only: Playwright trace and video (optional artifacts).

## Players

| Phase | Players |
|---|---|
| 1 (free) | dash.js, Shaka, hls.js, Video.js (VHS) |
| 3 (commercial) | Bitmovin, THEOplayer (Dolby OptiView); then JW Player; castLabs/Radiant/Flowplayer only on demand |
| Skip | Mux Player, Clappr, Plyr and similar (hls.js wrappers) |
| Reference | ffmpeg/ffprobe reading the manifest across a boundary (no browser); optional Apple `mediastreamvalidator` and DASH-IF validator |
| Out of scope | ExoPlayer/Media3, AVPlayer, TV platforms (manual, or later with an emulator and a small test app) |

Browsers: installed Chrome first (H.264). Firefox via Playwright later. Safari
native HLS: the in-browser Igor mode covers it; Playwright WebKit may not
behave like real Safari (to verify).

## Commercial players and keys

- Key sources, in order: environment variables (`BITMOVIN_LICENSE_KEY`,
  `THEOPLAYER_LICENSE`, ...), then `~/.dr-strangeloop/player-lab.toml`
  (outside the repo; the runner refuses it if group/world-readable).
- Headless: keys reach the page through a Playwright init script, never in
  URLs. Reports and logs redact them. Igor mode: the server hands them to the
  user's browser (acceptable for an internal tool; state it in the docs).
- SDKs are optional, installed from npm into a cache directory outside the
  repo. An adapter without its key or package is skipped with a clear message.
- Available keys: Bitmovin (yes), THEOplayer (no). Phase 3 is therefore
  Bitmovin only; the THEO adapter waits for a key.
- To verify before building phase 3: licences are domain-bound (the test host
  must be allowlisted in each vendor's dashboard); vendor terms for automated
  and headless use; whether the SDKs need outbound network for licence checks
  or analytics.

## Igor integration

Channel detail page, new **Playback test** panel:

- Player checklist, run button, results table (one row per player: startup,
  stalls, errors, Period/discontinuity count vs the timeline expectation,
  playhead/buffer traces), run history.
- Two modes: *in my browser* (Igor serves the harness page; results are posted
  back to an Igor endpoint) and *headless* (Igor starts the runner through its
  existing background job runner, with pollable status and log). Headless
  requires Chrome on the Igor host, which a container deployment may not have.
- In-browser caveats: background-tab throttling (keep the tab in the
  foreground), one browser at a time, not schedulable.
- Cross-origin: checked against `its-a-live/loop_stack.py`. `serve.py` adds
  `Access-Control-Allow-Origin: *` to every response (`_add_cors`, including
  `/timeline.json`). The CloudFront distribution has no response-headers or
  origin-request policy and does not forward `Origin`, so the origin's `*`
  passes through and is cached with the object: simple GETs work. CloudFront
  allows only GET/HEAD, so there is no OPTIONS: a player that sends a
  non-safelisted request header (custom headers, credentials) would fail its
  CORS preflight against a CDN channel. Watch for this in the SDKs (Bitmovin
  in particular, if configured with custom headers).

## Phasing

1. Core: harness, observers, adapters for dash.js/Shaka/hls.js/Video.js,
   `/timeline.json` expectations, Python runner, CLI, profile, ffmpeg tier.
2. Igor panel (both modes) and results UI.
3. Commercial adapters (Bitmovin, then THEOplayer) and key handling.
4. More scenarios, Firefox/Safari matrix, validators.

## Decisions taken

- Python runner (fits the uv workspace and Igor's backend; Playwright's
  tracing and trace viewer exist in the Python binding too).
- Manual tool for now (nightly scheduling deferred); no merge-request CI gate.
- The throwaway prototype in `loop-dee-loop/player-lab/` was deleted; its
  findings are in `loop-dee-loop/DASH_CONFORMANCE.md` item 4.
- Stalls measured on the `<video>` element, not from player events.

## Open questions

- Which hostnames are allowlisted on the Bitmovin licence (localhost, Igor's
  host)? Does the SDK need outbound network for the licence check?
- Nightly runs are deferred: nothing is designed for scheduling yet (the CLI
  exit code is enough to schedule it later).
