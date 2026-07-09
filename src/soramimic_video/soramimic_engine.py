"""soramimic ライブラリを直接使う変換エンジン(旧 Node ブリッジ bridge/convert.mjs の置換)。

bridge/convert.mjs と同一の出力構造
    {lines: [{units, words}], tokensList, phrases}
を返す run_convert() を提供する。生成画面(app.js)と同じ経路
(トークナイズ → generate_from_tokens)で組み立てる。

app(同梱辞書データ + fugashi/ipadic MeCab トークナイザ)の構築は重いので、
遅延初期化してモジュールレベルにキャッシュする。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

_app: Any = None


def _get_app() -> Any:
    """soramimic アプリ(辞書 + トークナイザ)を遅延構築してキャッシュする。"""
    global _app
    if _app is None:
        from soramimic import create_soramimic, load_default_data
        from soramimic.tokenizers.mecab import MeCabTokenizer

        tok = MeCabTokenizer()
        # MeCabTokenizer.tokenize/get_yomi は str|list[str] を受ける overload 的な
        # シグネチャで、create_soramimic の list[str] 前提と厳密には合わないが実行時は問題ない
        _app = create_soramimic(
            **load_default_data(),
            tokenize_sentenses=tok.tokenize,  # type: ignore[arg-type]
            get_yomi=tok.get_yomi,
        )
    return _app


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
    """
    app = _get_app()
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

    results = app.soramimi_maker.generate_from_tokens(tokens_list, db, params or {}, update_func)

    lines = [
        {"units": units_list[i], "words": [_json_safe(w) for w in words]}
        for i, words in enumerate(results)
    ]
    return {"lines": lines, "tokensList": tokens_list, "phrases": phrases}
