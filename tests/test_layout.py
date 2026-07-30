"""layout.py のテスト: レイアウト読み込みとPillowでのフレーム合成。"""

import json

import pytest
from PIL import Image

from soramimic_video import layout as layout_mod
from soramimic_video.layout import (
    APP_CREDIT,
    builtin_layout_names,
    load_layout,
    load_wordlist_layouts,
    parse_layout,
    render_frame,
    render_idle_frame,
    render_section_frame,
    resolve_app_credit,
)


def test_load_builtin_layouts():
    default = load_layout(None)
    assert len(default.elements) == 1
    caption = load_layout("caption")
    assert len(caption.elements) == 2


def test_wordlist_layouts_are_builtin():
    """同梱の対応表は組み込みレイアウト名だけを指していること。"""
    mapping = load_wordlist_layouts()
    assert mapping["scientist"] == "scientist_card"
    assert set(mapping.values()) <= set(builtin_layout_names())


def test_load_wordlist_layouts_skips_unknown(tmp_path, monkeypatch, caplog):
    p = tmp_path / "wordlist_layouts.json"
    p.write_text(json.dumps({
        "scientist": "caption",
        "stations": "no-such-layout",  # 組み込みに無いので捨てられる
        "plant": 123,                   # 文字列でないので捨てられる
    }), encoding="utf-8")
    monkeypatch.setattr(layout_mod, "WORDLIST_LAYOUTS_PATH", p)
    with caplog.at_level("WARNING"):
        assert load_wordlist_layouts() == {"scientist": "caption"}
    assert "no-such-layout" in caplog.text


def test_load_wordlist_layouts_missing_or_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(layout_mod, "WORDLIST_LAYOUTS_PATH", tmp_path / "none.json")
    assert load_wordlist_layouts() == {}
    broken = tmp_path / "broken.json"
    broken.write_text("[not json", encoding="utf-8")
    monkeypatch.setattr(layout_mod, "WORDLIST_LAYOUTS_PATH", broken)
    assert load_wordlist_layouts() == {}
    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(layout_mod, "WORDLIST_LAYOUTS_PATH", listed)
    assert load_wordlist_layouts() == {}


def test_load_unknown_layout():
    with pytest.raises(FileNotFoundError):
        load_layout("no-such-layout")


def test_load_layout_from_json_path(tmp_path):
    p = tmp_path / "my.json"
    p.write_text(json.dumps({
        "background": "#202020",
        "elements": [
            {"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.2]},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.background == "#202020"
    assert layout.render_texts({"surface": "静岡"}) == ["静岡"]


def test_load_subtitle_elements(tmp_path):
    p = tmp_path / "sub.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "image", "box": [0, 0, 1, 0.7]},
            {"type": "subtitle", "source": "original", "box": [0.1, 0.05, 0.8, 0.08]},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    # subtitleはPillow描画の対象外(elementsに混ざらない)
    assert len(layout.elements) == 1
    assert len(layout.subtitles) == 1
    assert layout.subtitles[0].source == "original"
    assert layout.render_texts({"surface": "x"}) == []


def test_load_subtitle_bad_source(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "elements": [{"type": "subtitle", "source": "karaoke", "box": [0, 0, 1, 0.1]}],
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_layout(str(p))


def test_load_subtitle_granularity(tmp_path):
    # レイアウトエディタで設定した粒度が読み込める。省略時は None(=入力欄/既定に従う)
    p = tmp_path / "gran.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "subtitle", "source": "parody", "box": [0, 0.7, 1, 0.1],
             "granularity": "line"},
            {"type": "subtitle", "source": "original", "box": [0, 0.9, 1, 0.05]},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.subtitles[0].granularity == "line"
    assert layout.subtitles[1].granularity is None  # 省略=既定


def test_load_subtitle_bad_granularity(tmp_path):
    p = tmp_path / "badg.json"
    p.write_text(json.dumps({
        "elements": [{"type": "subtitle", "source": "parody", "box": [0, 0, 1, 0.1],
                      "granularity": "word"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_layout(str(p))


def test_render_texts_missing_column_is_empty():
    layout = load_layout("caption")
    # original列がないデータでは空文字になる(エラーにしない)
    assert layout.render_texts({"surface": "ホシズム"}) == [""]


def test_render_frame_image_and_text(tmp_path):
    img = tmp_path / "word.png"
    Image.new("RGB", (300, 200), "red").save(img)
    layout = load_layout("caption")
    data = {"surface": "ホシズム", "original": "静岡駅"}
    out = render_frame(layout, img, data, 320, 180, tmp_path / "frames")
    assert out is not None and out.exists()
    with Image.open(out) as frame:
        assert frame.size == (320, 180)
    # 同内容の再呼び出しはキャッシュを返す
    again = render_frame(layout, img, data, 320, 180, tmp_path / "frames")
    assert again == out
    # テキストが違えば別フレームになる
    other = render_frame(layout, img, {**data, "original": "沼津駅"},
                         320, 180, tmp_path / "frames")
    assert other != out


def test_render_frame_text_only(tmp_path):
    layout = load_layout("caption")
    out = render_frame(layout, None, {"original": "静岡駅"}, 320, 180, tmp_path / "f")
    assert out is not None and out.exists()


def test_default_layout_has_fallback():
    # 既定レイアウトには未知語用のfallbackがある(elementsは画像のみのまま)
    layout = load_layout(None)
    assert len(layout.elements) == 1
    assert layout.fallback
    assert layout.render_texts({"surface": "未知語", "original": "元"}, use_fallback=True) == [
        "未知語",
        "(元)",
    ]


def test_fallback_elements_selected(tmp_path):
    p = tmp_path / "fb.json"
    p.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{achievement}", "box": [0.1, 0.1, 0.8, 0.1]}],
        "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    data = {"surface": "未知語", "achievement": ""}
    # 通常側: achievementが空なので空文字
    assert layout.render_texts(data) == [""]
    # fallback側: 単語フィールドで埋まる
    assert layout.render_texts(data, use_fallback=True) == ["未知語"]
    # fallback定義がなければ use_fallback でも通常側を使う(従来動作を維持)
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
    }), encoding="utf-8")
    assert load_layout(str(plain)).render_texts({"surface": "x"}, use_fallback=True) == ["x"]


def test_require_hides_element_when_column_empty(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "text", "text": "{original}", "box": [0.1, 0.1, 0.8, 0.1]},
            {"type": "text", "text": "没年 {death}", "box": [0.1, 0.3, 0.8, 0.1],
             "require": "death"},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.elements[1].require == "death"
    # deathがある単語は両方出る
    assert layout.render_texts({"original": "X", "death": "1900"}) == ["X", "没年 1900"]
    # deathが空/欠けている単語ではrequire要素は空文字(描画側でスキップ)
    assert layout.render_texts({"original": "X"}) == ["X", ""]
    assert layout.render_texts({"original": "X", "death": ""}) == ["X", ""]


def test_scientist_card_field_fallback_for_all_missing_entries():
    """生年・国籍・業績・説明が全欠損でも field 列があれば「分野: X」を出す。

    scientist.csv の一部エントリ(例: 西川正治, id=248)は画像もbirth_year/country/
    achievement/descriptionも全部NAで、従来は名前だけの黒背景カードになっていた。
    field列だけは持っているので、それを最低限のフォールバックとして表示する。
    """
    layout = load_layout("scientist_card")
    # 西川正治相当: field以外は全部欠損
    nishikawa = {
        "original": "西川正治", "surface": "西川", "field": "物理",
        "birth_year": "NA", "nationality": "NA", "country": "NA",
        "achievement": "NA", "description": "NA",
    }
    texts = layout.render_texts(nishikawa)
    assert "分野: 物理" in texts
    # 生年行(country由来)は出ない
    assert not any("生まれ" in t for t in texts)

    # birth_yearがある人では従来の生年行が出て、field行は出ない(重複回避)
    with_birth_year = {**nishikawa, "birth_year": "1902", "country": "日本"}
    texts2 = layout.render_texts(with_birth_year)
    assert "分野: 物理" not in texts2
    assert any("生まれ" in t for t in texts2)


def test_render_frame_fallback(tmp_path):
    p = tmp_path / "fb.json"
    p.write_text(json.dumps({
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
        "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2],
                      "size": 0.1}],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    data = {"surface": "未知語", "original": "元"}
    # fallback側(画像なし)でもフレームが出る
    out = render_frame(layout, None, data, 320, 180, tmp_path / "f", use_fallback=True)
    assert out is not None and out.exists()
    # 通常側と別キャッシュになる(別の要素集合)
    normal = render_frame(layout, None, data, 320, 180, tmp_path / "f", use_fallback=False)
    assert normal != out


def test_idle_and_hold_parse(tmp_path):
    # idle セクションと "hold": "next" を読み込む。idle内のsubtitleは無視される
    p = tmp_path / "idle.json"
    p.write_text(json.dumps({
        "hold": "next",
        "idle": [
            {"type": "text", "text": "{title}", "box": [0.1, 0.4, 0.8, 0.2]},
            {"type": "subtitle", "source": "parody", "box": [0, 0, 1, 0.1]},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.hold_next is True
    assert len(layout.idle) == 1  # subtitleはidleでは無視される
    # hold省略時はhold_next=Falseが既定
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
    }), encoding="utf-8")
    assert load_layout(str(plain)).hold_next is False


def test_render_idle_frame(tmp_path):
    p = tmp_path / "idle.json"
    p.write_text(json.dumps({
        "idle": [
            {"type": "text", "text": "{title}", "box": [0.1, 0.35, 0.8, 0.2], "size": 0.12},
            {"type": "text", "text": "単語リスト: {wordlist}", "box": [0.1, 0.6, 0.8, 0.08]},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    out = render_idle_frame(layout, {"title": "夜に駆ける", "wordlist": "stations"},
                            320, 180, tmp_path / "f")
    assert out is not None and out.exists()
    with Image.open(out) as frame:
        assert frame.size == (320, 180)
    # 同内容はキャッシュを返す / 文言が違えば別フレーム
    again = render_idle_frame(layout, {"title": "夜に駆ける", "wordlist": "stations"},
                              320, 180, tmp_path / "f")
    assert again == out
    other = render_idle_frame(layout, {"title": "別の曲", "wordlist": "stations"},
                              320, 180, tmp_path / "f")
    assert other != out


def test_render_idle_frame_absent_has_app_credit_only(tmp_path):
    # idleセクションが無くても、アプリクレジットだけを載せたフレームは作る
    # (間奏でだけ表記が消えないように)
    layout = load_layout("caption")
    out = render_idle_frame(layout, {"title": "x"}, 320, 180, tmp_path / "f")
    assert out is not None and out.exists()


def test_render_idle_frame_absent_is_none_without_app_credit(tmp_path):
    # idleセクションもクレジットも無ければ None(呼び出し側は黒画面のまま)
    p = tmp_path / "nocredit.json"
    p.write_text(json.dumps({
        "app_credit": False,
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert render_idle_frame(layout, {"title": "x"}, 320, 180, tmp_path / "f") is None


def test_credit_element_auto_added_for_image_layouts():
    # image要素のあるレイアウトには {image_credit} の自動焼き込み要素が付く
    layout = load_layout(None)
    assert layout.credit is not None
    assert layout.credit.template == "{image_credit}"
    # elements自体には混ぜない(render_textsや要素数は従来どおり)
    assert len(layout.elements) == 1


def test_credit_element_disabled_by_flag(tmp_path):
    p = tmp_path / "nc.json"
    p.write_text(json.dumps({
        "credit": False,
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
    }), encoding="utf-8")
    assert load_layout(str(p)).credit is None


def test_credit_element_skipped_when_placed_manually(tmp_path):
    # text要素で {image_credit} を自分で配置したレイアウトには自動追加しない
    p = tmp_path / "manual.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "image", "box": [0, 0, 1, 0.7]},
            {"type": "text", "text": "{image_credit}", "box": [0, 0.9, 1, 0.05],
             "size": 0.03},
        ],
    }), encoding="utf-8")
    assert load_layout(str(p)).credit is None


def test_credit_element_skipped_without_image(tmp_path):
    p = tmp_path / "noimg.json"
    p.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.2]}],
    }), encoding="utf-8")
    assert load_layout(str(p)).credit is None


def test_render_frame_draws_credit(tmp_path):
    img = tmp_path / "word.png"
    Image.new("RGB", (300, 200), "red").save(img)
    layout = load_layout(None)
    data = {"surface": "ホシズム", "original": "静岡駅"}
    plain = render_frame(layout, img, data, 320, 180, tmp_path / "f")
    # クレジット文言があるとフレーム内容(キャッシュキー)が変わる
    credited = render_frame(
        layout, img, {**data, "image_credit": "山田 太郎, CC BY-SA 4.0, via Wikimedia Commons"},
        320, 180, tmp_path / "f",
    )
    assert plain is not None and credited is not None
    assert credited != plain
    # 文言が空(表記不要)ならクレジットなしと同じフレーム
    empty = render_frame(layout, img, {**data, "image_credit": ""}, 320, 180, tmp_path / "f")
    assert empty == plain


def test_app_credit_element_auto_added(tmp_path):
    # どのレイアウトにも {app_credit} の自動焼き込み要素が付く(既定は左下)
    for name in builtin_layout_names():
        layout = load_layout(name)
        assert layout.app_credit is not None, name
        assert layout.app_credit.template == "{app_credit}"
        assert layout.app_credit.align == "left"
        assert layout.app_credit.valign == "bottom"
        # 画像クレジットより小さく、既定字幕(下端0.945)と重ならない最下段
        assert layout.app_credit.size <= 0.025
        assert layout.app_credit.box[1] >= 0.945
    # elements自体には混ぜない(render_textsや要素数は従来どおり)
    assert len(load_layout(None).elements) == 1


def test_app_credit_element_disabled_by_flag(tmp_path):
    p = tmp_path / "noapp.json"
    p.write_text(json.dumps({
        "app_credit": False,
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
    }), encoding="utf-8")
    assert load_layout(str(p)).app_credit is None


def test_app_credit_element_skipped_when_placed_manually(tmp_path):
    # text要素で {app_credit} を自分で配置したレイアウトには自動追加しない
    p = tmp_path / "manualapp.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.2]},
            {"type": "text", "text": "{app_credit}", "box": [0.7, 0.9, 0.3, 0.05],
             "size": 0.03},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.app_credit is None
    assert layout.render_texts(
        {"surface": "x", "app_credit": f"{APP_CREDIT} / VOICEVOX:四国めたん"}
    )[1].endswith("VOICEVOX:四国めたん")
    # 描画時は data に文言が無くても既定の署名が入る(自前配置でも空にならない)
    assert resolve_app_credit({"surface": "x"}) == APP_CREDIT
    assert resolve_app_credit({"app_credit": "  "}) == APP_CREDIT
    assert resolve_app_credit({"app_credit": "X / Y"}) == "X / Y"


def test_render_frame_draws_app_credit(tmp_path):
    layout = load_layout("caption")
    data = {"surface": "ホシズム", "original": "静岡駅"}
    plain = render_frame(layout, None, data, 320, 180, tmp_path / "f")
    # 署名の文言が変わればフレーム(キャッシュキー)も変わる
    with_synth = render_frame(
        layout, None, {**data, "app_credit": f"{APP_CREDIT} / VOICEVOX:四国めたん"},
        320, 180, tmp_path / "f",
    )
    assert plain is not None and with_synth is not None and with_synth != plain
    # 既定の署名を明示指定したものは既定と同じフレーム
    same = render_frame(
        layout, None, {**data, "app_credit": APP_CREDIT}, 320, 180, tmp_path / "f"
    )
    assert same == plain
    # 無効化したレイアウトでは描かれない(=別フレームになる)
    p = tmp_path / "noapp.json"
    p.write_text(json.dumps({
        "app_credit": False,
        "elements": [
            {"type": "image", "box": [0.09, 0.05, 0.82, 0.56]},
            {"type": "text", "text": "{original}", "box": [0.05, 0.63, 0.9, 0.1],
             "size": 0.065, "color": "white", "stroke_width": 0.004},
        ],
    }), encoding="utf-8")
    off = render_frame(load_layout(str(p)), None, data, 320, 180, tmp_path / "f")
    assert off != plain


def test_render_frame_wrap_long_text(tmp_path):
    p = tmp_path / "wrap.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "text", "text": "{achievement}", "box": [0.1, 0.1, 0.8, 0.6],
             "size": 0.1, "wrap": True, "valign": "top", "align": "left"},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    data = {"achievement": "天然鉱石と光の拡散関係の初期論説" * 5}
    out = render_frame(layout, None, data, 320, 180, tmp_path / "f")
    assert out is not None and out.exists()


# ---- 縁取り(strokes)とぼかし影(shadow) ----


def _outlined_layout(**text):
    """1つのtext要素だけを持つレイアウト(縁取りの検証用)。"""
    return parse_layout({
        "background": "#808080",
        "app_credit": False,
        "elements": [{"type": "text", "text": "I", "box": [0.0, 0.0, 1.0, 1.0],
                      "size": 0.5, "color": "white", **text}],
    })


def _row_runs(img, y):
    """1行ぶんの色を 明(w)/暗(k)/背景(b) に丸めて連続を畳んだ列。"""
    runs = []
    for x in range(img.width):
        r, g, b = img.getpixel((x, y))
        if r > 230 and g > 230 and b > 230:
            label = "w"
        elif r < 40 and g < 40 and b < 40:
            label = "k"
        elif (r, g, b) == (128, 128, 128):
            label = "b"
        else:
            continue  # アンチエイリアスの中間色は無視する
        if not runs or runs[-1] != label:
            runs.append(label)
    return runs


def test_strokes_draw_concentric_rings():
    """strokes は太い順に同心の環になる(白い文字→黒い環→白い環)。"""
    layout = _outlined_layout(strokes=[
        {"width": 0.04, "color": "black"}, {"width": 0.08, "color": "white"},
    ])
    img = layout_mod.render_image(layout, None, {}, 200, 120)
    runs = _row_runs(img, 60)
    assert runs[:4] == ["b", "w", "k", "w"], runs


def test_strokes_are_sorted_widest_first():
    layout = _outlined_layout(strokes=[[0.01, "white"], [0.03, "black"]])
    el = layout.elements[0]
    assert el.strokes == ((0.03, "black"), (0.01, "white"))


def test_strokes_bad_format():
    with pytest.raises(ValueError, match="strokes"):
        _outlined_layout(strokes={"width": 0.01})
    with pytest.raises(ValueError, match="strokes"):
        _outlined_layout(strokes=["black"])


def test_strokes_fit_inside_the_box():
    """太い縁取りぶんは文字を小さくして収める(boxからはみ出さない)。"""
    box = [0.2, 0.2, 0.6, 0.6]
    layout = parse_layout({
        "background": "#808080",
        "app_credit": False,
        "elements": [{"type": "text", "text": "IIIIIIII", "box": box, "size": 0.4,
                      "color": "white",
                      "strokes": [{"width": 0.08, "color": "black"}]}],
    })
    img = layout_mod.render_image(layout, None, {}, 200, 120)
    ink = [(x, y) for y in range(img.height) for x in range(img.width)
           if img.getpixel((x, y)) != (128, 128, 128)]
    assert ink, "何も描かれていない"
    assert min(x for x, _ in ink) >= int(box[0] * 200)
    assert max(x for x, _ in ink) <= int((box[0] + box[2]) * 200)
    assert min(y for _, y in ink) >= int(box[1] * 120)
    assert max(y for _, y in ink) <= int((box[1] + box[3]) * 120)


def test_shadow_darkens_only_around_the_text():
    plain = layout_mod.render_image(_outlined_layout(), None, {}, 200, 120)
    shaded = layout_mod.render_image(_outlined_layout(shadow=0.05), None, {}, 200, 120)
    # 文字のすぐ横は暗くなる
    assert shaded.getpixel((85, 60))[0] < plain.getpixel((85, 60))[0]
    # 遠く離れた隅はそのまま(矩形の帯ではない)
    assert shaded.getpixel((2, 2)) == plain.getpixel((2, 2))


def test_builtin_layouts_do_not_use_new_text_options():
    """既存レイアウトの見た目は変えない(縁取り・影は未使用のまま)。"""
    for name in builtin_layout_names():
        for el in load_layout(name).elements:
            if isinstance(el, layout_mod.TextElement):
                assert el.strokes == () and el.shadow == 0.0, name


def test_section_defaults_provide_interlude_and_outro():
    # レイアウトが何も書かなくても section_defaults.json の既定が入る
    from soramimic_video.layout import load_section_defaults

    layout = load_layout("default")
    assert layout.has_section("interlude")
    assert layout.has_section("outro")
    # 前奏はサムネが受け持つので既定なし
    assert not layout.has_section("intro")
    assert "interlude" in load_section_defaults()


def test_layout_can_override_and_disable_sections(tmp_path):
    p = tmp_path / "sec.json"
    p.write_text(json.dumps({
        "elements": [{"type": "image", "box": [0, 0, 1, 0.7]}],
        "interlude": [
            {"type": "text", "text": "〜{interlude_sec}秒〜", "box": [0.1, 0.4, 0.8, 0.2]},
        ],
        "outro": [],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.has_section("interlude")
    assert not layout.has_section("outro")  # 空配列で既定を打ち消せる
    elements, raw, tag = layout.section_elements("interlude")
    assert tag == "interlude" and raw[0]["text"] == "〜{interlude_sec}秒〜"
    # 専用定義の無い区間は idle にフォールバックする
    assert layout.section_elements("outro")[2] == "idle"


def test_render_section_frame_uses_section_elements(tmp_path):
    p = tmp_path / "sec.json"
    p.write_text(json.dumps({
        "idle": [{"type": "text", "text": "idle", "box": [0.1, 0.4, 0.8, 0.2]}],
        "interlude": [
            {"type": "text", "text": "間奏({interlude_sec}秒)", "box": [0.1, 0.4, 0.8, 0.2]},
        ],
        "outro": [],  # 既定のエンドロールを打ち消して idle へのフォールバックを見る
    }), encoding="utf-8")
    layout = load_layout(str(p))
    data = {"title": "t", "wordlist": "w", "interlude_sec": "12"}
    inter = render_section_frame(layout, data, 320, 180, tmp_path / "f", "interlude")
    idle = render_section_frame(layout, data, 320, 180, tmp_path / "f", "idle")
    assert inter is not None and idle is not None and inter != idle
    # 秒数が変われば別フレーム(テンプレート展開がキャッシュキーに効く)
    other = render_section_frame(
        layout, {**data, "interlude_sec": "20"}, 320, 180, tmp_path / "f", "interlude"
    )
    assert other != inter
    # 専用定義の無い区間は idle と同じフレームになる
    assert render_section_frame(layout, data, 320, 180, tmp_path / "f", "outro") == idle


def test_section_app_credit_not_duplicated(tmp_path):
    # 区間側が {app_credit} を自分で並べていても、単語フレームの署名は消えない
    p = tmp_path / "sec.json"
    p.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
        "outro": [{"type": "text", "text": "{app_credit}", "box": [0.1, 0.8, 0.8, 0.1]}],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.app_credit is not None


def _columns_element(columns=3, size=0.06):
    from soramimic_video.layout import TextElement

    return TextElement(
        template="", box=(0.0, 0.0, 1.0, 1.0), size=size, align="left", columns=columns
    )


def _columns_placement(items, columns=3, width=960, height=540, size=0.06):
    """段組みの割り付け結果 (draw, placed, 列幅, 列間) を返すテスト用ヘルパ。

    列幅は「実際に選ばれた段数」から求める(段数は語の幅で自動的に減りうる)。
    """
    from PIL import ImageDraw

    from soramimic_video.layout import (
        _choose_columns,
        _column_geometry,
        _layout_columns,
        resolve_font_path,
    )

    el = _columns_element(columns, size)
    font_path = resolve_font_path(None)
    draw = ImageDraw.Draw(Image.new("RGB", (width, height)))
    placed, _ = _layout_columns(
        draw, "\n".join(items), el, (0, 0, width, height), height, font_path, 0,
    )
    cols, _px = _choose_columns(draw, items, el, width, height, height, font_path)
    col_w, gap = _column_geometry(width, cols)
    return draw, placed, col_w, gap


def test_text_columns_place_items_column_major():
    items = [f"語{i}" for i in range(9)]
    _, placed, col_w, gap = _columns_placement(items)
    assert [t for _, _, t, _ in placed] == items
    xs = [x for x, _, _, _ in placed]
    ys = [y for _, y, _, _ in placed]
    # 3列×3行。列優先(上から下へ埋めて次の列)で、各列は左詰め
    assert xs[0] == xs[1] == xs[2] == 0
    assert xs[3] == xs[4] == xs[5] == pytest.approx(col_w + gap)
    assert ys[0] < ys[1] < ys[2]
    assert ys[3] == ys[0] and ys[6] == ys[0]


def _overflowing_item(
    width, height, size, columns, n_items, factor=1.2, unit="ヴィルヘルム・"
):
    """その段組みで列幅の factor 倍を確実に超える語を作る。

    文字の実寸はフォントによって変わる(CIと手元で違う)ので、固定の長い名前を
    置くと環境によっては収まってしまい縮小が発動しない。実測しながら伸ばす。
    factor を大きくすると「その段数では本文が下限を割る」ほど長い語になる。
    """
    from PIL import ImageDraw

    from soramimic_video.layout import _column_geometry, _font, resolve_font_path

    draw = ImageDraw.Draw(Image.new("RGB", (width, height)))
    col_w, _gap = _column_geometry(width, columns)
    # 高さから決まる上限(=そのレイアウトで取りうる最大の本文サイズ)で測る
    rows = max(1, -(-n_items // columns))
    px = max(9, min(int(size * height), int(height / (rows * 1.25))))
    font = _font(resolve_font_path(None), px)
    text = unit
    while draw.textlength(text, font=font) <= col_w * factor:
        text += unit
    return text


def test_text_columns_shrink_only_overflowing_item():
    width, height, size, columns = 960, 540, 0.06, 3
    short = [f"新宿{i}" for i in range(11)]
    long_name = _overflowing_item(width, height, size, columns, len(short) + 1)
    items = [*short[:2], long_name, *short[2:]]
    draw, placed, col_w, gap = _columns_placement(
        items, columns=columns, width=width, height=height, size=size
    )
    by_text = {t: (x, f) for x, _, t, f in placed}
    # 長い語も自分の列の幅からはみ出さない(隣の列に食い込まない)
    for x, _, text, font in placed:
        col_start = round(x / (col_w + gap)) * (col_w + gap)
        assert x + draw.textlength(text, font=font) <= col_start + col_w + 1
    # はみ出したのはこの1語だけなので、他の語のサイズは巻き添えで縮まない。
    # 日本語フォントが無い環境の既定フォントはサイズ指定を持たないので比較しない
    from soramimic_video.layout import resolve_font_path

    if resolve_font_path(None) is not None:
        assert by_text[long_name][1].size < by_text["新宿0"][1].size


def test_columns_element_parsed_and_rendered(tmp_path):
    p = tmp_path / "cols.json"
    p.write_text(json.dumps({
        "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.1]}],
        "outro": [
            {"type": "text", "text": "{used_words}", "box": [0.06, 0.2, 0.88, 0.6],
             "columns": 3, "align": "left"},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    assert layout.section_elements("outro")[0][0].columns == 3
    data = {"used_words": "\n".join(f"語{i}" for i in range(12))}
    frame = render_section_frame(layout, data, 320, 180, tmp_path / "f", "outro")
    assert frame is not None and frame.exists()


def test_default_outro_has_no_image_credits():
    from soramimic_video.layout import load_section_defaults

    defaults = load_section_defaults()
    # 帰属表示は各単語フレームの右下に個別に焼くので、後奏では集約しない
    assert not any("{image_credits}" in e.get("text", "") for e in defaults["outro"])
    # 間奏は「♪」と「間奏(X秒)」だけ(「〜で歌ってみた」は出さない)
    assert not any("{wordlist}" in e.get("text", "") for e in defaults["interlude"])


def _choose_cols(items, columns=4, width=1920, height=800, size=0.05):
    from PIL import ImageDraw

    from soramimic_video.layout import _choose_columns, resolve_font_path

    el = _columns_element(columns, size)
    draw = ImageDraw.Draw(Image.new("RGB", (width, height)))
    return _choose_columns(draw, items, el, width, height, height, resolve_font_path(None))


def test_columns_count_adapts_to_word_width():
    width, height, size, columns = 1920, 800, 0.05, 4
    kw = {"columns": columns, "width": width, "height": height, "size": size}
    # 駅名のような短い語だけなら上限いっぱいまで段を増やして本文を大きくする
    short_cols, short_px = _choose_cols(["新宿"] * 60, **kw)
    assert short_cols == columns
    # 外国人のフルネームが並ぶリストは段を減らして列幅を稼ぐ。語の実寸はフォントで
    # 変わるので、上限の段数では本文が下限を割るほど長い語を実測して作る
    long_name = _overflowing_item(width, height, size, columns, 60, factor=4)
    long_cols, long_px = _choose_cols([long_name] * 60, **kw)
    assert 1 <= long_cols < short_cols
    # 段を減らすと行数が増えるので本文は小さくなる(そのぶん語がはみ出さない)
    assert long_px < short_px
    assert _choose_cols([], **kw)[0] == 1


def _default_endroll_columns(items, width=1920, height=1080):
    """既定のエンドロール(section_defaults.json の outro)で選ばれる段数と本文サイズ。"""
    from PIL import ImageDraw

    from soramimic_video.layout import (
        _choose_columns,
        _parse_elements,
        load_section_defaults,
        resolve_font_path,
    )

    raw = load_section_defaults()["outro"]
    elements, _subs = _parse_elements(raw, "section_defaults.json")
    el = next(e for e in elements if "{used_words}" in getattr(e, "template", ""))
    _x, _y, bw, bh = el.box
    draw = ImageDraw.Draw(Image.new("RGB", (width, height)))
    return _choose_columns(
        draw, items, el, int(bw * width), int(bh * height), height, resolve_font_path(None)
    )


def test_default_endroll_packs_short_words_to_upper_limit():
    from soramimic_video.video import ENDROLL_WORDS_PER_PAGE

    n = ENDROLL_WORDS_PER_PAGE
    # 駅名のような短い語なら、1枚分(ENDROLL_WORDS_PER_PAGE)を上限の段数で詰められる
    cols, px = _default_endroll_columns([f"新宿{i % 10}" for i in range(n)])
    assert cols == 8
    # 短い語では段数は高さで決まる(列幅で頭打ちにならない)ので、行がboxを埋める
    rows = -(-n // cols)
    assert px * 1.25 * rows > 0.9 * 0.74 * 1080
    # 同じ枚数でも長いフルネームが並ぶと列幅が足りず、段数は減って本文も小さくなる
    long_cols, long_px = _default_endroll_columns(
        [f"ガスパール＝ギュスターヴ・コリオリ{i}" for i in range(n)]
    )
    assert long_cols < cols and long_px < px


def test_require_prefix_switches_elements_by_image_source(tmp_path):
    """require_prefix / require_not_prefix で画像の出典による出し分けができる。

    gimukyoiku_card の「Commons実写の行だけ写真レイアウト、生成イメージの行は
    文字だけ」のための条件。列が空の単語は require_prefix 不一致・
    require_not_prefix 一致として扱う。
    """
    p = tmp_path / "prefix.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "text", "text": "写真:{original}", "box": [0.1, 0.1, 0.8, 0.1],
             "require_prefix": {"image": "http://commons"}},
            {"type": "text", "text": "文字:{original}", "box": [0.1, 0.3, 0.8, 0.1],
             "require_not_prefix": {"image": "http://commons"}},
        ],
    }), encoding="utf-8")
    layout = load_layout(str(p))
    commons = {"original": "X", "image": "http://commons.wikimedia.org/wiki/Special:FilePath/x.jpg"}
    generated = {"original": "X", "image": "https://github.com/soramimic/soramimic-wordlists/releases/download/gimukyoiku-image-v1/gk_x.jpg"}
    empty = {"original": "X"}
    assert layout.render_texts(commons) == ["写真:X", ""]
    assert layout.render_texts(generated) == ["", "文字:X"]
    assert layout.render_texts(empty) == ["", "文字:X"]


def test_require_prefix_rejects_non_dict(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "elements": [
            {"type": "text", "text": "x", "box": [0.1, 0.1, 0.8, 0.1],
             "require_prefix": "image"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_layout(str(p))
