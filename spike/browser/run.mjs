// ヘッドレスChrome(playwright)でブラウザPoCを計測する。
//   node spike/browser/run.mjs [songId] [wordlist]
// CPUスロットリング(1x/4x/6x)を CDP Emulation.setCPUThrottlingRate で切り替え、
// 変換時間とメインスレッドのフレームギャップ、転送バイト数を記録する。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { start, transferLog } from './server.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const req = createRequire(path.join(REPO, 'external/soramimic/frontend/package.json'));
const { chromium } = req('playwright');

const SONG = process.argv[2] || 'ussewa';
const WORDLIST = process.argv[3] || 'stations';
const MODE = process.argv[4] || 'kuromoji'; // kuromoji | injected
const RATES = (process.argv[5] || '1,4,6').split(',').map(Number);
// worker | main。CDPのCPUスロットリングはレンダラのメインスレッドにしか効かない
// (Workerは別スレッドなので素通りする)ので、スマホ級CPUの再現は main で測る。
const WHERE = process.argv[6] || 'worker';

const PARAMS = {
  VOWEL_RATIO: 0.8, VARIATION_COST: 16, SAME_PHRASE_BREAK_REWARD: 0,
  MID_PHRASE_BREAK_PENALTY: 20, WORD_NUMBER_PENALTY: 20, DUPLICATE: false,
};

const phrasesAll = JSON.parse(
  fs.readFileSync(path.join(REPO, 'spike/out/phrases.json'), 'utf8'));

const { server, port } = await start();
const browser = await chromium.launch();
const out = { song: SONG, wordlist: WORDLIST, mode: MODE, where_run: WHERE, runs: [] };

for (const rate of RATES) {
  transferLog.length = 0;
  const page = await browser.newPage();
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate });
  await page.goto(`http://127.0.0.1:${port}/`);
  // 待たずに投げる(main実行だとページのJSが長時間ブロックされるため)
  await page.evaluate((cfg) => { void window.runPoC(cfg); }, {
    phrases: phrasesAll[SONG],
    wordlistUrl: `/wordlists/${WORDLIST}.csv`,
    where: null,
    params: PARAMS,
    tokenizer: MODE,
    where_run: WHERE,
  });
  const result = await page.waitForFunction(() => window.__result, null,
    { timeout: 20 * 60 * 1000, polling: 500 }).then((h) => h.jsonValue());
  if (!result.ok) throw new Error(result.error);

  const transfer = {};
  for (const t of transferLog) {
    const key = t.url.startsWith('/kuromoji/dict/') ? '/kuromoji/dict/*' : t.url;
    const e = transfer[key] || (transfer[key] = { raw: 0, sent: 0, count: 0 });
    e.raw += t.raw; e.sent += t.sent; e.count++;
  }
  out.runs.push({ cpu_throttle: rate, ...result, transfer });
  console.log(`[browser x${rate}] generate ${result.timings.generate_ms.toFixed(0)}ms` +
    ` / db ${result.timings.db_ms.toFixed(0)}ms` +
    ` / total ${result.timings.total_ms.toFixed(0)}ms` +
    ` / maxFrameGap ${result.main_thread.max_gap_ms.toFixed(1)}ms`);
  await page.close();
}

const dst = path.join(REPO, 'spike/out/browser');
fs.mkdirSync(dst, { recursive: true });
fs.writeFileSync(path.join(dst, `${WORDLIST}__${SONG}__${MODE}__${WHERE}.json`),
  JSON.stringify(out, null, 1));
await browser.close();
server.close();
