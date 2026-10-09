import { loadScript } from '../loadscript.js';

export default {
  id: 'videojs',
  formats: ['hls', 'dash'],
  reports: ['errors', 'periods'],
  version: null,
  async load({ video, url, format, emit }) {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = './vendor/video.js/dist/video-js.min.css';
    document.head.appendChild(css);
    await loadScript('./vendor/video.js/dist/video.min.js');
    this.version = videojs.VERSION;
    video.classList.add('video-js');
    const p = videojs(video, { muted: true, autoplay: true, controls: false, html5: { vhs: { overrideNative: true } } });
    p.on('error', () => { const e = p.error(); emit('error', { message: `${e && e.code} ${e && e.message || ''}`.slice(0, 300), fatal: true }); });
    // VHS fires 'timelinechange' when a segment on a new timeline is appended: a discontinuity (HLS)
    // or a Period (DASH). The first segment (from = -1) and the audio loader's twin event are not transitions.
    const seen = new Set();
    let hooked = false;
    p.on('loadstart', () => {
      const vhs = p.tech({ IWillNotUseThisInPlugins: true }).vhs;
      const ctl = vhs && (vhs.playlistController_ || vhs.masterPlaylistController_);
      if (hooked || !ctl) return;
      hooked = true;
      ctl.timelineChangeController_.on('timelinechange', (ev) => {
        const t = ev && ev.metadata && ev.metadata.timelineChangeInfo;
        if (!t || t.from < 0 || t.to === t.from || seen.has(t.to)) return;
        seen.add(t.to);
        emit('period', { id: String(t.to) });
      });
    });
    p.src({ src: url, type: format === 'hls' ? 'application/x-mpegURL' : 'application/dash+xml' });
    this.handle = p;
  },
};
