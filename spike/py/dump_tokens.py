"""Node経路に「Pythonと同じMeCabの読み」を注入するための生トークンを書き出す。

TextAnalyzer.tokenize_together は
    texts -> apostrophe.to_string -> tokenize_sentenses(=MeCabTokenizer.tokenize)
    -> format_tokens_list
の順に処理する。ここでは前2段だけを再現し、format 前の生トークンを
{フレーズ文字列: トークン列} で吐く(JS側は tokenizeSentenses に差し込むだけで
同じ formatTokensList を通せる)。

出力: spike/out/mecab_tokens.json
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out"


def main() -> int:
    from soramimic.tokenizers.mecab import MeCabTokenizer
    from soramimic_video.soramimic_engine import _get_app

    app = _get_app(0.8)
    ap = app.text_analyzer.english.apostrophe
    tok = MeCabTokenizer()

    phrases_all = json.loads((OUT / "phrases.json").read_text(encoding="utf-8"))
    texts = sorted({p for lines in phrases_all.values() for p in lines})
    converted = [ap.to_string(t) for t in texts]
    tokens_list = tok.tokenize(converted)

    # キーは JS 側が tokenizeSentenses で受け取る文字列(=to_string 済み)
    dump = {c: tokens for c, tokens in zip(converted, tokens_list, strict=True)}
    (OUT / "mecab_tokens.json").write_text(
        json.dumps(dump, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(dump)} フレーズ -> {OUT / 'mecab_tokens.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
