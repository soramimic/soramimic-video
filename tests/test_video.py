import hashlib
import shutil
from pathlib import Path

import pytest

from helpers import build_xf_midi
from soramimic_video.project import Parody, ParodyLine, ParodyWord
from soramimic_video.video import build_ass, build_image_cues, download_image, write_slideshow
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
