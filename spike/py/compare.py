"""Python経路 と Node経路 の変換結果を突き合わせて一致率と不一致内訳を出す。

    python spike/py/compare.py <mode>   # mode: injected | kuromoji

不一致の分類(行単位):
  reading   : ユニット列(=読み)が違う          → 読みソース由来
  tie       : ユニット列は同じ・行スコア合計が同じ → 同点候補のタイブレーク差
  algorithm : ユニット列は同じ・行スコア合計が違う → 実装差(どちらかが良い解を選んだ)

DUPLICATE=false のため used_words が行をまたいで持ち越される。一度ずれると
以降の行に波及しうるので「最初にずれた行」も併記する。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out"
SONGS = [
    "akatombo", "chatsumi", "furusato", "harugakita", "katatsumuri", "momiji",
    "momotarou", "nanatsunoko", "oborodukiyo", "shabondama",
    "lemon", "ussewa", "yorunikakeru",
]
LISTS = ["nations", "plant", "scientist", "stations", "pokemon"]


def line_key(line: dict) -> list[tuple]:
    return [(w["surface"], w["kana"], tuple(w["period"])) for w in line["words"]]


def units_key(line: dict) -> list[tuple]:
    return [(u["pronunciation"], u["phrase"]) for u in line["units"]]


def line_score(line: dict) -> float:
    return sum(float(w.get("score") or 0.0) for w in line["words"])


def main(mode: str, py_dir: str = "py") -> int:  # out/node-<mode> と out/<py_dir> を比べる
    rows = []
    total = Counter()
    examples: list[str] = []
    for wl in LISTS:
        for song in SONGS:
            pp = OUT / py_dir / f"{wl}__{song}.json"
            np_ = OUT / f"node-{mode}" / f"{wl}__{song}.json"
            if not (pp.exists() and np_.exists()):
                continue
            py = json.loads(pp.read_text(encoding="utf-8"))["lines"]
            nd = json.loads(np_.read_text(encoding="utf-8"))["lines"]
            cnt = Counter()
            first_div = None
            for i, (a, b) in enumerate(zip(py, nd, strict=True)):
                if line_key(a) == line_key(b):
                    cnt["match"] += 1
                    continue
                if first_div is None:
                    first_div = i
                if units_key(a) != units_key(b):
                    kind = "reading"
                elif abs(line_score(a) - line_score(b)) < 1e-6:
                    kind = "tie"
                else:
                    kind = "algorithm"
                cnt[kind] += 1
                if len(examples) < 40:
                    examples.append(
                        f"[{kind}] {wl}/{song} L{i}: "
                        f"PY={[w['surface'] for w in a['words']]}({line_score(a):.2f}) "
                        f"NODE={[w['surface'] for w in b['words']]}({line_score(b):.2f})"
                    )
            n = len(py)
            rows.append({
                "wordlist": wl, "song": song, "lines": n,
                "match": cnt["match"], "reading": cnt["reading"],
                "tie": cnt["tie"], "algorithm": cnt["algorithm"],
                "first_divergence_line": first_div,
            })
            total.update(cnt)
            total["lines"] += n

    print(f"=== node-{mode} vs {py_dir} ===")
    print(f"{'wordlist':10} {'song':14} {'lines':>5} {'match':>6} {'read':>5} {'tie':>4} {'algo':>5}  rate")
    for r in rows:
        rate = r["match"] / r["lines"] * 100 if r["lines"] else 0
        print(f"{r['wordlist']:10} {r['song']:14} {r['lines']:5} {r['match']:6} "
              f"{r['reading']:5} {r['tie']:4} {r['algorithm']:5}  {rate:5.1f}%")
    n = total["lines"]
    print(f"\nTOTAL lines={n} match={total['match']} ({total['match'] / n * 100:.2f}%) "
          f"reading={total['reading']} tie={total['tie']} algorithm={total['algorithm']}")
    print("\n--- 不一致の例(最大40件) ---")
    for e in examples:
        print(e)

    (OUT / f"compare-{mode}-vs-{py_dir}.json").write_text(
        json.dumps({"rows": rows, "total": dict(total), "examples": examples},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
