"""サムネのデザインを実データで見比べるためのサンプル生成。

実際の単語リストで曲名を空耳変換し、スタイル(全面 / 旧・右側)ごとにサムネPNGを
書き出す。1語・2語、画像あり・なし、明るい画像・暗い画像が並ぶように選べる。

使い方:
    uv run python scripts/render_thumbnail_samples.py --out /tmp/thumbs
    # 単語リストが submodule に無いworktreeでは置き場所を指定する
    uv run python scripts/render_thumbnail_samples.py --wordlists ../soramimic-wordlists

画像はキャッシュ(--image-cache)に落として使い回す。--no-download を付けると
キャッシュ済みのぶんだけで描く(プレビューと同じ「待たない」条件の確認用)。
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from soramimic_video import thumbnail as thumb

# (曲名, 単語リスト)。UIのおまかせ例示と、言い換えが短くなりやすい曲を混ぜてある
DEFAULT_COMBOS = [
    ("ふるさと", "baseball"),
    ("赤とんぼ", "stations"),
    ("桃太郎", "fictional_anime_character"),
    ("茶摘", "stations"),
    ("しゃぼん玉", "youtuber"),
    ("春が来た", "pokemon"),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("thumbnail-samples"))
    p.add_argument("--wordlists", type=Path, help="単語リストCSVの置き場所")
    p.add_argument("--setting-json", type=Path, help="単語リスト表示名(conf/setting.json)")
    p.add_argument("--image-cache", type=Path, help="単語画像のキャッシュ先")
    p.add_argument("--no-download", action="store_true", help="画像を取りに行かない")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument(
        "--styles", default=f"{thumb.STYLE_FULLBLEED},{thumb.STYLE_SIDE}",
        help="比較するスタイル(カンマ区切り)",
    )
    p.add_argument("--combo", action="append", help="曲名:単語リスト(複数可)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.wordlists:
        from soramimic_video import convert

        convert.WORDLISTS_DIR = args.wordlists.resolve()
    if args.setting_json:
        from soramimic_video import editor_io

        editor_io.SETTING_JSON = args.setting_json.resolve()
    image_cache = (args.image_cache or args.out / "image-cache").resolve()
    combos = (
        [tuple(c.split(":", 1)) for c in args.combo] if args.combo else DEFAULT_COMBOS
    )

    args.out.mkdir(parents=True, exist_ok=True)
    for style in [s.strip() for s in args.styles.split(",") if s.strip()]:
        for title, wordlist in combos:
            for with_image in (True, False):
                tag = "img" if with_image else "noimg"
                out = args.out / f"{style}_{wordlist}_{title}_{tag}.png"
                path = thumb.build_thumbnail(
                    out,
                    title,
                    wordlist,
                    image_cache=image_cache if with_image else None,
                    width=args.width,
                    height=args.height,
                    download_images=not args.no_download,
                    style=style,
                )
                print(path or f"失敗: {out}")


if __name__ == "__main__":
    main()
