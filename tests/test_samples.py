"""同梱サンプル曲(examples/gen_samples.py の生成物)の妥当性検証。

生成スクリプトの SONGS と static/sample/ の中身がずれていないか
(= 生成し直し忘れ)を含めて、XF MIDI として読み戻せることを確かめる。
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from soramimic_video.xfparse import analyze_midi, normalize_kana

SAMPLE_DIR = Path(__file__).parent.parent / "src" / "soramimic_video" / "static" / "sample"
GEN_SCRIPT = Path(__file__).parent.parent / "examples" / "gen_samples.py"


def _load_gen_samples():
    spec = importlib.util.spec_from_file_location("gen_samples", GEN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gen_samples = _load_gen_samples()
MANIFEST = json.loads((SAMPLE_DIR / "samples.json").read_text(encoding="utf-8"))
SAMPLE_IDS = [entry["id"] for entry in MANIFEST]


def test_manifest_matches_generator():
    """samples.json と gen_samples.SONGS が同じ曲・同じ順で並んでいる。"""
    assert SAMPLE_IDS == list(gen_samples.SONGS)
    for entry in MANIFEST:
        song = gen_samples.SONGS[entry["id"]]
        assert entry["title"] == song["title"]
        # description はUIの補足表示(権利区分)。空だと何も出ないので必須にする
        assert entry["description"] == song["description"]
        assert entry["description"]
        # title_kana は曲名の読み。サムネの曲名変換の入力に使う(MeCabの推定を
        # 使わずに済ませるためのものなので、空だと意味が無い)
        assert entry["title_kana"] == song["title_kana"]
        assert entry["title_kana"]


def test_title_kana_is_katakana():
    """読みはカタカナ(長音符含む)だけ。ひらがな・漢字が混ざっていたら誤り。"""
    for entry in MANIFEST:
        kana = entry["title_kana"]
        assert re.fullmatch(r"[ァ-ヶー]+", kana), (entry["id"], kana)


@pytest.mark.parametrize("sample_id", SAMPLE_IDS)
def test_sample_midi_roundtrip(sample_id: str):
    song = gen_samples.SONGS[sample_id]
    score = [item for item in song["score"] if isinstance(item, tuple)]  # 休符/行区切りを除く
    project = analyze_midi(SAMPLE_DIR / f"{sample_id}.mid")

    # 音符数=歌詞モーラ数=打ち込みデータの要素数(取りこぼした歌詞イベントが無い)
    assert len(project.notes) == len(score)
    # 読みは助詞が発音形に直る(は→ワ)ので、打ち込みデータとは生テキスト側で
    # 突き合わせる(取りこぼし・生成し直し忘れの検出という目的は変わらない)
    assert [normalize_kana(n.raw) for n in project.notes] == [
        normalize_kana(k) for k, _, _ in score
    ]
    assert [n.midi_note for n in project.notes] == [note for _, note, _ in score]
    # 1音符=1モーラに正規化してある(拗音「チャ」等だけが2文字)
    for n in project.notes:
        assert len(n.kana) == 1 or (len(n.kana) == 2 and n.kana[1] in "ャュョ"), n.kana

    # 行数は元歌詞ファイルの行数と一致する(アライメントの前提)
    lyrics = (SAMPLE_DIR / f"{sample_id}_lyrics.txt").read_text(encoding="utf-8")
    lyric_lines = [ln for ln in lyrics.splitlines() if ln.strip()]
    assert len(project.lines) == len(lyric_lines)

    # 演奏時間が常識的な範囲(短すぎ/長すぎのデータ壊れを弾く)
    duration = project.notes[-1].end_sec
    assert 10.0 <= duration <= 150.0, f"{sample_id}: {duration}s"

    # 音域が歌える範囲に収まっている
    lo = min(n.midi_note for n in project.notes)
    hi = max(n.midi_note for n in project.notes)
    assert 48 <= lo and hi <= 84 and hi - lo <= 24, f"{sample_id}: {lo}-{hi}"


def test_generated_files_are_up_to_date():
    """コミット済みの .mid が現在の SONGS から再生成したものと一致する。"""
    for sample_id, song in gen_samples.SONGS.items():
        assert gen_samples.build(song) == (SAMPLE_DIR / f"{sample_id}.mid").read_bytes(), sample_id
        assert song["lyrics"] == (SAMPLE_DIR / f"{sample_id}_lyrics.txt").read_text(
            encoding="utf-8"
        ), sample_id
