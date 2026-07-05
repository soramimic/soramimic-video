"""替え歌変換ステージ: soramimic(Nodeブリッジ)で行ごとの替え歌単語列を得る。

変換入力はXFの読み(カナ)を行ごとに連結した文字列。変換結果の period は
ブリッジが返すユニット列(mora単位)へのindexなので、
ユニットの文字オフセット → XFモーラ(音符)の文字オフセット の対応で
各単語を音符ID列に写像する。
"""

from __future__ import annotations

import csv
import difflib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .project import Parody, ParodyLine, ParodyWord, Project

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_DIR = Path(os.environ.get("SORAMIMI_VIDEO_BRIDGE", REPO_ROOT / "bridge"))
WORDLISTS_DIR = REPO_ROOT / "external" / "soramimi-wordlists"

# editor(conf/setting.json)と同じ既定の絞り込み
DEFAULT_WHERE = {
    "baseball": "type=family or type=registered or type=full",
    "football": "type=family or type=registered or type=full",
}


def resolve_wordlist(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".csv" and p.exists():
        return p
    candidate = WORDLISTS_DIR / f"{name_or_path}.csv"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"単語リストが見つかりません: {name_or_path} "
        f"(external/soramimi-wordlists のリスト名かCSVパスを指定してください)"
    )


def _coerce_params(params: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def run_bridge(phrases: list[str], wordlist_csv: Path, where: str | None,
               params: dict[str, Any]) -> dict:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node が見つかりません(変換ブリッジに必要です)")
    script = BRIDGE_DIR / "convert.mjs"
    if not (BRIDGE_DIR / "node_modules").exists():
        raise RuntimeError(f"ブリッジが未セットアップです: cd {BRIDGE_DIR} && npm ci")
    payload = json.dumps(
        {
            "phrases": phrases,
            "wordlist": {"file": str(wordlist_csv), "where": where},
            "params": params,
        },
        ensure_ascii=False,
    )
    proc = subprocess.run(
        [node, str(script)],
        input=payload.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"変換ブリッジが失敗しました:\n{proc.stderr.decode('utf-8')}")
    return json.loads(proc.stdout.decode("utf-8"))


def _offset_map(src: str, dst: str) -> list[int]:
    """srcの各文字オフセット(0..len(src))をdstのオフセットに写す表。

    完全一致なら恒等。差異があればdifflibで最善の対応を取る。
    """
    if src == dst:
        return list(range(len(src) + 1))
    table = [0] * (len(src) + 1)
    sm = difflib.SequenceMatcher(None, src, dst, autojunk=False)
    last_dst = 0
    for a, b, size in sm.get_matching_blocks():
        for i in range(a, a + size + 1):
            table[i] = b + (i - a)
        if size:
            last_dst = b + size
        # マッチしない区間は直前のdst位置を引き継ぐ(単調性を保つ)
        for i in range(a + size + 1, len(src) + 1):
            table[i] = max(table[i], last_dst)
    table[len(src)] = max(table[len(src)], len(dst))
    return table


def _map_word_to_notes(
    unit_lens: list[int],
    note_lens: list[int],
    offset_map: list[int],
    period: tuple[int, int],
) -> list[int]:
    """periodユニット区間 → (文字区間) → 重なる音符indexの列。"""
    unit_cum = [0]
    for length in unit_lens:
        unit_cum.append(unit_cum[-1] + length)
    start_c = offset_map[unit_cum[period[0]]]
    end_c = offset_map[unit_cum[period[1]]]

    note_cum = [0]
    for length in note_lens:
        note_cum.append(note_cum[-1] + length)
    ids = [
        i
        for i in range(len(note_lens))
        if note_cum[i] < end_c and note_cum[i + 1] > start_c
    ]
    return ids


def _load_wordlist_rows(csv_path: Path) -> dict[str, list[dict[str, str]]]:
    rows: dict[str, list[dict[str, str]]] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.setdefault(row.get("id", ""), []).append(row)
    return rows


def _find_row(
    rows_by_id: dict[str, list[dict[str, str]]], word: dict
) -> dict[str, str] | None:
    rows = rows_by_id.get(str(word.get("id", "")))
    if not rows:
        return None
    for row in rows:
        if row.get("surface") == word.get("surface"):
            return row
    return rows[0]


def convert_project(
    project: Project,
    wordlist: str,
    where: str | None = None,
    params: dict[str, str] | None = None,
) -> None:
    """project.parody を埋める(破壊的)。"""
    csv_path = resolve_wordlist(wordlist)
    name = csv_path.stem
    if where is None:
        where = DEFAULT_WHERE.get(name)
    coerced = _coerce_params(params or {})

    phrases = [line.xf_kana for line in project.lines]
    result = run_bridge(phrases, csv_path, where, coerced)
    rows_by_id = _load_wordlist_rows(csv_path)

    parody = Parody(wordlist=name, where=where, params=coerced)
    for line, converted in zip(project.lines, result["lines"], strict=True):
        pline = ParodyLine(line_id=line.id)
        unit_lens = [len(u["pronunciation"]) for u in converted["units"]]
        unit_concat = "".join(u["pronunciation"] for u in converted["units"])
        note_lens = [len(project.notes[i].kana) for i in line.note_ids]
        note_concat = "".join(project.notes[i].kana for i in line.note_ids)
        if unit_concat != note_concat:
            logger.debug(
                "行%d: ユニット列と音符列の読みが不一致 (%r != %r)。difflibで対応づけます",
                line.id, unit_concat, note_concat,
            )
        offset_map = _offset_map(unit_concat, note_concat)
        for word in converted["words"]:
            note_idx = _map_word_to_notes(
                unit_lens, note_lens, offset_map, tuple(word["period"])
            )
            if not note_idx:
                logger.warning(
                    "行%d: 単語 %r を音符に対応づけられずスキップ", line.id, word["surface"]
                )
                continue
            pline.words.append(
                ParodyWord(
                    surface=word["surface"],
                    kana=word["kana"],
                    original=word.get("original", ""),
                    original_surface=word.get("original_surface", ""),
                    originalkana=word.get("originalkana", ""),
                    note_ids=[line.note_ids[i] for i in note_idx],
                    wordlist_row=_find_row(rows_by_id, word),
                )
            )
        parody.lines.append(pline)
    project.parody = parody
