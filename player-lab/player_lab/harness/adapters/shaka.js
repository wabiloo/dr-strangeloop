import { loadScript } from '../loadscript.js';

export default {
  id: 'shaka',
  formats: ['hls', 'dash'],
  reports: ['errors', 'quality', 'periods'],
  version: null,
  async load({ video, url, format, emit }) {
    await loadScript('./vendor/shaka-player/dist/shaka-player.compiled.js');
    shaka.polyfill.installAll();
    this.version = shaka.Player.version;
    const p = new shaka.Player();
    await p.attach(video);
    p.addEventListener('error', (e) => emit('error', { message: `${e.detail.code} ${e.detail.message || ''}`.slice(0, 300), fatal: e.detail.severity === 2 }));
    p.addEventListener('variantchanged', () => emit('quality', {}));
    // Shaka flattens Periods/discontinuities into one timeline and has no event for them, but each
    // 'segmentappended' carries the presentation start and the segment's own media timestamp: the
    // difference between the two jumps when playback moves onto a new Period / discontinuity.
    // Shaka does not parse a media timestamp from fMP4 (DASH) segments, so there it cannot tell: not reported.
    if (format === 'dash') this.reports = this.reports.filter((r) => r !== 'periods');
    let offset = null;
    p.addEventListener('segmentappended', (e) => {
      if (e.contentType !== 'video' || e.mediaTimestamp == null) return;
      const o = e.start - e.mediaTimestamp;
      if (offset !== null && Math.abs(o - offset) > 0.5) emit('period', { id: o.toFixed(2) });
      offset = o;
    });
    await p.load(url);
    this.handle = p;
  },
};
