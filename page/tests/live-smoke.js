// Live smoke test: the real Skyborne server (Python) serving the built page, fed real (scrubbed) hook
// events. Checks what only the live path can show:
//   - a new session's district appears within 1 s of its first event reaching the server (5 s on CI,
//     which has no graphics chip)
//   - a tab opened mid-session gets the whole current state at once (backfill), not an empty city
//   - two tabs both stay live
//   - renaming goes through POST /api/names with the page's token (and fails without it)
//   - a held permission request shows a "Needs you" card in both tabs; Approve answers the hook, both
//     cards say "sent" until the transcript shows Claude Code applied it, then clear within 2 s (the
//     server reads the transcript first); a late click is refused; an answer without the token is
//     refused; a tool that ran (a "Yes" in the terminal) clears its card within 1 s; an answer the
//     terminal beat is shown as too late; after a server restart the page, not reloaded, shows the new
//     server's requests, and a Deny, a rename and a detail work with the new launch token. On CI the tabs
//     get 15 s (16 s once the transcript is read), but the server itself, timed on a stream read here with
//     no drawing, holds 2 s (3 s) there too
//   - a past session (imported, two days old) shows as an asleep district once "Show past sessions" is on (the
//     tabs start with it on, as a browser that chose it would); with it off, ended sessions leave the city
//   - a session's detail comes from GET /api/session: one step per tool call, the conversation, a step's
//     full input from /api/step (the server sends it within 1 s, 2 s on CI; the panel shows it within 8 s,
//     24 s on CI), and the decisions of the approvals made here; a card shows its time limit
//   - the console answers what needs me, what's costing the most and what changed within 5 s (15 s on CI)
//   - a recording made by `skyborne record` plays (when tests/fixtures/recordings/demo.json exists)
//   - no request ever leaves 127.0.0.1
// Needs: `pip install -e .` (or the repo's .venv), then  node build.js && node tests/live-smoke.js
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const venv = path.join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const PY = process.env.SKYBORNE_PYTHON || (fs.existsSync(venv) ? venv : process.platform === 'win32' ? 'python' : 'python3');
const recording = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'recordings', 'live-session.json'), 'utf8'));
const DEMO = path.join(ROOT, 'tests', 'fixtures', 'recordings', 'demo.json');
const PAST = '00000000-0000-4000-8000-0000000000aa';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// a free port, so the server can be restarted on the same one
const freePort = () => new Promise((res) => { const s = require('net').createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); });

(async () => {
  const errors = [];
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'skyborne-live-smoke-'));
  const env = { ...process.env, SKYBORNE_HOME: home, CLAUDE_CONFIG_DIR: path.join(home, 'claude') };

  // a past session, as `skyborne import` leaves one: two days old, ended
  execFileSync(PY, ['-c', `
import sys, time
from skyborne.store import Store
st = Store(sys.argv[1]); t = int(time.time() * 1000) - 2 * 86400_000
base = {'session_id': '${PAST}', 'cwd': '/home/user/past-project', 'transcript_path': '/x.jsonl'}
for i, p in enumerate([{'hook_event_name': 'SessionStart', 'source': 'startup'}, {'hook_event_name': 'UserPromptSubmit', 'prompt': 'Old work'},
                       {'hook_event_name': 'Stop', 'last_assistant_message': 'Done long ago'}, {'hook_event_name': 'SessionEnd', 'reason': 'imported'}]):
    st.add_event(t + i * 1000, {**base, **p}, source='imported')
st.add_import('${PAST}', '/x.jsonl', 4, 0); st.commit()
`, path.join(home, 'skyborne.db')], { env });

  const port = await freePort();
  let out = '', server = null;
  const startServer = async () => {
    out = '';
    server = spawn(PY, ['-m', 'skyborne', '--no-open', '--port', String(port)], { env, cwd: ROOT });
    server.stdout.on('data', (d) => { out += d; });
    server.stderr.on('data', (d) => { out += d; });
    let url = null;
    for (let i = 0; i < 100 && !url; i++) { await sleep(100); url = (/http:\/\/127\.0\.0\.1:\d+\//.exec(out) || [])[0]; }
    return url;
  };
  const base = await startServer();
  if (!base) { console.log('FAIL server did not start:', out); server.kill(); process.exit(1); }
  const post = (payload) => fetch(base + 'hook', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });

  const browser = await chromium.launch({ args: ['--ignore-gpu-blocklist', '--enable-unsafe-swiftshader'] });
  // two tabs drawing the city at once: CI draws them in software, so the tabs are kept smallish
  const context = await browser.newContext({ viewport: { width: 1024, height: 640 }, serviceWorkers: 'block' });
  const offMachine = [];
  await context.route((url) => url.hostname !== '127.0.0.1', (route) => { offMachine.push(route.request().url()); route.abort(); });
  // "Show past sessions" on, as this browser had chosen: the recorded session ends partway through these checks
  await context.addInitScript(() => { try { localStorage.setItem('skyborne.showPast', 'true'); } catch (e) {} });
  const renames = [];
  context.on('request', (r) => { if (r.url().endsWith('/api/names')) renames.push({ token: r.headers()['x-skyborne-token'] || '', body: r.postData() }); });
  const timings = {};
  const open = async (name) => {
    const t = Date.now();
    const p = await context.newPage();
    p.on('pageerror', (e) => errors.push('page error: ' + e.message));
    p.on('console', (m) => { if (m.type() === 'error' && /Skyborne frame error|THREE\.WebGLProgram|Shader Error/.test(m.text())) errors.push(m.text()); });
    await p.goto(base);
    // a tab boots in well under a second with a graphics chip; in CI's software renderer a second tab beside a
    // busy first one has taken over a minute
    await p.waitForFunction(() => window.__skyborne && window.__skyborne.city.status === 'live', null, { timeout: 120000 });
    timings[name + 'LiveMs'] = Date.now() - t;
    return p;
  };
  // the first frames compile the city's shaders (seconds, on CI's software renderer): timings start after them
  const settle = (p) => p.waitForFunction(() => { const r = window.__skyborne.renderer.info.render; window.__smokeFrame ??= r.frame; return r.frame - window.__smokeFrame >= 5; },
    null, { timeout: 120000 }).catch(() => errors.push('The page drew no frames'));
  const district = (p, id) => p.evaluate((id) => {
    const d = window.__skyborne.city.districts.get(id);
    return d && !d.leaving ? { asleep: d.asleep, bots: [...d.robots.values()].filter((b) => !b.leaving).length, turns: d.doc?.turns, tools: d.doc?.agents?.[0]?.tools,
      headline: d.doc?.headline, name: d.displayName(), tokens: d.doc?.tokens?.total } : null;
  }, id);
  const waitFor = async (check, ms = 5000) => { const end = Date.now() + ms; let v; while (Date.now() < end) { v = await check(); if (v) return v; await sleep(25); } return v; };
  // how long to wait for what a tab shows ("eventually" checks, not timing targets): CI draws in software on machines
  // of varying speed (a second tab has taken 71 s to go live there), so it gets three times as long
  const room = (ms) => (process.env.CI ? 3 * ms : ms);
  // rename a district from the Sessions list, as a person would; false if its ✎ never showed
  const renameFrom = async (p, id, name) => {
    await p.evaluate(() => document.querySelector('.tab[data-tab="city"]').click());
    await p.waitForTimeout(500);
    if (!await waitFor(() => p.evaluate((id) => { const btn = document.querySelector(`.dren[data-ren="${id}"]`); btn?.click(); return !!btn; }, id), room(5000))) return false;
    await p.waitForTimeout(400);
    await p.fill('#renameIn', name); await p.press('#renameIn', 'Enter');
    return true;
  };

  try {
    const a = await open('firstTab');
    await settle(a);
    // the past session is there, asleep
    const past = await waitFor(() => district(a, PAST), room(5000));
    if (!past || !past.asleep) errors.push('Past session not shown asleep: ' + JSON.stringify(past));

    // a new session: its district rises within 1 s of the first event
    const events = recording.events.map((e) => e.payload);
    const SID = events[0].session_id;
    // 1 s on a machine with a graphics chip (here: about 0.1 s; 0.6 s even in software). CI machines have
    // none and draw the city in software at a frame or two a second, which delays the page itself: there
    // the check is only that the district comes promptly (5 s), not the 1 s target
    const limit = process.env.CI ? 5000 : 1000;
    const t0 = Date.now();
    await post(events[0]);
    const seen = await waitFor(() => district(a, SID), limit + 2000); // keeps looking past the limit, to report the real time
    const appearMs = Date.now() - t0;
    if (!seen || appearMs > limit) errors.push(`New session took ${appearMs} ms to appear (${JSON.stringify(seen)})`);

    // half the session, then a second tab opened mid-session gets everything at once
    const half = Math.floor(events.length / 2);
    for (const e of events.slice(1, half)) { await post(e); await sleep(20); }
    await sleep(800);
    const b = await open('secondTab');
    // both tabs compared at the same moment: a helper leaves 30 s after it finishes, in every tab, and the
    // second tab can take that long to start on CI. Nothing is sent meanwhile, so the new tab's state can
    // only come from the backfill; the first tab redraws its districts every 5 s, hence the wait
    const same = (x, y) => x && y && x.turns >= 1 && x.turns === y.turns && x.tools === y.tools && x.tokens === y.tokens && x.bots === y.bots;
    let aNow, bNow;
    await waitFor(async () => { aNow = await district(a, SID); bNow = await district(b, SID); return same(aNow, bNow); }, room(20000));
    if (!same(aNow, bNow)) errors.push(`Backfill differs: first tab ${JSON.stringify(aNow)}, new tab ${JSON.stringify(bNow)}`);

    // the rest of the session, then a new prompt: both tabs follow
    for (const e of events.slice(half)) { await post(e); await sleep(20); }
    await post({ ...events.find((e) => e.hook_event_name === 'UserPromptSubmit'), prompt: 'Both tabs see this' });
    const both = await waitFor(async () => (await district(a, SID))?.headline === 'Both tabs see this' && (await district(b, SID))?.headline === 'Both tabs see this', room(15000));
    if (!both) errors.push('Not both tabs stayed live');

    // renaming: through the page's own token, seen by both tabs; refused without the token
    if (!await renameFrom(a, SID, 'Live smoke')) errors.push("The recorded session's rename button did not show");
    const renamed = await waitFor(async () => (await district(a, SID))?.name === 'Live smoke' && (await district(b, SID))?.name === 'Live smoke', room(5000));
    const token = await a.evaluate(() => document.querySelector('meta[name="skyborne-token"]').content);
    if (!renamed || !renames.length || renames[0].token !== token || token.length < 40) errors.push(`Rename failed: ${JSON.stringify({ renamed, renames })}`);
    const noToken = await a.evaluate((id) => fetch('/api/names', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, name: 'x' }) }).then((r) => r.status), SID);
    if (noToken !== 403) errors.push(`A rename without the token got ${noToken}`);

    // approvals: the plugin's curl holds a PermissionRequest on /permission until it's answered. In a
    // session of its own: the recorded one has ended, and a request in an ended session is over at once
    const ASID = '00000000-0000-4000-8000-0000000000ab';
    const transcript = path.join(home, 'approvals-session.jsonl');  // where Claude Code says what came of an answer
    fs.writeFileSync(transcript, '');
    const line = (obj) => fs.appendFileSync(transcript, JSON.stringify({ ...obj, timestamp: new Date().toISOString() }) + '\n');
    const decided = (tuid, decision) => line({ type: 'attachment', attachment: { type: 'hook_permission_decision', decision, toolUseID: tuid, hookEvent: 'PermissionRequest' } });
    const result = (tuid) => line({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: tuid, is_error: false, content: 'ok' }] } });
    await post({ session_id: ASID, hook_event_name: 'UserPromptSubmit', cwd: events[0].cwd, prompt: 'Make three files', transcript_path: transcript });
    const permission = (payload) => fetch(base + 'permission', { method: 'POST', body: JSON.stringify(payload),
      headers: { 'Content-Type': 'application/json', 'X-Skyborne-Entrypoint': 'cli', 'X-Skyborne-Wait': '60' } }).then(async (r) => ({ status: r.status, body: await r.text() }));
    const bash = (name, tuid, cmd, extra = {}) => ({ session_id: ASID, hook_event_name: name, cwd: events[0].cwd, transcript_path: transcript, tool_name: 'Bash',
      tool_input: { command: cmd }, ...(tuid ? { tool_use_id: tuid } : {}), ...extra });
    // cards: 1 s here too. On CI two tabs draw in software side by side, and one of them can lag several
    // seconds behind (1 to 6.6 s seen across runs): there the tabs are only checked to clear a card
    // promptly (10 s), and the server's own part is timed on a stream of its own (below)
    const cardLimit = process.env.CI ? 15000 : limit;  // CI runners vary: 9.3 s has been seen
    const cardWait = Math.max(5000, cardLimit + 2000);
    // the server's part, with no drawing: its event stream, read here. 1 s (2 s on CI, where the tabs' software
    // drawing shares the machine)
    const serverLimit = process.env.CI ? 2000 : 1000;
    const wire = [];  // { event, data, at }
    const wireStop = new AbortController();
    fetch(base + 'events', { signal: wireStop.signal }).then(async (res) => {
      const dec = new TextDecoder(); let buf = '';
      for await (const chunk of res.body) {
        buf += dec.decode(chunk, { stream: true });
        for (let i; (i = buf.indexOf('\n\n')) >= 0; buf = buf.slice(i + 2)) {
          const msg = buf.slice(0, i), ev = /^event: (.*)$/m.exec(msg)?.[1], data = /^data: (.*)$/m.exec(msg)?.[1];
          if (ev) wire.push({ event: ev, data: data === undefined ? null : JSON.parse(data), at: Date.now() });
        }
      }
    }).catch(() => {});
    if (!await waitFor(() => wire.some((m) => m.event === 'ready'))) errors.push("The server's event stream did not start");
    // ms from t0 until the server sends a list of asks without this one, or null
    const serverClearedIn = async (t0, id) => {
      const m = await waitFor(() => wire.find((m) => m.at >= t0 && m.event === 'asks' && !m.data.asks.some((c) => c.id === id)), cardWait);
      return m ? m.at - t0 : null;
    };
    const cards = (p) => p.evaluate(() => [...document.querySelectorAll('#asks .ask')].map((el) => ({ id: el.dataset.ask, text: el.innerText })));
    // ms from t0 until a tab shows no card (each tab watched on its own, so neither waits on the other), or null
    const clearedIn = (t0, p) => waitFor(async () => (await cards(p)).length === 0, cardWait).then((ok) => (ok ? Date.now() - t0 : null));
    const answerFrom = (p, id, decision, withToken = true) => p.evaluate(({ id, decision, withToken }) => fetch('/api/answer', { method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(withToken ? { 'X-Skyborne-Token': document.querySelector('meta[name="skyborne-token"]').content } : {}) },
      body: JSON.stringify({ id, decision }) }).then((r) => r.status), { id, decision, withToken });
    await post(bash('PreToolUse', 'toolu_smoke_1', 'touch smoke-1.txt'));
    const held1 = permission(bash('PermissionRequest', null, 'touch smoke-1.txt'));
    if (!await waitFor(async () => (await cards(a)).length === 1 && (await cards(b)).length === 1, room(cardWait))) errors.push('The approval card did not show in both tabs');
    const [card1] = await cards(a);
    if (!card1 || !card1.text.includes('touch smoke-1.txt')) errors.push('The approval card does not show the command: ' + JSON.stringify(card1));
    if (!card1 || !/Times out in \d+:\d\d/.test(card1.text)) errors.push('The approval card has no time limit: ' + JSON.stringify(card1));
    await a.evaluate((id) => document.querySelector(`[data-ask="${id}"] [data-ans="allow"]`).click(), card1?.id);
    const answered = await held1;
    if (answered.status !== 200 || !answered.body.includes('"behavior":"allow"')) errors.push('The hook did not get the approval: ' + JSON.stringify(answered));
    const sentIn = (p) => waitFor(async () => (await cards(p))[0]?.text.includes('Waiting for Claude Code'), room(cardWait));
    if (!await sentIn(a) || !await sentIn(b)) errors.push('A sent answer is not shown as waiting for Claude Code in both tabs');
    let t = Date.now();
    decided('toolu_smoke_1', 'allow'); result('toolu_smoke_1');  // Claude Code applied it
    timings.appliedClearedMs = await Promise.all([clearedIn(t, a), clearedIn(t, b)]);
    timings.appliedServerMs = await serverClearedIn(t, card1?.id);
    if (timings.appliedServerMs === null || timings.appliedServerMs > serverLimit + 1000) errors.push(`The server took ${timings.appliedServerMs} ms to clear an applied answer`);
    const applied = wire.filter((m) => m.event === 'answer' && m.data.id === card1?.id).map((m) => m.data.applied);  // true: applied; false: the terminal beat it; null: unknown
    if (applied.at(-1) !== true || applied.includes(false)) errors.push('An applied answer was not reported as applied: ' + JSON.stringify(applied));
    // the 1 s target is for answers typed in the terminal; here the server reads the transcript first
    if (timings.appliedClearedMs.some((ms) => ms === null || ms > cardLimit + 1000)) errors.push(`An applied answer took ${timings.appliedClearedMs} ms to clear its card`);
    const late = await answerFrom(b, card1?.id, 'deny');
    if (late !== 409) errors.push(`A second answer to the same request got ${late}`);
    await post(bash('PreToolUse', 'toolu_smoke_2', 'touch smoke-2.txt'));
    const held2 = permission(bash('PermissionRequest', null, 'touch smoke-2.txt'));
    const card2 = await waitFor(async () => (await cards(a))[0], room(cardWait));
    // the questions the console answers, on the recorded session in the second tab while this request waits: what
    // needs me (the inbox), what's costing the most (Usage → By session), what changed (Logs), each within 5 s
    // (15 s on CI, where the second of two software-drawn tabs is the slowest to reach)
    const questionLimit = process.env.CI ? 15000 : 5000;
    const askQ = async (act, test) => { const t = Date.now(); await b.evaluate(act); return (await waitFor(() => b.evaluate(test), questionLimit)) ? Date.now() - t : null; };
    timings.questionsMs = {
      needsMe: await askQ(() => {}, () => !!document.querySelector('#asks .ask [data-ans="allow"]:not([hidden])')),
      cost: await askQ(() => document.getElementById('tokStat').click(), () => !!document.querySelector('#tokTipBody .tt-s')),
      changed: await askQ(() => { document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); document.querySelector('.tab[data-tab="log"]').click(); },
        () => !!document.querySelector('#logList .lrow')),
    };
    await b.evaluate(() => document.querySelector('.tab[data-tab="city"]').click());
    if (Object.values(timings.questionsMs).some((ms) => ms === null || ms > questionLimit)) errors.push('The console was slow to answer: ' + JSON.stringify(timings.questionsMs));
    const noTokenAnswer = await answerFrom(a, card2?.id, 'allow', false);
    await sleep(400);
    if (noTokenAnswer !== 403 || (await cards(a)).length !== 1) errors.push(`An answer without the token got ${noTokenAnswer} and changed the cards`);
    t = Date.now();
    await post(bash('PostToolUse', 'toolu_smoke_2', 'touch smoke-2.txt', { duration_ms: 5 }));  // a "Yes" in the terminal
    timings.terminalYesClearedMs = await Promise.all([clearedIn(t, a), clearedIn(t, b)]);
    timings.terminalYesServerMs = await serverClearedIn(t, card2?.id);
    if (timings.terminalYesServerMs === null || timings.terminalYesServerMs > serverLimit) errors.push(`The server took ${timings.terminalYesServerMs} ms to clear a card after a tool ran`);
    if (timings.terminalYesClearedMs.some((ms) => ms === null || ms > cardLimit)) errors.push(`A tool that ran took ${timings.terminalYesClearedMs} ms to clear its card`);
    const released = await held2;
    if (released.status !== 200 || released.body !== '') errors.push('A request answered in the terminal got a decision: ' + JSON.stringify(released));
    // a click the terminal beat: sent while the tool runs after a "Yes" there; the result comes with no hook decision
    await post(bash('PreToolUse', 'toolu_smoke_4', 'touch smoke-4.txt'));
    const held4 = permission(bash('PermissionRequest', null, 'touch smoke-4.txt'));
    const card4 = await waitFor(async () => (await cards(a))[0], room(cardWait));
    await a.evaluate((id) => document.querySelector(`[data-ask="${id}"] [data-ans="allow"]`).click(), card4?.id);
    await held4;
    await waitFor(async () => (await cards(a))[0]?.text.includes('Waiting for Claude Code'), room(cardWait));
    result('toolu_smoke_4');
    const tooLate = await waitFor(async () => (await cards(a)).find((c) => c.text.includes('Already answered in the terminal')), room(cardWait));
    if (!tooLate) errors.push('An answer the terminal beat is not shown as too late');
    if (!await waitFor(async () => (await cards(a)).length === 0, 3000 + room(cardWait))) errors.push('The too-late card did not close');  // it stays 3 s
    wireStop.abort();

    // a session's detail, from the server's whole record: the recorded session's steps (one per tool call), its
    // conversation, and a step's full input; then the approvals session's decisions, as answered here
    const callIds = new Set(events.filter((e) => e.hook_event_name === 'PreToolUse').map((e) => e.tool_use_id));
    const detailOf = (id) => a.evaluate((id) => {
      if (window.__skyborne.detail.id !== id) { window.__skyborne.closeDetail(); document.querySelector('.tab[data-tab="city"]').click(); document.querySelector(`#sessList [data-d="${id}"]`)?.click(); }
      const d = window.__skyborne.detail;
      return d.data && !document.getElementById('dtBody').hidden && document.getElementById('dtTok').childElementCount > 0 && { steps: d.data.steps.length, rows: document.querySelectorAll('#dtSteps .srow').length, talk: d.data.conversation.length,
        asks: document.getElementById('dtAsks').innerText, files: document.getElementById('dtFiles').innerText, tok: document.querySelectorAll('#dtTok .frow').length,
        made: d.data.steps.find((s) => s.text.includes('made-by-test'))?.id };
    }, id);
    const det = await waitFor(() => detailOf(SID), room(15000));
    if (!det || det.steps !== callIds.size || det.rows !== callIds.size || det.talk < 7 || !det.files.includes('No files edited') || !det.asks.includes('No approvals asked') || det.tok < 1 || !det.made)
      errors.push(`Session detail of the recorded session (${callIds.size} calls): ` + JSON.stringify(det));
    if (det?.made) {
      // the server's part, timed alone: one call's full input from GET /api/step
      let t = Date.now();
      const stepRes = await fetch(`${base}api/step?session=${SID}&id=${det.made}`, { headers: { 'X-Skyborne-Token': token } });
      timings.stepServerMs = stepRes.ok && (await stepRes.json()).input?.command?.includes('made-by-test') ? Date.now() - t : null;
      if (timings.stepServerMs === null || timings.stepServerMs > serverLimit) errors.push(`The server took ${timings.stepServerMs} ms to send a step`);
      // and the page's: a click shows it in full (CI's busy software-drawn tabs need several of their slow turns for it)
      t = Date.now();
      await a.evaluate((id) => document.querySelector(`[data-step="${id}"]`).click(), det.made);
      const full = await waitFor(() => a.evaluate(() => document.getElementById('dtStep').innerText.includes('touch made-by-test.txt') && document.getElementById('dtStep').innerText.includes('Input')), process.env.CI ? 24000 : 8000);
      timings.stepOpenMs = full ? Date.now() - t : null;
      if (!full) errors.push('A step did not open in full: ' + await a.evaluate(() => document.getElementById('dtStep').innerText.slice(0, 200)));
    }
    const asksDet = await waitFor(async () => { const d = await detailOf(ASID); return d && d.asks.includes('In Skyborne') && d.asks.includes('In the terminal') && d; }, room(15000));
    if (!asksDet) errors.push('Approvals in the detail: ' + JSON.stringify(await detailOf(ASID)));
    await a.evaluate(() => window.__skyborne.closeDetail());
    // with "Show past sessions" off, ended and old sessions leave the city; the live one stays
    await a.evaluate(() => document.getElementById('btnPast').click());
    const pastGone = await waitFor(async () => !(await district(a, PAST)) && !(await district(a, SID)) && !!(await district(a, ASID)), room(10000));
    if (!pastGone) errors.push('With past sessions hidden: ' + JSON.stringify({ past: await district(a, PAST), recorded: await district(a, SID), live: await district(a, ASID) }));
    await a.evaluate(() => document.getElementById('btnPast').click());

    // a recording plays in place of the live city
    if (fs.existsSync(DEMO)) {
      await a.setInputFiles('#inRec', DEMO);
      const rec = JSON.parse(fs.readFileSync(DEMO, 'utf8'));
      const playing = await waitFor(async () => (await a.evaluate(() => window.__skyborne.city.status)) === 'replay' && district(a, rec.session.id), room(30000));
      if (!playing) errors.push('The recording did not play');
      await a.evaluate(() => window.__skyborne.backToLive());
    }

    // a restarted server (a new launch token): the page reconnects by itself, shows the new server's requests
    // (its list versions start over) and takes the new token from `ready`, so with no reload a Deny reaches the
    // hook, a rename saves and a detail loads. The token in the page's HTML is still the old one: refused
    server.kill(); await sleep(500);
    if (!await startServer()) errors.push('The server did not restart: ' + out);
    if (!await waitFor(async () => (await a.evaluate(() => window.__skyborne.city.status)) === 'live' && district(a, ASID), room(30000))) errors.push('The page did not reconnect after a server restart');
    const newToken = (/name="skyborne-token" content="([^"]+)"/.exec(await (await fetch(base)).text()) || [])[1];
    if (!newToken || newToken === token) errors.push('The restarted server kept the old token');
    await post(bash('PreToolUse', 'toolu_smoke_3', 'touch smoke-3.txt'));
    const held3 = permission(bash('PermissionRequest', null, 'touch smoke-3.txt'));
    const card3 = await waitFor(async () => (await cards(a))[0], room(10000));
    if (!card3) errors.push('After a server restart the page shows no approval card');
    await a.screenshot({ path: path.resolve(__dirname, '..', 'dist', 'smoke-live-asks.png') });
    const oldKey = await answerFrom(a, card3?.id, 'allow');
    if (oldKey !== 403) errors.push(`An answer with the old launch's token got ${oldKey}`);
    await a.evaluate((id) => document.querySelector(`[data-ask="${id}"] [data-ans="deny"]`)?.click(), card3?.id);
    const denied = await Promise.race([held3, sleep(room(cardWait)).then(() => null)]);  // a broken swap mustn't wait out the 60 s hold
    if (denied?.status !== 200 || !denied.body.includes('"behavior":"deny"')) errors.push('After a server restart a Deny from the page did not reach the hook: ' + JSON.stringify(denied));
    decided('toolu_smoke_3', 'deny');  // Claude Code applied it
    if (!await waitFor(async () => (await cards(a)).length === 0, room(cardWait))) errors.push('After a server restart the denied card did not close');
    if (!await renameFrom(a, ASID, 'After restart')) errors.push("After a server restart the approvals session's rename button did not show");
    // tab a shows a new name before the server answers: tab b shows it only once the server saved it
    if (!await waitFor(async () => (await district(b, ASID))?.name === 'After restart', room(10000)) || renames.at(-1)?.token !== newToken)
      errors.push('After a server restart a rename from the page failed: ' + JSON.stringify({ b: await district(b, ASID), sent: renames.at(-1)?.token === newToken }));
    if (!await waitFor(() => detailOf(ASID), room(15000))) errors.push("After a server restart a session's detail did not load");
    await a.evaluate(() => window.__skyborne.closeDetail());
    await a.screenshot({ path: path.resolve(__dirname, '..', 'dist', 'smoke-live.png') });
    if (offMachine.length) errors.push('Requests left 127.0.0.1: ' + [...new Set(offMachine)].join(', '));
    console.log(errors.length ? 'FAIL' : 'OK', JSON.stringify({ appearMs, past, backfill: bNow, recording: fs.existsSync(DEMO), ...timings }), errors.length ? errors : '');
  } catch (e) {
    errors.push(String(e && e.stack || e));
    console.log('FAIL', JSON.stringify(timings), errors);
  } finally {
    await browser.close();
    server.kill();
    await sleep(300);
    fs.rmSync(home, { recursive: true, force: true });
  }
  process.exit(errors.length ? 1 : 0);
})();
