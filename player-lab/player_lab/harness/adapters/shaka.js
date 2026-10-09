import { loadScript } from '../loadscript.js';

export default {
  id: 'shaka',
  formats: ['hls', 'dash'],
  reports: ['errors', 'quality'],
  version: null,
  async load({ video, url, emit }) {
    await loadScript('./vendor/shaka-player/dist/shaka-player.compiled.js');
    shaka.polyfill.installAll();
    this.version = shaka.Player.version;
    const p = new shaka.Player();
    await p.attach(video);
    p.addEventListener('error', (e) => emit('error', { message: `${e.detail.code} ${e.detail.message || ''}`.slice(0, 300), fatal: e.detail.severity === 2 }));
    p.addEventListener('variantchanged', () => emit('quality', {}));
    // Shaka flattens Periods/discontinuities into one timeline and has no event for them (shaka-project/shaka-player#3159,
    // closed unfixed): no transitions are reported, in either format.
    await p.load(url);
    this.handle = p;
  },
};
