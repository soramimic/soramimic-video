"""自作の単語リスト(CSVアップロード)のテスト。

- wordlist_csv: 受け入れ形式(tidy / かんたん形式)と、弾くべき入力
- /api/wordlist-check: 投入前の検査エンドポイント
- /api/jobs: アップロードしたCSVがジョブに保存され、変換でリスト名より優先される
"""

from __future__ import annotations

from pathlib import Path

import pytest

from soramimic_video import wordlist_csv as wc

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"
# client フィクスチャが api_mod.run_pipeline を差し替えるので、本物を先に押さえておく
REAL_RUN_PIPELINE = api_mod.run_pipeline


# ---- 形式の受け入れ ----


def test_tidy_csv_is_normalized():
    out = wc.parse("surface,pronunciation\nネコ,ネコ\n東京,トウキョウ\n".encode())
    assert out.style == "tidy"
    assert out.rows == 2
    # id / original はエンジンが名前で引くので、無ければ補って必ず先頭4列に入れる
    assert out.columns == ["id", "original", "surface", "pronunciation"]
    # 末尾に改行を付けない(エンジンのCSVパーサが空行で落ちる)
    assert out.text == (
        "id,original,surface,pronunciation\n1,ネコ,ネコ,ネコ\n2,東京,東京,トウキョウ"
    )


def test_tidy_csv_keeps_extra_columns_and_drops_images():
    out = wc.parse(
        "id,original,surface,pronunciation,team,image,image_page\n"
        "7,カピバラ,カピバラ,カピバラ,森,http://example.com/a.png,http://example.com/\n".encode()
    )
    assert out.columns == ["id", "original", "surface", "pronunciation", "team"]
    # 画像URLはサーバーが取りに行くので、外から差し込めないよう落とす
    assert out.dropped_columns == ["image", "image_page"]
    assert out.text.splitlines()[1] == "7,カピバラ,カピバラ,カピバラ,森"


def test_header_tolerates_bom_spaces_case_and_japanese_names():
    out = wc.parse("﻿ Surface , 読み \nねこ,ネコ\n".encode())
    assert out.columns[:4] == ["id", "original", "surface", "pronunciation"]
    assert out.text.splitlines()[1] == "1,ねこ,ねこ,ネコ"


def test_shift_jis_is_accepted():
    out = wc.parse("単語,読み\n猫,ネコ\n".encode("cp932"))
    assert out.rows == 1
    assert out.text.splitlines()[1] == "1,猫,猫,ネコ"


def test_quoted_values_lose_their_commas_and_newlines():
    # エンジンのCSVパーサはクオートを解釈しないので、値の中のカンマ・改行は潰す
    out = wc.parse('surface,pronunciation,note\n"あ,い",アイ,"x\ny"\n'.encode())
    assert out.text.splitlines()[1] == "1,あ、い,あ、い,アイ,x y"


def test_plain_style_one_word_per_line():
    out = wc.parse("ネコ\n東京,トウキョウ,トーキョー\n#まるごとコメント\n".encode())
    assert out.style == "plain"
    assert out.auto_reading_rows == 1  # 読みを省いた「ネコ」
    assert out.text.splitlines()[1:] == [
        "1,ネコ,ネコ,",              # 読み無し: エンジンが表記から推定する
        "2,東京,東京,トウキョウ",     # 同じ語に読みが複数なら id を共有する行が並ぶ
        "2,東京,東京,トーキョー",
    ]


def test_plain_style_strips_trailing_comment():
    out = wc.parse("東京,トウキョウ # 首都\n".encode())
    assert out.text.splitlines()[1] == "1,東京,東京,トウキョウ"


def test_fingerprint_changes_with_content():
    a = wc.parse("ネコ,ネコ\n".encode())
    b = wc.parse("ネコ,ネコ\nイヌ,イヌ\n".encode())
    assert a.fingerprint != b.fingerprint
    assert a.fingerprint == wc.parse("ネコ,ネコ\n".encode()).fingerprint


# ---- 弾くべき入力 ----


def test_missing_surface_column_is_rejected():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wc.parse("id,original,pronunciation\n1,x,エックス\n".encode())
    assert "surface" in str(exc.value)


def test_kanji_reading_is_rejected_with_line_numbers():
    with pytest.raises(wc.WordlistCsvError) as exc:
        wc.parse("surface,pronunciation\nネコ,ネコ\n猫,猫又\n".encode())
    assert "3行目" in str(exc.value) and "猫又" in str(exc.value)


def test_empty_file_is_rejected():
    with pytest.raises(wc.WordlistCsvError):
        wc.parse(b"   \n")


def test_size_limit(monkeypatch):
    monkeypatch.setenv(wc.MAX_BYTES_ENV, "16")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wc.parse(("ネコ,ネコ\n" * 10).encode())
    assert "大きすぎ" in str(exc.value)


def test_row_limit(monkeypatch):
    monkeypatch.setenv(wc.MAX_ROWS_ENV, "2")
    with pytest.raises(wc.WordlistCsvError) as exc:
        wc.parse("ネコ,ネコ\nイヌ,イヌ\nサル,サル\n".encode())
    assert "多すぎ" in str(exc.value)


# ---- 変換に使えること ----


def test_normalized_csv_converts_without_touching_the_shared_cache(tmp_path: Path):
    from soramimic_video import soramimic_engine as engine
    from soramimic_video.convert import convert_project
    from test_convert import _tiny_project

    csv_path = tmp_path / "custom.csv"
    csv_path.write_text(
        wc.parse("静岡,シズオカ\n鈴鹿,スズカ\n".encode()).text, encoding="utf-8"
    )
    engine.clear_db_cache()
    project = _tiny_project()
    convert_project(project, wordlist=str(csv_path), cache_db=False)
    assert project.parody is not None
    assert project.parody.lines[0].words, "自作リストで変換結果が空"
    # ジョブ限りのCSVは共有キャッシュに載せない(他のリストを押し出さない)
    assert len(engine._db_cache) == 0


# ---- API ----


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


def test_wordlist_check_returns_summary(client):
    res = client.post(
        "/api/wordlist-check",
        files={"wordlist_csv": (
            "わたしの単語.csv", "ネコ,ネコ\n東京,トウキョウ\n".encode(), "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["rows"] == 2
    assert body["style"] == "plain"
    assert body["name"] == "わたしの単語"
    assert body["fingerprint"]


def test_wordlist_check_rejects_broken_csv(client):
    res = client.post(
        "/api/wordlist-check",
        files={"wordlist_csv": (
            "bad.csv", "surface,pronunciation\n猫,猫又\n".encode(), "text/csv")},
    )
    assert res.status_code == 400
    assert "カタカナ" in res.json()["detail"]


def test_job_accepts_uploaded_wordlist(client, tmp_path):
    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("わたしの/単語.csv", "ネコ,ネコ\n".encode(), "text/csv"),
        },
        data={"where": "type=family"},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    body = client.get(f"/api/jobs/{job_id}").json()
    params = body["params"]
    # 表示名(履歴・サムネ・ダウンロード名)はファイル名から。区切り文字は潰す
    assert params["wordlist"] == "わたしの_単語"
    assert params["wordlist_csv"] == "わたしの_単語.csv"
    assert params["wordlist_rows"] == 1
    assert params["wordlist_fingerprint"]
    # 自作リストに絞り込みは効かないので落とす
    assert params["where"] == ""
    saved = tmp_path / "jobs" / job_id / api_mod.WORDLIST_DIRNAME / "わたしの_単語.csv"
    assert saved.read_text(encoding="utf-8").splitlines()[0] == (
        "id,original,surface,pronunciation"
    )


def test_job_rejects_broken_uploaded_wordlist(client):
    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("bad.csv", b"id,original\n1,x\n", "text/csv"),
        },
    )
    assert res.status_code == 400
    assert "surface" in res.json()["detail"]


def test_run_pipeline_prefers_the_uploaded_wordlist(client, tmp_path, monkeypatch):
    """変換ステージはリスト名ではなくジョブ内のCSVを使い、共有キャッシュに載せない。"""
    from soramimic_video import convert as convert_mod
    from soramimic_video import (
        editor_io,
        xfparse,
    )
    from soramimic_video import mix as mix_mod
    from soramimic_video import video as video_mod

    res = client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "wordlist_csv": ("mine.csv", "ネコ,ネコ\n".encode(), "text/csv"),
        },
        data={"wordlist": "stations"},  # 名前も送るが、CSVのほうが優先される
    )
    job_id = res.json()["id"]
    params = client.get(f"/api/jobs/{job_id}").json()["params"]
    job = api_mod.Job(id=job_id, dir=tmp_path / "jobs" / job_id, params=params)

    class _Project:
        parody = None
        lines: list = []

        def save(self, d):
            pass

    calls: dict = {}

    def fake_convert(project, wordlist, where=None, params=None, cache_db=True):
        calls.update(wordlist=wordlist, cache_db=cache_db)
        return {"lines": []}

    out = tmp_path / "out.mp4"
    monkeypatch.setattr(xfparse, "analyze_midi", lambda path: _Project())
    monkeypatch.setattr(convert_mod, "convert_project", fake_convert)
    monkeypatch.setattr(editor_io, "save_raw", lambda raw, d: None)
    monkeypatch.setattr(api_mod, "_run_synthesize", lambda *a, **k: None)
    monkeypatch.setattr(mix_mod, "mix", lambda *a, **k: None)
    monkeypatch.setattr(video_mod, "make_video", lambda *a, **k: out)

    assert REAL_RUN_PIPELINE(job, {"parallel_video": False}) == out
    assert calls["wordlist"] == str(job.dir / api_mod.WORDLIST_DIRNAME / "mine.csv")
    assert calls["cache_db"] is False
