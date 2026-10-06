// Serves the dev preview (dist/preview.html, with the fake city) and the bundled assets on 127.0.0.1.
// Browsers won't load module scripts from file://, so the preview needs a server.
//   node dev/serve.js [port]      then open http://127.0.0.1:8000/
// The smoke test starts it in-process: require('./dev/serve').start(0) -> Promise<server>.
const fs = require('fs');
const http = require('http');
const path = require('path');

const DIST = path.resolve(__dirname, '..', 'dist');
const ASSETS = path.resolve(__dirname, '..', '..', 'skyborne', 'web', 'assets');
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml' };

function resolve(urlPath) {
  const p = decodeURIComponent(urlPath);
  if (p === '/' || p === '/preview.html') return path.join(DIST, 'preview.html');
  if (p.startsWith('/assets/')) return path.join(ASSETS, p.slice('/assets/'.length));
  if (p.startsWith('/dist/')) return path.join(DIST, p.slice('/dist/'.length)); // e.g. a recording to ?play=
  return null;
}

function start(port = 8000) {
  const server = http.createServer((req, res) => {
    let file = null;
    try { file = resolve(new URL(req.url, 'http://x').pathname); } catch (e) {}
    const inside = file && [DIST, ASSETS].some((root) => path.resolve(file).startsWith(root + path.sep));
    if (!inside || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); res.end('Not found'); return; }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((ok) => server.listen(port, '127.0.0.1', () => ok(server)));
}

module.exports = { start };
if (require.main === module) {
  start(Number(process.argv[2]) || 8000).then((s) => console.log(`Preview at http://127.0.0.1:${s.address().port}/`));
}
