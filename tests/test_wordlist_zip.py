"""自作の単語リスト(CSV+画像のzipアップロード)のテスト。

- wordlist_zip: 画像の結びつけ方(image列 / 表記と同じ名前)と、弾くべきzip
- wordlist_zip.parse_parts: zipにまとめず「テキスト+画像ファイル」で来た場合
- /api/wordlist-check: 画像の枚数まで返す
- /api/jobs: 画像がジョブ内に展開され、CSVの image 列が実体のパスになる
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from soramimic_video import wordlist_csv as wc
from soramimic_video import wordlist_zip as wz

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"


# ---- zipを組み立てる道具 ----


def make_zip(entries: dict[str, bytes]) -> bytes:
    """{名前: 中身} からzipのバイト列を作る。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class Cp932Info(zipfile.ZipInfo):
    """Windowsの「圧縮フォルダー」と同じ書き方(UTF-8フラグ無しのcp932名)をするZipInfo。"""

    def _encodeFilenameFlags(self):  # type: ignore[override]
        return self.filename.encode("cp932"), self.flag_bits


def png_bytes(color: str = "red", size: tuple[int, int] = (8, 6)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(color: str = "blue", size: tuple[int, int] = (8, 6), exif: bytes = b"") -> bytes:
    buf = io.BytesIO()
    im = Image.new("RGB", size, color)
    if exif:
        im.save(buf, format="JPEG", exif=exif)
    else:
        im.save(buf, format="JPEG")
    return buf.getvalue()


def webp_bytes(color: str = "green", size: tuple[int, int] = (8, 6)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="WEBP")
    return buf.getvalue()


def image_column(text: str) -> list[str]:
    """正規化済みCSVの image 列(無ければ空リスト)。"""
    rows = [line.split(",") for line in text.splitlines()]
    if "image" not in rows[0]:
        return []
    i = rows[0].index("image")
    return [row[i] if i < len(row) else "" for row in rows[1:]]


# ---- 画像の結びつけ ----


def test_tidy_image_column_and_name_convention():
    out = wz.parse(
        make_zip(
            {
                "words.csv": (
                    "surface,pronunciation,image\n"
                    "たなか,タナカ,tanaka.jpg\n"     # image列で明示
                    "田中太郎,タロウ,\n"              # 表記と同じ名前の画像が自動で当たる
                    "ねこ,ネコ,\n"                    # 画像なしの行(そのまま文字だけ)
                ).encode(),
                "tanaka.jpg": jpeg_bytes(),
                "田中太郎.png": png_bytes(),
            }
        )
    )
    assert out.csv.columns == ["id", "original", "surface", "pronunciation", "image"]
    names = image_column(out.csv.text)
    assert names[0].startswith("img_") and names[0].endswith(".jpg")
    assert names[1].startswith("img_") and names[1].endswith(".png")
    assert names[2] == ""
    # 返す画像は保存名で引ける再エンコード済みのバイト列
    assert set(out.images) == {names[0], names[1]}
    assert out.image_count == 2
    assert Image.open(io.BytesIO(out.images[names[0]])).format == "JPEG"
    assert out.csv.image_rows == 2
    assert out.summary()["images"] == 2


def test_explicit_column_wins_over_the_name_convention():
    out = wz.parse(
        make_zip(
            {
                "words.csv": "surface,image\nねこ,いぬ.png\n".encode(),
                "ねこ.png": png_bytes("red"),
                "いぬ.png": png_bytes("blue"),
            }
        )
    )
    # 「いぬ.png」を書いた行に「ねこ.png」が当たってはいけない
    assert out.image_count == 1
    used = image_column(out.csv.text)[0]
    assert Image.open(io.BytesIO(out.images[used])).getpixel((0, 0))[2] > 200


def test_original_column_is_used_for_the_convention():
    out = wz.parse(
        make_zip(
            {
                "words.csv": "original,surface,pronunciation\n田中太郎,たなか,タナカ\n".encode(),
                "田中太郎.png": png_bytes(),
            }
        )
    )
    assert out.image_count == 1
    assert image_column(out.csv.text)[0] in out.images


def test_plain_style_matches_images_by_surface():
    out = wz.parse(
        make_zip(
            {
                "list.txt": "ネコ\n東京,トウキョウ,トーキョー\n".encode(),
                "東京.jpg": jpeg_bytes(),
            }
        )
    )
    assert out.csv.style == "plain"
    # 同じ語に読みが複数ある行は、どちらにも同じ画像が付く
    names = image_column(out.csv.text)
    assert names[0] == ""
    assert names[1] and names[1] == names[2]


def test_no_image_column_when_nothing_matches():
    out = wz.parse(
        make_zip({"list.csv": "ネコ,ネコ\n".encode(), "いぬ.png": png_bytes()})
    )
    assert out.csv.columns == ["id", "original", "surface", "pronunciation"]
    assert out.csv.text.splitlines()[1] == "1,ネコ,ネコ,ネコ"
    # どの行からも参照されなかった画像はジョブに持ち込まない
    assert out.images == {}


def test_same_image_is_stored_once():
    same = png_bytes()
    out = wz.parse(
        make_zip(
            {
                "words.csv": "surface,image\nねこ,a.png\nいぬ,b.png\n".encode(),
                "a.png": same,
                "b.png": same,
            }
        )
    )
    names = image_column(out.csv.text)
    assert names[0] == names[1]
    assert out.image_count == 1


def test_image_page_column_is_still_dropped():
    out = wz.parse(
        make_zip(
            {
                "words.csv": "surface,image,image_page\nねこ,a.png,http://example.com/\n".encode(),
                "a.png": png_bytes(),
            }
        )
    )
    assert out.csv.dropped_columns == ["image_page"]
    assert "image_page" not in out.csv.columns


def test_windows_cp932_filenames_are_repaired():
    """Windowsの圧縮フォルダーはcp932のまま名前を書く。化けたままだと自動マッチが外れる。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(Cp932Info("表記.csv"), "surface\n田中太郎\n".encode())
        zf.writestr(Cp932Info("田中太郎.png"), png_bytes())
    # zipfile自身はUTF-8フラグが無い名前をcp437として読む(=化ける)
    assert "田中太郎.png" not in zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    out = wz.parse(buf.getvalue())
    assert out.image_count == 1
    assert image_column(out.csv.text)[0] in out.images


def test_subdirectories_are_flattened_to_the_basename():
    out = wz.parse(
        make_zip(
            {
                "mylist/words.csv": "surface,image\nねこ,neko.png\n".encode(),
                "mylist/images/neko.png": png_bytes(),
            }
        )
    )
    assert out.image_count == 1


def test_macos_metadata_entries_are_ignored():
    out = wz.parse(
        make_zip(
            {
                "words.csv": "surface,image\nねこ,neko.png\n".encode(),
                "neko.png": png_bytes(),
                "__MACOSX/._neko.png": b"\x00\x05\x16\x07garbage",
                ".DS_Store": b"garbage",
            }
        )
    )
    assert out.image_count == 1


# ---- 再エンコード ----


def test_jpeg_is_reencoded_and_loses_its_exif():
    exif = Image.Exif()
    exif[0x010E] = "taken at home"  # ImageDescription(EXIFのASCII欄なので日本語は入らない)
    raw = jpeg_bytes(exif=exif.tobytes())
    assert Image.open(io.BytesIO(raw)).getexif().get(0x010E) == "taken at home"
    out = wz.parse(make_zip({"w.csv": "surface,image\nねこ,a.jpg\n".encode(), "a.jpg": raw}))
    saved = out.images[image_column(out.csv.text)[0]]
    assert saved != raw
    assert not Image.open(io.BytesIO(saved)).getexif()


def test_webp_is_stored_as_png():
    out = wz.parse(
        make_zip({"w.csv": "surface,image\nねこ,a.webp\n".encode(), "a.webp": webp_bytes()})
    )
    name = image_column(out.csv.text)[0]
    assert name.endswith(".png")
    assert Image.open(io.BytesIO(out.images[name])).format == "PNG"


def test_pixel_limit(monkeypatch):
    monkeypatch.setattr(wz, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "surface,image\nねこ,a.png\n".encode(), "a.png": png_bytes()}))
    assert "画素数" in str(exc.value)


def test_broken_image_is_rejected_with_its_name():
    broken = png_bytes()[:20] + b"\x00" * 40
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "surface\nねこ\n".encode(), "a.png": broken}))
    assert "a.png" in str(exc.value)


# ---- 弾くべきzip ----


def test_zip_without_csv_is_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"a.png": png_bytes()}))
    assert "CSV" in str(exc.value)


def test_two_csvs_are_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"a.csv": "ねこ\n".encode(), "b.txt": "いぬ\n".encode()}))
    assert "2つ以上" in str(exc.value)


def test_unknown_image_reference_names_the_row():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(
            make_zip(
                {
                    "w.csv": "surface,image\nねこ,neko.png\nいぬ,inu.png\n".encode(),
                    "neko.png": png_bytes(),
                }
            )
        )
    assert "3行目" in str(exc.value) and "inu.png" in str(exc.value)


def test_image_url_is_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(
            make_zip(
                {
                    "w.csv": "surface,image\nねこ,http://example.com/a.png\n".encode(),
                    "a.png": png_bytes(),
                }
            )
        )
    assert "ファイル名" in str(exc.value)


def test_image_path_is_rejected():
    with pytest.raises(wc.WordlistCsvError):
        wz.parse(
            make_zip(
                {
                    "w.csv": "surface,image\nねこ,../a.png\n".encode(),
                    "a.png": png_bytes(),
                }
            )
        )


def test_path_traversal_entry_is_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "../evil.png": png_bytes()}))
    assert ".." in str(exc.value)


def test_absolute_path_entry_is_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "/etc/evil.png": png_bytes()}))
    assert "絶対パス" in str(exc.value)


def test_windows_path_entry_is_rejected():
    with pytest.raises(wc.WordlistCsvError):
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "C:\\tmp\\evil.png": png_bytes()}))


def test_symlink_entry_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("w.csv", "ねこ\n".encode())
        link = zipfile.ZipInfo("neko.png")
        link.external_attr = (0o120777 << 16)  # S_IFLNK
        zf.writestr(link, "/etc/passwd")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(buf.getvalue())
    assert "シンボリックリンク" in str(exc.value)


def test_svg_is_rejected():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "a.svg": svg}))
    assert "SVG" in str(exc.value)


def test_svg_disguised_as_png_is_rejected():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "a.png": svg}))
    assert "SVG" in str(exc.value)


def test_non_image_binary_is_rejected_by_name():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "song.mid": FAKE_MIDI}))
    assert "song.mid" in str(exc.value)


def test_gif_is_rejected_with_its_format():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="GIF")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode(), "a.png": buf.getvalue()}))
    assert "GIF" in str(exc.value)


def test_duplicate_image_names_are_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(
            make_zip(
                {
                    "w.csv": "ねこ\n".encode(),
                    "a/ねこ.png": png_bytes(),
                    "b/ねこ.jpg": jpeg_bytes(),
                }
            )
        )
    assert "ねこ" in str(exc.value)


def test_per_image_size_limit(monkeypatch):
    monkeypatch.setenv(wz.MAX_IMAGE_BYTES_ENV, "16")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "surface,image\nねこ,a.png\n".encode(), "a.png": png_bytes()}))
    assert "大きすぎ" in str(exc.value) and "a.png" in str(exc.value)


def test_image_count_limit(monkeypatch):
    monkeypatch.setenv(wz.MAX_IMAGES_ENV, "1")
    entries = {"w.csv": "ねこ\n".encode(), "a.png": png_bytes("red"), "b.png": png_bytes("blue")}
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip(entries))
    assert "多すぎ" in str(exc.value)


def test_zip_size_limit(monkeypatch):
    monkeypatch.setenv(wz.MAX_ZIP_BYTES_ENV, "32")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": "ねこ\n".encode()}))
    assert "大きすぎ" in str(exc.value)


def test_csv_size_limit_inside_the_zip(monkeypatch):
    monkeypatch.setenv(wc.MAX_BYTES_ENV, "16")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(make_zip({"w.csv": ("ネコ,ネコ\n" * 10).encode()}))
    assert "大きすぎ" in str(exc.value)


def test_broken_zip_is_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse(b"PK\x03\x04" + b"garbage" * 10)
    assert "zip" in str(exc.value)


# ---- CSV単体のときは今までどおり ----


def test_plain_csv_upload_still_drops_the_image_column():
    out = wz.parse_upload(
        "surface,image,image_page\nねこ,http://example.com/a.png,http://example.com/\n".encode()
    )
    assert out.images == {}
    assert out.csv.dropped_columns == ["image", "image_page"]
    assert out.csv.columns == ["id", "original", "surface", "pronunciation"]
    assert out.summary()["images"] == 0


def test_zip_is_detected_by_magic_not_by_name():
    zip_bytes = make_zip({"w.csv": "ねこ\n".encode(), "ねこ.png": png_bytes()})
    assert wz.looks_like_zip(zip_bytes)
    assert not wz.looks_like_zip("ねこ,ネコ\n".encode())
    assert wz.parse_upload(zip_bytes).image_count == 1


def test_used_images_survive_values_with_quotes():
    """値に「"」が残る語(正規化はクオートを潰さない)があっても、参照済み画像を
    間違って捨てないこと。csv.readerで読み直すと引用符扱いで列がずれる回帰テスト。"""
    zip_bytes = make_zip(
        {
            # 入力の「"""ネコ"」はCSVとしては『"ネコ』(先頭にクオートが残る値)
            "words.csv": 'surface,pronunciation,image\n"""ネコ",ネコ,neko.png\n'.encode(),
            "neko.png": png_bytes(),
            "sonota.png": png_bytes("yellow"),  # 参照されない画像(捨てられるほう)
        }
    )
    wl = wz.parse(zip_bytes)
    assert wl.image_count == 1
    assert wl.csv.image_rows == 1


# ---- zipにまとめない入口(貼り付けテキスト + 画像ファイル) ----


def test_parse_parts_binds_images_like_a_zip():
    out = wz.parse_parts(
        (
            "surface,pronunciation,image\n"
            "たなか,タナカ,tanaka.jpg\n"     # image列で明示
            "田中太郎,タロウ,\n"              # 表記と同じ名前の画像が自動で当たる
            "ねこ,ネコ,\n"                    # 画像なしの行
        ).encode(),
        {"tanaka.jpg": jpeg_bytes(), "田中太郎.png": png_bytes()},
    )
    names = image_column(out.csv.text)
    assert names[0].endswith(".jpg") and names[1].endswith(".png") and names[2] == ""
    assert out.image_count == 2
    assert out.csv.image_rows == 2


def test_parse_parts_uses_only_the_basename():
    """ブラウザがフォルダ付きの名前を送ってきても、参照はファイル名だけで通る。"""
    out = wz.parse_parts(
        "ねこ\n".encode(), {"わたしの写真/ねこ.png": png_bytes()}
    )
    assert out.image_count == 1


def test_parse_parts_rejects_duplicate_names():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse_parts(
            "ねこ\n".encode(),
            {"a/ねこ.png": png_bytes("red"), "b/ねこ.png": png_bytes("blue")},
        )
    assert "同じ名前" in str(exc.value) and "ねこ.png" in str(exc.value)


def test_parse_parts_rejects_duplicate_stems():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse_parts("ねこ\n".encode(), {"ねこ.png": png_bytes(), "ねこ.jpg": jpeg_bytes()})
    assert "拡張子違い" in str(exc.value)


def test_parse_parts_rejects_a_broken_image():
    """画像の中身の検査はzipと同じ道を通る(SVGや壊れた画像はここでも弾かれる)。"""
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse_parts("ねこ\n".encode(), {"ねこ.png": b"<svg xmlns='...'></svg>"})
    assert "SVG" in str(exc.value)


def test_parse_parts_per_image_size_limit(monkeypatch):
    monkeypatch.setenv(wz.MAX_IMAGE_BYTES_ENV, "16")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse_parts("ねこ\n".encode(), {"ねこ.png": png_bytes()})
    assert "大きすぎ" in str(exc.value) and "ねこ.png" in str(exc.value)


def test_parse_parts_image_count_limit(monkeypatch):
    monkeypatch.setenv(wz.MAX_IMAGES_ENV, "1")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse_parts(
            "ねこ\n".encode(), {"a.png": png_bytes("red"), "b.png": png_bytes("blue")}
        )
    assert "多すぎ" in str(exc.value)


def test_parse_parts_total_size_limit(monkeypatch):
    """全体の上限はzipに合わせる(zipにまとめれば通らない量を素通しにしない)。"""
    monkeypatch.setenv(wz.MAX_ZIP_BYTES_ENV, "32")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wz.parse_parts("ねこ\n".encode(), {"ねこ.png": png_bytes()})
    assert "大きすぎ" in str(exc.value)


# ---- API ----

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "video" / "song.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    return TestClient(app)


def _sample_zip() -> bytes:
    return make_zip(
        {
            "words.csv": "surface,pronunciation,image\nねこ,ネコ,neko.png\n田中,タナカ,\n".encode(),
            "neko.png": png_bytes(),
            "田中.jpg": jpeg_bytes(),
        }
    )


def test_wordlist_check_returns_the_image_count(client):
    res = client.post(
        "/api/wordlist-check",
        files={"wordlist_csv": ("わたしの単語.zip", _sample_zip(), "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rows"] == 2
    assert body["images"] == 2
    assert body["image_rows"] == 2
    assert body["name"] == "わたしの単語"  # .zip も .csv と同じく落とす


def test_wordlist_check_rejects_a_broken_zip(client):
    res = client.post(
        "/api/wordlist-check",
        files={"wordlist_csv": ("mine.zip", make_zip({"a.png": png_bytes()}), "application/zip")},
    )
    assert res.status_code == 400
    assert "CSV" in res.json()["detail"]


def test_image_paths_are_absolute_even_with_a_relative_jobs_dir(tmp_path: Path, monkeypatch):
    """cli既定の --jobs-dir work/api-jobs のような相対パスでも、CSVには絶対パスを書く。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_mod, "run_pipeline", lambda job, config: None)
    client = TestClient(api_mod.create_app(jobs_dir=Path("rel-jobs")))
    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("mine.zip", _sample_zip(), "application/zip"),
        },
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    saved = (tmp_path / "rel-jobs" / job_id / "wordlist" / "mine.csv").read_text(encoding="utf-8")
    for value in image_column(saved):
        assert Path(value).is_absolute() and Path(value).exists()


def test_job_stores_the_images_and_rewrites_the_image_column(client, tmp_path: Path):
    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("わたしの単語.zip", _sample_zip(), "application/zip"),
        },
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    params = client.get(f"/api/jobs/{job_id}").json()["params"]
    assert params["wordlist"] == "わたしの単語"
    assert params["wordlist_csv"] == "わたしの単語.csv"
    assert params["wordlist_images"] == 2

    wl_dir = tmp_path / "jobs" / job_id / api_mod.WORDLIST_DIRNAME
    saved = (wl_dir / "わたしの単語.csv").read_text(encoding="utf-8")
    values = image_column(saved)
    assert len(values) == 2
    for value in values:
        path = Path(value)
        # video.download_image が「://を含まない値」をローカルパスとして取り込む
        assert path.is_absolute() and path.exists()
        assert path.parent == wl_dir / api_mod.WORDLIST_IMAGES_DIRNAME
        assert Image.open(path).size == (8, 6)


def test_quoted_values_survive_the_image_rewrite(client, tmp_path: Path):
    """表記に「"」が残っていても、画像パスへの書き換えで他の列が崩れないこと。"""
    zip_bytes = make_zip(
        {
            "words.csv": 'surface,pronunciation,image\n"""ネコ",ネコ,neko.png\n'.encode(),
            "neko.png": png_bytes(),
        }
    )
    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("mine.zip", zip_bytes, "application/zip"),
        },
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    wl_dir = tmp_path / "jobs" / job_id / api_mod.WORDLIST_DIRNAME
    saved = (wl_dir / "mine.csv").read_text(encoding="utf-8")
    cells = saved.splitlines()[1].split(",")
    # id,original,surface,pronunciation,image の5列のまま、クオートも保たれる
    assert cells[1] == '"ネコ' and cells[2] == '"ネコ'
    path = Path(cells[4])
    assert path.is_absolute() and path.exists()


def test_job_with_a_plain_csv_has_no_images_dir(client, tmp_path: Path):
    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("mine.csv", "ネコ,ネコ\n".encode(), "text/csv"),
        },
    )
    job_id = res.json()["id"]
    wl_dir = tmp_path / "jobs" / job_id / api_mod.WORDLIST_DIRNAME
    assert not (wl_dir / api_mod.WORDLIST_IMAGES_DIRNAME).exists()
    assert "image" not in (wl_dir / "mine.csv").read_text(encoding="utf-8")
    assert "wordlist_images" not in client.get(f"/api/jobs/{job_id}").json()["params"]


def test_wordlist_check_accepts_text_and_images(client):
    res = client.post(
        "/api/wordlist-check",
        data={"wordlist_text": "ねこ,ネコ\n田中,タナカ\n"},
        files=[
            ("wordlist_images", ("ねこ.png", png_bytes(), "image/png")),
            ("wordlist_images", ("田中.jpg", jpeg_bytes(), "image/jpeg")),
        ],
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rows"] == 2
    assert body["images"] == 2
    assert body["name"] == "custom"   # リスト名を書かなければ既定の名前


def test_wordlist_check_uses_the_given_list_name(client):
    res = client.post(
        "/api/wordlist-check",
        data={"wordlist_text": "ねこ,ネコ\n", "wordlist_name": "わたしの単語"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "わたしの単語"
    assert res.json()["images"] == 0


def test_wordlist_check_rejects_both_sources(client):
    res = client.post(
        "/api/wordlist-check",
        data={"wordlist_text": "ねこ,ネコ\n"},
        files={"wordlist_csv": ("mine.csv", "いぬ,イヌ\n".encode(), "text/csv")},
    )
    assert res.status_code == 400
    assert "どちらか一方" in res.json()["detail"]


def test_wordlist_check_rejects_an_empty_request(client):
    res = client.post("/api/wordlist-check", data={"wordlist_text": "   "})
    assert res.status_code == 400
    assert "ありません" in res.json()["detail"]


def test_job_with_text_and_images_stores_the_images(client, tmp_path: Path):
    res = client.post(
        "/api/jobs",
        data={"wordlist_text": "ねこ,ネコ\n田中,タナカ\n", "wordlist_name": "わたしの単語"},
        files=[
            ("midi", ("song.mid", FAKE_MIDI, "audio/midi")),
            ("wordlist_images", ("ねこ.png", png_bytes(), "image/png")),
            ("wordlist_images", ("田中.jpg", jpeg_bytes(), "image/jpeg")),
        ],
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    params = client.get(f"/api/jobs/{job_id}").json()["params"]
    assert params["wordlist"] == "わたしの単語"
    assert params["wordlist_images"] == 2

    wl_dir = tmp_path / "jobs" / job_id / api_mod.WORDLIST_DIRNAME
    saved = (wl_dir / "わたしの単語.csv").read_text(encoding="utf-8")
    values = image_column(saved)
    assert len(values) == 2
    for value in values:
        path = Path(value)
        assert path.is_absolute() and path.exists()
        assert path.parent == wl_dir / api_mod.WORDLIST_IMAGES_DIRNAME


def test_job_with_text_only(client, tmp_path: Path):
    res = client.post(
        "/api/jobs",
        data={"wordlist_text": "ねこ,ネコ\n"},
        files={"midi": ("song.mid", FAKE_MIDI, "audio/midi")},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    params = client.get(f"/api/jobs/{job_id}").json()["params"]
    assert params["wordlist"] == "custom"   # リスト名なしは既定の名前
    assert "wordlist_images" not in params
    wl_dir = tmp_path / "jobs" / job_id / api_mod.WORDLIST_DIRNAME
    assert "ねこ" in (wl_dir / "custom.csv").read_text(encoding="utf-8")
    assert not (wl_dir / api_mod.WORDLIST_IMAGES_DIRNAME).exists()


def test_job_with_a_broken_text_wordlist_is_rejected(client):
    res = client.post(
        "/api/jobs",
        data={"wordlist_text": "surface,pronunciation\nねこ,ネコ\n", "wordlist_name": "x"},
        files=[
            ("midi", ("song.mid", FAKE_MIDI, "audio/midi")),
            ("wordlist_images", ("ねこ.svg", b"<svg xmlns='...'></svg>", "image/svg+xml")),
        ],
    )
    assert res.status_code == 400
    assert "SVG" in res.json()["detail"]


def test_config_exposes_the_zip_limits(client):
    conf = client.get("/api/config").json()
    assert conf["max_wordlist_zip_bytes"] == wz.DEFAULT_MAX_ZIP_BYTES
    assert conf["max_wordlist_image_bytes"] == wz.DEFAULT_MAX_IMAGE_BYTES
    assert conf["max_wordlist_images"] == wz.DEFAULT_MAX_IMAGES
