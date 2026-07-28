// soramimic_video の run_convert(Python)と同じ入出力を、本家JSエンジン
// (external/soramimic/frontend/src/lib)で実現する薄いブリッジ(スパイク用)。
//
//   node spike/node/convert.mjs <job.json>
//
// job.json:
//   {
//     "phrases":     ["ウッセエワ...", ...],   // 変換対象(行ごとのカナ)
//     "wordlistCsv": "external/soramimic-wordlists/stations.csv",
//     "where":       "type=family or ..." | null,
//     "params":      {"VOWEL_RATIO":0.8, ...},
//     "tokenizer":   "kuromoji" | "injected",
//     "tokensFile":  "...json",   // tokenizer=injected のとき: Python MeCab の生トークン
//     "out":         "result.json"
//   }
//
// 出力(out):  {lines:[{units,words}], tokensList, phrases, timings:{...}}
//   = soramimic_engine.run_convert() の戻り値 + 計測値
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');            // worktree ルート
const SORA = path.join(REPO, 'external/soramimic');  // 本家 submodule

const t0 = Date.now();
const marks = {};
const mark = (k) => { marks[k] = Date.now() - t0; };

function loadJson(rel) {
  return JSON.parse(fs.readFileSync(path.join(SORA, rel), 'utf8'));
}

// 移植元コードは console.log を多用するので黙らせる(harness-lib.mjs と同じ)
const realLog = console.log.bind(console);
for (const k of ['log', 'time', 'timeEnd', 'timeLog', 'warn']) console[k] = () => {};

async function kuromojiTokenizer() {
  const req = createRequire(pathToFileURL(path.join(SORA, 'frontend/package.json')).href);
  const kuromoji = req('kuromoji');
  const dicPath = path.join(SORA, 'frontend/node_modules/kuromoji/dict');
  const raw = await new Promise((res, rej) =>
    kuromoji.builder({ dicPath }).build((e, tk) => (e ? rej(e) : res(tk))));
  const { KuromojiTokenizer } = await import(
    pathToFileURL(path.join(SORA, 'frontend/src/lib/kuromojiTokenizer.js')).href);
  return KuromojiTokenizer(raw);
}

// Python(fugashi/ipadic MeCab)が出した生トークンをそのまま返すトークナイザ。
// tokenizeTogether は「texts → tokens_list」しか要求しないので、
// 事前に同じ phrases で作ったトークン列を順番に返せば読みソースを揃えられる。
function injectedTokenizer(tokensFile) {
  const dump = JSON.parse(fs.readFileSync(tokensFile, 'utf8')); // {phrase: tokens}
  return {
    tokenize: (texts) => {
      const arr = Array.isArray(texts) ? texts : [texts];
      const out = arr.map((t) => {
        if (!(t in dump)) throw new Error(`injected tokens missing for: ${t}`);
        return JSON.parse(JSON.stringify(dump[t])); // formatTokensList が破壊的なのでコピー
      });
      return Array.isArray(texts) ? out : out[0];
    },
    // parseTidy は pronunciation に漢字を含む行だけ getYomi を呼ぶ。
    // 対象リストにはほぼ無い(scientist の2行のみ)のでフォールバックで足りる。
    getYomi: null,
  };
}

async function main() {
  const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

  const { createSoramimic } = await import(
    pathToFileURL(path.join(SORA, 'frontend/src/lib/index.js')).href);

  const kuro = await kuromojiTokenizer();
  mark('tokenizer_ready');

  const inj = job.tokenizer === 'injected' ? injectedTokenizer(job.tokensFile) : null;
  const tokenizeSentenses = inj ? inj.tokenize : kuro.tokenize;
  const getYomi = kuro.getYomi;

  const baseInputs = {
    kanjiDict: loadJson('data/kanjiyomi.json'),
    englishDict: loadJson('data/english-kana.json'),
    romanTree: loadJson('data/tree_roma2kana.json'),
    kana2phonon: loadJson('data/kana2phonon.json'),
    tokenizeSentenses,
    getYomi,
  };
  const vowelSimilarity = loadJson('data/simVowelsMonoTie.json');
  const consonantSimilarity = loadJson('data/simConsonantsMonoTie.json');
  mark('data_loaded');

  // appCore.js / harness-lib.mjs と同じ appFor(r)
  const scale = (m, f) => {
    const out = {};
    for (const k1 in m) { out[k1] = {}; for (const k2 in m[k1]) out[k1][k2] = m[k1][k2] * f; }
    return out;
  };
  const r = Math.min(0.9, Math.max(0.1, Number(job.params?.VOWEL_RATIO) || 0.8));
  const app = createSoramimic({
    ...baseInputs,
    vowelSimilarity: scale(vowelSimilarity, 2 * r),
    consonantSimilarity: scale(consonantSimilarity, 2 * (1 - r)),
  });
  mark('app_ready');

  const csvText = fs.readFileSync(job.wordlistCsv, 'utf8');
  const tDb = Date.now();
  const db = app.wordList.parseTidy(csvText, job.where ?? undefined);
  const dbMs = Date.now() - tDb;
  mark('db_ready');

  // run_convert と同じ経路: tokenizeTogether → generateFromTokens
  const tTok = Date.now();
  const tokensList = app.textAnalyzer.tokenizeTogether(job.phrases);
  const tokMs = Date.now() - tTok;
  mark('tokenized');

  const unitsList = job.phrases.map(() => []);
  const tGen = Date.now();
  const results = await new Promise((resolve) => {
    app.soramimiMaker.generateFromTokens(
      tokensList, db, job.params, (result, i, tokenizedPhrases) => {
        unitsList[i] = tokenizedPhrases[i].map((u) => ({
          surface_form: u.surface_form,
          pronunciation: u.pronunciation,
          phrase: u.phrase,
        }));
      }, resolve);
  });
  const genMs = Date.now() - tGen;
  mark('generated');

  const lines = results.map((words, i) => ({ units: unitsList[i], words }));
  const out = {
    lines, tokensList, phrases: job.phrases,
    timings: {
      total_ms: Date.now() - t0,
      db_ms: dbMs, tokenize_ms: tokMs, generate_ms: genMs, marks,
    },
  };
  fs.writeFileSync(job.out, JSON.stringify(out));
  realLog(JSON.stringify(out.timings));
}

main().catch((e) => { console.error(e); process.exit(1); });
