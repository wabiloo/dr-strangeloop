// Player-independent playback observer. Stalls are defined here, on the <video>
// element, so every player is judged by the same rule.
//   playing  : currentTime advanced since the last sample
//   stalled  : not advancing for >= STALL_AFTER_S while started, not paused/ended
const SAMPLE_MS = 250;
const STALL_AFTER_S = 1.0;

export function createObserver(getVideo, clock) {
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
  };
  const t = () => +(clock() - o.loadedAt).toFixed(2);
  let lastTraceAt = -1;

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
      trace: o.trace,
    };
  };
  const first = getVideo();
  if (first) first.addEventListener('seeking', () => { o.seeks++; });
  return o;
}
