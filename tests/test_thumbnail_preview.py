"""生成前の仮サムネ(/api/thumbnail-preview)のテスト。

空耳変換(run_convert)はモックするのでネットワーク・辞書構築は不要。
キャッシュヒット・不正な引数・レート制限を、公開/非公開モードの両方で確認する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402
from soramimic_video import thumbnail as thumb_mod  # noqa: E402
from soramimic_video import thumbnail_preview as preview_mod  # noqa: E402

SAMPLE_ID = "mysong"
SAMPLE_TITLE = "夜に駆ける"


def _fake_convert(surface: str = "米原"):
    """run_convert の戻り値(1フレーズぶん)を返すモック。"""

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None):
        return {
            "lines": [{"units": [], "words": [{"surface": surface, "id": "1"}]}],
            "tokensList": [],
            "phrases": phrases,
        }

    return fake


@pytest.fixture
def wordlist_dir(tmp_path: Path, monkeypatch) -> Path:
    """テスト用の単語リスト置き場(submoduleの実データに依存しない)。"""
    d = tmp_path / "wordlists"
    d.mkdir()
    (d / "mylist.csv").write_text(
        "id,surface,original,image\n1,米原,米原駅,\n", encoding="utf-8"
    )
    monkeypatch.setattr("soramimic_video.convert.WORDLISTS_DIR", d)
    return d


@pytest.fixture
def samples(tmp_path: Path, monkeypatch) -> Path:
    """サンプル曲の差し替え(midiの実体はプレビューに不要なのでmanifestだけ)。"""
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps([{"id": SAMPLE_ID, "title": SAMPLE_TITLE, "description": "テスト"}]),
        encoding="utf-8",
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    return d


@pytest.fixture
def client(tmp_path, monkeypatch, wordlist_dir, samples) -> TestClient:
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    monkeypatch.setattr(api_mod, "run_pipeline", lambda job, config: job.dir / "x.mp4")
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    return TestClient(app)


def get_preview(client: TestClient, **params):
    return client.get("/api/thumbnail-preview", params={
        "sample": SAMPLE_ID, "wordlist": "mylist", **params
    })


# ---- 正常系・キャッシュ ----


def test_returns_png_and_caches(client: TestClient, tmp_path: Path):
    res = get_preview(client)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/png"
    assert res.headers["x-preview-cache"] == "miss"
    cache_dir = preview_mod.preview_cache_dir(tmp_path / "jobs")
    assert len(list(cache_dir.glob("*.png"))) == 1

    # 2回目は変換せずキャッシュから返す
    again = get_preview(client)
    assert again.headers["x-preview-cache"] == "hit"
    assert again.content == res.content
    assert len(list(cache_dir.glob("*.png"))) == 1


def test_preview_uses_converted_word_and_small_size(client: TestClient, tmp_path: Path):
    assert get_preview(client).status_code == 200
    png = next(preview_mod.preview_cache_dir(tmp_path / "jobs").glob("*.png"))
    with Image.open(png) as img:
        assert img.size == (preview_mod.PREVIEW_WIDTH, preview_mod.PREVIEW_HEIGHT)


def test_cache_key_changes_with_wordlist_content(
    client: TestClient, tmp_path: Path, wordlist_dir: Path
):
    assert get_preview(client).status_code == 200
    (wordlist_dir / "mylist.csv").write_text(
        "id,surface,original,image\n1,米原,米原駅,\n2,大津,大津駅,\n", encoding="utf-8"
    )
    assert get_preview(client).headers["x-preview-cache"] == "miss"


# ---- 不正な引数 ----


def test_unknown_sample_is_404(client: TestClient):
    assert get_preview(client, sample="nope").status_code == 404


def test_unknown_wordlist_is_404(client: TestClient):
    assert get_preview(client, wordlist="nope").status_code == 404


def test_missing_wordlist_is_400(client: TestClient):
    assert get_preview(client, wordlist="  ").status_code == 400


# ---- レート制限 ----


def test_rate_limit_returns_429_on_miss(client: TestClient, monkeypatch):
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "1")
    assert get_preview(client, where="a=1").status_code == 200
    res = get_preview(client, where="a=2")
    assert res.status_code == 429
    assert "プレビュー" in res.json()["detail"]


def test_cache_hits_do_not_consume_rate_limit(client: TestClient, monkeypatch):
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "1")
    assert get_preview(client).status_code == 200  # 生成(1回ぶん消費)
    for _ in range(5):  # キャッシュヒットは何回でも通る
        assert get_preview(client).headers["x-preview-cache"] == "hit"


def test_rate_limit_disabled_by_zero(client: TestClient, monkeypatch):
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "0")
    for i in range(3):
        assert get_preview(client, where=f"a={i}").status_code == 200


# ---- 公開モード ----


@pytest.fixture
def public_client(tmp_path, monkeypatch, wordlist_dir, samples) -> TestClient:
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    monkeypatch.setattr(api_mod, "run_pipeline", lambda job, config: job.dir / "x.mp4")
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def test_public_mode_preview_works_and_keeps_quota(public_client: TestClient):
    res = get_preview(public_client)
    assert res.status_code == 200
    # ジョブではないので日次クォータ(=ジョブ数)は増えない
    assert public_client.get("/api/jobs").json() == []


def test_public_mode_rate_limit_is_per_session(
    tmp_path, monkeypatch, wordlist_dir, samples
):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "1")
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    first, second = TestClient(app), TestClient(app)
    assert get_preview(first, where="a=1").status_code == 200
    assert get_preview(first, where="a=2").status_code == 429
    # 別セッション(別cookie)は自分の枠で作れる
    assert get_preview(second, where="a=3").status_code == 200


# ---- キャッシュの刈り取り ----


def test_prune_cache_by_ttl_and_count(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    paths = []
    for i in range(5):
        p = cache / f"{i}.png"
        p.write_bytes(b"x")
        import os

        os.utime(p, (1000 + i, 1000 + i))
        paths.append(p)
    # TTL超過(now=2000, ttl=100)は全部消える
    assert len(preview_mod.prune_cache(cache, max_entries=10, ttl_seconds=100, now=2000)) == 5

    for p in paths:
        p.write_bytes(b"x")
    removed = preview_mod.prune_cache(cache, max_entries=2, ttl_seconds=0, now=2000)
    assert len(removed) == 3
    assert len(list(cache.glob("*.png"))) == 2
