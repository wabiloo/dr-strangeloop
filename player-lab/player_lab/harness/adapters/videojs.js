import { loadScript } from '/loadscript.js';

export default {
  id: 'videojs',
  formats: ['hls', 'dash'],
  reports: ['errors'],
  version: null,
  async load({ video, url, format, emit }) {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/vendor/video.js/dist/video-js.min.css';
    document.head.appendChild(css);
    await loadScript('/vendor/video.js/dist/video.min.js');
    this.version = videojs.VERSION;
    video.classList.add('video-js');
    const p = videojs(video, { muted: true, autoplay: true, controls: false, html5: { vhs: { overrideNative: true } } });
    p.on('error', () => { const e = p.error(); emit('error', { message: `${e && e.code} ${e && e.message || ''}`.slice(0, 300), fatal: true }); });
    p.src({ src: url, type: format === 'hls' ? 'application/x-mpegURL' : 'application/dash+xml' });
    this.handle = p;
  },
};
