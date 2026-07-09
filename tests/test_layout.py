"""layout.py のテスト: レイアウト読み込みとPillowでのフレーム合成。"""

import json

import pytest
from PIL import Image

from soramimic_video.layout import load_layout, render_frame


def test_load_builtin_layouts():
    default = load_layout(None)
    assert len(default.elements) == 1
    caption = load_layout("caption")
    assert len(caption.elements) == 2


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
