"""青空文庫ルビ記法(``｜表層《よみ》``)の前処理。

エンジン(soramimic)の ``parse_ruby`` を薄く包み、video 側で必要な
「素テキスト(plain)」と「区間ごとの強制読み」を扱いやすい形にする。

video では歌詞テキストの使われ方が2通りある。

* **変換エンジンに渡す**: 記法つきのまま渡してよい。``tokenize_together`` が
  注釈区間を強制トークン(pronunciation=指定読み)にしてくれる。
* **表示・アライメントに使う**: 記法が混ざると字幕に ``｜``/``《》`` が出たり、
  表層位置(align.split_lyric_to_phrases)がズレる。こちらには plain を使う。

読みの生成(reading.py / soramimic-yomi)はルビを知らないので、
:func:`segments` で「素の断片」と「強制読みの断片」に割って、
前者だけを読みエンジンに掛ける(reading.py 側で実施)。

**記法を含まない入力は完全に従来どおり**であることを保つため、注釈が1つも
無いテキストは(エスケープ解決すらせず)元の文字列をそのまま返す。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RubySpan:
    """plain 上の区間 [start, end) に強制する読み(カタカナ)。"""

    start: int
    end: int
    reading: str


@dataclass(frozen=True)
class RubyText:
    """記法テキストを分解した結果。``spans`` が空なら記法なし。"""

    plain: str
    spans: tuple[RubySpan, ...] = ()

    @property
    def has_ruby(self) -> bool:
        return bool(self.spans)


def parse(text: str) -> RubyText:
    """記法テキストを (素テキスト, 区間注釈) に分解する。

    エンジン(soramimic)が使えない環境や解析に失敗した場合は、記法なし
    (=入力そのもの)として扱う。読みの品質は落ちるが機能は壊れない。
    """
    if not text:
        return RubyText(text)
    try:
        from soramimic import parse_ruby
    except ImportError:  # pragma: no cover - soramimic は必須依存
        return RubyText(text)
    try:
        parsed: dict[str, Any] = parse_ruby(text)
    except Exception:  # pragma: no cover - パーサは寛容なので通常起きない
        logger.warning("ルビ記法の解析に失敗したため素テキストとして扱います: %r", text)
        return RubyText(text)
    annotations = parsed.get("annotations") or []
    if not annotations:
        # 記法なし: エスケープ解決もせず入力をそのまま返す(従来動作の保存)
        return RubyText(text)
    plain = str(parsed.get("plain", text))
    spans = tuple(
        RubySpan(int(a["start"]), int(a["end"]), str(a["reading"]))
        for a in annotations
    )
    return RubyText(plain, spans)


def strip_ruby(text: str) -> str:
    """表示・アライメント用の素テキスト(記法なしならそのまま)。"""
    return parse(text).plain


def has_ruby(text: str) -> bool:
    """有効なルビ記法を含むか。"""
    return parse(text).has_ruby


def segments(text: str) -> list[tuple[str, str | None]]:
    """``(素テキストの断片, 強制読み or None)`` の列に割る。

    断片を連結すると :func:`strip_ruby` と一致する。強制読みが None の断片だけを
    読みエンジンに掛ければ、注釈区間の読みを尊重した行全体の読みが作れる。
    """
    parsed = parse(text)
    if not parsed.has_ruby:
        return [(parsed.plain, None)] if parsed.plain else []
    out: list[tuple[str, str | None]] = []
    pos = 0
    for span in parsed.spans:
        if span.start > pos:
            out.append((parsed.plain[pos:span.start], None))
        out.append((parsed.plain[span.start:span.end], span.reading))
        pos = span.end
    if pos < len(parsed.plain):
        out.append((parsed.plain[pos:], None))
    return out
