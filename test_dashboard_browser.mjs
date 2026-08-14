/**
 * Browser-level regression checks for the generated dashboard.
 *
 * Guards the production failure where the SOFR-IORB panel rendered a chart but
 * every KPI collapsed to an em dash on a frozen upstream series. Runs against
 * the deployment-style static artifact (index.html) or any live URL.
 *
 *   node test_dashboard_browser.mjs [fileOrUrl] [--max-stale-days=6]
 */
import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const args = process.argv.slice(2);
const staleArg = args.find((a) => a.startsWith('--max-stale-days='));
const MAX_STALE_DAYS = staleArg ? Number(staleArg.split('=')[1]) : 6;
const target = args.find((a) => !a.startsWith('--')) || 'index.html';
const url = /^https?:/.test(target)
  ? target
  : (existsSync(resolve(target)) ? pathToFileURL(resolve(target)).href : null);
if (!url) {
  console.error(`FAIL: target not found: ${target}`);
  process.exit(1);
}

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(name + (detail ? ': ' + detail : ''));
};

const viewports = [
  { label: 'desktop', width: 1440, height: 1000, isMobile: false },
  { label: 'mobile', width: 390, height: 844, isMobile: true },
];

const browser = await chromium.launch();
try {
  for (const vp of viewports) {
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      isMobile: vp.isMobile,
      hasTouch: vp.isMobile,
    });
    const page = await context.newPage();
    const consoleErrors = [];
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));

    await page.goto(url, { waitUntil: 'load' });
    await page.waitForFunction(() => {
      const el = document.getElementById('chart-sofr-iorb');
      return !!el && (!!el.querySelector('.main-svg') || el.textContent.includes('unavailable'));
    }, null, { timeout: 60000 });

    const state = await page.evaluate(() => {
      const el = document.getElementById('chart-sofr-iorb');
      const bars = document.getElementById('chart-sofr-change');
      const panel = document.querySelector('.sofr-panel');
      const rect = el.getBoundingClientRect();
      const barRect = bars ? bars.getBoundingClientRect() : { width: 0, height: 0 };
      const kpis = [...panel.querySelectorAll('.sofr-kpi')].map((k) => ({
        label: k.querySelector('label').textContent.trim(),
        value: k.querySelector('b').textContent.trim(),
      }));
      const traces = (el.data || []).map((t) => ({
        name: t.name, mode: t.mode, type: t.type,
        points: (t.x || []).length,
        finite: (t.y || []).filter((v) => typeof v === 'number' && isFinite(v)).length,
      }));
      const drawn = el.querySelectorAll('.scatterlayer .trace path.js-line, .scatterlayer .points path').length;
      return {
        hasChart: !!el.querySelector('.main-svg'),
        width: rect.width, height: rect.height,
        barWidth: barRect.width, barHeight: barRect.height,
        traces,
        drawn,
        barPoints: bars && bars.data ? (bars.data[0].x || []).length : 0,
        lastDate: traces.length ? el.data[0].x[el.data[0].x.length - 1] : null,
        xTickLabels: [...el.querySelectorAll('.xtick text')].map((t) => t.textContent).slice(0, 4),
        yTickLabels: [...el.querySelectorAll('.ytick text')].map((t) => t.textContent).slice(0, 4),
        kpis,
        rangeButtons: [...document.querySelectorAll('.sofr-range')].map((b) => b.dataset.range),
      };
    });

    const tag = `[${vp.label}]`;
    check(`${tag} SOFR chart rendered`, state.hasChart);
    check(`${tag} chart has non-zero dimensions`, state.width > 200 && state.height > 100,
      `${Math.round(state.width)}x${Math.round(state.height)}`);
    check(`${tag} daily-change chart has non-zero dimensions`, state.barWidth > 200 && state.barHeight > 60,
      `${Math.round(state.barWidth)}x${Math.round(state.barHeight)}`);
    check(`${tag} three series present (raw, filtered median, calendar noise)`, state.traces.length === 3,
      JSON.stringify(state.traces.map((t) => t.name)));
    check(`${tag} raw series has finite data points`, (state.traces[0]?.finite || 0) > 20,
      String(state.traces[0]?.finite));
    check(`${tag} filtered median series has finite data points`, (state.traces[1]?.finite || 0) > 20,
      String(state.traces[1]?.finite));
    check(`${tag} calendar-noise markers present`, (state.traces[2]?.points || 0) > 0,
      String(state.traces[2]?.points));
    check(`${tag} visible series drawn in SVG`, state.drawn > 0, String(state.drawn));
    check(`${tag} daily-change bars have data`, state.barPoints > 20, String(state.barPoints));
    check(`${tag} axis labels present`, state.xTickLabels.length > 1 && state.yTickLabels.length > 1,
      `${state.xTickLabels.join('|')} / ${state.yTickLabels.join('|')}`);

    // KPI strip must show real values whenever the series has data.
    const headline = state.kpis.slice(0, 3);
    check(`${tag} KPI strip shows values, not em dashes`,
      headline.every((k) => k.value && k.value !== '\u2014'),
      JSON.stringify(headline));

    // Observation recency.
    if (state.lastDate) {
      const ageDays = Math.floor((Date.now() - Date.parse(state.lastDate + 'T00:00:00Z')) / 86400000);
      check(`${tag} latest observation within ${MAX_STALE_DAYS} days`, ageDays <= MAX_STALE_DAYS,
        `${state.lastDate} (${ageDays}d old)`);
    } else {
      check(`${tag} latest observation available`, false, 'no series data');
    }

    // Range controls.
    check(`${tag} 3M/6M/1Y/3Y controls present`,
      ['3M', '6M', '1Y', '3Y'].every((r) => state.rangeButtons.includes(r)),
      state.rangeButtons.join(','));
    const spans = {};
    for (const r of ['3M', '6M', '1Y', '3Y']) {
      await page.click(`.sofr-range[data-range="${r}"]`);
      await page.waitForFunction((range) => {
        const b = document.querySelector('.sofr-range.active');
        return b && b.dataset.range === range;
      }, r, { timeout: 5000 });
      const res = await page.evaluate(() => {
        const c = document.getElementById('chart-sofr-iorb');
        const b = document.getElementById('chart-sofr-change');
        const toMs = (v) => (typeof v === 'number' ? v : Date.parse(v));
        const cr = c._fullLayout.xaxis.range.map(toMs);
        const br = b._fullLayout.xaxis.range.map(toMs);
        return { span: cr[1] - cr[0], barSpan: br[1] - br[0] };
      });
      spans[r] = res.span;
      check(`${tag} ${r} control applies a range`, res.span > 0 && Math.abs(res.span - res.barSpan) < 864e5,
        `${Math.round(res.span / 864e5)}d, bars ${Math.round(res.barSpan / 864e5)}d`);
    }
    check(`${tag} ranges are ordered 3M < 6M < 1Y < 3Y`,
      spans['3M'] < spans['6M'] && spans['6M'] < spans['1Y'] && spans['1Y'] < spans['3Y'],
      Object.entries(spans).map(([k, v]) => `${k}=${Math.round(v / 864e5)}d`).join(' '));

    // Tooltip must contain a real basis-point value.
    await page.click('.sofr-range[data-range="6M"]');
    const box = await page.locator('#chart-sofr-iorb .nsewdrag').boundingBox();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5);
    await page.waitForTimeout(400);
    const hover = await page.evaluate(() =>
      [...document.querySelectorAll('#chart-sofr-iorb .hoverlayer text')].map((t) => t.textContent).join(' | '));
    check(`${tag} tooltip shows numeric bp values`, /-?\d+(\.\d+)?\s*bp/.test(hover), hover.slice(0, 160));

    check(`${tag} no console errors`, consoleErrors.length === 0, consoleErrors.join(' :: ').slice(0, 200));
    await context.close();
  }
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`\n${failures.length} browser check(s) failed:\n- ` + failures.join('\n- '));
  process.exit(1);
}
console.log('\nAll browser checks passed.');
