// Worker でもメインスレッドでも同じ手順で変換を回す共通モジュール。
// kuromoji(UMD)は呼び出し側が先に読み込んでおくこと(worker: importScripts、
// ページ: <script src="/kuromoji.js">)。
function scale(m, f) {
  const out = {};
  for (const k1 in m) { out[k1] = {}; for (const k2 in m[k1]) out[k1][k2] = m[k1][k2] * f; }
  return out;
}

const getJson = (url) => fetch(url).then((r) => r.json());

export async function run(cfg) {
  const timings = {};
  const t0 = performance.now();

  // 読みをサーバから注入する構成では kuromoji 辞書(約17MB)自体が要らない。
  // 単語リスト側の getYomi は pronunciation に漢字を含む行でしか呼ばれないので、
  // そういう行が無いリスト(stations等)ならトークナイザ抜きで完結する。
  const useKuromoji = cfg.tokenizer !== 'injected';
  const kuromoji = globalThis.kuromoji;
  const tokenizerPromise = useKuromoji
    ? new Promise((resolve, reject) => {
      kuromoji.builder({ dicPath: '/kuromoji/dict/' })
        .build((err, tk) => (err ? reject(err) : resolve(tk)));
    })
    : Promise.resolve(null);
  const [{ createSoramimic }, { KuromojiTokenizer }] = await Promise.all([
    import('/lib/index.js'), import('/lib/kuromojiTokenizer.js'),
  ]);
  const [kanjiDict, englishDict, romanTree, kana2phonon, vowelSim, consonantSim] =
    await Promise.all([
      getJson('/data/kanjiyomi.json'), getJson('/data/english-kana.json'),
      getJson('/data/tree_roma2kana.json'), getJson('/data/kana2phonon.json'),
      getJson('/data/simVowelsMonoTie.json'), getJson('/data/simConsonantsMonoTie.json'),
    ]);
  timings.data_ms = performance.now() - t0;

  const tk = await tokenizerPromise;
  timings.kuromoji_ms = performance.now() - t0;
  const mecab = tk
    ? KuromojiTokenizer(tk)
    : { tokenize: null, getYomi: () => { throw new Error('getYomi: トークナイザ無し'); } };

  // MeCab注入モード: Nodeブリッジと同じ生トークンを差し込んで読みソースを揃える
  let tokenizeSentenses = mecab.tokenize;
  if (cfg.tokenizer === 'injected') {
    const dump = await getJson('/out/mecab_tokens.json');
    tokenizeSentenses = (texts) => {
      const arr = Array.isArray(texts) ? texts : [texts];
      const out = arr.map((x) => JSON.parse(JSON.stringify(dump[x])));
      return Array.isArray(texts) ? out : out[0];
    };
  }

  const r = Math.min(0.9, Math.max(0.1, Number(cfg.params.VOWEL_RATIO) || 0.8));
  const app = createSoramimic({
    kanjiDict, englishDict, romanTree, kana2phonon,
    vowelSimilarity: scale(vowelSim, 2 * r),
    consonantSimilarity: scale(consonantSim, 2 * (1 - r)),
    tokenizeSentenses, getYomi: mecab.getYomi,
  });
  timings.app_ms = performance.now() - t0;

  const t1 = performance.now();
  const csvText = await (await fetch(cfg.wordlistUrl)).text();
  timings.csv_fetch_ms = performance.now() - t1;

  const t2 = performance.now();
  const db = app.wordList.parseTidy(csvText, cfg.where ?? undefined);
  timings.db_ms = performance.now() - t2;

  const t3 = performance.now();
  const tokensList = app.textAnalyzer.tokenizeTogether(cfg.phrases);
  timings.tokenize_ms = performance.now() - t3;

  const t4 = performance.now();
  const results = await new Promise((resolve) => {
    app.soramimiMaker.generateFromTokens(tokensList, db, cfg.params, null, resolve);
  });
  timings.generate_ms = performance.now() - t4;
  timings.total_ms = performance.now() - t1;

  return { timings, surfaces: results.map((ws) => ws.map((w) => w.surface)) };
}
