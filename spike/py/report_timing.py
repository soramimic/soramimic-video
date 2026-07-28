"""Python / Node / ブラウザ の計測結果を表にまとめる。

    python spike/py/report_timing.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out"
SONGS = [
    "akatombo", "chatsumi", "furusato", "harugakita", "katatsumuri", "momiji",
    "momotarou", "nanatsunoko", "oborodukiyo", "shabondama",
    "lemon", "ussewa", "yorunikakeru",
]
LISTS = ["nations", "plant", "scientist", "stations"]


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> int:
    print("## 1. 起動・単語リストDB構築(1回きりのコスト)\n")
    print(f"{'wordlist':10} {'PY import':>10} {'PY engine':>10} {'PY db':>10} "
          f"{'NODE 起動+data+app':>20} {'NODE db':>9}")
    for wl in LISTS:
        py = load(OUT / "py" / f"{wl}__timing.json")
        nd = load(OUT / "node-injected" / f"{wl}__timing.json")
        if not py or not nd:
            continue
        s = next(iter(nd["songs"].values()))
        boot = s["marks"]["app_ready"]
        print(f"{wl:10} {py['import_ms']:9.0f}ms {py['engine_init_ms']:9.0f}ms "
              f"{py['db_build_ms']:9.0f}ms {boot:19.0f}ms {s['db_ms']:8.0f}ms")

    print("\n## 2. 変換本体(DB構築済み・秒)\n")
    header = f"{'wordlist':10} {'song':14} {'lines':>5} {'PY':>8} {'NODE(gen)':>10} {'NODE(wall)':>11} {'比(PY/NODEgen)':>14}"
    print(header)
    agg = {"py": 0.0, "gen": 0.0, "wall": 0.0}
    for wl in LISTS:
        py = load(OUT / "py" / f"{wl}__timing.json")
        nd = load(OUT / "node-injected" / f"{wl}__timing.json")
        if not py or not nd:
            continue
        for song in SONGS:
            if song not in py["songs"] or song not in nd["songs"]:
                continue
            p = py["songs"][song]["convert_ms"] / 1000
            g = nd["songs"][song]["generate_ms"] / 1000
            w = nd["songs"][song]["wall_ms"] / 1000
            agg["py"] += p
            agg["gen"] += g
            agg["wall"] += w
            print(f"{wl:10} {song:14} {py['songs'][song]['lines']:5} "
                  f"{p:7.2f}s {g:9.2f}s {w:10.2f}s {p / g if g else 0:13.1f}x")
    print(f"\n合計: PY {agg['py']:.1f}s / NODE生成 {agg['gen']:.1f}s "
          f"/ NODE実時間(都度起動) {agg['wall']:.1f}s  → 生成部で {agg['py'] / agg['gen']:.1f}倍速")

    print("\n## 3. ブラウザ(Web Worker + CPUスロットリング)\n")
    for p in sorted((OUT / "browser").glob("*.json")) if (OUT / "browser").exists() else []:
        d = load(p)
        print(f"### {d['wordlist']} x {d['song']} (tokenizer={d['mode']})")
        print(f"{'CPU':>5} {'data+kuromoji':>14} {'db':>8} {'tokenize':>9} {'generate':>9} "
              f"{'total':>8} {'maxFrameGap':>12} {'p95FrameGap':>12}")
        for r in d["runs"]:
            t = r["timings"]
            m = r["main_thread"]
            print(f"{r['cpu_throttle']:4}x {t['app_ms'] / 1000:13.1f}s {t['db_ms'] / 1000:7.2f}s "
                  f"{t['tokenize_ms'] / 1000:8.2f}s {t['generate_ms'] / 1000:8.2f}s "
                  f"{t['total_ms'] / 1000:7.2f}s {m['max_gap_ms']:11.1f}ms {m['p95_gap_ms']:11.1f}ms")
        first = d["runs"][0]["transfer"]
        print("\n転送(1x実行時):")
        for k, v in sorted(first.items(), key=lambda kv: -kv[1]["sent"]):
            print(f"  {k:28} raw {v['raw'] / 1048576:7.2f}MB  sent {v['sent'] / 1048576:7.2f}MB  ({v['count']}req)")
        tot_raw = sum(v["raw"] for v in first.values())
        tot_sent = sum(v["sent"] for v in first.values())
        print(f"  {'TOTAL':28} raw {tot_raw / 1048576:7.2f}MB  sent {tot_sent / 1048576:7.2f}MB\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
