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
    LEAD_REST_FRAMES,
    build_score,
    run_voicevox,
    split_score,
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


# ---- auto_octave_shift ----


def test_auto_octave_shift_high_song_goes_down():
    # 実曲相当: 67〜89(高すぎ)は-12で55〜77になり安全域(54〜78)に全部入る
    keys = list(range(67, 90))
    assert vv.auto_octave_shift(keys) == -12


def test_auto_octave_shift_in_range_stays():
    assert vv.auto_octave_shift([60, 65, 70, 75]) == 0


def test_auto_octave_shift_considers_user_transpose():
    # ユーザーが既に-12している場合は追加調整不要
    keys = list(range(67, 90))
    assert vv.auto_octave_shift(keys, transpose=-12) == 0


def test_auto_octave_shift_low_song_goes_up():
    assert vv.auto_octave_shift(list(range(40, 60))) == 12


def test_auto_octave_shift_empty():
    assert vv.auto_octave_shift([]) == 0


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


# ---- split_score ----


def _seg(key, length, lyric=""):
    return {"key": key, "frame_length": length, "lyric": lyric}


def test_split_score_splits_within_rest_and_preserves_frames():
    score = {"notes": [_seg(60, 60, "ド"), _seg(None, 50), _seg(62, 60, "レ")]}
    chunks = split_score(score, max_sec=1.0, min_rest_sec=0.5)
    assert len(chunks) == 2
    # 各チャンク境界は休符(音符の途中では切らない)
    assert chunks[0].notes[-1]["key"] is None
    assert chunks[1].notes[0]["key"] is None
    # 2番目のチャンク先頭休符は LEAD 以上
    assert chunks[1].notes[0]["frame_length"] >= LEAD_REST_FRAMES
    # start_frame は前チャンクのframe_length合計(絶対時間が保たれる)
    assert chunks[1].start_frame == chunks[0].frame_length
    # 合計フレーム保存
    assert sum(c.frame_length for c in chunks) == 170
    # 音符のフレームは元のまま(途中で分割されない)
    pitched = [n for c in chunks for n in c.notes if n["key"] is not None]
    assert [(n["key"], n["frame_length"]) for n in pitched] == [(60, 60), (62, 60)]


def test_split_score_short_rest_not_a_candidate():
    # min_rest_sec 未満の休符では切らない(1チャンクのまま超過を許す)
    score = {"notes": [_seg(60, 60, "ド"), _seg(None, 40), _seg(62, 60, "レ")]}
    chunks = split_score(score, max_sec=1.0, min_rest_sec=0.5)
    assert len(chunks) == 1
    assert chunks[0].frame_length == 160


def test_split_score_no_candidate_single_chunk():
    # 分割候補(十分長い休符)が無ければ1チャンク
    score = {"notes": [_seg(60, 200, "ドー")]}
    chunks = split_score(score, max_sec=1.0)
    assert len(chunks) == 1
    assert chunks[0].start_frame == 0
    assert chunks[0].frame_length == 200


def test_split_score_avoids_one_frame_rest():
    # 前チャンク末尾に1フレーム休符が出そうな場合は境界で切る
    score = {"notes": [_seg(60, 92, "ド"), _seg(None, 50), _seg(62, 60, "レ")]}
    chunks = split_score(score, max_sec=1.0, min_rest_sec=0.5)
    assert len(chunks) == 2
    # どの休符も2フレーム以上(1フレーム休符を作らない)
    for c in chunks:
        for n in c.notes:
            if n["key"] is None:
                assert n["frame_length"] >= 2
    # 音符境界で切ったので chunk0 は音符で終わり、chunk1 は休符始まり
    assert chunks[0].notes[-1]["key"] == 60
    assert chunks[1].notes[0]["key"] is None
    assert chunks[1].start_frame == 92
    assert sum(c.frame_length for c in chunks) == 202


# ---- run_voicevox チャンク合成 ----


def _wav_const(nsamples: int, value: int, rate: int = 24000) -> bytes:
    import array

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array("h", [value] * nsamples).tobytes())
    return buf.getvalue()


def _read_samples(path) -> list[int]:
    import array

    with wave.open(str(path)) as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    return list(a)


def test_run_voicevox_chunked_concat(tmp_path, monkeypatch):
    singers = [{"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 6000, "type": "sing"}]}]
    monkeypatch.setattr(vv.requests, "get", lambda url, timeout=5: _FakeResp(json_data=singers))

    posts = {"query": 0, "synth": 0}

    def fake_post(url, params=None, json=None, timeout=None):
        if "sing_frame_audio_query" in url:
            posts["query"] += 1
            return _FakeResp(json_data=json)  # クエリはスコアをそのまま返す
        posts["synth"] += 1
        frames = sum(n["frame_length"] for n in json["notes"])
        # チャンクごとに識別できる定数サンプルで埋める(結合位置を検証)
        return _FakeResp(content=_wav_const(frames * 256, 1000 * posts["synth"]))

    monkeypatch.setattr(vv.requests, "post", fake_post)

    # build_score上で [note60(66), rest(56), note62(66)] = 188frame になる曲。
    notes = [_note(0, 60, 0.0, 0.7, "ド"), _note(1, 62, 1.3, 2.0, "レ")]
    out = run_voicevox(_project(notes), tmp_path, style_id=6000, chunk_sec=0.7)

    assert posts["query"] == 2 and posts["synth"] == 2  # 2チャンク合成
    samples = _read_samples(out)
    assert len(samples) == 188 * 256  # 絶対フレーム位置で連結
    # chunk0(0..66frame)は1000、chunk1(66..188frame)は2000で埋まっている
    assert set(samples[: 66 * 256]) == {1000}
    assert set(samples[66 * 256 :]) == {2000}


def test_run_voicevox_skips_pure_rest_chunk(tmp_path, monkeypatch):
    # 歌い出しが遅い曲: 長い先頭休符の途中で切れて「休符のみのチャンク」ができる。
    # 純休符チャンクはエンジンに送らず、結合後は無音になること。
    singers = [{"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 6000, "type": "sing"}]}]
    monkeypatch.setattr(vv.requests, "get", lambda url, timeout=5: _FakeResp(json_data=singers))

    posts = {"query": 0, "synth": 0}

    def fake_post(url, params=None, json=None, timeout=None):
        if "sing_frame_audio_query" in url:
            posts["query"] += 1
            # 純休符チャンクが送られてきたらここで検出する
            assert any(n["key"] is not None for n in json["notes"])
            return _FakeResp(json_data=json)
        posts["synth"] += 1
        frames = sum(n["frame_length"] for n in json["notes"])
        return _FakeResp(content=_wav_const(frames * 256, 1000))

    monkeypatch.setattr(vv.requests, "post", fake_post)

    # build_score上で [rest(141), note60(47)] = 188frame。chunk_sec=0.7(max 65.625frame)
    # では先頭休符の途中(65frame)で切れ、chunk0が純休符になる。
    notes = [_note(0, 60, 1.5, 2.0, "ド")]
    out = run_voicevox(_project(notes), tmp_path, style_id=6000, chunk_sec=0.7)

    # 事前条件の確認: 実際に純休符チャンク+非休符チャンクに分かれている
    chunks = vv.split_score(build_score(_project(notes)), max_sec=0.7)
    assert len(chunks) == 2
    assert all(n["key"] is None for n in chunks[0].notes)

    # (a) エンジンへのリクエストは非休符チャンクの1回だけ
    assert posts["query"] == 1 and posts["synth"] == 1
    # (b) 総サンプル数が正しく、スキップ区間(chunk0)は無音
    samples = _read_samples(out)
    assert len(samples) == 188 * 256
    cut = chunks[1].start_frame * 256
    assert set(samples[:cut]) == {0}
    assert set(samples[cut:]) == {1000}


def test_run_voicevox_chunk_disabled_single_request(tmp_path, monkeypatch):
    singers = [{"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 6000, "type": "sing"}]}]
    monkeypatch.setattr(vv.requests, "get", lambda url, timeout=5: _FakeResp(json_data=singers))

    posts = {"query": 0, "synth": 0}

    def fake_post(url, params=None, json=None, timeout=None):
        if "sing_frame_audio_query" in url:
            posts["query"] += 1
            return _FakeResp(json_data=json)
        posts["synth"] += 1
        return _FakeResp(content=_valid_wav_bytes())

    monkeypatch.setattr(vv.requests, "post", fake_post)

    # チャンク有効なら分割される曲でも chunk_sec=0 なら1リクエスト
    notes = [_note(0, 60, 0.0, 0.7, "ド"), _note(1, 62, 1.3, 2.0, "レ")]
    run_voicevox(_project(notes), tmp_path, style_id=6000, chunk_sec=0.0)
    assert posts["query"] == 1 and posts["synth"] == 1


def test_run_voicevox_engine_aborted_midrequest(tmp_path, monkeypatch):
    import requests as real_requests

    singers = [{"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 6000, "type": "sing"}]}]
    monkeypatch.setattr(vv.requests, "get", lambda url, timeout=5: _FakeResp(json_data=singers))

    def boom(url, params=None, json=None, timeout=None):
        raise real_requests.ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed "
            "connection without response'))"
        )

    monkeypatch.setattr(vv.requests, "post", boom)
    with pytest.raises(RuntimeError, match="処理中に異常終了しました"):
        run_voicevox(_project([_note(0, 60, 0.5, 1.0, "ラ")]), tmp_path, style_id=6000)


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
