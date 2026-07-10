"""VOICEVOX歌唱バックエンド(voicevox.py)のテスト。

build_score(休符埋め・モーラ分割・移調・フレーム丸め)と、HTTPをモックした
run_voicevoxのフローを確認する。エンジンが起動していれば実機E2Eも1本走らせる
(いなければ ffmpeg 系と同様にスキップ)。
"""

from __future__ import annotations

import io
import wave

import pytest

import soramimic_video.voicevox as vv
from soramimic_video.project import Note, Project, SongInfo
from soramimic_video.voicevox import (
    FRAME_RATE,
    build_score,
    run_voicevox,
    split_voicevox_moras,
)

ENGINE_URL = "http://127.0.0.1:50021"


def _note(id: int, midi: int, start_sec: float, end_sec: float, kana: str) -> Note:
    return Note(
        id=id,
        midi_note=midi,
        start_tick=int(start_sec * 480),
        end_tick=int(end_sec * 480),
        start_sec=start_sec,
        end_sec=end_sec,
        line=0,
        surface=kana,
        kana=kana,
        raw="",
    )


def _project(notes: list[Note]) -> Project:
    return Project(song=SongInfo(midi_path="", ticks_per_beat=480), notes=notes)


def _valid_wav_bytes(seconds: float = 0.1, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


# ---- split_voicevox_moras ----


def test_split_moras_single_and_yoon():
    assert split_voicevox_moras("シ") == ["シ"]
    assert split_voicevox_moras("シャ") == ["シャ"]  # 拗音は1モーラ
    assert split_voicevox_moras("サト") == ["サ", "ト"]


def test_split_moras_long_vowel_becomes_vowel():
    # 「ー」はVOICEVOXが弾くので直前の母音に置換する
    assert split_voicevox_moras("ラー") == ["ラ", "ア"]
    assert split_voicevox_moras("ドー") == ["ド", "オ"]
    assert split_voicevox_moras("キャー") == ["キャ", "ア"]
    assert split_voicevox_moras("ー") == ["ア"]  # 前音が無ければア


# ---- build_score ----


def test_build_score_leading_rest_from_zero():
    score = build_score(_project([_note(0, 60, 0.5, 1.0, "ド")]))
    notes = score["notes"]
    assert notes[0]["key"] is None
    assert notes[0]["lyric"] == ""
    # 0.5秒 = round(0.5*93.75) = 47フレームの休符
    assert notes[0]["frame_length"] == round(0.5 * FRAME_RATE)
    assert notes[1]["key"] == 60
    assert notes[1]["lyric"] == "ド"


def test_build_score_absorbs_one_frame_gap():
    # 1フレームだけの休符はエンジンが500を返すので、直前の音符を伸ばして埋める。
    # ド: 1.0秒 = 93.75 → 94フレームで終わり、レ: 1.0107秒 ≒ 95フレーム開始(隙間1)
    notes = [_note(0, 60, 0.5, 1.0, "ド"), _note(1, 62, 95 / FRAME_RATE, 1.5, "レ")]
    score = build_score(_project(notes))
    keys = [n["key"] for n in score["notes"]]
    assert keys == [None, 60, 62]  # 1フレーム休符は出ない
    # ドは 47〜94フレーム(47個)+吸収した1フレーム
    assert score["notes"][1]["frame_length"] == 94 - round(0.5 * FRAME_RATE) + 1
    # どの休符も2フレーム以上(エンジン制約)
    assert all(
        n["frame_length"] >= 2 for n in score["notes"] if n["key"] is None
    )


def test_build_score_one_frame_gap_at_head():
    # 曲頭の1フレーム休符は音符の前倒しで吸収する
    score = build_score(_project([_note(0, 60, 1 / FRAME_RATE, 0.5, "ド")]))
    assert score["notes"][0]["key"] == 60
    assert score["notes"][0]["frame_length"] == round(0.5 * FRAME_RATE)


def test_build_score_transpose():
    score = build_score(_project([_note(0, 60, 0.5, 1.0, "ド")]), transpose=3)
    pitched = [n for n in score["notes"] if n["key"] is not None]
    assert pitched[0]["key"] == 63


def test_build_score_fills_gap_with_rest():
    notes = [_note(0, 60, 0.5, 1.0, "ド"), _note(1, 62, 1.5, 2.0, "レ")]
    score = build_score(_project(notes))
    keys = [n["key"] for n in score["notes"]]
    # 休符, ド, 休符(隙間), レ
    assert keys == [None, 60, None, 62]
    gap = score["notes"][2]
    assert gap["frame_length"] == round(1.5 * FRAME_RATE) - round(1.0 * FRAME_RATE)


def test_build_score_splits_multimora_note_across_frames():
    # kana="ラー" 1音符 → ラ・ア の2ノーツにフレーム分配、合計は元の長さ
    score = build_score(_project([_note(0, 60, 1.0, 2.0, "ラー")]))
    pitched = [n for n in score["notes"] if n["key"] is not None]
    assert [n["lyric"] for n in pitched] == ["ラ", "ア"]
    total = sum(n["frame_length"] for n in pitched)
    assert total == round(2.0 * FRAME_RATE) - round(1.0 * FRAME_RATE)
    assert all(n["frame_length"] >= 1 for n in pitched)


def test_build_score_clips_overlap():
    # 音符が重なる: 後の音符は前の終端まで切り詰められ、絶対時間は単調増加
    notes = [_note(0, 60, 0.5, 1.5, "ド"), _note(1, 62, 1.0, 2.0, "レ")]
    score = build_score(_project(notes))
    frames = score["notes"]
    # 累積フレームが単調増加(重なりで負の休符が出ない)
    assert all(n["frame_length"] >= 1 for n in frames)


def test_build_score_empty_raises():
    with pytest.raises(ValueError):
        build_score(_project([]))


# ---- run_voicevox (HTTPモック) ----


class _FakeResp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.text = ""

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def test_run_voicevox_http_flow(tmp_path, monkeypatch):
    calls = {}

    singers = [
        {"name": "ずんだもん", "styles": [
            {"name": "ノーマル", "id": 3003, "type": "frame_decode"}]},
        {"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 6000, "type": "sing"}]},
    ]
    wav = _valid_wav_bytes()

    def fake_get(url, timeout=5):
        return _FakeResp(json_data=singers)

    def fake_post(url, params=None, json=None, timeout=None):
        if "sing_frame_audio_query" in url:
            calls["query_speaker"] = params["speaker"]
            calls["score"] = json
            return _FakeResp(json_data={"f0": [0.0], "phonemes": []})
        calls["synth_speaker"] = params["speaker"]
        return _FakeResp(content=wav)

    monkeypatch.setattr(vv.requests, "get", fake_get)
    monkeypatch.setattr(vv.requests, "post", fake_post)

    out = run_voicevox(
        _project([_note(0, 60, 0.5, 1.0, "ド")]),
        tmp_path,
        style_id=3003,
    )
    assert out.exists()
    # frame_decodeを選んだので先生は歌の先生6000、合成は3003
    assert calls["query_speaker"] == 6000
    assert calls["synth_speaker"] == 3003


def test_run_voicevox_sing_style_is_its_own_teacher(tmp_path, monkeypatch):
    calls = {}
    singers = [{"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 6000, "type": "sing"}]}]
    monkeypatch.setattr(vv.requests, "get", lambda url, timeout=5: _FakeResp(json_data=singers))

    def fake_post(url, params=None, json=None, timeout=None):
        if "sing_frame_audio_query" in url:
            calls["query_speaker"] = params["speaker"]
            return _FakeResp(json_data={"f0": [], "phonemes": []})
        calls["synth_speaker"] = params["speaker"]
        return _FakeResp(content=_valid_wav_bytes())

    monkeypatch.setattr(vv.requests, "post", fake_post)
    run_voicevox(_project([_note(0, 60, 0.5, 1.0, "ラ")]), tmp_path, style_id=6000)
    assert calls["query_speaker"] == 6000
    assert calls["synth_speaker"] == 6000


def test_run_voicevox_engine_unreachable(tmp_path, monkeypatch):
    import requests as real_requests

    def boom(*a, **k):
        raise real_requests.ConnectionError("refused")

    monkeypatch.setattr(vv.requests, "get", boom)
    monkeypatch.setattr(vv.requests, "post", boom)
    with pytest.raises(RuntimeError, match="VOICEVOXエンジンに接続できません"):
        run_voicevox(_project([_note(0, 60, 0.5, 1.0, "ド")]), tmp_path)


# ---- dispatch ----


def test_synthesize_dispatches_to_voicevox(tmp_path, monkeypatch):
    import soramimic_video.synthesize as syn

    called = {}

    def fake_run_voicevox(project, project_dir, **kw):
        called.update(kw)
        return project_dir / "vocal.wav"

    monkeypatch.setattr(vv, "run_voicevox", fake_run_voicevox)
    syn.synthesize(
        _project([_note(0, 60, 0.5, 1.0, "ド")]),
        tmp_path,
        synthesizer="voicevox",
        voicevox_style=3001,
        transpose=2,
    )
    assert called["style_id"] == 3001
    assert called["transpose"] == 2


def test_synthesize_rejects_unknown_backend(tmp_path):
    import soramimic_video.synthesize as syn

    with pytest.raises(ValueError, match="未対応の合成エンジン"):
        syn.synthesize(_project([_note(0, 60, 0.5, 1.0, "ド")]), tmp_path, synthesizer="foo")


# ---- 実機E2E(エンジンが起動していれば) ----


def _engine_up() -> bool:
    import requests

    try:
        requests.get(f"{ENGINE_URL}/version", timeout=1).raise_for_status()
        return True
    except requests.RequestException:
        return False


@pytest.mark.skipif(not _engine_up(), reason="VOICEVOXエンジンが起動していない")
def test_voicevox_end_to_end(tmp_path):
    notes = [
        _note(0, 60, 0.5, 1.0, "ド"),
        _note(1, 62, 1.0, 1.5, "レ"),
        _note(2, 64, 1.5, 2.5, "ミー"),  # 長音を含む
    ]
    out = run_voicevox(_project(notes), tmp_path, style_id=3003)
    assert out.exists()
    with wave.open(str(out)) as w:
        assert w.getnframes() > 0
        dur = w.getnframes() / w.getframerate()
    # 楽譜末尾は2.5秒なので、生成WAVもおおよそ2.5秒(絶対時間を保つ)
    assert dur == pytest.approx(2.5, abs=0.3)
