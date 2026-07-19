"""モーラ配分の正解データ評価: 保存済みエンジン出力(soramimic_raw.json)から
apply_converted_lines で parody を再計算し、ground_truth_full.json と比較する。

エンジン出力を再利用するため、単語選択は固定で配分ロジックの差分だけを測れる。

使い方:
    uv run python scripts/eval_mora_alignment.py --project work/yoru_fantasy [-v]
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from soramimic_video.convert import apply_converted_lines
from soramimic_video.project import Project


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("-v", "--verbose", action="store_true", help="不一致の内訳を表示")
    args = ap.parse_args()
    proj_dir = Path(args.project)

    project = Project.load(proj_dir)
    raw = json.loads((proj_dir / "soramimic_raw.json").read_text())
    gt = json.loads((proj_dir / "ground_truth_full.json").read_text())

    parody = project.parody
    assert parody is not None
    apply_converted_lines(
        project, raw["lines"], parody.wordlist, parody.where, parody.params
    )
    assert project.parody is not None
    got_by_line = {pl.line_id: pl for pl in project.parody.lines}

    n_words = n_words_ok = n_notes = n_notes_ok = 0
    for line in gt["lines"]:
        pl = got_by_line[line["line_id"]]
        for w_gt, w_got in zip(line["words"], pl.words, strict=True):
            assert w_gt["surface"] == w_got.surface, (
                f"単語不一致: {w_gt['surface']} != {w_got.surface}"
            )
            got_by_nid = dict(zip(w_got.note_ids, w_got.note_kana, strict=True))
            word_ok = True
            for n in w_gt["notes"]:
                n_notes += 1
                if got_by_nid.get(n["note_id"]) == n["truth"]:
                    n_notes_ok += 1
                else:
                    word_ok = False
            n_words += 1
            if word_ok:
                n_words_ok += 1
            elif args.verbose:
                truth = [n["truth"] for n in w_gt["notes"]]
                got = [got_by_nid.get(n["note_id"], "?") for n in w_gt["notes"]]
                orig = [n["orig_kana"] for n in w_gt["notes"]]
                print(
                    f"行{line['line_id']:2d} {w_gt['surface']}: "
                    f"元={'|'.join(orig)} 正={'|'.join(truth)} 今={'|'.join(got)}"
                )

    print(f"word : {n_words_ok}/{n_words} = {100 * n_words_ok / n_words:.1f}%")
    print(f"note : {n_notes_ok}/{n_notes} = {100 * n_notes_ok / n_notes:.1f}%")


if __name__ == "__main__":
    main()
