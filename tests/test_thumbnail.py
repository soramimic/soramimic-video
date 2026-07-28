"""サムネ画像(thumbnail.py)のテスト。

変換エンジン(run_convert)と画像取得はモックし、ネットワーク無しで
レイアウトの文言・画像なしフォールバック・失敗時の挙動を確認する。
"""

from __future__ import annotations

from pathlib import Path

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


def test_pick_headline_uses_one_long_word():
    words = [{"surface": "ダイオージャ"}, {"surface": "加藤"}]
    assert [w["surface"] for w in pick_headline_words(words)] == ["ダイオージャ"]


def test_pick_headline_adds_second_word_when_first_is_short():
    words = [{"surface": "モノカ"}, {"surface": "加藤"}, {"surface": "鈴木"}]
    # 短い1語だけでは意味が取りにくいので2語目まで。3語目は足さない
    assert [w["surface"] for w in pick_headline_words(words)] == ["モノカ", "加藤"]


def test_pick_headline_handles_missing_and_empty():
    assert pick_headline_words([]) == []
    assert pick_headline_words([{"surface": ""}]) == []
    # 短い語しか無ければ1語のまま
    assert len(pick_headline_words([{"surface": "モノ"}])) == 1


# ---- レイアウト・文言 ----


def test_layout_texts_with_word():
    layout = thumbnail_layout(has_word=True, has_image=False)
    data = thumbnail_data("夜に駆ける", "駅名", word="米原")
    texts = [t for t in layout.render_texts(data) if t]
    assert "【米原】" in texts
    assert "夜に駆ける を 駅名 で歌ってみた" in texts
    assert SIGNATURE in texts


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


def test_fullbleed_texts_have_band_or_stroke():
    """背景画像の明暗に依存しないよう、全面スタイルの文字は帯か輪郭を必ず持つ。"""
    layout = thumbnail_layout(has_word=True, has_image=True)
    from soramimic_video.layout import TextElement

    texts = [e for e in layout.elements if isinstance(e, TextElement)]
    assert texts, "文字要素が無い"
    for el in texts:
        assert el.background or el.stroke_width > 0, el.template


def test_layout_image_credit_hidden_when_empty():
    layout = thumbnail_layout(has_word=True, has_image=True, style=STYLE_SIDE)
    texts = layout.render_texts(thumbnail_data("曲", "駅名", word="米原"))
    assert "" in texts  # クレジット文言が空の画像では帯を描かない
    texts_with = layout.render_texts(
        thumbnail_data("曲", "駅名", word="米原", image_credit="山田太郎 (CC BY)")
    )
    assert "山田太郎 (CC BY)" in texts_with


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
    assert compose_background([], 320, 180) is None
    assert compose_background([tmp_path / "missing.png"], 320, 180) is None


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
