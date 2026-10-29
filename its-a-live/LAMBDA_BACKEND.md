# its-a-live: an `aws-lambda` backend — scope

Status: implemented on the `aws-lambda-backend` branch (loop-dee-loop
`s3_package.py`/`package_io.py`/`Dockerfile.lambda`; its-a-live
`lambda_stack.py`/`_aws_lambda_ops.py`; igor, galvanise, docs). Not yet
deployed against a real account: the acceptance steps in §11 that need AWS
(3 and 4) are still to do, and §9's cost figures are estimates.
Self-contained: the implementer should not need the chat that produced it,
but should ask before deviating from anything marked **DECIDED**.

Deviations from the first draft of this document, found while building it:
`reserved_concurrency = -1` means "no reservation" and `0` is rejected (it is
what `stop` sets); `/timeline.json` and `/health` got their own uncached
all-query-string CloudFront behaviors (the manifest cache policy's allow-list
would have dropped their parameters, breaking igor and player-lab); the
manifest cache policy has a high max TTL so time-shift responses'
own `Cache-Control` is honoured; `start` reads the cap from the function's
`RESERVED_CONCURRENCY` env var because the scheduler Lambda calls it without
a config; `refresh` also invalidates the CloudFront cache.

## 1. Problem and goal

`ecs-express` keeps one Fargate task per channel running whenever the
channel is started (~$108/yr at 0.25 vCPU / 512 MB, plus a share of the
ALB and a public IPv4). For rarely-watched channels that is almost all
waste, and `stop` -> `start` is a manual, minutes-long step.

`aws-lambda` serves the same loop package from a Lambda function behind
CloudFront: **$0 while nobody watches, the first viewer wakes it up, no
ECS cluster, no ALB, no `start` needed for the channel to be playable.**
Same `serve.py`, same URLs, same manifests, same query parameters.

Non-goals: no MediaLive/MediaPackage; no change to the loop package or to
`bake.py`; no change to any URL a player sees; not replacing `ecs-express`
(heavily-watched channels are still better on a task — see §9).

## 2. Key finding that shapes the design

"Serve segments straight from S3, only manifests through Lambda" does
**not** work for the default configuration:

1. `[timeline] continuous = true` (the default) makes `serve.py` rewrite
   each segment's `tfdt`/`mfhd` (CMAF) or PTS/DTS/PCR (TS) **per request**
   (`continuity.py`, `serve.py:_serve_segment`). A `/cseg/<global>` URL
   names a different byte string on every loop, so no static S3 object
   backs it.
2. Even for the un-patched `/seg/<n>` URLs, the on-disk names
   (`1080p_dash_track257_25.m4s`) are discovered by glob at startup, not
   derivable from the URL, so CloudFront cannot map URL -> S3 key without
   a bake-time layout change.

**DECIDED**: *all* requests go to Lambda; S3 is the **storage**, Lambda
reads from it, CloudFront caches what comes back. This is still cheap,
because every segment URL is immutable: CloudFront fetches each unique
segment from Lambda about once per edge/regional cache, then serves it
from cache. Direct CloudFront->S3 for `/seg/` (continuous=false) is a
possible later optimisation (§10), not v1.

## 3. Architecture

```
viewer -> CloudFront ──(OAC, SigV4)──> Lambda Function URL (AWS_IAM, RESPONSE_STREAM)
           caches /seg /cseg /rseg            │  container image: Lambda Web Adapter
           + init*.mp4; manifests 1 s         │  + gunicorn(wsgi:app) = unchanged serve.py
                                              ▼
                                  S3 loop package (written by `spark`, unchanged)
```

- **Package access (DECIDED)**: skeleton + lazy fetch, no format change.
  On cold start `s3_package.materialize(uri, /tmp/pkg)`:
  `ListObjectsV2` the prefix; download every key that is **not**
  `*.m4s`/`*.ts` (descriptor, cues, init segments — all small) for real;
  create a sparse placeholder file (right size, via `truncate`) for every
  `*.m4s`/`*.ts`. `LoopPackage`'s globs, counts and `.exists()` checks then
  pass untouched. At request time the segment routes call a reader hook
  that fetches the object with `GetObject` (and keeps it in an LRU under
  `/tmp`, bounded by `cache_mb`). Cold-start cost is one list call per
  1000 keys plus the small files; an ABR ladder with a 30-minute loop is
  a few thousand keys, i.e. a handful of calls.
- **Runtime (DECIDED)**: container image, **Lambda Web Adapter** extension,
  `gunicorn -w 1 --threads 4 -k gthread wsgi:app`. `wsgi.py` already takes
  its whole configuration from env vars (the ECS path); the Lambda stack
  sets the same vars, plus `LOOP_PACKAGE_STORE=s3`. New slim
  `loop-dee-loop/Dockerfile.lambda`: python + `requirements.txt` + `boto3`,
  **no** GPAC/ffmpeg/awscli (bake never runs here) — the smaller image is
  the main cold-start lever. arm64 (cheaper; every dependency is pure
  Python).
- **Ingress (DECIDED)**: Function URL with `AWS_IAM` + `RESPONSE_STREAM`,
  fronted by CloudFront Origin Access Control. Unlike the ECS ALB, the
  origin is **not** publicly reachable. Streaming lifts the 6 MB buffered
  response cap (a 6 s, 10 Mbps segment is ~7.5 MB).
- **CloudFront (DECIDED)**: mandatory (no `cdn = false`; an uncached,
  IAM-locked Lambda origin has no use). Same cache policies and query-string
  allow-list as `loop_stack.py`, with two deliberate differences:
  manifest `default_ttl` is **1 s** (not 0), so CloudFront collapses
  concurrent identical requests and cost stops scaling with viewer count;
  and `*/init*.mp4` (+ `audio/init*.mp4`) join the segment behaviours, since
  inits are immutable. Time-shift manifests already send their own
  `Cache-Control`.

## 4. Stack (`lambda_stack.py`, `LambdaLoopStack`)

Stack name `ItsALiveStack-<name>-aws-lambda`. No shared stack. Resources:
`DockerImageFunction` (arm64, `memory`, `timeout`, env, log retention
7 days), execution role with `s3:GetObject`/`ListBucket` scoped to
`<content_folder>/<name>/*` (existing bucket referenced only, as today),
`FunctionUrl`, CloudFront distribution + OAC. Outputs keep the existing
names so the rest of the tooling is untouched: `HlsPlaybackUrl`,
`DashPlaybackUrl`, `CloudFrontDomainName`, `LoopPackageS3Uri`,
`S3BucketName`; plus `LambdaFunctionName`, `LambdaFunctionUrl`.
Verify the CDK version's `FunctionUrlOrigin.withOriginAccessControl` grants
both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` (required for
new function URLs).

## 5. Config (`[infrastructure.lambda]`, mirrored in Igor — see AGENTS.md)

| key | default | meaning |
|---|---|---|
| `memory` | 1024 | MB. CPU scales with it; use 1769 (=1 vCPU) for `hls_format = ts` (O(packets) patch) |
| `timeout` | 30 | s |
| `reserved_concurrency` | 10 | blast-radius / cost cap; `-1` = no reservation. Lambda keeps at least 100 unreserved, so reserving needs an account quota above 100 + this — check the limit first (new accounts can be as low as 10; use `-1` there). `0` is not a configuration: `stop` sets it |
| `cache_mb` | 512 | `/tmp` segment LRU (ephemeral storage is sized to match) |
| `architecture` | `arm64` | or `x86_64` |

`[deploy].backend = "aws-lambda"`. `[infrastructure.express]` keys are
ignored. `[timeline]`, `[query]`, `[timeshift]`, `[packaging]`, `[markers]`
work unchanged (they only produce serve flags).

## 6. `channel.py` command semantics (`_aws_lambda_ops.py`)

| command | behaviour |
|---|---|
| `spark` | identical to ecs-express (bake locally, `aws s3 sync`) — reuse, don't copy |
| `create` | `cdk deploy` the stack (no shared stack step) |
| `start` | restore `reserved_concurrency`; `--epoch-utc` handling as ecs-express (env var update) |
| `stop` | reserved concurrency = 0 (viewers get 429 immediately; nothing is billed either way — `stop` is an access control, not a cost control) |
| `refresh` | bump env `LOOP_PACKAGE_REVISION` -> all execution environments recycle and re-read S3 (needed after a re-`spark`) |
| `status` | reserved concurrency + `check_manifest_reachable`; `min_tasks`-style fields n/a |
| `terminate` | `cdk destroy` |

Also: `_scheduler_lambda.py` `_OPS` gets `"aws-lambda"` (and the scheduler
Lambda's bundled modules); `app.py` accepts the new backend;
`channel.py` `_BACKENDS` and dispatch; `galvanise.py --backend`.

## 7. Code changes outside its-a-live

- `loop-dee-loop/s3_package.py` (new): `materialize`, `SegmentReader`
  (S3 GET + LRU). `serve.py`: one `segment_reader` hook replacing the
  `send_file(path)` / `path.read_bytes()` call sites (init routes and
  `_serve_segment`); default stays the filesystem, so every existing
  backend is byte-for-byte unaffected. `wsgi.py`: pick the store from env.
- `igor/`: backend enum (`types.ts`), `BackendBadge.vue`, `channelPhase.ts`,
  `channelConfigLayout.ts` (+ test), `ChannelNew.vue`, `ChannelDetail.vue`,
  `routes/channels.py`, `integrations/its_a_live.py`, tests.
- Docs: `its-a-live/AGENTS.md`, root `AGENTS.md` diagram, `docs/*`,
  `its-a-live/migrate_config.py` untouched (new keys only).
- Version: minor bump, propose after implementation.

## 8. Risks

1. **Cold start** (image init + list + inits): target p50 < 3 s for the
   first manifest. Players tolerate it; `provisioned concurrency` would
   defeat the purpose. INIT is billed.
2. **Concurrency quota.** One execution environment per in-flight request;
   a new account's limit can be as low as 10. CloudFront request collapsing
   helps (all viewers ask for the same URLs). Check quota before shipping.
3. **Stale package after re-`spark`** until `refresh` — same contract as
   ecs-express' `start`/`refresh`.
4. **TS continuity patching** is CPU-bound; memory default is for CMAF.
5. Lambda's S3 reads are the only new failure mode in the byte path:
   retry once, then 502 (CloudFront serves stale on error if configured —
   not in v1).

## 9. Cost (estimate, list prices, arm64 1 GB; not measured)

Per request: manifest ~30 ms warm ≈ $0.6e-6; segment (S3 GET + patch)
~100 ms ≈ $1.5e-6; cold start ~1.5 s ≈ $20e-6. One actively-watched
POP/regional cache for a 3-rendition + audio ladder is ≈ 2 origin
requests/s ≈ $2e-6/s:

- idle: **$0**; one viewer 1 h/day: ~$3/yr; 24/7 on one regional cache:
  ~$65/yr, i.e. already under Fargate's $108 + ALB share + IPv4.
- cost grows with the number of **regional caches touched**, not viewers,
  because manifests are 1 s-cached and segments immutable. Many geographies
  at 24/7 can exceed a task; that is the crossover to move a channel back
  to `ecs-express`.
- CloudFront egress is the same as today and dominates either way.

## 10. Later (not v1)

- `continuous = false` + `/seg/` direct from S3 via CloudFront: needs a
  URL-shaped key layout written at bake time (or a CloudFront Function with
  a generated map); removes Lambda from the segment path.
- Serve-stale-on-error origin group; optional keep-warm schedule.
- Apply the init-caching and 1 s manifest TTL improvements to `loop_stack.py`.

## 11. Acceptance

1. Unit: `materialize` against a stubbed S3 listing yields a package
   `LoopPackage` accepts; `SegmentReader` LRU eviction.
2. Parity: with a fixed clock/epoch, the Flask test client over the
   S3-backed store returns byte-identical manifests and segments to the
   filesystem store, continuous on and off, CMAF and TS.
3. Deploy a real channel; `player-lab run --channel <name>` passes in
   hls.js, dash.js, Shaka; no Period/discontinuity regressions vs
   `/timeline.json`.
4. Measure and record cold-start p50/p95 and per-request billed ms; replace
   §9's estimates with them.
