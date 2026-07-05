from pathlib import Path

from helpers import build_xf_midi
from soramimi_video.musicxml import build_musicxml
from soramimi_video.project import Parody, ParodyLine, ParodyWord
from soramimi_video.synthesize import build_lyric_map
from soramimi_video.xfparse import analyze_midi


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


def test_build_musicxml_tempo(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, {})
    assert '<sound tempo="120' in xml  # 500000us/beat = 120bpm
