// Watches one session in the city, in a real browser window, and logs when each part of it first
// shows: the district, a tool call, helpers, a waiting approval (and when it clears), tokens.
// Times are this machine's clock in ms, so they can be compared with the server's received_at.
//   node dev/watch.js <city url> <session id> <out.jsonl> [screenshot dir]
// Runs until the session's district has left, or it is stopped.
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const [url, sid, out, shots] = process.argv.slice(2);
  const log = (o) => fs.appendFileSync(out, JSON.stringify(o) + '\n');
  const browser = await chromium.launch({ headless: false, args: ['--ignore-gpu-blocklist'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const offMachine = [];
  await context.route((u) => u.hostname !== '127.0.0.1', (route) => { offMachine.push(route.request().url()); route.abort(); });
  const page = await context.newPage();
  const shot = async (name) => { if (shots) await page.screenshot({ path: path.join(shots, name + '.png') }); };
  await page.exposeFunction('skyborneSeen', async (o) => { log(o); if (o.what !== 'gone') await shot(o.what); });
  await page.goto(url);
  await page.waitForFunction(() => window.__skyborne && window.__skyborne.city.status === 'live', null, { timeout: 60000 });
  log({ what: 'page live', at: Date.now() });
  // checked in the page every 20 ms; each milestone is reported once, the moment it's true
  await page.evaluate((sid) => {
    const seen = new Set();
    const mark = (what, extra) => { if (!seen.has(what)) { seen.add(what); window.skyborneSeen({ what, at: Date.now(), ...extra }); } };
    setInterval(() => {
      const d = window.__skyborne.city.districts.get(sid);
      if (!d || d.leaving) { if (seen.has('district')) mark('gone'); return; }
      const doc = d.doc || {}, lead = (doc.agents || [])[0] || {};
      const bots = [...d.robots.values()].filter((b) => !b.leaving);
      mark('district', { title: doc.title });
      if ((lead.tools || 0) > 0) mark('tool', { activity: lead.activity });
      if (bots.length > 1) mark('helpers', { bots: bots.length });
      if (doc.waiting) mark('waiting', { tool: doc.waiting.tool });
      if (seen.has('waiting') && !doc.waiting) mark('waiting cleared');
      if ((doc.tokens?.total || 0) > 0) mark('tokens', { tokens: doc.tokens.total });
      if (bots.length > 1 && bots.slice(1).every((b) => b.data.status === 'done')) mark('helpers done');
    }, 20);
  }, sid);
  process.on('SIGTERM', async () => {
    log({ what: 'stopped', at: Date.now(), offMachine });
    try { await shot('final'); await browser.close(); } catch (e) {} // the window may already be closing
    process.exit(0);
  });
})();
