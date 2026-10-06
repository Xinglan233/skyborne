// A pretend city for local previews (dist/preview.html only; never shipped). It is the page's
// third data source (window.__skyborneFake): sessions stream in, keep working, a new one joins
// after ~12 s, and renames are kept in memory (and listed in window.__writes for the smoke test).
// Two permission requests wait on the console's "Needs you" cards: pixel-forge's lead wants to run a
// command, and atlas-api's scout asks a question only the terminal can answer. Answers are listed in
// window.__writes too, and window.__fakeAsk() adds another request. Claude Code confirms an answer
// 0.6 s later; a request added with `late: 'now'` was already answered in the terminal (refused at once),
// and one with `late: 'after'` turns out to have been (told 0.8 s after the answer is sent).
// A session's detail (GET /api/session on the real server) is built from the fake's own log, plus a canned
// conversation, files and approvals carrying marker words ("XYZ", "secretco") that Safe to film must hide.
// window.__fakeBig() adds a session with 2,500 steps; window.__fakeMany(live, past) swaps the city for that
// many live and past sessions, one past one waiting on you.
// atlas-api plays a ~40 s story on a loop so every bot moment can be seen: a helper beams in
// (salute, lead waves), finishes and high-fives the lead, the lead hits an error (every other
// loop), then finishes (victory dance, others clap), idles, and gets back to work.
(() => {
  const now = Date.now();
  window.__writes = [];
  const helper = (id, name, type, status = 'working') => ({ id, name, type, status, kind: status === 'working' ? 'search' : status });
  const S = [
    { id: 'p-atlas', title: 'atlas-api', headline: 'Add rate limits to the public API', startedAt: now - 42 * 60e3, turns: 9, tok: 1_240_000,
      helpers: [helper('a1', 'Scout 1', 'Explore'), helper('a2', 'Builder 1', 'general-purpose')], busy: true, story: true, ctx: 30 },
    { id: 'p-forge', title: 'pixel-forge', headline: 'Ship the new export dialog', startedAt: now - 15 * 60e3, turns: 3, tok: 320_000,
      helpers: [helper('p1', 'Architect 1', 'Plan')], busy: true, waiting: true, waitSince: now - 70e3, ctx: 87 },
    { id: 'p-sky', title: 'skyline-ui', headline: 'Fix the flaky login test', startedAt: now - 5 * 60e3, turns: 1, tok: 40_000,
      helpers: [helper('s1', 'Tester 1', 'test-runner', 'idle')], busy: false, sessionName: 'Login Fixer', ctx: 21 },
    { id: 'p-port', title: 'data-port', headline: 'Draft the weekly report', startedAt: now - 90 * 60e3, turns: 2, tok: 90_000, helpers: [], busy: false, stale: true, ctx: 45 },
    // a command (a video export) that has run for 7 minutes with no news for 6: the console flags it Slow, and its
    // bot stays awake (a district sleeps after 3 quiet minutes only when no bot is mid-step)
    { id: 'p-slow', title: 'etl-jobs', headline: 'Backfill last month', startedAt: now - 20 * 60e3, turns: 1, tok: 60_000, helpers: [], busy: false, ctx: 12,
      quietAt: now - 6 * 60e3, running: { kind: 'bash', tool: 'Bash', since: now - 7 * 60e3, text: '$ make backfill' } },
    // a conversation moved to the background a minute ago: this session ended there, so it's past
    { id: 'p-moved', title: 'film-studio', headline: 'Re-time the film to the voice', startedAt: now - 30 * 60e3, turns: 4, tok: 80_000, helpers: [], busy: false,
      ended: true, pastAt: now - 60e3, endReason: 'continued' },
  ];
  const kinds = ['bash', 'edit', 'read', 'search', 'think', 'web', 'edit', 'bash'];
  const text = { bash: '$ npm test', edit: 'Editing routes.ts', read: 'Reading README.md', search: 'Searching handleLogin', think: 'Thinking', web: 'Web search: rate limits',
    spawn: 'Sending a Critic to review', answer: 'Finished: rate limits added', idle: 'Waiting for the next prompt', done: 'Handed back the result', error: 'Hit an error' };
  for (const s of S) { s.feed = [{ ts: now - 30e3, agent: 'Skybot', agentId: 'main', kind: 'prompt', text: s.headline }]; s.tools = 20; s.lead = { status: s.busy ? 'working' : 'idle', kind: s.busy ? 'think' : 'idle' }; }
  for (const s of S) if (s.running) { s.lead = { status: 'working', kind: s.running.kind }; s.feed.unshift({ ts: s.running.since, agent: 'Skybot', agentId: 'main', kind: s.running.kind, text: s.running.text, tool: s.running.tool, toolUseId: s.id + '-run' }); }
  let callN = 0;
  const TOOLS = { bash: 'Bash', edit: 'Edit', write: 'Write', read: 'Read', search: 'Grep', web: 'WebSearch' };
  let tick = 0;
  // tokens split the way a real session's are: mostly cache re-reads
  const split = (n) => { const cr = Math.round(n * .84), cw = Math.round(n * .1), inn = Math.round(n * .04); return { cr, cw, in: inn, out: n - cr - cw - inn }; };
  const ctxOf = (s) => (s.story ? Math.min(98, 30 + (tick % 27) * 2.6) : s.ctx);
  const modelFor = (type) => (type === 'Explore' ? 'claude-haiku-4-5-20251001' : type === 'general-purpose' ? 'claude-sonnet-5-5' : 'claude-opus-5-5');
  const doc = (s) => ({
    v: 2, title: s.title, ...(s.sessionName ? { sessionName: s.sessionName } : {}), headline: s.headline, startedAt: s.startedAt,
    updatedAt: s.pastAt || s.quietAt || (s.stale ? now - 10 * 60e3 : Date.now()), turns: s.turns, ...(s.ended ? { ended: { at: s.pastAt, reason: s.endReason || 'prompt_input_exit' } } : {}), tokens: { total: s.tok, ...split(s.tok), helpers: (s.doneList || []).map((h) => ({ id: h.id, name: h.name, model: 'claude-opus-5-5', usage: split(h.tok) })) },
    ...(s.ctx != null || s.story ? { context: { tokens: Math.round(ctxOf(s) * 2000), window: 200000, percent: Math.round(ctxOf(s) * 10) / 10 } } : {}), cost: { usd: Math.round(s.tok / 1e6 * 900) / 1000 },
    waiting: s.waiting ? { agent: 'main', tool: 'Bash', since: s.waitSince || now - 4e3 } : null,
    ...(window.__fakeLimits ? { rateLimits: window.__fakeLimits } : {}),
    agents: [{ id: 'main', name: 'Skybot', role: 'Lead Agent', type: 'lead', status: s.lead.status, kind: s.waiting ? 'wait' : s.lead.kind, tool: s.running ? s.running.tool : 'Bash',
      activity: s.waiting ? 'Needs approval: Bash' : s.running ? s.running.text : text[s.lead.kind] || '', activitySince: s.running ? s.running.since : now, waiting: !!s.waiting, tools: s.tools,
      model: 'claude-opus-5-5', usage: split(Math.round(s.tok * .7)) }]
      // a helper's tokens arrive as it finishes: none while it works
      .concat(s.helpers.map((h) => ({ id: h.id, name: h.name, role: h.type + ' Agent', type: h.type, status: h.status, kind: h.kind, activity: text[h.kind] || '', activitySince: h.since || now, parent: 'main', waiting: false, tools: 5, model: modelFor(h.type), usage: split(h.status === 'working' ? 0 : (h.tok || 0)) }))),
    feed: s.feed.slice(0, 50),
  });
  // a tool call carries its id and how long it ran, as the server's feed does
  const say = (s, who, kind, extra = 0) => s.feed.unshift({ ts: Date.now() + extra, agent: who ? who.name : 'Skybot', agentId: who ? who.id : 'main', kind, text: text[kind],
    ...(TOOLS[kind] ? { tool: TOOLS[kind], toolUseId: 'toolu_fake_' + (++callN), durationMs: 40 + (callN * 977) % 3000 } : {}) });
  const LOOP = 27; // ticks of 1.5 s
  const story = (s) => {
    const n = tick % LOOP, loop = Math.floor(tick / LOOP);
    // helpers that finished linger 30 s (20 ticks), like the real mod, then leave
    for (const h of s.helpers) if (h.doneAt && tick - h.doneAt >= 20) (s.doneList ||= []).unshift({ id: h.id, name: h.name, tok: h.tok });
    s.doneList = (s.doneList || []).slice(0, 20);
    s.helpers = s.helpers.filter((h) => !(h.doneAt && tick - h.doneAt >= 20));
    const critic = s.helpers.find((h) => h.loop === loop);
    if (n === 1) { s.helpers.push({ ...helper('c' + loop, 'Critic ' + (loop + 1), 'code-reviewer'), loop }); s.lead.kind = 'spawn'; say(s, null, 'spawn'); }
    if (n === 8 && critic) { critic.status = 'done'; critic.kind = 'done'; critic.doneAt = tick; critic.since = Date.now(); critic.tok = 180_000; say(s, critic, 'done'); }
    if (n === 12 && loop % 2) say(s, null, 'error');
    if (n === 16) { s.lead.status = 'done'; s.lead.kind = 'answer'; say(s, null, 'answer'); }
    if (n === 19) { s.lead.status = 'idle'; s.lead.kind = 'idle'; }
    if (n === 0 || n === 26) { s.lead.status = 'working'; s.lead.kind = 'think'; }
  };
  const step = () => {
    tick++;
    for (const s of S) {
      if (!s.busy || s.waiting) continue;
      const k = kinds[(tick + s.title.length) % kinds.length]; s.tools += 3; s.tok += 12000;
      if (s.lead.status === 'working' && s.lead.kind !== 'spawn') s.lead.kind = k;
      else if (s.lead.kind === 'spawn') s.lead.kind = 'think';
      s.helpers.forEach((h, i) => { if (h.status === 'working') h.kind = kinds[(tick + i * 3) % kinds.length]; });
      const busy = s.helpers.filter((h) => h.status === 'working');
      const who = busy.length && tick % 2 ? busy[tick % busy.length] : null;
      if (who || s.lead.status === 'working') say(s, who, who ? who.kind : k);
      if (tick % 4 === 0) { s.turns++; s.feed.unshift({ ts: Date.now() + 1, agent: 'Skybot', agentId: 'main', kind: 'prompt', text: 'Next: ship it' }); }
      if (s.story) story(s);
    }
    if (tick === 8) {
      const s = { id: 'p-new', title: 'landing-page', headline: 'Build the landing page', startedAt: Date.now(), turns: 1, tok: 10_000, helpers: [], busy: true, tools: 1,
        lead: { status: 'working', kind: 'think' }, ctx: 4, feed: [{ ts: Date.now(), agent: 'Skybot', agentId: 'main', kind: 'join', text: 'Moved into the landing-page district' }] };
      S.push(s);
    }
  };
  const names = {};
  let on = null, timer = 0, first = 0, askN = 0;
  let asks = [
    { id: 'ask-1', session: 'p-forge', agent: 'main', tool: 'Bash', input: { command: 'rm -rf dist && npm run build' }, since: now - 70e3, until: now + 525e3, state: 'open', terminalOnly: false },
    { id: 'ask-2', session: 'p-atlas', agent: 'a1', tool: 'AskUserQuestion', input: { questions: [{ question: 'Which API version should the limits apply to?' }] }, since: now - 20e3, state: 'terminal', terminalOnly: true },
  ];
  window.__fakeAsk = (input = { file_path: '/repo/pixel-forge/src/export.ts' }, tool = 'Edit', late = '') => {
    asks = asks.concat({ id: 'ask-x' + (++askN), session: 'p-forge', agent: 'p1', tool, input, since: Date.now(), until: Date.now() + 595e3, state: 'open', terminalOnly: false, late });
    on?.asks(asks);
    return 'ask-x' + askN;
  };
  const docs = () => new Map(S.map((s) => [s.id, doc(s)]));
  const TOOL = { bash: 'Bash', edit: 'Edit', write: 'Write', read: 'Read', search: 'Grep', web: 'WebSearch', mcp: 'mcp__secretco__query' };
  const big = { steps: null };
  const detailOf = (s) => {
    if (s.id === 'p-big') return big.detail;
    const feed = s.feed.slice().reverse(), steps = [];
    feed.forEach((f, i) => {
      if (!f.toolUseId) return;
      const ms = f.durationMs ?? null;
      steps.push({ id: f.toolUseId, agent: f.agentId || 'main', tool: f.tool, kind: f.kind, text: f.text, start: f.ts, end: ms == null ? null : f.ts + ms, ms, outcome: ms == null ? null : 'ok', error: '' });
    });
    const t0 = s.startedAt;
    steps.unshift(
      { id: `${s.id}-m1`, agent: 'main', tool: 'mcp__secretco__query', kind: 'mcp', text: 'Using mcp__secretco__query', start: t0 + 1000, end: t0 + 1800, ms: 800, outcome: 'ok', error: '' },
      { id: `${s.id}-b1`, agent: 'main', tool: 'Bash', kind: 'bash', text: '$ echo SECRET-CMD-XYZ', start: t0 + 2000, end: t0 + 2600, ms: 600, outcome: 'failed', error: 'SECRET-ERR-XYZ' },
      { id: `${s.id}-e1`, agent: 'main', tool: 'Edit', kind: 'edit', text: 'Editing notes-XYZ.md', start: t0 + 9 * 60e3, end: t0 + 9 * 60e3 + 500, ms: 500, outcome: 'ok', error: '' });
    const agents = [{ id: 'main', name: 'Skybot', role: 'Lead Agent', type: 'lead', parent: null, start: t0, end: null }]
      .concat(s.helpers.map((h) => ({ id: h.id, name: h.name, role: h.type + ' Agent', type: h.type, parent: 'main', start: t0 + 60e3, end: null })));
    return { id: s.id, updatedAt: Date.now(), agents, steps,
      conversation: [{ ts: t0 + 500, who: 'you', text: 'SECRET-PROMPT-XYZ: ' + s.headline }, { ts: t0 + 3000, who: 'claude', text: 'SECRET-REPLY-XYZ\n' + 'Long reply line.\n'.repeat(14) }],
      files: [{ path: '/secret/path/notes-XYZ.md', edits: 3, failed: 1 }, { path: '/repo/src/routes.ts', edits: 1, failed: 0 }],
      approvals: [
        { tool: 'mcp__secretco__query', agent: 'main', text: 'SECRET-APPROVAL-XYZ', askedAt: t0 + 900, answeredAt: t0 + 5100, decision: 'allow', answeredIn: 'skyborne', how: 'page', waitedMs: 4200, timedOut: false, confirmed: true },
        { tool: 'Bash', agent: 'main', text: '$ echo SECRET-CMD-XYZ', askedAt: t0 + 1900, answeredAt: t0 + 66900, decision: 'allow', answeredIn: 'terminal', how: 'ran', waitedMs: 65000, timedOut: false, confirmed: null },
        { tool: 'Bash', agent: 'main', text: '$ echo SECRET-CMD-XYZ', askedAt: t0 + 70000, answeredAt: t0 + 800000, decision: 'allow', answeredIn: 'terminal', how: 'ran', waitedMs: 730000, timedOut: true, confirmed: null }] };
  };
  window.__fakeBig = () => {
    const t0 = Date.now() - 2 * 3600e3, steps = [], kinds = ['bash', 'read', 'edit', 'search', 'write', 'web'], who = ['main', 'g1', 'g2', 'g3'];
    let t = t0;
    for (let i = 0; i < 2500; i++) {
      t += i % 400 === 399 ? 15 * 60e3 : 900 + (i * 7919) % 2200;  // a few long quiet breaks
      const k = kinds[i % kinds.length], ms = 120 + (i * 104729) % 2600;
      steps.push({ id: 'big-' + i, agent: who[i % 4], tool: TOOL[k], kind: k, text: `${k} step ${i}`, start: t, end: t + ms, ms, outcome: i % 97 === 0 ? 'failed' : 'ok', error: '' });
    }
    big.detail = { id: 'p-big', updatedAt: Date.now(), steps, conversation: [], files: [], approvals: [],
      agents: who.map((id, i) => ({ id, name: i ? 'Builder ' + i : 'Skybot', role: i ? 'general-purpose Agent' : 'Lead Agent', type: i ? 'general-purpose' : 'lead', parent: i ? 'main' : null, start: t0, end: null })) };
    S.push({ id: 'p-big', title: 'big-session', headline: 'A long day', startedAt: t0, turns: 40, tok: 9_000_000, busy: false, tools: 2500, ctx: 60,
      helpers: [helper('g1', 'Builder 1', 'general-purpose', 'done'), helper('g2', 'Builder 2', 'general-purpose', 'done'), helper('g3', 'Builder 3', 'general-purpose', 'done')],
      lead: { status: 'idle', kind: 'idle' }, feed: [{ ts: Date.now(), agent: 'Skybot', agentId: 'main', kind: 'prompt', text: 'A long day' }] });
    on?.docs(docs());
  };
  window.__fakeGone = (id) => { const i = S.findIndex((s) => s.id === id); if (i >= 0) S.splice(i, 1); on?.docs(docs()); };
  window.__fakeMany = (live = 6, past = 20) => {
    S.length = 0;
    for (let i = 0; i < live + past; i++) {
      const isPast = i >= live, waits = i === live;  // the first past one waits on you
      S.push({ id: 'm-' + i, title: (isPast ? 'past-' : 'live-') + i, headline: 'Session ' + i, startedAt: now - 3 * 3600e3, turns: 1, tok: 1000 * (i + 1), helpers: [],
        busy: false, lead: { status: 'idle', kind: 'idle' }, feed: [], tools: 0, ...(isPast ? { pastAt: now - (2 + i / 10) * 3600e3, ended: !waits && i % 2 === 0 } : {}), ...(waits ? { waiting: true, waitSince: now - 2 * 3600e3 } : {}) });
    }
    asks = [{ id: 'ask-past', session: 'm-' + live, agent: 'main', tool: 'Bash', input: { command: 'make release' }, since: now - 2 * 3600e3, until: now + 300e3, state: 'open', terminalOnly: false }];
    on?.docs(docs()); on?.asks(asks);
  };
  window.__skyborneFake = {
    start(handlers) {
      on = handlers;
      on.status('connecting');
      first = setTimeout(() => { on.names({ ...names }); on.docs(docs()); on.status('live'); on.asks(asks); }, 200);
      timer = setInterval(() => { step(); on.docs(docs()); }, 1500);
    },
    async rename(id, name) {
      if (name) { names[id] = name; window.__writes.push(['names/' + id, { name, at: Date.now() }]); }
      else { delete names[id]; window.__writes.push(['names/' + id, 'deleted']); }
      on?.names({ ...names });
    },
    async answer(id, decision) {
      const a = asks.find((x) => x.id === id && x.state === 'open');
      if (!a) throw Object.assign(new Error('answer'), { code: 409, reason: 'closed' });
      if (a.late === 'now') { asks = asks.filter((x) => x !== a); on?.asks(asks); throw Object.assign(new Error('answer'), { code: 409, reason: 'terminal' }); }
      window.__writes.push(['answers/' + id, decision]);
      asks = asks.map((x) => (x === a ? { ...x, state: 'sent' } : x)); on?.asks(asks);
      setTimeout(() => {
        asks = asks.filter((x) => x.id !== id);
        on?.answer({ id, session: a.session, agent: a.agent, decision, applied: a.late !== 'after' }); on?.asks(asks);
      }, a.late === 'after' ? 800 : 600);
    },
    async detail(id) { const s = S.find((x) => x.id === id); if (!s) throw Object.assign(new Error('detail'), { code: 404 }); return detailOf(s); },
    async step(sid, id) {
      if (id.endsWith('-b1') && window.__fakeStepRunning) return { tool: 'Bash', input: { command: 'echo SECRET-CMD-XYZ' } };  // no result stored yet
      if (id.endsWith('-b1')) return { tool: 'Bash', input: { command: 'echo SECRET-CMD-XYZ' }, output: { stdout: 'SECRET-OUT-XYZ' }, error: 'SECRET-ERR-XYZ' };
      return { tool: 'Read', input: { file_path: '/repo/README.md' }, output: { skyborneTruncated: true, bytes: 30720, head: 'The first 20 KB…' } };
    },
    stop() { clearTimeout(first); clearInterval(timer); },
  };
})();
