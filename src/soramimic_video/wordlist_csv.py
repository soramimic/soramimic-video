"""自作の単語リスト(アップロードCSV)の検証と正規化。

Web UI / API から受け取ったユーザー製の単語リストを、変換エンジン(soramimic)が
そのまま読める tidy CSV へ揃えるための入口。エンジンの CSV パーサは
``split(",")`` ベースでクオートも BOM も扱えず、列名も ``id/original/surface/
pronunciation`` の完全一致しか見ない(soramimic/word_list.py)。そのため

* 文字コード(UTF-8/BOM付き/Shift_JIS)の判別
* ヘッダの揺れ(BOM・前後空白・大文字小文字・日本語の列名)の吸収
* 引用符つきCSVの解釈と、値に含まれるカンマ・改行の除去
* ``id`` / ``original`` 列の補完

をここで済ませ、あとはエンジンに素直に渡せる文字列にして返す。
壊れた入力は :class:`WordlistCsvError` で「どこが駄目か」を日本語で返す
(/api/midi-check と同じで、ジョブを走らせる前に弾くための材料にする)。

受け付ける書き方は2通り:

1. tidy CSV — ヘッダ行に ``surface``(表記)があるもの。``pronunciation``(読み)
   ほか任意の列を足せる。既存の external/soramimic-wordlists と同じ形。
2. かんたん形式 — ヘッダ無しで1行1語 ``表記,読み1,読み2,...``。読みは省略可
   (省略すると表記から自動推定)。``#`` 以降は行コメント。
   本家 soramimic の「自作の単語リストを使用」と同じ書き方。
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from dataclasses import dataclass, field

# 上限。既定はどちらも「手で作ったリスト」には十分すぎる大きさで、
# 行数の上限は変換前処理(読み推定+バリエーション展開)が現実的な時間で
# 終わる範囲に切っている(1万行で数分かかる)。
MAX_BYTES_ENV = "SORAMIMIC_MAX_WORDLIST_BYTES"
MAX_ROWS_ENV = "SORAMIMIC_MAX_WORDLIST_ROWS"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ROWS = 10000

# 必ず出力する先頭4列(エンジンが名前で引く列)
BASE_COLUMNS = ("id", "original", "surface", "pronunciation")

# 追加列として受け取らない列。画像URLは動画生成時にサーバーが取りに行くので、
# 任意のURLを外から差し込めないようにここで落とす(自作リストは文字だけで出る)
DROPPED_COLUMNS = frozenset({"image", "image_page"})

# ヘッダの言い換え。正規化(BOM除去・前後空白除去・小文字化)したあとで引く
COLUMN_ALIASES: dict[str, str] = {
    "id": "id",
    "no": "id",
    "original": "original",
    "正式名称": "original",
    "原語": "original",
    "surface": "surface",
    "word": "surface",
    "text": "surface",
    "単語": "surface",
    "表記": "surface",
    "見出し": "surface",
    "pronunciation": "pronunciation",
    "kana": "pronunciation",
    "yomi": "pronunciation",
    "reading": "pronunciation",
    "読み": "pronunciation",
    "よみ": "pronunciation",
    "読み方": "pronunciation",
    "カナ": "pronunciation",
    "かな": "pronunciation",
    "ふりがな": "pronunciation",
}

# 読みとして許すのはカナ(ひらがな/カタカナ)と長音・繰り返し記号だけ。
# 漢字混じりでもエンジンは MeCab で読みを推定するが、推定が外れると
# 歌詞が黙って変になるので「読み欄に漢字」は投入前に知らせる
_KANA_RE = re.compile(r"^[ぁ-ゖァ-ヺーゝゞヽヾ]+$")

# エンジンのCSVパーサはクオートを解釈しないので、値の中のカンマ・改行は潰す
_INFIELD_COMMA = "、"


class WordlistCsvError(ValueError):
    """自作リストCSVとして受け付けられない入力。detail はそのままAPIの本文に出す。"""


@dataclass
class WordlistCsv:
    """検証を通った自作リスト。``text`` をそのまま .csv として保存すれば変換に使える。"""

    text: str
    rows: int
    columns: list[str]
    dropped_columns: list[str] = field(default_factory=list)
    auto_reading_rows: int = 0
    style: str = "tidy"  # "tidy" | "plain"
    samples: list[dict[str, str]] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """正規化後の中身の指紋。中身が変われば別のリストとして扱うためのキー。"""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict[str, object]:
        """APIが返す要約(UIの「◯語を読み込みました」表示に使う)。"""
        return {
            "ok": True,
            "rows": self.rows,
            "columns": self.columns,
            "dropped_columns": self.dropped_columns,
            "auto_reading_rows": self.auto_reading_rows,
            "style": self.style,
            "samples": self.samples,
            "fingerprint": self.fingerprint,
        }


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def max_bytes() -> int:
    return _env_int(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)


def max_rows() -> int:
    return _env_int(MAX_ROWS_ENV, DEFAULT_MAX_ROWS)


def decode(data: bytes) -> str:
    """UTF-8(BOM有無)→ Shift_JIS の順に解釈する。

    Excel で「CSV(カンマ区切り)」として保存すると Shift_JIS になるので、
    そこで詰まらないようフォールバックを持つ。
    """
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise WordlistCsvError(
        "文字コードが読めません。UTF-8(BOM付きも可)かShift_JISで保存してください。"
    )


def normalize_column(name: str) -> str:
    """列名の揺れを均す。BOM・前後空白・全角空白を落とし、ASCIIは小文字に。"""
    cleaned = name.replace("﻿", "").strip().strip("　").strip()
    return COLUMN_ALIASES.get(cleaned.lower(), cleaned.lower() if cleaned.isascii() else cleaned)


def _clean_value(value: str) -> str:
    """1セルの値をエンジンが読める形に均す(カンマ・改行・前後空白を落とす)。"""
    value = value.replace("﻿", "").replace("\r", " ").replace("\n", " ")
    return value.replace(",", _INFIELD_COMMA).strip()


def _split_rows(text: str) -> list[list[str]]:
    """引用符つきCSVとして行に分ける。"""
    try:
        return [row for row in csv.reader(io.StringIO(text, newline=""))]
    except csv.Error as exc:  # pragma: no cover - csv.reader が落ちるのは稀
        raise WordlistCsvError(f"CSVとして読めません: {exc}") from exc


def _looks_like_header(row: list[str]) -> bool:
    """先頭行が tidy CSV のヘッダかどうか。

    surface に当たる列があれば当然ヘッダ。surface が無くても id/original/
    pronunciation のような既知の列名が並んでいればヘッダのつもりだと見なし、
    「かんたん形式の1語目」として黙って取り込まずに surface 欠落エラーへ倒す。
    """
    names = {normalize_column(cell) for cell in row}
    return bool(names & set(BASE_COLUMNS))


def _check_reading(pronunciation: str, surface: str, line_no: int, bad: list[str]) -> None:
    if pronunciation and not _KANA_RE.fullmatch(pronunciation):
        bad.append(f"{line_no}行目「{surface}」の読み「{pronunciation}」")


def _parse_plain(rows: list[list[str]]) -> tuple[list[str], list[list[str]], int]:
    """かんたん形式(1行1語 ``表記,読み1,読み2``)を tidy の行列に変換する。

    id は行(=語)ごとに1つ振る。同じ語に読みを複数書いたときは id を共有する
    行が並ぶ形になり、既存の単語リスト(1つの id に表記が複数)と同じ構造になる。
    本家 soramimic は surface に読みのほうを入れるが、こちらは字幕に出るのが
    surface なので表記を入れる(読みだけ複数、表示は同じ表記)。
    """
    out: list[list[str]] = []
    bad: list[str] = []
    auto = 0
    word_id = 0
    for line_no, row in enumerate(rows, start=1):
        # 「#」以降は行末までコメント(本家と同じ)。全部コメントなら空行と同じ
        cells = [_clean_value(c) for c in row]
        for i, cell in enumerate(cells):
            if "#" in cell:
                cells = [*cells[:i], cell.split("#", 1)[0].strip()]
                break
        if not cells or not cells[0]:
            continue
        surface = cells[0]
        readings = [c for c in cells[1:] if c]
        word_id += 1
        if not readings:
            auto += 1
            out.append([str(word_id), surface, surface, ""])
            continue
        for reading in readings:
            _check_reading(reading, surface, line_no, bad)
            out.append([str(word_id), surface, surface, reading])
    if bad:
        raise WordlistCsvError(_reading_error(bad))
    return list(BASE_COLUMNS), out, auto


def _reading_error(bad: list[str]) -> str:
    head = "、".join(bad[:5])
    more = f" ほか{len(bad) - 5}件" if len(bad) > 5 else ""
    return (
        f"読みはカタカナ(またはひらがな)で書いてください: {head}{more}。"
        "読みを空にすると表記から自動で推定します。"
    )


def _parse_tidy(
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]], int, list[str]]:
    header = [normalize_column(c) for c in rows[0]]
    # 同名の列は先に出たほうを採用する(エンジンの h2i も後勝ちで壊れるため)
    keep: list[tuple[int, str]] = []
    seen: set[str] = set()
    dropped: list[str] = []
    for i, name in enumerate(header):
        if not name or name in seen:
            continue
        if name in DROPPED_COLUMNS:
            dropped.append(name)
            continue
        seen.add(name)
        keep.append((i, name))
    if "surface" not in seen:
        raise WordlistCsvError(
            "surface(表記)の列がありません。1行目に列名を書き、"
            "表記の列を surface(または「単語」「表記」)にしてください。"
            f" 見つかった列: {', '.join(n for n in header if n) or '(なし)'}"
        )

    extras = [name for _, name in keep if name not in BASE_COLUMNS]
    columns = list(BASE_COLUMNS) + extras
    idx = {name: i for i, name in keep}
    out: list[list[str]] = []
    bad: list[str] = []
    auto = 0
    for line_no, row in enumerate(rows[1:], start=2):
        cells = [_clean_value(c) for c in row]

        def cell(name: str, cells: list[str] = cells) -> str:
            i = idx.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        surface = cell("surface")
        if not surface:
            continue  # 空行・表記なしの行は黙って捨てる(末尾の空行を許すため)
        pronunciation = cell("pronunciation")
        if pronunciation in ("NA", "na"):
            pronunciation = ""
        if not pronunciation:
            auto += 1
        _check_reading(pronunciation, surface, line_no, bad)
        values = [
            cell("id") or str(len(out) + 1),
            cell("original") or surface,
            surface,
            pronunciation,
        ]
        values += [cell(name) for name in extras]
        out.append(values)
    if bad:
        raise WordlistCsvError(_reading_error(bad))
    return columns, out, auto, sorted(set(dropped))


def parse(data: bytes) -> WordlistCsv:
    """アップロードされたCSVを検証して、正規化済みの tidy CSV を返す。

    受け付けられない入力は :class:`WordlistCsvError` を送出する。
    """
    limit = max_bytes()
    if len(data) > limit:
        raise WordlistCsvError(
            f"ファイルが大きすぎます({len(data) / 1024 / 1024:.1f}MB、"
            f"上限は{limit / 1024 / 1024:.1f}MBです)。"
        )
    if not data.strip():
        raise WordlistCsvError("ファイルが空です。")

    rows = [row for row in _split_rows(decode(data)) if any(c.strip() for c in row)]
    if not rows:
        raise WordlistCsvError("中身がありません。1行に1語ずつ書いてください。")

    if _looks_like_header(rows[0]):
        style = "tidy"
        columns, body, auto, dropped = _parse_tidy(rows)
    else:
        style = "plain"
        columns, body, auto = _parse_plain(rows)
        dropped = []

    if not body:
        raise WordlistCsvError("単語が1つもありません。表記の列を埋めてください。")
    row_limit = max_rows()
    if len(body) > row_limit:
        raise WordlistCsvError(
            f"単語が多すぎます({len(body)}行、上限は{row_limit}行です)。"
            "行を減らしてからもう一度お試しください。"
        )

    # 末尾に改行を付けない。エンジンのCSVパーサは行を素朴に split するので、
    # 最終行の空文字が「列の足りない行」として IndexError になる
    # (external/soramimic-wordlists の既存CSVも末尾改行なしで揃っている)
    text = "\n".join([",".join(columns), *(",".join(values) for values in body)])
    samples = [dict(zip(columns, values, strict=True)) for values in body[:3]]
    return WordlistCsv(
        text=text,
        rows=len(body),
        columns=columns,
        dropped_columns=dropped,
        auto_reading_rows=auto,
        style=style,
        samples=samples,
    )
