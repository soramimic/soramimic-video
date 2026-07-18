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


def _two_line_project(tmp_path: Path):
    """1つの元歌詞行に2つのXF行が対応するプロジェクト(粒度テスト用)。"""
    from soramimic_video.align import align_lines

    midi = build_xf_midi(
        tmp_path / "two.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64), (1200, 240, 65)],
        lyric_events=[(480, "沈む"), (720, "ように"), (960, "/溶ける"), (1200, "ように")],
    )
    project = analyze_midi(midi)
    align_lines(project, ["沈むように 溶けるように"])
    # 2つのXF行(=2フレーズ)が同じ元歌詞行に対応する
    assert [ln.original_text for ln in project.lines] == ["沈むように 溶けるように"] * 2
    project.parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(line_id=project.lines[0].id, words=[
                ParodyWord(surface="静", kana="シズ", original="", original_surface="",
                           originalkana="", note_ids=[0, 1])]),
            ParodyLine(line_id=project.lines[1].id, words=[
                ParodyWord(surface="川", kana="カワ", original="", original_surface="",
                           originalkana="", note_ids=[2, 3])]),
        ],
    )
    return project


def _orig_texts(ass: str) -> list[str]:
    return [ln.split(",,")[-1].split("}")[-1]
            for ln in ass.splitlines() if ln.startswith("Dialogue:") and ",Original," in ln]


def _parody_texts(ass: str) -> list[str]:
    return [ln.split(",,")[-1].split("}")[-1]
            for ln in ass.splitlines()
            if ln.startswith("Dialogue:") and ",Parody," in ln and "\\fs" not in ln]


def test_build_ass_original_line_merges_group(tmp_path: Path):
    # 既定(original=line): 同じ元歌詞行に対応する2フレーズは1枚に畳まれ通しで出る
    project = _two_line_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font")
    assert _orig_texts(ass) == ["沈むように 溶けるように"]  # 2行ぶんが1枚に
    starts = [ln.split(",")[1] for ln in ass.splitlines()
              if ln.startswith("Dialogue:") and ",Original," in ln]
    # 1枚だけ: 開始=1フレーズ目の頭、終了=2フレーズ目の終わり(通しタイミング)
    assert len(starts) == 1
    assert _parody_texts(ass) == ["静", "川"]  # 替え歌は既定でフレーズ


def test_build_ass_original_phrase_splits(tmp_path: Path):
    project = _two_line_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", None, {"original": "phrase"})
    # 元歌詞の行を各フレーズの部分文字列に切り分けて別々に出す
    assert _orig_texts(ass) == ["沈むように", "溶けるように"]


def test_build_ass_parody_line_concatenates(tmp_path: Path):
    project = _two_line_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", None, {"parody": "line", "original": "line"})
    assert _parody_texts(ass) == ["静  川"]  # 同じ元歌詞行の替え歌を連結して1枚に
    assert _orig_texts(ass) == ["沈むように 溶けるように"]


def test_build_ass_granularity_from_layout_element(tmp_path: Path):
    # subtitle要素の granularity 指定が override より優先される
    import json

    from soramimic_video.layout import load_layout

    spec = tmp_path / "gran.json"
    spec.write_text(json.dumps({
        "elements": [
            {"type": "subtitle", "source": "original", "box": [0.02, 0.9, 0.96, 0.05],
             "granularity": "phrase"},
        ],
    }), encoding="utf-8")
    project = _two_line_project(tmp_path)
    # override で line を渡しても、要素の phrase が勝つ
    ass = build_ass(project, 1280, 720, "Font", load_layout(str(spec)), {"original": "line"})
    assert _orig_texts(ass) == ["沈むように", "溶けるように"]


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
    # ルビ文言 = kana のひらがな表示
    joined = "\n".join(ruby)
    assert "しず" in joined and "やま" in joined and "カワ" not in joined and "かわ" not in joined
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
def test_black_frame_creates_missing_dir(tmp_path: Path):
    # キュー画像ゼロのジョブではframesディレクトリを誰も作らない。
    # _black_frame自身が作らないと実ffmpegがCould not open fileで失敗する(実障害)
    from soramimic_video.video import _black_frame

    out = _black_frame(tmp_path / "video" / "frames", 64, 48)
    assert out.exists() and out.stat().st_size > 0


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


def _precache_image(work: Path, url: str) -> None:
    """ネットワークを使わないよう、画像キャッシュにダミー画像を事前配置する。"""
    from PIL import Image

    cache = work / "images"
    cache.mkdir(parents=True)
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    Image.new("RGB", (64, 48), "red").save(cache / f"{name}.png")


def test_image_cues_credit_from_wordlist_column(tmp_path: Path):
    # 単語リストにimage_credit列があればその文言を使う(Commons取得より優先)
    project = _project(tmp_path)
    project.parody.lines[0].words[0].wordlist_row["image_credit"] = "山田 太郎 (CC BY 2.0)"
    work = tmp_path / "video"
    _precache_image(work, "https://example.com/shizu.jpg")
    cues, credits = build_image_cues(project, work, 320, 180)
    assert len(cues) == 1
    assert credits[0]["credit"] == "山田 太郎 (CC BY 2.0)"


def test_image_cues_credit_fetched(tmp_path: Path, monkeypatch):
    # Commonsから取得したcredit_textがフレームデータとcredits一覧に入る
    import soramimic_video.video as video_mod

    project = _project(tmp_path)
    work = tmp_path / "video"
    _precache_image(work, "https://example.com/shizu.jpg")

    def fake_fetch(url, page, cache):
        assert page == "https://example.com/page"
        return {"artist": "山田 太郎", "license": "CC BY-SA 4.0",
                "attribution_required": True,
                "credit_text": "山田 太郎, CC BY-SA 4.0, via Wikimedia Commons"}

    monkeypatch.setattr(video_mod, "fetch_image_credit", fake_fetch)
    cues, credits = build_image_cues(project, work, 320, 180)
    assert len(cues) == 1
    assert credits[0]["credit"] == "山田 太郎, CC BY-SA 4.0, via Wikimedia Commons"
    # クレジットなしで作ったフレームとは別内容になる(焼き込まれている)
    monkeypatch.setattr(video_mod, "fetch_image_credit", lambda *a: None)
    cues2, credits2 = build_image_cues(project, tmp_path / "video2", 320, 180,
                                       image_cache=work / "images")
    assert credits2[0]["credit"] == ""
    assert cues[0].frame.name != cues2[0].frame.name


def test_write_credits_table(tmp_path: Path):
    from soramimic_video.video import write_credits

    path = write_credits([
        {"word": "静", "original": "静岡", "image": "http://img", "image_page": "http://page",
         "credit": "山田 太郎, CC BY-SA 4.0, via Wikimedia Commons"},
        {"word": "山", "original": "山田", "image": "http://img2", "image_page": "http://page2",
         "credit": ""},
    ], tmp_path)
    text = path.read_text(encoding="utf-8")
    assert ("| 静岡 | http://img | 山田 太郎, CC BY-SA 4.0, via Wikimedia Commons "
            "| http://page |") in text
    assert "| 山田 | http://img2 |  | http://page2 |" in text


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


# ---- 後奏で動画が切れるバグの回帰テスト ----
# song.wav(伴奏はMIDI全体をfluidsynthでレンダリングするため後奏込みで長い)が
# 最後の歌唱ノート+3秒より長い場合、動画の総尺は音声の実長に合わせる必要がある。

HAS_FFPROBE = shutil.which("ffprobe") is not None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_audio_duration_sec_reads_real_length(tmp_path: Path):
    import subprocess

    from soramimic_video.video import _audio_duration_sec

    wav = tmp_path / "silence.wav"
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "anullsrc=r=8000:cl=mono", "-t", "2.5", str(wav)],
        check=True, capture_output=True,
    )

    duration = _audio_duration_sec(wav)
    assert duration is not None
    assert abs(duration - 2.5) < 0.1


def test_audio_duration_sec_missing_ffprobe(tmp_path: Path, monkeypatch):
    from soramimic_video import video as video_mod

    def _raise() -> str:
        raise RuntimeError("ffprobe が見つかりません")

    monkeypatch.setattr(video_mod, "_ffprobe", _raise)
    assert video_mod._audio_duration_sec(tmp_path / "nope.wav") is None


@pytest.mark.skipif(not HAS_FFPROBE, reason="ffprobeがない")
def test_audio_duration_sec_ffprobe_failure_returns_none(tmp_path: Path):
    from soramimic_video.video import _audio_duration_sec

    # 存在しないファイルを渡すとffprobeがエラー終了する(returncode != 0)
    assert _audio_duration_sec(tmp_path / "does-not-exist.wav") is None


def test_resolve_total_sec_uses_audio_when_longer():
    from soramimic_video.video import _resolve_total_sec

    # 後奏があり音声の方が長いケース: 音声の実長が採用される
    assert _resolve_total_sec(10.0, 20.0) == 20.0


def test_resolve_total_sec_keeps_sung_end_when_audio_shorter():
    from soramimic_video.video import _resolve_total_sec

    # 音声側が短い(取得誤差等)ケース: 従来通り歌唱ノート側が採用される
    assert _resolve_total_sec(10.0, 5.0) == 10.0


def test_resolve_total_sec_falls_back_when_audio_duration_unknown():
    from soramimic_video.video import _resolve_total_sec

    # ffprobe失敗(None)のケース: 従来の計算にフォールバックする
    assert _resolve_total_sec(10.0, None) == 10.0
