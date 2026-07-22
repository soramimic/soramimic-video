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
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

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


def _get_app(vowel_ratio: Any = None) -> Any:
    """「音の合わせ方」r に応じた soramimic アプリを返す(appCore.js の appFor)。"""
    from soramimic import create_soramimic, scale_similarity

    try:
        r = float(vowel_ratio)
    except (TypeError, ValueError):
        r = 0.0
    # JSの Number(vowelRatio) || 0.8 相当(0/NaN/未指定 → 既定)
    r = min(0.9, max(0.1, r or DEFAULT_VOWEL_RATIO))
    key = f"{r:.2f}"
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
) -> dict:
    """bridge/convert.mjs と同じ入出力の変換。

    行ごとの {units(mora単位), words(period付き単語列)} と、
    editor 再生成用の tokensList・phrases を返す。
    params の VOWEL_RATIO は app の行列スケーリングに使い、本家 app.js と
    同様にそのままエンジンにも渡す(エンジン側では未知キーとして無害)。
    """
    params = params or {}
    app = _get_app(params.get("VOWEL_RATIO"))
    csv_text = Path(wordlist_csv).read_text(encoding="utf-8")
    db = app.word_list.parse_tidy(csv_text, where or "")

    # 生成画面(app.js)と同じ経路: トークナイズ → 生成
    tokens_list = app.text_analyzer.tokenize_together(phrases)

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

    results = app.soramimi_maker.generate_from_tokens(tokens_list, db, params, update_func)

    lines = [
        {"units": units_list[i], "words": [_json_safe(w) for w in words]}
        for i, words in enumerate(results)
    ]
    return {"lines": lines, "tokensList": tokens_list, "phrases": phrases}
