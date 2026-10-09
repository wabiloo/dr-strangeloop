import { loadScript } from '/loadscript.js';

export default {
  id: 'hlsjs',
  formats: ['hls'],
  reports: ['periods', 'errors', 'quality'],
  version: null,
  async load({ video, url, emit }) {
    await loadScript('/vendor/hls.js/dist/hls.min.js');
    if (!Hls.isSupported()) throw new Error('hls.js: MSE not supported');
    this.version = Hls.version;
    // "periods" here = EXT-X-DISCONTINUITY crossings: count changes of the fragment continuity counter.
    const hls = new Hls();
    let lastCc = null;
    hls.on(Hls.Events.ERROR, (_e, d) => emit('error', { message: `${d.type}/${d.details}`, fatal: !!d.fatal }));
    hls.on(Hls.Events.FRAG_CHANGED, (_e, d) => {
      const cc = d.frag.cc;
      if (lastCc !== null && cc !== lastCc) emit('period', {});
      lastCc = cc;
    });
    hls.on(Hls.Events.LEVEL_SWITCHED, () => emit('quality', {}));
    hls.loadSource(url);
    hls.attachMedia(video);
    this.handle = hls;
  },
};
