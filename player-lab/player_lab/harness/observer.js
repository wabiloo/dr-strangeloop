// Player-independent playback observer. Stalls are defined here, on the <video>
// element, so every player is judged by the same rule.
//   playing  : currentTime advanced since the last sample
//   stalled  : not advancing for >= STALL_AFTER_S while started, not paused/ended
//   live latency (optional): once a second after startup, `liveProbe()` (the adapter's `live()`)
//     is asked for { playheadDateMs, latencyS, targetS }. The primary number is wall-clock based --
//     Date.now() minus the wall-clock time of the playhead -- so every player is measured the same
//     way (it needs this machine's clock to be right); the player's own latency and target are kept
//     alongside. Summaries and thresholds are applied in Python (profile.summarize_live).
const SAMPLE_MS = 250;
const STALL_AFTER_S = 1.0;

export function createObserver(getVideo, clock, liveProbe) {
  const o = {
    loadedAt: clock(),
    startedAtS: null,
    playBase: null,
    stalls: [],
    seeks: 0,
    trace: [],
    maxBufferAhead: 0,
    droppedFrames: 0,
    totalFrames: 0,
    lastTime: -1,
    lastAdvanceAt: clock(),
    stall: null,
    latencyTrace: [],  // [tS, wallClockLatencyS|null, playerLatencyS|null, targetS|null, playbackRate]
    rate: { min: 1, max: 1, offNominalS: 0, changes: 0, last: 1 },
  };
  const t = () => +(clock() - o.loadedAt).toFixed(2);
  let lastTraceAt = -1;
  let lastProbeAt = -1;
  const num = (x) => (typeof x === 'number' && isFinite(x) ? +x.toFixed(3) : null);

  function probeLive(video, now) {
    let p = null;
    try { p = liveProbe(); } catch (e) { p = null; }
    if (!p) return;
    const wall = p.playheadDateMs ? num((Date.now() - p.playheadDateMs) / 1000) : null;
    const own = num(p.latencyS);
    if (wall === null && own === null) return;
    if (o.latencyTrace.length < 3600) {
      o.latencyTrace.push([Math.round(now), wall, own, num(p.targetS), +video.playbackRate.toFixed(3)]);
    }
  }

  function sample() {
    const video = getVideo();
    if (!video) return;
    const now = t();
    const ct = video.currentTime;
    const advanced = ct > o.lastTime + 0.01;
    if (advanced) {
      o.lastTime = ct;
      o.lastAdvanceAt = now;
      if (o.startedAtS === null && video.readyState >= 3 && !video.paused) {
        if (o.playBase === null) o.playBase = ct;
        else if (ct - o.playBase >= 0.3) o.startedAtS = now;
      }
      if (o.stall) {
        o.stall.durS = +(now - o.stall.atS).toFixed(2);
        o.stalls.push(o.stall);
        o.stall = null;
      }
    } else if (o.startedAtS !== null && !video.paused && !video.ended && !o.stall && now - o.lastAdvanceAt >= STALL_AFTER_S) {
      o.stall = { atS: o.lastAdvanceAt, durS: 0, playhead: +ct.toFixed(2) };
    }
    const b = video.buffered;
    const ahead = b.length ? b.end(b.length - 1) - ct : 0;
    if (ahead > o.maxBufferAhead) o.maxBufferAhead = ahead;
    if (o.startedAtS !== null) {
      const rate = video.playbackRate;
      if (Math.abs(rate - 1) > 0.005) o.rate.offNominalS += SAMPLE_MS / 1000;
      if (Math.abs(rate - o.rate.last) > 0.005) { o.rate.changes++; o.rate.last = rate; }
      o.rate.min = Math.min(o.rate.min, rate);
      o.rate.max = Math.max(o.rate.max, rate);
      if (liveProbe && now - lastProbeAt >= 1) { lastProbeAt = now; probeLive(video, now); }
    }
    const q = video.getVideoPlaybackQuality && video.getVideoPlaybackQuality();
    if (q) { o.droppedFrames = q.droppedVideoFrames; o.totalFrames = q.totalVideoFrames; }
    if (now - lastTraceAt >= 1) {
      lastTraceAt = now;
      o.trace.push([Math.round(now), +ct.toFixed(1), +ahead.toFixed(1)]);
    }
  }
  const timer = setInterval(sample, SAMPLE_MS);

  o.stop = () => clearInterval(timer);
  o.snapshot = () => {
    const open = o.stall ? [{ ...o.stall, durS: +(t() - o.stall.atS).toFixed(2), open: true }] : [];
    const stalls = [...o.stalls, ...open];
    return {
      elapsedS: t(),
      startedAfterS: o.startedAtS,
      playhead: o.lastTime < 0 ? 0 : +o.lastTime.toFixed(2),
      stallCount: stalls.length,
      stallSeconds: +stalls.reduce((a, s) => a + s.durS, 0).toFixed(2),
      longestStallS: stalls.reduce((a, s) => Math.max(a, s.durS), 0),
      stalls,
      seeks: o.seeks,
      maxBufferAheadS: +o.maxBufferAhead.toFixed(1),
      droppedFrames: o.droppedFrames,
      totalFrames: o.totalFrames,
      stalledNow: !!o.stall,
      playbackRate: { min: +o.rate.min.toFixed(3), max: +o.rate.max.toFixed(3), offNominalS: +o.rate.offNominalS.toFixed(2), changes: o.rate.changes },
      latencyTrace: o.latencyTrace,
      trace: o.trace,
    };
  };
  const first = getVideo();
  if (first) first.addEventListener('seeking', () => { o.seeks++; });
  return o;
}
