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
    # original=line: 同じ元歌詞行に対応する2フレーズは1枚に畳まれ通しで出る
    project = _two_line_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font", None, {"original": "line"})
    assert _orig_texts(ass) == ["沈むように 溶けるように"]  # 2行ぶんが1枚に
    starts = [ln.split(",")[1] for ln in ass.splitlines()
              if ln.startswith("Dialogue:") and ",Original," in ln]
    # 1枚だけ: 開始=1フレーズ目の頭、終了=2フレーズ目の終わり(通しタイミング)
    assert len(starts) == 1
    assert _parody_texts(ass) == ["静", "川"]  # 替え歌は既定でフレーズ


def test_build_ass_default_is_phrase(tmp_path: Path):
    # 既定(override・要素指定なし): 替え歌・元歌詞ともフレーズ単位
    project = _two_line_project(tmp_path)
    ass = build_ass(project, 1280, 720, "Font")
    # 元歌詞の行を各フレーズの部分文字列に切り分けて別々に出す(行全文にはしない)
    assert _orig_texts(ass) == ["沈むように", "溶けるように"]
    assert _parody_texts(ass) == ["静", "川"]


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
    assert _needs_ruby("時計", "トケー")  # 漢字は読みの表記に関わらずルビ
    assert not _needs_ruby("カワ", "カワ")  # 既にカタカナで同じ
    assert not _needs_ruby("しずむ", "シズム")  # ひらがな⇔カタカナで同じ
    assert not _needs_ruby("トウキョウ", "トーキョー")  # 長音表記ゆれを吸収
    assert not _needs_ruby("ウィキ", "ウイキ")  # 全カナ表記は発音とゆれてもルビ不要
    assert not _needs_ruby("こんにちは", "コンニチワ")  # ひらがな表記も同様
    assert not _needs_ruby("", "シズ")  # 表記が空ならルビなし
    # 読みを持たない記号(中黒・イコール・空白)は判定から外す=実質全カナならルビ不要
    assert not _needs_ruby("バリッシュ・コノル", "バリッシュコノル")
    assert not _needs_ruby("ジャン=ピエール", "ジャンピエール")
    assert not _needs_ruby("ドン キホーテ", "ドンキホーテ")
    assert _needs_ruby("アテル＝参", "アテルサン")  # 記号を除いても漢字が残ればルビ対象


def test_ruby_segments():
    from soramimic_video.video import _ruby_segments

    # カナ混じり: 漢字部分にだけ読みが割り当たる(「シノノ」はそのまま読めるので対象外)
    assert _ruby_segments("燦花シノノ", "サンカシノノ") == [(0, 2, "サンカ")]
    # 全部漢字: 単語全体が1ラン
    assert _ruby_segments("空色", "ソライロ") == [(0, 2, "ソライロ")]
    # カナ挟み: ランごとに読みが分かれる
    assert _ruby_segments("夜ノ街", "ヨルノマチ") == [(0, 1, "ヨル"), (2, 3, "マチ")]
    # 送りがな(ひらがな)もカナランとして扱う
    assert _ruby_segments("走る", "ハシル") == [(0, 1, "ハシ")]
    # 長音の表記ゆれは吸収する(ケイ⇔ケー)
    assert _ruby_segments("少女ケイ", "ショージョケー") == [(0, 2, "ショージョ")]
    # 対応づけ不能(カナランが読みと合わない)→ None でフォールバック
    assert _ruby_segments("コーヒ", "コーヒー") is None
    assert _ruby_segments("静カ", "シズケサ") is None
    assert _ruby_segments("", "シズ") is None
    # 読みを持たない記号(中黒等)はカナ扱い。全部カナ+記号ならルビを振る範囲は無い
    assert _ruby_segments("バリッシュ・コノル", "バリッシュコノル") == []
    # 記号+漢字の混在では漢字部分にだけ読みが割り当たる
    assert _ruby_segments("アテル＝参", "アテルサン") == [(4, 5, "サン")]
    assert _ruby_segments("参・アテル", "サンアテル") == [(0, 1, "サン")]


def test_build_ass_ruby_partial(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    # 混在表記「燦花シノノ」: ルビは漢字部分の読み「さんか」だけ
    project.parody.lines[0].words = [
        ParodyWord(surface="燦花シノノ", kana="サンカシノノ", original="", original_surface="",
                   originalkana="", note_ids=[0]),
    ]
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    ruby = _ruby_events(ass)
    assert len(ruby) == 1
    assert "さんか" in ruby[0] and "しのの" not in ruby[0]


def test_build_ass_ruby_silent_symbol_word(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    # 中黒入りのカタカナ語(バリッシュ・コノル)は読みが表記から自明なのでルビなし。
    # 以前は「・」が漢字扱いになり、単語全体にひらがな読みが振られていた
    project.parody.lines[0].words = [
        ParodyWord(surface="バリッシュ・コノル", kana="バリッシュコノル", original="",
                   original_surface="", originalkana="", note_ids=[0]),
    ]
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    assert _ruby_events(ass) == []


def test_build_ass_ruby_silent_symbol_with_kanji(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    # 記号+漢字の混在: ルビは漢字「参」の読み「さん」だけ
    project.parody.lines[0].words = [
        ParodyWord(surface="アテル＝参", kana="アテルサン", original="",
                   original_surface="", originalkana="", note_ids=[0]),
    ]
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    ruby = _ruby_events(ass)
    assert len(ruby) == 1
    assert "さん" in ruby[0] and "あてる" not in ruby[0]


def test_build_ass_ruby_partial_positions(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    # 「夜ノ街」は漢字ランごとに2つのルビが出て、表記順にx位置が並ぶ
    project.parody.lines[0].words = [
        ParodyWord(surface="夜ノ街", kana="ヨルノマチ", original="", original_surface="",
                   originalkana="", note_ids=[0]),
    ]
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    ruby = _ruby_events(ass)
    assert len(ruby) == 2
    assert "よる" in ruby[0] and "まち" in ruby[1]
    xs = [_pos_x(ln) for ln in ruby]
    assert xs[0] < xs[1]


def test_build_ass_ruby_fallback_whole_word(tmp_path: Path):
    from soramimic_video.layout import load_layout

    project = _project(tmp_path)
    # 読みをランに割り付けられない語は、従来どおり単語全体に読み全体を置く
    project.parody.lines[0].words = [
        ParodyWord(surface="静カ", kana="シズケサ", original="", original_surface="",
                   originalkana="", note_ids=[0]),
    ]
    ass = build_ass(project, 1280, 720, "Font", load_layout(_ruby_layout(tmp_path)))
    ruby = _ruby_events(ass)
    assert len(ruby) == 1
    assert "しずけさ" in ruby[0]


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


def test_image_cues_fallback_for_missing_image(tmp_path: Path):
    # 行はあるが画像が無い/取得できない既知語も、未知語と同じfallbackの文字フレームで出る
    import json

    from soramimic_video.layout import load_layout

    fb = tmp_path / "fb.json"
    fb.write_text(json.dumps({
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
        "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2],
                      "size": 0.1}],
    }), encoding="utf-8")

    # image列が空
    project = _project(tmp_path)
    project.parody.lines[0].words[0].wordlist_row = {"image": ""}
    cues, _ = build_image_cues(project, tmp_path / "v1", 320, 180, layout=load_layout(str(fb)))
    assert len(cues) == 1 and cues[0].frame.exists()

    # image列はあるがローカルパスが存在しない(ダウンロード失敗と同じ経路)
    project2 = _project(tmp_path)
    project2.parody.lines[0].words[0].wordlist_row = {"image": str(tmp_path / "nai.png")}
    cues2, _ = build_image_cues(project2, tmp_path / "v2", 320, 180, layout=load_layout(str(fb)))
    assert len(cues2) == 1 and cues2[0].frame.exists()


def test_app_credit_text_appends_synth_credit():
    from soramimic_video.layout import APP_CREDIT
    from soramimic_video.video import app_credit_text

    assert app_credit_text() == APP_CREDIT
    assert app_credit_text("  ") == APP_CREDIT
    assert app_credit_text("VOICEVOX:四国めたん") == f"{APP_CREDIT} / VOICEVOX:四国めたん"


def test_idle_frame_data_carries_app_credit(tmp_path: Path):
    from soramimic_video.layout import APP_CREDIT
    from soramimic_video.video import idle_frame_data

    project = _project(tmp_path)
    assert idle_frame_data(project)["app_credit"] == APP_CREDIT
    data = idle_frame_data(project, f"{APP_CREDIT} / VOICEVOX:四国めたん")
    assert data["app_credit"].endswith("VOICEVOX:四国めたん")


def test_image_cues_bake_app_credit(tmp_path: Path):
    # 単語フレームにも署名が入る(文言が変わればフレームも変わる)
    import json

    from soramimic_video.layout import load_layout

    lay = tmp_path / "l.json"
    lay.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2],
                      "size": 0.1}],
    }), encoding="utf-8")
    layout = load_layout(str(lay))

    project = _project(tmp_path)
    plain, _ = build_image_cues(project, tmp_path / "v1", 320, 180, layout=layout)
    project2 = _project(tmp_path)
    credited, _ = build_image_cues(
        project2, tmp_path / "v2", 320, 180, layout=layout,
        app_credit="lyrics by Soramimic / VOICEVOX:四国めたん",
    )
    assert plain and credited
    assert plain[0].frame.name != credited[0].frame.name


def test_effective_fallback_keeps_normal_texts():
    # 通常側にまだ出せるテキストがある単語はfallbackへ落とさない
    from soramimic_video.layout import parse_layout
    from soramimic_video.video import effective_fallback

    layout = parse_layout({
        "elements": [
            {"type": "image", "box": [0, 0, 1, 0.7]},
            {"type": "text", "text": "{original}", "box": [0.1, 0.8, 0.8, 0.1]},
        ],
        "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2]}],
    }, "<test>")
    data = {"surface": "静", "original": "沈"}
    assert effective_fallback(layout, data, False, has_image=False) is False
    assert effective_fallback(layout, {"surface": "静", "original": ""}, False,
                              has_image=False) is True
    assert effective_fallback(layout, data, False, has_image=True) is False
    assert effective_fallback(layout, data, True, has_image=False) is True


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


def _two_word_project(second_start: float = 4.5) -> Project:
    # 2単語を大きく離して配置(単語0: 0.5〜0.75s / 単語1: 4.5〜4.75s、隙間3.75s)
    song = SongInfo(midi_path="mysong.mid", ticks_per_beat=480)
    notes = [
        Note(0, 60, 0, 240, 0.5, 0.75, 0, "静", "シズ", ""),
        Note(1, 62, 0, 240, second_start, second_start + 0.25, 1, "山", "ヤマ", ""),
    ]
    lines = [Line(0, "", "", [0]), Line(1, "", "", [1])]
    parody = Parody(wordlist="test", lines=[
        ParodyLine(0, [ParodyWord("静", "シズ", "", "", "", [0])]),
        ParodyLine(1, [ParodyWord("山", "ヤマ", "", "", "", [1])]),
    ])
    return Project(song=song, notes=notes, lines=lines, parody=parody)


def test_hold_next_extends_show_end(tmp_path: Path):
    project = _two_word_project()
    cap, hold = _text_layouts(tmp_path)

    # 既定: 3秒(HOLD_MAX_SEC)で上限。1単語目は3.75s(0.75+3.0)で切れる
    cues, _ = build_image_cues(project, tmp_path / "cap", 320, 180, layout=cap)
    assert len(cues) == 2
    assert abs(cues[0].end - 3.75) < 0.01
    assert abs(cues[1].end - 7.75) < 0.01  # 最終単語は end+3.0

    # hold=next: 1単語目は次の歌唱(4.5s)まで持続。最終単語は end 止め(後奏はidle/黒)
    cues_h, _ = build_image_cues(project, tmp_path / "hold", 320, 180, layout=hold)
    assert abs(cues_h[0].end - 4.5) < 0.01
    assert abs(cues_h[1].end - 4.75) < 0.01


_TEXT_LAYOUT = {
    "elements": [
        {"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.3], "size": 0.1}
    ]
}


def _text_layouts(tmp_path: Path):
    """同じ表示内容で hold なし/あり(hold=next)の2レイアウト。"""
    from soramimic_video.layout import load_layout

    cap = load_layout(str(_write(tmp_path / "cap.json", _TEXT_LAYOUT)))
    hold = load_layout(str(_write(tmp_path / "hold.json", {**_TEXT_LAYOUT, "hold": "next"})))
    return cap, hold


def _dialogue_spans(ass: str, style: str = "Parody") -> list[tuple[float, float]]:
    """ASSのDialogue行から、指定スタイルの表示区間(秒)を順に取り出す。"""

    def _sec(t: str) -> float:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    spans = []
    for ln in ass.splitlines():
        if not ln.startswith("Dialogue:"):
            continue
        parts = ln.split(",")
        if parts[3] == style:
            spans.append((_sec(parts[1]), _sec(parts[2])))
    return spans


def test_subtitle_end_matches_image_hold(tmp_path: Path):
    # 間奏の手前(次の単語まで3.75s)で、字幕が画像より先に消えない
    from soramimic_video.video import HOLD_MAX_SEC, SUB_PAD_SEC

    project = _two_word_project()
    cap, hold = _text_layouts(tmp_path)

    # 既定: 画像と同じく 行末+HOLD_MAX_SEC まで残る(従来は 行末+0.15s で消えていた)
    spans = _dialogue_spans(build_ass(project, 1280, 720, "Font", cap))
    assert abs(spans[0][1] - (0.75 + HOLD_MAX_SEC)) < 0.01
    assert abs(spans[1][1] - (4.75 + HOLD_MAX_SEC)) < 0.01  # 最終行も画像に揃える
    cues, _ = build_image_cues(project, tmp_path / "cap", 320, 180, layout=cap)
    assert abs(spans[0][1] - cues[0].end) < 0.01

    # hold=next: 次の単語(4.5s)まで。ただし次の行の字幕開始(4.5-0.15s)で交代する
    spans_h = _dialogue_spans(build_ass(project, 1280, 720, "Font", hold))
    assert abs(spans_h[0][1] - (4.5 - SUB_PAD_SEC)) < 0.01
    assert abs(spans_h[1][0] - (4.5 - SUB_PAD_SEC)) < 0.01
    # hold=next の最終単語は余韻なし(後奏はidle/黒)なので従来のパディングのまま
    assert abs(spans_h[1][1] - (4.75 + SUB_PAD_SEC)) < 0.01


def test_subtitle_end_kept_when_next_line_is_close(tmp_path: Path):
    # 次の行がすぐ来る通常の並びでは、従来どおり次の行の開始で詰める
    from soramimic_video.video import SUB_PAD_SEC

    project = _two_word_project(second_start=1.0)
    for layout in _text_layouts(tmp_path):
        spans = _dialogue_spans(build_ass(project, 1280, 720, "Font", layout))
        assert abs(spans[0][1] - (1.0 - SUB_PAD_SEC)) < 0.01
        assert abs(spans[1][0] - (1.0 - SUB_PAD_SEC)) < 0.01


def test_subtitle_end_keeps_padding_without_word_frames(tmp_path: Path):
    # このレイアウトで表示できる単語が無い行(画像なし等)は従来のパディング挙動
    from soramimic_video.layout import load_layout
    from soramimic_video.video import SUB_PAD_SEC

    project = _two_word_project()
    for pl in project.parody.lines:
        pl.words[0].wordlist_row = {"death": ""}
    layout = load_layout(str(_write(tmp_path / "req2.json", {
        "elements": [
            {"type": "text", "text": "没年 {death}", "box": [0.1, 0.3, 0.8, 0.2],
             "require": "death"},
        ],
    })))
    cues, _ = build_image_cues(project, tmp_path / "none", 320, 180, layout=layout)
    assert cues == []
    spans = _dialogue_spans(build_ass(project, 1280, 720, "Font", layout))
    assert abs(spans[0][1] - (0.75 + SUB_PAD_SEC)) < 0.01


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


# ---- SVGのラスタライズ(生成カード画像はSVG配布でPillowが開けない) ----

# ポケモン/選手/YouTuberカードと同じ書き方(viewBoxつき・日本語のtext)の最小SVG
SVG_FIXTURE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200" width="320" height="200">'
    '<rect x="0" y="0" width="320" height="200" fill="#4fbe5c"/>'
    '<text x="20" y="120" font-family="\'Hiragino Sans\',\'Noto Sans JP\',sans-serif"'
    ' font-size="40" fill="#111">フシギダネ</text>'
    "</svg>"
).encode()
# XML宣言が前置されるSVG(先頭が `<svg` ではない)
SVG_FIXTURE_XML_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n' + SVG_FIXTURE


def test_looks_like_svg_detects_data_not_content_type():
    from soramimic_video.video import looks_like_svg

    # GitHubのReleaseは .svg を application/octet-stream で返すのでデータで判定する
    assert looks_like_svg(SVG_FIXTURE)
    assert looks_like_svg(SVG_FIXTURE_XML_DECL)
    assert looks_like_svg(b"  \n" + SVG_FIXTURE)  # 先頭の空白は無視
    assert not looks_like_svg(_png_bytes())
    assert not looks_like_svg(b"\xff\xd8\xff\xe0jpeg")
    assert not looks_like_svg(b'<?xml version="1.0"?><rss><item/></rss>')  # SVGでないXML


def test_svg_to_png_keeps_viewbox_ratio():
    pytest.importorskip("cairosvg")
    from PIL import Image

    from soramimic_video.video import svg_to_png

    png = svg_to_png(SVG_FIXTURE, width=640)
    assert png is not None and png.startswith(b"\x89PNG")
    import io

    with Image.open(io.BytesIO(png)) as img:
        assert img.size == (640, 400)  # viewBox 320x200 の比を保つ


def test_svg_to_png_returns_none_for_broken_svg():
    pytest.importorskip("cairosvg")
    from soramimic_video.video import svg_to_png

    assert svg_to_png(b"<svg><unclosed>") is None


def test_download_image_rasterizes_svg(tmp_path: Path):
    pytest.importorskip("cairosvg")
    from PIL import Image

    src = tmp_path / "card.svg"
    src.write_bytes(SVG_FIXTURE_XML_DECL)
    cache = tmp_path / "cache"
    got = download_image(str(src), cache)
    assert got is not None and got.suffix == ".png"
    with Image.open(got) as img:  # Pillowで開ける = フレーム合成に使える
        assert img.width == 1280
    # 2回目はキャッシュ済みのPNGをそのまま返す(変換し直さない)
    assert download_image(str(src), cache) == got
    assert list(cache.glob("*")) == [got]


def test_cached_image_converts_legacy_svg_cache(tmp_path: Path):
    """SVGのまま残っている既存キャッシュは、読み込み時にPNGへ移行する。"""
    pytest.importorskip("cairosvg")
    from PIL import Image

    from soramimic_video.video import cached_image

    url = "https://example.com/card.svg"
    cache = tmp_path / "cache"
    cache.mkdir()
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    legacy = cache / f"{name}.img"  # 旧バージョンはSVGを .img で置いていた
    legacy.write_bytes(SVG_FIXTURE)

    got = cached_image(url, cache)
    assert got is not None and got.name == f"{name}.png"
    with Image.open(got) as img:
        assert img.width == 1280
    assert not legacy.exists()  # 開けないファイルは残さない
    assert cached_image(url, cache) == got


def test_svg_stays_cached_when_conversion_fails(tmp_path: Path, monkeypatch):
    """変換できなくても再ダウンロードは繰り返さない(SVGはキャッシュに残す)。"""
    import soramimic_video.video as video_mod

    monkeypatch.setattr(video_mod, "svg_to_png", lambda data, width=1280: None)
    src = tmp_path / "card.svg"
    src.write_bytes(SVG_FIXTURE)
    cache = tmp_path / "cache"
    assert download_image(str(src), cache) is None
    kept = list(cache.glob("*"))
    assert [p.suffix for p in kept] == [".svg"]
    assert download_image(str(src), cache) is None
    assert list(cache.glob("*")) == kept

    # cairosvgが使えるようになれば、次の読み込みでPNGに移行する
    pytest.importorskip("cairosvg")
    monkeypatch.undo()
    got = download_image(str(src), cache)
    assert got is not None and got.suffix == ".png"


def test_image_cues_render_svg_word_image(tmp_path: Path, monkeypatch):
    """SVGの単語画像でもフレームが作られる(従来は画像なしに落ちていた)。"""
    pytest.importorskip("cairosvg")
    card = tmp_path / "card.svg"
    card.write_bytes(SVG_FIXTURE)
    project = _project(tmp_path)
    project.parody.lines[0].words[0].wordlist_row = {"image": str(card), "image_page": ""}
    cues, _credits = build_image_cues(project, tmp_path / "work", 640, 360)
    assert len(cues) == 1
    from PIL import Image

    with Image.open(cues[0].frame) as frame:
        assert frame.size == (640, 360)


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
# ---- リトライ / プリフェッチ / prewarm ----


class _FakeResp:
    """requests.Response の代わり(http_get_with_retry / download_image のモック用)。"""

    def __init__(self, content: bytes = b"", status_code: int = 200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        import json

        return json.loads(self.content.decode())


def _png_bytes(color: str = "red") -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def test_http_get_retries_on_429(monkeypatch):
    # 429を1回返してから200を返すと、再試行して成功レスポンスを返す
    import soramimic_video.image_credit as ic

    monkeypatch.setattr(ic.time, "sleep", lambda s: None)  # バックオフ待ちを飛ばす
    seq = [
        _FakeResp(status_code=429, headers={"Retry-After": "0"}),
        _FakeResp(content=b"ok", status_code=200),
    ]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=30):
        calls.append(url)
        return seq.pop(0)

    monkeypatch.setattr(ic.requests, "get", fake_get)
    resp = ic.http_get_with_retry("https://example.com/x")
    assert resp.status_code == 200 and resp.content == b"ok"
    assert len(calls) == 2  # 429で1回再試行している


def test_http_get_raises_after_retries(monkeypatch):
    # 429が続けば最終的にHTTPErrorを送出する(呼び出し側はNone扱いにできる)
    import requests

    import soramimic_video.image_credit as ic

    monkeypatch.setattr(ic.time, "sleep", lambda s: None)
    calls = []

    def fake_get(url, headers=None, params=None, timeout=30):
        calls.append(url)
        return _FakeResp(status_code=503)

    monkeypatch.setattr(ic.requests, "get", fake_get)
    with pytest.raises(requests.HTTPError):
        ic.http_get_with_retry("https://example.com/x", max_attempts=3)
    assert len(calls) == 3  # 最大試行回数まで試す


def _same_url_project(tmp_path: Path, url: str) -> Project:
    project = _project(tmp_path)
    row = {"image": url, "image_page": ""}  # 非Commons=クレジット取得は即Noneでネット無し
    project.parody.lines[0].words = [
        ParodyWord(surface="静", kana="シズ", original="", original_surface="", originalkana="",
                   note_ids=[0], wordlist_row=dict(row)),
        ParodyWord(surface="山", kana="ヤマ", original="", original_surface="", originalkana="",
                   note_ids=[1], wordlist_row=dict(row)),
    ]
    return project


def test_prefetch_downloads_each_url_once(tmp_path: Path, monkeypatch):
    # 同一URLを持つ2単語でも、プリフェッチ+逐次ループを通して画像取得は1回だけ
    import soramimic_video.image_credit as ic

    url = "https://example.com/shizu.jpg"
    png = _png_bytes()
    calls = []

    def fake_get(url_, headers=None, params=None, timeout=30):
        calls.append(url_)
        return _FakeResp(content=png)

    monkeypatch.setattr(ic.requests, "get", fake_get)
    project = _same_url_project(tmp_path, url)
    cues, _ = build_image_cues(project, tmp_path / "video", 320, 180)
    assert len(cues) == 2  # 両単語ともフレームが出る
    assert calls == [url]  # ダウンロードは重複せず1回だけ


def test_prewarm_skips_cached(tmp_path: Path, monkeypatch):
    # キャッシュ済みURLはダウンロードせずスキップ、未キャッシュだけ取得する
    import soramimic_video.image_credit as ic
    from soramimic_video.prewarm import prewarm_images

    cache = tmp_path / "cache"
    cache.mkdir()
    cached_url = "https://example.com/a.jpg"
    new_url = "https://example.com/b.jpg"
    name = hashlib.sha1(cached_url.encode()).hexdigest()[:16]
    (cache / f"{name}.jpg").write_bytes(_png_bytes())  # 事前にキャッシュ配置

    calls = []

    def fake_get(url_, headers=None, params=None, timeout=30):
        calls.append(url_)
        return _FakeResp(content=_png_bytes("blue"))

    monkeypatch.setattr(ic.requests, "get", fake_get)
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "image,image_page,image_credit\n" f"{cached_url},,\n{new_url},,\n",
        encoding="utf-8",
    )
    summary = prewarm_images([csv_path], cache, delay=0)
    assert summary["skipped"] == 1 and summary["fetched"] == 1 and summary["failed"] == 0
    assert calls == [new_url]  # キャッシュ済みURLはダウンロードされない


# ---- サムネ(前奏区間の表示) ----


def test_thumbnail_show_end_uses_intro(tmp_path: Path):
    from soramimic_video.video import SUB_PAD_SEC, thumbnail_show_end

    project = _two_word_project()  # 最初の歌唱ノートは0.5s(前奏が短い曲)
    assert thumbnail_show_end(project) == 0.0  # 一瞬しか出せないので出さない

    project.notes[0].start_sec = 8.0  # 前奏8秒の曲は字幕が出る直前まで
    project.notes[1].start_sec = 9.0
    assert abs(thumbnail_show_end(project) - (8.0 - SUB_PAD_SEC)) < 0.01


def test_thumbnail_does_not_overlap_subtitles(tmp_path: Path):
    """サムネの表示区間が最初の字幕の開始より前に終わること(重なり防止)。"""
    from soramimic_video.video import SUB_PAD_SEC, thumbnail_show_end

    project = _two_word_project()
    project.notes[0].start_sec = 5.0
    project.notes[1].start_sec = 6.0
    first_subtitle_start = 5.0 - SUB_PAD_SEC
    assert thumbnail_show_end(project) <= first_subtitle_start


def test_prepend_thumbnail_cue_shifts_overlapping_cues(tmp_path: Path):
    from soramimic_video.video import prepend_thumbnail_cue

    thumb = tmp_path / "thumbnail.png"
    frame = tmp_path / "frame.png"
    cues = [
        ImageCue(start=0.5, end=1.0, frame=frame),  # サムネに完全に隠れる
        ImageCue(start=2.0, end=5.0, frame=frame),  # 途中から出る
        ImageCue(start=6.0, end=8.0, frame=frame),  # そのまま
    ]
    out = prepend_thumbnail_cue(cues, thumb, 3.0)
    assert [(c.start, c.end) for c in out] == [(0.0, 3.0), (3.0, 5.0), (6.0, 8.0)]
    assert out[0].frame == thumb
    # 区間が重ならない=スライドショーの連結で以降の映像がずれない
    assert all(a.end <= b.start for a, b in zip(out, out[1:], strict=False))


def test_prepend_thumbnail_cue_without_intro_overlap(tmp_path: Path):
    from soramimic_video.video import prepend_thumbnail_cue

    thumb = tmp_path / "thumbnail.png"
    cues = [ImageCue(start=10.0, end=12.0, frame=tmp_path / "frame.png")]
    out = prepend_thumbnail_cue(cues, thumb, 9.0)
    assert [(c.start, c.end) for c in out] == [(0.0, 9.0), (10.0, 12.0)]
    # end<=0(サムネを出さない)ならキューは変えない
    assert prepend_thumbnail_cue(cues, thumb, 0.0) == cues


# ---- 歌唱なし区間(前奏・間奏・後奏) ----


def _cue(start: float, end: float, name: str = "f.png") -> ImageCue:
    return ImageCue(start=start, end=end, frame=Path(name))


def test_idle_sections_classify_gaps():
    from soramimic_video.video import idle_sections

    cues = [_cue(2.0, 5.0), _cue(20.0, 25.0)]
    sections = [(s.kind, s.start, s.end) for s in idle_sections(cues, 40.0)]
    assert sections == [
        ("intro", 0.0, 2.0),
        ("interlude", 5.0, 20.0),
        ("outro", 25.0, 40.0),
    ]


def test_idle_sections_edge_cases():
    from soramimic_video.video import idle_sections

    # 隙間なく歌い切る曲は区間なし
    assert idle_sections([_cue(0.0, 30.0)], 30.0) == []
    # 表示できる単語が1つも無い曲は全編を前奏とみなす
    only = idle_sections([], 12.0)
    assert [(s.kind, s.start, s.end) for s in only] == [("intro", 0.0, 12.0)]
    assert idle_sections([], 0.0) == []


def test_endroll_pages_splits_at_threshold():
    from soramimic_video.video import ENDROLL_WORDS_PER_PAGE, endroll_pages

    per = ENDROLL_WORDS_PER_PAGE
    words = [f"w{i}" for i in range(per)]
    assert len(endroll_pages(words)) == 1
    assert len(endroll_pages(words + ["extra"])) == 2
    # 2枚を超える語数でも枚数は増やさず、1枚あたりを詰める
    many = [f"w{i}" for i in range(per * 5)]
    pages = endroll_pages(many)
    assert len(pages) == 2
    assert sum(len(p) for p in pages) == len(many)
    assert endroll_pages([]) == []


def test_used_words_dedupes_in_order(tmp_path: Path):
    from soramimic_video.video import used_words

    project = _project(tmp_path)
    word = project.parody.lines[0].words[0]
    project.parody.lines[0].words.append(word)  # 同じ単語の2回目
    project.parody.lines[0].words.append(
        ParodyWord(surface="謎", kana="ナゾ", original="", original_surface="ナゾ",
                   originalkana="ナゾ", note_ids=[2], note_kana=["ム"])
    )
    # original 列を出し、無い単語(手入力)は替え歌表記で代用する
    assert used_words(project) == ["静", "謎"]


def test_image_credits_text_dedupes():
    from soramimic_video.video import image_credits_text

    assert image_credits_text([]) == ""
    got = image_credits_text(
        [{"credit": "A / CC BY"}, {"credit": "A / CC BY"}, {"credit": "B"}, {"credit": ""}]
    )
    assert got == "A / CC BY / B"


def test_section_frame_data_template_values(tmp_path: Path):
    from soramimic_video.video import section_frame_data

    inter = section_frame_data(_project(tmp_path), section="interlude", duration=12.4)
    assert inter["interlude_sec"] == "12"
    assert inter["wordlist"] == "test"
    # 間奏以外では秒数を出さない(テンプレートに書いても空になる)
    assert section_frame_data(_project(tmp_path), section="outro", duration=12.4)[
        "interlude_sec"
    ] == ""
    end = section_frame_data(
        _project(tmp_path), section="outro", duration=20.0,
        words=["静", "謎"], image_credits="A / CC BY", page=2, pages=2,
    )
    assert end["used_words"] == "静、謎"
    assert end["image_credits"] == "A / CC BY"
    assert end["page_label"] == "(2/2)"
    # 1枚のときはページ表示を出さない
    assert section_frame_data(_project(tmp_path), section="outro")["page_label"] == ""


def _section_layout(tmp_path: Path):
    import json

    from soramimic_video.layout import load_layout

    p = tmp_path / "sec.json"
    p.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
        "interlude": [
            {"type": "text", "text": "間奏({interlude_sec}秒)", "box": [0.1, 0.4, 0.8, 0.2]},
        ],
        "outro": [{"type": "text", "text": "{used_words}", "box": [0.1, 0.2, 0.8, 0.5]}],
    }), encoding="utf-8")
    return load_layout(str(p))


def test_build_section_cues_respects_thresholds(tmp_path: Path):
    from soramimic_video.video import INTERLUDE_MIN_SEC, OUTRO_MIN_SEC, build_section_cues

    project = _project(tmp_path)
    layout = _section_layout(tmp_path)
    work = tmp_path / "video"
    # 長い間奏 + 長い後奏 → どちらも出る
    cues = [_cue(0.0, 5.0), _cue(5.0 + INTERLUDE_MIN_SEC + 1, 30.0)]
    total = 30.0 + OUTRO_MIN_SEC + 1
    got = build_section_cues(project, cues, total, layout, work, 320, 180)
    assert len(got) == 2
    assert got[0].start == 5.0 and got[0].end == 5.0 + INTERLUDE_MIN_SEC + 1
    assert got[1].start == 30.0 and got[1].end == total
    assert all(c.frame.exists() for c in got)

    # 短い間奏・短い後奏は出さない(一瞬だけ出て消えるのを避ける)
    short = [_cue(0.0, 5.0), _cue(5.0 + INTERLUDE_MIN_SEC - 0.5, 30.0)]
    assert build_section_cues(
        project, short, 30.0 + OUTRO_MIN_SEC - 0.5, layout, work, 320, 180
    ) == []


def test_build_section_cues_paginates_endroll(tmp_path: Path):
    from soramimic_video.video import ENDROLL_WORDS_PER_PAGE, build_section_cues

    project = _project(tmp_path)
    base = project.parody.lines[0].words[0]
    for i in range(ENDROLL_WORDS_PER_PAGE):
        project.parody.lines[0].words.append(
            ParodyWord(surface=f"語{i}", kana="ゴ", original=f"語{i}",
                       original_surface="ゴ", originalkana="ゴ",
                       note_ids=base.note_ids, note_kana=base.note_kana)
        )
    layout = _section_layout(tmp_path)
    got = build_section_cues(project, [_cue(0.0, 10.0)], 30.0, layout, tmp_path / "v",
                             320, 180)
    assert len(got) == 2  # 2枚に分かれ、後奏を等分する
    assert got[0].start == 10.0 and abs(got[0].end - 20.0) < 0.01
    assert abs(got[1].start - 20.0) < 0.01 and got[1].end == 30.0
    assert got[0].frame != got[1].frame


def test_build_section_cues_skips_layouts_without_sections(tmp_path: Path):
    import json

    from soramimic_video.layout import load_layout
    from soramimic_video.video import build_section_cues

    p = tmp_path / "plain.json"
    p.write_text(json.dumps({
        "interlude": [], "outro": [],
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
    }), encoding="utf-8")
    got = build_section_cues(
        _project(tmp_path), [_cue(0.0, 5.0), _cue(20.0, 30.0)], 60.0,
        load_layout(str(p)), tmp_path / "v", 320, 180,
    )
    assert got == []


def test_sung_gap_sec_measures_from_notes(tmp_path: Path):
    from soramimic_video.video import IdleSection, sung_gap_sec

    project = _project(tmp_path)
    # ノートは 0.5〜1.5s(480〜1200tick @120bpm)。余韻ぶん後ろから始まる区間でも
    # 「歌が止まっている長さ」は最後のノート終端から測る
    ends = [n.end_sec for n in project.notes]
    last = max(ends)
    assert sung_gap_sec(project, IdleSection("interlude", last + 3.0, last + 9.0)) == 6.0
    # 前後どちらかに歌唱ノートが無ければ区間そのものの長さ
    tail = IdleSection("outro", last + 1.0, last + 5.0)
    assert sung_gap_sec(project, tail) == tail.duration


def test_build_section_cues_reports_sung_gap(tmp_path: Path):
    from soramimic_video.video import build_section_cues

    project = _project(tmp_path)
    layout = _section_layout(tmp_path)
    last = max(n.end_sec for n in project.notes)
    # 単語フレームが last+3 まで残り、次の歌唱が last+9 に始まる曲を模した配置
    project.notes.append(
        Note(id=len(project.notes), midi_note=60, start_tick=0, end_tick=0,
             start_sec=last + 9.0, end_sec=last + 12.0, line=0,
             surface="ラ", kana="ラ", raw="ラ")
    )
    cues = [_cue(0.0, last + 3.0), _cue(last + 9.0, last + 12.0)]
    work = tmp_path / "v"
    got = build_section_cues(project, cues, last + 12.0, layout, work, 320, 180)
    assert len(got) == 1
    assert got[0].start == last + 3.0 and got[0].end == last + 9.0  # 表示は6秒
    # なのに「間奏(9秒)」と出る(区間の長さではなく歌の途切れを測っている)
    from soramimic_video.layout import render_section_frame
    from soramimic_video.video import section_frame_data

    expected = render_section_frame(
        layout,
        section_frame_data(project, section="interlude", duration=9.0),
        320, 180, work / "frames", "interlude",
    )
    assert got[0].frame == expected
