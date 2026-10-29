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
    epoch_utc: '2026-03-04T05:06:07Z',
    continuous: false,
    period_on_segmentation: ['0x22', '0x30'],
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
    epoch_utc: '2026-01-01T00:00:00Z',
    continuous: true,
    period_on_segmentation: [],
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
  assert.deepEqual(rows('packaging.dash'), { dash_addressing: 'time', dash_timeline: 'compact' })
  assert.deepEqual(rows('query'), {
    timeshift_enabled: 'yes', query_overrides: 'no', query_prefix: 'dsl_', timeshift_start_param: 'from',
    timeshift_end_param: 'to',
  })
  assert.deepEqual(rows('timeshift'), { timeshift_max_span_seconds: '1800' })
  assert.deepEqual(rows('markers'), { increment_event_ids: 'yes', period_on_segmentation: '0x22, 0x30' })
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
  assert.deepEqual(cmaf.find((s) => s.id === 'packaging.hls')!.rows.map((r) => r.key), ['hls_format'])
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
    cue_tags: 'alongside',
    descriptor_grouping: 'HLS grouped, DASH narrowed',
    increment_event_ids: 'on',
    signal_format: 'xml',
    id_template: 'x-{eventid}',
    split_on: '0x22,0x30',
  })
})

test('channelDefaultsByParam: defaults for an empty config, and only names the registry knows', async () => {
  const d = channelDefaultsByParam(formFieldsFromConfig({}))
  assert.equal(d.timeline_mode, 'continuous')
  assert.equal(d.split_on, 'none')
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
