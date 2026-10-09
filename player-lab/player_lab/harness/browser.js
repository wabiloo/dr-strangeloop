// In-browser driver: plays every case in a same-origin iframe (harness page), tracks
// boundaries from /timeline.json and POSTs the snapshots to Igor, which judges them
// with the same Python code as the headless runner.
// Query: channel= cases=player:fmt,... hls= dash= timeline= post= boundaries= duration= max= settle= grace=
// Opened by Igor in its own tab; reports progress/result to window.opener.
const LOGO_EXT = { dashjs: 'png', videojs: 'png', bitmovin: 'png', shaka: 'png' };
const LOGO_IS_MARK = new Set(['shaka']);
const q = new URLSearchParams(location.search);
const num = (k, d) => (q.has(k) ? Number(q.get(k)) : d);
const ORDER = ['hlsjs', 'dashjs', 'bitmovin', 'shaka', 'videojs'];
const FORMATS = { hlsjs: ['hls'], dashjs: ['dash'], bitmovin: ['hls', 'dash'], shaka: ['hls', 'dash'], videojs: ['hls', 'dash'] };
const rank = (p) => (ORDER.includes(p) ? ORDER.indexOf(p) : ORDER.length);
const cases = (q.get('cases') || '').split(',').filter(Boolean).map((c) => c.split(':')).sort((a, b) => rank(a[0]) - rank(b[0]));
const urls = { hls: q.get('hls'), dash: q.get('dash') };
const skipped = (q.get('skipped') || '').split(',').filter(Boolean);
const timelineUrl = q.get('timeline');
const postUrl = q.get('post');
const channel = q.get('channel') || '';
const want = num('boundaries', 2);
const durationS = num('duration', 120);
const maxS = num('max', 600);
const settleS = num('settle', 40);
const graceS = num('grace', 40);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tell = (msg) => { try { if (window.opener) window.opener.postMessage({ source: 'player-lab', ...msg }, location.origin); } catch { /* opener gone */ } };
const PLAYER_LABEL = { dashjs: 'dash.js', shaka: 'Shaka', hlsjs: 'hls.js', videojs: 'Video.js', bitmovin: 'Bitmovin' };

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const pill = (labelCls, label) => {
  const p = el('span', 'stat-pill');
  p.append(el('span', `stat-pill-label ${labelCls}`, label));
  const v = el('span', 'stat-pill-value', '–');
  p.append(v);
  document.getElementById('stats').append(p);
  return v;
};
const pillChannel = pill('l-channel', 'Channel');
const pillElapsed = pill('l-elapsed', 'Elapsed');
const pillBound = pill('l-bound', 'Boundaries');
const pillStatus = pill('l-status', 'Status');
pillChannel.textContent = channel || '–';
const dot = document.getElementById('dot');
const banner = document.getElementById('banner');
const setBanner = (text, kind) => { banner.className = `banner ${kind || ''}`; banner.textContent = text; };
setBanner('Keep this tab in the foreground while the test runs: browsers throttle background tabs, which would show up as stalls. Closing it discards the run.');
document.title = `Playback test${channel ? ` · ${channel}` : ''}`;

const chip = (icon, label, fixed) => {
  const c = el('span', 'playhead-chip');
  c.append(el('i', null, icon), el('span', 'playback-meta-label', label));
  const v = el('span', 'playback-meta-value', '–');
  c.append(v);
  return { root: c, set(text, level) { v.textContent = text; c.className = `playhead-chip ${fixed || ''} ${level || ''}`; } };
};
const panelGrids = {};
const copyUrl = async (btn, text) => {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  btn.classList.add('done');
  btn.textContent = '✓';
  setTimeout(() => { btn.classList.remove('done'); btn.textContent = '⧉'; }, 1200);
};
['hls', 'dash'].filter((fmt) => cases.some((c) => c[1] === fmt)).forEach((fmt) => {
  const panel = el('div', `format-panel ${fmt}`);
  const head = el('div', 'format-head');
  head.append(el('h4', null, fmt.toUpperCase()));
  const row = el('div', 'url-row mono');
  const copy = el('button', 'copy', '⧉');
  copy.type = 'button';
  copy.title = 'Copy URL';
  copy.addEventListener('click', () => copyUrl(copy, urls[fmt]));
  row.append(el('span', 'u', urls[fmt]), copy);
  const grid = el('div', 'players-grid');
  head.append(row);
  panel.append(head, grid);
  document.getElementById('panels').append(panel);
  panelGrids[fmt] = grid;
});
const playerTitle = (player) => {
  const title = el('span');
  const logo = el('img', 'logo');
  logo.src = `./logos/${player}.${LOGO_EXT[player] || 'svg'}`;
  logo.alt = PLAYER_LABEL[player] || player;
  title.append(logo);
  if (LOGO_IS_MARK.has(player)) title.append(el('strong', null, PLAYER_LABEL[player] || player));
  return title;
};
// Players that cannot run (no licence key) still get a greyed-out card, in their normal place.
const entries = [
  ...cases.map(([player, fmt]) => ({ player, fmt, active: true })),
  ...skipped.flatMap((player) => (FORMATS[player] || []).filter((fmt) => panelGrids[fmt]).map((fmt) => ({ player, fmt, active: false }))),
].sort((a, b) => rank(a.player) - rank(b.player));
const cards = [];
entries.forEach(({ player, fmt, active }) => {
  if (!active) {
    const off = el('div', 'player-card disabled');
    const h = el('div', 'player-card-header');
    h.append(playerTitle(player), el('span', 'verdict skip', 'no licence key'));
    off.append(h);
    panelGrids[fmt].append(off);
    return;
  }
  const card = el('div', 'player-card');
  const head = el('div', 'player-card-header');
  const title = playerTitle(player);
  const verdict = el('span', 'verdict run', 'running');
  head.append(title, verdict);
  const wrap = el('div', 'video-wrapper');
  const f = document.createElement('iframe');
  f.allow = 'autoplay';
  f.title = `${player} ${fmt}`;
  f.src = `./index.html?${new URLSearchParams({ player, format: fmt, url: urls[fmt], embed: '1' })}`;
  wrap.append(f);
  const meta = el('div', 'playback-meta-panel');
  const chips = {
    startup: chip('⏱', 'Startup'),
    stalls: chip('⏸', 'Stalls', 'orange'),
    stalled: chip('∑', 'Stalled', 'orange'),
    errors: chip('!', 'Errors', 'orange'),
    seen: chip('⇄', fmt === 'hls' ? 'Discontinuities' : 'Periods'),
    playhead: chip('▶', 'Playhead'),
    buffer: chip('▤', 'Buffer'),
  };
  const left = el('div', 'meta-col');
  const right = el('div', 'meta-col');
  [chips.startup, chips.playhead, chips.buffer, chips.seen].forEach((c) => left.append(c.root));
  [chips.stalls, chips.stalled, chips.errors].forEach((c) => right.append(c.root));
  meta.append(left, right);
  const details = el('div', 'failures');
  card.append(head, wrap, meta, details);
  panelGrids[fmt].append(card);
  cards.push({ card, verdict, frame: f, wrap, chips, details });
});
const frames = cards.map((c) => c.frame);
const snaps = () => frames.map((f) => (f.contentWindow && f.contentWindow.__lab && f.contentWindow.__lab.snapshot()) || {});

const fmtS = (v, d = 1) => (v == null ? '–' : `${Number(v).toFixed(d)} s`);
function renderCards(s) {
  s.forEach((x, i) => {
    const c = cards[i].chips;
    c.startup.set(x.startedAfterS == null ? 'waiting' : fmtS(x.startedAfterS), x.startedAfterS == null ? 'warn' : 'good');
    c.stalls.set(String(x.stallCount || 0), x.stallCount ? 'hot' : '');
    c.stalled.set(fmtS(x.stallSeconds || 0), x.stallSeconds ? 'hot' : '');
    c.errors.set(String(x.fatalErrors || 0), x.fatalErrors ? 'hot' : '');
    c.seen.set(x.periodTransitions == null ? 'n/a' : String(x.periodTransitions));
    c.playhead.set(fmtS(x.playhead || 0));
    c.buffer.set(x.maxBufferAheadS == null ? '–' : fmtS(x.maxBufferAheadS));
  });
}
function renderResult(report) {
  cards.forEach((c, i) => {
    const r = report.cases[i];
    if (!r) return;
    c.verdict.className = `verdict ${r.pass ? 'pass' : 'fail'}`;
    c.verdict.textContent = r.pass ? 'pass' : 'fail';
    c.card.classList.add(r.pass ? 'pass' : 'fail');
    c.frame.remove();
    c.wrap.append(el('div', 'gone', 'stopped'));
    (r.failures || []).forEach((t) => c.details.append(el('div', null, `✗ ${t}`)));
    (r.warnings || []).forEach((t) => c.details.append(el('div', 'w', `! ${t}`)));
  });
}
const updateHeader = (now, marked, crossedNow, label) => {
  pillElapsed.textContent = `${Math.round(now)} s`;
  pillBound.textContent = timelineUrl && marked !== null
    ? Object.entries(crossedNow).map(([f, n]) => `${f.toUpperCase()} ${n}/${want}`).join('  ')
    : (timelineUrl ? 'waiting for players' : 'n/a');
  if (label) pillStatus.textContent = label;
};
const finish = (kind, label) => { dot.className = `live-dot ${kind}`; pillStatus.textContent = label; };

const periods = new Set();
const discs = new Set();
let basePeriods = null;
let baseDiscs = null;
let polls = 0;
let pollErrors = 0;
async function pollTimeline() {
  try {
    const r = await fetch(timelineUrl, { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    const doc = await r.json();
    polls++;
    (doc.periods || []).forEach((p) => periods.add(p.id));
    (doc.discontinuities || []).forEach((d) => discs.add(d.segment));
  } catch {
    pollErrors++;
  }
}
const countNew = (all, base) => [...all].filter((x) => !base.has(x)).length;
const crossed = (fmt) => (fmt === 'hls' ? countNew(discs, baseDiscs) : countNew(periods, basePeriods));

async function main() {
  const startedAt = new Date().toISOString();
  const t0 = performance.now();
  const formatsRun = [...new Set(cases.map((c) => c[1]))];
  let lastPoll = -99;
  let marked = null;
  let reached = null;
  for (;;) {
    await sleep(1000);
    const now = (performance.now() - t0) / 1000;
    if (timelineUrl && now - lastPoll >= 5) {
      lastPoll = now;
      await pollTimeline();
    }
    const s = snaps();
    if (marked === null) {
      if (s.every((x) => x.startedAfterS != null) || now > graceS) {
        if (timelineUrl && polls === 0) continue;
        basePeriods = new Set(periods);
        baseDiscs = new Set(discs);
        marked = now;
      }
    } else if (timelineUrl) {
      const c = Object.fromEntries(formatsRun.map((f) => [f, crossed(f)]));
      if (reached === null && Object.values(c).every((n) => n >= want)) reached = now;
      if (reached !== null) {
        const since = now - reached;
        const reporting = s.filter((x) => x.periodTransitions != null);
        const caughtUp = reporting.length > 0 && reporting.every((x) => x.periodTransitions >= want);
        const silent = reporting.length < s.length;
        if (since >= settleS || (caughtUp && !silent && since >= 3)) break;
      }
    } else if (now >= durationS) {
      break;
    }
    if (now >= maxS) break;
    renderCards(s);
    const crossedNow = marked !== null && timelineUrl ? Object.fromEntries(formatsRun.map((f) => [f, crossed(f)])) : {};
    const label = marked === null ? 'starting players' : timelineUrl ? 'playing until boundaries pass' : `playing for ${durationS} s`;
    updateHeader(now, marked, crossedNow, label);
    tell({ type: 'progress', text: `t=${Math.round(now)}s ${label}` });
  }
  const final = snaps();
  const started = marked !== null && timelineUrl;
  const crossedBy = started ? Object.fromEntries(formatsRun.map((f) => [f, crossed(f)])) : null;
  renderCards(final);
  finish('idle', 'judging');
  tell({ type: 'progress', text: 'judging...' });
  const res = await fetch(postUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      startedAt,
      durationS: +((performance.now() - t0) / 1000).toFixed(1),
      userAgent: navigator.userAgent,
      target: { hls: urls.hls, dash: urls.dash, timeline: timelineUrl },
      boundaries: {
        requested: want,
        source: timelineUrl ? 'timeline.json' : 'none (fixed duration)',
        newPeriods: started ? countNew(periods, basePeriods) : null,
        newDiscontinuities: started ? countNew(discs, baseDiscs) : null,
        timelinePolls: polls,
        timelinePollErrors: pollErrors,
        crossed: crossedBy,
      },
      cases: cases.map(([player, format], i) => ({ player, format, snapshot: final[i] })),
    }),
  });
  if (!res.ok) throw new Error(`Igor refused the results: ${res.status} ${await res.text()}`);
  const out = await res.json();
  renderResult(out);
  finish(out.pass ? 'ok' : 'bad', out.pass ? 'pass' : 'fail');
  setBanner(`${out.pass ? 'All cases passed.' : 'Some cases failed.'} The report is saved in Igor (run ${out.run_id}); you can close this tab and look at it in the Playback test panel.`, out.pass ? 'ok' : 'bad');
  tell({ type: 'done', runId: out.run_id, pass: out.pass });
}

main().catch((e) => {
  const msg = String((e && e.message) || e);
  frames.forEach((f) => f.remove());
  finish('bad', 'failed');
  setBanner(`The test failed: ${msg}`, 'bad');
  tell({ type: 'error', message: msg });
});
