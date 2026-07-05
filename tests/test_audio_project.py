from pathlib import Path

from soramimic_video.audio_project import (
    MoraNote,
    build_project,
    sec_to_tick,
    write_srt,
)
from soramimic_video.project import Project


def _mora(line: int, kana: str, start: float, end: float, note: int = 60) -> MoraNote:
    return MoraNote(line=line, kana=kana, start_sec=start, end_sec=end, midi_note=note)


def test_sec_to_tick_fixed_bpm():
    # 120BPM/480tpb: 1秒 = 2拍 = 960tick
    assert sec_to_tick(0.0) == 0
    assert sec_to_tick(1.0) == 960
    assert sec_to_tick(0.5, bpm=60.0) == 240


def test_build_project_basic():
    moras = [
        _mora(0, "シ", 1.0, 1.25, 65),
        _mora(0, "ズ", 1.25, 1.5, 67),
        _mora(1, "ト", 3.0, 3.4, 60),
    ]
    project = build_project(
        audio_path=Path("song.wav"),
        vocals_path=Path("vocals.wav"),
        accompaniment_path=Path("no_vocals.wav"),
        line_texts=["沈む", "溶ける"],
        mora_notes=moras,
    )
    assert [n.kana for n in project.notes] == ["シ", "ズ", "ト"]
    assert project.notes[0].start_tick == 960
    assert project.notes[0].end_tick == 1200
    assert [ln.xf_kana for ln in project.lines] == ["シズ", "ト"]
    assert project.lines[0].note_ids == [0, 1]
    assert project.lines[0].original_text == "沈む"
    assert project.song.midi_path == ""
    assert project.song.accompaniment_path == "no_vocals.wav"
    assert project.song.tempo_map == [[0, 500000]]
    # 行のカナ = 音符カナの連結(convertステージの前提)
    for ln in project.lines:
        assert ln.xf_kana == "".join(project.notes[i].kana for i in ln.note_ids)


def test_build_project_min_duration_and_overlap():
    moras = [
        _mora(0, "ア", 1.0, 1.01),  # 短すぎ → 最小長に伸長
        _mora(0, "イ", 1.02, 1.5),  # 伸長した前のモーラと重なる → 前をクリップ
    ]
    project = build_project(
        audio_path=Path("a.wav"),
        vocals_path=None,
        accompaniment_path=None,
        line_texts=["あい"],
        mora_notes=moras,
    )
    assert project.notes[0].end_sec == 1.02
    assert project.notes[0].end_tick <= project.notes[1].start_tick
    assert all(n.end_tick > n.start_tick for n in project.notes)


def test_build_project_keeps_lyric_order_and_clamps_inversion():
    """時刻が局所的に逆転していても歌詞順を保つ(並べ替えない)。"""
    moras = [
        _mora(0, "ア", 1.0, 1.2),
        _mora(0, "イ", 0.9, 1.1),  # 開始が前のモーラより早い → クランプ
        _mora(0, "ウ", 1.5, 1.8),
    ]
    project = build_project(
        audio_path=Path("a.wav"),
        vocals_path=None,
        accompaniment_path=None,
        line_texts=["あいう"],
        mora_notes=moras,
    )
    assert [n.kana for n in project.notes] == ["ア", "イ", "ウ"]
    starts = [n.start_sec for n in project.notes]
    assert starts == sorted(starts)
    assert all(n.end_sec > n.start_sec for n in project.notes)


def test_build_project_skips_empty_lines():
    moras = [_mora(2, "ア", 0.5, 0.8)]
    project = build_project(
        audio_path=Path("a.wav"),
        vocals_path=None,
        accompaniment_path=None,
        line_texts=["(間奏)", "", "あ"],
        mora_notes=moras,
    )
    assert len(project.lines) == 1
    assert project.lines[0].id == 0
    assert project.lines[0].original_text == "あ"
    assert project.notes[0].line == 0


def test_project_roundtrip_with_audio_fields(tmp_path: Path):
    project = build_project(
        audio_path=Path("song.wav"),
        vocals_path=Path("v.wav"),
        accompaniment_path=Path("a.wav"),
        line_texts=["あ"],
        mora_notes=[_mora(0, "ア", 0.0, 0.5)],
    )
    project.save(tmp_path)
    loaded = Project.load(tmp_path)
    assert loaded.song.audio_path == "song.wav"
    assert loaded.song.vocals_path == "v.wav"
    assert loaded.song.accompaniment_path == "a.wav"
    assert loaded.notes[0].kana == "ア"


def test_project_load_without_audio_fields(tmp_path: Path):
    """既存(MIDI由来)のproject.jsonも読める(後方互換)。"""
    import json

    data = {
        "version": 1,
        "song": {"midi_path": "song.mid", "ticks_per_beat": 480},
        "notes": [],
        "lines": [],
    }
    (tmp_path / "project.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = Project.load(tmp_path)
    assert loaded.song.audio_path is None


def test_write_srt(tmp_path: Path):
    path = write_srt(tmp_path / "x.srt", [(0.0, 1.5, "ア"), (61.25, 62.0, "イ")])
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,500" in text
    assert "00:01:01,250 --> 00:01:02,000" in text
    assert text.startswith("1\n")
