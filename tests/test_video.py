import hashlib
import shutil
from pathlib import Path

import pytest

from helpers import build_xf_midi
from soramimic_video.project import Parody, ParodyLine, ParodyWord
from soramimic_video.video import build_ass, build_image_cues, write_slideshow
from soramimic_video.xfparse import analyze_midi

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _project(tmp_path: Path):
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64)],
        lyric_events=[(480, "沈[し"), (720, "ず]"), (960, "む")],
    )
    project = analyze_midi(midi)
    project.lines[0].original_text = "沈むように"
    project.parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="静", kana="シズ", original="静",
                        original_surface="シズ", originalkana="シズ",
                        note_ids=[0, 1], note_kana=["シ", "ズ"],
                        wordlist_row={
                            "image": "https://example.com/shizu.jpg",
                            "image_page": "https://example.com/page",
                        },
                    )
                ],
            )
        ],
    )
    return project


def test_build_ass(tmp_path: Path):
    project = _project(tmp_path)
    ass = build_ass(project, 1280, 720, "Hiragino Sans")
    assert "Style: Sub" in ass
    # 替え歌と元歌詞は1イベントに\N連結(別イベントだと折り返しで上下入れ替わる)
    assert ass.count("Dialogue:") == 1
    assert "静" in ass
    assert "沈むように" in ass
    # 同じ行内で替え歌が上(先)、元歌詞が下(後)
    line = next(ln for ln in ass.splitlines() if ln.startswith("Dialogue:"))
    assert line.index("静") < line.index("沈むように")


def test_build_ass_escapes_braces(tmp_path: Path):
    project = _project(tmp_path)
    project.lines[0].original_text = "て{す}と"
    ass = build_ass(project, 1280, 720, "Font")
    # ユーザーテキストの波括弧はエスケープされる(スタイル上書きタグの{ }とは別)
    assert "て(す)と" in ass
    assert "て{す}と" not in ass


def _ass_seconds(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_build_ass_events_do_not_overlap():
    # 隣接行の区間が接していても、字幕イベントは時間的に重ならない
    # (重なると切り替わりでlibassの衝突回避により字幕が上下にずれる)
    from soramimic_video.project import Line, Note, Project, SongInfo

    def note(i, start, end):
        return Note(id=i, midi_note=60, start_tick=0, end_tick=1,
                    start_sec=start, end_sec=end, line=i, surface="", kana="ア", raw="ア")

    notes = [note(0, 1.0, 2.0), note(1, 2.0, 3.0), note(2, 3.0, 4.0)]
    lines = [
        Line(id=i, xf_surface="ア", xf_kana="ア", note_ids=[i], original_text="ア")
        for i in range(3)
    ]
    project = Project(song=SongInfo(midi_path="", ticks_per_beat=480), notes=notes,
                      lines=lines)
    ass = build_ass(project, 1280, 720, "Font")
    spans = []
    for ln in ass.splitlines():
        if ln.startswith("Dialogue:"):
            parts = ln.split(",")
            spans.append((_ass_seconds(parts[1]), _ass_seconds(parts[2])))
    spans.sort()
    for (_, end), (nxt, _) in zip(spans, spans[1:], strict=False):
        assert end <= nxt, f"字幕が重なっている: end={end} > next_start={nxt}"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_image_cues_and_slideshow(tmp_path: Path):
    project = _project(tmp_path)
    work = tmp_path / "video"
    # ネットワークを使わないよう、キャッシュに画像を事前配置する
    url = "https://example.com/shizu.jpg"
    cache = work / "images"
    cache.mkdir(parents=True)
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    import subprocess

    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "color=red:s=64x48",
         "-frames:v", "1", str(cache / f"{name}.png")],
        check=True, capture_output=True,
    )

    cues, credits = build_image_cues(project, work, 320, 180)
    assert len(cues) == 1
    assert credits[0]["image_page"] == "https://example.com/page"
    # 単語の歌唱区間から始まる(tick480 @120bpm = 0.5s)
    assert abs(cues[0].start - 0.5) < 0.01

    out = write_slideshow(cues, work, 320, 180, total_sec=3.0)
    assert out.exists() and out.stat().st_size > 0
