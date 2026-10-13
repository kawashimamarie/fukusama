// 公開成果物の表示確認（PC 1440px・スマートフォン 390px）。実際のブラウザ（Chromium）で全ページを開く。
//   node scripts/qa/browser_check.mjs [out/] [スクリーンショットの出力先]
// 不合格：JavaScript のエラー・同じサイトのファイルの読み込み失敗・横スクロール（はみ出し）・スクロールしても表示されない要素
// 外部（Google Fonts など）の読み込みは判定に使わない。
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const out = path.resolve(process.argv[2] || 'out');
const shots = process.argv[3] ? path.resolve(process.argv[3]) : null;
if (shots) fs.mkdirSync(shots, { recursive: true });

const SITES = [
  { domain: 'ebisenka.jp', port: 8901, pages: ['/', '/wholesale/', '/bento/', '/kitchen/', '/about/', '/contact/', '/contact/thanks/', '/privacy/', '/404.html'] },
  { domain: 'ebisenka-seavans.com', port: 8902, pages: ['/', '/bento/', '/kitchen/', '/wholesale/', '/access/', '/contact/', '/404.html'] },
  { domain: 'fukushin-e.co.jp', port: 8903, pages: ['/', '/business.html', '/restaurant.html', '/liquor.html', '/food-import.html', '/company.html', '/recruit.html', '/contact.html'] },
];
// 既存の公開中サイトにもともとある事象（今回の変更ではない）。警告として表示し、不合格にはしない
// （liquor.html のスマートフォンの 12px のはみ出しは 2026-10-13 に修正したため、一覧から外した＝再発したら不合格）
const KNOWN = [];

const servers = SITES.map(s => spawn('python3', ['-m', 'http.server', String(s.port), '--bind', '127.0.0.1', '--directory', path.join(out, s.domain)], { stdio: 'ignore' }));
await new Promise(r => setTimeout(r, 1200));
const problems = [], warnings = [];
const browser = await chromium.launch();
try {
  for (const [vp, size] of [['pc', { width: 1440, height: 900 }], ['sp', { width: 390, height: 844 }]]) {
    const ctx = await browser.newContext({ viewport: size, isMobile: vp === 'sp', deviceScaleFactor: 1 });
    for (const s of SITES) {
      const base = `http://127.0.0.1:${s.port}`;
      for (const pg of s.pages) {
        const p = await ctx.newPage();
        const errs = [];
        p.on('pageerror', e => errs.push('JS ' + e.message));
        p.on('response', r => { if (r.url().startsWith(base) && r.status() >= 400 && !r.url().includes('404.html')) errs.push(`HTTP ${r.status()} ${r.url().slice(base.length)}`); });
        await p.goto(base + pg, { waitUntil: 'load' });
        await p.evaluate(async () => { for (let y = 0; y < document.body.scrollHeight; y += 300) { scrollTo(0, y); await new Promise(r => setTimeout(r, 40)); } scrollTo(0, 0); });
        await p.waitForTimeout(900);
        const r = await p.evaluate(() => ({
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          hidden: [...document.querySelectorAll('.reveal, .reveal-media')].filter(e => !e.classList.contains('is-visible')).length,
        }));
        const where = `${s.domain}${pg}（${vp}）`;
        const known = k => KNOWN.some(x => x.domain === s.domain && x.page === pg && x.vp === vp && x.kind === k);
        if (r.overflow > 1) (known('overflow') ? warnings : problems).push(`${where}：横にはみ出し ${r.overflow}px`);
        if (r.hidden) problems.push(`${where}：スクロールしても表示されない要素 ${r.hidden} 件`);
        for (const e of errs) problems.push(`${where}：${e}`);
        if (shots && (pg === '/' || s.domain !== 'fukushin-e.co.jp')) {
          await p.screenshot({ path: path.join(shots, `${s.domain}${pg.replace(/[\/.]+/g, '_')}-${vp}.png`), fullPage: true });
        }
        await p.close();
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
  servers.forEach(s => s.kill());
}
for (const w of warnings) console.log('注意（既存）：' + w);
if (problems.length) { console.log('【不合格】\n  ' + problems.join('\n  ')); process.exit(1); }
console.log(`表示確認：合格（${SITES.reduce((n, s) => n + s.pages.length, 0)} ページ × PC・スマートフォン）`);
