"""サムネ画像(thumbnail.png)の生成。

曲名をそのジョブと同じ単語リスト・パラメータで空耳変換し、言い換え単語を
大きく載せた動画と同じアスペクト(既定1280x720)のPNGを作る。

既定のスタイル(STYLE_FULLBLEED)は単語の画像を全面に敷き、その上に見出しと
説明文を載せる。文字が背景写真に負けないようにする方法は「可読性デザイン」
(TextDesign / TEXT_DESIGNS)として差し替え可能にしてあり、TEXT_DESIGN を
書き換えるだけで全経路(本番サムネ・プレビュー・サンプル生成)に反映される。
どの案も黒帯は敷かない(写真が隠れるし見た目も重いため)。

    double_outline 現行 二重縁取り(白文字→黒環→白環)+ぼかし影
    scrim          案1  背景の上下を境界線なしに暗くする + 細い縁取りだけ
    soft_shadow    案2  細い縁取り + 広く薄いぼかし影で背景から浮かせる
    adaptive       案3  文字の裏の明るさを測り、明るければ黒文字+白縁に反転
    scrim_adaptive 案1+3 / soft_adaptive 案2+3

    ┌─────────────────────────────┐
    │▓▓▓▓▓【ダイオージャ】▓▓▓▓▓▓▓▓▓▓│  ← 背景は単語の画像(1語なら1枚、
    │▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓│     2語なら左右に2枚)
    │▓▓▓ lemon を 架空のアニメキャラ で歌ってみた ▓│
    │ 撮影者 (CC BY)      lyrics & video by Soramimic │
    └─────────────────────────────┘

言い換えは1語だけだと意味が取りにくいことがあるので、短い語のときは2語目まで
使う(pick_headline_words)。画像も語ごとに1枚ずつ敷く。
旧スタイル(STYLE_SIDE: 左に【単語】、右に小さく画像)も比較用に残してある。

描画は layout.py のレイアウト機構(フォント解決・テキスト収め込み・縁取り・影)を
そのまま使うので、見た目のトーンは動画のカードレイアウトと揃う。
変換に失敗した・曲名が無い・言い換え単語が取れないときは、言い換えなしの
「<曲名> を <リスト名> で歌ってみた」だけのサムネにフォールバックする
(サムネ生成の失敗で動画生成そのものは止めない)。

生成物は make_video から呼ばれてプロジェクト(ジョブ)ディレクトリ直下に
thumbnail.png として置かれ、動画の前奏区間にも表示される(video.py 参照)。
生成前のプレビュー(おまかせモーダル)も同じ描画を使う(thumbnail_preview.py)。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
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
from .layout import (
    APP_CREDIT,
    LAYOUTS_DIR,
    Layout,
    fitted_image_box,
    parse_layout,
    render_image,
)
from .project import Project
from .soramimic_engine import run_convert

logger = logging.getLogger(__name__)

THUMBNAIL_FILENAME = "thumbnail.png"
# 動画本編の左下に焼き込むものと同じ署名(layout.APP_CREDIT)。
# サムネは署名を自前で配置する({app_credit})ので、レイアウト側の自動追加はされない
SIGNATURE = APP_CREDIT
# サムネのレイアウト定義(文言・枠・文字サイズ)。UIのレイアウト選択には出さない
THUMBNAIL_LAYOUT_PATH = LAYOUTS_DIR / "thumbnail.json"

# image_wait_sec の待ちで同時に落とす本数(見出しは高々2語なので少なくてよい)
IMAGE_WAIT_WORKERS = 4

STYLE_FULLBLEED = "fullbleed"  # 画像を全面に敷き、その上に文字を載せる(既定)
STYLE_SIDE = "side"  # 旧: 左に【単語】、右に画像
DEFAULT_STYLE = STYLE_FULLBLEED

# 見出しに使う言い換え単語の最大数と、「1語で済ませてよい」最小文字数。
# 「モノカ」のような短い1語だけでは意味が取りにくいので、その場合は2語目を足す。
HEADLINE_MAX_WORDS = 4  # 見出しに並べる言い換え単語の上限(長い曲名の保険)

# キャプション(「<曲名> を <リスト名> で歌ってみた」)を自分で2行に折る文字数。
# 1行に入るのはフレーム幅の9割・既定サイズでおよそ20文字で、それを超えると
# 自動の折り返し(文字単位)に任せることになり「架空の日常アニ / メキャラ」の
# ように語の途中で切れるうえ、2行ぶんの高さを作るために文字も縮む。
# 意味の切れ目(「<曲名> を」/「<リスト名> で歌ってみた」)で先に折っておけば、
# 長い曲名×長いリスト名でも縮まずスマホの小ささでも読める
CAPTION_ONE_LINE_CHARS = 20

# 見出し・キャプションの文字サイズと枠は layouts/thumbnail.json にある
# (枠の決め方の意図はそのファイルの _comment を参照)

# ---- 文字の可読性デザイン(比較中の案を切り替えられるようにしてある) ----
#
# どの案も「べた塗りの矩形帯」は敷かない(写真が隠れるし見た目が重い)。
# 採用案が決まったら TEXT_DESIGN の1行を書き換えるだけで全経路
# (本番サムネ・プレビュー・サンプル生成)に反映される。
DESIGN_DOUBLE_OUTLINE = "double_outline"  # 現行: 二重縁取り(白文字→黒環→白環)+影
DESIGN_SCRIM = "scrim"  # 案1: 上下グラデーション + 細い縁取りだけ
DESIGN_SOFT_SHADOW = "soft_shadow"  # 案2: 細い縁取り + 広く薄いぼかし影
DESIGN_ADAPTIVE = "adaptive"  # 案3: 背景の明るさで白黒を反転
DESIGN_SCRIM_ADAPTIVE = "scrim_adaptive"  # 案1+案3
DESIGN_SOFT_ADAPTIVE = "soft_adaptive"  # 案2+案3

# 案3の判定。文字を置く領域の「暗いほう」(下位 DARK_PERCENTILE の明るさ)が
# BRIGHT_THRESHOLD を超えたときだけ黒文字+白縁に反転する。中央値ではなく
# 低いパーセンタイルを見るのは、左が空・右が黒のような枠で「明るい」と判断して
# 黒文字にしてしまうと、暗いほうで文字が消えるため(白文字が既定で安全側)
BRIGHT_THRESHOLD = 150.0
DARK_PERCENTILE = 0.25

# グラデーション(scrim)1枚ぶん: (辺, 濃さを保つ範囲, 消えるまでの範囲, 端での濃さ)。
# 範囲はフレーム高さ比率。hold までは端と同じ濃さで、そこから extent まで
# smoothstep で 0 に落とす。両端とも傾きがゼロになるので帯のような境界線が出ない
Scrim = tuple[str, float, float, float]
# 見出しは上、キャプションとクレジットは下にあるので上下の両方から掛ける。
# hold は文字が乗る範囲(見出し 0.10〜0.48 / キャプション 0.60〜0.83)を覆う長さにし、
# 上下の extent の合計をちょうど1.0にして中央で両者が0になるようにしてある
DEFAULT_SCRIMS: tuple[Scrim, ...] = (
    ("top", 0.38, 0.56, 0.60),
    ("bottom", 0.30, 0.44, 0.72),
)


@dataclass(frozen=True)
class TextDesign:
    """サムネの文字を背景写真に負けさせないための設定一式。

    縁取り・影の太さは「文字サイズに対する比率」で持つ(小さいクレジットだけ
    縁が相対的に太くならないように)。実寸はフレーム高さ比率に直して layout.py に渡す。
    """

    name: str
    # 背景写真の暗転(1.0=そのまま)。可読性を文字側で作るほど 1.0 に近づけられる
    background_dim: float = 0.82
    # 背景に掛ける上下グラデーション(空なら掛けない)
    scrim: tuple[Scrim, ...] = ()
    # 文字と同色の外側の環 / 文字と反対色の縁取り。太さ = 比率 × 文字サイズ
    ink_stroke: float = 0.0
    contrast_stroke: float = 0.03
    min_ink_stroke: float = 0.0042  # 小さい文字でも縁が消えない下限(フレーム比)
    min_contrast_stroke: float = 0.0022
    # 文字の形をぼかした影(矩形の帯ではない)。半径 = 比率 × 文字サイズ
    shadow: float = 0.05
    min_shadow: float = 0.003
    shadow_alpha: float = 0.70
    # True なら文字を置く領域の明るさを測って白黒を反転する(要素ごとに独立)
    adaptive: bool = False
    light_ink: str = "#ffffff"  # 暗い背景で使う文字色
    dark_ink: str = "#121212"  # 明るい背景で使う文字色(adaptive のときだけ出番がある)


TEXT_DESIGNS: dict[str, TextDesign] = {
    # 現行。白文字の外に黒い環、その外に白い環。明暗どちらの背景でもどれかの環が
    # 効く代わりに、縁が太くて文字自体の形が潰れやすい(今回の比較対象)
    DESIGN_DOUBLE_OUTLINE: TextDesign(
        name=DESIGN_DOUBLE_OUTLINE,
        background_dim=0.82,
        ink_stroke=0.085,
        contrast_stroke=0.055,
        min_ink_stroke=0.0042,
        min_contrast_stroke=0.0022,
        shadow=0.06,
        shadow_alpha=0.70,
    ),
    # 案1: 可読性は背景側のグラデーションで作り、文字には細い縁だけ残す。
    # 帯と違って境界線が出ないので、写真をほぼそのまま見せられる(暗転も弱くできる)
    DESIGN_SCRIM: TextDesign(
        name=DESIGN_SCRIM,
        background_dim=0.95,
        scrim=DEFAULT_SCRIMS,
        contrast_stroke=0.024,
        shadow=0.0,
    ),
    # 案2: 縁を現行の1/2以下に細くして文字の形を残し、代わりに影を広く薄く掛けて
    # 背景から浮かせる。写真は全面そのまま見える
    DESIGN_SOFT_SHADOW: TextDesign(
        name=DESIGN_SOFT_SHADOW,
        background_dim=0.88,
        contrast_stroke=0.028,
        shadow=0.13,
        min_shadow=0.004,
        shadow_alpha=0.55,
    ),
    # 案3: 文字を置く領域の明るさを測り、明るければ黒文字+白縁、暗ければ
    # 白文字+黒縁。要素ごとに独立して判定するので上下で明暗が違う写真にも効く
    DESIGN_ADAPTIVE: TextDesign(
        name=DESIGN_ADAPTIVE,
        background_dim=0.95,
        contrast_stroke=0.035,
        shadow=0.05,
        shadow_alpha=0.45,
        adaptive=True,
    ),
    # 案1+3: グラデーションを掛けたうえで、それでも明るい領域だけ黒文字に反転する
    DESIGN_SCRIM_ADAPTIVE: TextDesign(
        name=DESIGN_SCRIM_ADAPTIVE,
        background_dim=0.95,
        scrim=DEFAULT_SCRIMS,
        contrast_stroke=0.026,
        shadow=0.05,
        shadow_alpha=0.45,
        adaptive=True,
    ),
    # 案2+3: 広く薄い影の色も文字の反対色にする(黒文字のときは白い光背になる)
    DESIGN_SOFT_ADAPTIVE: TextDesign(
        name=DESIGN_SOFT_ADAPTIVE,
        # 反転する案は暗転を揃えておく(暗転が違うと同じ写真で反転の判定が割れる)
        background_dim=0.95,
        contrast_stroke=0.028,
        shadow=0.13,
        min_shadow=0.004,
        shadow_alpha=0.55,
        adaptive=True,
    ),
}

# ★ 採用案: スクリム(案1)。背景の上下を境界線なしに暗くするだけで、写真を
# ほぼそのまま見せつつ文字を読ませる。白黒反転の保険(adaptive)は付けない
TEXT_DESIGN = DESIGN_SCRIM

# 背景に敷いた画像の明るさ(1.0=そのまま)。案ごとに違うので既定案の値を公開する
BACKGROUND_DIM = TEXT_DESIGNS[TEXT_DESIGN].background_dim


def resolve_design(design: str | TextDesign | None = None) -> TextDesign:
    """デザイン名(またはそのもの)を TextDesign に解決する。未知の名前は既定に落とす。"""
    if isinstance(design, TextDesign):
        return design
    name = design or TEXT_DESIGN
    found = TEXT_DESIGNS.get(name)
    if found is None:
        logger.warning("知らないサムネデザインです(既定を使います): %s", name)
        return TEXT_DESIGNS[TEXT_DESIGN]
    return found


def _with_alpha(color: str, alpha: float) -> str:
    """"#rrggbb" に不透明度を足して "#rrggbbaa" にする。"""
    return f"{color}{round(min(1.0, max(0.0, alpha)) * 255):02x}"


def outline_style(
    size: float, design: str | TextDesign | None = None, dark_text: bool = False
) -> dict:
    """文字サイズに見合う縁取り+影の指定(text要素にマージして使う)。

    dark_text=True で文字と縁の白黒を入れ替える(案3の明るい背景側)。影も
    反対色にするので、黒文字のときは暗い影ではなく白い光背になる。
    """
    d = resolve_design(design)
    ink = d.dark_ink if dark_text else d.light_ink
    contrast = d.light_ink if dark_text else d.dark_ink
    strokes = []
    if d.ink_stroke > 0:
        # 文字と同色の外側の環。反対色の縁より太くして同心の環にする
        strokes.append(
            {"width": max(size * d.ink_stroke, d.min_ink_stroke), "color": ink}
        )
    if d.contrast_stroke > 0:
        strokes.append(
            {"width": max(size * d.contrast_stroke, d.min_contrast_stroke), "color": contrast}
        )
    style: dict[str, Any] = {"color": ink, "strokes": strokes}
    if d.shadow > 0:
        style["shadow"] = max(size * d.shadow, d.min_shadow)
        style["shadow_color"] = _with_alpha(contrast, d.shadow_alpha)
    return style


def design_fingerprint(design: str | TextDesign | None = None) -> dict:
    """プレビューのキャッシュ指紋に入れるデザイン定数。

    レイアウト定義(spec)に出てくるのは文字側の縁取り・影だけなので、背景側の
    暗転やグラデーション、白黒反転の閾値はここで拾う。デザインを差し替えたら
    プレビューのキャッシュが自動で無効になる。
    """
    return {
        **asdict(resolve_design(design)),
        "bright_threshold": BRIGHT_THRESHOLD,
        "dark_percentile": DARK_PERCENTILE,
    }


def load_thumbnail_layouts() -> dict:
    """サムネのレイアウト定義(layouts/thumbnail.json)を読む。

    スタイル(fullbleed / side)→ 型(word_image / word_only / no_word)の2段の
    dict。文言や枠を変えたいときはこのJSONを直接編集すればよく、コードは触らない。
    """
    global _thumbnail_layouts_cache
    if _thumbnail_layouts_cache is None:
        _thumbnail_layouts_cache = json.loads(
            THUMBNAIL_LAYOUT_PATH.read_text(encoding="utf-8")
        )
    return _thumbnail_layouts_cache


_thumbnail_layouts_cache: dict | None = None


def thumbnail_image_box() -> tuple[float, float, float, float]:
    """旧スタイル(STYLE_SIDE)で単語画像を貼る枠(thumbnail.json の image 要素)。"""
    for el in load_thumbnail_layouts()[STYLE_SIDE]["word_image"]["elements"]:
        if el.get("type") == "image":
            x, y, w, h = el["box"]
            return (x, y, w, h)
    raise ValueError(f"side/word_image に image 要素がありません: {THUMBNAIL_LAYOUT_PATH}")


def _spec_variant(has_word: bool, has_image: bool) -> str:
    """thumbnail.json のどの型を使うか。"""
    if not has_word:
        return "no_word"
    return "word_image" if has_image else "word_only"


def _thumbnail_element(
    raw: dict,
    design: str | TextDesign | None,
    credit_box: tuple[float, float, float, float] | None,
) -> dict:
    """thumbnail.json の1要素を layout.py が読める要素に直す。

    "outline": true は採用中の可読性デザイン(outline_style)の色・縁取り・影を
    当てる印、"credit_box": true は実際に貼られた画像の枠へ box を差し替える印で、
    どちらもこのファイル専用のキーなので展開したうえで取り除く。
    """
    el = {k: v for k, v in raw.items() if k not in ("outline", "credit_box")}
    if raw.get("credit_box") and credit_box is not None:
        el["box"] = list(credit_box)
    if raw.get("outline"):
        size = float(el["size"])
        el = {"type": "text", "size": size, **outline_style(size, design), **el}
    return el


def thumbnail_layout_spec(
    has_word: bool,
    has_image: bool,
    credit_box: tuple[float, float, float, float] | None = None,
    style: str = DEFAULT_STYLE,
    design: str | TextDesign | None = None,
) -> dict:
    """サムネのレイアウト定義(layout.py と同じ書式のdict)。

    中身は layouts/thumbnail.json にあり、ここではスタイルと型を選んで
    "outline"(可読性デザイン)と "credit_box"(実際の画像枠)を展開するだけ。

    STYLE_FULLBLEED(既定)では背景画像を呼び出し側が合成して渡すので、この
    定義に image 要素は出てこない(文字だけ)。文字は縁取りとぼかし影で
    背景の明暗に依存せず読めるようにする(帯は敷かない)。design で
    可読性デザインを差し替えられる(既定は TEXT_DESIGN)。

    STYLE_SIDE(旧)は
    - 言い換え単語+画像: 左に【単語】、右に画像(クレジットは画像の右下)
    - 言い換え単語のみ: 中央に大きく【単語】
    - どちらも無い: 「<曲名> を <リスト名> で歌ってみた」だけ(フォールバック)

    見出し(【単語】)は折り返さず、長い単語ではフォントを縮めて1行に収める
    (「】」だけが次の行に落ちるのを避けるため)。credit_box を渡すと
    クレジットの帯をその枠(=実際に貼られた画像の領域)の右下に置く(STYLE_SIDE)。
    """
    layouts = load_thumbnail_layouts()
    styles = layouts.get(style)
    if not isinstance(styles, dict):
        logger.warning("知らないサムネスタイルです(既定を使います): %s", style)
        styles = layouts[DEFAULT_STYLE]
    spec = styles[_spec_variant(has_word, has_image)]
    return {
        **{k: v for k, v in spec.items() if k != "elements"},
        "elements": [
            _thumbnail_element(el, design, credit_box) for el in spec["elements"]
        ],
    }


def thumbnail_layout(
    has_word: bool,
    has_image: bool,
    credit_box: tuple[float, float, float, float] | None = None,
    style: str = DEFAULT_STYLE,
    design: str | TextDesign | None = None,
) -> Layout:
    return parse_layout(
        thumbnail_layout_spec(has_word, has_image, credit_box, style, design),
        "<thumbnail>",
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
    キャプションが1行に入らない長さ(CAPTION_ONE_LINE_CHARS超)のときは
    「<曲名> を」/「<リスト名> で歌ってみた」の意味の切れ目で改行を入れる。
    """
    words = [word] if isinstance(word, str) else list(word)
    headline = " ".join(w for w in words if w)
    credits = [image_credit] if isinstance(image_credit, str) else list(image_credit)
    # 同じ画像・同じ撮影者のときに同じ文言が2つ並ばないよう、順序を保って重複を消す
    credit_text = " / ".join(dict.fromkeys(c for c in credits if c))
    if title and wordlist_text:
        caption = f"{title} を {wordlist_text} で歌ってみた"
        if len(caption) > CAPTION_ONE_LINE_CHARS:
            # 1行に入らない長さは、文字単位の自動折り返しに任せず意味の切れ目で折る
            caption = f"{title} を\n{wordlist_text} で歌ってみた"
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

    filler(万能候補)は「元歌詞のかなのまま」の仮想語なので、それしか無い
    ときは言い換えになっていない。エンジンが filler を返すようになる前と
    同じく空リストを返す(見出しを出さない)。
    """
    picked = [w for w in words if str(w.get("surface") or "")][:HEADLINE_MAX_WORDS]
    if not any(not w.get("filler") for w in picked):
        return []
    return picked


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


def _scrim_mask(width: int, height: int, scrim: Scrim) -> Image.Image:
    """上端(または下端)から中央へ薄れていくグラデーションのマスク(Lモード)。

    端から hold までは alpha のまま、そこから extent で 0 に落ちる。減衰は
    smoothstep なので変化の始めも終わりも傾きがゼロになり、帯のような
    境界線が見えない(そこがべた塗りの帯との違い)。
    """
    edge, hold, extent, alpha = scrim
    keep = max(0, int(hold * height))
    span = max(1, int(extent * height) - keep)
    column = Image.new("L", (1, height), 0)
    pixels = column.load()
    assert pixels is not None  # pragma: no cover - Lモードなら必ず取れる
    for i in range(min(keep + span, height)):
        t = 1.0 if i < keep else 1.0 - (i - keep) / span  # 端で1、消える位置で0
        smooth = t * t * (3.0 - 2.0 * t)
        y = i if edge == "top" else height - 1 - i
        pixels[0, y] = round(alpha * 255 * smooth)
    # 縦方向は等倍なので最近傍で引き伸ばせば値は変わらない
    return column.resize((width, height), Image.Resampling.NEAREST)


def apply_scrim(canvas: Image.Image, scrims: Sequence[Scrim]) -> Image.Image:
    """背景の上下を下地ごと自然に暗くする(案1)。矩形の帯は敷かない。"""
    if not scrims:
        return canvas
    black = Image.new("RGB", canvas.size, "black")
    for scrim in scrims:
        canvas.paste(black, (0, 0), _scrim_mask(canvas.width, canvas.height, scrim))
    return canvas


def region_luminance(
    image: Image.Image, box: Sequence[float], percentile: float = DARK_PERCENTILE
) -> float:
    """box(フレーム比率)の領域の明るさ(0-255)。

    既定は下位 DARK_PERCENTILE の値=「その領域の暗いほうの代表値」。平均や
    中央値だと、左半分が空・右半分が黒い写真のような枠で「明るい」と判断して
    しまい、黒文字にすると暗いほうで読めなくなる。暗いほうまで明るいときだけ
    反転させたいので、低いパーセンタイルを見る。
    """
    w, h = image.size
    x0 = max(0, min(w - 1, int(box[0] * w)))
    y0 = max(0, min(h - 1, int(box[1] * h)))
    x1 = max(x0 + 1, min(w, int((box[0] + box[2]) * w)))
    y1 = max(y0 + 1, min(h, int((box[1] + box[3]) * h)))
    hist = image.convert("L").crop((x0, y0, x1, y1)).histogram()
    total = sum(hist)
    if not total:  # pragma: no cover - cropが空になることは上のclampで無い
        return 0.0
    target = total * min(1.0, max(0.0, percentile))
    seen = 0
    for value, count in enumerate(hist):
        seen += count
        if seen >= target:
            return float(value)
    return 255.0  # pragma: no cover


def apply_adaptive_colors(
    spec: dict, background: Image.Image | None, design: str | TextDesign | None = None
) -> dict:
    """文字要素ごとに背景の明るさを測り、明るい所だけ黒文字+白縁に反転する(案3)。

    要素単位で判定するので、上が空(明るい)・下が影(暗い)のような写真でも
    見出しとキャプションが別々に最適な色になる。adaptive でない案では素通し。
    """
    d = resolve_design(design)
    if not d.adaptive or background is None:
        return spec
    elements = []
    for el in spec.get("elements", []):
        if el.get("type") == "text" and "size" in el and "box" in el:
            dark_text = region_luminance(background, el["box"]) > BRIGHT_THRESHOLD
            el = {**el, **outline_style(float(el["size"]), d, dark_text=dark_text)}
        elements.append(el)
    return {**spec, "elements": elements}


def compose_background(
    image_paths: Sequence[Path],
    width: int,
    height: int,
    dim: float | None = None,
    design: str | TextDesign | None = None,
) -> Image.Image | None:
    """単語画像を全面に敷いた背景を作る(2枚なら左右に等分)。1枚も読めなければ None。

    文字を載せるので、そのままだと明るい写真で白文字が飛ぶ。全体を dim 倍に
    落としたうえで、文字側の縁取り・影(と案によっては上下のグラデーション)で
    可読性を担保する。可読性の主役は文字側なので、dim は写真の中身が分かる
    程度の弱い暗転に留める。
    """
    d = resolve_design(design)
    dim = d.background_dim if dim is None else dim
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
    return apply_scrim(ImageEnhance.Brightness(canvas).enhance(dim), d.scrim)


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
    design: str | TextDesign | None = None,
) -> Path:
    """サムネPNGを描いて out_path に保存する。

    words / image_paths / image_credits は1件でも複数(言い換え2語)でもよい。
    STYLE_FULLBLEED では画像を全面に敷いた背景を先に合成し、その上に文字を描く。
    app_credit は隅の署名(既定は「lyrics & video by Soramimic」。動画本編と同じ文言)。
    design で文字の可読性デザインを差し替えられる(既定は TEXT_DESIGN)。
    """
    if isinstance(image_paths, Path):
        images = [image_paths]
    else:
        images = list(image_paths or [])
    data = thumbnail_data(title, wordlist_text, words, image_credits, app_credit)
    has_word = bool(data["headline"])

    if style == STYLE_FULLBLEED:
        background = compose_background(images, width, height, design=design)
        spec = thumbnail_layout_spec(
            has_word, background is not None, style=style, design=design
        )
        # 白黒反転は「実際に文字の裏に来る絵」(暗転・グラデーション適用後)で判定する
        layout = parse_layout(
            apply_adaptive_colors(spec, background, design), "<thumbnail>"
        )
        canvas = render_image(
            layout, None, data, width, height, background=background
        )
    else:
        image_path = images[0] if images else None
        credit_box = None
        if image_path is not None and data["image_credit"]:
            # クレジットの帯は枠ではなく実際に貼られた画像の右下に載せる(動画と同じ)
            credit_box = fitted_image_box(
                image_path, thumbnail_image_box(), width, height
            )
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


def resolve_headline(
    song: str,
    wordlist: str,
    where: str | None = None,
    params: dict[str, Any] | None = None,
    image_cache: Path | None = None,
    download_images: bool = True,
    missing_images: list[tuple[str, str]] | None = None,
    image_wait_sec: float = 0.0,
    song_kana: str = "",
) -> tuple[list[str], list[Path], list[str]]:
    """曲名を1フレーズ変換し、(見出しの単語, 単語画像, クレジット文言)を返す。

    変換や画像取得に失敗しても例外にはせず、取れたところまでを返す
    (サムネの失敗でジョブを落とさない)。中断要求(Cancelled)は伝播する。
    missing_images を渡すと、使いたかったのにキャッシュに無かった画像・クレジットの
    (URL, 画像ページ)が積まれる(download_images=False のときだけ起きる)。
    image_wait_sec を渡すと、download_images=False でも「合計その秒数まで」は
    画像のダウンロードを待つ(プレビューの1回目から絵を出すため)。
    song_kana(曲名の読み・カタカナ)があれば、変換の入力にはそちらを使う
    (「紅葉」→ MeCab推定の「コーヨー」ではなく「モミジ」で変換したいときに使う。
    サンプル曲は samples.json の title_kana から来る)。
    """
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
    return words, image_paths, image_credits


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
    design: str | TextDesign | None = None,
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
    words, image_paths, image_credits = resolve_headline(
        song,
        wordlist,
        where,
        params,
        image_cache,
        download_images,
        missing_images,
        image_wait_sec=image_wait_sec,
        song_kana=song_kana,
    )

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
            design=design,
        )
    except Exception as e:  # noqa: BLE001 - 描画失敗もジョブは落とさない
        logger.warning("サムネ画像を生成できませんでした: %s", e)
        return None
    runproc.log_generated_path(logger, "サムネ画像を生成しました", path)
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
