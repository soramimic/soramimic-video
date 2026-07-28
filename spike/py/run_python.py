"""Python経路(現行)で run_convert を回し、結果と時間を書き出す。

    python spike/py/run_python.py <wordlist> <song_id> [<song_id> ...]

1プロセスで「エンジン初期化 → 単語DB構築 → 曲ごとの変換」を順に行う
(サーバーの実運用と同じく、DBはプロセス内キャッシュに載った状態で各曲を変換)。
出力: spike/out/py/<wordlist>__<song>.json と spike/out/py/<wordlist>__timing.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out"


def apply_csv_fix() -> None:
    """soramimic-python の CSV 空白正規化を JS 本家(wordList.js)と同じ式に直す。

    Python 側は ``re.sub(r"\\s*,\\s*", ",", text)`` のままで、``\\s`` が改行を含むため
    「行末が , の行(最終列が空)」が次行を飲み込み、次行の単語がDBから丸ごと消える。
    JS本家は ``[ \\t]*,[ \\t]*`` に修正済み(#77)。この差だけを実験的に消して
    Python/JS の出力がどこまで揃うかを見るためのランタイムパッチ。
    """
    import soramimic.word_list as wl

    class _ReProxy:
        def __getattr__(self, name: str):  # re.split など素通し
            return getattr(re, name)

        def sub(self, pattern, repl, string, *args, **kwargs):
            if pattern == r"\s*,\s*":
                pattern = r"[ \t]*,[ \t]*"
            return re.sub(pattern, repl, string, *args, **kwargs)

    wl.re = _ReProxy()


def main(argv: list[str]) -> int:
    wordlist, songs = argv[0], argv[1:]
    t_import = time.monotonic()
    from soramimic_video.convert import resolve_convert_settings, resolve_wordlist
    from soramimic_video.soramimic_engine import _get_app, _get_db, _app_key, run_convert

    import_ms = (time.monotonic() - t_import) * 1000
    tag = ""
    if os.environ.get("CSVFIX"):
        apply_csv_fix()
        tag = "-csvfix"

    phrases_all = json.loads((OUT / "phrases.json").read_text(encoding="utf-8"))
    csv_path = resolve_wordlist(wordlist)
    where, params, _alpha = resolve_convert_settings(csv_path, None, None)

    t = time.monotonic()
    app = _get_app(params.get("VOWEL_RATIO"))
    engine_ms = (time.monotonic() - t) * 1000

    # 単語DBはウォームアップ相当(max_units=None)で先に作る。以後の変換はキャッシュヒット
    t = time.monotonic()
    _get_db(app, _app_key(params.get("VOWEL_RATIO")), csv_path, where or "", None)
    db_ms = (time.monotonic() - t) * 1000

    timing = {
        "wordlist": wordlist, "where": where, "params": params,
        "import_ms": import_ms, "engine_init_ms": engine_ms, "db_build_ms": db_ms,
        "songs": {},
    }
    dst = OUT / f"py{tag}"
    dst.mkdir(parents=True, exist_ok=True)
    for song in songs:
        phrases = phrases_all[song]
        t = time.monotonic()
        result = run_convert(phrases, csv_path, where, params)
        ms = (time.monotonic() - t) * 1000
        timing["songs"][song] = {"convert_ms": ms, "lines": len(phrases)}
        (dst / f"{wordlist}__{song}.json").write_text(
            json.dumps({"lines": result["lines"], "phrases": phrases}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[py] {wordlist} x {song}: {ms:.0f}ms ({len(phrases)}行)", file=sys.stderr)

    (dst / f"{wordlist}__timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
