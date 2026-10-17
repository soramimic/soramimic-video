from __future__ import annotations

import io
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from soramimic_video import api, thumbnail, thumbnail_preview, title_choices
from soramimic_video.thumbnail_preview import PreviewSpec

WORDS = [{"id": "1", "surface": "ハンゼン"}, {"id": "2", "surface": "ホフ"}]
CSV = (
    "id,original,surface,pronunciation,field,image\n"
    "1,ペーター・ハンゼン,ハンゼン,ハンゼン,天文学,\n"
    "2,マーシャン・ホフ,ホフ,ホフ,計算機科学,\n"
    "3,アントン・パンネクーク,パンネクーク,パンネクーク,天文学,"
)


@pytest.fixture
def wordlist(tmp_path, monkeypatch):
    root = tmp_path / "wordlists"
    root.mkdir()
    path = root / "test.csv"
    path.write_text(CSV, encoding="utf-8")
    monkeypatch.setattr("soramimic_video.convert.WORDLISTS_DIR", root)
    return path


@pytest.fixture
def client(tmp_path, wordlist, monkeypatch):
    monkeypatch.setattr(api, "run_pipeline", lambda job, config: job.dir / "video.mp4")
    return TestClient(api.create_app(jobs_dir=tmp_path / "jobs"))


def test_balance_search_offers_multiword_title_and_preserves_rows(wordlist):
    candidates = title_choices.find_title_choices("サンゲンショク", wordlist)
    chosen = next(c for c in candidates if c["words"] == WORDS)
    assert candidates[0] == chosen
    assert chosen["label"] == "ハンゼン ＋ ホフ"
    assert chosen["reading"] == "ハンゼン・ホフ"
    assert chosen["originals"] == ["ペーター・ハンゼン", "マーシャン・ホフ"]
    filtered = title_choices.generate_choices("サンゲンショク", wordlist, "field=天文学")
    assert all(w["id"] != "2" for c in filtered for w in c["words"])


@pytest.mark.parametrize("value", [
    "null", "{}", "[]", "[null]", '[{"surface":"ホフ"}]',
    '[{"id":"2","surface":"ホフ","image":"file:///etc/passwd"}]',
    '[{"id":2,"surface":"ホフ"}]', json.dumps(WORDS * 3), "x" * 4097,
])
def test_selection_rejects_non_identity_payloads(value):
    with pytest.raises(ValueError, match="候補"):
        title_choices.parse_title_words(value)


def test_selection_resolves_exact_rows_and_filter():
    assert title_choices.parse_title_words("") is None
    rows = title_choices.selected_rows(WORDS, CSV)
    assert [r["original"] for r in rows] == ["ペーター・ハンゼン", "マーシャン・ホフ"]
    with pytest.raises(ValueError, match="リスト"):
        title_choices.selected_rows(WORDS, CSV, "field=天文学")
    with pytest.raises(ValueError, match="リスト"):
        title_choices.selected_rows([{"id": "2", "surface": "別名"}], CSV)


def test_manual_words_override_lyric_params_and_match_preview(wordlist, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("explicit title words must bypass automatic conversion")

    monkeypatch.setattr(thumbnail, "run_convert", forbidden)
    strong = {"MID_PHRASE_BREAK_PENALTY": 160}
    words, images, _credits = thumbnail.resolve_headline(
        "サンゲンショク", str(wordlist), params=strong, title_words=WORDS,
    )
    assert words == ["ハンゼン", "ホフ"]
    assert images == []
    auto = PreviewSpec.create("サンゲンショク", "test", with_images=False)
    manual = PreviewSpec.create("サンゲンショク", "test", with_images=False, title_words=WORDS)
    assert auto.key != manual.key
    path = manual.render(tmp_path / "preview")
    assert path is not None
    output = thumbnail.build_thumbnail(
        tmp_path / "final.png", "サンゲンショク", "test", params=strong,
        width=manual.width, height=manual.height, title_words=WORDS,
    )
    assert output is not None
    assert path.read_bytes() == output.read_bytes()


def test_candidates_endpoint_uses_sample_reading_and_private_response(client, monkeypatch):
    monkeypatch.setattr(api, "sample_entry", lambda _id: {"title": "紅葉", "title_kana": "モミジ"})
    seen = []

    def find(title, path, where, **kwargs):
        seen.append((title, path.name, where))
        return [{"words": WORDS}]

    monkeypatch.setattr(title_choices, "find_title_choices", find)
    res = client.post("/api/title-candidates", json={
        "sample": "momiji", "wordlist": "test", "where": "field=天文学",
    })
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    assert res.json() == {"candidates": [{"words": WORDS}]}
    assert seen == [("モミジ", "test.csv", "field=天文学")]


def test_custom_candidates_normalize_the_same_ids_as_submission(client, monkeypatch):
    seen = []
    monkeypatch.setattr(title_choices, "find_title_choices", lambda title, path, where, **kw:
                        seen.append((path, kw["csv_text"])) or [])
    res = client.post("/api/title-candidates", json={
        "title": "サンゲンショク", "wordlist_text": "ハンゼン,ハンゼン\nホフ,ホフ",
    })
    assert res.status_code == 200
    assert seen == [(None, "id,original,surface,pronunciation\n"
                    "1,ハンゼン,ハンゼン,ハンゼン\n2,ホフ,ホフ,ホフ")]


def test_candidates_reject_paths_and_require_auth(client, monkeypatch):
    response = client.post("/api/title-candidates", json={"title": "曲", "wordlist": "/etc/passwd"})
    assert response.status_code == 404
    monkeypatch.setenv(api.API_KEY_ENV, "private")
    assert client.post("/api/title-candidates", json={}).status_code == 401


def test_selected_preview_is_private_and_rejects_stale_rows(client, tmp_path):
    params = {"title": "サンゲンショク", "wordlist": "test", "images": "0",
              "title_words": json.dumps(WORDS)}
    response = client.get("/api/thumbnail-preview", params=params)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert not list((tmp_path / "jobs" / "thumbnail-preview-cache").glob("*.png"))
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (640, 360)
    response = client.get("/api/thumbnail-preview", params={**params, "where": "field=天文学"})
    assert response.status_code == 422


def test_custom_preview_uses_selected_sequence(client):
    response = client.post("/api/thumbnail-preview", json={
        "title": "サンゲンショク", "wordlist_name": "名前", "wordlist_text": CSV,
        "title_words": json.dumps(WORDS),
    })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    invalid = client.post("/api/thumbnail-preview", json={
        "title": "サンゲンショク", "wordlist_text": "別の単語,ベツノタンゴ",
        "title_words": json.dumps(WORDS),
    })
    assert invalid.status_code == 422


def test_selection_keeps_each_words_image_and_credit(wordlist, tmp_path, monkeypatch):
    seen = []
    paths = [tmp_path / "hansen.png", tmp_path / "hoff.png"]

    def image_for(row, *_args, **_kwargs):
        seen.append(row["original"])
        return paths[len(seen) - 1], f"credit {row['id']}"

    monkeypatch.setattr(thumbnail, "_word_image", image_for)
    words, images, credits = thumbnail.resolve_headline(
        "サンゲンショク", str(wordlist), title_words=WORDS, image_cache=tmp_path,
    )
    assert words == ["ハンゼン", "ホフ"]
    assert seen == ["ペーター・ハンゼン", "マーシャン・ホフ"]
    assert images == paths and credits == ["credit 1", "credit 2"]


def test_submission_persists_selection_and_rejects_stale_filter(client):
    fields = {"wordlist": "test", "title_words": json.dumps(WORDS)}
    files = {"midi": ("song.mid", b"MThd" + b"\0" * 16, "audio/midi")}
    response = client.post("/api/jobs", data=fields, files=files)
    assert response.status_code == 200, response.text
    job = client.get(f"/api/jobs/{response.json()['id']}").json()
    assert job["params"]["title_words"] == WORDS
    stale = client.post("/api/jobs", data={**fields, "where": "field=天文学"}, files=files)
    assert stale.status_code == 422


@pytest.mark.parametrize("original", [False, True])
def test_editor_submission_validates_against_its_own_wordlist(client, original):
    payload = {
        "format": "soramimic-editor/1", "results": [[]], "unitsList": [[]],
        "wordlist": (
            {"value": "ORIGINAL", "csvText": CSV}
            if original else {"filepath": "test.csv"}
        ),
    }

    def submit():
        return client.post("/api/jobs", data={
            "wordlist": "another-list", "title_words": json.dumps(WORDS),
        }, files={
            "midi": ("song.mid", b"MThd" + b"\0" * 16, "audio/midi"),
            "editor": ("lyrics.json", json.dumps(payload), "application/json"),
        })

    response = submit()
    assert response.status_code == 200, response.text
    job = client.get(f"/api/jobs/{response.json()['id']}").json()
    assert job["params"]["title_words"] == WORDS
    if original:
        payload["wordlist"]["csvText"] = CSV.replace("ホフ", "別人")
    else:
        payload["where"] = "field=天文学"
    assert submit().status_code == 422


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_candidate_worker_cleans_private_inputs(monkeypatch, timeout):
    directories = []

    def fail(cmd, **kwargs):
        directory = Path(cmd[-1])
        directories.append(directory)
        assert kwargs["timeout"] <= 20
        assert json.loads((directory / "request.json").read_text())["title"] == "private title"
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 20)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(title_choices.subprocess, "run", fail)
    with pytest.raises(TimeoutError if timeout else ValueError):
        title_choices.find_title_choices("private title", None, csv_text=CSV)
    assert directories and not directories[0].exists()


def test_candidates_wait_for_an_active_thumbnail_without_adding_a_worker(monkeypatch):
    started = threading.Event()
    waiting = threading.Event()
    render_slot = thumbnail_preview.render_slot

    @contextmanager
    def slot(timeout):
        waiting.set()
        with render_slot(timeout=timeout):
            yield

    def worker(cmd, **kwargs):
        started.set()
        assert 0 < kwargs["timeout"] < 18
        (Path(cmd[-1]) / "choices.json").write_text(json.dumps([{"words": WORDS}]))

    monkeypatch.setattr(title_choices.subprocess, "run", worker)
    monkeypatch.setattr(thumbnail_preview, "render_slot", slot)
    # A title request must share the existing render slot, including when its
    # current thumbnail takes longer than the old two-second queue limit.
    with ThreadPoolExecutor(max_workers=1) as executor:
        thumbnail_preview._render_lock.acquire()
        try:
            result = executor.submit(title_choices.find_title_choices, "サンゲンショク", None,
                                     csv_text=CSV)
            assert waiting.wait(timeout=5)
            assert not started.wait(timeout=2.2)
        finally:
            thumbnail_preview._render_lock.release()
        assert result.result(timeout=5) == [{"words": WORDS}]


@pytest.mark.parametrize("elapsed", [7, 21])
def test_candidate_queue_time_counts_toward_the_worker_deadline(monkeypatch, elapsed):
    from soramimic_video.custom_thumbnail_preview import WORKER_TIMEOUT_SECONDS

    clock = iter([100., 100. + elapsed])
    monkeypatch.setattr(title_choices, "monotonic", lambda: next(clock))

    @contextmanager
    def slot(timeout):
        assert timeout == WORKER_TIMEOUT_SECONDS
        yield

    monkeypatch.setattr(thumbnail_preview, "render_slot", slot)
    calls = []

    def worker(cmd, **kwargs):
        calls.append(kwargs["timeout"])
        (Path(cmd[-1]) / "choices.json").write_text("[]")

    monkeypatch.setattr(title_choices.subprocess, "run", worker)
    if elapsed >= WORKER_TIMEOUT_SECONDS:
        with pytest.raises(TimeoutError):
            title_choices.find_title_choices("曲名", None, csv_text=CSV)
        assert calls == []
    else:
        assert title_choices.find_title_choices("曲名", None, csv_text=CSV) == []
        assert calls == [WORKER_TIMEOUT_SECONDS - elapsed]
