import json
from pathlib import Path

import pytest

from soramimic_video.neutrino import (
    model_pitch_range,
    note_name_to_midi,
    parse_pitch_range,
)
from soramimic_video.octave import auto_octave_shift

# 実 model_info.json 相当のfixture(実NEUTRINO不要)。区切りは全角～/半角~が混在する。
_MODEL_INFO = {
    "MERROW": "<b>めろう</b><br>推奨音域：mid2A～hiE（A3～E5）<br>得意なBPM：80~140",
    "NAKUMO": "<b>ナクモ</b><br>推奨音域：mid1A~hiB（A2~B4）<br>得意なBPM：80~160",
    "SEVEN": "<b>No.7</b><br>推奨音域：mid2A~hiC（A3~C5）<br>得意なBPM：120~180",
    "YOKO": "<b>謡子</b><br>力強い声質が特徴です。<br>得意なBPM：90~110",  # 音域表記なし
    "None": "モデルが選択されていません。",
}


def _root(tmp_path: Path) -> Path:
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings" / "model_info.json").write_text(
        json.dumps(_MODEL_INFO, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


# ---- note_name_to_midi ----


def test_note_name_to_midi_basic():
    assert note_name_to_midi("C4") == 60  # 基準
    assert note_name_to_midi("A3") == 57
    assert note_name_to_midi("E5") == 76
    assert note_name_to_midi("A2") == 45
    assert note_name_to_midi("G2") == 43


def test_note_name_to_midi_accidentals():
    assert note_name_to_midi("C#5") == 73
    assert note_name_to_midi("Bb2") == 46  # B2=47 の半音下


def test_note_name_to_midi_invalid():
    with pytest.raises(ValueError):
        note_name_to_midi("H3")


# ---- parse_pitch_range ----


def test_parse_pitch_range_fullwidth_sep():
    assert parse_pitch_range(_MODEL_INFO["MERROW"]) == (57, 76)


def test_parse_pitch_range_halfwidth_sep():
    assert parse_pitch_range(_MODEL_INFO["NAKUMO"]) == (45, 71)


def test_parse_pitch_range_missing_returns_none():
    assert parse_pitch_range(_MODEL_INFO["YOKO"]) is None
    assert parse_pitch_range("推奨音域なしの説明文") is None


# ---- model_pitch_range(fixtureのroot) ----


def test_model_pitch_range_from_fixture(tmp_path: Path):
    root = _root(tmp_path)
    assert model_pitch_range("MERROW", root) == (57, 76)
    assert model_pitch_range("NAKUMO", root) == (45, 71)


def test_model_pitch_range_case_insensitive(tmp_path: Path):
    # フォーム等から小文字で来ても大文字キーに照合する
    assert model_pitch_range("merrow", _root(tmp_path)) == (57, 76)


def test_model_pitch_range_unknown_model(tmp_path: Path):
    assert model_pitch_range("NOSUCHMODEL", _root(tmp_path)) is None


def test_model_pitch_range_model_without_range(tmp_path: Path):
    assert model_pitch_range("YOKO", _root(tmp_path)) is None


def test_model_pitch_range_missing_file(tmp_path: Path):
    # settings/model_info.json が無い(rootだけ渡す)
    assert model_pitch_range("MERROW", tmp_path) is None


def test_model_pitch_range_broken_json(tmp_path: Path):
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings" / "model_info.json").write_text("{ broken", encoding="utf-8")
    assert model_pitch_range("MERROW", tmp_path) is None


def test_model_pitch_range_no_neutrino_root(monkeypatch):
    # NEUTRINO_ROOT未設定・root省略なら None(汎用音域へフォールバック)
    monkeypatch.delenv("NEUTRINO_ROOT", raising=False)
    assert model_pitch_range("MERROW") is None


# ---- 2オクターブより狭い/広い音域でも auto_octave_shift が扱える ----


def test_narrow_model_range_still_shifts(tmp_path: Path):
    # SEVEN: A3~C5 = 57~72(約1.25オクターブ、2オクターブより狭い)
    lo, hi = model_pitch_range("SEVEN", _root(tmp_path))
    assert (lo, hi) == (57, 72)
    # 高音曲(80,82,84)はこの狭い音域でも-12で 68,70,72 に収まる
    assert auto_octave_shift([80, 82, 84], 0, lo, hi) == -12


def test_wide_range_reduces_shifting():
    # 3オクターブ超の広い音域なら、多少高めの曲でもシフト不要と判定できる
    assert auto_octave_shift([80, 82, 84], 0, 48, 96) == 0
