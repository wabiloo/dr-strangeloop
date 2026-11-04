import { loadScript } from '../loadscript.js';

export default {
  id: 'shaka',
  formats: ['hls', 'dash'],
  reports: ['errors', 'quality'],
  version: null,
  async load({ video, url, emit, lowLatency }) {
    await loadScript('./vendor/shaka-player/dist/shaka-player.compiled.js');
    shaka.polyfill.installAll();
    this.version = shaka.Player.version;
    const p = new shaka.Player();
    await p.attach(video);
    // Without this Shaka plays a low-latency manifest as a normal live one (no LL-HLS parts, no early requests).
    if (lowLatency) p.configure({ streaming: { lowLatencyMode: true } });
    p.addEventListener('error', (e) => emit('error', { message: `${e.detail.code} ${e.detail.message || ''}`.slice(0, 300), fatal: e.detail.severity === 2 }));
    p.addEventListener('variantchanged', () => emit('quality', {}));
    // Shaka flattens Periods/discontinuities into one timeline and has no event for them (shaka-project/shaka-player#3159,
    // closed unfixed): no transitions are reported, in either format.
    await p.load(url);
    this.handle = p;
  },
  // Live latency: the playhead's wall-clock time (Shaka's own figure, when it has one, is kept alongside).
  live() {
    const p = this.handle;
    const d = p && p.isLive() ? p.getPlayheadTimeAsDate() : null;
    if (!d) return null;
    const s = p.getStats();
    return { playheadDateMs: d.getTime(), latencyS: s && typeof s.liveLatency === 'number' ? s.liveLatency : null, targetS: null };
  },
};
