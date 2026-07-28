"""サムネの文字可読性デザインを、条件の違う実画像で並べた1枚の比較シートにする。

thumbnail.TEXT_DESIGNS の各案(現行の二重縁取り + 比較中の案)を列に、背景写真の
条件(暗い / 明るい / 白っぽい / 細かい / 上下で明暗が違う)を行にして1枚のPNGに
並べる。どのマスも本番と同じ経路(resolve_headline → render_thumbnail)で描くので、
見出しの文字列は実際の空耳変換の結果、背景はその単語の実画像そのままになる。
比較シートにダミーの見出しや無関係な写真を置くと「変換品質が悪い」という誤解の
もとになるので、絶対に手書きの文言を混ぜないこと。

行(条件)は COMPARISON_ROWS の (説明, 曲名, 単語リスト)。条件を選び直したいときは
実際に変換して背景の輝度・細かさを測り、その写真に対応する実在の単語で差し替える。

使い方:
    uv run python scripts/compare_thumbnail_designs.py --out /tmp/compare.png
    # 単語リストが submodule に無いworktreeでは置き場所を指定する
    uv run python scripts/compare_thumbnail_designs.py --out /tmp/compare.png \
        --wordlists ../soramimic-wordlists --image-cache work/api-jobs/image-cache
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image, ImageDraw

from soramimic_video import thumbnail as thumb
from soramimic_video.layout import _font, resolve_font_path

# (条件, 実測, 曲名, 単語リスト)。実測は暗転前の背景の輝度(中央値・上下半分)と
# エッジ量で、この組み合わせで実際に出てくる写真を測った値
COMPARISON_ROWS: list[tuple[str, str, str, str]] = [
    ("暗い写真", "輝度22", "しゃぼん玉", "youtuber"),
    ("明るい写真", "輝度177", "赤とんぼ", "fictional_anime_character"),
    ("白っぽい写真", "輝度255", "村祭", "scientist"),
    ("細かい模様", "エッジ52", "茶摘", "insect"),
    ("上下で明暗が違う", "上180 / 下53", "桃太郎", "plant"),
    ("平坦な絵で左右に明暗差", "上255 / 下92", "冬景色", "nations"),
]

# 列に並べるデザイン(見出しの文言つき)
COMPARISON_COLUMNS: list[tuple[str, str]] = [
    (thumb.DESIGN_DOUBLE_OUTLINE, "現行: 二重縁取り+影"),
    (thumb.DESIGN_SCRIM, "案1: 上下グラデ+細縁"),
    (thumb.DESIGN_SOFT_SHADOW, "案2: 細縁+広く薄い影"),
    (thumb.DESIGN_ADAPTIVE, "案3: 背景で白黒反転"),
    (thumb.DESIGN_SCRIM_ADAPTIVE, "案1+3"),
    (thumb.DESIGN_SOFT_ADAPTIVE, "案2+3"),
]

CELL_W = 440  # 1マスのサムネ幅(16:9)
LABEL_W = 310  # 左の条件ラベルの幅
HEAD_H = 52  # 列見出しの帯
TITLE_H = 82  # シート全体の見出し
GAP = 10
BG = (24, 24, 26)
FG = (240, 240, 240)
SUB = (170, 170, 176)


def _text(
    draw: ImageDraw.ImageDraw, xy, s: str, px: int, fill=FG, anchor=None, max_w=0
) -> None:
    font = _font(resolve_font_path(None), px)
    while max_w and len(s) > 1 and draw.textlength(s, font=font) > max_w:
        s = s[:-2] + "…"  # ラベル欄からはみ出す行は末尾を省く
    draw.text(xy, s, font=font, fill=fill, anchor=anchor)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("thumbnail-design-comparison.png"))
    p.add_argument("--wordlists", type=Path, help="単語リストCSVの置き場所")
    p.add_argument("--setting-json", type=Path, help="単語リスト表示名(conf/setting.json)")
    p.add_argument("--image-cache", type=Path, help="単語画像のキャッシュ先")
    p.add_argument("--no-download", action="store_true", help="画像を取りに行かない")
    p.add_argument("--cells", type=Path, help="マスごとのサムネPNGの置き場所")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.wordlists:
        from soramimic_video import convert

        convert.WORDLISTS_DIR = args.wordlists.resolve()
    if args.setting_json:
        from soramimic_video import editor_io

        editor_io.SETTING_JSON = args.setting_json.resolve()
    cells = (args.cells or args.out.parent / "thumbnail-design-cells").resolve()
    image_cache = (args.image_cache or cells / "image-cache").resolve()
    cells.mkdir(parents=True, exist_ok=True)

    cell_h = round(CELL_W * 9 / 16)
    width = LABEL_W + len(COMPARISON_COLUMNS) * (CELL_W + GAP) + GAP
    height = TITLE_H + HEAD_H + len(COMPARISON_ROWS) * (cell_h + GAP) + GAP
    sheet = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(sheet)
    _text(draw, (GAP + 6, 14), "サムネ文字の可読性: 実変換・実画像での比較", 30)
    _text(
        draw, (GAP + 6, 50),
        "見出しは実際の空耳変換の結果、背景はその単語の実画像(手書きのダミー文言は無し)",
        18, SUB,
    )
    for col, (_design, label) in enumerate(COMPARISON_COLUMNS):
        x = LABEL_W + col * (CELL_W + GAP)
        _text(draw, (x + CELL_W // 2, TITLE_H + HEAD_H // 2), label, 24, FG, "mm")

    for row, (condition, measured, title, wordlist) in enumerate(COMPARISON_ROWS):
        y = TITLE_H + HEAD_H + row * (cell_h + GAP)
        # 変換と画像取得は行に1回。同じ単語・同じ写真の上で案だけを差し替える
        words, image_paths, image_credits = thumb.resolve_headline(
            title, wordlist, image_cache=image_cache,
            download_images=not args.no_download,
        )
        wordlist_text = thumb.wordlist_text_of(wordlist)
        for i, line in enumerate([
            f"{title} × {wordlist_text}",
            f"変換結果: {' '.join(words) or '(なし)'}",
            f"実画像 {len(image_paths)}枚 / {measured}",
        ]):
            _text(draw, (GAP + 6, y + 46 + i * 26), line, 18, SUB, max_w=LABEL_W - GAP - 16)
        _text(draw, (GAP + 6, y + 10), condition, 24, max_w=LABEL_W - GAP - 16)
        for col, (design, _label) in enumerate(COMPARISON_COLUMNS):
            x = LABEL_W + col * (CELL_W + GAP)
            out = cells / f"{row}_{design}.png"
            thumb.render_thumbnail(
                out, title, wordlist_text, words=words, image_paths=image_paths,
                image_credits=image_credits, width=1280, height=720, design=design,
            )
            with Image.open(out) as img:
                sheet.paste(img.resize((CELL_W, cell_h), Image.Resampling.LANCZOS), (x, y))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    print(args.out)


if __name__ == "__main__":
    main()
