from __future__ import annotations

import csv
import json
from pathlib import Path

from PIL import Image

from soramimic_video import asset_store, convert, prewarm, video


def _csv(root: Path, url: str, *, credit: str = 'COVER', usage: str = '') -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'vtuber.csv'
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'id', 'original', 'surface', 'image', 'image_credit', 'image_usage',
            'image_page', 'image_terms_page',
        ])
        writer.writeheader()
        writer.writerow(dict(id='938', original='大神ミオ', surface='ミオ', image=url,
                             image_credit=credit, image_usage=usage))
    return path


def _download(tmp_path, monkeypatch):
    def download(url, *args, **kwargs):
        path = tmp_path / ('source.webp' if url.endswith('.webp') else 'source.png')
        Image.new('RGB', (8, 8), 'blue' if url.endswith('.webp') else 'red').save(path)
        return path
    monkeypatch.setattr(prewarm, 'download_image', download)


def test_shared_identity_updates_both_clients_and_legacy_url(tmp_path, monkeypatch):
    _download(tmp_path, monkeypatch)
    store = tmp_path / 'store'
    wordlists = tmp_path / 'dev'
    old_url, new_url = 'https://example.test/mio.png', 'https://example.test/mio.webp'
    path = _csv(wordlists, old_url)
    old_csv = _csv(tmp_path / 'public', old_url)
    prewarm.sync_asset_store([path], store, wordlists_dir=wordlists)
    _csv(wordlists, new_url)
    result = prewarm.sync_asset_store(
        [path], store, wordlists_dir=wordlists, compatibility_csv_paths=[old_csv],
    )
    assert result['promoted'] == 1
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    public_row = dict(id='938', original='大神ミオ', image=old_url, image_credit='COVER')
    dev_row = {**public_row, 'image': new_url}
    assert asset_store.resolve_word_row('vtuber', public_row) == asset_store.resolve_word_row(
        'vtuber', dev_row,
    )
    assert asset_store.resolve_word_row('vtuber', public_row)['image'] == new_url
    assert video.download_image(old_url, tmp_path / 'cache') == video.download_image(
        new_url, tmp_path / 'cache',
    )
    assert asset_store.verified_preview_asset(old_url)[1] == asset_store.local_asset(new_url)[1]
    assert asset_store.local_credit(old_url)[1]['credit_text'] == 'COVER'
    # Historical clients remain compatible after subsequent synchronizations.
    prewarm.sync_asset_store([path], store, wordlists_dir=wordlists)
    assert asset_store.local_asset(old_url)[1] == asset_store.local_asset(new_url)[1]
    # Switching back must fetch the original URL, rather than adopting its alias.
    _csv(wordlists, old_url)
    prewarm.sync_asset_store([path], store, wordlists_dir=wordlists)
    adopted = asset_store.resolve_word_row('vtuber', public_row)
    assert adopted['image'] == old_url
    with Image.open(asset_store.local_asset(old_url)[1]) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)


def test_failed_replacement_preserves_image_and_credit_together(tmp_path, monkeypatch):
    _download(tmp_path, monkeypatch)
    store, wordlists = tmp_path / 'store', tmp_path / 'words'
    path = _csv(wordlists, 'https://example.test/mio.png', credit='old credit')
    prewarm.sync_asset_store([path], store, wordlists_dir=wordlists)
    before = (store / 'manifest.json').read_bytes()
    _csv(wordlists, 'https://example.test/missing.webp', credit='new credit')
    monkeypatch.setattr(prewarm, 'download_image', lambda *a, **k: None)
    result = prewarm.sync_asset_store([path], store, wordlists_dir=wordlists)
    assert result['failed'] == 1 and result['promoted'] == 0
    assert (store / 'manifest.json').read_bytes() == before
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    row = asset_store.resolve_word_row('vtuber', dict(id='938', original='大神ミオ'))
    assert row['image'] == 'https://example.test/mio.png'
    assert row['image_credit'] == 'old credit'


def test_namespace_variants_and_custom_lists_stay_distinct(tmp_path, monkeypatch):
    _download(tmp_path, monkeypatch)
    store, wordlists = tmp_path / 'store', tmp_path / 'words'
    path = _csv(wordlists, 'https://example.test/current.webp')
    with path.open('a') as f:
        f.write('938,旧名,旧名,https://example.test/old.png,old credit,,,\n')
    other = wordlists / 'other.csv'
    other.write_text(path.read_text().replace('current.webp', 'other.png'))
    prewarm.sync_asset_store([path, other], store, wordlists_dir=wordlists)
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    current = dict(id='938', original='大神ミオ', image='stale')
    assert asset_store.resolve_word_row('vtuber', current)['image'].endswith('current.webp')
    assert asset_store.resolve_word_row('other', current)['image'].endswith('other.png')
    assert asset_store.resolve_word_row('vtuber', dict(id='938', original='旧名'))[
        'image'
    ].endswith('old.png')
    monkeypatch.setattr(convert, 'WORDLISTS_DIR', wordlists)
    custom = _csv(tmp_path / 'custom', 'https://example.test/custom.png')
    assert convert._load_wordlist_rows(custom)['938'][0]['image'].endswith('custom.png')
    assert convert._load_wordlist_rows(path)['938'][0]['image'].endswith('current.webp')


def test_changed_terms_do_not_alias_old_clients(tmp_path, monkeypatch):
    _download(tmp_path, monkeypatch)
    store, wordlists = tmp_path / 'store', tmp_path / 'words'
    old_url, new_url = 'https://example.test/mio.png', 'https://example.test/mio.webp'
    path = _csv(wordlists, old_url)
    old_csv = _csv(tmp_path / 'public', old_url)
    prewarm.sync_asset_store([path], store, wordlists_dir=wordlists)
    _csv(wordlists, new_url, usage='noncommercial_fanwork')
    prewarm.sync_asset_store([path], store, wordlists_dir=wordlists,
                            compatibility_csv_paths=[old_csv], allow_noncommercial_fanwork=True)
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    assert asset_store.manifest_entry(old_url) is None
    adopted = asset_store.resolve_word_row('vtuber', dict(id='938', original='大神ミオ'))
    assert adopted['image_usage'] == 'noncommercial_fanwork'
    assert adopted['image'] == new_url
    assert 'orphaned_at' in json.loads((store / 'manifest.json').read_text())['assets'][old_url]
