"""歌詞テキストのカナ読み変換。

ベースは soramimic-yomi(pyopenjtalk-plus + ユーザー辞書 + 英語カナ変換)。
読みは発音形(は→ワ、トーキョー式の長音)で、CTCアライメントの音響と整合する。
フォールバック/読み候補生成用に MeCab + unidic-lite の発音形も使う。

読みの誤りはアライメント先の取り違えとして行全体に伝播する
(例: ipadicの「二人」→ニニン誤読)。エンジン間で読みが割れた行は
音響スコアで判定できるよう、行ごとの候補読みを返す reading_candidates を提供する。

読みエンジン(soramimic-yomi / unidic)は青空文庫ルビ記法(``｜表層《よみ》``)を
知らないので、この層で ruby.segments により「素の断片」と「強制読みの断片」に
割り、前者だけを読みエンジンに掛ける。記法を含まない入力では割らずにそのまま
1回で掛けるので、従来と完全に同じ読みになる。
"""

from __future__ import annotations

import csv
import logging
import re
from typing import Any

import jaconv

from . import ruby
from .kana import normalize_long_vowels

logger = logging.getLogger(__name__)

_KATAKANA_RE = re.compile(r"[ァ-ヶー]+")
_PRON_FIELD = 9  # unidic: 発音形(出現形)

_tagger: Any = None
_yomi_available: bool | None = None


def _get_tagger() -> Any:
    global _tagger
    if _tagger is None:
        try:
            import MeCab
            import unidic_lite
        except ImportError as e:
            raise RuntimeError(
                "mecab-python3 / unidic-lite がインストールされていません"
                "(uv sync --extra audio)"
            ) from e
        _tagger = MeCab.Tagger("-d " + unidic_lite.DICDIR)
    return _tagger


def _feature_fields(feature: str) -> list[str]:
    """unidicのfeature文字列をパースする(引用符内のカンマを含むフィールドがある)。"""
    return next(csv.reader([feature]))


def _kana_only(text: str) -> str:
    return "".join(_KATAKANA_RE.findall(text))


def _forced_kana(reading: str) -> str:
    """ルビ注釈の読みをカタカナに揃える(エンジン側で既にカタカナだが念のため)。"""
    return _kana_only(jaconv.hira2kata(reading))


def _kana_with_ruby(text: str, to_kana: Any) -> str | None:
    """ルビ注釈を尊重して行全体のカタカナ読みを作る。

    to_kana は「素テキスト → カタカナ読み or None」。記法を含まない入力は
    割らずにそのまま to_kana へ渡す(従来と完全に同じ読み)。
    どれか1断片でも読みが取れなければ(None)行全体を None にする。
    """
    parts = ruby.segments(text)
    if len(parts) == 1 and parts[0][1] is None:
        return to_kana(parts[0][0])
    out: list[str] = []
    for chunk, forced in parts:
        if forced is not None:
            out.append(_forced_kana(forced))
            continue
        kana = to_kana(chunk)
        if kana is None:
            return None
        out.append(kana)
    return "".join(out)


def _unidic_kana(text: str) -> str:
    """MeCab + unidic-lite の発音形によるカタカナ読み(素テキスト前提)。"""
    node = _get_tagger().parseToNode(text)
    parts: list[str] = []
    while node:
        if node.surface:
            fields = _feature_fields(node.feature)
            reading = (
                fields[_PRON_FIELD]
                if len(fields) > _PRON_FIELD and fields[_PRON_FIELD] not in ("", "*")
                else None
            )
            if reading is None:
                # 未知語: 既にカナならそのまま読みにする
                reading = jaconv.hira2kata(node.surface)
                if not _KATAKANA_RE.fullmatch(reading):
                    logger.warning("読みが取れないため無視: %r", node.surface)
            parts.append(reading)
        node = node.next
    return _kana_only("".join(parts))


def text_to_kana_unidic(text: str) -> str:
    """MeCab + unidic-lite の発音形によるカタカナ読み(ルビ記法対応)。"""
    return _kana_with_ruby(text, _unidic_kana) or ""


def _yomi_tokens(text: str) -> list[tuple[str, str]]:
    """soramimic-yomi の (表層形, カタカナ読み) トークン列(素テキスト前提)。"""
    import soramimic_yomi  # 遅延import(未導入環境で既存機能を壊さない)

    tokens: list[tuple[str, str]] = []
    for tok in soramimic_yomi.get_tokens(text):
        surface = tok.get("surface_form", "")
        if not surface:
            continue
        reading = tok.get("reading") or tok.get("pronunciation") or ""
        tokens.append((surface, _kana_only(reading)))
    return tokens


def reading_tokens(text: str) -> list[tuple[str, str]]:
    """soramimic-yomi で (表層形, カタカナ発音) のトークン列に分割する。

    元歌詞のフレーズ切り出し(align.split_lyric_to_phrases)で、表層位置と読みの
    対応を取るために使う。get_tokens はデフォルトで表層を保持する(位置写像に好都合)。
    読みは reading(表層準拠。は→ハ 等)を採る。XFカナも表層準拠のことが多く、
    長音のゆれ(ヨウ/ヨー)は突き合わせ側の正規化で吸収するため、pronunciation
    (は→ワ)よりも取りこぼしが少ない。soramimic-yomi 未インストールなら
    ImportError(呼び出し側で按分にフォールバック)。

    ルビ記法つきのテキストを渡すと、注釈区間は1トークン(表層=素テキスト,
    読み=指定読み)にまとめて返す。表層は plain 座標なので、呼び出し側が
    素テキスト(strip_ruby 済み)を持っていれば位置写像がそのまま通る。
    """
    parts = ruby.segments(text)
    if len(parts) == 1 and parts[0][1] is None:
        return _yomi_tokens(parts[0][0])
    tokens: list[tuple[str, str]] = []
    for chunk, forced in parts:
        if forced is not None:
            tokens.append((chunk, _forced_kana(forced)))
        else:
            tokens.extend(_yomi_tokens(chunk))
    return tokens


def _yomi_kana(text: str) -> str | None:
    """soramimic-yomi によるカタカナ読み(素テキスト前提)。未インストールなら None。"""
    global _yomi_available
    if _yomi_available is False:
        return None
    try:
        import soramimic_yomi
    except ImportError:
        if _yomi_available is None:
            logger.warning(
                "soramimic-yomi が無いため unidic-lite の読みを使います"
                "(英語・数字の読みが弱くなります)"
            )
        _yomi_available = False
        return None
    _yomi_available = True
    return _kana_only(soramimic_yomi.get_yomi(text))


def text_to_kana_yomi(text: str) -> str | None:
    """soramimic-yomi によるカタカナ読み(ルビ記法対応)。未インストールなら None。"""
    return _kana_with_ruby(text, _yomi_kana)


def text_to_kana(text: str) -> str:
    """漢字かな交じりの歌詞1行をカタカナ読みにする(yomi優先、unidicフォールバック)。

    ルビ記法(``｜表層《よみ》``)を含む行では、注釈区間の読みが優先される。
    """
    return text_to_kana_yomi(text) or text_to_kana_unidic(text)


def reading_candidates(text: str) -> list[str]:
    """行の読み候補(重複除去済み、第1候補が既定)。

    yomi と unidic の発音形が長音正規化後も異なる場合のみ複数候補になる。
    候補が複数の行は音響スコア(CTC)で判定する(mora_align.align_moras_with_variants)。
    ルビ注釈のある区間は両エンジンで同じ(指定)読みになるので、候補は増えない。
    """
    yomi = text_to_kana_yomi(text)
    unidic = text_to_kana_unidic(text)
    candidates = [k for k in (yomi, unidic) if k]
    unique: list[str] = []
    seen: set[str] = set()
    for k in candidates:
        norm = normalize_long_vowels(k)
        if norm not in seen:
            seen.add(norm)
            unique.append(k)
    return unique
