// Run: node --test --experimental-strip-types src/utils/channelConfigLayout.test.ts
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  CONFIG_FIELD_LABEL,
  CONFIG_SECTION_TITLE,
  CONFIG_VERSION,
  configVersionOf,
  buildConfigSections,
  channelDefaultsByParam,
  formFieldsFromConfig,
  lambdaFieldsFromConfig,
  advertisesLowLatency,
  lowLatencyFromConfig,
  lowLatencyIssues,
  lowLatencyProblems,
  announceOnFromConfig,
  periodTypesFromConfig,
  usesChannelSection,
} from './channelConfigLayout.ts'

// A fully specified config in the current TOML layout (a parsed channel TOML).
const FULL = {
  deploy: { name: 'demo', backend: 'local-docker' },
  input: { source_path: '../outputs/demo.ts', source_kind: 'playlist' },
  infrastructure: { docker: { port: 8090, bundle_content: false } },
  markers: {
    increment_event_ids: true,
    split_on: [0x22, 0x30],
    announce_ahead: 10,
    announce_on: [0x22, 'splice_insert'],
    hls: { descriptor_grouping: 'grouped', cue_tags: 'alongside', id_template: 'x-{eventid}' },
    dash: { descriptor_grouping: 'narrowed', signal_format: 'xml' },
  },
  packaging: {
    segment_duration: 2,
    dvr_window: 45,
    hls: { container: 'ts', mux_audio: false },
    dash: { timeline_style: 'compact', addressing: 'time' },
  },
  timeline: { epoch: '2026-03-04T05:06:07Z', continuous: false },
  query: { timeshift: true, overrides: false, prefix: 'dsl_', start_param: 'from', end_param: 'to' },
  timeshift: { max_span_seconds: 1800 },
}

test('formFieldsFromConfig reads every setting from its new table', () => {
  assert.deepEqual(formFieldsFromConfig(FULL), {
    segment_duration: 2,
    dvr_window_seconds: 45,
    hls_format: 'ts',
    hls_ts_mux_audio: false,
    dash_addressing: 'time',
    dash_timeline: 'compact',
    dash_low_latency: 'off',
    hls_low_latency: 'off',
    chunk_duration: 0.5,
    target_latency: null,
    utc_timing: 'direct',
    utc_timing_url: '',
    epoch_utc: '2026-03-04T05:06:07Z',
    continuous: false,
    period_on_segmentation: ['0x22', '0x30'],
    announce_ahead: 10,
    announce_on: ['0x22', 'splice_insert'],
    timeshift_enabled: true,
    query_overrides: false,
    query_prefix: 'dsl_',
    timeshift_start_param: 'from',
    timeshift_end_param: 'to',
    timeshift_max_span_seconds: 1800,
    daterange_mode: 'grouped',
    cue_tags: 'alongside',
    increment_event_ids: true,
    daterange_id_format: 'x-{eventid}',
    dash_signal_format: 'xml',
    dash_descriptor_mode: 'narrowed',
  })
})

test('formFieldsFromConfig: an empty config gives its-a-live defaults', () => {
  assert.deepEqual(formFieldsFromConfig({}), {
    segment_duration: 4,
    dvr_window_seconds: 30,
    hls_format: 'cmaf',
    hls_ts_mux_audio: true,
    dash_addressing: 'number',
    dash_timeline: 'full',
    dash_low_latency: 'off',
    hls_low_latency: 'off',
    chunk_duration: 0.5,
    target_latency: null,
    utc_timing: 'direct',
    utc_timing_url: '',
    epoch_utc: '2026-01-01T00:00:00Z',
    continuous: true,
    period_on_segmentation: [],
    announce_ahead: 0,
    announce_on: [],
    timeshift_enabled: true,
    query_overrides: true,
    query_prefix: '',
    timeshift_start_param: 'start',
    timeshift_end_param: 'end',
    timeshift_max_span_seconds: 21600,
    daterange_mode: 'shared',
    cue_tags: 'none',
    increment_event_ids: false,
    daterange_id_format: '{segcode}-{eventid}-{loop}',
    dash_signal_format: 'binary',
    dash_descriptor_mode: 'shared',
  })
})

test('formFieldsFromConfig does not read the pre-rename key names', () => {
  const old = {
    markers: { daterange_mode: 'grouped', cue_tags: 'only', dash_signal_format: 'xml', period_on_segmentation: [0x22] },
    packaging: { dvr_window_seconds: 99, hls_format: 'ts', dash_timeline: 'compact' },
    timeline: { epoch_utc: '2027-01-01T00:00:00Z' },
    timeshift: { enabled: false, start_param: 'from' },
    query: { enabled: false },
  }
  const f = formFieldsFromConfig(old)
  assert.equal(f.daterange_mode, 'shared')
  assert.equal(f.cue_tags, 'none')
  assert.equal(f.dash_signal_format, 'binary')
  assert.deepEqual(f.period_on_segmentation, [])
  assert.equal(f.dvr_window_seconds, 30)
  assert.equal(f.hls_format, 'cmaf')
  assert.equal(f.dash_timeline, 'full')
  assert.equal(f.epoch_utc, '2026-01-01T00:00:00Z')
  assert.equal(f.timeshift_enabled, true)
  assert.equal(f.timeshift_start_param, 'start')
})

test('HLS and DASH descriptor grouping are read from their own tables', () => {
  const f = formFieldsFromConfig({ markers: { hls: { descriptor_grouping: 'grouped' } } })
  assert.equal(f.daterange_mode, 'grouped')
  assert.equal(f.dash_descriptor_mode, 'shared')
  const g = formFieldsFromConfig({ markers: { dash: { descriptor_grouping: 'narrowed' } } })
  assert.equal(g.daterange_mode, 'shared')
  assert.equal(g.dash_descriptor_mode, 'narrowed')
})

test('periodTypesFromConfig: ints or strings -> hex, junk dropped', () => {
  assert.deepEqual(periodTypesFromConfig([0x22, '48', 256, 'x', -1]), ['0x22', '0x30'])
  assert.deepEqual(periodTypesFromConfig(undefined), [])
})

test('announceOnFromConfig: ints, hex strings and splice_insert, deduplicated, junk dropped', () => {
  assert.deepEqual(announceOnFromConfig([0x22, 'splice_insert', '0x30', 34, 'Splice_Insert', 'x', 256]), ['0x22', 'splice_insert', '0x30'])
  assert.deepEqual(announceOnFromConfig(undefined), [])
})

test('every config section is one TOML table, titled after it', () => {
  assert.deepEqual(Object.keys(CONFIG_SECTION_TITLE), [
    'deploy', 'input', 'infrastructure.aws', 'infrastructure.s3', 'infrastructure.express', 'infrastructure.lambda',
    'infrastructure.docker', 'timeline', 'packaging', 'packaging.hls', 'packaging.dash', 'query', 'timeshift',
    'markers', 'markers.hls', 'markers.dash',
  ])
  assert.equal(CONFIG_SECTION_TITLE['markers.hls'], 'Markers · HLS')
  assert.equal(CONFIG_SECTION_TITLE['packaging.dash'], 'Packaging · DASH')
})

test('buildConfigSections: one group per table, same keys as the TOML', () => {
  const sections = buildConfigSections(FULL)
  const byId = Object.fromEntries(sections.map((s) => [s.id, s]))
  const rows = (id: string) => Object.fromEntries(byId[id].rows.map((r) => [r.key, r.value]))
  assert.deepEqual(sections.map((s) => s.id), [
    'input', 'infrastructure.docker', 'timeline', 'packaging', 'packaging.hls', 'packaging.dash',
    'query', 'timeshift', 'markers', 'markers.hls', 'markers.dash',
  ])
  assert.deepEqual(rows('timeline'), { epoch_utc: '2026-03-04T05:06:07Z', continuous: 'no' })
  assert.deepEqual(rows('packaging'), { segment_duration: '2', dvr_window_seconds: '45' })
  assert.deepEqual(rows('packaging.hls'), { hls_format: 'ts', hls_ts_mux_audio: 'no' })
  assert.deepEqual(rows('packaging.dash'), { dash_addressing: 'time', dash_timeline: 'compact', dash_low_latency: 'off' })
  assert.deepEqual(rows('query'), {
    timeshift_enabled: 'yes', query_overrides: 'no', query_prefix: 'dsl_', timeshift_start_param: 'from',
    timeshift_end_param: 'to',
  })
  assert.deepEqual(rows('timeshift'), { timeshift_max_span_seconds: '1800' })
  assert.deepEqual(rows('markers'), {
    increment_event_ids: 'yes', period_on_segmentation: '0x22, 0x30', announce_ahead: '10 s', announce_on: '0x22, splice_insert',
  })
  assert.deepEqual(rows('markers.hls'), { daterange_mode: 'grouped', cue_tags: 'alongside', daterange_id_format: 'x-{eventid}' })
  assert.deepEqual(rows('markers.dash'), { dash_descriptor_mode: 'narrowed', dash_signal_format: 'xml' })
  assert.equal(byId['markers.hls'].title, 'Markers · HLS')
  for (const s of sections) for (const r of s.rows) assert.equal(r.label, CONFIG_FIELD_LABEL[r.key as keyof typeof CONFIG_FIELD_LABEL])
})

test('buildConfigSections: query section shows defaults for a config without [query]; prefix none', () => {
  const sections = buildConfigSections({ deploy: { backend: 'ecs-express' } })
  const query = sections.find((s) => s.id === 'query')!
  assert.deepEqual(Object.fromEntries(query.rows.map((r) => [r.key, r.value])), {
    timeshift_enabled: 'yes', query_overrides: 'yes', query_prefix: 'none', timeshift_start_param: 'start',
    timeshift_end_param: 'end',
  })
})

test('buildConfigSections: with both switches off the detail rows and [timeshift] are hidden', () => {
  const sections = buildConfigSections({ deploy: { backend: 'local-docker' }, query: { timeshift: false, overrides: false } })
  const query = sections.find((s) => s.id === 'query')!
  assert.deepEqual(query.rows.map((r) => r.key), ['timeshift_enabled', 'query_overrides'])
  assert.equal(sections.some((s) => s.id === 'timeshift'), false)
})

test('buildConfigSections: each switch shows only its own detail rows', () => {
  const keys = (query: Record<string, unknown>) =>
    buildConfigSections({ deploy: { backend: 'local-docker' }, query })
      .find((s) => s.id === 'query')!.rows.map((r) => r.key)
  assert.deepEqual(keys({ timeshift: true, overrides: false }), [
    'timeshift_enabled', 'query_overrides', 'query_prefix', 'timeshift_start_param', 'timeshift_end_param',
  ])
  assert.deepEqual(keys({ timeshift: false, overrides: true }), ['timeshift_enabled', 'query_overrides', 'query_prefix'])
  const only = buildConfigSections({ deploy: { backend: 'local-docker' }, query: { timeshift: false, overrides: true } })
  assert.equal(only.some((s) => s.id === 'timeshift'), true) // max_span still caps dvr_window
})

test('config_version: absent or junk reads as 1, the current layout as 2', () => {
  assert.equal(CONFIG_VERSION, 2)
  assert.equal(configVersionOf({}), 1)
  assert.equal(configVersionOf(null), 1)
  assert.equal(configVersionOf({ config_version: 2 }), 2)
  assert.equal(configVersionOf({ config_version: '2' }), 1)
  assert.equal(configVersionOf({ config_version: 0 }), 1)
  assert.equal(configVersionOf({ config_version: 2.5 }), 1)
})

test('buildConfigSections: aws-media has no packaging/markers/query sections', () => {
  const sections = buildConfigSections({ deploy: { backend: 'aws-media' }, input: { source_path: 'x.ts' } })
  assert.deepEqual(sections.map((s) => s.id).filter((id) => /^(packaging|markers|query|timeline)/.test(id)), [])
})

test('buildConfigSections: TS-only mux_audio row appears only for the ts container', () => {
  const cmaf = buildConfigSections({ deploy: { backend: 'local-docker' }, packaging: { hls: { container: 'cmaf', mux_audio: true } } })
  // a CMAF channel on a backend that can do low latency also shows the (off) low-latency row; no mux_audio row
  assert.deepEqual(cmaf.find((s) => s.id === 'packaging.hls')!.rows.map((r) => r.key), ['hls_format', 'hls_low_latency'])
})

test('buildConfigSections: archive sources show "as source" instead of a segment duration', () => {
  const sections = buildConfigSections({
    deploy: { backend: 'local-docker' }, input: { source_kind: 'archive', source_path: 'a/manifest.json' },
    packaging: { segment_duration: 4 },
  })
  const packaging = sections.find((s) => s.id === 'packaging')!
  assert.equal(packaging.rows.find((r) => r.key === 'segment_duration')!.value, 'as source')
})

test('channelDefaultsByParam: the channel value of every overridable setting, by parameter name', () => {
  assert.deepEqual(channelDefaultsByParam(formFieldsFromConfig(FULL)), {
    timeline_mode: 'periodic',
    epoch: '2026-03-04T05:06:07Z',
    dvr_window: '45',
    timeline_style: 'compact',
    addressing: 'time',
    low_latency: 'HLS off (not enabled), DASH off (not enabled)',
    ll_target_latency: 'automatic',
    ll_utc_timing: 'direct',
    cue_tags: 'alongside',
    descriptor_grouping: 'HLS grouped, DASH narrowed',
    increment_event_ids: 'on',
    signal_format: 'xml',
    id_template: 'x-{eventid}',
    split_on: '0x22,0x30',
    announce_ahead: '10 s',
    announce_on: '0x22,splice_insert',
  })
})

test('channelDefaultsByParam: defaults for an empty config, and only names the registry knows', async () => {
  const d = channelDefaultsByParam(formFieldsFromConfig({}))
  assert.equal(d.timeline_mode, 'continuous')
  assert.equal(d.split_on, 'none')
  assert.equal(d.announce_ahead, 'off')
  assert.equal(d.announce_on, 'every marker')
  assert.equal(d.increment_event_ids, 'off')
  const { QP_NAMES } = await import('./queryParams.ts')
  for (const name of Object.keys(d)) assert.ok(QP_NAMES.includes(name), name)
  assert.deepEqual(QP_NAMES.filter((n) => !(n in d)), ['max_height', 'max_bitrate']) // no channel setting to default to
})

const LAMBDA = {
  deploy: { name: 'lam', backend: 'aws-lambda' },
  packaging: { segment_duration: 4, dvr_window: 30 },
  input: { source_path: '../outputs/demo.ts', source_kind: 'playlist' },
  infrastructure: {
    aws: { region: 'eu-west-1' },
    s3: { bucket_name: 'b', content_folder: 'c' },
    lambda: { memory: 1769, timeout: 60, reserved_concurrency: -1, cache_mb: 2048, architecture: 'x86_64' },
  },
}

test('lambdaFieldsFromConfig reads [infrastructure.lambda], defaults for a missing key', () => {
  assert.deepEqual(lambdaFieldsFromConfig(LAMBDA), {
    lambda_memory: 1769,
    lambda_timeout: 60,
    lambda_reserved_concurrency: -1,
    lambda_cache_mb: 2048,
    lambda_architecture: 'x86_64',
  })
  assert.deepEqual(lambdaFieldsFromConfig({}), {
    lambda_memory: 1024,
    lambda_timeout: 30,
    lambda_reserved_concurrency: 10,
    lambda_cache_mb: 512,
    lambda_architecture: 'arm64',
  })
})

test('aws-lambda: has its own infrastructure section, no port, plus the shared serve sections', () => {
  const ids = buildConfigSections(LAMBDA).map((s) => s.id)
  assert.ok(ids.includes('infrastructure.lambda'))
  assert.ok(!ids.includes('infrastructure.express') && !ids.includes('infrastructure.docker'))
  assert.ok(ids.includes('infrastructure.aws') && ids.includes('infrastructure.s3'))
  assert.ok(ids.includes('timeline') && ids.includes('packaging') && ids.includes('query'))
  const lambda = buildConfigSections(LAMBDA).find((s) => s.id === 'infrastructure.lambda')!
  assert.deepEqual(
    lambda.rows.map((r) => [r.key, r.value]),
    [
      ['lambda_memory', '1769'],
      ['lambda_timeout', '60'],
      ['lambda_reserved_concurrency', '-1'],
      ['lambda_cache_mb', '2048'],
      ['lambda_architecture', 'x86_64'],
    ],
  )
  assert.equal(CONFIG_SECTION_TITLE['infrastructure.lambda'], 'Infrastructure · Lambda')
  assert.ok(usesChannelSection('aws-lambda') && !usesChannelSection('aws-media'))
})


// ── low latency ───────────────────────────────────────────────────────────

const withLl = (
  llDash: unknown,
  llHls: unknown,
  extra: { chunk?: number; backend?: string; container?: string; dash?: Record<string, unknown>; infra?: object } = {},
) => ({
  ...FULL,
  deploy: { name: 'demo', backend: extra.backend ?? 'ecs-express' },
  packaging: {
    ...FULL.packaging,
    ...(extra.chunk === undefined ? {} : { chunk_duration: extra.chunk }),
    hls: { container: extra.container ?? 'cmaf', mux_audio: true, low_latency: llHls },
    dash: { ...(llDash === undefined ? {} : { low_latency: llDash }), ...(extra.dash ?? {}) },
  },
  ...(extra.infra ?? {}),
})

test('lowLatencyFromConfig: the choices, the TOML booleans, and junk reads as off', () => {
  assert.equal(lowLatencyFromConfig('on'), 'on')
  assert.equal(lowLatencyFromConfig('optin'), 'optin')
  assert.equal(lowLatencyFromConfig(true), 'on')
  for (const v of [false, undefined, null, 'off', 'fast', 3]) assert.equal(lowLatencyFromConfig(v), 'off')
})

test('formFieldsFromConfig reads each format\'s low latency and the shared chunk duration from their tables', () => {
  const f = formFieldsFromConfig(
    withLl('optin', 'on', { chunk: 0.25, dash: { target_latency: 1.5, utc_timing: 'http', utc_timing_url: 'https://t.example/time' } }),
  )
  assert.deepEqual(
    [f.dash_low_latency, f.hls_low_latency, f.chunk_duration, f.target_latency, f.utc_timing, f.utc_timing_url],
    ['optin', 'on', 0.25, 1.5, 'http', 'https://t.example/time'],
  )
  assert.equal(formFieldsFromConfig(withLl(true, false)).dash_low_latency, 'on') // TOML booleans
  assert.equal(formFieldsFromConfig(withLl('on', 'off')).target_latency, null) // absent = automatic
  assert.equal(formFieldsFromConfig(withLl('on', 'off')).chunk_duration, 0.5)
})

test('buildConfigSections: low latency rows follow the table each key is stored in', () => {
  const rows = (cfg: object, id: string) => {
    const s = buildConfigSections(cfg as never).find((x) => x.id === id)
    return Object.fromEntries((s?.rows ?? []).map((r) => [r.key, r.value]))
  }
  // off: one row per format, no chunk size
  assert.equal(rows(withLl('off', 'off'), 'packaging.dash').dash_low_latency, 'off')
  assert.equal(rows(withLl('off', 'off'), 'packaging.hls').hls_low_latency, 'off')
  assert.equal(rows(withLl('off', 'off'), 'packaging').chunk_duration, undefined)
  // DASH only: the chunk size is in [packaging], the DASH rows in [packaging.dash]
  const dash = withLl('on', 'off', { chunk: 0.5, dash: { target_latency: 2 } })
  assert.equal(rows(dash, 'packaging').chunk_duration, '0.5')
  assert.deepEqual(rows(dash, 'packaging.dash'), {
    dash_addressing: 'number', dash_timeline: 'full', dash_low_latency: 'on', target_latency: '2', utc_timing: 'direct',
  })
  // HLS only: the chunk size shows, but no DASH detail rows
  const hls = withLl('off', 'optin')
  assert.equal(rows(hls, 'packaging').chunk_duration, '0.5')
  assert.equal(rows(hls, 'packaging.hls').hls_low_latency, 'optin')
  assert.equal(rows(hls, 'packaging.dash').utc_timing, undefined)
  // the URL row is only for "http", and says when it is automatic
  assert.equal(rows(withLl('on', 'off', { dash: { utc_timing: 'http' } }), 'packaging.dash').utc_timing_url, 'automatic (set at deploy)')
  assert.equal(rows(withLl('on', 'off', { dash: { utc_timing: 'none' } }), 'packaging.dash').utc_timing_url, undefined)
  // MPEG-TS cannot carry parts: no HLS row; and neither format is offered on aws-lambda
  assert.equal(rows(withLl('off', 'off', { container: 'ts' }), 'packaging.hls').hls_low_latency, undefined)
  const lambda = withLl('on', 'on', { backend: 'aws-lambda', infra: { infrastructure: LAMBDA.infrastructure } })
  assert.equal(rows(lambda, 'packaging.dash').dash_low_latency, undefined)
  assert.equal(rows(lambda, 'packaging.hls').hls_low_latency, undefined)
  assert.equal(rows(lambda, 'packaging').chunk_duration, undefined)
})

test('channelDefaultsByParam: what a request gets without the low-latency parameters, per format', () => {
  const d = (dash: unknown, hls: unknown, extra: object = {}) => channelDefaultsByParam(formFieldsFromConfig(withLl(dash, hls, extra)))
  assert.equal(d('on', 'off').low_latency, 'HLS off (not enabled), DASH on')
  assert.equal(d('optin', 'on').low_latency, 'HLS on, DASH off (opt-in)')
  assert.equal(d('off', 'off').low_latency, 'HLS off (not enabled), DASH off (not enabled)')
  assert.equal(d('on', 'off', { dash: { target_latency: 3 } }).ll_target_latency, '3')
  assert.equal(d('on', 'off', { dash: { utc_timing: 'none' } }).ll_utc_timing, 'none')
})

const LLF = {
  backend: 'local-docker' as const, dash_low_latency: 'on' as const, hls_low_latency: 'off' as const, hls_format: 'cmaf' as const,
  segment_duration: 2, chunk_duration: 0.5, target_latency: null, utc_timing: 'direct' as const, utc_timing_url: '',
  query_overrides: true,
}

test('lowLatencyProblems: a valid setup, and off needs nothing', () => {
  assert.deepEqual(lowLatencyProblems(LLF), [])
  assert.deepEqual(lowLatencyProblems({ ...LLF, dash_low_latency: 'off', chunk_duration: 99 }), []) // ignored while both are off
  assert.deepEqual(lowLatencyProblems({ ...LLF, dash_low_latency: 'optin' }), [])
  assert.deepEqual(lowLatencyProblems({ ...LLF, dash_low_latency: 'off', hls_low_latency: 'on' }), [])
})

test('lowLatencyProblems: each rule the server enforces', () => {
  const has = (f: object, text: RegExp) => lowLatencyProblems({ ...LLF, ...f }).some((p) => text.test(p))
  assert.ok(has({ backend: 'aws-lambda' }, /not supported on the aws-lambda/))
  assert.ok(has({ backend: 'aws-media', dash_low_latency: 'off', hls_low_latency: 'on' }, /not supported/))
  assert.ok(has({ chunk_duration: 2 }, /shorter than the segment duration/)) // not below the 2 s segment
  assert.ok(has({ dash_low_latency: 'off', hls_low_latency: 'on', chunk_duration: 0 }, /Chunk duration/)) // either format
  assert.ok(has({ dash_low_latency: 'optin', query_overrides: false }, /Opt-in needs/))
  assert.ok(has({ dash_low_latency: 'off', hls_low_latency: 'optin', query_overrides: false }, /Opt-in needs/))
  assert.ok(has({ hls_low_latency: 'on', hls_format: 'ts' }, /needs the CMAF container/))
  assert.ok(!has({ dash_low_latency: 'on', hls_format: 'ts' }, /CMAF/)) // DASH is always CMAF
  assert.ok(has({ target_latency: 0 }, /Target latency/))
  assert.ok(has({ target_latency: 121 }, /Target latency/))
  assert.ok(has({ utc_timing: 'direct', utc_timing_url: 'https://t/x' }, /only applies to clock sync/))
  assert.ok(has({ utc_timing: 'http', utc_timing_url: '/time' }, /absolute http\(s\) URL/))
  assert.deepEqual(lowLatencyProblems({ ...LLF, utc_timing: 'http', utc_timing_url: 'https://t.example/time' }), [])
  // the target / URL rules apply even with low latency off, like the server's
  assert.ok(has({ dash_low_latency: 'off', target_latency: 500 }, /Target latency/))
})

test('lowLatencyIssues: each problem is attributed to the form group that shows it', () => {
  const part = (f: object, text: RegExp) => lowLatencyIssues({ ...LLF, ...f }).find((i) => text.test(i.message))?.part
  assert.equal(part({ chunk_duration: 2 }, /Chunk duration/), 'packaging')
  assert.equal(part({ hls_low_latency: 'on', hls_format: 'ts' }, /CMAF/), 'hls')
  assert.equal(part({ target_latency: 500 }, /Target latency/), 'dash')
  assert.equal(part({ dash_low_latency: 'optin', query_overrides: false }, /Opt-in/), 'dash')
  assert.equal(part({ dash_low_latency: 'off', hls_low_latency: 'optin', query_overrides: false }, /Opt-in/), 'hls')
})

test('advertisesLowLatency: on follows the URL unless it turns it off; optin only when asked; prefix respected', () => {
  const u = 'http://h/index.mpd'
  assert.equal(advertisesLowLatency('on', u), true)
  assert.equal(advertisesLowLatency('on', `${u}?low_latency=false`), false)
  assert.equal(advertisesLowLatency('on', `${u}?a=1&low_latency=true`), true)
  assert.equal(advertisesLowLatency('optin', u), false)
  assert.equal(advertisesLowLatency('optin', `${u}?low_latency=true`), true)
  assert.equal(advertisesLowLatency('optin', `${u}?low_latency=false`), false)
  assert.equal(advertisesLowLatency('off', `${u}?low_latency=true`), false)
  assert.equal(advertisesLowLatency(undefined, u), false)
  assert.equal(advertisesLowLatency('on', null), false)
  // a prefixed channel: the bare name is not its parameter
  assert.equal(advertisesLowLatency('optin', `${u}?low_latency=true`, 'dsl_'), false)
  assert.equal(advertisesLowLatency('optin', `${u}?dsl_low_latency=true`, 'dsl_'), true)
  assert.equal(advertisesLowLatency('optin', `${u}?x_low_latency=true`), false)
})
