// soramimic の変換ロジック(external/soramimic devブランチの frontend/src/lib)を
// 完全オフライン(kuromoji)で実行するブリッジ。
// soramimic 本体の tests/golden/harness-lib.mjs と同じ組み立て方。
//
// 入力(stdin, JSON):
//   { phrases: ["シズムヨウニ", ...],
//     wordlist: { file: "path/to.csv", where: "type=family or ..." | null },
//     params: { LENGTH: 2, ... } }
// 出力(stdout, JSON):
//   { lines: [ { units: [{surface_form, pronunciation}, ...],
//                words: [{id, original, surface, kana, originalkana,
//                         original_surface, period, score, ...}, ...] } ] }
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const BRIDGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const SORAMIMIC_ROOT = path.join(BRIDGE_DIR, '../external/soramimic');

// 移植元コードは console.log を多用するので stdout を汚さないよう黙らせる
const printOut = (s) => process.stdout.write(s);
for (const k of ['log', 'time', 'timeEnd', 'timeLog', 'warn']) console[k] = () => {};

function loadJson(rel) {
  return JSON.parse(fs.readFileSync(path.join(SORAMIMIC_ROOT, rel), 'utf8'));
}

async function buildApp() {
  const libUrl = pathToFileURL(path.join(SORAMIMIC_ROOT, 'frontend/src/lib/index.js')).href;
  const { createSoramimic } = await import(libUrl);

  const require = createRequire(import.meta.url);
  const kuromoji = require('kuromoji');
  const dicPath = path.join(BRIDGE_DIR, 'node_modules/kuromoji/dict');
  const rawTokenizer = await new Promise((resolve, reject) => {
    kuromoji.builder({ dicPath }).build((err, tk) => (err ? reject(err) : resolve(tk)));
  });
  const { KuromojiTokenizer } = await import(
    pathToFileURL(path.join(SORAMIMIC_ROOT, 'frontend/src/lib/kuromojiTokenizer.js')).href);
  const tokenizer = KuromojiTokenizer(rawTokenizer);

  return createSoramimic({
    kanjiDict: loadJson('data/kanjiyomi.json'),
    englishDict: loadJson('data/bep-eng.json'),
    romanTree: loadJson('data/tree_roma2kana.json'),
    vowelSimilarity: loadJson('data/simVowelsSimple.json'),
    consonantSimilarity: loadJson('data/simConsonantsSimple.json'),
    kana2phonon: loadJson('data/kana2phonon.json'),
    tokenizeSentenses: tokenizer.tokenize,
    getYomi: tokenizer.getYomi,
  });
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const app = await buildApp();

  const csvText = fs.readFileSync(input.wordlist.file, 'utf8');
  const db = app.wordList.parseTidy(csvText, input.wordlist.where || '');

  // 生成画面(app.js)と同じ経路: トークナイズ→生成。tokensListは
  // editorの読み修正・部分再生成に必要なので出力にも含める
  const tokensList = app.textAnalyzer.tokenizeTogether(input.phrases);
  const unitsList = [];
  const results = await new Promise((resolve) => {
    app.soramimiMaker.generateFromTokens(
      tokensList,
      db,
      input.params || {},
      (result, i, tokenizedPhrases) => {
        // updateFuncで行ごとのユニット列(mora単位)を受け取る
        unitsList[i] = tokenizedPhrases[i].map((u) => ({
          surface_form: u.surface_form,
          pronunciation: u.pronunciation,
          phrase: u.phrase,
        }));
      },
      resolve,
    );
  });

  const lines = results.map((words, i) => ({
    units: unitsList[i] || [],
    words: words.map((w) => ({ ...w })),
  }));
  printOut(JSON.stringify({ lines, tokensList, phrases: input.phrases }));
}

main().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
