"""生成前の仮サムネ(/api/thumbnail-preview)のテスト。

空耳変換(run_convert)はモックするのでネットワーク・辞書構築は不要。
キャッシュヒット・不正な引数・レート制限を、公開/非公開モードの両方で確認する。
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402
from soramimic_video import custom_thumbnail_preview as custom_preview  # noqa: E402
from soramimic_video import thumbnail as thumb_mod  # noqa: E402
from soramimic_video import thumbnail_preview as preview_mod  # noqa: E402

SAMPLE_ID = "mysong"
SAMPLE_TITLE = "夜に駆ける"
SAMPLE_KANA = "ヨルニカケル"


def _fake_convert(surface: str = "米原"):
    """run_convert の戻り値(1フレーズぶん)を返すモック。"""

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None, *, cache_db=True):
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


def post_custom_preview(client: TestClient, **fields):
    return client.post(
        "/api/thumbnail-preview",
        content=json.dumps({
            "title": "ねこ", "wordlist_name": "動物",
            "wordlist_text": "ねこ,ネコ\nいぬ,イヌ", **fields,
        }),
        headers={"Content-Type": "application/json"},
    )


def test_custom_preview_renders_real_private_png(client, tmp_path, caplog):
    response = post_custom_preview(client)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-preview-cache"] == "private"
    assert response.headers["x-preview-images"] == "ready"
    with Image.open(io.BytesIO(response.content)) as png:
        assert png.size == (preview_mod.PREVIEW_WIDTH, preview_mod.PREVIEW_HEIGHT)
    edited = post_custom_preview(client, wordlist_text="ぞう,ゾー")
    assert edited.status_code == 200
    assert edited.content != response.content
    assert not list(preview_mod.preview_cache_dir(tmp_path / "jobs").glob("*.png"))
    assert "動物" not in caplog.text
    assert "ねこ" not in caplog.text


def test_custom_preview_normalizes_and_drops_image_references(client, samples, monkeypatch):
    seen = []
    (samples / "samples.json").write_text(json.dumps([{
        "id": SAMPLE_ID, "title": SAMPLE_TITLE, "title_kana": SAMPLE_KANA,
    }]), encoding="utf-8")

    def render(*args):
        seen.append(args)
        return b"png"

    monkeypatch.setattr(custom_preview, "render_custom_preview", render)
    response = post_custom_preview(
        client, sample=SAMPLE_ID, wordlist_name="../動物",
        wordlist_text=(
            "surface,pronunciation,image,image_page\n"
            "ねこ,ネコ,http://127.0.0.1/private,file:///etc/passwd\n"
            "いぬ,イヌ,/etc/passwd,http://internal/"
        ),
    )
    assert response.status_code == 200
    csv_text, title, name, kana = seen[0]
    assert csv_text == "id,original,surface,pronunciation\n1,ねこ,ねこ,ネコ\n2,いぬ,いぬ,イヌ"
    assert title == SAMPLE_TITLE
    assert name == "../動物"  # Display text; never used as a path.
    assert kana == SAMPLE_KANA


@pytest.mark.parametrize("fields", [
    {"wordlist_text": ""},
    {"wordlist_text": "ねこ,invalid"},
    {"wordlist_text": "\ud800"},
    {"wordlist_name": "\ud800"},
    {"title": "\ud800"},
    {"wordlist_text": ["ねこ"]},
    {"wordlist": "/etc/passwd"},
    {"wordlist_name": "x" * 101},
    {"title": "x" * 201},
    {"title": ""},
])
def test_custom_preview_rejects_invalid_input_before_render(client, monkeypatch, fields):
    def forbidden(*args):
        pytest.fail("invalid input reached the renderer")

    monkeypatch.setattr(custom_preview, "render_custom_preview", forbidden)
    assert post_custom_preview(client, **fields).status_code == 400


def test_custom_preview_bounds_stream_csv_bytes_and_rows(client, monkeypatch):
    monkeypatch.setenv("SORAMIMIC_MAX_WORDLIST_BYTES", "32")
    monkeypatch.setenv("SORAMIMIC_MAX_WORDLIST_ROWS", "1")
    assert post_custom_preview(client).status_code == 400  # Two rows.
    assert post_custom_preview(client, wordlist_text="ね" * 12).status_code == 400
    response = client.post(
        "/api/thumbnail-preview",
        content=iter([b'{"wordlist_text":"', b"x" * 5000, b'"}']),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("public", [False, True])
def test_custom_preview_is_rate_limited_before_parsing(tmp_path, monkeypatch, public):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1" if public else "0")
    monkeypatch.setenv(api_mod.SIMPLE_UI_ENV, "0")
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "1")
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    client = TestClient(app)
    assert client.post("/api/thumbnail-preview", json={}).status_code == 400
    assert client.post("/api/thumbnail-preview", json={}).status_code == 429


def test_custom_preview_preserves_api_key_and_simple_mode(client, monkeypatch):
    monkeypatch.setenv(api_mod.API_KEY_ENV, "secret")
    assert post_custom_preview(client).status_code == 401
    monkeypatch.setenv(api_mod.SIMPLE_UI_ENV, "1")
    response = client.post(
        "/api/thumbnail-preview", json={}, headers={"X-API-Key": "secret"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("error, status", [(TimeoutError("private words"), 429),
                                         (ValueError("private words"), 500)])
def test_custom_preview_failure_does_not_echo_input(client, monkeypatch, error, status):
    def fail(*args):
        raise error

    monkeypatch.setattr(custom_preview, "render_custom_preview", fail)
    response = post_custom_preview(client)
    assert response.status_code == status
    assert "private words" not in response.text


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


# ---- 曲名の読み(samples.json の title_kana) ----


def test_preview_converts_the_sample_reading(
    client: TestClient, samples: Path, monkeypatch
):
    """変換の入力は samples.json の読み(「紅葉」を「コーヨー」と推定させない)。"""
    (samples / "samples.json").write_text(
        json.dumps([
            {
                "id": SAMPLE_ID,
                "title": SAMPLE_TITLE,
                "title_kana": SAMPLE_KANA,
                "description": "テスト",
            }
        ]),
        encoding="utf-8",
    )
    seen: list[list[str]] = []

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None, *, cache_db=True):
        seen.append(list(phrases))
        return {
            "lines": [{"units": [], "words": [{"surface": "米原", "id": "1"}]}],
            "tokensList": [],
            "phrases": phrases,
        }

    monkeypatch.setattr(thumb_mod, "run_convert", fake)
    assert get_preview(client).status_code == 200
    assert seen == [[SAMPLE_KANA]]


def test_preview_without_reading_converts_the_title(
    client: TestClient, monkeypatch
):
    # 読みの無い(古い・差し替えの)samples.json では従来どおり曲名を変換に渡す
    seen: list[list[str]] = []

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None, *, cache_db=True):
        seen.append(list(phrases))
        return {
            "lines": [{"units": [], "words": [{"surface": "米原", "id": "1"}]}],
            "tokensList": [],
            "phrases": phrases,
        }

    monkeypatch.setattr(thumb_mod, "run_convert", fake)
    assert get_preview(client).status_code == 200
    assert seen == [[SAMPLE_TITLE]]


def test_uploaded_song_title_returns_uncached_private_preview(
    client: TestClient, monkeypatch, tmp_path: Path
):
    """持ち込み曲名の派生PNGを共有ディスクキャッシュへ残さない。"""
    seen: list[list[str]] = []

    def fake(phrases, wordlist_csv, where, params, weights_per_line=None, *, cache_db=True):
        seen.append(list(phrases))
        return {
            "lines": [{"units": [], "words": [{"surface": "米原", "id": "1"}]}],
            "tokensList": [],
            "phrases": phrases,
        }

    monkeypatch.setattr(thumb_mod, "run_convert", fake)
    res = client.get(
        "/api/thumbnail-preview", params={"title": " marigold ", "wordlist": "mylist"}
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.headers["x-preview-cache"] == "private"
    assert res.headers["cache-control"] == "private, no-store"
    assert seen == [["marigold"]]
    assert not list(preview_mod.preview_cache_dir(tmp_path / "jobs").glob("*.png"))


def test_cache_key_changes_with_reading(wordlist_dir: Path):
    # 読みを足した/変えたら作り直す(古い読みのPNGを返し続けない)
    plain = preview_mod.PreviewSpec.create(SAMPLE_TITLE, "mylist")
    with_kana = preview_mod.PreviewSpec.create(
        SAMPLE_TITLE, "mylist", title_kana=SAMPLE_KANA
    )
    other = preview_mod.PreviewSpec.create(
        SAMPLE_TITLE, "mylist", title_kana="ヨルニカケール"
    )
    assert len({plain.key, with_kana.key, other.key}) == 3


# ---- 不正な引数 ----


def test_unknown_sample_is_404(client: TestClient):
    assert get_preview(client, sample="nope").status_code == 404


def test_unknown_wordlist_is_404(client: TestClient):
    assert get_preview(client, wordlist="nope").status_code == 404


def test_missing_wordlist_is_400(client: TestClient):
    assert get_preview(client, wordlist="  ").status_code == 400


def test_missing_sample_and_title_is_400(client: TestClient):
    res = client.get("/api/thumbnail-preview", params={"wordlist": "mylist"})
    assert res.status_code == 400
    assert "曲名" in res.json()["detail"]


def test_uploaded_song_title_is_limited(client: TestClient):
    res = client.get(
        "/api/thumbnail-preview", params={"title": "a" * 201, "wordlist": "mylist"}
    )
    assert res.status_code == 400


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
    monkeypatch.setattr(api_mod, "launch_wordlist_names", lambda: {"mylist"})
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
    monkeypatch.setattr(api_mod, "launch_wordlist_names", lambda: {"mylist"})
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "1")
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    first, second = TestClient(app), TestClient(app)
    assert get_preview(first, where="a=1").status_code == 200
    assert get_preview(first, where="a=2").status_code == 429
    # 別セッション(別cookie)は自分の枠で作れる
    assert get_preview(second, where="a=3").status_code == 200


def test_ip_backstop_survives_cookie_deletion(
    tmp_path, monkeypatch, wordlist_dir, samples
):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setattr(api_mod, "launch_wordlist_names", lambda: {"mylist"})
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "100")
    monkeypatch.setenv(api_mod.GET_IP_RATE_LIMIT_ENV, "1")
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    first, fresh_cookie = TestClient(app), TestClient(app)
    assert get_preview(first, where="a=1").status_code == 200
    # 別cookieでも接続元IPが同じなので、キャッシュミスはIP枠で止まる。
    assert get_preview(fresh_cookie, where="a=2").status_code == 429


# ---- 画像の待ち / 裏読み(1回目から絵入りを出す) ----


def test_prefetch_downloads_images(tmp_path: Path):
    # image列がローカルパスなら download_image はコピーで取り込む(ネットワーク不要)
    source = tmp_path / "word.png"
    Image.new("RGB", (8, 8), "red").save(source)
    cache_dir = tmp_path / "images"
    assert preview_mod.prefetch_images([(str(source), "")], cache_dir) == 1
    assert list(cache_dir.glob("*.png"))  # 画像はキャッシュに入った
    # 2回目はもうキャッシュにあるので「新しく取れた」件数は0
    assert preview_mod.prefetch_images([(str(source), "")], cache_dir) == 0


def test_prefetch_returns_zero_when_nothing_downloaded(tmp_path: Path):
    assert preview_mod.prefetch_images(
        [(str(tmp_path / "missing.png"), "")], tmp_path / "images"
    ) == 0


@pytest.fixture
def image_wordlist(tmp_path: Path, wordlist_dir: Path) -> Path:
    """画像つきの単語リスト。画像の実体はローカルPNG(ネットワーク不要)。"""
    source = tmp_path / "word.png"
    Image.new("RGB", (8, 8), "red").save(source)
    (wordlist_dir / "mylist.csv").write_text(
        f"id,surface,original,image\n1,米原,米原駅,{source}\n", encoding="utf-8"
    )
    return source


def test_first_preview_waits_and_includes_image(
    tmp_path: Path, monkeypatch, image_wordlist, samples
):
    """キャッシュが空でも、2秒の待ちで間に合う画像は1回目から入る。"""
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    res = get_preview(client)
    assert res.status_code == 200
    assert res.headers["x-preview-images"] == "ready"  # 待って間に合った
    assert list((tmp_path / "jobs" / "image-cache").glob("*.png"))
    cache_dir = preview_mod.preview_cache_dir(tmp_path / "jobs")
    assert not list(cache_dir.glob(f"*{preview_mod.PENDING_SUFFIX}"))


def test_slow_image_returns_pending_then_refreshes_to_ready(
    tmp_path: Path, monkeypatch, image_wordlist, samples
):
    """間に合わない画像は pending で返し、裏で取り切って絵入りに作り直す。"""
    import soramimic_video.video as video_mod

    real_download = video_mod.download_image
    slow = threading.Event()

    def slow_download(url, cache_dir):
        slow.wait(5.0)  # 1回目の待ち(0.2秒)には間に合わない
        return real_download(url, cache_dir)

    monkeypatch.setattr(video_mod, "download_image", slow_download)
    monkeypatch.setattr(preview_mod, "IMAGE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    first = get_preview(client)
    assert first.status_code == 200
    assert first.headers["x-preview-images"] == "pending"  # 文字だけで返した
    cache_dir = preview_mod.preview_cache_dir(tmp_path / "jobs")
    assert list(cache_dir.glob(f"*{preview_mod.PENDING_SUFFIX}"))

    slow.set()  # 裏読みが完了できるようにする
    for _ in range(300):
        if not list(cache_dir.glob(f"*{preview_mod.PENDING_SUFFIX}")):
            break
        time.sleep(0.02)

    # UIの取り直し相当。作り直し済みなのでキャッシュヒット(=レート制限を消費しない)
    second = get_preview(client)
    assert second.headers["x-preview-cache"] == "hit"
    assert second.headers["x-preview-images"] == "ready"
    assert second.content != first.content  # 画像が入って中身が変わる


def test_auto_retry_does_not_consume_rate_limit(
    tmp_path: Path, monkeypatch, image_wordlist, samples
):
    """pending のあとの自動取り直しは、レート制限が1回ぶんでも通る。"""
    monkeypatch.setenv(preview_mod.RATE_LIMIT_ENV, "1")
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    assert get_preview(client).status_code == 200  # 生成(1回ぶん消費)
    for _ in range(3):  # 取り直しはキャッシュヒットなので429にならない
        again = get_preview(client)
        assert again.status_code == 200
        assert again.headers["x-preview-cache"] == "hit"


def test_prune_cache_removes_orphan_pending_markers(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"abc{preview_mod.PENDING_SUFFIX}").write_text("", encoding="utf-8")
    (cache / "def.png").write_bytes(b"x")
    (cache / f"def{preview_mod.PENDING_SUFFIX}").write_text("", encoding="utf-8")
    preview_mod.prune_cache(cache, max_entries=10, ttl_seconds=0)
    assert not (cache / f"abc{preview_mod.PENDING_SUFFIX}").exists()  # PNGが無い目印は捨てる
    assert (cache / f"def{preview_mod.PENDING_SUFFIX}").exists()  # PNGが残っていれば残す


# ---- ビルダーカード(index.html)側の約束事 ----


def test_index_html_builder_uses_preview_with_fallback():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/api/thumbnail-preview?" in html  # カードはプレビューを取りに行く
    assert "builder-loading" in html  # 生成待ちのローディング表示がある
    assert "PREVIEW_TIMEOUT_MS = 8000" in html  # 8秒で打ち切る
    # 失敗・429・タイムアウトは派生代表画像(/api/asset-preview)にフォールバックする
    assert "loadWordlistImage(combo.wordlistName, seq);" in html
    assert "/api/asset-preview?wordlist=" in html
    assert "/api/wordlist-image?" not in html
    # フォールバックしたままにはせず、本物のプレビューを裏で聞き直す
    assert "retryPreviewAfterFallback(url, seq, PREVIEW_FALLBACK_RETRIES," in html


def test_index_html_preview_is_debounced():
    """プルダウンの連続変更・ボタン連打でプレビューAPIを叩きすぎない。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "PREVIEW_DEBOUNCE_MS = 400" in html
    # 予約はタイマーを張り直す1か所だけ。同じ組み合わせなら取りに行かない
    assert "clearTimeout(previewTimer);" in html
    assert "previewTimer = setTimeout(renderBuilder, force ? 0 : PREVIEW_DEBOUNCE_MS);" in html
    assert "if (key === previewKey) return;" in html


def test_index_html_retries_after_falling_back_to_wordlist_image():
    """代表画像へ落ちたあとも本物のプレビューを聞き直し、届いたら差し替える。

    初回の空耳変換や画像取得が8秒に間に合わないだけのことがあり、そのまま
    代表画像(文字なし)で固定されてしまうのを防ぐ。
    """
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # 上限つきで間隔を伸ばしながら聞き直す
    assert "const PREVIEW_FALLBACK_RETRY_MS = 4000;" in html
    assert "const PREVIEW_FALLBACK_RETRIES = 8;" in html
    assert "function retryPreviewAfterFallback(url, seq, left, wait)" in html
    # フォールバックした直後に取り直しを仕掛ける
    assert "loadWordlistImage(combo.wordlistName, seq);" in html
    assert "setPreviewPending(true);" in html
    assert "retryPreviewAfterFallback(url, seq, PREVIEW_FALLBACK_RETRIES," in html
    # 本物が届いたら差し替え、待っている表示は消える
    assert "function showPreviewBlob(blob)" in html
    assert "previewHasReal = true;" in html
    # 遅れて届いた代表画像で本物を上書きしない
    assert "if (seq !== previewSeq || previewHasReal) return;" in html
    # 準備中であることが分かる控えめな表示
    assert '<p class="hint" id="builder-preview-pending" hidden>プレビューを準備中…</p>' in html


def test_index_html_retries_once_when_images_pending():
    """絵なしで返ってきたら数秒後に1回だけ取り直し、ちらつかせずに差し替える。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "PREVIEW_RETRY_MS = 4000" in html
    assert 'res.headers.get("X-Preview-Images") === "pending"' in html
    assert "if (pending) retryThumbnailPreview(url, seq);" in html
    # 取り直しは世代番号(previewSeq)で取り違えを防ぎ、1回だけで打ち切る
    assert "if (seq !== previewSeq) return;   // 選び直された" in html
    # 定義と、初回・フォールバック取り直しからの呼び出しだけ(自分では再帰しない)
    assert html.count("retryThumbnailPreview(") == 3


def test_index_html_preview_respects_hidden_wordlists():
    """画像を初期非表示にする単語リストでは、プレビューにも単語画像を入れない。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'params.images = "0";' in html  # 画像なしのサムネを頼む
    # 代表画像へのフォールバックも隠す(「画像を表示する」を押すまで読まない)
    assert "if (!name || hiddenPreviewReason(name)) { hide(); return; }" in html


# ---- 画像なしプレビュー(画像を初期非表示にする単語リスト向け) ----


def test_images_off_renders_without_word_images(
    tmp_path: Path, monkeypatch, wordlist_dir, samples
):
    source = tmp_path / "word.png"
    Image.new("RGB", (8, 8), "red").save(source)
    (wordlist_dir / "mylist.csv").write_text(
        f"id,surface,original,image\n1,米原,米原駅,{source}\n", encoding="utf-8"
    )
    monkeypatch.setattr(thumb_mod, "run_convert", _fake_convert())
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    # 画像を初期非表示にするリストでは裏読みもしない(画像キャッシュは空のまま)
    off = get_preview(client, images="0")
    assert off.status_code == 200
    time.sleep(0.3)
    image_cache = tmp_path / "jobs" / "image-cache"
    assert not image_cache.exists() or not list(image_cache.glob("*"))

    # 画像ありは同じ組み合わせでも別キャッシュになり、絵入りで中身が変わる
    # (裏読みを待たずに済むよう、画像キャッシュは直接温めておく)
    from soramimic_video.video import download_image

    download_image(str(source), image_cache)
    cache_dir = preview_mod.preview_cache_dir(tmp_path / "jobs")
    on = get_preview(client)
    assert on.headers["x-preview-cache"] == "miss"
    assert on.content != off.content
    assert len(list(cache_dir.glob("*.png"))) == 2


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


def test_edited_preview_label_is_private_and_reaches_renderer(client, tmp_path, monkeypatch):
    seen = []
    original = thumb_mod.render_thumbnail

    def capture(path, song, wordlist_text, **kwargs):
        seen.append(wordlist_text)
        return original(path, song, wordlist_text, **kwargs)

    monkeypatch.setattr(thumb_mod, "render_thumbnail", capture)
    first = get_preview(client, wordlist_label="Stations A")
    second = get_preview(client, wordlist_label="Stations B")
    assert first.status_code == second.status_code == 200
    assert first.headers["x-preview-cache"] == second.headers["x-preview-cache"] == "private"
    assert first.content != second.content
    assert seen == ["Stations A", "Stations B"]
    assert not list(preview_mod.preview_cache_dir(tmp_path / "jobs").glob("*.png"))
    assert get_preview(client, wordlist_label="あ" * 101).status_code == 400
