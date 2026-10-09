# player-lab

Plays a channel's HLS and DASH in several real players (hls.js, dash.js, Shaka,
Video.js) in a headless Chrome and reports startup time, stalls, errors and the
Period/discontinuity transitions each player saw, compared with what the
channel's own `/timeline.json` says it crossed. Design and phasing:
[`DESIGN.md`](./DESIGN.md). Manual tool; not part of any test suite or CI.

## Use

```bash
uv sync --all-packages
uv run player-lab setup          # npm install of the SDKs into ~/.dr-strangeloop/player-lab (or $PLAYER_LAB_HOME)

uv run player-lab run --channel my-channel                    # data/channels/my-channel.toml, URLs via its-a-live status
uv run player-lab run --hls http://host/index.m3u8 --dash http://host/stream.mpd
uv run player-lab run ... --players dashjs,hlsjs --format dash --boundaries 3
uv run player-lab run ... --no-timeline --duration 120        # no /timeline.json (e.g. aws-media): generic checks only
uv run player-lab run ... --profile profile.toml --ffmpeg 60 --trace
```

Needs the installed Google Chrome (H.264) and Node/npm for `setup`. Exit code 0
= all cases pass; 1 = a case failed; 2 = usage/setup error. Reports go to
`<outputs>/player-lab/<timestamp>/report.json` (+ a screenshot per failed case).

## How it works

- `harness/` is a static page (`?player=&url=&format=`). `adapters/<player>.js`
  load one SDK and emit normalised events; `observer.js` measures on the
  `<video>` element itself (stall = playhead frozen >= 1 s while started and not
  paused), so all players share one definition. Vendor libs are served from
  the npm cache under `/vendor/`.
- `runner.py` opens one page per player x format in one Chrome, polls
  `/timeline.json` every ~5 s into `boundaries.BoundaryTracker` (baseline taken
  when all players started; later-appearing Period ids / discontinuity segments
  = boundaries to cross), waits until the players had time to reach them
  (`--settle`, max wait after the timeline shows them: players trail the live
  edge by about the window), then judges with `profile.judge`.
- Period/discontinuity transitions come from each player's own mechanisms, never from the clock
  or the timeline: hls.js (`FRAG_CHANGED` continuity counter), dash.js (`PERIOD_SWITCH_COMPLETED`),
  Video.js/VHS (the playlist controller's `timelineChangeController_` `timelinechange` event) and
  Shaka on HLS (no event: a jump of `start - mediaTimestamp` in `segmentappended`, i.e. the
  timestamp offset it applies at a discontinuity). Shaka on DASH cannot tell (no media timestamp
  for fMP4, Periods are flattened), so its boundary count is not compared (stalls/errors still are).
- `--ffmpeg N` also demuxes each manifest with ffmpeg: informational only.
  ffmpeg's HLS demuxer is known to choke on discontinuities with separate
  audio playlists, so a problem there is not by itself a channel defect.
- Profile TOML: top-level thresholds (`max_startup_s`, `max_stall_count`,
  `max_stall_seconds`, `max_errors`, `max_dropped_frame_ratio`,
  `boundary_tolerance`), per-player overrides in `[player.<id>]` or
  `[player."<id>:<fmt>"]`.

## Adding a player

Add `harness/adapters/<id>.js` (copy the closest one; set `formats` and
`reports`), register the id in `PLAYERS` in `runner.py`, add its npm package to
`vendor/package.json`. Keys for commercial players (phase 3) come from env vars
or `~/.dr-strangeloop/player-lab.toml`, never from the repo.

## Tests

`uv run --project player-lab pytest player-lab/tests -q` (pure logic; the
browser run needs a live channel, e.g. a local `loop-dee-loop/serve.py`).

## Igor

Igor's channel page has a **Playback test** panel that runs `player-lab run --channel` as a background job and renders `report.json` (code: `igor/src/igor/integrations/player_lab.py`, `igor/src/igor/app/routes/playback.py`, `igor/frontend/src/components/PlaybackTestPanel.vue`). Runs are stored under `outputs/player-lab/channels/<name>/<run_id>/`. It needs Chrome on the Igor host.

The panel has a second mode, **In this browser**: Igor serves the harness and the vendor SDKs same-origin (`GET /api/v1/playback-test/harness/*`), the panel opens `harness/browser.html` in a new tab, which plays each case in an iframe (`index.html?embed=1`), tracks boundaries from `/timeline.json`, and POSTs the raw snapshots to `.../channels/{name}/browser-results`. Igor judges them with `player-lab judge` (stdin JSON, same `profile.judge` as the headless runner; `judge_browser.py`) and stores the same `report.json` (with `mode: "browser"` and the `userAgent`). The browser only measures; the tab must stay in the foreground (background tabs load no media), must be able to reach the channel URLs (mind HTTPS-vs-HTTP mixed content), and its codecs decide what plays.
