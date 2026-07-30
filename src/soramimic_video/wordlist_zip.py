"""自作の単語リスト(zipアップロード)の取り込みと検証。

CSV単体では単語画像を付けられない(wordlist_csv.DROPPED_COLUMNS が image 列を
落としている。任意のURLをサーバーに取りに行かせないため)。zipなら画像の実体が
中に入っているので、URLを一切許さないまま「自作リストにも画像を出す」ができる。

zipの中身は「CSV 1枚 + 画像(PNG/JPEG/WebP)」。行と画像の結びつけ方は2通り:

1. CSVの ``image`` 列にzip内のファイル名を書く(``tanaka.jpg``)。URLは受け付けない。
2. 何も書かなければ、``original``(正式名称。かんたん形式では表記)と同じ名前の
   画像を自動で当てる(``田中太郎`` の行に ``田中太郎.jpg``)。列に書いたほうが優先。

外から来たzipなので、エントリ名(traversal・絶対パス・symlink)、展開後のサイズ、
画像の中身(magic → Pillowで再エンコード)まで見てから受け取る。通った画像は
``img_<sha1>.png/.jpg`` に名前を付け替えて返し、ジョブ側(api.JobManager.create)が
ジョブディレクトリへ書き出して image 列を実体のパスに差し替える。動画生成側
(video.download_image)は「``://`` を含まない値」をローカルパスとして取り込むので、
描画側には手を入れずに画像が出る。

zip内のサブディレクトリは受け取るが、参照に使うのはファイル名(basename)だけ。
その代わり、名前がぶつかる画像が2つ以上あるzipは「どっちの画像か」が決まらないので
受け取らない。単語リストは .csv と .txt のどちらでもよい(かんたん形式は .txt で
書かれることが多い)が、そのぶん readme.txt のような添え物は入れられない
(「リストが2つある」として断る)。

zipにまとめてもらう代わりに「貼り付けたテキスト + 画像ファイルを複数選択」でも
同じことができる(:func:`parse_parts`)。zipを作れない/作りたくない人のための入口で、
画像の検証・保存名付け・行との結びつけ・上限は :func:`parse` と同じものを通る。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import zipfile
from collections.abc import Container, Mapping
from dataclasses import dataclass, field

from PIL import Image

from .wordlist_csv import WordlistCsv, WordlistCsvError, _env_int, _image_stem, max_bytes
from .wordlist_csv import parse as parse_csv

# 上限。zip全体は「写真を数十枚入れたリスト」が通る程度、1枚あたりはスマホの
# 写真がそのまま入る程度に切っている。どれも展開後のバイト数で見る
MAX_ZIP_BYTES_ENV = "SORAMIMIC_MAX_WORDLIST_ZIP_BYTES"
MAX_IMAGE_BYTES_ENV = "SORAMIMIC_MAX_WORDLIST_IMAGE_BYTES"
MAX_IMAGES_ENV = "SORAMIMIC_MAX_WORDLIST_IMAGES"
DEFAULT_MAX_ZIP_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_IMAGES = 1000

# 画素数の上限。ファイルは小さいのに展開すると巨大になる画像(展開爆弾)は、
# Pillowが実データを読む前(load()の前)にここで弾く
MAX_IMAGE_PIXELS = 50_000_000

# zipのシグネチャ。空zip(05 06)・分割zip(07 08)も「zipのつもり」として拾って、
# CSVとして読めない旨ではなくzipとしての理由を返す
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# CSVとして受け取る拡張子(.txt もかんたん形式のリストとして扱う)と、画像の拡張子
CSV_EXTS = (".csv", ".txt")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# 画像のmagic。ここに無い形式は名前を添えて断る
_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"%PDF", "PDF"),
    (b"PK\x03\x04", "zip"),
)


@dataclass
class WordlistZip:
    """検証を通った自作リスト。CSV単体でアップロードされたときは images が空。"""

    csv: WordlistCsv
    images: dict[str, bytes] = field(default_factory=dict)

    @property
    def image_count(self) -> int:
        return len(self.images)

    def summary(self) -> dict[str, object]:
        """APIが返す要約。CSVの要約に画像の枚数を足しただけ。"""
        return {**self.csv.summary(), "images": self.image_count}


def max_zip_bytes() -> int:
    return _env_int(MAX_ZIP_BYTES_ENV, DEFAULT_MAX_ZIP_BYTES)


def max_image_bytes() -> int:
    return _env_int(MAX_IMAGE_BYTES_ENV, DEFAULT_MAX_IMAGE_BYTES)


def max_images() -> int:
    return _env_int(MAX_IMAGES_ENV, DEFAULT_MAX_IMAGES)


def looks_like_zip(data: bytes) -> bool:
    """先頭4バイトがzipのシグネチャかどうか。中身が壊れていても True になりうる。"""
    return data[:4] in ZIP_MAGIC


def _mb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f}MB"


def _entry_name(info: zipfile.ZipInfo) -> str:
    """エントリ名を、人が付けたはずの名前に戻す。

    Windowsの「圧縮フォルダー」はUTF-8フラグ(0x800)を立てずcp932のまま名前を書くので、
    zipfileはそれをcp437として読んでしまい日本語が化ける。フラグが立っていないものだけ
    バイト列に戻してcp932で読み直す(``表記.jpg`` の自動マッチにはこれが要る)。
    cp932として読めなければ諦めて元の名前を使う。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _check_safe_name(name: str) -> None:
    """展開先を外に逃がせる名前を弾く。実際に展開しなくても名前だけで断る。"""
    if "\\" in name:
        raise WordlistCsvError(
            f"zipの中に使えないファイル名があります: {name}。"
            "「\\」を含む名前は受け付けられません。"
        )
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise WordlistCsvError(
            f"zipの中に絶対パスのファイルがあります: {name}。"
            "フォルダごとではなく、中身を選んで圧縮してください。"
        )
    if ".." in name.split("/"):
        raise WordlistCsvError(
            f"zipの中に「..」を含むファイルがあります: {name}。"
            "そのままだとzipの外に展開されるので受け付けられません。"
        )


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _skipped(name: str) -> bool:
    """中身と関係ないエントリ(macOSのメタデータや隠しファイル)かどうか。"""
    parts = name.split("/")
    return "__MACOSX" in parts or parts[-1].startswith(".")


def _read_limited(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, name: str) -> bytes:
    """展開しながら limit+1 バイトだけ読む。

    中央ディレクトリに書いてあるサイズは自己申告なので信用せず、実際に読めた量で見る
    (小さいzipが展開すると巨大になる、いわゆるzip爆弾よけ)。
    """
    with zf.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise WordlistCsvError(
            f"zipの中の「{name}」が大きすぎます(上限は1ファイル{_mb(limit)}です)。"
        )
    return data


def _image_format(data: bytes) -> str | None:
    """先頭バイトから見た形式名。分からなければ None。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    for magic, label in _MAGICS:
        if data.startswith(magic):
            return label
    return None


def _reencode(data: bytes, name: str, where: str = "zipの中") -> tuple[str, bytes]:
    """画像を検証して開き直し、(拡張子, 保存するバイト列) を返す。

    そのまま保存すると「PNGに見えるが実は別物」やEXIFの位置情報まで持ち込むので、
    Pillowで開いて描き直したものだけを通す。JPEGはJPEGのまま、PNG/WebPはPNGにする。
    ``where`` は理由文の頭に付く入口の呼び名(zipの中 / 選んだファイル)。
    """
    # SVGはPillowで開けず、中にスクリプトや外部参照を書ける。自作リストでは受け取らない
    from .video import looks_like_svg  # 描画側の一式を読み込まないようここで import

    if looks_like_svg(data):
        raise WordlistCsvError(
            f"{where}の「{name}」はSVGです。自作リストの画像はPNG・JPEG・WebPだけです。"
        )
    fmt = _image_format(data)
    if fmt not in ("PNG", "JPEG", "WebP"):
        found = f"({fmt})" if fmt else ""
        raise WordlistCsvError(
            f"{where}の「{name}」は画像として読めません{found}。"
            "画像はPNG・JPEG・WebPで入れてください。"
        )
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_IMAGE_PIXELS:
                raise WordlistCsvError(
                    f"{where}の「{name}」は画素数が多すぎます"
                    f"({im.width}x{im.height}、上限は{MAX_IMAGE_PIXELS}画素です)。"
                )
            im.load()
            out = io.BytesIO()
            if fmt == "JPEG":
                im.convert("RGB").save(out, format="JPEG", quality=90)
                return "jpg", out.getvalue()
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            im.convert("RGBA" if has_alpha else "RGB").save(out, format="PNG")
            return "png", out.getvalue()
    except WordlistCsvError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillowが投げる例外は形式ごとにばらばら
        raise WordlistCsvError(
            f"{where}の「{name}」は画像として開けません(壊れているかもしれません)。"
        ) from exc


def _check_image_count(count: int, where: str) -> None:
    """枚数の上限。zipでは展開を始める前に、複数選択では受け取った数で見る。"""
    limit = max_images()
    if count > limit:
        raise WordlistCsvError(f"{where}の画像が多すぎます({count}枚、上限は{limit}枚です)。")


def _reject_duplicate_image(
    base: str, seen: Container[str], stems: Mapping[str, str], where: str
) -> None:
    """名前がぶつかる画像を弾く(zipでも、画像を複数選んだときでも理由は同じ)。

    行と画像を結びつけるのに使うのはファイル名(basename)だけなので、同じ名前や
    拡張子違いの同名が2つあると「どちらの画像か」が決まらない。
    """
    if base in seen:
        raise WordlistCsvError(
            f"{where}に同じ名前の画像が複数あります: {base}。"
            "フォルダを分けても名前で見分けるので、名前を変えてください。"
        )
    stem = _image_stem(base)
    if stem in stems:
        raise WordlistCsvError(
            f"{where}に拡張子違いの同じ名前の画像があります({stems[stem]} / {base})。"
            "どちらを使うか決まらないので、名前を変えてください。"
        )


def _collect_entries(
    zf: zipfile.ZipFile,
) -> tuple[zipfile.ZipInfo | None, dict[str, zipfile.ZipInfo]]:
    """zipのエントリを (CSV, {ファイル名: 画像}) に仕分ける。名前の検査もここで済ませる。"""
    csv_info: zipfile.ZipInfo | None = None
    csv_name = ""
    images: dict[str, zipfile.ZipInfo] = {}
    stems: dict[str, str] = {}
    for info in zf.infolist():
        name = _entry_name(info)
        if info.is_dir():
            continue
        _check_safe_name(name)
        if _is_symlink(info):
            raise WordlistCsvError(
                f"zipの中にシンボリックリンクがあります: {name}。"
                "画像は実体のファイルとして入れてください。"
            )
        if _skipped(name):
            continue
        base = name.rsplit("/", 1)[-1]
        ext = os.path.splitext(base)[1].lower()
        if ext in CSV_EXTS:
            if csv_info is not None:
                raise WordlistCsvError(
                    f"zipの中に単語リストのファイルが2つ以上あります({csv_name} / {name})。"
                    "CSV(.csv または .txt)は1つだけにしてください。"
                )
            csv_info, csv_name = info, name
            continue
        if ext == ".svg":
            raise WordlistCsvError(
                f"zipの中の「{name}」はSVGです。自作リストの画像はPNG・JPEG・WebPだけです。"
            )
        if ext not in IMAGE_EXTS:
            raise WordlistCsvError(
                f"zipの中に単語リストにも画像にも使えないファイルがあります: {name}。"
                "CSV(.csv/.txt)1つとPNG・JPEG・WebPの画像だけを入れてください。"
            )
        _reject_duplicate_image(base, images, stems, "zipの中")
        images[base] = info
        stems[_image_stem(base)] = base
    return csv_info, images


def parse(data: bytes) -> WordlistZip:
    """アップロードされたzip(CSV1枚+画像)を検証して、CSVと画像を返す。

    受け付けられない入力は :class:`WordlistCsvError` を送出する(CSV単体のときと
    同じ例外なので、APIは同じ except で400にできる)。
    """
    limit = max_zip_bytes()
    if len(data) > limit:
        raise WordlistCsvError(
            f"ファイルが大きすぎます({_mb(len(data))}、上限は{_mb(limit)}です)。"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise WordlistCsvError(
            "zipファイルとして読めません(壊れているかもしれません)。"
            "作り直すか、CSVだけをアップロードしてください。"
        ) from exc
    with zf:
        csv_info, image_infos = _collect_entries(zf)
        if csv_info is None:
            raise WordlistCsvError(
                "zipの中に単語リストのCSVがありません。"
                "CSV(.csv または .txt)1つと画像を同じzipに入れてください。"
            )
        # 枚数は展開を始める前に見る(何万枚も入ったzipを読んでから断らない)
        _check_image_count(len(image_infos), "zipの中")
        csv_bytes = _read_limited(zf, csv_info, max_bytes(), _entry_name(csv_info))
        byte_limit = max_image_bytes()
        raw_images = {
            base: _read_limited(zf, info, byte_limit, base)
            for base, info in image_infos.items()
        }
    return _build(csv_bytes, raw_images, "zipの中")


def _build(csv_bytes: bytes, raw_images: Mapping[str, bytes], where: str) -> WordlistZip:
    """CSVと画像(ファイル名 → 中身)から、検証済みの自作リストを組み立てる。

    zipで来ても(:func:`parse`)、テキスト+画像ファイルで来ても(:func:`parse_parts`)
    ここに合流する。画像の検証・保存名付け・行との結びつけ・使われなかった画像の
    切り落としは、どちらの入口でも同じ。
    """
    _check_image_count(len(raw_images), where)
    byte_limit = max_image_bytes()
    # 保存名(img_<中身のsha1>.png)にすると、同じ画像を何度入れても1つにまとまる
    images: dict[str, bytes] = {}
    image_map: dict[str, str] = {}
    by_digest: dict[str, str] = {}
    for base, raw in raw_images.items():
        if len(raw) > byte_limit:
            raise WordlistCsvError(
                f"{where}の「{base}」が大きすぎます(上限は1ファイル{_mb(byte_limit)}です)。"
            )
        digest = hashlib.sha1(raw).hexdigest()[:16]
        saved = by_digest.get(digest)
        if saved is None:
            ext, blob = _reencode(raw, base, where)
            saved = f"img_{digest}.{ext}"
            images[saved] = blob
            by_digest[digest] = saved
        # CSVからは「ファイル名」でも「拡張子を落とした名前」でも引けるようにする
        image_map[base] = saved
        image_map.setdefault(_image_stem(base), saved)

    parsed = parse_csv(csv_bytes, image_map=image_map)
    # どの行からも参照されなかった画像はジョブに置かない(ジョブディレクトリを太らせない)
    used = _used_images(parsed.text)
    return WordlistZip(csv=parsed, images={k: v for k, v in images.items() if k in used})


def parse_parts(csv_bytes: bytes, files: Mapping[str, bytes]) -> WordlistZip:
    """貼り付けたテキストと、別々に選ばれた画像を、zipのときと同じ形で取り込む。

    zipを作らなくても画像付きの自作リストを書けるようにするための入口
    (WebUIの「自作の単語リスト」モーダルがこちらを使う)。``files`` のキーは
    ブラウザから来たファイル名で、行との結びつけに使うのはそのbasenameだけ(zipと同じ)。

    上限もzipに合わせる。1枚あたりと枚数はそのまま、全体は「zipにまとめていたら
    通っていたか」で見たいので CSV+画像の合計を :func:`max_zip_bytes` と比べる。
    """
    total_limit = max_zip_bytes()
    total = len(csv_bytes) + sum(len(raw) for raw in files.values())
    if total > total_limit:
        raise WordlistCsvError(
            f"入力が大きすぎます({_mb(total)}、上限は{_mb(total_limit)}です)。"
        )
    where = "選んだファイル"
    raw_images: dict[str, bytes] = {}
    stems: dict[str, str] = {}
    for name, raw in files.items():
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not base:
            continue
        _reject_duplicate_image(base, raw_images, stems, where)
        raw_images[base] = raw
        stems[_image_stem(base)] = base
    return _build(csv_bytes, raw_images, where)


def _used_images(text: str) -> set[str]:
    """正規化済みCSVの image 列に実際に入った保存名。

    テキストはクオート無しの ",".join なので、csvモジュールではなくエンジンと同じ
    split(",") で読む(値に「"」が残っていてもフィールドがずれないように)。
    """
    lines = text.splitlines()
    if not lines:
        return set()
    header = lines[0].split(",")
    if "image" not in header:
        return set()
    i = header.index("image")
    return {c[i] for c in (line.split(",") for line in lines[1:]) if i < len(c) and c[i]}


def parse_upload(data: bytes) -> WordlistZip:
    """アップロードされたファイルを、zipかCSVかを見分けて取り込む。

    APIの入口(/api/jobs と /api/wordlist-check)はどちらもこれを呼ぶ。
    zipかどうかは拡張子やcontent-typeではなく先頭のシグネチャで決める。
    """
    if looks_like_zip(data):
        return parse(data)
    return WordlistZip(csv=parse_csv(data))
