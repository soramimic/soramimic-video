"""Node経路(本家JSエンジン)を subprocess で回すドライバ。

    python spike/py/run_node.py <wordlist> <kuromoji|injected> <song_id> ...

1プロセス = 1(単語リスト×曲)。Node常駐前提ではないので、プロセス起動・
データ読み込み・単語DB構築のコストも毎回含む(内訳は timings に出る)。
出力: spike/out/node-<mode>/<wordlist>__<song>.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "spike" / "out"


def main(argv: list[str]) -> int:
    wordlist, mode, songs = argv[0], argv[1], argv[2:]
    sys.path.insert(0, str(ROOT / "src"))
    from soramimic_video.convert import resolve_convert_settings, resolve_wordlist

    csv_path = resolve_wordlist(wordlist)
    where, params, _ = resolve_convert_settings(csv_path, None, None)

    phrases_all = json.loads((OUT / "phrases.json").read_text(encoding="utf-8"))
    # 出力先の接尾辞(JSエンジンのリビジョン違いを並べて比較するため)
    tag = os.environ.get("NODE_TAG", "")
    dst = OUT / f"node-{mode}{tag}"
    dst.mkdir(parents=True, exist_ok=True)
    timing = {"wordlist": wordlist, "mode": mode, "where": where, "songs": {}}

    for song in songs:
        job = {
            "phrases": phrases_all[song],
            "wordlistCsv": str(csv_path),
            "where": where,
            "params": params,
            "tokenizer": mode,
            "tokensFile": str(OUT / "mecab_tokens.json"),
            "out": str(dst / f"{wordlist}__{song}.json"),
        }
        job_path = dst / f"{wordlist}__{song}.job.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        t = time.monotonic()
        proc = subprocess.run(
            ["node", "--max-old-space-size=8192",
             str(ROOT / "spike" / "node" / "convert.mjs"), str(job_path)],
            capture_output=True, text=True,
        )
        wall_ms = (time.monotonic() - t) * 1000
        if proc.returncode != 0:
            print(proc.stderr[-3000:], file=sys.stderr)
            return 1
        inner = json.loads(proc.stdout.strip().splitlines()[-1])
        timing["songs"][song] = {"wall_ms": wall_ms, **inner}
        print(
            f"[node/{mode}] {wordlist} x {song}: wall {wall_ms:.0f}ms "
            f"(db {inner['db_ms']}ms, tok {inner['tokenize_ms']}ms, gen {inner['generate_ms']}ms)",
            file=sys.stderr,
        )

    (dst / f"{wordlist}__timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
