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

from soramimic_video.align import align_lines
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
FULL_JAPANESE_SAMPLE_LINES = {
    "furusato": 12,
    "akatombo": 8,
    "momotarou": 18,
    "katatsumuri": 6,
    "harugakita": 6,
    "oborodukiyo": 8,
    "chatsumi": 8,
    "nanatsunoko": 6,
    "momiji": 8,
    "shabondama": 10,
}
ENGLISH_SAMPLE_LINES = {
    "amazinggrace": [
        "Amazing grace! How sweet the sound",
        "That saved a wretch like me!",
        "I once was lost, but now am found,",
        "Was blind, but now I see.",
    ],
    "twinkle": [
        "Twinkle, twinkle, little star,",
        "How I wonder what you are!",
        "Up above the world so high,",
        "Like a diamond in the sky.",
        "Twinkle, twinkle, little star,",
        "How I wonder what you are!",
    ],
}
ENGLISH_SAMPLE_KANA = {
    "amazinggrace": [
        "アメイジンググレイスハウスイートザサウンド",
        "ザットセイブドアアレッチライクミー",
        "アイワンスワズロストバットナウアムフアウンド",
        "ワズブラインドバットナウアイシー",
    ],
    "twinkle": [
        "ツインクルツインクルリトルスター",
        "ハウアイワンダーワットユーアー",
        "アップアバブザワールドソーハイ",
        "ライクアダイヤモンドインザスカイ",
        "ツインクルツインクルリトルスター",
        "ハウアイワンダーワットユーアー",
    ],
}


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


def test_japanese_samples_include_all_verses():
    """日本の童謡・唱歌は1番だけへ戻らず、一般的な全番を収録している。"""
    for sample_id, expected_lines in FULL_JAPANESE_SAMPLE_LINES.items():
        lyrics = (SAMPLE_DIR / f"{sample_id}_lyrics.txt").read_text(encoding="utf-8")
        assert len([line for line in lyrics.splitlines() if line.strip()]) == expected_lines


@pytest.mark.parametrize(("sample_id", "expected_lines"), ENGLISH_SAMPLE_LINES.items())
def test_english_samples_keep_surface_and_reading(sample_id: str, expected_lines: list[str]):
    """XF表記と字幕は英語、歌唱に使う読みはカタカナに分ける。"""
    project = analyze_midi(SAMPLE_DIR / f"{sample_id}.mid")
    assert [line.xf_surface for line in project.lines] == expected_lines
    assert [line.xf_kana for line in project.lines] == ENGLISH_SAMPLE_KANA[sample_id]

    subtitle_lines = (SAMPLE_DIR / f"{sample_id}_lyrics.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert subtitle_lines == expected_lines
    align_lines(project, subtitle_lines)
    assert [line.original_text for line in project.lines] == expected_lines


@pytest.mark.parametrize("sample_id", SAMPLE_IDS)
def test_sample_midi_roundtrip(sample_id: str):
    song = gen_samples.SONGS[sample_id]
    score = [
        item for item in gen_samples.expanded_score(song) if isinstance(item, tuple)
    ]  # 休符/行区切りを除く
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

    # Xへ添付できる動画は140秒まで。動画工程は最後の歌唱音符から3秒の
    # 余韻に加え、最大4ページの使用語とクレジットを各3秒表示しうるため、
    # 最悪ケースの18秒を足しても上限内に収まることを保証する。
    duration = project.notes[-1].end_sec
    assert 10.0 <= duration
    assert duration + 18.0 <= 140.0, f"{sample_id}: video may reach {duration + 18.0}s"

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
