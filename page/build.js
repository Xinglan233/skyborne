// Builds the Skyborne page. Everything it needs is bundled: no CDN, no web fonts from elsewhere.
//   ../skyborne/web/index.html  -> the city the local server serves at / (it puts this launch's token
//                                  in place of __SKYBORNE_TOKEN__); shipped inside the Python package
//   ../skyborne/web/assets/     -> three.js 0.170 and the addons the page imports, the 4 fonts
//                                  (latin and latin-ext), their licenses, and the tab icons (src/icons;
//                                  `node dev/icons.js` makes the PNGs from the SVG)
//   dist/preview.html           -> the same page with a fake, live city (dev/fake-city.js) for local
//                                  work; serve it with `node dev/serve.js`
// The output only depends on the sources and node_modules (no dates), so CI can check it's current.
// Usage: node build.js
const fs = require('fs');
const path = require('path');
const here = (...p) => path.join(__dirname, ...p);
const mod = (...p) => here('node_modules', ...p);
const WEB = here('..', 'skyborne', 'web');
const ASSETS = path.join(WEB, 'assets');

// order matters: later files use what earlier ones define
const ORDER = ['01-core.js', '02-world.js', '03a-blocks.js', '03-district.js', '03b-transit.js', '04-robot.js', '05-city.js', '05b-detail.js', '06-ui.js'];
// the families the page's CSS and canvases name, and the Fontsource package each comes from
const FONTS = [
  { family: 'Inter', pkg: 'inter', file: 'inter' },
  { family: 'Unbounded', pkg: 'unbounded', file: 'unbounded' },
  { family: 'JetBrains Mono', pkg: 'jetbrains-mono', file: 'jetbrains-mono' },
  { family: 'Martian Mono', pkg: 'martian-mono', file: 'martian-mono' },
];
const SUBSETS = ['latin', 'latin-ext'];

const write = (file, data) => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, data); };
const copy = (from, to) => { fs.mkdirSync(path.dirname(to), { recursive: true }); fs.copyFileSync(from, to); };
fs.rmSync(ASSETS, { recursive: true, force: true }); // nothing stale survives a rebuild

const app = ORDER.map((f) => fs.readFileSync(here('src', f), 'utf8')).join('\n');
const body = fs.readFileSync(here('src', 'page.html'), 'utf8').replace('/*APP*/', () => app);

// three.js: the module build, plus each addon the app imports and every file those import in turn
const three = JSON.parse(fs.readFileSync(mod('three', 'package.json'), 'utf8'));
if (three.version !== '0.170.0') throw new Error(`three ${three.version} installed; the page is built for 0.170.0 (run npm ci)`);
copy(mod('three', 'build', 'three.module.min.js'), path.join(ASSETS, 'three', 'three.module.min.js'));
const addons = new Set();
const todo = [...app.matchAll(/from 'three\/addons\/([^']+)'/g)].map((m) => m[1]);
while (todo.length) {
  const rel = path.posix.normalize(todo.pop());
  if (addons.has(rel)) continue;
  addons.add(rel);
  const src = fs.readFileSync(mod('three', 'examples', 'jsm', ...rel.split('/')), 'utf8');
  for (const m of src.matchAll(/(?:import|export)[^'"]*?from\s*['"](\.{1,2}\/[^'"]+)['"]/g)) todo.push(path.posix.join(path.posix.dirname(rel), m[1]));
}
for (const rel of [...addons].sort()) copy(mod('three', 'examples', 'jsm', ...rel.split('/')), path.join(ASSETS, 'three', 'addons', ...rel.split('/')));
copy(mod('three', 'LICENSE'), path.join(ASSETS, 'licenses', 'three.js-LICENSE.txt'));
for (const f of ['icon.svg', 'icon-32.png', 'apple-touch-icon.png']) copy(here('src', 'icons', f), path.join(ASSETS, 'icons', f));

// fonts: the variable (wght) files for the subsets the UI's text uses, under the names the page already uses
const faces = [];
for (const f of FONTS) {
  const css = fs.readFileSync(mod('@fontsource-variable', f.pkg, 'wght.css'), 'utf8');
  for (const sub of SUBSETS) {
    const file = `${f.file}-${sub}-wght-normal.woff2`;
    const block = css.split('@font-face').find((b) => b.includes(`/${file})`));
    if (!block) throw new Error(`no ${file} in @fontsource-variable/${f.pkg}`);
    const weight = /font-weight:\s*([^;]+);/.exec(block)[1].trim();
    const range = /unicode-range:\s*([^;]+);/.exec(block)[1].trim();
    copy(mod('@fontsource-variable', f.pkg, 'files', file), path.join(ASSETS, 'fonts', file));
    faces.push(`@font-face{font-family:'${f.family}';font-style:normal;font-display:swap;font-weight:${weight};`
      + `src:url(./assets/fonts/${file}) format('woff2');unicode-range:${range}}`);
  }
  copy(mod('@fontsource-variable', f.pkg, 'LICENSE'), path.join(ASSETS, 'licenses', `${f.family.replace(/ /g, '')}-OFL.txt`));
}

const importMap = JSON.stringify({ imports: { three: './assets/three/three.module.min.js', 'three/addons/': './assets/three/addons/' } });
const doc = (headExtra, bodyStart) => '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
  + '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
  + '<link rel="icon" href="./assets/icons/icon-32.png" sizes="32x32" type="image/png">\n'
  + '<link rel="icon" href="./assets/icons/icon.svg" type="image/svg+xml">\n'
  + '<link rel="apple-touch-icon" href="./assets/icons/apple-touch-icon.png">\n'
  + headExtra + '<title>Skyborne</title>\n'
  + '<style>\n' + faces.join('\n') + '\nbody{margin:0;background:#9fc3dd}[hidden]{display:none!important}\n</style>\n'
  + `<script type="importmap">${importMap}</script>\n`
  + '</head><body>\n' + bodyStart + body + '\n</body></html>\n';

write(path.join(WEB, 'index.html'), doc('<meta name="skyborne-token" content="__SKYBORNE_TOKEN__">\n', ''));
const fake = fs.readFileSync(here('dev', 'fake-city.js'), 'utf8');
write(here('dist', 'preview.html'), doc('', '<script>\n' + fake + '\n</script>\n'));

const size = (dir) => fs.readdirSync(dir, { withFileTypes: true }).reduce((n, e) => n + (e.isDirectory() ? size(path.join(dir, e.name)) : fs.statSync(path.join(dir, e.name)).size), 0);
console.log(`built skyborne/web (page ${(fs.statSync(path.join(WEB, 'index.html')).size / 1024).toFixed(0)} KB, assets ${(size(ASSETS) / 1024).toFixed(0)} KB: `
  + `${addons.size} three.js addons, ${faces.length} font files) and dist/preview.html`);
