import { loadScript } from '../loadscript.js';

export default {
  id: 'bitmovin',
  formats: ['hls', 'dash'],
  reports: ['errors', 'quality', 'periods'],
  version: null,
  async load({ video, url, format, emit }) {
    const key = (await (await fetch('./keys.json', { cache: 'no-store' })).json()).bitmovin;
    if (!key) throw new Error('no Bitmovin licence key (BITMOVIN_LICENSE_KEY or ~/.dr-strangeloop/config.toml [keys])');
    await loadScript('./vendor/bitmovin-player/bitmovinplayer.js');
    const { Player, PlayerEvent } = bitmovin.player;
    this.version = Player.version;
    // Bitmovin builds its own <video>; replace the harness one so the observer finds it.
    const box = document.createElement('div');
    box.id = 'bm';
    video.replaceWith(box);
    const p = new Player(box, { key, ui: false, playback: { muted: true, autoplay: true }, analytics: false });
    const fail = (e) => emit('error', { message: `${e.code} ${e.name || ''} ${e.message || ''}`.slice(0, 300), fatal: true });
    p.on(PlayerEvent.Error, fail);
    p.on(PlayerEvent.Warning, () => {});
    // PeriodSwitched did not fire across boundaries; SegmentPlayback carries the period id (DASH) or the
    // discontinuity sequence number (HLS) of the segment being played: a change is a transition.
    let current;
    p.on(PlayerEvent.SegmentPlayback, (e) => {
      if (!/video/.test(e.mimeType || '')) return;
      const id = format === 'dash' ? e.periodId : e.discontinuitySequenceNumber;
      if (id == null) return;
      if (current !== undefined && id !== current) emit('period', { id: String(id) });
      current = id;
    });
    p.on(PlayerEvent.VideoPlaybackQualityChanged, () => emit('quality', {}));
    await p.load({ [format]: url });
    this.handle = p;
  },
};
