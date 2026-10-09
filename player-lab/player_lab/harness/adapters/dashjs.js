import { loadScript } from '/loadscript.js';

export default {
  id: 'dashjs',
  formats: ['dash'],
  reports: ['periods', 'errors', 'quality'],
  version: null,
  async load({ video, url, emit }) {
    await loadScript('/vendor/dashjs/dist/modern/umd/dash.all.min.js');
    this.version = dashjs.Version;
    const p = dashjs.MediaPlayer().create();
    const E = dashjs.MediaPlayer.events;
    let first = true;
    p.on(E.ERROR, (e) => emit('error', { message: JSON.stringify(e.error).slice(0, 300), fatal: true }));
    p.on(E.PERIOD_SWITCH_COMPLETED, () => { if (first) first = false; else emit('period', {}); });
    p.on(E.QUALITY_CHANGE_RENDERED, () => emit('quality', {}));
    p.initialize(video, url, true);
    this.handle = p;
  },
};
