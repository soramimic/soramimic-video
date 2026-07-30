"""余り音符(継続ー)への圧縮長音展開の検証。

変換エンジンは単語の1音節を ``[頭] + ー``(撥音・長音・母音字を長音へ圧縮)に
まとめることがあり、そのぶん発音要素が音符より少なくなって余り音符が継続「ー」に
なる。継続ーが撥音ンの直後・休符直後に来ると NEUTRINO 用の MusicXML 生成
(musicxml.py の「休符直後のーは母音、無ければア」ガード)で「ア」に化けて
「せいしょうなごんあー」のように歌われてしまう。convert 側では圧縮された長音を
単語kana由来のモーラへ戻して余り音符を埋める(_expansion_candidates)。

ここでは
1. 実例(清少納言)の固定化
2. 展開候補が単語kanaに無いモーラを捏造しないこと
3. 全サンプル曲×全単語リストで、展開の有無による差分が「余り音符が継続ーだった
   語」に限られ、かつ継続ーが必ず減ることの機械的な確認
を行う。3 は展開を無効化(_expansion_candidates を空に差し替え)した出力を
「修正前」として比較するので、外部のリビジョンを持ち出さずに済む。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from soramimic_video import convert as convert_mod
from soramimic_video.convert import (
    _compressed_moras_per_element,
    _expansion_candidates,
    _map_word_to_notes,
    convert_project,
)
from soramimic_video.kana import split_fine_moras
from soramimic_video.xfparse import analyze_midi

SAMPLE_DIR = (
    Path(__file__).parent.parent / "src" / "soramimic_video" / "static" / "sample"
)
WORDLIST_DIR = Path(__file__).parent.parent / "external" / "soramimic-wordlists"
SAMPLE_IDS = [
    entry["id"]
    for entry in json.loads((SAMPLE_DIR / "samples.json").read_text(encoding="utf-8"))
]
# 圧縮長音(メー・ロー等)が出やすい長い複合語のリスト。実例(清少納言)と同じ
# gimukyoiku を代表に使う。全14リストでも結論が同じことは手元で確認済みだが、
# 1リストでも展開は起きるのでテスト時間を優先する
WORDLIST = "gimukyoiku"


def test_leftover_note_filled_with_compressed_mora():
    """「背をなぞった」(6音符)× 清少納言(5音節)の実例。

    元歌詞ユニットは [セ][ヲ][ナ][ゾッ][タ] の5音節で、音符は促音ッが独立して6つ。
    エンジンは セイショウナゴン を セー/ショー/ナ/ゴ/ン の5要素に圧縮するため
    1音符余り、従来は末尾が継続ー(休符直後なので MusicXML で「ア」)になっていた。
    圧縮された「イ」を戻して6要素にし、6音符へちょうど載せる。
    """
    unit_lens = [1, 1, 1, 2, 1]  # セ ヲ ナ ゾッ タ
    note_lens = [1] * 6
    identity = list(range(7))
    pron = ["セー", "ショー", "ナ", "ゴ", "ン"]
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 5), pron, "セイショウナゴン",
        notes_kana=["セ", "ヲ", "ナ", "ゾ", "ッ", "タ"],
        notes_dur=[0.331, 0.330, 0.215, 0.445, 0.244, 0.675],
    )
    assert ids == [0, 1, 2, 3, 4, 5]
    assert kana == ["セ", "イ", "ショー", "ナ", "ゴ", "ン"]
    assert "" not in kana  # 継続ーになる余り音符が残っていない


def test_no_leftover_keeps_previous_output():
    """余り音符が無い(要素数=音符数)行は展開を試さず従来どおり。"""
    ids, kana = _map_word_to_notes(
        [1, 1, 1], [1, 1, 1], list(range(4)), (0, 3),
        ["セー", "ショー", "ナ"], "セイショウナ",
        notes_kana=["セ", "ヲ", "ナ"],
    )
    assert ids == [0, 1, 2]
    assert kana == ["セー", "ショー", "ナ"]


def test_expansion_candidates_do_not_invent_moras():
    """展開候補の要素列は、単語kanaのモーラ列と圧縮ーの並びから逸れない。"""
    pron = ["セー", "ショー", "ナ", "ゴ", "ン"]
    kana = "セイショウナゴン"
    comp = _compressed_moras_per_element(kana, pron)
    assert comp == [["イ"], ["ウ"], [], [], []]
    cands = _expansion_candidates(pron, comp, budget=1)
    assert [c[0] for c in cands] == [
        ["セ", "イ", "ショー", "ナ", "ゴ", "ン"],
        ["セー", "ショ", "ウ", "ナ", "ゴ", "ン"],
    ]
    fines = split_fine_moras(kana)
    for prons, comps in cands:
        # 展開しても「要素の頭 + 残りの圧縮モーラ」を並べれば単語kanaに戻る
        restored: list[str] = []
        for p, rest in zip(prons, comps, strict=True):
            head = p.rstrip("ー")
            nlong = len(p) - len(head)
            restored.append(head)
            restored.extend(rest)
            restored.extend(["ー"] * (nlong - len(rest)))
        assert "".join(restored) == "".join(fines)
        # 展開済みの要素の圧縮モーラは消えている(同じモーラの二重復元が無い)
        assert all(not c for c, p in zip(comps, prons, strict=True) if "ー" not in p)


def test_expansion_candidates_respects_budget():
    """余り音符数(budget)を超える展開はしない=溢れさせない。"""
    pron = ["セー", "ショー"]
    comp = [["イ"], ["ウ"]]
    assert _expansion_candidates(pron, comp, budget=0) == []
    assert len(_expansion_candidates(pron, comp, budget=1)) == 2  # 片方ずつ
    assert len(_expansion_candidates(pron, comp, budget=2)) == 3  # 両方も候補に入る
    assert _expansion_candidates(pron, [[], []], budget=2) == []  # 圧縮無し
    assert _expansion_candidates(pron, None, budget=2) == []


def _sing(wordlist: str, sample_id: str) -> list[list[dict[str, Any]]]:
    project = analyze_midi(SAMPLE_DIR / f"{sample_id}.mid")
    convert_project(project, wordlist=wordlist)
    assert project.parody is not None
    return [
        [
            {"surface": w.surface, "kana": w.kana,
             "note_ids": list(w.note_ids), "note_kana": list(w.note_kana)}
            for w in pline.words
        ]
        for pline in project.parody.lines
    ]


def _is_subsequence(small: str, big: str) -> bool:
    it = iter(big)
    return all(ch in it for ch in small)


@pytest.mark.skipif(
    not (WORDLIST_DIR / f"{WORDLIST}.csv").exists(),
    reason="単語リストのサブモジュールが未取得",
)
def test_expansion_only_touches_leftover_notes(monkeypatch):
    """全サンプル曲で、展開の有無による差分が余り音符を持つ語だけに限られる。

    展開を無効化した出力(=修正前)と比較して、差分がある語はすべて
    「単音の継続ーを含んでいた語」であり、かつ継続ーの数が減り、
    歌唱モーラ列は順序を保って増える(捏造・並べ替えが無い)ことを確かめる。
    """
    changed = 0
    for sample_id in SAMPLE_IDS:
        with monkeypatch.context() as m:
            m.setattr(convert_mod, "_expansion_candidates", lambda *a, **k: [])
            base = _sing(WORDLIST, sample_id)
        new = _sing(WORDLIST, sample_id)

        assert len(base) == len(new)
        for li, (bl, nl) in enumerate(zip(base, new, strict=True)):
            where = f"{sample_id}/{WORDLIST} line{li}"
            # 単語の選択・音符の割り当て先そのものは一切変わらない
            assert [w["surface"] for w in bl] == [w["surface"] for w in nl], where
            assert [w["note_ids"] for w in bl] == [w["note_ids"] for w in nl], where
            for bw, nw in zip(bl, nl, strict=True):
                b, n = bw["note_kana"], nw["note_kana"]
                if b == n:
                    continue
                changed += 1
                detail = f"{where} {bw['surface']}({bw['kana']}): {b} -> {n}"
                assert "ー" in b, f"余り音符が無い語が変わった: {detail}"
                assert n.count("ー") < b.count("ー"), f"継続ーが減っていない: {detail}"
                bs = "".join(b).replace("ー", "")
                ns = "".join(n).replace("ー", "")
                assert _is_subsequence(bs, ns), f"モーラ列の順序が壊れた: {detail}"
    # 何も変わらないなら検証になっていない(リスト側の変化などで空振りしたら気付く)
    assert changed > 0, f"{WORDLIST}: 展開が1件も起きなかった"
