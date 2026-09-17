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
import threading
import unicodedata
from typing import Any

import jaconv

from . import ruby, runproc
from .kana import normalize_long_vowels, split_moras, vowel_of

logger = logging.getLogger(__name__)

_KATAKANA_RE = re.compile(r"[ァ-ヶー]+")
_PRON_FIELD = 9  # unidic: 発音形(出現形)
_UNIDIC_NBEST_PATHS = 8
_UNIDIC_MAX_READINGS = 2
_LATIN_RE = re.compile(r"[A-Za-z\uff21-\uff3a\uff41-\uff5a]")
_STRUCTURED_RE = re.compile(
    r"[0-9\uff10-\uff19]+|[&+#%@=\u00d7\u00f7\uff06\uff0b\uff03\uff05\uff20\uff1d]"
)
_DIGIT_READINGS = {
    "0": "\u30bc\u30ed",
    "1": "\u30a4\u30c1",
    "2": "\u30cb",
    "3": "\u30b5\u30f3",
    "4": "\u30e8\u30f3",
    "5": "\u30b4",
    "6": "\u30ed\u30af",
    "7": "\u30ca\u30ca",
    "8": "\u30cf\u30c1",
    "9": "\u30ad\u30e5\u30fc",
}
_SYMBOL_READINGS = {
    "&": "\u30a2\u30f3\u30c9",
    "+": "\u30d7\u30e9\u30b9",
    "#": "\u30b7\u30e3\u30fc\u30d7",
    "%": "\u30d1\u30fc\u30bb\u30f3\u30c8",
    "@": "\u30a2\u30c3\u30c8",
    "=": "\u30a4\u30b3\u30fc\u30eb",
    "\u00d7": "\u30ab\u30b1\u30eb",
    "\u00f7": "\u30ef\u30eb",
}

_tagger: Any = None
_tagger_lock = threading.Lock()
_yomi_available: bool | None = None


def _get_tagger() -> Any:
    global _tagger
    if _tagger is None:
        try:
            import MeCab
            import unidic_lite
        except ImportError as e:
            raise RuntimeError(
                "mecab-python3 / unidic-lite がインストールされていません(uv sync --extra audio)"
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


def _unidic_node_reading(node: Any, *, warn_unknown: bool = True) -> tuple[tuple[str, ...], str]:
    """MeCab の1解析経路から表層分割と UniDic 発音形を返す。"""
    surfaces: list[str] = []
    parts: list[str] = []
    while node:
        if node.surface:
            surfaces.append(node.surface)
            fields = _feature_fields(node.feature)
            reading = (
                fields[_PRON_FIELD]
                if len(fields) > _PRON_FIELD and fields[_PRON_FIELD] not in ("", "*")
                else None
            )
            if reading is None:
                # 未知語: 既にカナならそのまま読みにする
                reading = jaconv.hira2kata(node.surface)
                if warn_unknown and not _KATAKANA_RE.fullmatch(reading):
                    logger.warning("読みが取れないため無視: %r", node.surface)
            parts.append(reading)
        node = node.next
    return tuple(surfaces), _kana_only("".join(parts))


def _unidic_kana_candidates(text: str) -> list[str]:
    """MeCab + unidic-lite の上位解析経路から異なる読みを返す。"""
    readings: list[str] = []
    seen: set[str] = set()
    best_surfaces: tuple[str, ...] | None = None
    # parseNBestInit/nextNode は Tagger 内に列挙状態を持つ。同一プロセスの
    # 並行解析で経路が混ざらないよう、列挙全体を直列化する。
    with _tagger_lock:
        tagger = _get_tagger()
        if not tagger.parseNBestInit(text):
            return []
        for path_index in range(_UNIDIC_NBEST_PATHS):
            node = tagger.nextNode()
            if node is None:
                break
            surfaces, kana = _unidic_node_reading(node, warn_unknown=path_index == 0)
            if best_surfaces is None:
                best_surfaces = surfaces
            # N-best には「沈/むよう」のような別の単語分割も含まれる。
            # ここで必要なのは同じ表記トークンの読み違いなので、分割が
            # 1-best と異なる経路は音響候補に混ぜない。
            if surfaces != best_surfaces:
                continue
            normalized = normalize_long_vowels(kana)
            if kana and normalized not in seen:
                seen.add(normalized)
                readings.append(kana)
                if len(readings) >= _UNIDIC_MAX_READINGS:
                    break
    return readings


def _unidic_kana(text: str) -> str:
    """MeCab + unidic-lite の1-best発音形(素テキスト前提)。"""
    with _tagger_lock:
        node = _get_tagger().parseToNode(text)
        _surfaces, kana = _unidic_node_reading(node)
    return kana


def _unidic_candidates_with_ruby(text: str) -> list[str]:
    """明示ルビを守りつつ、UniDic N-best の行読み候補を作る。"""
    parts = ruby.segments(text)
    if len(parts) == 1 and parts[0][1] is None:
        return _unidic_kana_candidates(parts[0][0])
    # 明示ルビはその区間の読みを固定する入力。ルビ前後を別々に N-best
    # 列挙すると文全体の接続コストを失うため、従来どおり1-bestだけを使う。
    kana = text_to_kana_unidic(text)
    return [kana] if kana else []


def text_to_kana_unidic(text: str) -> str:
    """MeCab + unidic-lite の発音形によるカタカナ読み(ルビ記法対応)。"""
    return _kana_with_ruby(text, _unidic_kana) or ""


_PARTICLE_PRON = {"ハ": "ワ", "ヘ": "エ", "ヲ": "オ"}
_PARTICLE_SURFACES = frozenset("はハへヘをヲ")


def particle_pronunciations(text: str) -> list[tuple[int, str]]:
    """助詞の「は/へ/を」の位置と発音形カナを返す。

    XFの歌詞カナは表記どおりで、助詞の「は」も ``ハ`` のまま入っている
    (市販のXFでも同じ)。そのまま歌うと「ボクは」が「ボクハ」になり、
    発音形で書かれた単語リストとの突き合わせでも取りこぼす。表記は変えずに
    読みだけ直したいので、置き換える位置を返す。

    戻り値は ``(text 上の文字オフセット, 発音形カナ1文字)`` の列。
    1文字→1文字なので文字オフセットの対応は恒等に保たれる。
    形態素解析器(MeCab + unidic-lite)が無ければ RuntimeError。
    """
    found: dict[int, str] = {}
    # カタカナ書きの歌詞(消失のセリフ等)は解析器が助詞と見てくれないので、
    # ひらがなに開いた写しでも解析する。カタカナ→ひらがなは1文字→1文字なので
    # オフセットはそのまま使える。
    variants = [text]
    hira = jaconv.kata2hira(text)
    if hira != text:
        variants.append(hira)
    for variant in variants:
        with _tagger_lock:
            node = _get_tagger().parseToNode(variant)
            cur = 0
            while node:
                surface = node.surface
                if surface:
                    pos = variant.find(surface, cur)
                    if pos < 0:
                        pos = cur
                    cur = pos + len(surface)
                    if len(surface) == 1 and surface in _PARTICLE_SURFACES:
                        fields = _feature_fields(node.feature)
                        pron = _PARTICLE_PRON.get(jaconv.hira2kata(surface))
                        if pron and fields and fields[0] == "助詞":
                            found.setdefault(pos, pron)
                node = node.next
    return sorted(found.items())


def _yomi_tokens(text: str) -> list[tuple[str, str]]:
    """soramimic-yomi の (表層形, カタカナ読み) トークン列(素テキスト前提)。"""
    import soramimic_yomi  # 遅延import(未導入環境で既存機能を壊さない)

    with runproc.suppress_native_output_in_public_mode():
        raw_tokens = list(soramimic_yomi.get_tokens(text))
    tokens: list[tuple[str, str]] = []
    for tok in raw_tokens:
        surface = tok.get("surface_form", "")
        if not surface:
            continue
        reading = tok.get("pronunciation") or tok.get("reading") or ""
        tokens.append((surface, _kana_only(reading)))
    return tokens


def reading_tokens(text: str) -> list[tuple[str, str]]:
    """soramimic-yomi で (表層形, カタカナ発音) のトークン列に分割する。

    元歌詞のフレーズ切り出し(align.split_lyric_to_phrases)で、表層位置と読みの
    対応を取るために使う。get_tokens はデフォルトで表層を保持する(位置写像に好都合)。
    読みは pronunciation(発音形。は→ワ 等)を採る。突き合わせ相手のXFカナも
    xfparse が助詞を発音形に直しているので、こちらを揃えないと助詞のモーラで
    フレーズの切れ目がずれる。長音のゆれ(ヨウ/ヨー)は突き合わせ側
    (align._pron_normalize)の normalize_long_vowels が両側に掛かるので吸収される。
    soramimic-yomi 未インストールなら ImportError(呼び出し側で按分にフォールバック)。

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
    with runproc.suppress_native_output_in_public_mode():
        result = soramimic_yomi.get_yomi(text)
    return _kana_only(result)


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

    yomi の既定読みと UniDic N-best の発音形を候補にする。
    N-best は上限付きで、長音正規化後の重複を除く。
    候補が複数の行は音響スコア(CTC)で判定する(mora_align.align_moras_with_variants)。
    ルビ注釈のある区間は両エンジンで同じ(指定)読みになるので、候補は増えない。
    """
    yomi = text_to_kana_yomi(text)
    unidic = _unidic_candidates_with_ruby(text)
    candidates = [k for k in [yomi, *unidic] if k]
    unique: list[str] = []
    seen: set[str] = set()
    for k in candidates:
        norm = normalize_long_vowels(k)
        if norm not in seen:
            seen.add(norm)
            unique.append(k)
    return unique


def _structured_reading(surface: str) -> str:
    normalized = unicodedata.normalize("NFKC", surface)
    if normalized.isascii() and normalized.isdigit():
        return "".join(_DIGIT_READINGS[digit] for digit in normalized)
    return _SYMBOL_READINGS.get(normalized, "")


def _structured_variant(text: str, target_indices: set[int]) -> str:
    """Build a reading while replacing only selected structured spans."""
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(_STRUCTURED_RE.finditer(text)):
        parts.append(text_to_kana(text[cursor:match.start()]))
        surface = match.group()
        if index in target_indices:
            parts.append(_structured_reading(surface))
        else:
            parts.append(text_to_kana(surface))
        cursor = match.end()
    parts.append(text_to_kana(text[cursor:]))
    return "".join(parts)


def _vowel_sequence(reading: str) -> tuple[str, ...]:
    return tuple(
        vowel
        for mora in split_moras(normalize_long_vowels(reading))
        if (vowel := vowel_of(mora)) is not None
    )


def automatic_reading_candidates(text: str) -> list[str]:
    """Conservative readings for automatically transcribed surface text.

    The dictionary-first reading is always the default. Digit strings and a small
    explicit symbol set gain deterministic spoken-form alternatives. Latin text
    otherwise stays dictionary-only. Japanese dictionary alternatives are kept
    when their vowel sequence differs, preventing consonant-only ambiguity from
    reaching the acoustic selector while allowing genuine length alternatives.
    """
    dictionary = reading_candidates(text)
    if not dictionary:
        # An empty default would make an added spoken symbol form win trivially.
        return []
    default = dictionary[0]
    structured = list(_STRUCTURED_RE.finditer(text))
    candidates = [default]
    if structured:
        for index in range(len(structured)):
            candidates.append(_structured_variant(text, {index}))
        if len(structured) > 1:
            candidates.append(_structured_variant(text, set(range(len(structured)))))
    elif not _LATIN_RE.search(text):
        default_vowels = _vowel_sequence(default)
        candidates.extend(
            candidate
            for candidate in dictionary[1:]
            if _vowel_sequence(candidate) != default_vowels
        )

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_long_vowels(candidate)
        if candidate and normalized not in seen:
            seen.add(normalized)
            unique.append(candidate)
    return unique
