// Measures frames per second and draw calls for a city of N districts, in a real (headed) browser
// window on this machine's GPU. Not part of the tests: headless runs use software rendering.
//   node dev/fps.js [districts=60] [seconds=8]
const { chromium } = require('playwright');
const { start } = require('./serve');

(async () => {
  const n = Number(process.argv[2]) || 60, secs = Number(process.argv[3]) || 8;
  const server = await start(0);
  const browser = await chromium.launch({ headless: false, args: ['--ignore-gpu-blocklist'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.waitForFunction(() => window.__skyborne && window.__skyborne.city.status === 'live', null, { timeout: 30000 });
  const rec = { format: 'skyborne-recording', version: 1, session: { id: 'c-0', title: 'city' }, duration: 600000,
    frames: Array.from({ length: n }, (_, i) => ({ t: 0, id: 'c-' + i, doc: { v: 2, title: 'district-' + i, headline: 'Past work', startedAt: 0, updatedAt: -i * 60000, turns: 3 + (i % 20),
      tokens: { total: 50000 * (i + 1) }, waiting: null, feed: [], agents: [{ id: 'main', name: 'Skybot', role: 'Lead Agent', type: 'lead', status: 'idle', kind: 'idle', activity: 'Left the city', activitySince: 0, tools: 5 + i * 3 }] } })) };
  await page.evaluate((r) => window.__skyborne.playRecording(r), JSON.stringify(rec));
  await page.waitForTimeout(15000); // every district risen and built
  const out = await page.evaluate(async (secs) => {
    const r = window.__skyborne.renderer; r.info.autoReset = false; r.info.reset();
    const times = []; let last = performance.now();
    await new Promise((done) => { const t0 = last; const f = (now) => { times.push(now - last); last = now; if (now - t0 < secs * 1000) requestAnimationFrame(f); else done(); }; requestAnimationFrame(f); });
    const calls = r.info.render.calls / times.length; r.info.autoReset = true;
    times.sort((a, b) => a - b);
    return { fps: Math.round(times.length / secs), p50ms: +times[times.length >> 1].toFixed(1), p95ms: +times[Math.floor(times.length * 0.95)].toFixed(1),
      callsPerFrame: Math.round(calls), pixelRatio: +r.getPixelRatio().toFixed(2), districts: window.__skyborne.city.districts.size };
  }, secs);
  console.log(JSON.stringify({ districts: n, ...out }));
  await browser.close(); server.close();
})();
