
// =====================================================================
// live sessions, the session detail, and long lists
// =====================================================================
// The city shows live sessions (not ended, heard from in the last few minutes) unless you ask for the past
// ones too; one that needs you, or whose detail is open, always shows. A session's detail is the whole
// session step by step: live it comes from GET /api/session (everything the server stored), a recording or
// the preview build it from the session's own document. Anything in it that could name a file, a command,
// a prompt or an MCP server goes through safeStep() or a prefs.safe check: Safe to film hides them all.
// This file only declares: it runs nothing at load, so it never touches 06-ui's constants too early.

const LIVE_WINDOWS = [15, 30, 60, 180];   // Settings → Live window, in minutes
const SLOW_MS = 120_000;                   // a working session with no news for this long is flagged Slow
const BUSY_MAX_MS = 3 * 3600_000;          // a bot mid-step stays awake and live this long without news (the longest Live window)
const TL_GAP_MS = 120_000;                 // the timeline folds quiet stretches longer than this…
const TL_BREAK_PX = 16;                    // …into a break this wide
const TL_MIN_W = 2, TL_ROW_H = 16, TL_AXIS_H = 16, TL_LABEL_W = 92;  // TL_LABEL_W: the names column, until it's measured
const STEP_ROW_H = 26, LOG_ROW_H = 42;
const TOOL_KINDS = new Set(['bash', 'edit', 'write', 'read', 'search', 'web', 'mcp', 'tool', 'task']);
const FAILED = new Set(['failed', 'interrupted', 'denied', 'refused']);
// the timeline's bars and the log's kind dots share these
const KIND_COLOR = { bash: '#3a6aa6', edit: '#2b7a40', write: '#4f9a5c', read: '#8e939c', search: '#7a5aa6', web: '#2f8a8a',
  spawn: '#5a5fa6', task: '#a08a3a', mcp: '#a6537a', tool: '#56607a', think: '#b9bbc0', wait: '#e8a54b', error: '#c0392f',
  prompt: '#1b1d22', answer: '#3a6aa6', done: '#3a6aa6', join: '#8e939c', leave: '#8e939c', idle: '#b9bbc0' };
const LOG_KINDS = [['all', 'All'], ['prompt', 'Prompts'], ['tool', 'Tools'], ['error', 'Errors'], ['answer', 'Replies'], ['helper', 'Helpers']];
function logKindMatch(lk, f) {
  if (lk === 'all') return true;
  if (lk === 'tool') return TOOL_KINDS.has(f.kind);
  if (lk === 'helper') return f.kind === 'spawn' || f.kind === 'done' || (f.kind === 'join' && f.agentId && f.agentId !== 'main');
  return f.kind === lk;
}

// ---------------- live or past ----------------
// a long command (a video export, a build) or a long think sends nothing until it ends: a bot mid-step is busy, not
// asleep, until BUSY_MAX_MS of silence says the session is gone (killed without a goodbye)
function isBusy(doc, now) {
  return !!doc && !doc.ended && now - (Number(doc.updatedAt) || 0) < BUSY_MAX_MS
    && (doc.agents || []).some((a) => a.status === 'working');
}
function isLive(doc, now, windowMs) { return !!doc && !doc.ended && (now - (Number(doc.updatedAt) || 0) < windowMs || isBusy(doc, now)); }
// which sessions the city shows: `all` is [{id, doc}]; returns the shown ones and how many are hidden
function visibleDocs(all, { now, windowMs, showPast, needs, pinned }) {
  const shown = [];
  let hidden = 0;
  for (const e of all) {
    if (showPast || isLive(e.doc, now, windowMs) || needs.has(e.id) || e.id === pinned) shown.push(e); else hidden++;
  }
  return { shown, hidden };
}
// a working, not waiting, live session with no news for SLOW_MS: what it's on, and for how long
function slowOf(d, now) {
  const doc = d.doc;
  if (!doc || d.isSample || city.needs.has(d.id) || !isLive(doc, now, prefs.liveMin * 60_000)) return null;
  const quiet = now - (doc.updatedAt || 0);
  if (quiet < SLOW_MS) return null;
  const busy = doc.agents.filter((a) => a.status === 'working' && !a.waiting);
  if (!busy.length) return null;
  // a tool call with no duration yet is still running: say which, and since when
  for (const a of busy) {
    const call = doc.feed.find((f) => f.toolUseId && f.agentId === a.id && f.ts === a.activitySince && f.kind !== 'error');
    if (call && call.durationMs == null && a.tool) return { text: (prefs.safe ? KIND_WORD[a.kind] || 'Tool' : safeTool(a.tool)) + ' for ' + fmtMin(now - a.activitySince), ms: now - a.activitySince };
  }
  return { text: 'No news for ' + fmtMin(quiet), ms: quiet };
}
const KIND_WORD = { bash: 'Command', edit: 'Edit', write: 'Write', read: 'Read', search: 'Search', web: 'Web', spawn: 'Helper', task: 'Task list', mcp: 'App', tool: 'Tool', think: 'Thinking' };

// ---------------- small formatting ----------------
function fmtDur(ms) {
  ms = Math.max(0, Math.round(Number(ms) || 0));
  if (ms < 1000) return ms + ' ms';
  const s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + ' s';
  const m = Math.floor(s / 60), ss = Math.floor(s % 60);
  if (m < 60) return `${m}m ${String(ss).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
// whole minutes, so a line that shows it changes once a minute, not every second (a rebuilt row loses its focus)
function fmtMin(ms) { const m = Math.floor(ms / 60_000); return m < 60 ? m + 'm' : Math.floor(m / 60) + 'h ' + (m % 60) + 'm'; }
function fmtClock(ms) { // a countdown: 7:55, or 1:02:03
  const s = Math.max(0, Math.ceil(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(r).padStart(2, '0');
}
function timeStr(ts) { return new Date(ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }); }
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
// an MCP tool's name carries its server's name, often a project or company: "Tool" in Safe to film
function safeTool(tool) { return prefs.safe && /^mcp__/.test(tool || '') ? 'Tool' : tool || 'Tool'; }
// a step as Safe to film allows: the tool and one line about it
function safeStep(s) { return { tool: safeTool(s.tool), text: prefs.safe ? SAFE_TEXT[s.kind] || 'Working' : s.text }; }
// "Lead", or a helper's agent type ("Explore"): roles come first in labels, "Explore · Pip"
function roleOf(d, id) {
  if (!id || id === 'main') return 'Lead';
  const a = (detail.id === d.id && detail.data?.agents?.find((x) => x.id === id)) || (city.allDocs.get(d.id)?.agents || []).find((x) => x.id === id)
    || (d.doc?.agents || []).find((x) => x.id === id);
  return cap(a?.type && a.type !== 'lead' ? a.type : '') || 'Helper';
}
function nameOf(d, id) {
  const b = d.robots.get(id || 'main');
  return b && !b.leaving ? b.displayName() : nickFor(d.id + ':' + (id || 'main'));
}
const whoOf = (d, id) => roleOf(d, id) + ' · ' + nameOf(d, id);

// ---------------- the timeline's layout ----------------
// x is proportional to time, but a quiet stretch longer than TL_GAP_MS folds into a TL_BREAK_PX break, so a
// session with two busy hours a day apart still shows both. One row per agent: `agents` order first (the
// lead, then helpers as they started), then any other agent a step names. A step still running reaches `now`.
function layoutTimeline(steps, agents, width, now) {
  const rows = agents.map((a) => a.id);
  for (const s of steps) if (!rows.includes(s.agent)) rows.push(s.agent);
  const spans = steps.map((s) => [s.start, Math.max(s.start, s.end == null ? now : s.end)]);
  const order = spans.map((_, i) => i).sort((a, b) => spans[a][0] - spans[b][0]);
  const parts = [];  // busy stretches, [start, end]
  for (const i of order) {
    const [a, b] = spans[i], p = parts[parts.length - 1];
    if (p && a - p[1] <= TL_GAP_MS) p[1] = Math.max(p[1], b); else parts.push([a, b]);
  }
  const breaks = Math.max(0, parts.length - 1);
  const breakPx = breaks ? Math.min(TL_BREAK_PX, (width * 0.4) / breaks) : 0;
  const len = (p) => Math.max(1, p[1] - p[0]);
  const busy = parts.reduce((n, p) => n + len(p), 0);
  const scale = parts.length ? (width - breaks * breakPx) / busy : 0;
  const offs = [];
  let x = 0;
  for (const p of parts) { offs.push(x); x += len(p) * scale + breakPx; }
  const at = (t) => { let k = parts.length - 1; while (k > 0 && t < parts[k][0]) k--; return offs[k] + clamp(t - parts[k][0], 0, len(parts[k])) * scale; };
  const bars = steps.map((s, i) => {
    const x0 = at(spans[i][0]), w = Math.max(TL_MIN_W, at(spans[i][1]) - x0);
    return { i, row: rows.indexOf(s.agent), x: Math.max(0, Math.min(x0, width - w)), w };
  });
  const gaps = parts.slice(1).map((p, k) => ({ x: offs[k + 1] - breakPx, w: breakPx, ms: p[0] - parts[k][1] }));
  const marks = parts.map((p, k) => ({ x: offs[k], t: p[0] }));
  return { rows, bars, gaps, marks, width };
}

// ---------------- a long list that only builds the rows in view ----------------
// `host` holds one box n rows tall; `scroller` is whatever scrolls it (the host itself, or the console).
function drawVList(host, scroller, n, rowH, rowHtml) {
  if (!host.firstElementChild?.classList.contains('vl-in')) setHTML(host, '<div class="vl-in"></div>');
  const inner = host.firstElementChild;
  inner.style.height = n * rowH + 'px';
  const top = scroller === host ? host.scrollTop
    : scroller.scrollTop - (host.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop);
  const first = clamp(Math.floor(top / rowH) - 10, 0, n), last = clamp(Math.ceil((top + scroller.clientHeight) / rowH) + 10, 0, n);
  let h = '';
  for (let i = first; i < last; i++) h += rowHtml(i, `top:${i * rowH}px;height:${rowH}px`);
  setHTML(inner, h);
}
function scrollRowIntoView(scroller, host, i, rowH) {
  const hostTop = scroller === host ? 0 : host.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  const y = hostTop + i * rowH;
  if (y < scroller.scrollTop) scroller.scrollTop = y;
  else if (y + rowH > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = y + rowH - scroller.clientHeight;
}

// ---------------- the session detail ----------------
// id: the session shown; bot: the agent it's filtered to; sel / open: the step picked / opened in full;
// back: the tab to go back to (Sessions, or Logs when a log line opened it)
const detail = { id: null, bot: null, data: null, loading: false, error: '', fetchedAt: 0, docAt: 0, sel: null, open: null,
  stepCache: new Map(), more: new Set(), drawKey: '', lay: null, back: 'city' };
function openDetail(d, botId = null, stepId = null) {
  if (!d) return;
  if (!detail.id) detail.back = tab === 'log' ? 'log' : 'city';
  if (detail.id !== d.id) {
    Object.assign(detail, { id: d.id, data: null, loading: false, error: '', fetchedAt: 0, docAt: 0, sel: null, open: null, drawKey: '', lay: null });
    detail.stepCache = new Map(); detail.more = new Set();
    $('dtSteps').scrollTop = 0;
  }
  detail.bot = botId; detail.drawKey = '';
  if (stepId) { detail.sel = stepId; detail.open = stepId; loadStep(stepId); }
  if (city.renaming) cancelRename();
  if (tab !== 'city') setTab('city');
  $('sessDetail').hidden = false; $('sessMain').hidden = true;
  showDocs(); // its session stays in the city while its detail is open
  uiDirty = true;
}
function closeDetail(note) {
  if (!detail.id) return;
  const id = detail.id;
  detail.id = null; detail.data = null; detail.open = null; detail.bot = null; detail.sel = null;
  $('sessDetail').hidden = true; $('sessMain').hidden = false;
  if (note) toast(note);
  showDocs();
  if (detail.back === 'log') setTab('log');
  uiDirty = true;
  renderUI();
  if (detail.back === 'log') $('logList').focus({ preventScroll: true });
  else document.querySelector(`#sessList [data-d="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
  detail.back = 'city';
}
async function loadDetail() {
  const id = detail.id;
  if (!id || !source?.detail) return;
  detail.loading = true;
  try {
    const data = await source.detail(id);
    if (detail.id !== id) return;
    detail.data = data; detail.error = data ? '' : 'Nothing is stored for this session yet.';
  } catch (e) {
    if (detail.id !== id) return;
    detail.error = e?.code === 403 ? RESTARTED : e?.code === 404 ? 'Nothing is stored for this session yet.' : "Couldn't load this session. Try again.";
  } finally {
    if (detail.id === id) { detail.loading = false; detail.fetchedAt = Date.now(); detail.drawKey = ''; uiDirty = true; if (detail.open) loadStep(detail.open); }
  }
}
// the detail from a session's document alone (a recording, the preview): the feed is what it has
function detailFromDoc(id, raw) {
  if (!raw) return null;
  const feed = (raw.feed || []).slice().sort((a, b) => a.ts - b.ts), endAt = raw.ended?.at ?? null;
  const failed = new Set(feed.filter((f) => f.kind === 'error' && f.toolUseId).map((f) => f.toolUseId));
  const steps = feed.filter((f) => f.toolUseId && f.kind !== 'error').map((f) => ({
    id: f.toolUseId, agent: f.agentId || 'main', tool: f.tool || '', kind: f.kind, text: f.text, start: f.ts,
    end: f.durationMs != null ? f.ts + f.durationMs : endAt, ms: f.durationMs ?? null,
    outcome: failed.has(f.toolUseId) ? 'failed' : f.durationMs != null ? 'ok' : null, error: '' }));
  const first = {};
  for (const f of feed) if (f.agentId && first[f.agentId] == null) first[f.agentId] = f.ts;
  const agents = (raw.agents || []).map((a) => ({ id: a.id, name: a.name, role: a.role, type: a.type, parent: a.parent || null, start: first[a.id] ?? raw.startedAt ?? 0, end: null }));
  const conversation = feed.filter((f) => f.kind === 'prompt' || f.kind === 'answer').map((f) => ({ ts: f.ts, who: f.kind === 'prompt' ? 'you' : 'claude', text: f.text }));
  return { id, updatedAt: raw.updatedAt, agents, steps, conversation, files: null, approvals: null };
}

function detailSteps() {
  const steps = detail.data?.steps || [];
  return detail.bot ? steps.filter((s) => s.agent === detail.bot) : steps;
}
function selectStep(id, open) {
  detail.sel = id; if (open) detail.open = id;
  const steps = detailSteps(), i = steps.findIndex((s) => s.id === id);
  if (i >= 0) scrollRowIntoView($('dtSteps'), $('dtSteps'), i, STEP_ROW_H);
  detail.drawKey = ''; uiDirty = true;
  if (open) loadStep(id);
}
// a step is fetched again with each new detail until it's final: finished and its output (or error) stored.
// A failed load is tried again too. Until then the panel keeps what it has.
async function loadStep(id) {
  const d = city.districts.get(detail.id), cache = detail.stepCache, had = cache.get(id);
  if (!d || !source?.step || had?.loading || had?.final || had?.again) return;
  const ended = (detail.data?.steps || []).find((x) => x.id === id)?.end != null;
  if (had) had.again = true; else cache.set(id, { loading: true });
  try {
    const got = (await source.step(d.id, id)) || { none: true };
    got.final = !!got.none || (ended && (got.output !== undefined || (got.error != null && got.error !== '')));
    cache.set(id, got);
  } catch (e) { cache.set(id, { loadError: e?.code === 403 ? RESTARTED : "Couldn't load this step." }); }
  uiDirty = true;
}

function renderDetail() {
  const d = city.districts.get(detail.id);
  if (!d || d.leaving) { closeDetail('That session left the city.'); return; }
  const doc = d.doc || {};
  if (!detail.loading && (doc.updatedAt || 0) > detail.docAt && Date.now() - detail.fetchedAt > 2000) { detail.docAt = doc.updatedAt || 0; loadDetail(); }
  renderDetailHead(d);
  const data = detail.data;
  $('dtNote').hidden = !!data;
  setHTML($('dtNote'), data ? '' : `<div class="empty-note">${esc(detail.error || 'Loading the whole session…')}</div>`);
  $('dtBody').hidden = !data;
  if (!data) return;
  drawTimeline(d, data);
  drawSteps(d);
  if (detail.open && detail.reveal !== detail.open) {  // a step opened from the log: bring it into view once it's drawn
    detail.reveal = detail.open;
    const i = detailSteps().findIndex((s) => s.id === detail.open);
    if (i >= 0) { scrollRowIntoView($('dtSteps'), $('dtSteps'), i, STEP_ROW_H); drawSteps(d); }
  }
  renderStepPanel(d);
  renderTalk(data);
  renderFiles(data);
  renderApprovals(d, data);
  renderAgentTokens(d);
}
function renderDetailHead(d) {
  const doc = d.doc || {}, st = d.status(), now = Date.now();
  const agents = detail.data?.agents || (city.allDocs.get(d.id)?.agents || doc.agents || []);
  const lead = d.lead(), model = modelName(lead?.data.model);
  const slow = slowOf(d, now);
  let h = `<div class="drow"><span class="swatch" style="background:${hexCss(d.hue)}"></span><span class="dname">${esc(d.displayName())}</span>${slow ? '<span class="slow">Slow</span>' : ''}<span class="pill ${st}">${STATUS_WORD[st]}</span></div>`;
  h += `<div class="dmeta">${prefs.safe ? '' : `<span>Folder ${esc(d.title)}</span>`}${doc.startedAt ? `<span>Started ${clockStr(doc.startedAt)}</span>` : ''}<span>${doc.turns || 0} turns</span>${model ? `<span>${esc(model)}</span>` : ''}${doc.ended ? '<span>Ended</span>' : ''}${slow ? `<span class="slow-t">${esc(slow.text)}</span>` : ''}</div>`;
  h += `<div class="filters" role="group" aria-label="Agents"><button type="button" data-agent="" aria-pressed="${!detail.bot}">All</button>`
    + agents.map((a) => `<button type="button" data-agent="${esc(a.id)}" aria-pressed="${detail.bot === a.id}">${esc(whoOf(d, a.id))}</button>`).join('') + '</div>';
  const b = detail.bot ? d.robots.get(detail.bot) : null;
  if (detail.bot) {
    const a = b?.data, raw = agents.find((x) => x.id === detail.bot) || {};
    const bst = b ? b.statusKey() : 'done';
    const usage = a?.usage || raw.usage;
    h += `<div class="bview"><div class="who"><div class="avatar" style="background:${hexCss(b ? b.color : 0xd0d6e6)}"></div><div><h3>${esc(whoOf(d, detail.bot))}</h3><p>${esc(cap(raw.type && raw.type !== 'lead' ? raw.type : 'Lead') + ' agent')}${a?.model ? ' · ' + esc(modelName(a.model)) : ''}</p></div><span class="pill ${bst}" style="margin-left:auto">${STATUS_WORD[bst] || 'Done'}</span></div>`;
    if (a) h += `<div class="now${a.waiting ? ' wait' : ''}"><small>${a.waiting ? 'Needs you' : 'Right now'}${a.activitySince ? ' · Since ' + clockStr(a.activitySince) : ''}</small><div>${esc(b.activityText() || '—')}</div></div>`;
    h += `<div class="kv"><div class="stat"><b>${a ? a.tools || 0 : detailSteps().length}</b><span>Tool calls</span></div><div class="stat doing"><b>${a ? KIND_LABEL[a.kind] || '—' : '—'}</b><span>Doing</span></div><div class="stat"><b>${detail.bot === 'main' ? fmtTokens(d.tokens) : usage ? fmtTokens(usageSum(usage)) : '—'}</b><span>Tokens</span></div></div>`;
    h += `<div class="actions">${b ? `<button type="button" class="chipbtn" data-act="follow" aria-pressed="${selection.follow && selection.bot === b}">Follow</button>` : ''}<button type="button" class="chipbtn" data-act="district">Whole district</button><button type="button" class="chipbtn" data-act="skyline">Back to the skyline</button></div></div>`;
  }
  setHTML($('dtHead'), h);
}

function drawTimeline(d, data) {
  const box = $('dtTl'), cv = $('dtCanvas'), now = Date.now();
  const width = Math.max(120, box.clientWidth - ($('dtRows').offsetWidth || TL_LABEL_W) - 4);
  const running = data.steps.some((s) => s.end == null);
  const key = [detail.id, data.updatedAt, data.steps.length, width, detail.bot, detail.sel, prefs.safe, running ? Math.floor(now / 1000) : 0].join('|');
  if (key === detail.drawKey) return;
  detail.drawKey = key;
  const lay = layoutTimeline(data.steps, data.agents, width, now);
  detail.lay = lay;
  setHTML($('dtRows'), lay.rows.map((id) => `<div style="height:${TL_ROW_H}px"${detail.bot && detail.bot !== id ? ' class="dim"' : ''}>${esc(whoOf(d, id))}</div>`).join(''));
  const h = lay.rows.length * TL_ROW_H + TL_AXIS_H, ratio = Math.min(2, window.devicePixelRatio || 1);
  cv.width = Math.round(width * ratio); cv.height = Math.round(h * ratio);
  cv.style.width = width + 'px'; cv.style.height = h + 'px';
  const g = cv.getContext('2d');
  g.setTransform(ratio, 0, 0, ratio, 0, 0);
  g.clearRect(0, 0, width, h);
  for (let r = 0; r < lay.rows.length; r++) if (r % 2) { g.fillStyle = 'rgba(27,29,34,.035)'; g.fillRect(0, r * TL_ROW_H, width, TL_ROW_H); }
  for (const gap of lay.gaps) {  // a folded quiet stretch: a hatched break
    g.fillStyle = 'rgba(27,29,34,.06)'; g.fillRect(gap.x, 0, gap.w, lay.rows.length * TL_ROW_H);
    g.strokeStyle = 'rgba(27,29,34,.22)'; g.beginPath();
    for (let y = -gap.w; y < lay.rows.length * TL_ROW_H; y += 5) { g.moveTo(gap.x, y + gap.w); g.lineTo(gap.x + gap.w, y); }
    g.stroke();
  }
  for (const bar of lay.bars) {
    const s = data.steps[bar.i];
    g.globalAlpha = (detail.bot && s.agent !== detail.bot ? 0.25 : 1) * (s.end == null ? 0.55 : 1);
    g.fillStyle = FAILED.has(s.outcome) ? KIND_COLOR.error : KIND_COLOR[s.kind] || KIND_COLOR.tool;
    g.fillRect(bar.x, bar.row * TL_ROW_H + 3, bar.w, TL_ROW_H - 6);
    if (s.id === detail.sel) { g.globalAlpha = 1; g.strokeStyle = '#1b1d22'; g.lineWidth = 1.5; g.strokeRect(bar.x - 1.5, bar.row * TL_ROW_H + 1.5, bar.w + 3, TL_ROW_H - 3); }
  }
  g.globalAlpha = 1;
  // times only: when each busy stretch starts, and how long each folded break was
  g.font = '10px Inter, system-ui, sans-serif'; g.fillStyle = '#6e7179'; g.textBaseline = 'top';
  const y = lay.rows.length * TL_ROW_H + 3;
  let right = -1;
  for (const m of lay.marks) {
    const label = clockStr(m.t), w = g.measureText(label).width;
    if (m.x >= right + 6 && m.x + w <= width) { g.fillText(label, m.x, y); right = m.x + w; }
  }
  for (const gap of lay.gaps) {
    const label = fmtDur(gap.ms).replace(/ \d+s$/, ''), w = g.measureText(label).width, x = gap.x + gap.w / 2 - w / 2;
    if (x >= right + 4 && x + w <= width) { g.fillStyle = '#9a5b0c'; g.fillText(label, x, y); g.fillStyle = '#6e7179'; right = x + w; }
  }
}
// which step is under the pointer on the timeline
function timelineHit(px, py) {
  const lay = detail.lay, steps = detail.data?.steps;
  if (!lay || !steps) return null;
  const row = Math.floor(py / TL_ROW_H);
  let best = null, dist = 5;
  for (const bar of lay.bars) {
    if (bar.row !== row) continue;
    const dx = px < bar.x ? bar.x - px : px > bar.x + bar.w ? px - bar.x - bar.w : 0;
    if (dx < dist) { dist = dx; best = steps[bar.i]; }
  }
  return best;
}
function stepTip(d, s) {
  const st = safeStep(s), dur = s.ms != null ? fmtDur(s.ms) : s.end == null ? 'Running' : '';
  const out = FAILED.has(s.outcome) ? ({ failed: 'Failed', interrupted: 'Interrupted', denied: 'Refused by auto mode', refused: "Didn't run" })[s.outcome] : '';
  return `<b>${esc(whoOf(d, s.agent))}</b> · ${esc(st.tool)}${dur ? ' · ' + esc(dur) : ''}${out ? ' · ' + out : ''}<br><span>${esc(st.text)}</span>`;
}

function drawSteps(d) {
  const steps = detailSteps(), host = $('dtSteps');
  $('cntSteps').textContent = steps.length ? String(steps.length) : '';
  if (!steps.length) { setHTML(host, `<div class="empty-note">${detail.bot ? 'No tool calls from this agent.' : 'No tool calls yet.'}</div>`); return; }
  drawVList(host, host, steps.length, STEP_ROW_H, (i, style) => {
    const s = steps[i], st = safeStep(s);
    const dur = s.ms != null ? fmtDur(s.ms) : s.end == null ? 'Running' : '';
    return `<div class="srow${s.id === detail.sel ? ' sel' : ''}${FAILED.has(s.outcome) ? ' bad' : ''}" style="${style}" data-step="${esc(s.id)}" id="st-${esc(s.id)}" role="option" aria-selected="${s.id === detail.sel}">`
      + `<span class="t">${clockStr(s.start)}</span><i style="background:${FAILED.has(s.outcome) ? KIND_COLOR.error : KIND_COLOR[s.kind] || KIND_COLOR.tool}"></i>`
      + `<span class="w">${esc(whoOf(d, s.agent))}</span><span class="x">${esc(st.text)}</span><span class="dur">${esc(dur)}</span></div>`;
  });
  if (detail.sel) host.setAttribute('aria-activedescendant', 'st-' + detail.sel); else host.removeAttribute('aria-activedescendant');
}
function fmtValue(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'object' && !Array.isArray(v)) {
    if (typeof v.command === 'string' && Object.keys(v).length <= 3) return v.command;
    if (typeof v.stdout === 'string' || typeof v.stderr === 'string') return [v.stdout, v.stderr].filter(Boolean).join('\n');
  }
  return JSON.stringify(v, null, 1);
}
function renderStepPanel(d) {
  const box = $('dtStep'), s = detail.open && (detail.data?.steps || []).find((x) => x.id === detail.open);
  box.hidden = !s;
  if (!s) { setHTML(box, ''); return; }
  const st = safeStep(s), got = detail.stepCache.get(s.id);
  const out = s.outcome === 'ok' ? 'Ran' : FAILED.has(s.outcome) ? ({ failed: 'Failed', interrupted: 'Interrupted', denied: 'Refused by auto mode', refused: "Didn't run" })[s.outcome] : s.end == null ? 'Running' : 'No result recorded';
  let h = `<div class="st-h"><div><b>${esc(whoOf(d, s.agent))}</b> · ${esc(st.tool)}<br><small>${timeStr(s.start)}${s.ms != null ? ' · ' + fmtDur(s.ms) : ''} · ${out}</small></div><button type="button" class="chipbtn" data-close-step>Close</button></div>`;
  if (prefs.safe) h += '<div class="empty-note">Hidden while Safe to film is on.</div>';
  else if (!got || got.loading) h += '<div class="empty-note">Loading…</div>';
  else if (got.loadError) h += `<div class="empty-note">${esc(got.loadError)}</div>`;
  else if (got.none) h += `<div class="st-sec">What it did</div><pre>${esc(s.text)}</pre><div class="empty-note">${source?.replay ? "The full input and output aren't in this recording." : "Skyborne has no input or output stored for this step."}</div>`;
  else {
    if (got.input !== undefined) h += `<div class="st-sec">Input</div><pre>${esc(fmtValue(got.input))}</pre>`;
    // the store cuts a big output or error to its first 20 KB and marks it
    const cut = (v) => v && typeof v === 'object' && v.skyborneTruncated;
    const sec = (title, v, cls) => `<div class="st-sec">${title}${cut(v) ? ` <small>Cut to 20 KB when stored (it was ${Math.round(v.bytes / 1024)} KB)</small>` : ''}</div><pre${cls}>${esc(cut(v) ? v.head : fmtValue(v))}</pre>`;
    if (got.output !== undefined) h += sec('Output', got.output, '');
    if (got.error !== undefined && got.error !== '') h += sec('Error', got.error, ' class="bad"');
    else if (s.error) h += `<div class="st-sec">Error</div><pre class="bad">${esc(s.error)}</pre>`;
  }
  setHTML(box, h);
}
function renderTalk(data) {
  const items = data.conversation || [];
  let h = items.length ? '' : '<div class="empty-note">No prompts yet.</div>';
  for (const [i, c] of items.entries()) {
    const who = c.who === 'you' ? 'You' : 'Claude';
    if (prefs.safe) { h += `<div class="talk ${c.who}"><small>${c.who === 'you' ? 'Prompt' : 'Reply'} · ${clockStr(c.ts)}</small></div>`; continue; }
    const long = (c.text || '').length > 600 || (c.text || '').split('\n').length > 10;
    const open = detail.more?.has(i);
    h += `<div class="talk ${c.who}"><small>${who} · ${clockStr(c.ts)}</small><div class="tx${long && !open ? ' fold' : ''}">${esc(c.text || (c.who === 'you' ? '(No text)' : ''))}</div>${long ? `<button type="button" class="more" data-more="${i}">${open ? 'Show less' : 'Show more'}</button>` : ''}</div>`;
  }
  if (items.length) h += '<div class="foot-note">Claude\'s final reply in each turn. Text between tool calls isn\'t recorded.</div>';
  setHTML($('dtTalk'), h);
}
function renderFiles(data) {
  const files = data.files;
  let h;
  if (files == null) h = '<div class="empty-note">Not in this recording.</div>';
  else if (!files.length) h = '<div class="empty-note">No files edited.</div>';
  else h = files.map((f, i) => `<div class="frow"><span class="x">${esc(prefs.safe ? 'File ' + (i + 1) : f.path)}</span><span>${f.edits} edit${f.edits === 1 ? '' : 's'}${f.failed ? ` · <em>${f.failed} failed</em>` : ''}</span></div>`).join('');
  if (files != null) h += '<div class="foot-note">Edits made through shell commands aren\'t counted.</div>';
  setHTML($('dtFiles'), h);
}
const DECISION_WORD = { allow: 'Approved', deny: 'Denied', unknown: 'Answered', none: 'No answer' };
const ANSWERED_IN = { skyborne: 'In Skyborne', terminal: 'In the terminal', nowhere: 'Nowhere' };
function renderApprovals(d, data) {
  const list = data.approvals;
  let h;
  if (list == null) h = '<div class="empty-note">Not in this recording.</div>';
  else if (!list.length) h = '<div class="empty-note">No approvals asked.</div>';
  else h = list.map((a) => {
    const upTo = a.answeredIn === 'terminal' && (a.how === 'ran' || a.how === 'failed');  // the stored wait ran until the call finished
    // waitedMs runs from the ask to its answer; a timed-out one went on waiting in the terminal
    const waited = (a.timedOut ? 'Skyborne stopped holding it · ' : '') + (upTo ? 'Up to ' : 'Waited ') + fmtDur(a.waitedMs);
    return `<div class="arow"><div><b>${esc(DECISION_WORD[a.decision] || cap(a.decision))}</b> · ${esc(safeTool(a.tool))} · ${esc(whoOf(d, a.agent))}<small>${clockStr(a.askedAt)}</small></div>`
      + `${prefs.safe || !a.text ? '' : `<div class="x">${esc(a.text)}</div>`}<div class="m">${esc(ANSWERED_IN[a.answeredIn] || cap(a.answeredIn))} · ${esc(waited)}</div></div>`;
  }).join('');
  setHTML($('dtAsks'), h);
}
function renderAgentTokens(d) {
  const r = districtTokens(d);
  let h = `<div class="frow"><span class="x"><b>Whole session</b></span><span>${fmtTokens(r.total)}</span></div>`;
  if (!r.parts) h += '<div class="empty-note">Not broken down yet. Restart this session to see it.</div>';
  for (const a of r.agents) h += `<div class="frow${a.pending ? ' late' : ''}"><span class="x">${esc(a.role)} · ${esc(a.name)}${a.model ? ' · ' + esc(a.model) : ''}${a.left ? ' · Finished' : ''}</span><span>${a.pending ? 'When it finishes' : a.tokens ? fmtTokens(a.tokens) : 'None yet'}</span></div>`;
  if (r.earlier) h += `<div class="frow late"><span class="x">Earlier helpers</span><span>${fmtTokens(r.earlier)}</span></div>`;
  h += '<div class="foot-note">Each API message counted once, from the transcripts.</div>';
  setHTML($('dtTok'), h);
}
