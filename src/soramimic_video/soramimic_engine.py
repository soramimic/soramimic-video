"""soramimic ライブラリを直接使う変換エンジン(旧 Node ブリッジ bridge/convert.mjs の置換)。

bridge/convert.mjs と同一の出力構造
    {lines: [{units, words}], tokensList, phrases}
を返す run_convert() を提供する。生成画面(app.js)と同じ経路
(トークナイズ → generate_from_tokens)で組み立てる。

本家 soramimic.com 現行版と同じく、類似度行列は monophone タイブレーク方式
(MonoTie #102)を使い、「音の合わせ方」(VOWEL_RATIO = r)は行列を
母音×2r・子音×2(1-r) に前処理して表現する(appCore.js の appFor 相当)。

app(同梱辞書データ + fugashi/ipadic MeCab トークナイザ)の構築は重いので、
辞書データ・トークナイザは一度だけ読み、r ごとの app をキャッシュする。
単語リストCSVの前処理(parse_tidy: 読み推定 → 音節バリエーション展開)も
行数が多いと支配的なコストになるため、プロセス内でLRUキャッシュする。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 「行ごとのユニット列(音節単位)」から「行ごとのユニット重み列」を作る関数。
# run_convert に渡すと、エンジン内部と同じユニット列を使って重みを計算できる
# (トークナイズをやり直さずに済む)。None を返せば重み無し(従来動作)。
UnitWeightsFunc = Callable[[list[list[dict[str, Any]]]], "list[list[float]] | None"]

_base: dict[str, Any] | None = None  # 辞書データ(monotie行列)+ トークナイザ
_apps: dict[str, Any] = {}  # r(小数2桁キー) → Soramimic インスタンス

DEFAULT_VOWEL_RATIO = 0.8  # 本家の既定(appCore.js appFor)


def _get_base() -> dict[str, Any]:
    """辞書データとトークナイザを遅延構築してキャッシュする。"""
    global _base
    if _base is None:
        from soramimic import load_default_data
        from soramimic.tokenizers.mecab import MeCabTokenizer

        _base = {
            "data": load_default_data(similarity="monotie"),
            "tokenizer": MeCabTokenizer(),
        }
    return _base


def _app_key(vowel_ratio: Any = None) -> str:
    """「音の合わせ方」r を _apps のキー(小数2桁)に正規化する。"""
    try:
        r = float(vowel_ratio)
    except (TypeError, ValueError):
        r = 0.0
    # JSの Number(vowelRatio) || 0.8 相当(0/NaN/未指定 → 既定)
    r = min(0.9, max(0.1, r or DEFAULT_VOWEL_RATIO))
    return f"{r:.2f}"


def _get_app(vowel_ratio: Any = None) -> Any:
    """「音の合わせ方」r に応じた soramimic アプリを返す(appCore.js の appFor)。"""
    from soramimic import create_soramimic, scale_similarity

    key = _app_key(vowel_ratio)
    r = float(key)
    if key not in _apps:
        base = _get_base()
        data = base["data"]
        tok = base["tokenizer"]
        # MeCabTokenizer.tokenize/get_yomi は str|list[str] を受ける overload 的な
        # シグネチャで、create_soramimic の list[str] 前提と厳密には合わないが実行時は問題ない
        _apps[key] = create_soramimic(
            **{
                **data,
                "vowel_similarity": scale_similarity(data["vowel_similarity"], 2 * r),
                "consonant_similarity": scale_similarity(
                    data["consonant_similarity"], 2 * (1 - r)
                ),
            },
            tokenize_sentenses=tok.tokenize,  # type: ignore[arg-type]
            get_yomi=tok.get_yomi,
        )
    return _apps[key]


# --- 単語リストDB(parse_tidy の結果)のプロセス内LRUキャッシュ ---
#
# parse_tidy は「漢字表記の読み推定 → 読みの音節バリエーション展開」を全行に対して
# 行うため、行数の多いリスト(動物辞書16,731行で約4分)では変換本体より桁違いに重い。
# 結果は同じ (app, CSV内容, where) なら決定的で、呼び出し先でも読み取り専用なので
# ジョブをまたいで使い回せる。
#
# 【破壊的変更が無いことの確認】
#   * ResultDB の値(Word)は str/float だけの浅い dict で、入れ子の可変オブジェクトが無い
#   * soramimic/maker.py が wordlist に触るのは `clen not in wordlist` と
#     `for w in wordlist[key]` の読み取りのみ(代入・pop は一切無い)
#   * get_similar_word は共有オブジェクトの書き換えを避けるため `{**w, "sim": sim}` と
#     コピーを作る(soramimic 側 issue #99 の修正)。generate_from_tokens が
#     `v["original_surface"] = ...` で書き込むのはこのコピーであってDB本体ではない
#   * run_convert の戻り値も _json_safe が dict(word) でコピーする
#   よって deepcopy は不要で、同じ db を複数ジョブ・複数スレッドで共有してよい。
#
# 【容量の決め方】DBは大きい。sekitsui.csv(16,731行/4.3MB)は 172万バリエーション =
# 実測RSS約6.6GB(1バリエーションあたり約3.9KB)。件数だけで上限を切ると大きいリストを
# 数本抱えた時点でOOMするので、バリエーション総数の予算でも切る。予算超過時はLRU順に
# 捨てるが、直近に入れた1本だけは(単体で予算を超えていても)必ず残す
# ——そうしないと大きいリストが永久にキャッシュされず、この最適化の意味が無くなる。
_DB_CACHE_MAXSIZE = 8
_DB_CACHE_MAX_VARIANTS = 2_000_000  # 約8GB相当
# キー: (appキー, CSVの絶対パス, mtime_ns, ファイルサイズ, where)
_DbCacheKey = tuple[str, str, int, int, str]
_db_cache: OrderedDict[_DbCacheKey, Any] = OrderedDict()
_db_cache_lock = threading.Lock()


def clear_db_cache() -> None:
    """単語リストDBキャッシュを空にする(主にテスト用)。"""
    with _db_cache_lock:
        _db_cache.clear()


def _db_variants(db: Any) -> int:
    """DBが保持する単語バリエーション数(メモリ量の代理指標)。"""
    return sum(len(v) for v in db.values())


def _evict_locked() -> None:
    """上限(件数・バリエーション総数)を満たすまでLRU順に捨てる。要ロック。"""
    total = sum(_db_variants(db) for db in _db_cache.values())
    while len(_db_cache) > 1 and (
        len(_db_cache) > _DB_CACHE_MAXSIZE or total > _DB_CACHE_MAX_VARIANTS
    ):
        _, dropped = _db_cache.popitem(last=False)
        total -= _db_variants(dropped)


def db_cache_key(app_key: str, wordlist_csv: Path, where: str) -> _DbCacheKey:
    """キャッシュキー。CSVの内容が変われば作り直せるよう mtime(ns)とサイズを含める。"""
    path = Path(wordlist_csv).resolve()
    st = path.stat()
    return (app_key, str(path), st.st_mtime_ns, st.st_size, where)


def _get_db(app: Any, app_key: str, wordlist_csv: Path, where: str) -> Any:
    """parse_tidy の結果をキャッシュ付きで返す。"""
    path = Path(wordlist_csv).resolve()
    key = db_cache_key(app_key, path, where)

    with _db_cache_lock:
        if key in _db_cache:
            _db_cache.move_to_end(key)
            return _db_cache[key]

    # 構築はロックの外で行う(数分かかることがあるので、その間ほかのスレッドの
    # キャッシュヒットまで止めない)。同一キーが同時に来ると二重に構築されるが、
    # parse_tidy は決定的で結果は読み取り専用なので正しさには影響しない。
    db = app.word_list.parse_tidy(path.read_text(encoding="utf-8"), where)

    with _db_cache_lock:
        _db_cache[key] = db
        _db_cache.move_to_end(key)
        _evict_locked()
    return db


# --- 起動時ウォームアップ ---
#
# キャッシュは「2回目以降が速い」だけなので、よく使うリストはサーバー起動直後に
# バックグラウンドで先に構築しておく。ユーザーから見た初回変換も速くなる。
WARMUP_ENV = "SORAMIMIC_WARMUP_WORDLISTS"  # カンマ区切りの単語リスト名


def warmup_wordlists(names: list[str], where: str = "") -> None:
    """指定の単語リストのDBを順に構築してキャッシュに載せる(同期・例外を出さない)。

    where は既定で空文字(絞り込み無し)。同梱Web UIはファセットが全ONのとき
    where="" を送るので、これが最も当たりやすいキーになる。
    """
    from .convert import resolve_wordlist

    app_key = _app_key(DEFAULT_VOWEL_RATIO)
    for name in names:
        try:
            csv_path = resolve_wordlist(name)
        except FileNotFoundError as e:
            logger.warning("ウォームアップをスキップ: %s (%s)", name, e)
            continue
        try:
            with _db_cache_lock:
                cached = db_cache_key(app_key, csv_path, where) in _db_cache
            if cached:
                logger.info("ウォームアップ済み(スキップ): %s", name)
                continue
            logger.info("ウォームアップ開始: %s", name)
            started = time.monotonic()
            db = _get_db(_get_app(DEFAULT_VOWEL_RATIO), app_key, csv_path, where)
            elapsed = time.monotonic() - started
            with _db_cache_lock:
                entries = len(_db_cache)
                total = sum(_db_variants(v) for v in _db_cache.values())
            logger.info(
                "ウォームアップ完了: %s (%.1f秒, %d バリエーション / キャッシュ %d件・計%d)",
                name,
                elapsed,
                _db_variants(db),
                entries,
                total,
            )
        except Exception:
            logger.warning("ウォームアップ失敗: %s", name, exc_info=True)


def start_warmup_thread() -> threading.Thread | None:
    """環境変数の指定があればウォームアップをdaemonスレッドで開始する。

    起動そのものはブロックしない。未設定なら何もせず None を返す。
    """
    names = [n.strip() for n in os.environ.get(WARMUP_ENV, "").split(",") if n.strip()]
    if not names:
        return None
    thread = threading.Thread(
        target=warmup_wordlists,
        args=(names,),
        name="wordlist-warmup",
        daemon=True,
    )
    thread.start()
    logger.info("単語リストのウォームアップを開始しました: %s", ", ".join(names))
    return thread


def _json_safe(word: dict[str, Any]) -> dict[str, Any]:
    """単語 dict の非有限 float(inf/nan)を None にする(JSON.stringify 相当)。"""
    out = dict(word)
    for k, v in out.items():
        if isinstance(v, float) and not math.isfinite(v):
            out[k] = None
    return out


def run_convert(
    phrases: list[str],
    wordlist_csv: Path,
    where: str | None,
    params: dict[str, Any],
    weights_per_line: list[list[float]] | UnitWeightsFunc | None = None,
) -> dict:
    """bridge/convert.mjs と同じ入出力の変換。

    行ごとの {units(mora単位), words(period付き単語列)} と、
    editor 再生成用の tokensList・phrases を返す。
    params の VOWEL_RATIO は app の行列スケーリングに使い、本家 app.js と
    同様にそのままエンジンにも渡す(エンジン側では未知キーとして無害)。

    weights_per_line: エンジンの generate_from_tokens にそのまま渡す
        「行ごとのユニット位置別重み」(行の長さはその行の音節ユニット数)。
        重みはターゲット側ユニットの一致距離だけに掛かり、行ごとに平均1へ
        正規化される。None なら重み無しの従来動作と完全に同一。
        重みの計算にユニット列そのものが要る場合は UnitWeightsFunc(callable)を
        渡せる。エンジンが使うのと同じユニット列を引数に呼ばれるので、
        MeCabトークナイズを二重に走らせずに済む。
    """
    params = params or {}
    app_key = _app_key(params.get("VOWEL_RATIO"))
    app = _get_app(params.get("VOWEL_RATIO"))
    db = _get_db(app, app_key, Path(wordlist_csv), where or "")

    # 生成画面(app.js)と同じ経路: トークナイズ → 生成
    tokens_list = app.text_analyzer.tokenize_together(phrases)

    # callable が渡されたら、エンジンが内部で作るのと同じユニット列
    # (get_yomi_and_phrase_break の結果)を先に作って重み計算に渡す。
    # tokenize_together(MeCab)は上の1回きりで、ここでは走らない。
    weights: list[list[float]] | None
    if callable(weights_per_line):
        units_per_line = [
            app.text_analyzer.get_yomi_and_phrase_break(t) for t in tokens_list
        ]
        weights = weights_per_line(units_per_line)
    else:
        weights = weights_per_line

    units_list: list[list[dict[str, Any]]] = [[] for _ in phrases]

    def update_func(result: Any, i: int, tokenized_phrases: list[list[dict[str, Any]]]) -> None:
        # 行ごとのユニット列(mora単位)を受け取る
        units_list[i] = [
            {
                "surface_form": u["surface_form"],
                "pronunciation": u["pronunciation"],
                "phrase": u["phrase"],
            }
            for u in tokenized_phrases[i]
        ]

    results = app.soramimi_maker.generate_from_tokens(
        tokens_list, db, params, update_func, weights_per_line=weights
    )

    lines = [
        {"units": units_list[i], "words": [_json_safe(w) for w in words]}
        for i, words in enumerate(results)
    ]
    return {"lines": lines, "tokensList": tokens_list, "phrases": phrases}
