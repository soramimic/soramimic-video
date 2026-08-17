import json

import pytest

from soramimic_video import convert, private_wordlists


def _manifest(tmp_path, *, csv="fanwork_private.csv"):
    wordlist = tmp_path / csv
    wordlist.write_text("id,surface\n1,example", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "version": 1,
        "wordlists": [{
            "name": "fanwork_private",
            "label": "非公開ファン素材",
            "phrase": "非公開素材名",
            "layout": "youtuber_card",
            "csv": csv,
        }],
    }), encoding="utf-8")
    return manifest, wordlist


def test_resolve_private_named_wordlist_only_when_not_public(tmp_path, monkeypatch):
    manifest, expected = _manifest(tmp_path)
    monkeypatch.setenv(private_wordlists.PRIVATE_WORDLIST_MANIFEST_ENV, str(manifest))
    monkeypatch.delenv(private_wordlists.PUBLIC_ENV, raising=False)
    assert convert.resolve_wordlist("fanwork_private") == expected.resolve()
    assert private_wordlists.editor_entries()[0]["filepath"].endswith(
        "fanwork_private.csv"
    )

    monkeypatch.setenv(private_wordlists.PUBLIC_ENV, "1")
    assert private_wordlists.entries() == {}
    with pytest.raises(FileNotFoundError):
        convert.resolve_wordlist("fanwork_private")


@pytest.mark.parametrize("csv", ["../secret.csv", "nested/list.csv", "/tmp/x.csv"])
def test_private_manifest_rejects_paths(tmp_path, monkeypatch, csv):
    manifest, _wordlist = _manifest(tmp_path)
    data = json.loads(manifest.read_text())
    data["wordlists"][0]["csv"] = csv
    manifest.write_text(json.dumps(data))
    monkeypatch.setenv(private_wordlists.PRIVATE_WORDLIST_MANIFEST_ENV, str(manifest))
    assert private_wordlists.entries() == {}
