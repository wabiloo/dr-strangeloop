// Harness entry point. Query: ?player=<id>&url=<manifest>&format=hls|dash
// Exposes window.__lab = { ready, snapshot() } for the driver (Playwright or Igor).
import { createObserver } from './observer.js';

const params = new URLSearchParams(location.search);
const playerId = params.get('player');
const url = params.get('url');
const format = params.get('format') || (url && url.includes('.m3u8') ? 'hls' : 'dash');
const video = document.getElementById('v');
const statusEl = document.getElementById('status');
document.getElementById('title').textContent = `${playerId} ${format} ${url}`;

const events = { errors: [], periodTransitions: 0, qualityChanges: 0, markers: [], log: [] };
const clock = () => performance.now() / 1000;
const t0 = clock();
const emit = (kind, detail) => {
  const at = +(clock() - t0).toFixed(2);
  if (kind === 'error') events.errors.push({ atS: at, ...detail });
  else if (kind === 'period') events.periodTransitions++;
  else if (kind === 'quality') events.qualityChanges++;
  else if (kind === 'marker') events.markers.push({ atS: at, ...detail });
  if (events.log.length < 200) events.log.push([at, kind, detail && (detail.message || detail.id || '')]);
};

let observer = null;
let adapter = null;
const lab = (window.__lab = {
  ready: false,
  playerId,
  format,
  snapshot() {
    const obs = observer ? observer.snapshot() : null;
    return {
      player: playerId,
      format,
      url,
      reports: adapter ? adapter.reports : [],
      version: adapter ? adapter.version : null,
      ...(obs || {}),
      errors: events.errors,
      fatalErrors: events.errors.filter((e) => e.fatal !== false).length,
      periodTransitions: adapter && adapter.reports.includes('periods') ? events.periodTransitions : null,
      qualityChanges: events.qualityChanges,
      markers: events.markers,
      log: events.log,
    };
  },
});

try {
  adapter = (await import(`./adapters/${playerId}.js`)).default;
  if (!adapter.formats.includes(format)) throw new Error(`${playerId} does not support ${format}`);
  observer = createObserver(() => document.querySelector('video'), clock, () => (adapter && adapter.live ? adapter.live() : null));
  // `ll` = the manifest advertises low latency (the page knows it from the channel config)
  await adapter.load({ video, url, format, emit, lowLatency: params.get('ll') === '1' });
  lab.ready = true;
} catch (e) {
  emit('error', { message: String((e && e.message) || e), fatal: true, where: 'load' });
  lab.ready = true;
}
setInterval(() => {
  const s = lab.snapshot();
  const lat = s.latencyTrace && s.latencyTrace.length ? s.latencyTrace[s.latencyTrace.length - 1] : null;
  const live = lat ? ` latency=${lat[1] !== null ? lat[1] : lat[2]}s rate=${lat[4]}` : '';
  statusEl.textContent = `t=${s.elapsedS} playhead=${s.playhead} started=${s.startedAfterS} stalls=${s.stallCount} errors=${s.errors.length} periods=${s.periodTransitions}${live}`;
}, 1000);
