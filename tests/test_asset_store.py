from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from PIL import Image

from soramimic_video import asset_store, image_credit, prewarm, video


def _png(path: Path, color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def _wordlist(root: Path, rows: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "words.csv"
    path.write_text("image,image_page,image_credit\n" + rows, encoding="utf-8")
    return path


def _source_manifest(entries: dict[str, tuple[int, Path]], revision: int = 1) -> dict:
    return {
        "$schema": prewarm.SOURCE_MANIFEST_JSON_SCHEMA,
        "schema": prewarm.SOURCE_MANIFEST_SCHEMA,
        "version": 1,
        "revision": revision,
        "generated_at": "2026-08-15T00:00:00Z",
        "repository": prewarm.SOURCE_MANIFEST_REPOSITORY,
        "assets": {
            url: {
                "revision": asset_revision,
                "updated_at": "2026-08-15T00:00:00Z",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
            for url, (asset_revision, path) in entries.items()
        },
    }


def test_manifest_cache_is_bounded():
    assert asset_store._read_manifest.cache_info().maxsize == 2


def test_orphaned_manifest_entry_uses_runtime_fallback(tmp_path):
    url = "https://example.com/old.png"
    store = tmp_path / "assets"
    store.mkdir()
    (store / "manifest.json").write_text(json.dumps({"assets": {url: {
        "status": "available", "local_path": "images/old.png", "orphaned_at": "now",
    }}}))
    assert asset_store.manifest_entry(url, store) is None
    assert asset_store.local_asset(url, store) == (False, None)


def test_sync_uses_local_raw_github_image_and_runtime_is_offline(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    local = wordlists / "images" / "cards" / "a.png"
    _png(local)
    url = (
        "https://raw.githubusercontent.com/soramimic/soramimic-wordlists/"
        "main/images/cards/a.png"
    )
    csv_path = _wordlist(wordlists, f"{url},,local credit\n")
    store = tmp_path / "assets"

    summary = prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    assert summary == {
        "total": 1, "new": 1, "updated": 0, "unchanged": 0,
        "failed": 0, "credit_failed": 0, "credit_unknown": 0, "orphaned": 0,
        "promoted": 1,
    }
    manifest = json.loads((store / "manifest.json").read_text())
    entry = manifest["assets"][url]
    assert entry["status"] == "available"
    assert entry["credit"]["credit_text"] == "local credit"
    assert (store / entry["local_path"]).is_file()

    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    monkeypatch.setattr(
        image_credit.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError),
    )
    assert video.cached_image(url, tmp_path / "job-cache") == store / entry["local_path"]
    assert video.download_image(url, tmp_path / "job-cache") == store / entry["local_path"]
    info = image_credit.fetch_image_credit(url, "", tmp_path / "job-cache")
    assert info["credit_text"] == "local credit"


def test_sync_batches_commons_and_distinguishes_no_attribution(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = "https://commons.wikimedia.org/wiki/Special:FilePath/A.png"
    csv_path = _wordlist(wordlists, f"{url},,\n")
    downloaded = tmp_path / "download.png"
    _png(downloaded)
    monkeypatch.setattr(prewarm, "download_image", lambda *a, **k: downloaded)
    calls = []

    def fake_batch(images, **kwargs):
        calls.append(images)
        return {
            url: {
                "credit": {
                    "artist": "", "license": "CC0", "attribution_required": False,
                    "credit_text": "",
                },
                "download_url": "https://upload.wikimedia.org/a.png",
            }
        }

    monkeypatch.setattr(prewarm, "fetch_commons_assets_batch", fake_batch)
    store = tmp_path / "assets"
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    entry = json.loads((store / "manifest.json").read_text())["assets"][url]
    assert len(calls) == 1
    assert entry["credit"]["status"] == "known"
    assert entry["credit"]["attribution_required"] is False


def test_sync_failure_keeps_last_good_and_orphans_are_not_deleted(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = "https://example.com/a.png"
    csv_path = _wordlist(wordlists, f"{url},,\n")
    downloaded = tmp_path / "download.png"
    _png(downloaded)
    monkeypatch.setattr(prewarm, "download_image", lambda *a, **k: downloaded)
    store = tmp_path / "assets"
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    before = json.loads((store / "manifest.json").read_text())["assets"][url]

    monkeypatch.setattr(prewarm, "download_image", lambda *a, **k: None)
    active_before = (store / "manifest.json").read_bytes()
    result = prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists, revalidate=True,
    )
    assert result["promoted"] == 0
    assert (store / "manifest.json").read_bytes() == active_before
    after = json.loads((store / "manifest.pending.json").read_text())["assets"][url]
    assert result["failed"] == 1
    assert after["status"] == "available"
    assert after["local_path"] == before["local_path"]
    assert "last_error" in after

    empty = _wordlist(wordlists, "")
    prewarm.sync_asset_store([empty], store, wordlists_dir=wordlists)
    orphan = json.loads((store / "manifest.pending.json").read_text())["assets"][url]
    assert "orphaned_at" in orphan
    assert (store / orphan["local_path"]).is_file()


def test_sync_bypasses_configured_store_and_detects_stale_revalidate(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = "https://example.com/a.png"
    csv_path = _wordlist(wordlists, f"{url},,\n")
    downloaded = tmp_path / "download.png"
    _png(downloaded)
    store = tmp_path / "assets"
    calls = []

    def first_download(*args, **kwargs):
        calls.append(kwargs)
        return downloaded

    monkeypatch.setattr(prewarm, "download_image", first_download)
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    result = prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists, revalidate=True,
    )
    assert calls[-1]["use_asset_store"] is False
    assert result["failed"] == 1  # metadata checked_at did not advance: stale fallback
    assert result["promoted"] == 0


def test_sync_skips_second_revalidation_for_priority_urls(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    first = "https://example.com/priority.png"
    second = "https://example.com/rest.png"
    priority_csv = _wordlist(wordlists, f"{first},,priority credit\n")
    store = tmp_path / "assets"
    downloaded = tmp_path / "download.png"
    _png(downloaded)
    calls: list[str] = []

    def download(url, *args, **kwargs):
        calls.append(url)
        return downloaded

    monkeypatch.setattr(prewarm, "download_image", download)
    prewarm.sync_asset_store([priority_csv], store, wordlists_dir=wordlists)
    all_csv = wordlists / "all.csv"
    all_csv.write_text(
        "image,image_page,image_credit\n"
        f"{first},,priority credit\n{second},,rest credit\n",
        encoding="utf-8",
    )
    calls.clear()
    result = prewarm.sync_asset_store(
        [all_csv], store, wordlists_dir=wordlists, revalidate=True,
        skip_revalidate_urls={first},
    )
    assert calls == [second]
    assert result["unchanged"] == 1
    assert result["new"] == 1
    assert result["promoted"] == 1


def test_sync_fetches_network_images_with_two_workers(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    urls = [f"https://example.com/{index}.png" for index in range(3)]
    csv_path = _wordlist(
        wordlists,
        "".join(f"{url},,credit {index}\n" for index, url in enumerate(urls)),
    )
    downloaded = tmp_path / "download.png"
    _png(downloaded)
    lock = threading.Lock()
    release = threading.Event()
    active = peak = started = 0

    def download(*args, **kwargs):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            if started >= 2:
                release.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return downloaded

    monkeypatch.setattr(prewarm, "download_image", download)
    result = prewarm.sync_asset_store(
        [csv_path], tmp_path / "assets", wordlists_dir=wordlists,
        download_workers=2,
    )
    assert result["promoted"] == 1
    assert peak == 2


def test_credit_refresh_failure_keeps_last_good(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = "https://commons.wikimedia.org/wiki/Special:FilePath/A.png"
    csv_path = _wordlist(wordlists, f"{url},,\n")
    downloaded = tmp_path / "download.png"
    _png(downloaded)
    monkeypatch.setattr(prewarm, "download_image", lambda *a, **k: downloaded)
    store = tmp_path / "assets"
    known = {
        "artist": "A", "license": "CC BY", "attribution_required": True,
        "credit_text": "A, CC BY",
    }
    monkeypatch.setattr(
        prewarm, "fetch_commons_assets_batch",
        lambda images, **kwargs: {url: {"credit": known, "download_url": None}},
    )
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    monkeypatch.setattr(
        prewarm, "fetch_commons_assets_batch",
        lambda images, **kwargs: {url: {"credit": None, "download_url": None}},
    )
    prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists, revalidate=True,
    )
    credit = json.loads((store / "manifest.pending.json").read_text())["assets"][url]["credit"]
    assert credit["status"] == "known"
    assert credit["credit_text"] == "A, CC BY"
    assert "last_error" in credit


def test_failed_initial_sync_creates_pending_only(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = "https://example.com/missing.png"
    csv_path = _wordlist(wordlists, f"{url},,\n")
    monkeypatch.setattr(prewarm, "download_image", lambda *a, **k: None)
    store = tmp_path / "assets"

    result = prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    assert result["failed"] == 1
    assert result["promoted"] == 0
    assert not (store / "manifest.json").exists()
    pending = json.loads((store / "manifest.pending.json").read_text())
    assert pending["assets"][url]["status"] == "failed"
    status = prewarm.asset_store_status(store)
    assert status["total"] == 0
    assert status["pending"] == 1
    assert status["pending_failed"] == 1


def test_dry_run_does_not_create_store(tmp_path):
    wordlists = tmp_path / "wordlists"
    csv_path = _wordlist(wordlists, "https://example.com/a.png,,\n")
    store = tmp_path / "assets"
    result = prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists, dry_run=True,
    )
    assert result["new"] == 1
    assert not store.exists()


def test_source_manifest_downloads_only_changed_hash(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    base = prewarm.SOURCE_RELEASE_URL_PREFIX + "test-v1/"
    urls = [base + "a.png", base + "b.png"]
    csv_path = _wordlist(
        wordlists, "".join(f"{url},,credit\n" for url in urls),
    )
    files = [tmp_path / "a.png", tmp_path / "b.png"]
    _png(files[0], "red")
    _png(files[1], "blue")
    current = _source_manifest(dict(zip(urls, ((1, files[0]), (1, files[1])), strict=True)))
    monkeypatch.setattr(prewarm, "fetch_source_manifest", lambda url: (current, "1" * 64))
    calls: list[str] = []

    def fetch(url, source, staging, max_bytes):
        calls.append(url)
        path = files[urls.index(url)]
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(prewarm, "_fetch_verified_source_image", fetch)
    store = tmp_path / "assets"
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    assert calls == urls

    calls.clear()
    result = prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    assert calls == []
    assert result["unchanged"] == 2

    _png(files[1], "green")
    current = _source_manifest(
        dict(zip(urls, ((1, files[0]), (2, files[1])), strict=True)), revision=2,
    )
    result = prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    assert calls == [urls[1]]
    assert result["updated"] == 1
    active = json.loads((store / "manifest.json").read_text())
    assert active["assets"][urls[1]]["source_revision"] == 2
    assert active["assets"][urls[1]]["blob_sha256"] == active["assets"][urls[1]]["sha256"]


def test_source_partial_failure_retries_from_active_and_keeps_last_good(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    base = prewarm.SOURCE_RELEASE_URL_PREFIX + "test-v1/"
    urls = [base + "a.png", base + "b.png"]
    csv_path = _wordlist(wordlists, "".join(f"{url},,credit\n" for url in urls))
    files = [tmp_path / "a.png", tmp_path / "b.png"]
    _png(files[0], "red")
    _png(files[1], "blue")
    current = _source_manifest(dict(zip(urls, ((1, files[0]), (1, files[1])), strict=True)))
    monkeypatch.setattr(prewarm, "fetch_source_manifest", lambda url: (current, "1" * 64))
    fail: set[str] = set()
    calls: list[str] = []

    def fetch(url, source, staging, max_bytes):
        calls.append(url)
        if url in fail:
            raise ValueError("injected")
        path = files[urls.index(url)]
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(prewarm, "_fetch_verified_source_image", fetch)
    store = tmp_path / "assets"
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    active_before = (store / "manifest.json").read_bytes()
    _png(files[0], "yellow")
    _png(files[1], "green")
    current = _source_manifest(
        dict(zip(urls, ((2, files[0]), (2, files[1])), strict=True)), revision=2,
    )
    fail.add(urls[1])
    assert prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists,
    )["promoted"] == 0
    assert (store / "manifest.json").read_bytes() == active_before
    calls.clear()
    fail.clear()
    assert prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists,
    )["promoted"] == 1
    assert calls == urls


def test_source_hash_mismatch_and_manifest_fetch_failure_keep_active(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = prewarm.SOURCE_RELEASE_URL_PREFIX + "test-v1/a.png"
    csv_path = _wordlist(wordlists, f"{url},,credit\n")
    image = tmp_path / "a.png"
    _png(image)
    current = _source_manifest({url: (1, image)})
    monkeypatch.setattr(prewarm, "fetch_source_manifest", lambda value: (current, "1" * 64))
    monkeypatch.setattr(
        prewarm, "_fetch_verified_source_image",
        lambda *args: (image, hashlib.sha256(image.read_bytes()).hexdigest()),
    )
    store = tmp_path / "assets"
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    active_before = (store / "manifest.json").read_bytes()
    _png(image, "blue")
    current = _source_manifest({url: (2, image)}, revision=2)
    monkeypatch.setattr(
        prewarm, "_fetch_verified_source_image",
        lambda *args: (_ for _ in ()).throw(ValueError("source sha256不一致")),
    )
    assert prewarm.sync_asset_store(
        [csv_path], store, wordlists_dir=wordlists,
    )["promoted"] == 0
    assert (store / "manifest.json").read_bytes() == active_before
    monkeypatch.setattr(
        prewarm, "fetch_source_manifest",
        lambda value: (_ for _ in ()).throw(ValueError("manifest unavailable")),
    )
    try:
        prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    except ValueError as exc:
        assert "manifest unavailable" in str(exc)
    else:
        raise AssertionError("manifest fetch failure was ignored")
    assert (store / "manifest.json").read_bytes() == active_before


def test_source_manifest_entry_deletion_does_not_gc_blob(tmp_path, monkeypatch):
    wordlists = tmp_path / "wordlists"
    url = prewarm.SOURCE_RELEASE_URL_PREFIX + "test-v1/a.png"
    csv_path = _wordlist(wordlists, f"{url},,credit\n")
    image = tmp_path / "a.png"
    _png(image)
    current = _source_manifest({url: (1, image)})
    monkeypatch.setattr(prewarm, "fetch_source_manifest", lambda value: (current, "1" * 64))
    monkeypatch.setattr(prewarm, "_fetch_verified_source_image", lambda *args: (image, "x"))
    store = tmp_path / "assets"
    prewarm.sync_asset_store([csv_path], store, wordlists_dir=wordlists)
    entry = json.loads((store / "manifest.json").read_text())["assets"][url]
    blob = store / entry["local_path"]
    current = _source_manifest({}, revision=2)
    empty = _wordlist(wordlists, "")
    # No Release URL remains in the catalog, so the source marker need not be fetched;
    # the old reference is orphaned and the content-addressed blob is left for later GC.
    prewarm.sync_asset_store([empty], store, wordlists_dir=wordlists)
    orphan = json.loads((store / "manifest.pending.json").read_text())["assets"][url]
    assert "orphaned_at" in orphan
    assert blob.is_file()


def test_fetch_image_credits_batch_uses_one_request(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"query": {"pages": [{
                "title": "File:A.png",
                "imageinfo": [{
                    "thumburl": "https://upload.wikimedia.org/thumb/a.png",
                    "extmetadata": {
                        "LicenseShortName": {"value": "CC0"},
                        "AttributionRequired": {"value": "false"},
                    },
                }],
            }]}}

    calls = []
    monkeypatch.setattr(
        image_credit.requests, "get",
        lambda *a, **kw: calls.append(kw["params"]) or Response(),
    )
    a = "https://commons.wikimedia.org/wiki/Special:FilePath/A.png"
    result = image_credit.fetch_image_credits_batch({a: ""})
    assert len(calls) == 1
    assert result[a]["attribution_required"] is False
    assets = image_credit.fetch_commons_assets_batch({a: ""})
    assert assets[a]["download_url"] == "https://upload.wikimedia.org/thumb/a.png"
    assert calls[-1]["iiprop"] == "extmetadata|url"
    assert calls[-1]["iiurlwidth"] == "1200"
