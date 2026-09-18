# Performance & sizing notes

Empirical resource-usage measurements for `loop-packager`, gathered by
actually running `bake.py` + `serve.py` against a real `franken-ts` output
and generating realistic concurrent viewer load. See `SCOPE.md` §8 for the
original (informational, not measured) sizing expectations this confirms.

## Methodology

- **Source asset**: `outputs/break-and-popos.ts` — 204s loop, 1920x1080
  H.264 @ ~9.3 Mbps + AAC audio, 4 real SCTE-35 markers (2 events).
- **Bake**: `bake.py ... --segment-duration 4` (real ABR-sized 4s segments,
  see `README.md`'s segmentation section).
- **Load**: `load_test.py`, a small concurrent-client script that spins up
  N threads, each continuously polling `/live.m3u8` every 2s and
  downloading every newly-advertised segment (i.e. a realistic HLS
  client access pattern: repeated manifest polls + segment fetches, no
  manifest/segment caching assumed on the client side).
- **Sampling**: server process RSS/CPU sampled via `ps` every 5s for the
  duration of the run; average CPU also cross-checked via cumulative
  `ps -o time` (CPU-seconds consumed) divided by wall-clock time, to avoid
  missing short bursts that point-in-time `%CPU` samples can miss.
- **Server**: `serve.py`'s built-in Flask **development** server (single
  process, single thread) — i.e. these numbers are a *conservative floor*
  for a naive deployment, not a tuned production server (see caveats
  below).

## Results: 3-minute run, 10 concurrent simulated viewers

### Traffic generated

| Metric | Value |
|---|---|
| Requests | 1,420 |
| Errors | 0 |
| Bytes served | ~2.39 GB over 180s |
| Aggregate throughput | ~13.3 Mbps (~1.3 Mbps/viewer) |

### `serve.py` (long-running process)

| Metric | Value |
|---|---|
| RSS, idle | 38.8 MB |
| RSS, end of 3min load | 41.7 MB (flat — no leak observed) |
| Peak point-in-time CPU (5s sample) | ~9% of one core |
| **Average CPU over the full run** | **~5.1% of one core** (11.2 CPU-s / 220 wall-s) |

Memory does not scale with the loop package's size on disk — segments are
read from disk per-request via `send_file`, never cached/buffered
in-process — so a large loop package inflates ephemeral storage
requirements, not the server's RSS.

### `bake.py` (one-shot task, run once per schedule change — SCOPE.md §4.1)

| Metric | Value |
|---|---|
| Peak combined RSS (bake.py + gpac subprocess) | ~288 MB |
| Wall time | ~3.5s (this 204s/2-track asset) |
| Output package size on disk | ~232 MB |

Bake is CPU/memory-heavier per-second than serving, but runs once and
exits — size it as a burst task, not a steady-state process.

## Sizing recommendation

Confirms `SCOPE.md` §8: there is genuinely very little CPU/memory pressure
in the *serve* path — it's manifest-text templating (integer arithmetic +
string formatting, see `loop_math.py`) plus static file reads, no
decode/encode/remux at request time.

### Fargate — `serve` task (long-running, one per channel)

- **0.25 vCPU / 0.5 GB** is comfortably sufficient at this traffic level
  (~5% of one core, ~42 MB RSS leaves large headroom even at the smallest
  Fargate task size).
- Caveat: the built-in Flask dev server is single-threaded and explicitly
  not production-grade (it says so in its own startup banner). It
  serializes concurrent requests. For real viewer counts:
  - front it with **gunicorn/uwsgi** with a handful of worker processes
    (still cheap — this is I/O-bound work, not CPU-bound, so a handful of
    workers goes a long way);
  - put a **CDN in front of `/seg/*` and `/audio/seg/*`** (SCOPE.md §8's
    existing recommendation) so the origin only needs to sustain the
    CDN's cache-fill rate, not full audience traffic.
- Even with a few gunicorn workers, **0.5–1 vCPU / 1 GB** should comfortably
  cover a single channel.

### Fargate — `bake` task (one-shot, run per schedule change)

- **1 vCPU / 2 GB** is a safe starting point (peak observed here: ~288 MB
  for a small 204s clip). Scale up for longer and/or higher-bitrate source
  content — GPAC needs to hold more in flight. Since this is a one-shot
  task rather than always-on, some over-provisioning here is cheap.

### EC2 equivalent

A `t3.small`/`t4g.small` (2 vCPU, 2 GB) comfortably runs both the serve
process and occasional bake jobs for a single channel. Multiple channels
are multiple containers/tasks (SCOPE.md §3: no multi-channel orchestration
in v1) — scale horizontally, not vertically.

## Reproducing these measurements

```bash
# 1. Bake a package
./run.sh path/to/output.ts --output /tmp/perf-test --segment-duration 4 --skip-bake=false &
# (or just: python3 bake.py path/to/output.ts --output /tmp/perf-test)

# 2. Start serving
python3 serve.py /tmp/perf-test --epoch-utc $(date -u +%Y-%m-%dT%H:%M:%SZ) --port 8400 &
SERVE_PID=$!

# 3. Generate load for 3 minutes with 10 concurrent simulated viewers
python3 load_test.py --base-url http://127.0.0.1:8400 --viewers 10 --duration 180

# 4. In another terminal, sample the server process while the load runs:
while sleep 5; do ps -p $SERVE_PID -o pid,rss,pcpu,pmem,time; done
```

`load_test.py` prints running totals (requests/bytes/errors) every 5s;
cross-check average CPU with the final `ps -o time` value (cumulative
CPU-seconds) divided by total wall-clock elapsed time.
