"""フレームレイアウト: 単語画像と単語リスト行の列情報をPillowで1枚に合成する。

レイアウトはJSONで宣言し、組み込み名(layouts/*.json)かJSONファイルパスで指定する:

    {
      "background": "black",
      "font": "/path/to/font.ttc",   # 省略時は日本語フォントを自動検出
      "elements": [
        {"type": "image", "box": [0.09, 0.07, 0.82, 0.62]},
        {"type": "text", "text": "{original}", "box": [0.05, 0.72, 0.9, 0.1],
         "size": 0.06, "color": "white", "align": "center"},
        {"type": "subtitle", "source": "original", "box": [0.02, 0.895, 0.96, 0.05],
         "size": 0.042, "color": "#b8b8b8"}
      ]
    }

- box は [x, y, 幅, 高さ] のフレーム比率。text/subtitle の size / stroke_width は高さ比率
- text は str.format 形式のテンプレート。単語リスト行の任意の列
  (original, prefecture, achievement など)と替え歌単語のフィールド
  (surface, kana, original_surface, originalkana)を参照でき、
  存在しない列は空文字になる
- wrap: true でboxの幅に合わせて文字単位で折り返す(説明文など長い列向け)。
  折り返してもboxに収まらないときはフォントを縮めて収める
- columns: 2以上を書くと段組みになる。改行(\n)区切りの各行を1項目として
  列優先(上から下へ、埋まったら次の列へ)に並べる。短い語をたくさん並べる
  用途(エンドロールの単語一覧)向け。行の高さは全列共通で、列幅から
  はみ出す語だけそのフォントを縮める(1語のせいで一覧全部が小さくならない)。
  値は段数の「上限」で、実際の段数は語の実測幅から自動で決まる。いちばん大きく
  組める段数を採るので、駅名のように短い語だけなら上限まで段が伸び、外国人の
  フルネームが混ざるリストでは列幅が足りず段が減る
- 文字の可読性(背景写真の明暗に負けない)は次の3つで作る。いずれも
  size と同じくフレーム高さ比率で指定する
    - stroke_width / stroke_color: 1重の縁取り(従来から)
    - strokes: 二重以上の縁取り。太い順に重ねて描くので
      [{"width": 0.011, "color": "white"}, {"width": 0.007, "color": "black"}]
      と書くと「白い文字 → 黒い内側の環 → 白い外側の環」になり、明るい背景でも
      暗い背景でもどちらかの環が効く。指定すると stroke_width は使われない
    - shadow / shadow_color: 文字の形にぼかした影(矩形の帯ではなく文字の
      周りだけを局所的に暗くする)。shadow はぼかし半径、色はα付きで書ける
- subtitle は行タイミングの歌詞字幕(ASSで焼く)の配置。source は
  parody(替え歌歌詞) / original(元歌詞)。boxのalign/valign側の辺が
  表示位置になる(既定は中央下)。subtitle要素を1つでも書くと既定の字幕
  (下部2段: 替え歌/元歌詞)は使われないので、両方出すなら両方書くこと
  (逆に元歌詞を消したいときは parody だけ書けばよい)
- subtitle要素がないレイアウトでは既定の字幕が画面下部約25%に載るので、
  image/text はそこを空けて配置すること
- fallback: 単語リストに行がない単語(手入力の未知語など)は elements の
  代わりに fallback の要素で描く。列参照({achievement}など)は空になるので、
  {surface}/{original} のような替え歌単語フィールドだけで組むとよい。
  fallback省略時は従来どおり elements を使う(未知語は表示できるものが
  なければスキップ)。字幕(subtitle)は行タイミングなので通常側で共通
- 各要素の require: "列名" を指定すると、その列が空の単語では要素を出さない。
  「行はあるが一部の列だけ欠ける」(没年不明など)ケースに使え、fallbackとは
  独立に効く(通常側・fallback側どちらの要素にも書ける)。逆に
  require_empty: "列名" はその列が埋まっている単語で要素を出さない。両者を
  組み合わせると「type2があれば『くさ・どく』、無ければ『くさ』」のように
  同じ位置で出し分けられる。CSVの NA/N/A/nan/none/null は空として扱う
- require_prefix: {"列名": "プレフィックス"} を指定すると、その列の値が
  プレフィックスで始まる単語だけ要素を出す(空の列は不一致扱い)。逆に
  require_not_prefix は「列が空、またはプレフィックスで始まらない」単語だけ出す。
  「imageがCommons実写(http://commons〜)の行だけ写真レイアウト、生成イメージの
  行は文字レイアウト」のような出典による出し分けに使う。複数列を書くと and 条件
- 画像のクレジット表記: image要素のあるレイアウトでは、クレジット表記が必要な
  画像(Wikimedia CommonsでAttributionRequiredのもの。image_credit.py参照)に
  限り、出典文言({image_credit})を画像の右下に自動で焼き込む。
  "credit": false で無効化できる。位置や見た目を変えたいときは text 要素で
  {image_credit} を自分で参照すれば自動追加はされない
- アプリのクレジット表記: どのレイアウトでも {app_credit}(既定
  「lyrics & video by Soramimic」。歌声合成のクレジットが必要なときは
  「lyrics & video by Soramimic / VOICEVOX:キャラ名」のように連結される)を
  フレーム左下に小さく自動で焼き込む。画像クレジット(画像の右下)や既定字幕
  (〜画面高95%)と重ならない最下段に置く。"app_credit": false で無効化でき、
  位置や見た目を変えたいときは text 要素で {app_credit} を自分で参照すれば
  自動追加はされない(無効化する場合は動画の説明欄などで表記すること)

歌唱がない区間(前奏・間奏・後奏)の表示は次の2つで指定できる(任意・opt-in):

    {
      "hold": "next",
      "idle": [
        {"type": "text", "text": "{title}", "box": [0.1, 0.4, 0.8, 0.2], "size": 0.1},
        {"type": "text", "text": "単語リスト: {wordlist}", "box": [0.1, 0.62, 0.8, 0.08]}
      ]
    }

- "hold": "next" は各単語のフレームを次の歌唱まで表示し続ける(既定の3秒上限
  HOLD_MAX_SEC を解除する)。省略時は従来どおり最大3秒で idle(なければ黒)に戻る
- "idle" は歌唱がない区間に出すフレーム(elementsと同じ書式)。単語データはない
  ので、固定文言か下記のプロジェクトレベルの列だけ {..} で参照できる。subtitle
  要素は無視される(行タイミングの歌詞が存在しないため)。省略時はその区間は黒画面
    - title: 入力MIDIのファイル名(拡張子なし。音源プロジェクトでは空)
    - wordlist: 使用した単語リスト名
- hold と idle を併用した場合、単語と単語の間の隙間は hold(直前フレーム)が、
  先頭(1単語目より前)と末尾(最終単語より後)は idle が受け持つ

さらに、歌唱なし区間は前奏・間奏・後奏の3種に分けて出し分けられる。
"intro" / "interlude" / "outro" を書くとその区間だけ別の要素で描き、
書かなければ従来どおり "idle" が受け持つ(どちらも無ければ黒画面)。
"credits" は後奏の最後(単語ページをめくり終わったあと)に1枚出すクレジット
ページで、後奏の余った時間を全部受け持つ:

    {
      "interlude": [
        {"type": "text", "text": "間奏({interlude_sec}秒)", "box": [0.1, 0.44, 0.8, 0.12]}
      ],
      "outro": [
        {"type": "text", "text": "{used_words}", "box": [0.06, 0.2, 0.88, 0.5],
         "columns": 8, "align": "left"}
      ]
    }

- 区間ごとに追加で参照できる列(idle の title / wordlist / app_credit に加えて):
    - interlude_sec: 歌が止まっている長さ(整数秒。直前の単語フレームの余韻を含む
      ので、間奏フレームが実際に映っている時間より少し長い)。間奏以外では空文字
    - used_words: 使った替え歌単語の一覧(後奏のエンドロール用)。1語1行の
      改行区切りなので、columns を付ければ段組み、付けなければ縦一列になる
    - image_credits: 使用画像のクレジットをまとめた文言。既定のエンドロールでは
      使っていない(各単語フレームの右下に個別のクレジットを焼いているため)
    - page / pages: 後奏が複数枚に分かれたときのページ番号と総ページ数
    - original_song: 元曲名
    - original_credit: 元曲の作詞・作曲・編曲等の著作者クレジット
    - credit_notice: 権利者やライセンスから指定された表記
    - synth_credit: 歌声合成側のクレジット表記(「VOICEVOX:四国めたん」など)。
      クレジットページで使う。表記が要らない合成では空なので、その行の要素には
      "require": "synth_credit" を付けて丸ごと出さないようにする
- 後奏の単語ページは1枚 video.ENDROLL_PAGE_SEC を目安に拍の切れ目でめくり、
  余った時間は "credits" のページが受け持つ("credits": [] で無効化すると
  最後の単語ページが後奏の終わりまで伸びる)
- 短い間奏(video.INTERLUDE_MIN_SEC 未満)や短い後奏(video.OUTRO_MIN_SEC 未満)
  では専用の表示を出さず idle(なければ黒)に戻る。一瞬だけ出て消えるのを避けるため。
  ただし後奏はエンドロールに足りないぶんを動画末尾の無音区間として足す
  (video.extend_for_endroll)ので、後奏が無い曲でも単語一覧とクレジットは出る
- 既定の文言は section_defaults.json(パッケージ直下)に置いてある。レイアウトJSON側に
  同名のキーを書けばそのレイアウトの指定が優先される("interlude": [] で無効化できる)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

LAYOUTS_DIR = Path(__file__).resolve().parent / "layouts"
# 単語リスト名(external/soramimic-wordlists のCSVのstem)→ 組み込みレイアウト名のマップ
WORDLIST_LAYOUTS_PATH = Path(__file__).resolve().parent / "wordlist_layouts.json"
# layouts/ にあるが動画フレームのレイアウトではないもの(スタイル別の入れ子構造なので
# そのままでは Layout にならない)。UIのレイアウト選択にも load_layout にも出さない
NON_FRAME_LAYOUTS = frozenset({"thumbnail"})
# 歌唱なし区間の種別。前奏(1単語目より前)・間奏(単語と単語の間)・後奏(最終単語より後)、
# credits=後奏の最後に出すクレジットページ
IDLE_SECTIONS = ("intro", "interlude", "outro", "credits")
# 描画ロジックを変えたときに共有PNGキャッシュを確実に無効化する。
# v2: 透明画像のアルファ合成(黒潰れの解消)と、画像なし語のfallback化
FRAME_RENDER_CACHE_VERSION = 2
# 区間ごとの既定の表示定義。レイアウトJSONに同名キーがあればそちらが優先される
SECTION_DEFAULTS_PATH = Path(__file__).resolve().parent / "section_defaults.json"
FONT_ENV = "SORAMIMIC_VIDEO_FONT"

# 日本語が描けるフォントの探索先(上から順に使う。macOS / Linux(Colab))
_FONT_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
]

_MIN_FONT_PX = 9
# 段組みのカラム間の余白。「1段あたりの持ち幅」に対する比なので、段数を増やしても
# 余白だけが画面を食い潰さない(box幅に対する比にすると8段で幅の3割が余白になる)
_COLUMN_GAP_RATIO = 0.12
# 段数を決めるとき、列幅からはみ出す語(=その語だけ縮める)を何割まで許すか
_COLUMN_OVERFLOW_RATIO = 0.15
# 段を増やしすぎて「横に長い帯」にならないよう、1段あたり最低これだけの行数を残す
_COLUMN_MIN_ROWS = 4

# 動画本編に焼き込むアプリのクレジット(サムネの署名と同じ文言)。
# 歌声合成側のクレジット表記が要るときは呼び出し側が
# 「lyrics & video by Soramimic / VOICEVOX:キャラ名」のように連結して data に入れる
APP_CREDIT = "lyrics & video by Soramimic"
# 自動追加するアプリクレジットの位置(フレーム左下)と見た目。
# 画像クレジット(画像の右下)・既定字幕(下端0.945)と重ならない最下段に、
# 画像クレジット(0.025)より小さい文字で、白を少し透かして置く
APP_CREDIT_BOX = (0.012, 0.945, 0.7, 0.045)
APP_CREDIT_SIZE = 0.022
APP_CREDIT_COLOR = "#ffffffb3"


# 単語リストCSVで「値なし」を表す文字列(R由来のNA等)。値として描画せず空扱いにする。
MISSING_VALUES = frozenset({"na", "n/a", "nan", "none", "null"})


def is_missing(value: object) -> bool:
    """CSVの値が実質的に空か(空文字・空白のみ・NA等の欠損マーカー)。"""
    return str(value or "").strip().lower() in MISSING_VALUES or not str(value or "").strip()


class _SafeDict(dict):
    """テンプレートにない列を空文字にする(リストごとに列構成が違うため)。"""

    def __missing__(self, key: str) -> str:
        return ""


def _display_value(column: str, value: object) -> object:
    """CSVの保存形式を変えず、カード表示向けに値を整える。"""
    if column == "team":
        return re.sub(r"(?<=[^\x00-\x7f])-(?=[^\x00-\x7f])", "・", str(value))
    return value


@dataclass
class TextElement:
    template: str
    box: tuple[float, float, float, float]
    size: float = 0.06
    color: str = "white"
    align: str = "center"  # left / center / right
    valign: str = "middle"  # top / middle / bottom
    wrap: bool = False
    # 2以上で段組み(値は段数の上限。実際の段数は語の幅から自動で決める)。
    # 改行区切りの各行を列優先(上から下へ、次の列へ)に並べる
    columns: int = 1
    stroke_width: float = 0.0
    stroke_color: str = "black"
    # 二重(以上)の縁取り。(幅, 色) を太い順に重ねて描く。指定すると
    # stroke_width / stroke_color は使わない。幅は size と同じフレーム高さ比率
    strokes: tuple[tuple[float, str], ...] = ()
    # 文字の形をぼかした影(帯を敷かずに文字の周りだけ暗くする)。ぼかし半径を
    # フレーム高さ比率で。0で無効
    shadow: float = 0.0
    shadow_color: str = "#000000cc"  # α付きで濃さを決める
    background: str | None = None  # テキスト背後の帯。"#00000080" のようにα付き可
    require: str | None = None  # この列が空の単語ではこの要素を出さない
    require_empty: str | None = None  # この列が埋まっている単語ではこの要素を出さない
    # 列の値がプレフィックスで始まる単語だけ出す({"image": "http://commons"} 等)
    require_prefix: dict[str, str] | None = None
    # 列が空、またはプレフィックスで始まらない単語だけ出す(上の逆)
    require_not_prefix: dict[str, str] | None = None


@dataclass
class ImageElement:
    box: tuple[float, float, float, float]
    require: str | None = None  # この列が空の単語ではこの要素を出さない
    require_empty: str | None = None  # この列が埋まっている単語ではこの要素を出さない
    # 列の値がプレフィックスで始まる単語だけ出す({"image": "http://commons"} 等)
    require_prefix: dict[str, str] | None = None
    # 列が空、またはプレフィックスで始まらない単語だけ出す(上の逆)
    require_not_prefix: dict[str, str] | None = None


@dataclass
class SubtitleElement:
    """行タイミングの歌詞字幕。Pillowではなく video.build_ass がASSに変換する。"""

    source: str  # "parody"(替え歌歌詞) / "original"(元歌詞)
    box: tuple[float, float, float, float]
    size: float = 0.05
    color: str = "white"
    align: str = "center"  # left / center / right
    valign: str = "bottom"  # top / middle / bottom
    bold: bool = False
    # 替え歌字幕に単語ごとのふりがな(ルビ)を付ける。ふりがなは ParodyWord.kana の
    # ひらがな表示。
    # 第1段階では source="parody" のみ有効(元歌詞ではカナ対応付けに課題があり無視する)。
    ruby: bool = False
    ruby_size: float = 0.5  # ルビの文字サイズ(本文フォントサイズに対する比)
    # 表示粒度。"line"(行) / "phrase"(フレーズ)。None は source 既定
    # (original=line, parody=phrase)。詳細は align.build_subtitle_segments。
    granularity: str | None = None


# subtitle要素を持たないレイアウトで使う既定の字幕(従来の下部2段と同じ見た目)
DEFAULT_SUBTITLES = [
    SubtitleElement(
        source="parody", box=(0.02, 0.77, 0.96, 0.10), size=0.065, color="white", bold=True
    ),
    SubtitleElement(source="original", box=(0.02, 0.895, 0.96, 0.05), size=0.042, color="#b8b8b8"),
]


def _require_met(el: ImageElement | TextElement, values: dict) -> bool:
    """要素の require / require_empty を満たすか(未指定なら常にTrue)。

    require は「その列が埋まっているとき出す」、require_empty は逆に
    「その列が空のときだけ出す」。両方書けば and 条件。NA等の欠損は空と見なす。
    「type2があれば『くさ・どく』、無ければ『くさ』」のような出し分けに使う。
    """
    if el.require and is_missing(values.get(el.require)):
        return False
    if el.require_empty and not is_missing(values.get(el.require_empty)):
        return False
    for col, prefix in (el.require_prefix or {}).items():
        v = values.get(col)
        if is_missing(v) or not str(v).startswith(prefix):
            return False
    for col, prefix in (el.require_not_prefix or {}).items():
        v = values.get(col)
        if not is_missing(v) and str(v).startswith(prefix):
            return False
    return True


def _element_texts(elements: list[ImageElement | TextElement], data: dict) -> list[str]:
    """要素列のtextテンプレートを埋めた文字列(要素順)。imageは含まない。

    require 列が空の要素は空文字にする(描画側でスキップされる)。
    """
    values = _SafeDict(
        # NA等の欠損マーカーは「NA年生まれ」と描画されてしまうので空文字に潰す
        {
            k: ("" if is_missing(v) else _display_value(k, v))
            for k, v in data.items()
            if v is not None
        }
    )
    out = []
    for el in elements:
        if isinstance(el, TextElement):
            if not _require_met(el, values):
                out.append("")
                continue
            try:
                out.append(el.template.format_map(values).strip())
            except (ValueError, IndexError, KeyError):
                # {0} や {a[b]} など format_map で解決できない指定は原文のまま
                out.append(el.template)
    return out


@dataclass
class Layout:
    elements: list[ImageElement | TextElement]
    subtitles: list[SubtitleElement] = field(default_factory=list)
    fallback: list[ImageElement | TextElement] = field(default_factory=list)
    # 歌唱がない区間(前奏・間奏・後奏)に出す固定フレームの要素(空なら黒画面)
    idle: list[ImageElement | TextElement] = field(default_factory=list)
    # 区間種別(IDLE_SECTIONS)ごとの要素。空の種別は idle が受け持つ
    sections: dict[str, list[ImageElement | TextElement]] = field(default_factory=dict)
    # sections の元JSON(フレームキャッシュのキー用)
    section_raw: dict[str, list] = field(default_factory=dict)
    # "hold": "next" で単語フレームを次の歌唱まで持続する(3秒上限を外す)
    hold_next: bool = False
    # 画像クレジット({image_credit})の自動焼き込み要素。credit_textが空の単語
    # (表記不要・情報なし)では描かれない。None = 自動追加なし
    credit: TextElement | None = None
    # アプリクレジット({app_credit})の自動焼き込み要素。全レイアウト共通で
    # フレーム左下に出す。None = 自動追加なし("app_credit": false か自前配置)
    app_credit: TextElement | None = None
    background: str = "black"
    font: str | None = None
    raw: dict = field(default_factory=dict)  # フレームキャッシュのキー用に元JSONを保持

    def active_elements(self, use_fallback: bool = False) -> list[ImageElement | TextElement]:
        """描画に使う要素列。未知語(use_fallback)でfallback定義があればそちら。"""
        return self.fallback if (use_fallback and self.fallback) else self.elements

    def render_texts(self, data: dict, use_fallback: bool = False) -> list[str]:
        """text要素のテンプレートを埋めた文字列(要素順)。imageは含まない。"""
        return _element_texts(self.active_elements(use_fallback), data)

    def section_elements(
        self, section: str
    ) -> tuple[list[ImageElement | TextElement], list, str]:
        """歌唱なし区間の (要素, 元JSON, キャッシュタグ)。

        専用の定義(intro/interlude/outro)があればそれを、無ければ従来の idle を
        使う。どちらも空なら空リスト(呼び出し側はクレジットだけ or 黒画面にする)。
        """
        elements = self.sections.get(section) or []
        if elements:
            return elements, self.section_raw.get(section, []), section
        return self.idle, self.raw.get("idle", []), "idle"

    def has_section(self, section: str) -> bool:
        """その区間に専用の表示定義があるか(idleへのフォールバックは含めない)。"""
        return bool(self.sections.get(section))


def builtin_layout_names() -> list[str]:
    return sorted(
        p.stem for p in LAYOUTS_DIR.glob("*.json") if p.stem not in NON_FRAME_LAYOUTS
    )


def load_wordlist_layouts() -> dict[str, str]:
    """単語リストごとの既定レイアウト(wordlist_layouts.json)を読む。

    「stationsを選んだらcaption」のように、単語リストの列構成に合うレイアウトを
    UIの初期選択にするためのマップ。組み込みレイアウトに無い名前を指しているエントリは
    警告を出して捨てる(マップの編集ミスでUIが壊れないように)。ファイルが無ければ空。
    """
    if not WORDLIST_LAYOUTS_PATH.exists():
        return {}
    try:
        raw = json.loads(WORDLIST_LAYOUTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("単語リスト別レイアウトを読めません: %s (%s)", WORDLIST_LAYOUTS_PATH, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "単語リスト別レイアウトはオブジェクトで書いてください: %s", WORDLIST_LAYOUTS_PATH
        )
        return {}
    builtin = set(builtin_layout_names())
    out: dict[str, str] = {}
    for wordlist, layout in raw.items():
        if not isinstance(layout, str) or layout not in builtin:
            logger.warning(
                "組み込みレイアウトに無いので無視します: %s -> %s (%s)",
                wordlist, layout, WORDLIST_LAYOUTS_PATH,
            )
            continue
        out[str(wordlist)] = layout
    return out


def load_layout(name_or_path: str | None) -> Layout:
    """組み込みレイアウト名(default等)またはJSONパスからレイアウトを読む。"""
    if not name_or_path:
        name_or_path = "default"
    p = Path(name_or_path)
    if p.suffix == ".json" and p.exists():
        path = p
    else:
        path = LAYOUTS_DIR / f"{name_or_path}.json"
        # フレームレイアウトでないJSON(サムネ定義)は組み込み名として受け付けない
        if name_or_path in NON_FRAME_LAYOUTS or not path.exists():
            builtin = ", ".join(builtin_layout_names())
            raise FileNotFoundError(
                f"レイアウトが見つかりません: {name_or_path} "
                f"(組み込み: {builtin}。またはJSONファイルのパスを指定してください)"
            )
    return parse_layout(json.loads(path.read_text(encoding="utf-8")), str(path))


def _parse_strokes(raw: object, origin: str) -> tuple[tuple[float, str], ...]:
    """text要素の strokes(多重縁取り)をパースする。太い順に並べて返す。

    書式は [{"width": 0.011, "color": "white"}, ...] か [[0.011, "white"], ...]。
    描画は太い順に重ねるだけなので、書いた順序は問わない。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"strokes は配列です: {raw!r} ({origin})")
    out: list[tuple[float, str]] = []
    for item in raw:
        if isinstance(item, dict):
            width, color = item.get("width", 0), item.get("color", "black")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            width, color = item
        else:
            raise ValueError(
                f'strokes の各要素は {{"width": 幅, "color": 色}} です: {item!r} ({origin})'
            )
        out.append((float(width), str(color)))
    return tuple(sorted(out, key=lambda s: -s[0]))


def _parse_prefix_map(raw: object, key: str, origin: str) -> dict[str, str] | None:
    """require_prefix / require_not_prefix の {列名: プレフィックス} を検証する。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ValueError(f"{key} は {{列名: プレフィックス}} の辞書です: {raw!r} ({origin})")
    return raw or None


def _color_or(e: dict, key: str, default: str) -> str:
    """色指定を取り出す。空文字・空白はレイアウトエディタ等の欠損とみなし既定色に落とす。"""
    v = e.get(key)
    if isinstance(v, str) and not v.strip():
        return default
    return v if v is not None else default


def _parse_elements(
    raw_elements: list, origin: str
) -> tuple[list[ImageElement | TextElement], list[SubtitleElement]]:
    """要素配列を (image/text要素, subtitle要素) に分けてパースする。"""
    elements: list[ImageElement | TextElement] = []
    subtitles: list[SubtitleElement] = []
    for e in raw_elements:
        box = tuple(float(v) for v in e["box"])
        if len(box) != 4:
            raise ValueError(f"box は [x, y, w, h] の4要素です: {e['box']} ({origin})")
        kind = e.get("type")
        if kind == "image":
            elements.append(
                ImageElement(
                    box=box,
                    require=e.get("require"),
                    require_empty=e.get("require_empty"),
                    require_prefix=_parse_prefix_map(
                        e.get("require_prefix"), "require_prefix", origin
                    ),
                    require_not_prefix=_parse_prefix_map(
                        e.get("require_not_prefix"), "require_not_prefix", origin
                    ),
                )
            )
        elif kind == "subtitle":
            source = e.get("source")
            if source not in ("parody", "original"):
                raise ValueError(
                    f"subtitle の source は parody / original です: {source!r} ({origin})"
                )
            granularity = e.get("granularity")
            if granularity is not None and granularity not in ("line", "phrase"):
                raise ValueError(
                    f"subtitle の granularity は line / phrase です: {granularity!r} ({origin})"
                )
            subtitles.append(
                SubtitleElement(
                    source=source,
                    box=box,
                    size=float(e.get("size", 0.05)),
                    color=_color_or(e, "color", "white"),
                    align=e.get("align", "center"),
                    valign=e.get("valign", "bottom"),
                    bold=bool(e.get("bold", False)),
                    ruby=bool(e.get("ruby", False)),
                    ruby_size=float(e.get("ruby_size", 0.5)),
                    granularity=granularity,
                )
            )
        elif kind == "text":
            elements.append(
                TextElement(
                    template=e.get("text", ""),
                    box=box,
                    size=float(e.get("size", 0.06)),
                    color=_color_or(e, "color", "white"),
                    align=e.get("align", "center"),
                    valign=e.get("valign", "middle"),
                    wrap=bool(e.get("wrap", False)),
                    columns=max(1, int(e.get("columns", 1))),
                    stroke_width=float(e.get("stroke_width", 0)),
                    stroke_color=_color_or(e, "stroke_color", "black"),
                    strokes=_parse_strokes(e.get("strokes"), origin),
                    shadow=float(e.get("shadow", 0)),
                    shadow_color=_color_or(e, "shadow_color", "#000000cc"),
                    background=e.get("background") or None,
                    require=e.get("require"),
                    require_empty=e.get("require_empty"),
                    require_prefix=_parse_prefix_map(
                        e.get("require_prefix"), "require_prefix", origin
                    ),
                    require_not_prefix=_parse_prefix_map(
                        e.get("require_not_prefix"), "require_not_prefix", origin
                    ),
                )
            )
        else:
            raise ValueError(f"未知のレイアウト要素 type={kind!r} ({origin})")
    return elements, subtitles


def _auto_credit_element(
    elements: list[ImageElement | TextElement],
    fallback: list[ImageElement | TextElement],
    raw: dict,
) -> TextElement | None:
    """画像クレジットの自動焼き込み要素(画像boxの右下に小さく載せる)。

    "credit": false のレイアウト、image要素のないレイアウト、text要素で
    {image_credit} を自分で配置しているレイアウトでは追加しない。
    """
    if raw.get("credit") is False:
        return None
    for el in (*elements, *fallback):
        if isinstance(el, TextElement) and "{image_credit}" in el.template:
            return None
    image_boxes = [el.box for el in elements if isinstance(el, ImageElement)]
    if not image_boxes:
        return None
    return TextElement(
        template="{image_credit}",
        box=image_boxes[0],
        size=0.025,
        color="#dddddd",
        align="right",
        valign="bottom",
        background="#00000080",
    )


def _auto_app_credit_element(
    element_groups: list[list[ImageElement | TextElement]], raw: dict
) -> TextElement | None:
    """アプリクレジットの自動焼き込み要素(フレーム左下に小さく載せる)。

    "app_credit": false のレイアウトと、text要素で {app_credit} を自分で
    配置しているレイアウト(サムネなど)では追加しない。
    """
    if raw.get("app_credit") is False:
        return None
    for group in element_groups:
        for el in group:
            if isinstance(el, TextElement) and "{app_credit}" in el.template:
                return None
    return TextElement(
        template="{app_credit}",
        box=APP_CREDIT_BOX,
        size=APP_CREDIT_SIZE,
        color=APP_CREDIT_COLOR,
        align="left",
        valign="bottom",
    )


def resolve_app_credit(data: dict) -> str:
    """{app_credit} に入れる文言。dataに指定があればそれ、無ければ既定の署名。

    歌声合成のクレジット表記が要るジョブでは video.py が
    「lyrics & video by Soramimic / VOICEVOX:キャラ名」を data に入れてくる。
    """
    text = str(data.get("app_credit") or "").strip()
    return text or APP_CREDIT


def _with_app_credit(data: dict) -> dict:
    """描画用データに {app_credit} の実文言を埋めたコピー。"""
    return {**data, "app_credit": resolve_app_credit(data)}


def load_section_defaults() -> dict[str, list]:
    """区間種別ごとの既定の表示定義(section_defaults.json)を読む。

    間奏の「間奏(X秒)」表示や後奏のエンドロールは、レイアウトごとに書き分けたい
    ものではないので既定をここに置き、必要なレイアウトだけJSONで上書きする。
    ファイルが無い・壊れているときは既定なし(=従来どおり idle が受け持つ)。
    """
    global _section_defaults_cache
    if _section_defaults_cache is not None:
        return _section_defaults_cache
    out: dict[str, list] = {}
    if SECTION_DEFAULTS_PATH.exists():
        try:
            raw = json.loads(SECTION_DEFAULTS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("区間の既定表示を読めません: %s (%s)", SECTION_DEFAULTS_PATH, e)
            raw = {}
        if isinstance(raw, dict):
            out = {
                name: list(raw[name])
                for name in IDLE_SECTIONS
                if isinstance(raw.get(name), list)
            }
    _section_defaults_cache = out
    return out


_section_defaults_cache: dict[str, list] | None = None


def parse_layout(raw: dict, origin: str = "<layout>") -> Layout:
    """レイアウトJSON(パース済みdict)を検証してLayoutにする。originはエラー表示用。"""
    elements, subtitles = _parse_elements(raw.get("elements", []), origin)
    # fallback(未知語用)の要素。字幕は行タイミングなので通常側と共通で使い、
    # fallback側に書かれた subtitle は無視する
    fallback, _ = _parse_elements(raw.get("fallback", []), origin)
    # idle(歌唱なし区間用)の要素。単語も行タイミングもないので subtitle は無視する
    idle, _ = _parse_elements(raw.get("idle", []), origin)
    # 前奏・間奏・後奏の出し分け。レイアウトに指定が無い種別は既定を使う
    defaults = load_section_defaults()
    section_raw = {
        name: list(raw[name]) if isinstance(raw.get(name), list) else defaults.get(name, [])
        for name in IDLE_SECTIONS
    }
    sections = {
        name: _parse_elements(items, origin)[0] for name, items in section_raw.items()
    }
    return Layout(
        elements=elements,
        subtitles=subtitles,
        fallback=fallback,
        idle=idle,
        sections=sections,
        section_raw=section_raw,
        hold_next=raw.get("hold") == "next",
        credit=_auto_credit_element(elements, fallback, raw),
        # 区間側(intro/interlude/outro)は含めない。後奏のエンドロールが自前で
        # {app_credit} を並べているだけで単語フレームの署名まで消えてしまうため
        # (区間フレーム側の重複は render_section_frame が個別に避ける)
        app_credit=_auto_app_credit_element([elements, fallback, idle], raw),
        background=raw.get("background", "black"),
        font=raw.get("font"),
        raw=raw,
    )


# ---- フォント ----

# Pillowのフォント型(TrueType or 既定のビットマップフォント)
_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
# 描画位置の確定した1行: (x, y, 文字列, フォント)。カラム組みでは語ごとに
# フォントサイズが変わりうるので、行ごとにフォントを持たせる
_Placed = tuple[float, float, str, _Font]
_font_cache: dict[tuple[str, int], _Font] = {}
_warned_no_font = False


def resolve_font_path(layout_font: str | None) -> Path | None:
    """レイアウト指定 → 環境変数 → 既知の日本語フォント の順で探す。"""
    for cand in (layout_font, os.environ.get(FONT_ENV), *_FONT_CANDIDATES):
        if cand:
            p = Path(cand).expanduser()
            if p.exists():
                return p
    return None


def _font(path: Path | None, px: int) -> _Font:
    global _warned_no_font
    key = (str(path), px)
    f = _font_cache.get(key)
    if f is None:
        if path is None:
            if not _warned_no_font:
                logger.warning(
                    "日本語フォントが見つかりません。%s で指定してください"
                    "(既定フォントでは日本語が描けない場合があります)",
                    FONT_ENV,
                )
                _warned_no_font = True
            f = ImageFont.load_default(px)
        else:
            f = ImageFont.truetype(str(path), px)
        _font_cache[key] = f
    return f


# ---- 描画 ----


def _box_px(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    x, y, w, h = box
    return int(x * width), int(y * height), max(1, int(w * width)), max(1, int(h * height))


def _paste_image(canvas: Image.Image, image_path: Path, el: ImageElement) -> None:
    x, y, w, h = _box_px(el.box, canvas.width, canvas.height)
    with Image.open(image_path) as source:
        scale = min(w / source.width, h / source.height)
        nw = max(1, round(source.width * scale))
        nh = max(1, round(source.height * scale))
        img = source.resize((nw, nh), Image.Resampling.LANCZOS)
    pos = (x + (w - nw) // 2, y + (h - nh) // 2)
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        # 透明なSVG/PNGは convert("RGB") すると透明部分が黒に潰れるので、
        # アルファをマスクにして下地(黒背景)の上に合成する
        rgba = img.convert("RGBA")
        canvas.paste(rgba, pos, rgba)
    else:
        canvas.paste(img.convert("RGB"), pos)


def fitted_image_box(
    image_path: Path, box: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float] | None:
    """image要素のboxにアスペクト維持で収めた画像の実表示領域(フレーム比率)。

    _paste_image と同じ配置計算。クレジット表記を(boxではなく)画像そのものの
    右下に載せるために使う。画像が読めなければ None。
    """
    try:
        with Image.open(image_path) as img:
            iw, ih = img.size
    except Exception:
        return None
    x, y, w, h = _box_px(box, width, height)
    scale = min(w / iw, h / ih)
    nw = max(1, round(iw * scale))
    nh = max(1, round(ih * scale))
    px = x + (w - nw) // 2
    py = y + (h - nh) // 2
    return (px / width, py / height, nw / width, nh / height)


def _wrap_chars(
    draw: ImageDraw.ImageDraw, text: str, font, max_w: int
) -> list[str]:
    """日本語向けの文字単位折り返し(空白区切りに頼らない)。"""
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for ch in para:
            if line and draw.textlength(line + ch, font=font) > max_w:
                lines.append(line)
                line = ch
            else:
                line += ch
        lines.append(line)
    return lines


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    el: TextElement,
    box_w: int,
    box_h: int,
    frame_h: int,
    font_path: Path | None,
    pad: int = 0,
):
    """boxに収まるフォントサイズ・行リスト・行送りを決める(収まるまで縮める)。

    pad は縁取りなど文字の外側にはみ出す装飾のぶん(px)。その太さを引いた
    内側にグリフを収めるので、太い縁取りがboxからはみ出さない。
    """
    box_w = max(1, box_w - 2 * pad)
    box_h = max(1, box_h - 2 * pad)
    px = max(_MIN_FONT_PX, int(el.size * frame_h))
    while True:
        font = _font(font_path, px)
        lines = _wrap_chars(draw, text, font, box_w) if el.wrap else text.split("\n")
        line_h = px * 1.25
        max_line_w = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if (max_line_w <= box_w and line_h * len(lines) <= box_h) or px <= _MIN_FONT_PX:
            return font, lines, line_h
        px = max(_MIN_FONT_PX, px - max(1, px // 8))


def _stroke_layers(el: TextElement, frame_h: int) -> list[tuple[int, str]]:
    """多重縁取りの (太さpx, 色) を太い順に。strokes未指定なら空。"""
    # 小さい文字では1px未満になりがちなので、幅を指定した縁取りは最低1px残す
    layers = [(max(1, round(w * frame_h)), c) for w, c in el.strokes if w > 0]
    return layers


def _font_px(font: _Font, fallback: float) -> float:
    """フォントの実サイズ(px)。日本語フォントが見つからないときに使う既定の
    ビットマップフォント(ImageFont.ImageFont)は size を持たないので fallback。"""
    return float(getattr(font, "size", fallback))


def _fit_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path | None,
    base_px: int,
    max_w: float,
) -> _Font:
    """1行が max_w に収まるフォント(収まるなら base_px のまま)。

    カラム組みで、幅からはみ出す語だけを縮めるために使う。全体を base_px から
    下げると長い外国人名1件のせいで一覧全部が小さくなるので、はみ出した語の
    フォントだけ落とす。
    """
    font = _font(font_path, base_px)
    if not text:
        return font
    w = draw.textlength(text, font=font)
    if w <= max_w:
        return font
    px = max(_MIN_FONT_PX, int(base_px * max_w / w))
    font = _font(font_path, px)
    while px > _MIN_FONT_PX and draw.textlength(text, font=font) > max_w:
        px = max(_MIN_FONT_PX, px - max(1, px // 16))
        font = _font(font_path, px)
    return font


def _column_geometry(w: float, cols: int) -> tuple[float, float]:
    """段数 cols のときの (列幅, 列間) 。列間は1段の持ち幅に対する比で取る。"""
    unit = w / cols
    gap = unit * _COLUMN_GAP_RATIO if cols > 1 else 0.0
    return unit - gap, gap


def _column_fit_px(
    draw: ImageDraw.ImageDraw,
    items: list[str],
    el: TextElement,
    cols: int,
    w: float,
    h: float,
    frame_h: int,
    font_path: Path | None,
) -> int:
    """段数 cols のときの本文フォントサイズ(高さにも列幅にも収まる最大)。

    行数から決まる上限に加えて、語の実測幅からも上限がかかる。ただし
    はみ出しを許す数(_COLUMN_OVERFLOW_RATIO)だけ幅の広い語は無視する。
    そこは _fit_line がその語だけ縮めるので、少数のために全体を小さくしない。
    """
    rows = max(1, -(-len(items) // cols)) if items else 1
    px = max(_MIN_FONT_PX, min(int(el.size * frame_h), int(h / (rows * 1.25))))
    if not items:
        return px
    col_w, _gap = _column_geometry(w, cols)
    widths = sorted(draw.textlength(it, font=_font(font_path, px)) for it in items)
    allowed = max(1, int(len(widths) * _COLUMN_OVERFLOW_RATIO))
    ref = widths[max(0, len(widths) - 1 - allowed)]
    if ref > col_w:
        px = max(_MIN_FONT_PX, int(px * col_w / ref))
    return px


def _choose_columns(
    draw: ImageDraw.ImageDraw,
    items: list[str],
    el: TextElement,
    w: int,
    h: int,
    frame_h: int,
    font_path: Path | None,
) -> tuple[int, int]:
    """段数(1〜el.columns)と本文フォントサイズを決める。

    段を増やすほど行数は減る(高さに余裕が出る)が列幅は狭くなるので、本文サイズは
    その両方の小さい方で決まる。いちばん大きく組める段数を選ぶと、駅名のような
    短い語では上限いっぱいまで段が伸び(高さが効くので段を増やすほど大きくなる)、
    外国人のフルネームが並ぶリストでは自然に段が減る(列幅が効くので段を減らした
    方が大きくなる)。同点なら段数の多い方=詰まった方を採る。

    「段数を最大化」ではなく「文字サイズを最大化」なのは、列幅で頭打ちになる段数を
    選ぶと本文が小さいうえに行数も減って、boxの上下が大きく余ってしまうため。
    """
    if not items:
        return 1, _column_fit_px(draw, [], el, 1, w, h, frame_h, font_path)
    top = max(1, min(el.columns, -(-len(items) // _COLUMN_MIN_ROWS)))
    best = (0, 1)
    for cols in range(top, 0, -1):
        px = _column_fit_px(draw, items, el, cols, w, h, frame_h, font_path)
        if px > best[0]:
            best = (px, cols)
    return best[1], best[0]


def _layout_columns(
    draw: ImageDraw.ImageDraw,
    text: str,
    el: TextElement,
    box: tuple[int, int, int, int],
    frame_h: int,
    font_path: Path | None,
    pad: int,
) -> tuple[list[_Placed], float]:
    """改行区切りの各行を段に割り付ける(列優先=上から下へ、次の列へ)。

    エンドロールの単語一覧のように「短い語がたくさん」並ぶときは、流し込みの
    1段落より段組みの方が読める。行の高さは全列で共通(base_px)にして段が
    ガタつかないようにし、列幅からはみ出す語だけ _fit_line で縮める。

    段数は el.columns を上限に、いちばん大きく組める段数を自動で選ぶ。駅名の
    ような短い語だけなら上限まで段が伸び、外国人のフルネームが混ざるリストでは
    列幅が足りず段が減る(_choose_columns)。
    """
    x, y, w, h = box
    x, y = x + pad, y + pad
    w, h = max(1, w - 2 * pad), max(1, h - 2 * pad)
    items = [t for t in text.split("\n") if t.strip()]
    cols, base_px = _choose_columns(draw, items, el, w, h, frame_h, font_path)
    col_w, gap = _column_geometry(w, cols)
    rows = -(-len(items) // cols) if items else 1
    line_h = base_px * 1.25
    total_h = line_h * rows
    if el.valign == "top":
        ty = float(y)
    elif el.valign == "bottom":
        ty = y + h - total_h
    else:
        ty = y + (h - total_h) / 2
    placed: list[_Placed] = []
    for i, item in enumerate(items):
        col, row = divmod(i, rows)
        font = _fit_line(draw, item, font_path, base_px, col_w)
        cx = x + col * (col_w + gap)
        lw = draw.textlength(item, font=font)
        if el.align == "right":
            lx = cx + col_w - lw
        elif el.align == "center":
            lx = cx + (col_w - lw) / 2
        else:
            lx = cx
        # 縮んだ語も行の帯の中で縦中央に置き、ベースラインの乱れを目立たせない
        ly = ty + row * line_h + (line_h - _font_px(font, base_px) * 1.25) / 2
        placed.append((lx, ly, item, font))
    return placed, line_h


def _draw_text(
    canvas: Image.Image, text: str, el: TextElement, font_path: Path | None
) -> None:
    x, y, w, h = _box_px(el.box, canvas.width, canvas.height)
    # α付きの色(背景帯や半透明文字)を正しく合成するため一旦RGBAに描く
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    strokes = _stroke_layers(el, canvas.height)
    # 縁取り・影は文字の外側に広がるので、そのぶん内側にグリフを収める
    pad = max([px for px, _ in strokes] + [int(el.shadow * canvas.height)] + [0])
    placed: list[_Placed] = []
    if el.columns > 1:
        placed, line_h = _layout_columns(
            draw, text, el, (x, y, w, h), canvas.height, font_path, pad
        )
    else:
        font, lines, line_h = _fit_text(
            draw, text, el, w, h, canvas.height, font_path, pad=pad
        )
        total_h = int(line_h * len(lines))
        if el.valign == "top":
            ty = float(y)
        elif el.valign == "bottom":
            ty = y + h - total_h
        else:
            ty = y + (h - total_h) // 2
        ly = float(ty)
        for line in lines:
            lw = draw.textlength(line, font=font)
            if el.align == "left":
                lx = float(x)
            elif el.align == "right":
                lx = x + w - lw
            else:
                lx = x + (w - lw) / 2
            placed.append((lx, ly, line, font))
            ly += line_h
    if not placed:
        return

    if el.background:
        bpad = max(4, int(line_h * 0.2))
        left = min(lx for lx, _, _, _ in placed)
        right = max(lx + draw.textlength(t, font=f) for lx, _, t, f in placed)
        top = min(ly for _, ly, _, _ in placed)
        bottom = max(ly + _font_px(f, line_h) * 1.25 for _, ly, _, f in placed)
        draw.rectangle(
            (left - bpad, top - bpad, right + bpad, bottom + bpad), fill=el.background
        )

    stroke = int(el.stroke_width * canvas.height)
    if el.shadow:
        # 文字の形にぼかした影。矩形の帯を敷かずに文字の周りだけを暗くする
        radius = max(1, int(el.shadow * canvas.height))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        for lx, ly, line, font in placed:
            sdraw.text(
                (lx, ly), line, font=font, fill=el.shadow_color,
                stroke_width=radius, stroke_fill=el.shadow_color,
            )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
        canvas.paste(shadow, (0, 0), shadow)

    for lx, ly, line, font in placed:
        if strokes:
            # 太い順に「その色で塗った文字の輪郭」を重ね、最後に本体を乗せる。
            # 幅の差が同心の環になるので二重・三重の縁取りになる
            for px, color in strokes:
                draw.text(
                    (lx, ly), line, font=font, fill=color,
                    stroke_width=px, stroke_fill=color,
                )
            draw.text((lx, ly), line, font=font, fill=el.color)
        else:
            draw.text(
                (lx, ly), line, font=font, fill=el.color,
                stroke_width=stroke, stroke_fill=el.stroke_color,
            )
    canvas.paste(overlay, (0, 0), overlay)


def _render_canvas(
    layout: Layout,
    image_path: Path | None,
    data: dict,
    width: int,
    height: int,
    elements: list[ImageElement | TextElement],
    texts: list[str],
    background: Image.Image | None = None,
) -> Image.Image:
    if background is not None:
        # 呼び出し側が用意した下地(サムネの全面画像など)にそのまま描き足す
        canvas = background.convert("RGB").resize((width, height))
    else:
        canvas = Image.new("RGB", (width, height), layout.background)
    font_path = resolve_font_path(layout.font)
    values = _SafeDict(
        # NA等の欠損マーカーは「NA年生まれ」と描画されてしまうので空文字に潰す
        {k: ("" if is_missing(v) else v) for k, v in data.items() if v is not None}
    )
    ti = 0
    for el in elements:
        if isinstance(el, ImageElement):
            if image_path is not None and _require_met(el, values):
                try:
                    _paste_image(canvas, image_path, el)
                except Exception as e:
                    logger.warning("画像を描画できません: %s (%s)", image_path, e)
        else:
            text = texts[ti]
            ti += 1
            if text:
                _draw_text(canvas, text, el, font_path)
    return canvas


def _render_to_cache(
    layout: Layout,
    image_path: Path | None,
    data: dict,
    width: int,
    height: int,
    out_dir: Path,
    elements: list[ImageElement | TextElement],
    texts: list[str],
    raw_elements: list,
    tag: str,
) -> Path:
    """要素列とテンプレート展開済みテキストからフレームPNGを合成(同内容なら再利用)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    values = _SafeDict(
        # NA等の欠損マーカーは「NA年生まれ」と描画されてしまうので空文字に潰す
        {k: ("" if is_missing(v) else v) for k, v in data.items() if v is not None}
    )
    # 実際に描く側(通常/fallback/idle)の要素だけをキャッシュキーに使う。
    # subtitle要素はASS側で描くので内容に影響しない(除外)。require が
    # 満たされない要素も描かれないので除外し、画像のrequireも取りこぼさない
    raw_visual = {
        **layout.raw,
        "elements": [
            e
            for e in raw_elements
            if e.get("type") != "subtitle"
            and (not e.get("require") or str(values.get(e["require"]) or "").strip())
        ],
    }
    image_key: list[str | int] = []
    if image_path is not None:
        try:
            st = image_path.stat()
            image_key = [image_path.name, st.st_size, st.st_mtime_ns]
        except OSError:
            image_key = [image_path.name]
    key = hashlib.sha1(
        json.dumps(
            [FRAME_RENDER_CACHE_VERSION, tag, raw_visual, image_key, texts, width, height],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    out = out_dir / f"frame_{key}.png"
    if out.exists():
        # 共有キャッシュの刈り取りで、最近使ったフレームを残せるようatime代わりにする
        try:
            os.utime(out, None)
        except OSError:
            pass
        return out
    # APIの並行ジョブが同じ共有キャッシュキーを同時に描画しても、ffmpegから
    # 書きかけのPNGが見えないよう、一時ファイルを完成させてから原子的に公開する。
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        _render_canvas(layout, image_path, data, width, height, elements, texts).save(
            tmp, format="PNG"
        )
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def render_frame(
    layout: Layout,
    image_path: Path | None,
    data: dict,
    width: int,
    height: int,
    out_dir: Path,
    use_fallback: bool = False,
) -> Path | None:
    """レイアウトに従いフレームPNGを合成して返す(同内容なら既存を再利用)。"""
    data = _with_app_credit(data)
    elements = list(layout.active_elements(use_fallback))
    texts = layout.render_texts(data, use_fallback)
    # 画像クレジットの自動焼き込み(文言が空の単語=表記不要では描かない)。
    # textsに文言が入るのでフレームキャッシュのキーにも自然に効く
    if layout.credit is not None:
        credit_text = _element_texts([layout.credit], data)[0]
        if credit_text:
            credit_el = layout.credit
            # boxより写真が狭いと帯が背景上に浮くので、実表示領域の右下に寄せる
            image_el = next((e for e in elements if isinstance(e, ImageElement)), None)
            if image_path is not None and image_el is not None:
                fitted = fitted_image_box(image_path, image_el.box, width, height)
                if fitted is not None:
                    credit_el = replace(credit_el, box=fitted)
            elements.append(credit_el)
            texts.append(credit_text)
    # アプリのクレジット(全レイアウト共通・左下)
    if layout.app_credit is not None:
        elements.append(layout.app_credit)
        texts.append(_element_texts([layout.app_credit], data)[0])
    raw_key = "fallback" if (use_fallback and layout.fallback) else "elements"
    return _render_to_cache(
        layout, image_path, data, width, height, out_dir,
        elements, texts, layout.raw.get(raw_key, []), tag=raw_key,
    )


def render_image(
    layout: Layout,
    image_path: Path | None,
    data: dict,
    width: int,
    height: int,
    use_fallback: bool = False,
    background: Image.Image | None = None,
) -> Image.Image:
    """レイアウトに従って1枚のPillow画像を合成して返す(ファイルには書かない)。

    render_frame と違いフレームキャッシュを使わないので、保存先とファイル名を
    呼び出し側が決めたいとき(サムネ画像など)に使う。
    background を渡すと単色ではなくその画像を下地にする(サムネの全面画像)。
    """
    data = _with_app_credit(data)
    elements = list(layout.active_elements(use_fallback))
    texts = layout.render_texts(data, use_fallback)
    if layout.app_credit is not None:
        elements.append(layout.app_credit)
        texts.append(_element_texts([layout.app_credit], data)[0])
    return _render_canvas(
        layout, image_path, data, width, height, elements, texts, background
    )


def render_section_frame(
    layout: Layout,
    data: dict,
    width: int,
    height: int,
    out_dir: Path,
    section: str = "idle",
) -> Path | None:
    """歌唱なし区間(前奏・間奏・後奏)に出すフレームPNG。

    section に IDLE_SECTIONS の種別を渡すとその専用定義で描き、専用定義が
    無ければ従来の idle 要素にフォールバックする。要素もアプリクレジットも
    無ければ None(呼び出し側は黒画面のまま)。要素が無くてもクレジットが
    有効なら、クレジットだけを載せた背景色のフレームを返す(間奏でだけ表記が
    消えないように)。単語画像はないので image要素を書いても描かれない
    (プロジェクトレベルの固定文言向け)。
    """
    section_elements, raw_elements, tag = layout.section_elements(section)
    if not section_elements and layout.app_credit is None:
        return None
    data = _with_app_credit(data)
    elements = list(section_elements)
    texts = _element_texts(elements, data)
    # 区間側が自分で {app_credit} を並べているときは自動追加しない(二重表示になる)
    own_credit = any(
        isinstance(el, TextElement) and "{app_credit}" in el.template
        for el in section_elements
    )
    if layout.app_credit is not None and not own_credit:
        elements.append(layout.app_credit)
        texts.append(_element_texts([layout.app_credit], data)[0])
    return _render_to_cache(
        layout, None, data, width, height, out_dir,
        elements, texts, raw_elements, tag=tag,
    )


def render_idle_frame(
    layout: Layout, data: dict, width: int, height: int, out_dir: Path
) -> Path | None:
    """歌唱なし区間の既定(idle)フレーム。区間種別を問わない従来の入口。"""
    return render_section_frame(layout, data, width, height, out_dir, "idle")
