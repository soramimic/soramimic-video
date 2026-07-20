"""単語レベル正解スナップショット(ground_truth_words.json)での横断評価。

各プロジェクトの soramimic_raw.json から apply_converted_lines で parody を
再計算し(単語選択は固定)、スナップショットの単語と note_kana を比較する。

使い方:
    uv run python scripts/eval_mora_words.py [--work work] [-v]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from soramimic_video.convert import apply_converted_lines
from soramimic_video.project import Project


def eval_project(proj_dir: Path, verbose: bool) -> tuple[int, int, int, int]:
    project = Project.load(proj_dir)
    raw = json.loads((proj_dir / "soramimic_raw.json").read_text())
    gt = json.loads((proj_dir / "ground_truth_words.json").read_text())
    parody = project.parody
    assert parody is not None
    apply_converted_lines(
        project, raw["lines"], parody.wordlist, parody.where, parody.params
    )
    assert project.parody is not None
    by_line: dict[int, list] = {}
    for pl in project.parody.lines:
        by_line[pl.line_id] = pl.words

    notes_kana = {n.id: n.kana for n in project.notes}
    nw = nw_ok = nn = nn_ok = 0
    for gw in gt["words"]:
        got = next(
            (w for w in by_line.get(gw["line_id"], [])
             if w.surface == gw["surface"] and set(w.note_ids) & set(gw["note_ids"])),
            None,
        )
        got_by_nid = (
            dict(zip(got.note_ids, got.note_kana, strict=True)) if got else {}
        )
        ok = True
        for nid, truth in zip(gw["note_ids"], gw["truth"], strict=True):
            nn += 1
            if got_by_nid.get(nid) == truth:
                nn_ok += 1
            else:
                ok = False
        nw += 1
        if ok:
            nw_ok += 1
        elif verbose:
            orig = [notes_kana.get(nid, "?") for nid in gw["note_ids"]]
            got_l = [got_by_nid.get(nid, "?") for nid in gw["note_ids"]]
            print(
                f"  {proj_dir.name} 行{gw['line_id']:2d} {gw['surface']}: "
                f"元={'|'.join(orig)} 正={'|'.join(gw['truth'])} 今={'|'.join(got_l)}"
            )
    return nw, nw_ok, nn, nn_ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="work")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    tot = [0, 0, 0, 0]
    for d in sorted(Path(args.work).iterdir()):
        if not (d / "ground_truth_words.json").exists():
            continue
        if not (d / "soramimic_raw.json").exists():
            continue
        r = eval_project(d, args.verbose)
        print(
            f"{d.name}: word {r[1]}/{r[0]}  note {r[3]}/{r[2]}"
        )
        for i in range(4):
            tot[i] += r[i]
    print(
        f"TOTAL: word {tot[1]}/{tot[0]} = {100 * tot[1] / tot[0]:.1f}%  "
        f"note {tot[3]}/{tot[2]} = {100 * tot[3] / tot[2]:.1f}%"
    )


if __name__ == "__main__":
    main()
