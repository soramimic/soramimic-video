from pathlib import Path

from helpers import build_xf_midi
from soramimic_video.musicxml import build_musicxml
from soramimic_video.project import Parody, ParodyLine, ParodyWord
from soramimic_video.synthesize import build_lyric_map
from soramimic_video.xfparse import analyze_midi


def _project(tmp_path: Path):
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 1200, 64)],  # 3音符目は小節をまたぐ
        lyric_events=[(480, "沈[し"), (720, "ず]"), (960, "む")],
    )
    return analyze_midi(midi)


def test_build_lyric_map_defaults_to_original(tmp_path: Path):
    project = _project(tmp_path)
    assert build_lyric_map(project) == {0: "シ", 1: "ズ", 2: "ム"}


def test_build_lyric_map_with_parody(tmp_path: Path):
    project = _project(tmp_path)
    project.parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="静", kana="シズオ", original="静",
                        original_surface="シズム", originalkana="シズム",
                        note_ids=[0, 1, 2], note_kana=["シ", "ズ", "オ"],
                    )
                ],
            )
        ],
    )
    assert build_lyric_map(project) == {0: "シ", 1: "ズ", 2: "オ"}


def test_build_lyric_map_warns_on_double_assignment(tmp_path: Path, caplog):
    # 同じ音符(id=2)を2単語が取ると後勝ち上書きになる。修正後の変換では
    # 起きないはずだが、将来の同種バグ検出のため警告を出すことを確認する。
    project = _project(tmp_path)
    project.parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="アロ", kana="アロ", original="",
                        original_surface="", originalkana="",
                        note_ids=[0, 1, 2], note_kana=["ア", "ロ", "ガ"],
                    ),
                    ParodyWord(
                        surface="オス", kana="オス", original="",
                        original_surface="", originalkana="",
                        note_ids=[2], note_kana=["オ"],
                    ),
                ],
            )
        ],
    )
    with caplog.at_level("WARNING", logger="soramimic_video.synthesize"):
        lyric_map = build_lyric_map(project)
    assert lyric_map[2] == "オ"  # 後勝ち(既存挙動は不変)
    assert any(
        "音符2" in r.getMessage() and "アロ" in r.getMessage() and "オス" in r.getMessage()
        for r in caplog.records
    )


def test_build_musicxml(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, build_lyric_map(project))
    assert "<divisions>480</divisions>" in xml
    assert "<text>シ</text>" in xml
    assert "<rest />" in xml  # 曲頭の休符
    # 3音符目(tick960〜2160)は小節境界(1920)をまたぐのでタイが付く
    assert '<tie type="start" />' in xml
    assert '<tie type="stop" />' in xml
    # タイの後半に歌詞は付かない(「ム」は1回だけ)
    assert xml.count("<text>ム</text>") == 1


def test_build_musicxml_transpose(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, {})
    down = build_musicxml(project, {}, transpose=-12)
    # 音名は同じままオクターブだけ1つ下がる
    for line, line_down in zip(xml.splitlines(), down.splitlines(), strict=True):
        if "<octave>" in line:
            octave = int(line.strip().removeprefix("<octave>").removesuffix("</octave>"))
            octave_down = int(
                line_down.strip().removeprefix("<octave>").removesuffix("</octave>")
            )
            assert octave_down == octave - 1
    assert "<octave>" in xml  # 比較対象が実在すること


def test_build_musicxml_tempo(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, {})
    assert '<sound tempo="120' in xml  # 500000us/beat = 120bpm
