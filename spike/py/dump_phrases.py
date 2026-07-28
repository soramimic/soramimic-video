"""サンプル曲のXF MIDIから、run_convert に渡す phrases(行ごとのカナ)を書き出す。

convert_project と同じ前処理(normalize_small_vowels)を通すので、
Python経路・Node経路の両方にそのまま同じ入力を渡せる。
出力: spike/out/phrases.json  {song_id: [phrase, ...]}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from soramimic_video.kana import normalize_small_vowels
from soramimic_video.xfparse import analyze_midi

OUT = Path(__file__).resolve().parents[1] / "out" / "phrases.json"


def main(argv: list[str]) -> int:
    out: dict[str, list[str]] = {}
    for spec in argv:
        song_id, _, path = spec.partition("=")
        project = analyze_midi(Path(path))
        out[song_id] = [normalize_small_vowels(ln.xf_kana) for ln in project.lines]
        print(f"{song_id}: {len(out[song_id])}行", file=sys.stderr)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
