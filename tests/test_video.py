import hashlib
import shutil
from pathlib import Path

import pytest

from helpers import build_xf_midi
from soramimic_video.project import Line, Note, Parody, ParodyLine, ParodyWord, Project, SongInfo
from soramimic_video.video import (
    ImageCue,
    build_ass,
    build_image_cues,
    download_image,
    write_slideshow,
)
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
    assert "Style: Parody" in ass and "Style: Original" in ass
    assert ass.count("Dialogue:") == 2
    assert "静" in ass
    assert "沈むように" in ass
    # 替え歌=レイヤー1 / 元歌詞=レイヤー0 で衝突回避の対象にならない
    assert any(ln.startswith("Dialogue: 1,") and ",Parody," in ln for ln in ass.splitlines())
    assert any(ln.startswith("Dialogue: 0,") and ",Original," in ln for ln in ass.splitlines())


def test_build_ass_layers_and_no_overlap(tmp_path: Path):
    # 2行の歌唱区間が近接していても、表示区間は重ならない(位置が跳ねる原因)
    midi = build_xf_midi(
        tmp_path / "song2.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64), (1200, 240, 65)],
        lyric_events=[(480, "沈[し"), (720, "ず]"), (960, "/溶[と"), (1200, "け]")],
    )
    project = analyze_midi(midi)
    ass = build_ass(project, 1280, 720, "Font")
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    # 替え歌はレイヤー1、元歌詞はレイヤー0(衝突回避で上下が入れ替わらないように)
    assert all(ln.split(",")[0] == "Dialogue: 0" for ln in dialogues if ",Original," in ln)
    spans = []
    for ln in dialogues:
        parts = ln.split(",")
        spans.append((parts[1], parts[2], parts[3]))
    starts = sorted({s for s, _, _ in spans})
    ends = sorted({e for _, e, _ in spans})
    assert ends[0] <= starts[1]  # 1行目の終了 <= 2行目の開始


def test_build_ass_escapes_braces(tmp_path: Path):
    # 歌詞由来の{}はASSの制御タグにならないよう()に置換される
    # (行頭の {\an\pos} は build_ass 自身が付ける配置タグ)
    project = _project(tmp_path)
    project.lines[0].original_text = "て{す}と"
    ass = build_ass(project, 1280, 720, "Font")
    assert "て(す)と" in ass
    assert "{す}" not in ass


def test_build_ass_layout_subtitles(tmp_path: Path):
    # レイアウトのsubtitle要素で元歌詞の位置を変えられる。
    # subtitle要素があるレイアウトでは既定の字幕は使われない(parodyは出ない)
    import json

    from soramimic_video.layout import load_layout

    spec = tmp_path / "sub.json"
    spec.write_text(json.dumps({
        "elements": [
            {"type": "subtitle", "source": "original", "box": [0.1, 0.05, 0.8, 0.08],
             "size": 0.05, "color": "#ffcc00", "align": "left", "valign": "top"},
        ],
    }), encoding="utf-8")
    project = _project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", load_layout(str(spec)))
    assert "Style: Original,Font,36,&H0000CCFF," in ass  # #ffcc00 → BGR、0.05*720=36px
    assert "Style: Parody" not in ass and ",Parody," not in ass
    # 左上寄せ: \an7、pos はboxの左上(0.1*1280, 0.05*720)
    assert "\\an7\\pos(128,36)" in ass
    assert "沈むように" in ass


def _ruby_layout(tmp_path: Path, ruby: bool = True) -> str:
    import json

    spec = tmp_path / f"ruby_{ruby}.json"
    spec.write_text(json.dumps({
        "elements": [
            {"type": "subtitle", "source": "parody", "box": [0.02, 0.77, 0.96, 0.1],
             "size": 0.065, "color": "white", "bold": True, "ruby": ruby, "ruby_size": 0.5},
        ],
    }), encoding="utf-8")
    return str(spec)


def _multi_word_project(tmp_path: Path):
    project = _project(tmp_path)
    # 「静(シズ)」漢字=ルビあり / 「カワ」既にカナ=ルビなし / 「山(ヤマ)」漢字=ルビあり
    project.parody.lines[0].words = [
        ParodyWord(surface="静", kana="シズ", original="", original_surface="", originalkana="",
                   note_ids=[0]),
        ParodyWord(surface="カワ", kana="カワ", original="", original_surface="", originalkana="",
                   note_ids=[1]),
        ParodyWord(surface="山", kana="ヤマ", original="", original_surface="", originalkana="",
                   note_ids=[2]),
    ]
    return project


def _ruby_events(ass: str):
    # ルビイベントは \fs でフォントサイズを上書きしているので本文と区別できる
    return [ln for ln in ass.splitlines()
            if ln.startswith("Dialogue:") and ",Parody," in ln and "\\fs" in ln]


def _pos_x(line: str) -> float:
    import re

    return float(re.search(r"\\pos\(([-\d.]+),", line).group(1))


def test_build_ass_ruby_events(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _multi_word_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    ruby = _ruby_events(ass)
    # ルビが要る単語(静・山)だけ。既にカナの「カワ」は出さない
    assert len(ruby) == 2
    # ルビ文言 = kana
    joined = "\n".join(ruby)
    assert "シズ" in joined and "ヤマ" in joined and "カワ" not in joined
    # 本文パロディイベント(\fsなし)と同じ開始・終了区間
    body = next(ln for ln in ass.splitlines()
                if ln.startswith("Dialogue:") and ",Parody," in ln and "\\fs" not in ln)
    bstart, bend = body.split(",")[1], body.split(",")[2]
    for ln in ruby:
        assert ln.split(",")[1] == bstart and ln.split(",")[2] == bend
        assert ln.split(",")[0] == "Dialogue: 1"  # 本文と同じレイヤー1


def test_build_ass_ruby_positions_monotonic(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _multi_word_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    xs = [_pos_x(ln) for ln in _ruby_events(ass)]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)  # 単語順に単調増加


def test_build_ass_ruby_disabled(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _multi_word_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path, ruby=False)))
    assert _ruby_events(ass) == []  # ruby=false ならルビイベントは出ない


def test_build_ass_no_ruby_by_default(tmp_path: Path):
    # 既定字幕(DEFAULT_SUBTITLES, ruby=false)ではルビは出ない
    project = _multi_word_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font")
    assert _ruby_events(ass) == []


def test_needs_ruby():
    from soramimic_video.video import _needs_ruby

    assert _needs_ruby("静", "シズ")  # 漢字
    assert not _needs_ruby("カワ", "カワ")  # 既にカタカナで同じ
    assert not _needs_ruby("しずむ", "シズム")  # ひらがな⇔カタカナで同じ
    assert not _needs_ruby("トウキョウ", "トーキョー")  # 長音表記ゆれを吸収
    assert not _needs_ruby("", "シズ")  # 表記が空ならルビなし


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


def test_image_cues_fallback_for_unknown_word(tmp_path: Path):
    # 単語リストに行がない単語(未知語)は、fallback定義があればフレームが出る
    import json

    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    project.parody.lines[0].words[0].wordlist_row = None  # 行なし = 未知語

    # fallbackなし・画像のみのレイアウトでは表示できずスキップされる
    plain = tmp_path / "plain.json"
    plain.write_text(
        json.dumps({"elements": [{"type": "image", "box": [0, 0, 1, 0.7]}]}), encoding="utf-8"
    )
    cues, _ = build_image_cues(project, tmp_path / "v1", 320, 180, layout=load_layout(str(plain)))
    assert cues == []

    # fallbackありのレイアウトでは未知語のフレームが出る(画像なしでもテキストで表示)
    fb = tmp_path / "fb.json"
    fb.write_text(json.dumps({
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
        "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2],
                      "size": 0.1}],
    }), encoding="utf-8")
    cues2, _ = build_image_cues(project, tmp_path / "v2", 320, 180, layout=load_layout(str(fb)))
    assert len(cues2) == 1
    assert cues2[0].frame.exists()


def test_image_cues_require_skips_empty_column(tmp_path: Path):
    # requireで、行はあるが列が欠ける単語の要素だけ隠せる。列が全部空+画像なしなら
    # 表示できずスキップされる
    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    project.parody.lines[0].words[0].wordlist_row = {"death": ""}  # 画像なし・death空
    layout = load_layout(str(_write(tmp_path / "req.json", {
        "elements": [
            {"type": "text", "text": "没年 {death}", "box": [0.1, 0.3, 0.8, 0.2],
             "require": "death"},
        ],
    })))
    cues, _ = build_image_cues(project, tmp_path / "v", 320, 180, layout=layout)
    assert cues == []  # require要素が空でテキストが無く、画像もない → スキップ


def _write(path: Path, obj) -> Path:
    import json

    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _two_word_project() -> Project:
    # 2単語を大きく離して配置(単語0: 0.5〜0.75s / 単語1: 4.5〜4.75s、隙間3.75s)
    song = SongInfo(midi_path="mysong.mid", ticks_per_beat=480)
    notes = [
        Note(0, 60, 0, 240, 0.5, 0.75, 0, "静", "シズ", ""),
        Note(1, 62, 0, 240, 4.5, 4.75, 1, "山", "ヤマ", ""),
    ]
    lines = [Line(0, "", "", [0]), Line(1, "", "", [1])]
    parody = Parody(wordlist="test", lines=[
        ParodyLine(0, [ParodyWord("静", "シズ", "", "", "", [0])]),
        ParodyLine(1, [ParodyWord("山", "ヤマ", "", "", "", [1])]),
    ])
    return Project(song=song, notes=notes, lines=lines, parody=parody)


def test_hold_next_extends_show_end(tmp_path: Path):
    project = _two_word_project()
    els = {"elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.3],
                         "size": 0.1}]}
    from soramimic_video.layout import load_layout

    # 既定: 3秒(HOLD_MAX_SEC)で上限。1単語目は3.75s(0.75+3.0)で切れる
    cap = load_layout(str(_write(tmp_path / "cap.json", els)))
    cues, _ = build_image_cues(project, tmp_path / "cap", 320, 180, layout=cap)
    assert len(cues) == 2
    assert abs(cues[0].end - 3.75) < 0.01
    assert abs(cues[1].end - 7.75) < 0.01  # 最終単語は end+3.0

    # hold=next: 1単語目は次の歌唱(4.5s)まで持続。最終単語は end 止め(後奏はidle/黒)
    hold = load_layout(str(_write(tmp_path / "hold.json", {**els, "hold": "next"})))
    cues_h, _ = build_image_cues(project, tmp_path / "hold", 320, 180, layout=hold)
    assert abs(cues_h[0].end - 4.5) < 0.01
    assert abs(cues_h[1].end - 4.75) < 0.01


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_slideshow_idle_fill(tmp_path: Path):
    from PIL import Image

    work = tmp_path / "video"
    (work / "frames").mkdir(parents=True)
    idle = work / "frames" / "idle.png"
    Image.new("RGB", (320, 180), "navy").save(idle)
    cue_frame = work / "frames" / "cue.png"
    Image.new("RGB", (320, 180), "red").save(cue_frame)
    cues = [ImageCue(start=1.0, end=2.0, frame=cue_frame)]
    out = write_slideshow(cues, work, 320, 180, total_sec=3.0, idle_frame=idle)
    txt = (work / "slideshow.txt").read_text(encoding="utf-8")
    assert "idle.png" in txt  # 前奏(0〜1s)・後奏(2〜3s)がidleで埋まる
    assert "black_" not in txt  # idle_frame指定時は黒フレームを使わない
    assert out.exists() and out.stat().st_size > 0


def test_download_image_local_path(tmp_path: Path):
    # ローカルパスの画像はコピーで取り込む(生成・ローカル単語リスト用)
    src = tmp_path / "portrait.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0dummy")
    cache = tmp_path / "cache"
    got = download_image(str(src), cache)
    assert got is not None and got.exists()
    assert got.read_bytes() == src.read_bytes()
    assert got.suffix == ".jpg"


def test_download_image_file_url(tmp_path: Path):
    src = tmp_path / "p.png"
    src.write_bytes(b"\x89PNGdummy")
    got = download_image(f"file://{src}", tmp_path / "cache")
    assert got is not None and got.read_bytes() == src.read_bytes()


def test_download_image_missing_local(tmp_path: Path):
    assert download_image(str(tmp_path / "nope.jpg"), tmp_path / "cache") is None
