// Makes the PNG tab icons from src/icons/icon.svg (the Skybot on its tile). Run it after changing the SVG,
// then `node build.js`, and commit both PNGs:
//   icon-32.png           the tab icon for browsers without SVG icons
//   apple-touch-icon.png  180 x 180, full-bleed (iOS rounds the corners itself)
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const DIR = path.resolve(__dirname, '..', 'src', 'icons');
const svg = fs.readFileSync(path.join(DIR, 'icon.svg'), 'utf8');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  for (const [file, size, bg] of [['icon-32.png', 32, 'transparent'], ['apple-touch-icon.png', 180, '#1B1D22']]) {
    await page.setContent(`<body style="margin:0"><div id="i" style="width:${size}px;height:${size}px;background:${bg}">${svg.replace('<svg ', `<svg width="${size}" height="${size}" `)}</div></body>`);
    await page.locator('#i').screenshot({ path: path.join(DIR, file), omitBackground: bg === 'transparent' });
  }
  await browser.close();
  console.log('made icon-32.png and apple-touch-icon.png in src/icons');
})();
