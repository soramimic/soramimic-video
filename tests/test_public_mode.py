"""公開モード(SORAMIMIC_PUBLIC)のテスト。

セッション分離・投入制限・自動削除・Turnstile・サンプル差し替えを確認する。
環境変数を設定しない従来の挙動は tests/test_api.py 側で担保する。
"""

from __future__ import annotations

import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"


def fast_pipeline(job, config):
    out = job.dir / "song.mp4"
    out.write_bytes(FAKE_MP4)
    return out


def submit(client: TestClient, **fields):
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    return client.post("/api/jobs", files=files, data={"wordlist": "stations", **fields})


def wait_done(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "error", "canceled"):
            return body
        time.sleep(0.02)
    raise AssertionError("ジョブが終わりません")


@pytest.fixture
def public_app(tmp_path, monkeypatch):
    """公開モードのアプリ。パイプラインと曲長判定はモックする。"""
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    # ダミーMIDIは解析できないので、曲長は0秒(=上限に引っかからない)扱いにする
    monkeypatch.setattr(api_mod, "song_seconds", lambda midi_bytes: 0.0)
    return api_mod.create_app(jobs_dir=tmp_path / "jobs")


def test_session_cookie_issued_once(public_app):
    client = TestClient(public_app)
    res = client.get("/api/config")
    assert api_mod.SESSION_COOKIE in res.cookies
    sid = res.cookies[api_mod.SESSION_COOKIE]
    assert len(sid) == 32
    # 2回目は既存のcookieを使い回すので発行し直さない
    res2 = client.get("/api/config")
    assert api_mod.SESSION_COOKIE not in res2.cookies
    assert client.cookies[api_mod.SESSION_COOKIE] == sid


def test_jobs_are_isolated_per_session(public_app):
    alice = TestClient(public_app)
    bob = TestClient(public_app)
    a_id = submit(alice).json()["id"]
    b_id = submit(bob).json()["id"]
    wait_done(alice, a_id)
    wait_done(bob, b_id)

    assert [j["id"] for j in alice.get("/api/jobs").json()] == [a_id]
    assert [j["id"] for j in bob.get("/api/jobs").json()] == [b_id]
    # 他人のジョブは存在しないものとして扱う(詳細・動画・中断すべて404)
    assert alice.get(f"/api/jobs/{b_id}").status_code == 404
    assert alice.get(f"/api/jobs/{b_id}/video").status_code == 404
    assert alice.post(f"/api/jobs/{b_id}/cancel").status_code == 404
    # 自分のジョブは従来どおり取れる
    assert alice.get(f"/api/jobs/{a_id}/video").content == FAKE_MP4


def test_private_mode_keeps_sharing_jobs(tmp_path, monkeypatch):
    # 環境変数が未設定なら従来どおり: cookieも発行せず、全ジョブが誰からも見える
    monkeypatch.delenv(api_mod.PUBLIC_ENV, raising=False)
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    alice, bob = TestClient(app), TestClient(app)
    a_id = submit(alice).json()["id"]
    wait_done(alice, a_id)
    assert api_mod.SESSION_COOKIE not in alice.cookies
    assert [j["id"] for j in bob.get("/api/jobs").json()] == [a_id]
    assert bob.get(f"/api/jobs/{a_id}").status_code == 200


def test_owner_is_persisted_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    monkeypatch.setattr(api_mod, "song_seconds", lambda midi_bytes: 0.0)
    jobs_dir = tmp_path / "jobs"
    alice = TestClient(api_mod.create_app(jobs_dir=jobs_dir))
    job_id = submit(alice).json()["id"]
    wait_done(alice, job_id)
    sid = alice.cookies[api_mod.SESSION_COOKIE]
    saved = json.loads((jobs_dir / job_id / api_mod.STATUS_FILENAME).read_text())
    assert saved["owner"] == sid

    # 再起動後も同じcookieなら自分のジョブとして見え、別セッションからは見えない
    app2 = api_mod.create_app(jobs_dir=jobs_dir)
    again = TestClient(app2)
    again.cookies.set(api_mod.SESSION_COOKIE, sid)
    assert [j["id"] for j in again.get("/api/jobs").json()] == [job_id]
    assert TestClient(app2).get("/api/jobs").json() == []


def test_queue_limit_returns_429(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(api_mod.QUEUE_LIMIT_ENV, "1")
    monkeypatch.setattr(api_mod, "song_seconds", lambda midi_bytes: 0.0)

    def slow_pipeline(job, config):
        time.sleep(1.0)
        return fast_pipeline(job, config)

    monkeypatch.setattr(api_mod, "run_pipeline", slow_pipeline)
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    alice, bob = TestClient(app), TestClient(app)
    assert submit(alice).status_code == 200
    # 上限は全体のキュー(ワーカーは1本)なので別セッションでも弾かれる
    res = submit(bob)
    assert res.status_code == 429
    assert "順番待ち" in res.json()["detail"]


def test_daily_quota_returns_429(public_app, monkeypatch):
    monkeypatch.setenv(api_mod.DAILY_QUOTA_ENV, "2")
    alice, bob = TestClient(public_app), TestClient(public_app)
    for _ in range(2):
        assert submit(alice).status_code == 200
    res = submit(alice)
    assert res.status_code == 429
    assert "1日に作れる本数" in res.json()["detail"]
    # クォータはセッションごとなので、別の人は投入できる
    assert submit(bob).status_code == 200


def test_song_length_limit_returns_400(public_app, monkeypatch):
    monkeypatch.setenv(api_mod.MAX_SONG_SECONDS_ENV, "420")
    monkeypatch.setattr(api_mod, "song_seconds", lambda midi_bytes: 600.0)
    res = submit(TestClient(public_app))
    assert res.status_code == 400
    assert "長すぎます" in res.json()["detail"]


def test_song_seconds_reads_real_midi(tmp_path):
    # 曲長判定は xfparse.analyze_midi の解析結果(最後の音符の終わり)を使う
    from helpers import build_xf_midi

    path = build_xf_midi(
        tmp_path / "s.mid",
        notes=[(0, 480, 60), (480, 480, 62)],  # 500000us/beat・480tpb = 1拍0.5秒
        lyric_events=[(0, "ア"), (480, "イ")],
    )
    assert api_mod.song_seconds(path.read_bytes()) == pytest.approx(1.0, abs=0.05)
    # 解析できないMIDIは None(上限チェックはスキップし、実行時のエラーに任せる)
    assert api_mod.song_seconds(FAKE_MIDI) is None


def test_job_ttl_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(api_mod.JOB_TTL_HOURS_ENV, "1")
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    monkeypatch.setattr(api_mod, "song_seconds", lambda midi_bytes: 0.0)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    job_id = submit(client).json()["id"]
    wait_done(client, job_id)
    manager = client.app.state.manager
    job_dir = manager.jobs[job_id].dir

    # まだTTL内なので消えない
    assert manager.cleanup_expired() == []
    manager.jobs[job_id].finished_at = time.time() - 2 * 3600
    assert manager.cleanup_expired() == [job_id]
    assert not job_dir.exists()
    assert client.get("/api/jobs").json() == []
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_job_ttl_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(api_mod.JOB_TTL_HOURS_ENV, raising=False)
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    job_id = submit(client).json()["id"]
    wait_done(client, job_id)
    manager = client.app.state.manager
    manager.jobs[job_id].finished_at = 0.0  # はるか昔に完了していても消さない
    assert manager.cleanup_expired() == []
    assert manager.jobs[job_id].dir.exists()


def test_turnstile_verification(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.TURNSTILE_SECRET_ENV, "secret")
    monkeypatch.setenv(api_mod.TURNSTILE_SITE_ENV, "site-key")
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    monkeypatch.setattr(
        api_mod, "verify_turnstile", lambda token, ip=None: token == "good"
    )
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    # サイトキーは /api/config 経由でフロントに渡る
    assert client.get("/api/config").json()["turnstile_site_key"] == "site-key"
    assert submit(client).status_code == 403  # トークン無し
    assert submit(client, turnstile_token="bad").status_code == 403
    assert submit(client, turnstile_token="good").status_code == 200


def test_turnstile_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(api_mod.TURNSTILE_SECRET_ENV, raising=False)
    monkeypatch.delenv(api_mod.TURNSTILE_SITE_ENV, raising=False)
    monkeypatch.setattr(api_mod, "run_pipeline", fast_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    assert "turnstile_site_key" not in client.get("/api/config").json()
    assert submit(client).status_code == 200  # トークン無しでも通る
    # 秘密鍵が無ければ検証自体を行わない
    assert api_mod.verify_turnstile("") is True


def test_turnstile_site_key_needs_both_keys(monkeypatch):
    monkeypatch.setenv(api_mod.TURNSTILE_SITE_ENV, "site-key")
    monkeypatch.delenv(api_mod.TURNSTILE_SECRET_ENV, raising=False)
    assert api_mod.turnstile_site_key() == ""
    monkeypatch.setenv(api_mod.TURNSTILE_SECRET_ENV, "secret")
    assert api_mod.turnstile_site_key() == "site-key"
    # 秘密鍵はあるがトークンが空なら検証は失敗(通信は行わない)
    assert api_mod.verify_turnstile("") is False


def test_samples_dir_override(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "samples.json").write_text(
        json.dumps([{"id": "mysong", "title": "自作サンプル"}]), encoding="utf-8"
    )
    (sample_dir / "mysong.mid").write_bytes(FAKE_MIDI)
    (sample_dir / "mysong_lyrics.txt").write_text("あいうえお", encoding="utf-8")
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(sample_dir))
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    assert client.get("/api/samples").json() == [{"id": "mysong", "title": "自作サンプル"}]
    assert client.get("/api/sample/mysong/midi").content == FAKE_MIDI
    assert client.get("/api/sample/mysong/lyrics").text == "あいうえお"
    # 差し替え先に無いIDは404(同梱サンプルも見に行かない)
    assert client.get("/api/sample/akatombo/midi").status_code == 404


def test_default_samples_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(api_mod.SAMPLES_DIR_ENV, raising=False)
    assert api_mod.samples_dir() == api_mod.STATIC_DIR / "sample"
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    assert any(s["id"] == "akatombo" for s in client.get("/api/samples").json())


def test_public_config_reports_limits(public_app, monkeypatch):
    monkeypatch.setenv(api_mod.DAILY_QUOTA_ENV, "3")
    monkeypatch.setenv(api_mod.MAX_SONG_SECONDS_ENV, "300")
    conf = TestClient(public_app).get("/api/config").json()
    assert conf["public"] is True
    assert conf["daily_quota"] == 3 and conf["max_song_seconds"] == 300


def test_private_config_has_no_public_keys(tmp_path, monkeypatch):
    monkeypatch.delenv(api_mod.PUBLIC_ENV, raising=False)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    conf = client.get("/api/config").json()
    assert "public" not in conf and "daily_quota" not in conf
