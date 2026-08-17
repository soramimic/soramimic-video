"""サムネ画像(thumbnail.py)のテスト。

変換エンジン(run_convert)と画像取得はモックし、ネットワーク無しで
レイアウトの文言・画像なしフォールバック・失敗時の挙動を確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from soramimic_video import thumbnail as thumb_mod
from soramimic_video.project import Note, Parody, ParodyLine, Project, SongInfo
from soramimic_video.thumbnail import (
    SIGNATURE,
    STYLE_SIDE,
    THUMBNAIL_FILENAME,
    compose_background,
    generate_thumbnail,
    pick_headline_words,
    render_thumbnail,
    song_title,
    thumbnail_data,
    thumbnail_layout,
)


def _wordlist_csv(tmp_path: Path, image: str = "", image2: str = "") -> Path:
    """テスト用の単語リストCSV(submoduleの実データに依存しない)。"""
    path = tmp_path / "mylist.csv"
    path.write_text(
        f"id,surface,original,image\n1,米原,米原駅,{image}\n2,大津,大津駅,{image2}\n",
        encoding="utf-8",
    )
    return path


def _project(wordlist: Path | str, midi_path: str = "mysong.mid") -> Project:
    song = SongInfo(midi_path=midi_path, ticks_per_beat=480)
    notes = [Note(0, 60, 0, 240, 0.5, 0.75, 0, "静", "シズ", "")]
    parody = Parody(
        wordlist=str(wordlist), params={"VOWEL_RATIO": 0.8}, lines=[ParodyLine(0)]
    )
    return Project(song=song, notes=notes, lines=[], parody=parody)


def _fake_convert(*surfaces: str):
    """run_convert の戻り値(1フレーズぶん)を作るモック。"""

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None):
        words = [{"surface": s, "id": str(i + 1)} for i, s in enumerate(surfaces)]
        return {
            "lines": [{"units": [], "words": words}],
            "tokensList": [],
            "phrases": phrases,
        }

    return fake


# ---- 見出しに使う単語の選び方 ----


def test_pick_headline_uses_all_words_of_the_title():
    # 変換結果は曲名を最後まで覆う単語列なので、途中で切らず全部並べる
    # (「春が来た → ダブラン」だけだと曲名の一部しか言い換えていない見出しになる)
    words = [{"surface": "ダブラン"}, {"surface": "キタ"}]
    assert [w["surface"] for w in pick_headline_words(words)] == ["ダブラン", "キタ"]

    words = [{"surface": "ダイオージャ"}]
    assert [w["surface"] for w in pick_headline_words(words)] == ["ダイオージャ"]


def test_pick_headline_caps_very_long_titles():
    from soramimic_video.thumbnail import HEADLINE_MAX_WORDS

    words = [{"surface": f"語{i}"} for i in range(HEADLINE_MAX_WORDS + 3)]
    assert len(pick_headline_words(words)) == HEADLINE_MAX_WORDS


def test_pick_headline_handles_missing_and_empty():
    assert pick_headline_words([]) == []
    assert pick_headline_words([{"surface": ""}]) == []
    assert len(pick_headline_words([{"surface": "モノ"}])) == 1


def test_pick_headline_skips_all_filler():
    # filler(万能候補)は「元歌詞のかなのまま」なので、それだけの結果は
    # 言い換えになっていない。見出しを出さない(エンジンにfillerが入る前と同じ)
    assert pick_headline_words([{"surface": "ハルガ", "filler": True},
                                {"surface": "キタ", "filler": True}]) == []
    # 実単語が1つでもあれば、filler ごと並べて曲名を最後まで覆う
    got = pick_headline_words([{"surface": "ダブラン"}, {"surface": "キタ", "filler": True}])
    assert [w["surface"] for w in got] == ["ダブラン", "キタ"]


# ---- レイアウト・文言 ----


def test_layout_texts_with_word():
    layout = thumbnail_layout(has_word=True, has_image=False)
    data = thumbnail_data("夜に駆ける", "駅名", word="米原")
    texts = [t for t in layout.render_texts(data) if t]
    assert "【米原】" in texts
    assert "夜に駆ける を 駅名 で歌ってみた" in texts
    assert SIGNATURE in texts


def test_caption_breaks_at_the_meaningful_spot_when_long():
    """長い曲名×長いリスト名は「<曲名> を」/「<リスト名> で歌ってみた」で折る。"""
    data = thumbnail_data("残酷な天使のテーゼ", "架空の日常アニメキャラ", word="米原")
    assert data["caption"] == "残酷な天使のテーゼ を\n架空の日常アニメキャラ で歌ってみた"
    # どの行も1行に入る長さになっている(自動の折り返しでさらに縮まない)
    assert all(len(line) <= thumb_mod.CAPTION_ONE_LINE_CHARS
               for line in data["caption"].split("\n"))
    # 1行で入る短さなら折らない(不要な2行組みで文字を小さくしない)
    assert "\n" not in thumbnail_data("夜に駆ける", "駅名", word="米原")["caption"]


def test_text_only_thumbnail_uses_bigger_type():
    """絵が無いサムネは文字が主役なので、見出しもキャプションも大きくする。"""
    with_image = thumb_mod.thumbnail_layout_spec(True, True)["elements"]
    text_only = thumb_mod.thumbnail_layout_spec(True, False)["elements"]
    for a, b in zip(with_image[:2], text_only[:2], strict=True):
        assert b["size"] > a["size"]


def test_headline_joins_two_words():
    data = thumbnail_data("夜に駆ける", "駅名", word=["モノカ", "加藤"])
    assert data["headline"] == "【モノカ 加藤】"


def test_image_credit_dedupes_and_joins():
    data = thumbnail_data("曲", "駅名", word="米原", image_credit=["A (CC BY)", "A (CC BY)"])
    assert data["image_credit"] == "A (CC BY)"
    data2 = thumbnail_data("曲", "駅名", word="米原", image_credit=["A", "", "B"])
    assert data2["image_credit"] == "A / B"


def test_layout_texts_fallback_without_word():
    layout = thumbnail_layout(has_word=False, has_image=False)
    data = thumbnail_data("夜に駆ける", "駅名")
    texts = [t for t in layout.render_texts(data) if t]
    # 言い換えなし: キャプションと署名だけ(【】の見出しは出ない)
    assert texts == ["夜に駆ける を 駅名 で歌ってみた", SIGNATURE]


def test_fullbleed_texts_are_outlined_without_band():
    """どの可読性デザインでも、全面スタイルの文字は縁取りで読ませる(黒帯は敷かない)。"""
    from soramimic_video.layout import TextElement

    for design in thumb_mod.TEXT_DESIGNS:
        for has_word in (True, False):
            for has_image in (True, False):
                layout = thumbnail_layout(
                    has_word=has_word, has_image=has_image, design=design
                )
                texts = [e for e in layout.elements if isinstance(e, TextElement)]
                assert texts, "文字要素が無い"
                for el in texts:
                    assert el.background is None, (design, el.template)  # 帯は使わない
                    assert el.strokes, (design, el.template)
                    widths = [w for w, _ in el.strokes]
                    assert widths == sorted(widths, reverse=True)  # 太い順


def test_double_outline_keeps_two_rings_and_a_shadow():
    """現行(比較の基準)は明色・暗色の2重の環+ぼかし影のまま。"""
    from soramimic_video.layout import TextElement

    layout = thumbnail_layout(
        has_word=True, has_image=True, design=thumb_mod.DESIGN_DOUBLE_OUTLINE
    )
    for el in layout.elements:
        if isinstance(el, TextElement):
            assert len(el.strokes) == 2, el.template
            assert el.shadow > 0, el.template


def test_thin_designs_have_thinner_strokes_than_the_current_one():
    """案1〜3の狙いは「文字の形を潰さない」こと。縁は現行より細い。"""
    current = thumb_mod.outline_style(0.19, thumb_mod.DESIGN_DOUBLE_OUTLINE)
    widest = max(s["width"] for s in current["strokes"])
    for design in (
        thumb_mod.DESIGN_SCRIM, thumb_mod.DESIGN_SOFT_SHADOW, thumb_mod.DESIGN_ADAPTIVE
    ):
        style = thumb_mod.outline_style(0.19, design)
        assert len(style["strokes"]) == 1, design  # 環は1本だけ
        assert style["strokes"][0]["width"] < widest / 2, design


def test_outline_scales_with_text_size():
    """縁取りは文字サイズに比例する(小さい文字だけ縁が太くならない)。"""
    for design in thumb_mod.TEXT_DESIGNS:
        big = thumb_mod.outline_style(0.19, design)
        small = thumb_mod.outline_style(0.075, design)
        assert big["strokes"][0]["width"] > small["strokes"][0]["width"], design
        # どんなに小さい文字でも輪郭が消えない下限がある
        d = thumb_mod.resolve_design(design)
        tiny = thumb_mod.outline_style(0.001, design)
        assert tiny["strokes"][-1]["width"] >= d.min_contrast_stroke, design
        if d.shadow:
            assert big["shadow"] > small["shadow"], design
            assert thumb_mod.outline_style(0.001, design)["shadow"] >= d.min_shadow


def test_unknown_design_falls_back_to_the_default():
    assert thumb_mod.resolve_design("なにこれ") is thumb_mod.resolve_design(None)
    assert thumb_mod.resolve_design(None).name == thumb_mod.TEXT_DESIGN


# ---- 案1: 背景の上下グラデーション(帯ではない) ----


def test_scrim_darkens_the_edges_without_a_visible_border(tmp_path: Path):
    image = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (200, 200, 200)).save(image)
    scrimmed = compose_background(
        [image], 1280, 720, dim=1.0, design=thumb_mod.DESIGN_SCRIM
    )
    assert scrimmed is not None
    column = [scrimmed.getpixel((640, y))[0] for y in range(720)]
    assert column[0] < 120  # 上端はしっかり暗い
    assert column[719] < 120  # 下端も暗い
    assert max(column) == 200  # 上下のグラデーションが切れる所は写真のまま
    # 帯と違って段差が出ない(隣の行との差はどこでも数階調まで)
    assert max(abs(a - b) for a, b in zip(column, column[1:], strict=False)) <= 4


def test_only_scrim_designs_touch_the_background(tmp_path: Path):
    image = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (200, 200, 200)).save(image)
    for design in thumb_mod.TEXT_DESIGNS.values():
        bg = compose_background([image], 320, 180, dim=1.0, design=design)
        assert bg is not None
        darkened = bg.getpixel((160, 2))[0] < 200
        assert darkened == bool(design.scrim), design.name


# ---- 案3: 背景の明るさによる白黒反転 ----


def test_region_luminance_looks_at_the_darker_side_of_the_box():
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    for x in range(100):  # 上半分だけ白
        for y in range(50):
            img.putpixel((x, y), (255, 255, 255))
    assert thumb_mod.region_luminance(img, [0.0, 0.0, 1.0, 0.5]) == 255
    assert thumb_mod.region_luminance(img, [0.0, 0.5, 1.0, 0.5]) == 0
    # 明暗が半々の枠は「暗いほう」で見る(中央値なら明るい側に振れてしまう)
    assert thumb_mod.region_luminance(img, [0.0, 0.0, 1.0, 1.0]) == 0


def _text_colors(spec: dict) -> list[str]:
    return [e["color"] for e in spec["elements"] if e.get("type") == "text"]


def test_adaptive_keeps_white_text_where_the_box_spans_bright_and_dark():
    """左が明るく右が暗い写真では、黒文字にすると暗いほうで消えるので白のまま。"""
    design = thumb_mod.resolve_design(thumb_mod.DESIGN_ADAPTIVE)
    background = Image.new("RGB", (320, 180), (0, 0, 0))
    for x in range(160):  # 左半分だけ真っ白
        for y in range(180):
            background.putpixel((x, y), (255, 255, 255))
    spec = thumb_mod.thumbnail_layout_spec(True, True, design=design)
    applied = thumb_mod.apply_adaptive_colors(spec, background, design)
    assert _text_colors(applied)[0] == design.light_ink


def test_adaptive_flips_to_dark_text_on_a_bright_background():
    design = thumb_mod.resolve_design(thumb_mod.DESIGN_ADAPTIVE)
    spec = thumb_mod.thumbnail_layout_spec(True, True, design=design)
    bright = thumb_mod.apply_adaptive_colors(
        spec, Image.new("RGB", (320, 180), "white"), design
    )
    dark = thumb_mod.apply_adaptive_colors(
        spec, Image.new("RGB", (320, 180), "black"), design
    )
    assert _text_colors(bright) == [design.dark_ink] * 4
    assert _text_colors(dark) == [design.light_ink] * 4
    # 縁と影は文字の反対色になる(黒文字なら白い縁・白い光背)
    headline = bright["elements"][0]
    assert headline["strokes"][0]["color"] == design.light_ink
    assert headline["shadow_color"].startswith(design.light_ink)


def test_adaptive_judges_each_element_independently():
    """上が明るく下が暗い写真では、見出しだけ黒文字・下の文字は白文字になる。"""
    design = thumb_mod.resolve_design(thumb_mod.DESIGN_ADAPTIVE)
    background = Image.new("RGB", (320, 180), "black")
    for x in range(320):
        for y in range(90):
            background.putpixel((x, y), (255, 255, 255))
    spec = thumb_mod.thumbnail_layout_spec(True, True, design=design)
    applied = thumb_mod.apply_adaptive_colors(spec, background, design)
    # 見出し(上部)/ キャプション・クレジット・署名(下部)
    assert _text_colors(applied) == [design.dark_ink] + [design.light_ink] * 3


def test_non_adaptive_designs_keep_white_text():
    for name in (thumb_mod.DESIGN_DOUBLE_OUTLINE, thumb_mod.DESIGN_SCRIM):
        design = thumb_mod.resolve_design(name)
        spec = thumb_mod.thumbnail_layout_spec(True, True, design=design)
        applied = thumb_mod.apply_adaptive_colors(
            spec, Image.new("RGB", (320, 180), "white"), design
        )
        assert applied == spec
        assert _text_colors(applied) == [design.light_ink] * 4


def test_adaptive_render_is_readable_on_a_white_photo(tmp_path: Path):
    """白っぽい写真でも文字が背景に溶けない(黒に反転して描かれる)。"""
    image = tmp_path / "a.png"
    Image.new("RGB", (40, 40), (250, 250, 250)).save(image)
    out = render_thumbnail(
        tmp_path / THUMBNAIL_FILENAME, "夜に駆ける", "駅名", words="米原",
        image_paths=image, width=640, height=360, design=thumb_mod.DESIGN_ADAPTIVE,
    )
    with Image.open(out) as img:
        dark = sum(count for value, count in enumerate(img.convert("L").histogram())
                   if value < 60)
    assert dark > 1000  # 黒い文字が実際に乗っている


# ---- デザインの指紋(プレビューのキャッシュ) ----


def test_design_fingerprint_differs_between_designs():
    seen = {
        name: str(thumb_mod.design_fingerprint(name)) for name in thumb_mod.TEXT_DESIGNS
    }
    assert len(set(seen.values())) == len(seen)
    # specに出てこない背景側の設定(暗転・グラデーション・反転)も入っている
    text = seen[thumb_mod.DESIGN_SCRIM_ADAPTIVE]
    assert "background_dim" in text and "scrim" in text and "adaptive" in text


def test_layout_image_credit_hidden_when_empty():
    layout = thumbnail_layout(has_word=True, has_image=True, style=STYLE_SIDE)
    texts = layout.render_texts(thumbnail_data("曲", "駅名", word="米原"))
    assert "" in texts  # クレジット文言が空の画像では帯を描かない
    texts_with = layout.render_texts(
        thumbnail_data("曲", "駅名", word="米原", image_credit="山田太郎 (CC BY)")
    )
    assert "山田太郎 (CC BY)" in texts_with


def test_layout_signature_shows_synth_credit():
    # 歌声合成のクレジットが要るジョブでは、署名に併記して動画本編と揃える
    layout = thumbnail_layout(has_word=True, has_image=False)
    data = thumbnail_data(
        "夜に駆ける", "駅名", word="米原",
        app_credit=f"{SIGNATURE} / VOICEVOX:四国めたん",
    )
    texts = [t for t in layout.render_texts(data) if t]
    assert f"{SIGNATURE} / VOICEVOX:四国めたん" in texts
    # サムネは署名を自前で配置するので、レイアウト側の自動追加は行われない
    assert layout.app_credit is None


def test_song_title_prefers_fallback_and_strips_extension(tmp_path: Path):
    project = _project(_wordlist_csv(tmp_path), midi_path="/tmp/jobs/abc/input.mid")
    assert song_title(project, "夜に駆ける.mid") == "夜に駆ける"
    assert song_title(project) == "input"  # fallback が無ければプロジェクト側
    assert song_title(project, "  ") == "input"
    # 拡張子に見えない末尾は残す(曲名の一部を切らない)
    assert song_title(project, "Mr.Children.mid") == "Mr.Children"
    assert song_title(Project(song=SongInfo(midi_path="", ticks_per_beat=480))) == ""


# ---- 描画 ----


def test_render_thumbnail_without_image(tmp_path: Path):
    out = render_thumbnail(
        tmp_path / THUMBNAIL_FILENAME, "夜に駆ける", "駅名", words="米原",
        width=640, height=360,
    )
    assert out.exists()
    with Image.open(out) as img:
        assert img.size == (640, 360)


def test_render_thumbnail_with_image_is_full_bleed(tmp_path: Path):
    image = tmp_path / "word.png"
    Image.new("RGB", (200, 150), "red").save(image)
    out = render_thumbnail(
        tmp_path / THUMBNAIL_FILENAME, "夜に駆ける", "駅名", words="米原",
        image_paths=image, image_credits="山田太郎 (CC BY)", width=640, height=360,
    )
    with Image.open(out) as img:
        assert img.size == (640, 360)
        # 全面に敷かれるので四隅まで背景色(暗くした赤)になる
        for xy in ((5, 5), (634, 5), (5, 354), (634, 354)):
            r, g, b = img.getpixel(xy)
            assert r > 60 and g < 40 and b < 40, xy


def test_render_thumbnail_two_images_split_left_and_right(tmp_path: Path):
    red, blue = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (200, 150), "red").save(red)
    Image.new("RGB", (200, 150), "blue").save(blue)
    out = render_thumbnail(
        tmp_path / THUMBNAIL_FILENAME, "曲", "駅名", words=["モノカ", "加藤"],
        image_paths=[red, blue], width=640, height=360,
    )
    with Image.open(out) as img:
        assert img.getpixel((5, 5))[0] > img.getpixel((5, 5))[2]  # 左は赤
        assert img.getpixel((634, 5))[2] > img.getpixel((634, 5))[0]  # 右は青


def test_render_thumbnail_side_style_keeps_image_on_the_right(tmp_path: Path):
    image = tmp_path / "word.png"
    Image.new("RGB", (200, 150), "red").save(image)
    out = render_thumbnail(
        tmp_path / THUMBNAIL_FILENAME, "夜に駆ける", "駅名", words="米原",
        image_paths=image, width=640, height=360, style=STYLE_SIDE,
    )
    with Image.open(out) as img:
        assert img.getpixel((440, 150)) != (0, 0, 0)  # 右の枠に画像
        assert img.getpixel((5, 350)) == (0, 0, 0)  # 背景は黒のまま


def test_compose_background_dims_and_covers(tmp_path: Path):
    image = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (255, 255, 255)).save(image)
    bg = compose_background([image], 320, 180)
    assert bg is not None and bg.size == (320, 180)
    assert bg.getpixel((160, 90))[0] < 250  # 明るさを落としている
    # 可読性は縁取りで作るので、暗転は写真の中身が分かる程度に留める
    assert bg.getpixel((160, 90))[0] > 180
    assert compose_background([image], 320, 180, dim=0.5).getpixel((160, 90))[0] < 140
    assert compose_background([], 320, 180) is None
    assert compose_background([tmp_path / "missing.png"], 320, 180) is None


def test_transparent_portrait_is_contained_without_cropping(tmp_path: Path):
    image = tmp_path / "portrait.png"
    portrait = Image.new("RGBA", (100, 200), (0, 0, 0, 0))
    for y, color in (
        (range(0, 20), (255, 0, 0, 255)),
        (range(20, 180), (0, 255, 0, 255)),
        (range(180, 200), (0, 0, 255, 255)),
    ):
        for py in y:
            for px in range(40, 60):
                portrait.putpixel((px, py), color)
    portrait.save(image)

    bg = compose_background([image], 320, 180, dim=1.0)
    assert bg is not None
    # containなら頭側の赤と足側の青が両方残る。coverだと中央だけが残る。
    top = bg.getpixel((160, 5))
    bottom = bg.getpixel((160, 174))
    assert top[0] > 50 and top[0] > top[1] + top[2]
    assert bottom[2] > 50 and bottom[2] > bottom[0] + bottom[1]
    # 透明余白に埋め込まれたRGB値ではなく、明示した黒背景へ合成する。
    assert bg.getpixel((10, 90)) == (0, 0, 0)


# ---- generate_thumbnail(組み立て) ----


def _capture_render(monkeypatch) -> list[str]:
    """render_thumbnail の呼ばれ方を記録する(実描画はしない)。"""
    calls: list[str] = []

    def fake_render(out_path, title, wordlist_text, **kw):
        images = ",".join(Path(p).name for p in kw["image_paths"])
        calls.append(f"{title}|{wordlist_text}|{','.join(kw['words'])}|{images}")
        Image.new("RGB", (16, 9), "black").save(out_path)
        return out_path

    monkeypatch.setattr(thumb_mod, "render_thumbnail", fake_render)
    return calls


def test_generate_thumbnail_uses_converted_word(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert("ダイオージャ"))
    calls = _capture_render(monkeypatch)
    out = generate_thumbnail(_project(_wordlist_csv(tmp_path)), tmp_path)
    assert out == tmp_path / THUMBNAIL_FILENAME and out.exists()
    # 表示名はconf/setting.jsonに無いリストなのでstemそのまま。画像列が空なので画像なし
    assert calls == ["mysong|mylist|ダイオージャ|"]


def test_generate_thumbnail_uses_two_words_and_two_images(tmp_path: Path, monkeypatch):
    # image列はローカルパスでもよい(download_imageがコピーで取り込む=ネットワーク不要)
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (32, 24), "red").save(first)
    Image.new("RGB", (32, 24), "blue").save(second)
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert("米原", "大津"))
    calls = _capture_render(monkeypatch)
    project = _project(_wordlist_csv(tmp_path, image=str(first), image2=str(second)))
    generate_thumbnail(project, tmp_path, image_cache=tmp_path / "cache")
    # 先頭が短い語(米原=2文字)なので2語目まで採り、画像も2枚使う
    assert calls and calls[0].startswith("mysong|mylist|米原,大津|")
    assert len(calls[0].rsplit("|", 1)[1].split(",")) == 2


def test_generate_thumbnail_uses_word_image(tmp_path: Path, monkeypatch):
    image = tmp_path / "word.png"
    Image.new("RGB", (32, 24), "red").save(image)
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert("ダイオージャ"))
    calls = _capture_render(monkeypatch)
    project = _project(_wordlist_csv(tmp_path, image=str(image)))
    generate_thumbnail(project, tmp_path, image_cache=tmp_path / "cache")
    assert calls and calls[0].endswith(".png") and "ダイオージャ" in calls[0]


def _capture_convert_input(monkeypatch) -> list[list[str]]:
    """run_convert に渡った変換入力(フレーズ列)を記録する。"""
    seen: list[list[str]] = []

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None):
        seen.append(list(phrases))
        return {
            "lines": [{"units": [], "words": [{"surface": "モミジ", "id": "1"}]}],
            "tokensList": [],
            "phrases": phrases,
        }

    monkeypatch.setattr(thumb_mod, "run_convert", fake)
    return seen


def test_generate_thumbnail_converts_the_reading_but_captions_the_title(
    tmp_path: Path, monkeypatch
):
    # 読みが分かっている曲(サンプル)は、MeCabの推定(紅葉→コーヨー)ではなく
    # データの読みで変換する。キャプションに出す曲名は漢字のまま
    seen = _capture_convert_input(monkeypatch)
    calls = _capture_render(monkeypatch)
    project = _project(_wordlist_csv(tmp_path), midi_path="input.mid")
    generate_thumbnail(project, tmp_path, title="紅葉.mid", title_kana="モミジ")
    assert seen == [["モミジ"]]
    assert calls[0].startswith("紅葉|")


def test_generate_thumbnail_without_reading_converts_the_title(
    tmp_path: Path, monkeypatch
):
    # 読みが無い(自分のMIDI)ときは従来どおり曲名の文字列を変換に渡す
    seen = _capture_convert_input(monkeypatch)
    _capture_render(monkeypatch)
    project = _project(_wordlist_csv(tmp_path), midi_path="input.mid")
    generate_thumbnail(project, tmp_path, title="紅葉.mid")
    generate_thumbnail(project, tmp_path, title="紅葉.mid", title_kana="  ")
    assert seen == [["紅葉"], ["紅葉"]]


def test_generate_thumbnail_falls_back_when_convert_fails(tmp_path: Path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("変換できません")

    monkeypatch.setattr(thumb_mod, "run_convert", boom)
    out = generate_thumbnail(
        _project(_wordlist_csv(tmp_path)), tmp_path, width=320, height=180
    )
    # 変換が落ちてもサムネ自体は作る(言い換えなし)
    assert out is not None and out.exists()
    with Image.open(out) as img:
        assert img.size == (320, 180)


def test_generate_thumbnail_without_wordlist(tmp_path: Path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("単語リストが無いのに変換した")

    monkeypatch.setattr(thumb_mod, "run_convert", unexpected)
    project = _project(_wordlist_csv(tmp_path))
    project.parody = None
    out = generate_thumbnail(project, tmp_path, width=320, height=180)
    assert out is not None and out.exists()


def test_generate_thumbnail_returns_none_on_render_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert("米原"))

    def boom(*args, **kwargs):
        raise OSError("書き込めません")

    monkeypatch.setattr(thumb_mod, "render_thumbnail", boom)
    assert generate_thumbnail(_project(_wordlist_csv(tmp_path)), tmp_path) is None


def test_generate_thumbnail_image_failure_falls_back(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert("米原"))

    def boom(row, cache, download=True):
        raise RuntimeError("取得できません")

    monkeypatch.setattr(thumb_mod, "_word_image", boom)
    project = _project(_wordlist_csv(tmp_path, image="https://x/y.jpg"))
    out = generate_thumbnail(project, tmp_path, width=320, height=180)
    assert out is not None and out.exists()


def test_thumbnail_spec_comes_from_layout_json(tmp_path, monkeypatch):
    # サムネのレイアウトは layouts/thumbnail.json が出どころ(コード内に組んでいない)
    import json as _json

    from soramimic_video import thumbnail as thumb_mod

    assert thumb_mod.THUMBNAIL_LAYOUT_PATH.name == "thumbnail.json"
    raw = _json.loads(thumb_mod.THUMBNAIL_LAYOUT_PATH.read_text(encoding="utf-8"))
    for style in (thumb_mod.STYLE_FULLBLEED, thumb_mod.STYLE_SIDE):
        assert set(raw[style]) == {"word_image", "word_only", "no_word"}

    # JSONを差し替えれば出力も変わる(キャッシュ経由でも読み直せる)
    other = tmp_path / "thumbnail.json"
    other.write_text(_json.dumps({
        "fullbleed": {
            "word_image": {"background": "navy", "elements": [
                {"type": "text", "text": "{headline}", "box": [0, 0, 1, 1], "size": 0.2},
            ]},
            "word_only": {"elements": []}, "no_word": {"elements": []},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(thumb_mod, "THUMBNAIL_LAYOUT_PATH", other)
    monkeypatch.setattr(thumb_mod, "_thumbnail_layouts_cache", None)
    spec = thumb_mod.thumbnail_layout_spec(True, True)
    assert spec["background"] == "navy"
    assert spec["elements"] == [
        {"type": "text", "text": "{headline}", "box": [0, 0, 1, 1], "size": 0.2}
    ]


def test_thumbnail_outline_flag_expands_to_design():
    # "outline": true は採用中の可読性デザインの色・縁取りに展開され、印は残らない
    from soramimic_video import thumbnail as thumb_mod

    headline = thumb_mod.thumbnail_layout_spec(True, True)["elements"][0]
    assert "outline" not in headline
    assert headline["strokes"] == thumb_mod.outline_style(headline["size"])["strokes"]


def test_thumbnail_credit_box_replaces_image_box():
    # side スタイルのクレジットだけ、実際に貼られた画像の枠へ差し替わる
    from soramimic_video import thumbnail as thumb_mod

    box = (0.6, 0.1, 0.3, 0.4)
    spec = thumb_mod.thumbnail_layout_spec(
        True, True, credit_box=box, style=thumb_mod.STYLE_SIDE
    )
    credit = next(e for e in spec["elements"] if e.get("text") == "{image_credit}")
    assert credit["box"] == list(box)
    assert "credit_box" not in credit
    # 渡さなければ画像の枠のまま
    default = thumb_mod.thumbnail_layout_spec(True, True, style=thumb_mod.STYLE_SIDE)
    plain = next(e for e in default["elements"] if e.get("text") == "{image_credit}")
    assert plain["box"] == list(thumb_mod.thumbnail_image_box())


def test_thumbnail_json_is_not_a_selectable_frame_layout():
    # thumbnail.json は動画フレームのレイアウトではないのでUIの選択肢に出さない
    from soramimic_video.layout import builtin_layout_names, load_layout

    assert "thumbnail" not in builtin_layout_names()
    with pytest.raises(FileNotFoundError):
        load_layout("thumbnail")
