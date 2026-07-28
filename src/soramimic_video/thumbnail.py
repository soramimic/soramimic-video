"""サムネ画像(thumbnail.png)の生成。

曲名をそのジョブと同じ単語リスト・パラメータで空耳変換し、言い換え単語を
大きく載せた動画と同じアスペクト(既定1280x720)のPNGを作る。

既定のスタイル(STYLE_FULLBLEED)は単語の画像を全面に敷き、その上に見出しと
説明文を載せる。文字は必ず輪郭(stroke)+半透明の帯の上に置くので、背景画像が
明るくても暗くても読める。

    ┌─────────────────────────────┐
    │▓▓▓▓▓【ダイオージャ】▓▓▓▓▓▓▓▓▓▓│  ← 背景は単語の画像(1語なら1枚、
    │▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓│     2語なら左右に2枚)
    │▓▓▓ lemon を 架空のアニメキャラ で歌ってみた ▓│
    │ 撮影者 (CC BY)      lyrics by Soramimic │
    └─────────────────────────────┘

言い換えは1語だけだと意味が取りにくいことがあるので、短い語のときは2語目まで
使う(pick_headline_words)。画像も語ごとに1枚ずつ敷く。
旧スタイル(STYLE_SIDE: 左に【単語】、右に小さく画像)も比較用に残してある。

描画は layout.py のレイアウト機構(フォント解決・テキスト収め込み・帯・輪郭)を
そのまま使うので、見た目のトーンは動画のカードレイアウトと揃う。
変換に失敗した・曲名が無い・言い換え単語が取れないときは、言い換えなしの
「<曲名> を <リスト名> で歌ってみた」だけのサムネにフォールバックする
(サムネ生成の失敗で動画生成そのものは止めない)。

生成物は make_video から呼ばれてプロジェクト(ジョブ)ディレクトリ直下に
thumbnail.png として置かれ、動画の前奏区間にも表示される(video.py 参照)。
生成前のプレビュー(おまかせモーダル)も同じ描画を使う(thumbnail_preview.py)。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance

from . import runproc
from .convert import (
    _find_row,
    _load_wordlist_rows,
    resolve_convert_settings,
    resolve_wordlist,
)
from .editor_io import wordlist_phrase_name
from .layout import APP_CREDIT, Layout, fitted_image_box, parse_layout, render_image
from .project import Project
from .soramimic_engine import run_convert

logger = logging.getLogger(__name__)

THUMBNAIL_FILENAME = "thumbnail.png"
# 動画本編の左下に焼き込むものと同じ署名(layout.APP_CREDIT)。
# サムネは署名を自前で配置する({app_credit})ので、レイアウト側の自動追加はされない
SIGNATURE = APP_CREDIT
# 単語画像を貼る枠(フレーム比率)。旧スタイル(STYLE_SIDE)専用
IMAGE_BOX = (0.53, 0.08, 0.41, 0.55)

# image_wait_sec の待ちで同時に落とす本数(見出しは高々2語なので少なくてよい)
IMAGE_WAIT_WORKERS = 4

STYLE_FULLBLEED = "fullbleed"  # 画像を全面に敷き、その上に文字を載せる(既定)
STYLE_SIDE = "side"  # 旧: 左に【単語】、右に画像
DEFAULT_STYLE = STYLE_FULLBLEED

# 見出しに使う言い換え単語の最大数と、「1語で済ませてよい」最小文字数。
# 「モノカ」のような短い1語だけでは意味が取りにくいので、その場合は2語目を足す。
HEADLINE_MAX_WORDS = 4  # 見出しに並べる言い換え単語の上限(長い曲名の保険)
# 背景に敷いた画像の明るさ(1.0=そのまま)。文字側の帯・輪郭と合わせて可読性を作る
BACKGROUND_DIM = 0.62

# 隅の署名と「<曲名> を <リスト名> で歌ってみた」は3パターン共通
_CAPTION_ELEMENT = {
    "type": "text", "text": "{caption}", "box": [0.05, 0.70, 0.90, 0.15],
    "size": 0.075, "color": "white", "wrap": True,
}
_SIGNATURE_ELEMENT = {
    "type": "text", "text": "{app_credit}", "box": [0.04, 0.89, 0.92, 0.05],
    "size": 0.032, "color": "#b8b8b8", "align": "right", "valign": "bottom",
}
# 全面スタイルの下段。背景画像の明暗に関わらず読めるよう、半透明の帯を必ず敷く
_FULLBLEED_CAPTION = {
    "type": "text", "text": "{caption}", "box": [0.05, 0.63, 0.90, 0.17],
    "size": 0.075, "color": "white", "wrap": True,
    "stroke_width": 0.003, "background": "#000000b3",
}
_FULLBLEED_CREDIT = {
    # 画像クレジット(表記不要な画像では空文字になり描かれない)
    "type": "text", "text": "{image_credit}", "box": [0.03, 0.895, 0.50, 0.055],
    "size": 0.024, "color": "#e8e8e8", "align": "left", "valign": "bottom",
    "background": "#000000a6",
}
_FULLBLEED_SIGNATURE = {
    "type": "text", "text": "{app_credit}", "box": [0.55, 0.895, 0.42, 0.055],
    "size": 0.032, "color": "#e8e8e8", "align": "right", "valign": "bottom",
    "background": "#000000a6",
}


def thumbnail_layout_spec(
    has_word: bool,
    has_image: bool,
    credit_box: tuple[float, float, float, float] | None = None,
    style: str = DEFAULT_STYLE,
) -> dict:
    """サムネのレイアウト定義(layout.py と同じ書式のdict)。

    STYLE_FULLBLEED(既定)では背景画像を呼び出し側が合成して渡すので、この
    定義に image 要素は出てこない(文字だけ)。文字は輪郭+半透明の帯を敷いて
    背景の明暗に依存せず読めるようにする。

    STYLE_SIDE(旧)は
    - 言い換え単語+画像: 左に【単語】、右に画像(クレジットは画像の右下)
    - 言い換え単語のみ: 中央に大きく【単語】
    - どちらも無い: 「<曲名> を <リスト名> で歌ってみた」だけ(フォールバック)

    見出し(【単語】)は折り返さず、長い単語ではフォントを縮めて1行に収める
    (「】」だけが次の行に落ちるのを避けるため)。credit_box を渡すと
    クレジットの帯をその枠(=実際に貼られた画像の領域)の右下に置く(STYLE_SIDE)。
    """
    if style == STYLE_FULLBLEED:
        return _fullbleed_spec(has_word, has_image)
    if not has_word:
        return {
            "background": "black",
            "elements": [
                {"type": "text", "text": "{caption}", "box": [0.06, 0.28, 0.88, 0.34],
                 "size": 0.11, "color": "white", "wrap": True},
                _SIGNATURE_ELEMENT,
            ],
        }
    if has_image:
        return {
            "background": "black",
            "elements": [
                {"type": "image", "box": list(IMAGE_BOX)},
                {"type": "text", "text": "{headline}", "box": [0.04, 0.10, 0.45, 0.51],
                 "size": 0.15, "color": "white", "stroke_width": 0.004},
                # 画像クレジット(表記不要な画像では空文字になり描かれない)
                {"type": "text", "text": "{image_credit}", "size": 0.024,
                 "box": list(credit_box or IMAGE_BOX),
                 "color": "#dddddd", "align": "right", "valign": "bottom",
                 "background": "#00000080"},
                _CAPTION_ELEMENT,
                _SIGNATURE_ELEMENT,
            ],
        }
    return {
        "background": "black",
        "elements": [
            {"type": "text", "text": "{headline}", "box": [0.06, 0.12, 0.88, 0.45],
             "size": 0.2, "color": "white", "stroke_width": 0.004},
            _CAPTION_ELEMENT,
            _SIGNATURE_ELEMENT,
        ],
    }


def _fullbleed_spec(has_word: bool, has_image: bool) -> dict:
    """全面スタイルの定義。背景(単色 or 合成済み画像)の上に文字だけを置く。"""
    if not has_word:
        return {
            "background": "black",
            "elements": [
                {"type": "text", "text": "{caption}", "box": [0.06, 0.28, 0.88, 0.34],
                 "size": 0.11, "color": "white", "wrap": True,
                 "stroke_width": 0.004,
                 **({"background": "#000000b3"} if has_image else {})},
                _FULLBLEED_CREDIT,
                _FULLBLEED_SIGNATURE,
            ],
        }
    return {
        "background": "black",
        "elements": [
            {"type": "text", "text": "{headline}", "box": [0.05, 0.10, 0.90, 0.38],
             "size": 0.19, "color": "white", "stroke_width": 0.007,
             **({"background": "#0000008c"} if has_image else {})},
            _FULLBLEED_CAPTION if has_image else _CAPTION_ELEMENT,
            _FULLBLEED_CREDIT,
            _FULLBLEED_SIGNATURE,
        ],
    }


def thumbnail_layout(
    has_word: bool,
    has_image: bool,
    credit_box: tuple[float, float, float, float] | None = None,
    style: str = DEFAULT_STYLE,
) -> Layout:
    return parse_layout(
        thumbnail_layout_spec(has_word, has_image, credit_box, style), "<thumbnail>"
    )


def thumbnail_data(
    title: str,
    wordlist_text: str,
    word: str | Sequence[str] = "",
    image_credit: str | Sequence[str] = "",
    app_credit: str = "",
) -> dict:
    """サムネのテンプレートに渡す値(headline / caption / image_credit / app_credit)。

    word は1語でも複数語でもよく、複数なら【】の中に空白区切りで並べる
    (「【モノカ 加藤】」)。クレジットも複数渡せる(重複は畳んで「 / 」で連結)。
    曲名・単語リスト名のどちらかが取れないときは、その部分を省いた文にする
    (「 を  で歌ってみた」のような空欄が残らないように)。
    """
    words = [word] if isinstance(word, str) else list(word)
    headline = " ".join(w for w in words if w)
    credits = [image_credit] if isinstance(image_credit, str) else list(image_credit)
    # 同じ画像・同じ撮影者のときに同じ文言が2つ並ばないよう、順序を保って重複を消す
    credit_text = " / ".join(dict.fromkeys(c for c in credits if c))
    if title and wordlist_text:
        caption = f"{title} を {wordlist_text} で歌ってみた"
    elif title:
        caption = f"{title} を歌ってみた"
    elif wordlist_text:
        caption = f"{wordlist_text} で歌ってみた"
    else:
        caption = ""
    return {
        "headline": f"【{headline}】" if headline else "",
        "caption": caption,
        "image_credit": credit_text,
        # 動画本編の隅と同じ署名(歌声合成のクレジットが要るときは連結済みで渡る)
        "app_credit": app_credit or SIGNATURE,
    }


def pick_headline_words(words: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """見出しに使う言い換え単語(曲名全体の言い換え)。

    変換結果は曲名を最後まで覆う単語列なので、先頭だけ採ると
    「春が来た → ダブラン」のように曲名の一部しか言い換えていない
    見出しになる。全部並べるのが正しい。長すぎる曲名の保険としてのみ
    HEADLINE_MAX_WORDS で切る。
    """
    return [w for w in words if str(w.get("surface") or "")][:HEADLINE_MAX_WORDS]


def _cover(img: Image.Image, width: int, height: int) -> Image.Image:
    """枠を埋めるように拡大して中央で切り出す(CSSの object-fit: cover)。"""
    scale = max(width / img.width, height / img.height)
    resized = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def compose_background(
    image_paths: Sequence[Path], width: int, height: int, dim: float = BACKGROUND_DIM
) -> Image.Image | None:
    """単語画像を全面に敷いた背景を作る(2枚なら左右に等分)。1枚も読めなければ None。

    文字を載せるので、そのままだと明るい写真で白文字が飛ぶ。全体を dim 倍に
    落としたうえで、文字側にも輪郭と半透明の帯を敷いて可読性を担保する。
    """
    images: list[Image.Image] = []
    for path in image_paths:
        try:
            with Image.open(path) as img:
                images.append(img.convert("RGB"))
        except Exception as e:  # noqa: BLE001 - 読めない画像は無いものとして続ける
            logger.warning("サムネ背景に使えない画像です: %s (%s)", path, e)
    if not images:
        return None
    canvas = Image.new("RGB", (width, height), "black")
    slot_w = width // len(images)
    for i, source in enumerate(images):
        # 最後の枠は端数ぶんまで受け持つ(1pxの黒すじを残さない)
        w = width - slot_w * i if i == len(images) - 1 else slot_w
        canvas.paste(_cover(source, w, height), (slot_w * i, 0))
    return ImageEnhance.Brightness(canvas).enhance(dim)


def song_title(project: Project, fallback: str | None = None) -> str:
    """サムネ・キャプションに出す曲名。

    APIジョブでは project の midi_path が固定名(input.mid)になるので、
    アップロード時のファイル名(job.params の midi_filename)を fallback として
    渡してもらい、そちらを優先する。どちらも無ければ空文字。
    """
    for candidate in (fallback, project.song.midi_path):
        if candidate and candidate.strip():
            name = candidate.strip().rsplit("/", 1)[-1]
            # 拡張子だけを落とす(「Mr.Children」のような曲名を切らないよう短い
            # 英数字の末尾に限る)
            stem = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name).strip()
            if stem:
                return stem
    return ""


def title_paraphrase(
    title: str,
    wordlist: str,
    where: str | None,
    params: dict[str, Any] | None,
) -> list[tuple[dict[str, Any], dict[str, str] | None]]:
    """曲名を1フレーズだけ空耳変換し、見出しに使う (単語, 単語リスト行) を返す。

    変換はジョブ本体(convert.py)と同じ経路・同じ単語リスト・同じパラメータで
    行うので、動画に出てくる単語と同じ雰囲気の言い換えになる。
    採るのは先頭1語、それが短ければ2語(pick_headline_words)。
    変換できる単語が無ければ空リスト。
    """
    csv_path = resolve_wordlist(wordlist)
    # ジョブ経由では解決済みのparamsが来るが、直接呼び(プレビュー等)では素の
    # 辞書が来る。エンジン既定(VARIATION_COST=0等)のままだと音の近さより
    # 変形の自由度が勝ってしまうので、本編と同じ既定解決を必ず通す
    eff_where, coerced, _alpha = resolve_convert_settings(csv_path, where, params)
    result = run_convert([title], csv_path, eff_where, coerced)
    lines = result.get("lines") or []
    words = lines[0].get("words") if lines else []
    picked = pick_headline_words(words or [])
    if not picked:
        return []
    rows = _load_wordlist_rows(csv_path)
    return [(w, _find_row(rows, w)) for w in picked]


def render_thumbnail(
    out_path: Path,
    title: str,
    wordlist_text: str,
    words: str | Sequence[str] = "",
    image_paths: Path | Sequence[Path] | None = None,
    image_credits: str | Sequence[str] = "",
    width: int = 1280,
    height: int = 720,
    style: str = DEFAULT_STYLE,
    app_credit: str = "",
) -> Path:
    """サムネPNGを描いて out_path に保存する。

    words / image_paths / image_credits は1件でも複数(言い換え2語)でもよい。
    STYLE_FULLBLEED では画像を全面に敷いた背景を先に合成し、その上に文字を描く。
    app_credit は隅の署名(既定は「lyrics by Soramimic」。動画本編と同じ文言)。
    """
    if isinstance(image_paths, Path):
        images = [image_paths]
    else:
        images = list(image_paths or [])
    data = thumbnail_data(title, wordlist_text, words, image_credits, app_credit)
    has_word = bool(data["headline"])

    if style == STYLE_FULLBLEED:
        background = compose_background(images, width, height)
        layout = thumbnail_layout(has_word, background is not None, style=style)
        canvas = render_image(
            layout, None, data, width, height, background=background
        )
    else:
        image_path = images[0] if images else None
        credit_box = None
        if image_path is not None and data["image_credit"]:
            # クレジットの帯は枠ではなく実際に貼られた画像の右下に載せる(動画と同じ)
            credit_box = fitted_image_box(image_path, IMAGE_BOX, width, height)
        layout = thumbnail_layout(has_word, image_path is not None, credit_box, style)
        canvas = render_image(layout, image_path, data, width, height)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path


def _word_image(
    row: dict[str, str] | None,
    cache: Path,
    download: bool = True,
    missing: list[tuple[str, str]] | None = None,
) -> tuple[Path | None, str]:
    """単語リスト行の image 列から画像とクレジット文言を取る(取れなければ空)。

    download=False ならネットワークを使わず、キャッシュ済みの画像・クレジットだけを
    使う(待てないプレビュー生成向け。無ければ画像なしのサムネになる)。
    その場合、キャッシュに無かった (画像URL, 画像ページ) を missing に積む
    (呼び出し側が後で先読みできるように)。
    """
    from .image_credit import commons_file_title, fetch_image_credit
    from .video import cached_image, download_image

    url = (row or {}).get("image") or ""
    if not url:
        return None, ""
    page = str((row or {}).get("image_page") or "")
    path = download_image(url, cache) if download else cached_image(url, cache)
    if path is None:
        if missing is not None:
            missing.append((url, page))
        return None, ""
    credit = str((row or {}).get("image_credit") or "").strip()
    if not credit:
        info = fetch_image_credit(url, page, cache, cached_only=not download)
        if (
            info is None
            and not download
            and missing is not None
            # Commons以外(ローカル・生成カード画像)はそもそも表記の取得先が無いので
            # 「未取得」に数えない(いつまでも取得待ち扱いになってしまう)
            and commons_file_title(url, page) is not None
        ):
            # 画像はあるがクレジットが未取得。表記付きで出せるよう次回までに温める
            missing.append((url, page))
        credit = info["credit_text"] if info else ""
    return path, credit


def wait_for_images(
    rows: Sequence[dict[str, str] | None], cache: Path, budget_sec: float
) -> None:
    """使う単語画像を「合計 budget_sec 秒まで」待ってダウンロードする。

    プレビュー(download_images=False)で、初見の1回目から絵入りを返すための
    短い待ち。間に合わなかったスレッドは止めずに走らせたままにするので、
    そのぶんはそのまま裏読みとしてキャッシュに入る。
    """
    from .video import cached_image, download_image

    if budget_sec <= 0:
        return
    urls = [
        url
        for url in dict.fromkeys(str((row or {}).get("image") or "") for row in rows)
        if url and cached_image(url, cache) is None
    ]
    if not urls:
        return
    started = time.monotonic()
    ex = ThreadPoolExecutor(
        max_workers=min(IMAGE_WAIT_WORKERS, len(urls)), thread_name_prefix="thumb-image"
    )
    try:
        futures = [ex.submit(download_image, url, cache) for url in urls]
        wait(futures, timeout=budget_sec)
    finally:
        # 期限切れのぶんは待たない(走り続けたスレッドの結果はキャッシュに残る)
        ex.shutdown(wait=False)
    logger.info(
        "サムネ用の画像を%d件だけ待ちました(%.1f秒/上限%.1f秒)",
        sum(1 for url in urls if cached_image(url, cache) is not None),
        time.monotonic() - started,
        budget_sec,
    )


def wordlist_text_of(wordlist: str) -> str:
    """サムネのキャプションに出す単語リストの表示名(解決できなければ空)。"""
    stem = wordlist
    try:
        stem = resolve_wordlist(wordlist).stem if wordlist else ""
    except FileNotFoundError:
        logger.warning("単語リストが見つかりません(表示名はそのまま使います): %s", wordlist)
    return wordlist_phrase_name(stem) if stem else ""


def build_thumbnail(
    out_path: Path,
    song: str,
    wordlist: str,
    where: str | None = None,
    params: dict[str, Any] | None = None,
    image_cache: Path | None = None,
    width: int = 1280,
    height: int = 720,
    download_images: bool = True,
    style: str = DEFAULT_STYLE,
    missing_images: list[tuple[str, str]] | None = None,
    app_credit: str = "",
    image_wait_sec: float = 0.0,
    song_kana: str = "",
) -> Path | None:
    """曲名を1フレーズ変換してサムネPNGを out_path に作る(サムネ生成の本体)。

    ジョブのサムネ(generate_thumbnail)と、生成前のプレビュー
    (thumbnail_preview.py)が共有する。変換・画像取得が失敗しても
    言い換えなし・画像なしのサムネにフォールバックし、描画自体に失敗した
    ときだけ None を返す(いずれも警告ログのみ)。中断要求(Cancelled)は伝播する。
    missing_images を渡すと、使いたかったのにキャッシュに無かった画像・クレジットの
    (URL, 画像ページ)が積まれる(download_images=False のときだけ起きる)。
    image_wait_sec を渡すと、download_images=False でも「合計その秒数まで」は
    画像のダウンロードを待つ(プレビューの1回目から絵を出すため)。
    song_kana(曲名の読み・カタカナ)があれば、変換の入力にはそちらを使う
    (「紅葉」→ MeCab推定の「コーヨー」ではなく「モミジ」で変換したいときに使う。
    サンプル曲は samples.json の title_kana から来る)。キャプションに出す
    曲名は読みの有無にかかわらず song(漢字まじりの表記)のまま。
    """
    wordlist_text = wordlist_text_of(wordlist)
    convert_input = song_kana.strip() or song

    found: list[tuple[dict[str, Any], dict[str, str] | None]] = []
    if convert_input and wordlist:
        try:
            found = title_paraphrase(convert_input, wordlist, where, params)
        except runproc.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - サムネの失敗でジョブを落とさない
            logger.warning("曲名の空耳変換に失敗しました(言い換えなしのサムネにします): %s", e)
    words = [str(word.get("surface") or "") for word, _row in found]

    image_paths: list[Path] = []
    image_credits: list[str] = []
    if found and image_cache is not None:
        try:
            if not download_images and image_wait_sec > 0:
                # 待てないなりに少しだけ待つ(初見の1回目から絵入りにするため)
                wait_for_images([row for _word, row in found], image_cache, image_wait_sec)
            for _word, row in found:
                path, credit = _word_image(
                    row, image_cache, download_images, missing_images
                )
                if path is not None:
                    image_paths.append(path)
                    image_credits.append(credit)
        except runproc.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 画像なしのサムネにフォールバック
            logger.warning("サムネ用の画像を取得できませんでした: %s", e)

    try:
        path = render_thumbnail(
            out_path,
            song,
            wordlist_text,
            words=words,
            image_paths=image_paths,
            image_credits=image_credits,
            width=width,
            height=height,
            style=style,
            app_credit=app_credit,
        )
    except Exception as e:  # noqa: BLE001 - 描画失敗もジョブは落とさない
        logger.warning("サムネ画像を生成できませんでした: %s", e)
        return None
    logger.info("サムネ画像を生成しました: %s", path)
    return path


def generate_thumbnail(
    project: Project,
    project_dir: Path,
    width: int = 1280,
    height: int = 720,
    image_cache: Path | None = None,
    title: str | None = None,
    app_credit: str = "",
    title_kana: str = "",
) -> Path | None:
    """曲名の空耳変換つきサムネPNGを project_dir/thumbnail.png に作る。

    変換条件(単語リスト・where・パラメータ)はジョブ本体の変換と同じものを
    project.parody から取る。失敗時の扱いは build_thumbnail と同じ。
    title_kana(曲名の読み)があれば変換の入力にそちらを使う(build_thumbnail 参照)。
    """
    from .video import VIDEO_DIR, image_cache_dir

    return build_thumbnail(
        project_dir / THUMBNAIL_FILENAME,
        song_title(project, title),
        project.parody.wordlist if project.parody else "",
        where=project.parody.where if project.parody else None,
        params=project.parody.params if project.parody else None,
        image_cache=image_cache_dir(project_dir / VIDEO_DIR, image_cache),
        width=width,
        height=height,
        app_credit=app_credit,
        song_kana=title_kana,
    )
