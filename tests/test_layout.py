"""layout.py のテスト: レイアウト読み込みとPillowでのフレーム合成。"""

import json

import pytest
from PIL import Image

from soramimic_video import layout as layout_mod
from soramimic_video.layout import (
    ImageElement,
    builtin_layout_names,
    images_hidden_by_default,
    load_layout,
    load_wordlist_image_optout,
    load_wordlist_layouts,
    render_frame,
    render_idle_frame,
    without_images,
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


def test_render_idle_frame_absent_is_none(tmp_path):
    # idleセクションのないレイアウトでは None(呼び出し側は黒画面のまま)
    layout = load_layout("caption")
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


# ---- 画像を既定で隠す単語リスト(wordlist_image_optout.json) ----


def _optout_file(tmp_path, monkeypatch, mapping):
    p = tmp_path / "wordlist_image_optout.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setattr(layout_mod, "WORDLIST_IMAGE_OPTOUT_PATH", p)
    return p


def test_wordlist_image_optout_is_builtin():
    """同梱の設定は組み込みレイアウト名だけを指していること。"""
    conf = load_wordlist_image_optout()
    assert conf["insect"]["layout"] == "noimage_card"
    assert {c["layout"] for c in conf.values() if "layout" in c} <= set(builtin_layout_names())


def test_load_wordlist_image_optout_skips_unknown_layout(tmp_path, monkeypatch, caplog):
    _optout_file(tmp_path, monkeypatch, {
        "insect": {"reason": "苦手な人がいるため", "layout": "no-such-layout"},
        "spider": {"reason": "同上"},          # layout省略は理由だけ持つ
        "broken": "画像を隠す",                 # オブジェクトでないので捨てられる
    })
    with caplog.at_level("WARNING"):
        conf = load_wordlist_image_optout()
    assert conf == {
        "insect": {"reason": "苦手な人がいるため"},
        "spider": {"reason": "同上"},
    }
    assert "no-such-layout" in caplog.text


def test_load_wordlist_image_optout_missing_or_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(layout_mod, "WORDLIST_IMAGE_OPTOUT_PATH", tmp_path / "none.json")
    assert load_wordlist_image_optout() == {}
    broken = tmp_path / "broken.json"
    broken.write_text("[not json", encoding="utf-8")
    monkeypatch.setattr(layout_mod, "WORDLIST_IMAGE_OPTOUT_PATH", broken)
    assert load_wordlist_image_optout() == {}


def test_images_hidden_by_default_accepts_name_or_csv_path(tmp_path, monkeypatch):
    _optout_file(tmp_path, monkeypatch, {"insect": {"reason": "苦手な人がいるため"}})
    assert images_hidden_by_default("insect")
    assert images_hidden_by_default("/data/wordlists/insect.csv")
    assert not images_hidden_by_default("stations")
    assert not images_hidden_by_default("")
    assert not images_hidden_by_default(None)


def test_without_images_drops_image_elements_and_credit():
    layout = load_layout("animal_card")
    assert any(isinstance(el, ImageElement) for el in layout.elements)
    assert layout.credit is not None  # image要素があるとクレジットが自動で載る

    hidden = without_images(layout)
    assert hidden.hide_images
    assert not any(isinstance(el, ImageElement) for el in hidden.elements)
    assert hidden.credit is None
    # テキストと字幕はそのまま残る(単語名・分類・歌詞は出したい)
    assert len(hidden.elements) == len(layout.elements) - 1
    assert len(hidden.subtitles) == len(layout.subtitles)
    # 元のレイアウトは書き換えない
    assert any(isinstance(el, ImageElement) for el in layout.elements)


def test_without_images_frame_has_no_image(tmp_path):
    img = tmp_path / "word.png"
    Image.new("RGB", (300, 200), "red").save(img)
    layout = without_images(load_layout("animal_card"))
    data = {"surface": "ホシズム", "original": "アゲハチョウ", "order": "チョウ目"}
    out = render_frame(layout, img, data, 320, 180, tmp_path / "f")
    assert out is not None
    with Image.open(out) as frame:
        # image要素を外したので、画像枠(上半分)は背景のまま
        assert frame.getpixel((160, 60)) == (0, 0, 0)
