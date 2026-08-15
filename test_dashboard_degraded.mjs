/**
 * Source-failure regression: one unavailable leg must not blank the section.
 *
 * Takes the generated index.html, blanks the embedded reserves series (the way a
 * failed H.4.1 + FRED fetch would), and asserts the other two panels still
 * render, the empty panel says so in words, and nothing throws.
 *
 *   node test_dashboard_degraded.mjs [index.html]
 */
import { chromium } from 'playwright';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const src = process.argv[2] || 'index.html';
if (!existsSync(resolve(src))) {
  console.error(`FAIL: target not found: ${src}`);
  process.exit(1);
}
const html = readFileSync(resolve(src), 'utf8');
const match = html.match(/var FPR=\{.*?\},FPT=/s);
if (!match) {
  console.error('FAIL: embedded reserves series (var FPR=...) not found in artifact');
  process.exit(1);
}
const degraded = html.replace(match[0], 'var FPR={"dates":[],"values":[],"available":false},FPT=');
const out = join(tmpdir(), 'mfd-degraded.html');
writeFileSync(out, degraded);

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name);
};

const launchOpts = process.env.PLAYWRIGHT_CHROMIUM_PATH
  ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
  : {};
const browser = await chromium.launch(launchOpts);
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(pathToFileURL(out).href, { waitUntil: 'load' });
  await page.waitForFunction(() => {
    const el = document.getElementById('chart-fp-tga');
    return !!el && !!el.querySelector('.main-svg');
  }, null, { timeout: 60000 });

  const state = await page.evaluate(() => ({
    reservesText: document.getElementById('chart-fp-reserves').textContent.trim(),
    reservesHasChart: !!document.getElementById('chart-fp-reserves').querySelector('.main-svg'),
    sofrHasChart: !!document.getElementById('chart-sofr-iorb').querySelector('.main-svg'),
    tgaHasChart: !!document.getElementById('chart-fp-tga').querySelector('.main-svg'),
    cards: document.querySelectorAll('.fp-card').length,
    score: (document.querySelector('.fp-score b') || {}).textContent,
  }));

  check('reserves panel shows an explicit unavailable message', /unavailable/i.test(state.reservesText),
    state.reservesText.slice(0, 80));
  check('reserves panel draws no fabricated series', !state.reservesHasChart);
  check('SOFR panel still renders', state.sofrHasChart);
  check('TGA panel still renders', state.tgaHasChart);
  check('all three cards still present', state.cards === 3, String(state.cards));

  // Range controls must survive a missing panel.
  await page.click('.sofr-range[data-range="1Y"]');
  const spans = await page.evaluate(() => {
    const toMs = (v) => (typeof v === 'number' ? v : Date.parse(v));
    return ['chart-sofr-iorb', 'chart-fp-tga'].map((id) => {
      const el = document.getElementById(id);
      const r = el._fullLayout.xaxis.range.map(toMs);
      return r[1] - r[0];
    });
  });
  check('range control still drives the remaining panels',
    spans.every((s) => s > 0) && Math.abs(spans[0] - spans[1]) < 864e5,
    spans.map((s) => Math.round(s / 864e5) + 'd').join(' '));
  check('no console errors in the degraded state', errors.length === 0, errors.join(' :: ').slice(0, 200));
  await context.close();
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`\n${failures.length} degraded-source check(s) failed:\n- ` + failures.join('\n- '));
  process.exit(1);
}
console.log('\nAll degraded-source checks passed.');
