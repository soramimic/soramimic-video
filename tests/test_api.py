"""APIサーバー(api.py)のテスト。

パイプライン本体はモックし、ジョブの受付→実行→動画取得の流れと
APIキー認証を確認する。NEUTRINO実行込みのE2Eは手動(serve)で行う。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"


@pytest.fixture
def client(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "video" / "song.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_MP4)
        job.stages.append({"name": "synthesize", "seconds": 0.0})
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    return TestClient(app)


def wait_done(client: TestClient, job_id: str, **kw) -> dict:
    for _ in range(200):
        res = client.get(f"/api/jobs/{job_id}", **kw)
        assert res.status_code == 200
        body = res.json()
        if body["status"] in ("done", "error", "canceled"):
            return body
        time.sleep(0.02)
    raise AssertionError("ジョブが終わりません")


def submit(client: TestClient, **fields) -> str:
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    if "editor" in fields:
        files["editor"] = ("editor.json", fields.pop("editor"), "application/json")
    res = client.post("/api/jobs", files=files, data=fields)
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_job_flow_with_editor(client):
    job_id = submit(client, editor=b'{"format": "soramimic-editor/1"}')
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["parody_source"] == "editor"
    res = client.get(body["video_url"])
    assert res.status_code == 200
    assert res.content == FAKE_MP4


def test_requires_editor_or_wordlist(client):
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    res = client.post("/api/jobs", files=files)
    assert res.status_code == 422

    job_id = submit(client, wordlist="stations")
    assert wait_done(client, job_id)["params"]["parody_source"] == "convert"


def test_rejects_unknown_synthesizer(client):
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    res = client.post(
        "/api/jobs",
        files=files,
        data={"wordlist": "stations", "synthesizer": "bogus"},
    )
    assert res.status_code == 422


def test_accepts_voicevox_params(client):
    job_id = submit(
        client, wordlist="stations", synthesizer="voicevox", voicevox_style="3001"
    )
    body = wait_done(client, job_id)
    assert body["params"]["synthesizer"] == "voicevox"
    assert body["params"]["voicevox_style"] == 3001


def test_config_has_voicevox_key(client):
    body = client.get("/api/config").json()
    assert "voicevox" in body  # 起動していればstyles、いなければNone


def test_preview_returns_audio(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        assert job.params["preview"] == 20.0
        out = job.dir / "neutrino" / "vocal.wav"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"RIFF-fake")
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    # プレビューはeditor/wordlistなしでも受け付ける(元歌詞で合成するため)
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    res = client.post("/api/jobs", files=files, data={"preview": "20"})
    assert res.status_code == 200
    body = wait_done(client, res.json()["id"])
    assert body["result_kind"] == "audio"
    video = client.get(body["video_url"])
    assert video.headers["content-type"] == "audio/wav"


def test_truncate_project():
    from types import SimpleNamespace

    def make_project():
        return SimpleNamespace(
            notes=[SimpleNamespace(id=i, start_sec=float(i)) for i in range(5)],
            lines=[
                SimpleNamespace(note_ids=[0, 1]),
                SimpleNamespace(note_ids=[2, 3]),
                SimpleNamespace(note_ids=[4]),
            ],
        )

    # 起点0から3秒: start_sec 0,1,2 を残す
    project = make_project()
    api_mod._truncate_project(project, 3.0)
    assert [n.id for n in project.notes] == [0, 1, 2]
    assert [ln.note_ids for ln in project.lines] == [[0, 1], [2]]

    # 起点2秒から2秒: [2, 4) に入る start_sec 2,3 を残す(前奏スキップ相当)
    project = make_project()
    api_mod._truncate_project(project, 2.0, start=2.0)
    assert [n.id for n in project.notes] == [2, 3]
    assert [ln.note_ids for ln in project.lines] == [[2, 3]]


def test_first_lyric_start():
    from types import SimpleNamespace

    # 歌詞(kana)のある最初の音符の開始秒を起点にする
    notes = [
        SimpleNamespace(id=0, start_sec=10.0, kana=""),
        SimpleNamespace(id=1, start_sec=30.0, kana="ア"),
        SimpleNamespace(id=2, start_sec=40.0, kana="イ"),
    ]
    project = SimpleNamespace(notes=notes)
    assert api_mod._first_lyric_start(project) == 30.0

    # 音符が無ければ0にフォールバック
    assert api_mod._first_lyric_start(SimpleNamespace(notes=[])) == 0.0


def test_trim_wav_head(tmp_path):
    import shutil
    import subprocess
    import wave

    # start<=0 は何もしない
    wav = tmp_path / "vocal.wav"
    wav.write_bytes(b"")
    assert api_mod._trim_wav_head(wav, 0.0) == wav

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpegがない環境")

    # 3秒の無音WAVの頭2秒を切ると約1秒になる
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "3", str(wav)],
        check=True, capture_output=True,
    )
    out = api_mod._trim_wav_head(wav, 2.0)
    assert out != wav
    with wave.open(str(out)) as w:
        duration = w.getnframes() / w.getframerate()
    assert 0.9 <= duration <= 1.1


def _running_synth_job(**kw) -> api_mod.Job:
    job = api_mod.Job(
        id="x", dir=Path("/tmp"), params={}, status="running", stage="synthesize"
    )
    for k, v in kw.items():
        setattr(job, k, v)
    return job


def test_to_dict_uses_real_neutrino_progress():
    # 50%到達までに10秒 → 残りも約10秒と見積る
    job = _running_synth_job(
        stage_started_at=time.time() - 10, stage_progress=50
    )
    d = job.to_dict(with_log=False)
    assert d["stage_progress"] == 50
    assert 8 <= d["stage_eta_seconds"] <= 12


def test_to_dict_falls_back_to_estimate_without_real_progress():
    # 実進捗なし・見積り総秒40秒・経過10秒 → 25%、残り約30秒
    job = _running_synth_job(
        stage_started_at=time.time() - 10, stage_estimated_total=40.0
    )
    d = job.to_dict(with_log=False)
    assert d["stage_progress"] == 25
    assert 29 <= d["stage_eta_seconds"] <= 31


def test_to_dict_no_progress_for_other_stages():
    job = _running_synth_job(stage="mix", stage_started_at=time.time())
    d = job.to_dict(with_log=False)
    assert "stage_progress" not in d
    assert "stage_eta_seconds" not in d


def test_cancel_running_and_queued(tmp_path, monkeypatch):
    from soramimic_video import runproc

    def slow_pipeline(job, config):
        for _ in range(100):
            time.sleep(0.02)
            runproc.raise_if_cancelled()
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", slow_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    running = submit(client, editor=b"{}")
    queued = submit(client, editor=b"{}")
    time.sleep(0.1)  # 1件目が実行中になるのを待つ

    # 順番待ちのジョブは即座にcanceledになり、実行されない
    res = client.post(f"/api/jobs/{queued}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "canceled"

    # 実行中のジョブは中断チェックで止まる
    client.post(f"/api/jobs/{running}/cancel")
    body = wait_done(client, running)
    assert body["status"] == "canceled"
    assert client.get(f"/api/jobs/{queued}").json()["status"] == "canceled"
    # 完了済みジョブへのcancelは何もしない
    assert client.post(f"/api/jobs/{running}/cancel").json()["status"] == "canceled"


def test_runproc_kill_current():
    import threading
    import time as _time

    from soramimic_video import runproc

    result = {}

    def target():
        result["proc"] = runproc.run(["sleep", "5"], capture_output=True)

    t = threading.Thread(target=target)
    started = _time.time()
    t.start()
    _time.sleep(0.2)
    assert runproc.kill_current()
    t.join(timeout=3)
    assert not t.is_alive()
    assert _time.time() - started < 3
    assert result["proc"].returncode != 0


def test_rejects_non_midi(client):
    res = client.post("/api/jobs", files={"midi": ("x.mid", b"not midi", "audio/midi")})
    assert res.status_code == 400


def test_config_lists_layouts(client):
    conf = client.get("/api/config").json()
    assert "default" in conf["layouts"] and "caption" in conf["layouts"]


def test_get_builtin_layout(client):
    body = client.get("/api/layouts/default").json()
    assert body["elements"][0]["type"] == "image"
    assert client.get("/api/layouts/no-such").status_code == 404


def test_rejects_bad_layout(client):
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    # 不正なJSONは投入前に400で返す
    res = client.post("/api/jobs", files=files,
                      data={"wordlist": "stations", "layout_json": "{oops"})
    assert res.status_code == 400
    res = client.post("/api/jobs", files=files,
                      data={"wordlist": "stations",
                            "layout_json": '{"elements": [{"type": "nope", "box": [0,0,1,1]}]}'})
    assert res.status_code == 400
    # 存在しないレイアウト名も400
    res = client.post("/api/jobs", files=files,
                      data={"wordlist": "stations", "layout": "no-such-layout"})
    assert res.status_code == 400


def test_wordlist_columns(client, tmp_path):
    # 未指定でも替え歌単語のフィールドは返る
    cols = client.get("/api/wordlist-columns").json()["columns"]
    assert "surface" in cols and "original" in cols
    # CSVパスを渡すとその列も返る(重複は除去)
    csv_path = tmp_path / "wl.csv"
    csv_path.write_text("id,original,surface,achievement\n0,a,b,c", encoding="utf-8")
    body = client.get(f"/api/wordlist-columns?wordlist={csv_path}").json()
    cols = body["columns"]
    assert "achievement" in cols
    assert cols.count("original") == 1
    # 代表行(WYSIWYG表示のサンプル)も返る
    assert body["row"]["achievement"] == "c"
    # 見つからないリスト名でもエラーにしない
    res = client.get("/api/wordlist-columns?wordlist=no-such-list")
    assert res.status_code == 200


def test_layout_json_saved_to_job_dir(client):
    spec = '{"elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.1, 0.8, 0.2]}]}'
    job_id = submit(client, wordlist="stations", layout="caption", layout_json=spec)
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["layout"] == "caption"
    manager = client.app.state.manager
    assert (manager.jobs[job_id].dir / "layout.json").read_text(encoding="utf-8") == spec


def test_video_not_ready(client, monkeypatch):
    # 実行前に取りに来たら409
    slow = api_mod.run_pipeline

    def slow_pipeline(job, config):
        time.sleep(0.3)
        return slow(job, config)

    monkeypatch.setattr(api_mod, "run_pipeline", slow_pipeline)
    job_id = submit(client, editor=b"{}")
    res = client.get(f"/api/jobs/{job_id}/video")
    assert res.status_code == 409
    wait_done(client, job_id)


def test_api_key_auth(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    monkeypatch.setenv(api_mod.API_KEY_ENV, "secret-key")
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    assert client.post("/api/jobs", files=files).status_code == 401
    assert client.get("/api/jobs").status_code == 401
    # configは鍵なしでも auth_required だけ返す
    assert client.get("/api/config").json() == {"auth_required": True}

    headers = {"X-API-Key": "secret-key"}
    res = client.post(
        "/api/jobs", files=files, data={"editor": ""}, headers=headers
    )
    assert res.status_code == 422  # 認証は通り、入力バリデーションで弾かれる

    files["editor"] = ("editor.json", b"{}", "application/json")
    res = client.post("/api/jobs", files=files, headers=headers)
    assert res.status_code == 200
    job_id = res.json()["id"]
    body = wait_done(client, job_id, headers=headers)
    assert body["status"] == "done"
    # <video>タグ用にクエリパラメータでも通る
    assert client.get(f"/api/jobs/{job_id}/video?api_key=secret-key").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/video?api_key=wrong").status_code == 401


def test_restart_recovers_history(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    jobs_dir = tmp_path / "jobs"
    client = TestClient(api_mod.create_app(jobs_dir=jobs_dir))
    job_id = submit(client, editor=b"{}")
    wait_done(client, job_id)

    # APIのstatusはメモリ上で先に"done"になり、status.jsonへの保存はその直後に
    # ワーカーが行う。再起動(履歴の読み直し)は永続化が終わってから行う
    import json as json_mod

    status_path = jobs_dir / job_id / api_mod.STATUS_FILENAME
    for _ in range(200):
        try:
            if json_mod.loads(status_path.read_text())["status"] == "done":
                break
        except (OSError, ValueError, KeyError):
            pass  # 未作成・書き込み途中
        time.sleep(0.02)
    else:
        raise AssertionError("status.jsonが書き込まれません")

    client2 = TestClient(api_mod.create_app(jobs_dir=jobs_dir))
    jobs = client2.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["status"] == "done"
    assert client2.get(f"/api/jobs/{job_id}/video").content == FAKE_MP4
