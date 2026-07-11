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
  独立に効く(通常側・fallback側どちらの要素にも書ける)
- 画像のクレジット表記: image要素のあるレイアウトでは、クレジット表記が必要な
  画像(Wikimedia CommonsでAttributionRequiredのもの。image_credit.py参照)に
  限り、出典文言({image_credit})を画像の右下に自動で焼き込む。
  "credit": false で無効化できる。位置や見た目を変えたいときは text 要素で
  {image_credit} を自分で参照すれば自動追加はされない

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
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

LAYOUTS_DIR = Path(__file__).resolve().parent / "layouts"
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


class _SafeDict(dict):
    """テンプレートにない列を空文字にする(リストごとに列構成が違うため)。"""

    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class TextElement:
    template: str
    box: tuple[float, float, float, float]
    size: float = 0.06
    color: str = "white"
    align: str = "center"  # left / center / right
    valign: str = "middle"  # top / middle / bottom
    wrap: bool = False
    stroke_width: float = 0.0
    stroke_color: str = "black"
    background: str | None = None  # テキスト背後の帯。"#00000080" のようにα付き可
    require: str | None = None  # この列が空の単語ではこの要素を出さない


@dataclass
class ImageElement:
    box: tuple[float, float, float, float]
    require: str | None = None  # この列が空の単語ではこの要素を出さない


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
    # 替え歌字幕に単語ごとのふりがな(ルビ)を付ける。ふりがなは ParodyWord.kana。
    # 第1段階では source="parody" のみ有効(元歌詞ではカナ対応付けに課題があり無視する)。
    ruby: bool = False
    ruby_size: float = 0.5  # ルビの文字サイズ(本文フォントサイズに対する比)


# subtitle要素を持たないレイアウトで使う既定の字幕(従来の下部2段と同じ見た目)
DEFAULT_SUBTITLES = [
    SubtitleElement(
        source="parody", box=(0.02, 0.77, 0.96, 0.10), size=0.065, color="white", bold=True
    ),
    SubtitleElement(source="original", box=(0.02, 0.895, 0.96, 0.05), size=0.042, color="#b8b8b8"),
]


def _require_met(el: ImageElement | TextElement, values: dict) -> bool:
    """要素の require 列が埋まっているか(未指定なら常にTrue)。"""
    if not el.require:
        return True
    return bool(str(values.get(el.require) or "").strip())


def _element_texts(elements: list[ImageElement | TextElement], data: dict) -> list[str]:
    """要素列のtextテンプレートを埋めた文字列(要素順)。imageは含まない。

    require 列が空の要素は空文字にする(描画側でスキップされる)。
    """
    values = _SafeDict({k: v for k, v in data.items() if v is not None})
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
    # "hold": "next" で単語フレームを次の歌唱まで持続する(3秒上限を外す)
    hold_next: bool = False
    # 画像クレジット({image_credit})の自動焼き込み要素。credit_textが空の単語
    # (表記不要・情報なし)では描かれない。None = 自動追加なし
    credit: TextElement | None = None
    background: str = "black"
    font: str | None = None
    raw: dict = field(default_factory=dict)  # フレームキャッシュのキー用に元JSONを保持

    def active_elements(self, use_fallback: bool = False) -> list[ImageElement | TextElement]:
        """描画に使う要素列。未知語(use_fallback)でfallback定義があればそちら。"""
        return self.fallback if (use_fallback and self.fallback) else self.elements

    def render_texts(self, data: dict, use_fallback: bool = False) -> list[str]:
        """text要素のテンプレートを埋めた文字列(要素順)。imageは含まない。"""
        return _element_texts(self.active_elements(use_fallback), data)


def builtin_layout_names() -> list[str]:
    return sorted(p.stem for p in LAYOUTS_DIR.glob("*.json"))


def load_layout(name_or_path: str | None) -> Layout:
    """組み込みレイアウト名(default等)またはJSONパスからレイアウトを読む。"""
    if not name_or_path:
        name_or_path = "default"
    p = Path(name_or_path)
    if p.suffix == ".json" and p.exists():
        path = p
    else:
        path = LAYOUTS_DIR / f"{name_or_path}.json"
        if not path.exists():
            builtin = ", ".join(builtin_layout_names())
            raise FileNotFoundError(
                f"レイアウトが見つかりません: {name_or_path} "
                f"(組み込み: {builtin}。またはJSONファイルのパスを指定してください)"
            )
    return parse_layout(json.loads(path.read_text(encoding="utf-8")), str(path))


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
            elements.append(ImageElement(box=box, require=e.get("require")))
        elif kind == "subtitle":
            source = e.get("source")
            if source not in ("parody", "original"):
                raise ValueError(
                    f"subtitle の source は parody / original です: {source!r} ({origin})"
                )
            subtitles.append(
                SubtitleElement(
                    source=source,
                    box=box,
                    size=float(e.get("size", 0.05)),
                    color=e.get("color", "white"),
                    align=e.get("align", "center"),
                    valign=e.get("valign", "bottom"),
                    bold=bool(e.get("bold", False)),
                    ruby=bool(e.get("ruby", False)),
                    ruby_size=float(e.get("ruby_size", 0.5)),
                )
            )
        elif kind == "text":
            elements.append(
                TextElement(
                    template=e.get("text", ""),
                    box=box,
                    size=float(e.get("size", 0.06)),
                    color=e.get("color", "white"),
                    align=e.get("align", "center"),
                    valign=e.get("valign", "middle"),
                    wrap=bool(e.get("wrap", False)),
                    stroke_width=float(e.get("stroke_width", 0)),
                    stroke_color=e.get("stroke_color", "black"),
                    background=e.get("background"),
                    require=e.get("require"),
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


def parse_layout(raw: dict, origin: str = "<layout>") -> Layout:
    """レイアウトJSON(パース済みdict)を検証してLayoutにする。originはエラー表示用。"""
    elements, subtitles = _parse_elements(raw.get("elements", []), origin)
    # fallback(未知語用)の要素。字幕は行タイミングなので通常側と共通で使い、
    # fallback側に書かれた subtitle は無視する
    fallback, _ = _parse_elements(raw.get("fallback", []), origin)
    # idle(歌唱なし区間用)の要素。単語も行タイミングもないので subtitle は無視する
    idle, _ = _parse_elements(raw.get("idle", []), origin)
    return Layout(
        elements=elements,
        subtitles=subtitles,
        fallback=fallback,
        idle=idle,
        hold_next=raw.get("hold") == "next",
        credit=_auto_credit_element(elements, fallback, raw),
        background=raw.get("background", "black"),
        font=raw.get("font"),
        raw=raw,
    )


# ---- フォント ----

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_warned_no_font = False


def resolve_font_path(layout_font: str | None) -> Path | None:
    """レイアウト指定 → 環境変数 → 既知の日本語フォント の順で探す。"""
    for cand in (layout_font, os.environ.get(FONT_ENV), *_FONT_CANDIDATES):
        if cand:
            p = Path(cand).expanduser()
            if p.exists():
                return p
    return None


def _font(path: Path | None, px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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
    img = Image.open(image_path).convert("RGB")
    scale = min(w / img.width, h / img.height)
    nw = max(1, round(img.width * scale))
    nh = max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas.paste(img, (x + (w - nw) // 2, y + (h - nh) // 2))


def _fitted_image_box(
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
):
    """boxに収まるフォントサイズ・行リスト・行送りを決める(収まるまで縮める)。"""
    px = max(_MIN_FONT_PX, int(el.size * frame_h))
    while True:
        font = _font(font_path, px)
        lines = _wrap_chars(draw, text, font, box_w) if el.wrap else text.split("\n")
        line_h = px * 1.25
        max_line_w = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if (max_line_w <= box_w and line_h * len(lines) <= box_h) or px <= _MIN_FONT_PX:
            return font, lines, line_h
        px = max(_MIN_FONT_PX, px - max(1, px // 8))


def _draw_text(
    canvas: Image.Image, text: str, el: TextElement, font_path: Path | None
) -> None:
    x, y, w, h = _box_px(el.box, canvas.width, canvas.height)
    # α付きの色(背景帯や半透明文字)を正しく合成するため一旦RGBAに描く
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font, lines, line_h = _fit_text(draw, text, el, w, h, canvas.height, font_path)
    total_h = int(line_h * len(lines))
    if el.valign == "top":
        ty = y
    elif el.valign == "bottom":
        ty = y + h - total_h
    else:
        ty = y + (h - total_h) // 2

    if el.background:
        pad = max(4, int(line_h * 0.2))
        max_w = int(max(draw.textlength(ln, font=font) for ln in lines))
        if el.align == "left":
            bx = x
        elif el.align == "right":
            bx = x + w - max_w
        else:
            bx = x + (w - max_w) // 2
        draw.rectangle(
            (bx - pad, ty - pad, bx + max_w + pad, ty + total_h + pad), fill=el.background
        )

    stroke = int(el.stroke_width * canvas.height)
    ly = float(ty)
    for line in lines:
        lw = draw.textlength(line, font=font)
        if el.align == "left":
            lx = float(x)
        elif el.align == "right":
            lx = x + w - lw
        else:
            lx = x + (w - lw) / 2
        draw.text(
            (lx, ly), line, font=font, fill=el.color,
            stroke_width=stroke, stroke_fill=el.stroke_color,
        )
        ly += line_h
    canvas.paste(overlay, (0, 0), overlay)


def _render_canvas(
    layout: Layout,
    image_path: Path | None,
    data: dict,
    width: int,
    height: int,
    elements: list[ImageElement | TextElement],
    texts: list[str],
) -> Image.Image:
    canvas = Image.new("RGB", (width, height), layout.background)
    font_path = resolve_font_path(layout.font)
    values = _SafeDict({k: v for k, v in data.items() if v is not None})
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
    values = _SafeDict({k: v for k, v in data.items() if v is not None})
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
    key = hashlib.sha1(
        json.dumps(
            [tag, raw_visual, image_path.name if image_path else "", texts, width, height],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    out = out_dir / f"frame_{key}.png"
    if out.exists():
        return out
    _render_canvas(layout, image_path, data, width, height, elements, texts).save(out)
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
                fitted = _fitted_image_box(image_path, image_el.box, width, height)
                if fitted is not None:
                    credit_el = replace(credit_el, box=fitted)
            elements.append(credit_el)
            texts.append(credit_text)
    raw_key = "fallback" if (use_fallback and layout.fallback) else "elements"
    return _render_to_cache(
        layout, image_path, data, width, height, out_dir,
        elements, texts, layout.raw.get(raw_key, []), tag=raw_key,
    )


def render_idle_frame(
    layout: Layout, data: dict, width: int, height: int, out_dir: Path
) -> Path | None:
    """歌唱なし区間(前奏・間奏・後奏)に出す idle フレームPNG。

    idle要素がなければ None(呼び出し側は黒画面のまま)。単語画像はないので
    image要素を書いても描かれない(プロジェクトレベルの固定文言向け)。
    """
    if not layout.idle:
        return None
    texts = _element_texts(layout.idle, data)
    return _render_to_cache(
        layout, None, data, width, height, out_dir,
        layout.idle, texts, layout.raw.get("idle", []), tag="idle",
    )


