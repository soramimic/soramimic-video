"""APIサーバー(api.py)のテスト。

パイプライン本体はモックし、ジョブの受付→実行→動画取得の流れと
APIキー認証を確認する。NEUTRINO実行込みのE2Eは手動(serve)で行う。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402
from soramimic_video import audio_input as audio_input_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"


def fake_wav(seconds: float = 0.1, rate: int = 8000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * round(seconds * rate))
    return out.getvalue()


def encoded_audio(tmp_path: Path, suffix: str, codec: str, muxer: str) -> bytes:
    path = tmp_path / f"tone{suffix}"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=0.2",
            "-c:a", codec, "-f", muxer, "-y", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "video" / "song.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_MP4)
        job.stages.append({"name": "synthesize", "seconds": 0.0})
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(api_mod, "audio_input_available", lambda: True)
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


FAKE_EDITOR = b'{"format":"soramimic-editor/1","results":[[]],"unitsList":[[]]}'

def submit(client: TestClient, **fields) -> str:
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    if "editor" in fields:
        files["editor"] = ("editor.json", fields.pop("editor"), "application/json")
    res = client.post("/api/jobs", files=files, data=fields)
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_job_flow_with_editor(client):
    job_id = submit(client, editor=FAKE_EDITOR)
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["parody_source"] == "editor"
    res = client.get(body["video_url"])
    assert res.status_code == 200
    assert res.content == FAKE_MP4
    assert res.headers["content-disposition"].startswith("attachment;")
    playback = client.get(body["playback_url"])
    assert playback.status_code == 200
    assert playback.content == FAKE_MP4
    assert playback.headers["content-type"] == "video/mp4"
    assert playback.headers["content-disposition"].startswith("inline;")
    assert playback.headers["cache-control"] == "private, no-store"


def test_job_flow_accepts_wav_with_auto_lyrics_by_default(client):
    wav = fake_wav()
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", wav, "audio/wav")},
        data={"wordlist": "stations"},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["input_kind"] == "audio"
    assert body["params"]["auto_lyrics"] is True
    assert body["params"]["auto_phrase_lyrics"] is False
    assert body["song_label"] == "アップロードした曲"
    job = client.app.state.manager.jobs[job_id]
    assert (job.dir / "input.wav").read_bytes() == wav
    assert not (job.dir / "input.mid").exists()
    assert not (job.dir / "lyrics.txt").exists()
    assert client.get(body["playback_url"]).content == FAKE_MP4


def test_audio_job_persists_generation_quality_warning(tmp_path, monkeypatch):
    def difficult_pipeline(job, config):
        analysis_dir = job.dir / "analyze_audio"
        analysis_dir.mkdir()
        (analysis_dir / "analysis.json").write_text(
            json.dumps({"generation_quality": {"status": "warning"}}),
            encoding="utf-8",
        )
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", difficult_pipeline)
    monkeypatch.setattr(api_mod, "audio_input_available", lambda: True)
    browser = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    response = browser.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"wordlist": "stations"},
    )

    body = wait_done(browser, response.json()["id"])

    assert body["status"] == "done"
    assert body["generation_quality_warning"] is True


def test_wordlist_exhaustion_notice_survives_cleanup_and_restart(tmp_path, monkeypatch):
    def exhausted_pipeline(job, config):
        job.wordlist_exhausted = True
        (job.dir / "project.json").write_text("{}")
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", exhausted_pipeline)
    jobs_dir = tmp_path / "jobs"
    app = api_mod.create_app(jobs_dir=jobs_dir)
    app.state.manager.config["scrub_private_artifacts"] = True
    browser = TestClient(app)
    job_id = submit(browser, wordlist="stations")
    body = wait_done(browser, job_id)
    assert body["status"] == "done"
    assert body["wordlist_exhausted"] is True
    assert not (jobs_dir / job_id / "project.json").exists()
    restarted = TestClient(api_mod.create_app(jobs_dir=jobs_dir))
    assert restarted.get(f"/api/jobs/{job_id}").json()["wordlist_exhausted"] is True
    assert restarted.get("/api/jobs").json()[0]["wordlist_exhausted"] is True


def test_run_pipeline_detects_wordlist_exhaustion(tmp_path, monkeypatch):
    from soramimic_video import mix as mix_mod
    from soramimic_video import video as video_mod
    from soramimic_video import xfparse
    from soramimic_video.project import Line, Note, Project, SongInfo

    csv_path = tmp_path / "words.csv"
    csv_path.write_text("id,original,surface,pronunciation\n0,カ,カ,カ", encoding="utf-8")
    project = Project(
        song=SongInfo(midi_path="", ticks_per_beat=480),
        notes=[Note(i, 60, i * 480, (i + 1) * 480, i, i + 1, 0, "カ", "カ", "カ")
               for i in range(3)],
        lines=[Line(0, "カカカ", "カカカ", [0, 1, 2])],
    )
    monkeypatch.setattr(xfparse, "analyze_midi", lambda path: project)
    monkeypatch.setattr(api_mod, "_run_synthesize", lambda *a, **k: None)
    monkeypatch.setattr(mix_mod, "mix", lambda *a, **k: None)
    monkeypatch.setattr(video_mod, "make_video", lambda *a, **k: tmp_path / "out.mp4")
    job = api_mod.Job(id="notice", dir=tmp_path, params={"wordlist": str(csv_path)})
    api_mod.run_pipeline(job, {"parallel_video": False})
    assert job.wordlist_exhausted
    assert "wordlist_exhausted" not in job.to_dict()  # 完成前は表示しない
    job.video = tmp_path / "out.mp4"
    job.status = "done"
    assert job.to_dict()["wordlist_exhausted"] is True


def test_retried_audio_submission_returns_the_same_job(client):
    fields = {
        "wordlist": "stations",
        "submission_id": "a" * 32,
    }
    files = {"audio": ("voice.wav", fake_wav(), "audio/wav")}
    first = client.post("/api/jobs", files=files, data=fields)
    second = client.post("/api/jobs", files=files, data=fields)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(client.app.state.manager.jobs) == 1
    job = client.app.state.manager.jobs[first.json()["id"]]
    saved = json.loads((job.dir / api_mod.STATUS_FILENAME).read_text(encoding="utf-8"))
    assert saved["submission_id"] == fields["submission_id"]
    assert "submission_id" not in client.get(f"/api/jobs/{job.id}").json()["params"]


def test_rejects_invalid_audio_submission_id(client):
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"wordlist": "stations", "submission_id": "not-a-valid-id"},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "送信IDが不正です"


@pytest.mark.parametrize("adjust", [False, True])
@pytest.mark.parametrize("auto_phrase", [None, False, True])
def test_manual_correct_lyrics_mode_is_persisted(client, adjust, auto_phrase):
    wav = fake_wav()
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", wav, "audio/wav")},
        data={
            "wordlist": "stations",
            "auto_lyrics": "false",
            "lyrics": "正しい歌詞",
            "adjust_lyrics": str(adjust).lower(),
            **({"auto_phrase_lyrics": str(auto_phrase).lower()}
               if auto_phrase is not None else {}),
        },
    )
    assert res.status_code == 200, res.text
    body = wait_done(client, res.json()["id"])
    job = client.app.state.manager.jobs[body["id"]]
    assert body["params"]["auto_lyrics"] is False
    assert body["params"]["adjust_lyrics"] is adjust
    assert body["params"]["auto_phrase_lyrics"] is (auto_phrase is not False)
    assert (job.dir / "lyrics.txt").read_text(encoding="utf-8") == "正しい歌詞"


def test_manual_correct_lyrics_mode_requires_lyrics(client):
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"wordlist": "stations", "auto_lyrics": "false", "lyrics": "  "},
    )
    assert res.status_code == 422
    assert "正式な元歌詞" in res.json()["detail"]


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("control", ["\0", "\x1b"])
def test_audio_lyrics_reject_controls_before_creating_a_job(client, upload, control):
    source = "か\u3099" + control + "くせい"
    files = {"audio": ("voice.wav", fake_wav(), "audio/wav")}
    data = {"wordlist": "stations", "auto_lyrics": "false"}
    if upload:
        files["lyrics_file"] = ("lyrics.txt", source.encode(), "text/plain")
    else:
        data["lyrics"] = source
    res = client.post("/api/jobs", files=files, data=data)
    assert res.status_code == 400
    assert "position 3" in res.json()["detail"]
    assert f"U+{ord(control):04X}" in res.json()["detail"]
    assert client.app.state.manager.jobs == {}


@pytest.mark.parametrize("source,position", [("ｶﾞ｜声《カ★ナ》", 7), ("\ufeff声", 1)])
def test_audio_lyrics_reject_invalid_syntax_without_changing_source(client, source, position):
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav"),
               "lyrics_file": ("lyrics.txt", source.encode(), "text/plain")},
        data={"wordlist": "stations", "auto_lyrics": "false"},
    )
    assert res.status_code == 400
    assert f"position {position}" in res.json()["detail"]
    assert client.app.state.manager.jobs == {}


@pytest.mark.parametrize("kind,automatic", [("audio", True), ("midi", False)])
@pytest.mark.parametrize("option", ["adjust_lyrics", "auto_phrase_lyrics"])
def test_lyric_adjustment_rejects_automatic_or_midi_input(client, kind, automatic, option):
    content = fake_wav() if kind == "audio" else FAKE_MIDI
    res = client.post(
        "/api/jobs", files={kind: ("input.wav" if kind == "audio" else "input.mid", content)},
        data={"wordlist": "stations", "lyrics": "正しい歌詞",
              "auto_lyrics": str(automatic).lower(), option: "true"},
    )
    assert res.status_code == 422
    assert "音源と入力歌詞" in res.json()["detail"]


@pytest.mark.parametrize("source", ["正式な歌詞", "\r\n 正式な歌詞\t\r\n\n"])
def test_audio_accepts_utf8_lyrics_file_without_trusting_filename(client, tmp_path, source):
    escaped = tmp_path / "escaped.txt"
    res = client.post(
        "/api/jobs",
        files={
            "audio": ("voice.wav", fake_wav(), "audio/wav"),
            "lyrics_file": ("../../escaped.txt", source.encode(), "text/plain"),
        },
        data={"wordlist": "stations", "auto_lyrics": "false"},
    )
    assert res.status_code == 200, res.text
    body = wait_done(client, res.json()["id"])
    job = client.app.state.manager.jobs[body["id"]]
    assert (job.dir / "lyrics.txt").read_bytes() == source.encode("utf-8")
    assert not escaped.exists()


@pytest.mark.parametrize(
    ("filename", "content", "status"),
    [
        ("lyrics.pdf", b"words", 400),
        ("lyrics.txt", b"\xff", 400),
        ("lyrics.txt", b"a" * (api_mod.MAX_LYRICS_UPLOAD_BYTES + 1), 413),
    ],
)
def test_rejects_unsafe_lyrics_upload(client, filename, content, status):
    res = client.post(
        "/api/jobs",
        files={
            "audio": ("voice.wav", fake_wav(), "audio/wav"),
            "lyrics_file": (filename, content, "application/octet-stream"),
        },
        data={"wordlist": "stations"},
    )
    assert res.status_code == status


def test_job_flow_accepts_bundled_wav_preset(client, tmp_path, monkeypatch):
    samples = tmp_path / "audio-samples"
    samples.mkdir()
    (samples / "samples.json").write_text("[]", encoding="utf-8")
    (samples / "audio_samples.json").write_text(
        json.dumps([{
            "id": "demo_audio",
            "title": "音源サンプル",
            "original_credit": "PD song",
            "credit_notice": "Synthetic voice (CC BY 3.0)",
        }]),
        encoding="utf-8",
    )
    wav = fake_wav()
    (samples / "demo_audio.wav").write_bytes(wav)
    (samples / "demo_audio_lyrics.txt").write_text("あ", encoding="utf-8")
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(samples))

    listed = client.get("/api/samples").json()
    assert [(row["id"], row["input_kind"]) for row in listed] == [
        ("demo_audio", "audio")
    ]
    res = client.post(
        "/api/jobs", data={"sample_id": "demo_audio", "wordlist": "stations"}
    )
    assert res.status_code == 200, res.text
    body = wait_done(client, res.json()["id"])
    assert body["params"]["input_kind"] == "audio"
    assert body["params"]["sample_id"] == "demo_audio"
    assert body["params"]["auto_phrase_lyrics"] is True
    assert body["params"]["original_credit"] == "PD song"
    assert body["params"]["credit_notice"] == "Synthetic voice (CC BY 3.0)"
    job = client.app.state.manager.jobs[body["id"]]
    assert (job.dir / "input.wav").read_bytes() == wav
    assert not (job.dir / "input.mid").exists()


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobeが必要")
@pytest.mark.parametrize(
    ("filename", "content", "detail"),
    [
        ("voice.mp3", fake_wav(), "音声ファイルを読み取れません"),
        ("voice.wav", b"not a wav", "WAVファイルではありません"),
    ],
)
def test_rejects_invalid_audio(client, filename, content, detail):
    res = client.post(
        "/api/jobs",
        files={"audio": (filename, content, "application/octet-stream")},
        data={"wordlist": "stations"},
    )
    assert res.status_code == 400
    assert detail in res.json()["detail"]


def test_rejects_wav_with_midi_or_sample(client):
    files = {
        "audio": ("voice.wav", fake_wav(), "audio/wav"),
        "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
    }
    res = client.post("/api/jobs", files=files, data={"wordlist": "stations"})
    assert res.status_code == 422
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"sample_id": "furusato", "wordlist": "stations"},
    )
    assert res.status_code == 422


def test_wav_input_is_hidden_and_rejected_without_audio_extra(client, monkeypatch):
    monkeypatch.setattr(api_mod, "audio_input_available", lambda: False)
    assert client.get("/api/config").json()["audio_input"] is False
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"wordlist": "stations"},
    )
    assert res.status_code == 503
    assert "音声入力を利用できません" in res.json()["detail"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpegが必要")
@pytest.mark.parametrize(
    ("suffix", "codec", "muxer"),
    [
        (".mp3", "libmp3lame", "mp3"),
        (".m4a", "aac", "ipod"),
        (".aac", "aac", "adts"),
        (".flac", "flac", "flac"),
        (".ogg", "libvorbis", "ogg"),
        (".opus", "libopus", "opus"),
        (".webm", "libopus", "webm"),
    ],
)
def test_normalizes_supported_audio_formats(tmp_path, suffix, codec, muxer):
    raw = encoded_audio(tmp_path, suffix, codec, muxer)
    wav, seconds = audio_input_mod.normalize_compressed_audio(
        raw, f"voice{suffix}", 200 * 1024 * 1024, 420,
    )
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert 0.1 < seconds < 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpegが必要")
def test_mp3_upload_is_stored_as_normalized_wav(client, tmp_path):
    raw = encoded_audio(tmp_path, ".mp3", "libmp3lame", "mp3")
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.mp3", raw, "audio/mpeg")},
        data={"wordlist": "stations", "lyrics": "あ"},
    )
    assert res.status_code == 200, res.text
    body = wait_done(client, res.json()["id"])
    job = client.app.state.manager.jobs[body["id"]]
    stored = (job.dir / "input.wav").read_bytes()
    assert stored[:4] == b"RIFF"
    assert body["params"]["input_kind"] == "audio"


def test_rejects_compressed_audio_over_upload_limit():
    with pytest.raises(audio_input_mod.AudioInputError) as caught:
        audio_input_mod.normalize_compressed_audio(b"x" * 11, "voice.mp3", 10, 420)
    assert caught.value.status_code == 413


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpegが必要")
def test_rejects_webm_containing_video(tmp_path):
    path = tmp_path / "movie.webm"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v", "error", "-f", "lavfi", "-i", "color=black:s=16x16:d=0.2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
            "-c:v", "libvpx", "-c:a", "libopus", "-shortest", "-y", str(path),
        ],
        check=True,
    )
    with pytest.raises(audio_input_mod.AudioInputError) as caught:
        audio_input_mod.normalize_compressed_audio(
            path.read_bytes(), path.name, 200 * 1024 * 1024, 420,
        )
    assert caught.value.status_code == 400


def test_wav_capability_preserves_full_ui_editor_and_local_samples(
    tmp_path, monkeypatch
):
    """WAV対応は通常UIを簡易UIへ変えず、既存の環境限定曲も絞らない。"""
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "samples.json").write_text(
        json.dumps([{"id": "bundled", "title": "同梱曲"}]), encoding="utf-8"
    )
    (samples / "samples.local.json").write_text(
        json.dumps([{"id": "local", "title": "環境限定曲"}]), encoding="utf-8"
    )
    (samples / "local.mid").write_bytes(FAKE_MIDI)
    editor_dist = tmp_path / "editor-dist"
    editor_dist.mkdir()
    (editor_dist / "editor.html").write_text("<!doctype html>", encoding="utf-8")

    def fake_pipeline(job, config):
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.delenv(api_mod.SIMPLE_UI_ENV, raising=False)
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(samples))
    monkeypatch.setattr(api_mod, "audio_input_available", lambda: True)
    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    full_client = TestClient(
        api_mod.create_app(jobs_dir=tmp_path / "jobs", editor_dist=editor_dist)
    )

    conf = full_client.get("/api/config").json()
    assert conf["audio_input"] is True
    assert "simple_ui" not in conf
    assert conf["editor"] is True
    assert [row["id"] for row in full_client.get("/api/samples").json()] == [
        "bundled",
        "local",
    ]
    selected = full_client.post(
        "/api/jobs", data={"sample_id": "local", "wordlist": "stations"}
    )
    assert selected.status_code == 200, selected.text
    job = full_client.app.state.manager.jobs[selected.json()["id"]]
    assert job.params["sample_id"] == "local"
    assert (job.dir / "input.mid").read_bytes() == FAKE_MIDI


def test_wav_upload_limit_is_configurable(client, monkeypatch):
    monkeypatch.setenv(api_mod.MAX_AUDIO_UPLOAD_BYTES_ENV, "64")
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"wordlist": "stations"},
    )
    assert res.status_code == 413


def test_run_pipeline_dispatches_wav_to_audio_analyzer(tmp_path, monkeypatch):
    from soramimic_video import score_audio as analyze_audio_mod

    class ReachedAnalyzer(Exception):
        pass

    audio = tmp_path / "input.wav"
    audio.write_bytes(fake_wav())
    def fake_analyze(audio_path, project_dir, **kwargs):
        assert audio_path == audio
        assert project_dir == tmp_path
        assert kwargs["lyrics_path"] is None
        assert kwargs["whisper_model"] == "large-v3"
        raise ReachedAnalyzer

    monkeypatch.setattr(analyze_audio_mod, "analyze_audio", fake_analyze)
    job = api_mod.Job(
        id="wavtest",
        dir=tmp_path,
        params={"input_kind": "audio", "auto_lyrics": True},
    )
    with pytest.raises(ReachedAnalyzer):
        api_mod.run_pipeline(job, {})


@pytest.mark.parametrize("adjust", [False, True])
def test_manual_wav_lyrics_are_sent_directly_to_forced_alignment(tmp_path, monkeypatch, adjust):
    from soramimic_video import score_audio as analyze_audio_mod

    class ReachedAnalyzer(Exception):
        pass

    audio = tmp_path / "input.wav"
    audio.write_bytes(fake_wav())
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("正しい歌詞", encoding="utf-8")
    def fake_analyze(audio_path, project_dir, **kwargs):
        assert audio_path == audio
        assert project_dir == tmp_path
        assert kwargs["lyrics_path"] == lyrics
        assert kwargs["adjust_lyrics"] is adjust
        assert callable(kwargs["progress"])
        raise ReachedAnalyzer

    monkeypatch.setattr(analyze_audio_mod, "analyze_audio", fake_analyze)
    job = api_mod.Job(
        id="wav-correct-lyrics",
        dir=tmp_path,
        params={"input_kind": "audio", "auto_lyrics": False, "adjust_lyrics": adjust},
    )

    with pytest.raises(ReachedAnalyzer):
        api_mod.run_pipeline(job, {})


@pytest.mark.parametrize("enabled", [None, False, True])
def test_audio_pipeline_forwards_lyric_phrasing_to_conversion(tmp_path, monkeypatch, enabled):
    from soramimic_video import convert, score_audio
    from soramimic_video.project import Project, SongInfo

    class ReachedConversion(Exception):
        pass

    project = Project(SongInfo("", 480))
    monkeypatch.setattr(score_audio, "analyze_audio", lambda *_args, **_kwargs: project)

    def convert_spy(actual, **kwargs):
        assert actual is project
        assert kwargs["auto_phrase_lyrics"] is enabled
        raise ReachedConversion

    monkeypatch.setattr(convert, "convert_project", convert_spy)
    (tmp_path / "lyrics.txt").write_text("入力した歌詞", encoding="utf-8")
    job = api_mod.Job(id="phrase", dir=tmp_path, params={
        "input_kind": "audio", "auto_lyrics": False, "wordlist": "stations",
        **({"auto_phrase_lyrics": enabled} if enabled is not None else {}),
    })
    with pytest.raises(ReachedConversion):
        api_mod.run_pipeline(job, {})


def test_noncommercial_fanwork_is_explicit_and_persisted(client):
    ordinary = submit(client, editor=FAKE_EDITOR)
    assert wait_done(client, ordinary)["params"]["allow_noncommercial_fanwork"] is False

    opted_in = submit(
        client,
        editor=FAKE_EDITOR,
        allow_noncommercial_fanwork="true",
    )
    assert wait_done(client, opted_in)["params"]["allow_noncommercial_fanwork"] is True


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


def test_rejects_neutrino_without_neutrino_root(client, monkeypatch):
    # NEUTRINO_ROOT未設定のサーバー(公開インスタンスなど)では合成が必ず落ちるので、
    # ジョブを走らせる前に422で断る
    monkeypatch.delenv("NEUTRINO_ROOT", raising=False)
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    res = client.post(
        "/api/jobs",
        files=files,
        data={"wordlist": "stations", "synthesizer": "neutrino"},
    )
    assert res.status_code == 422
    assert "NEUTRINO" in res.json()["detail"]
    # VOICEVOX指定なら通る(こちらは別プロセスのエンジンを使うので設定に依存しない)
    assert wait_done(client, submit(client, wordlist="stations",
                                    synthesizer="voicevox"))["status"] == "done"


def test_synthesizer_defaults_to_voicevox(client, monkeypatch):
    # 省略時はNEUTRINO_ROOT未設定のサーバーでも通る値(voicevox)になる
    monkeypatch.delenv("NEUTRINO_ROOT", raising=False)
    body = wait_done(client, submit(client, wordlist="stations"))
    assert body["status"] == "done"
    assert body["params"]["synthesizer"] == "voicevox"


def test_accepts_voicevox_params(client):
    job_id = submit(
        client, wordlist="stations", synthesizer="voicevox", voicevox_style="3001"
    )
    body = wait_done(client, job_id)
    assert body["params"]["synthesizer"] == "voicevox"
    assert body["params"]["voicevox_style"] == 3001


@pytest.mark.parametrize("synthesizer", ["prettypitch", "hybrid"])
def test_accepts_prettypitch_only_for_noncommercial_fanwork(client, monkeypatch, synthesizer):
    from soramimic_video import prettypitch as pp_mod

    monkeypatch.setattr(pp_mod, "installation_error", lambda *args, **kwargs: None)
    rejected = client.post(
        "/api/jobs",
        files={"midi": ("song.mid", FAKE_MIDI, "audio/midi")},
        data={"wordlist": "stations", "synthesizer": synthesizer},
    )
    assert rejected.status_code == 422
    assert "非商用" in rejected.json()["detail"]

    job_id = submit(
        client,
        wordlist="stations",
        synthesizer=synthesizer,
        allow_noncommercial_fanwork="true",
    )
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["synthesizer"] == synthesizer
    if synthesizer == "hybrid":
        assert body["params"]["voicevox_style"] == 6000


def test_auto_octave_defaults_on(client):
    job_id = submit(client, wordlist="stations")
    body = wait_done(client, job_id)
    assert body["params"]["auto_octave"] is True


def test_auto_octave_new_flag(client):
    job_id = submit(client, wordlist="stations", auto_octave="false")
    body = wait_done(client, job_id)
    assert body["params"]["auto_octave"] is False


def test_auto_octave_legacy_flag_name_backward_compat(client):
    # 旧名 voicevox_auto_octave も引き続き受け付ける(deprecated)
    job_id = submit(client, wordlist="stations", voicevox_auto_octave="false")
    body = wait_done(client, job_id)
    assert body["params"]["auto_octave"] is False


def test_auto_octave_new_name_takes_priority(client):
    # 新旧両方指定なら新名(auto_octave)を優先する
    job_id = submit(
        client, wordlist="stations", auto_octave="true", voicevox_auto_octave="false"
    )
    body = wait_done(client, job_id)
    assert body["params"]["auto_octave"] is True


def test_accepts_convert_params(client):
    job_id = submit(client, wordlist="stations", convert_params="DUPLICATE=true")
    body = wait_done(client, job_id)
    assert body["params"]["convert_params"] == "DUPLICATE=true"


def test_convert_params_default_empty(client):
    job_id = submit(client, wordlist="stations")
    body = wait_done(client, job_id)
    assert body["params"]["convert_params"] == ""


def test_index_html_hides_preview_for_sensitive_wordlists():
    """ビルダーカードのサムネプレビューで、昆虫などの画像を初期非表示にする。

    黙って出さないのではなく「隠している理由」と「画像を表示する」ボタンを出す。
    対象はこのプレビューだけで、動画・サムネの画像は従来どおり。
    """
    html = _index_html()
    # 対象リストは1か所の定数で複数指定できる(将来クモ等を足せるように)
    assert "const HIDDEN_PREVIEW_WORDLISTS = {" in html
    assert "insect:" in html
    # 隠していることが分かる説明と、その場で表示できるボタンがある
    assert (
        '<div class="builder-figure hidden-preview" id="builder-image-hidden" hidden>'
        in html
    )
    assert '<p class="hint" id="builder-image-hidden-note"></p>' in html
    assert '<button type="button" id="builder-show-image">画像を表示する</button>' in html
    # 表示ボタンを押したときだけ画像入りで作り直す(組み合わせを変えるとまた隠れる)
    assert "previewShowImages = true;" in html
    assert "schedulePreview(true);" in html


def test_index_html_polling_survives_background():
    """iOSでアプリを切り替えて戻ったとき、進捗ポーリングが再開すること(#139)。

    バックグラウンド中はタイマーが凍り、fetchも失敗する。以前は失敗すると
    setTimeoutのチェーンが張り直されず、戻っても進捗が止まったままだった。
    """
    html = _index_html()
    # 可視に戻ったら即座に取り直す(bfcache復帰・回線復帰も同じ入口)
    assert 'document.addEventListener("visibilitychange", resumePolling);' in html
    assert 'window.addEventListener("pageshow", resumePolling);' in html
    assert 'window.addEventListener("online", resumePolling);' in html
    assert "function resumePolling()" in html
    assert 'if (document.visibilityState === "hidden") return;' in html
    assert "restartPolling(currentJob);" in html
    # 復帰直後の死んだ接続にfetchが刺さったままにならないよう、1回ごとに
    # タイムアウトで見切る(2026-08-05: 復帰後もリクエストが届かないまま
    # 「再接続しています…」が続いた実測への対処)
    assert "const POLL_FETCH_TIMEOUT_MS" in html
    assert "setTimeout(() => ctrl.abort(), POLL_FETCH_TIMEOUT_MS);" in html
    assert "signal: ctrl.signal" in html
    assert "clearTimeout(killTimer);" in html
    # 一時的な失敗ではチェーンを殺さず、間隔を伸ばして取り直す
    assert "function pollFailed(id, seq, detail)" in html
    assert "pollFailed(id, seq, (e && e.message) || \"通信エラー\");" in html
    assert "const wait = Math.min(POLL_INTERVAL_MS * pollFailures, POLL_RETRY_MAX_MS);" in html
    assert "schedulePoll(id, seq, wait);" in html
    # 恒久エラー(ジョブが無い・権限が無い)だけは諦めて操作を返す
    assert "const POLL_FATAL_STATUS = [401, 403, 404, 410];" in html
    assert "if (POLL_FATAL_STATUS.includes(res.status))" in html
    # 二重にチェーンが走らないよう世代番号で古い方を捨てる
    assert "if (seq !== pollSeq || id !== currentJob) return;" in html
    assert "function stopPolling()" in html
    assert "pollSeq++;" in html
    # 完了・失敗したらタイマーを確実に止める
    assert "function finish() {\n  stopPolling();" in html
    # 進捗の待ち時間は素のsetTimeoutではなくschedulePoll経由(取り消せるように)
    assert "setTimeout(() => poll(id), 2000)" not in html
    assert "schedulePoll(id, seq, POLL_INTERVAL_MS);" in html


def test_running_job_reports_stage_elapsed(tmp_path):
    """ジョブの状態は実行中ステージの経過秒を持つ(クライアント表示の基準)。

    画面はこの値をそのまま出すので、バックグラウンドから戻って1回取り直せば
    経過秒は自動的に正しい値になる。
    """
    job = api_mod.Job(id="elapsed-test", dir=tmp_path, params={})
    job.status = "running"
    job.stage = "video"
    job.stage_started_at = time.time() - 12.0
    d = job.to_dict()
    assert d["stage"] == "video"
    assert 11.0 <= d["stage_elapsed"] <= 20.0


def test_config_has_voicevox_key(client):
    body = client.get("/api/config").json()
    assert "voicevox" in body  # 起動していればstyles、いなければNone
    assert "prettypitch" in body
    assert body["fixed_synthesizer"] == "voicevox"


@pytest.mark.parametrize("synthesizer", ["prettypitch", "hybrid"])
def test_config_can_fix_web_synthesis_to_prettypitch(tmp_path, monkeypatch, synthesizer):
    from soramimic_video import prettypitch as pp_mod

    monkeypatch.setenv(api_mod.FIXED_SYNTHESIZER_ENV, synthesizer)
    monkeypatch.setattr(pp_mod, "installation_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(pp_mod, "available", lambda: True)
    browser = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    body = browser.get("/api/config").json()
    assert body["fixed_synthesizer"] == synthesizer
    assert body["prettypitch"] == {"speaker": "波音リツ", "experimental": True}


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
    assert video.headers["content-disposition"].startswith("attachment;")
    playback = client.get(body["playback_url"])
    assert playback.headers["content-type"] == "audio/wav"
    assert playback.headers["content-disposition"].startswith("inline;")


def test_preview_mode_is_validated_and_stored(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_pipeline(job, config):
        seen[job.id] = job.params["preview_mode"]
        out = job.dir / "neutrino" / "vocal.wav"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"RIFF-fake")
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    for sent, stored in [("high", "high"), ("low", "low"), ("head", "head"), ("x", "")]:
        res = client.post(
            "/api/jobs", files=files, data={"preview": "20", "preview_mode": sent}
        )
        assert res.status_code == 200
        wait_done(client, res.json()["id"])
        assert seen[res.json()["id"]] == stored


def test_preview_uses_the_whole_song_for_auto_octave(tmp_path):
    """切り出したぶんだけで音域を決めると本番と違うキーになるので全曲を渡す。"""
    captured: dict = {}

    def fake_synthesize(project, project_dir, **kw):
        captured["octave_keys"] = kw["octave_keys"]
        captured["notes"] = [n.midi_note for n in project.notes]
        out = project_dir / "neutrino" / "vocal.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF-fake")
        return out

    project = _range_project()
    job = api_mod.Job(id="j", dir=tmp_path, params={"model": "MERROW"})
    api_mod._run_synthesize(
        job, {}, project, fake_synthesize, octave_keys=[60, 74, 50]
    )
    assert captured["octave_keys"] == [60, 74, 50]
    # 本番(プレビューでない)経路は渡さない = synthesize側でprojectから求める
    api_mod._run_synthesize(job, {}, project, fake_synthesize)
    assert captured["octave_keys"] is None


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


def _range_project():
    """3行の小さなproject。行1が最高音、行2が最低音を含む。"""
    from types import SimpleNamespace

    keys = [60, 62, 74, 70, 50, 55]
    notes = [
        SimpleNamespace(id=i, midi_note=k, start_sec=float(i), end_sec=i + 0.9, kana="ア")
        for i, k in enumerate(keys)
    ]
    lines = [
        SimpleNamespace(id=0, note_ids=[0, 1]),
        SimpleNamespace(id=1, note_ids=[2, 3]),
        SimpleNamespace(id=2, note_ids=[4, 5]),
    ]
    return SimpleNamespace(
        notes=notes,
        lines=lines,
        line_time_range=lambda ln: (notes[ln.note_ids[0]].start_sec,
                                    notes[ln.note_ids[-1]].end_sec),
    )


def test_extreme_line_picks_the_highest_and_lowest_phrase():
    project = _range_project()
    assert api_mod._extreme_line(project, "high").id == 1
    assert api_mod._extreme_line(project, "low").id == 2
    # 音符のある行が無ければNone(head相当にフォールバックする)
    from types import SimpleNamespace

    assert api_mod._extreme_line(SimpleNamespace(lines=[]), "high") is None


def test_preview_window_covers_the_whole_phrase():
    project = _range_project()
    # high: 行1(2.0〜3.9秒)。窓は行末の音符が残り、次の行は入らない長さ
    start, seconds = api_mod._preview_window(project, "high", 20.0)
    assert start == 2.0
    api_mod._truncate_project(project, seconds, start=start)
    assert [n.id for n in project.notes] == [2, 3]

    # モード無し(head)は従来どおり歌い出しからpreview秒
    assert api_mod._preview_window(_range_project(), "", 20.0) == (0.0, 20.0)


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


def test_to_dict_hides_unreliable_audio_analysis_eta():
    job = _running_synth_job(
        stage="analyze",
        params={"input_kind": "audio"},
        stage_started_at=time.time() - 10,
        stage_progress=25,
    )
    d = job.to_dict(with_log=False)
    assert d["stage_progress"] == 25
    assert d["stage_elapsed"] >= 9
    assert "stage_eta_seconds" not in d


def test_done_job_exposes_generation_quality_warning(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    job = api_mod.Job(
        id="difficult-song",
        dir=tmp_path,
        params={"input_kind": "audio"},
        status="done",
        video=video,
        generation_quality_warning=True,
    )

    assert job.to_dict(with_log=False)["generation_quality_warning"] is True


def test_queued_job_reports_input_aware_start_wait(tmp_path):
    """前方のWAVはXF MIDIより長い幅で足し、他人の詳細は返さない。"""
    now = time.time()
    running_audio = api_mod.Job(
        id="audio",
        dir=tmp_path / "audio",
        params={"input_kind": "audio"},
        status="running",
        created_at=now - 60,
        started_at=now - 30,
    )
    queued_midi = api_mod.Job(
        id="midi",
        dir=tmp_path / "midi",
        params={"input_kind": "midi"},
        status="queued",
        created_at=now - 10,
    )
    manager = object.__new__(api_mod.JobManager)
    manager.jobs = {running_audio.id: running_audio, queued_midi.id: queued_midi}
    manager._lock = threading.Lock()

    body = manager.job_dict(queued_midi, with_log=False)

    assert body["queue_ahead"] == 1
    assert 89 <= body["queue_wait_min_seconds"] <= 91
    assert 269 <= body["queue_wait_max_seconds"] <= 271
    assert 9 <= body["queued_elapsed_seconds"] <= 11
    assert "audio" not in body


def test_queued_wait_estimate_distinguishes_midi_from_audio(tmp_path):
    now = time.time()
    first_midi = api_mod.Job(
        id="first-midi", dir=tmp_path / "first-midi",
        params={"input_kind": "midi"}, status="running",
        created_at=now - 20, started_at=now - 10,
    )
    second_audio = api_mod.Job(
        id="second-audio", dir=tmp_path / "second-audio",
        params={"input_kind": "audio"}, status="queued", created_at=now - 5,
    )
    last = api_mod.Job(
        id="last", dir=tmp_path / "last",
        params={"input_kind": "midi"}, status="queued", created_at=now,
    )
    manager = object.__new__(api_mod.JobManager)
    manager.jobs = {job.id: job for job in (first_midi, second_audio, last)}
    manager._lock = threading.Lock()

    body = manager.job_dict(last, with_log=False)

    assert body["queue_ahead"] == 2
    # 実行中MIDIの残り35〜170秒 + 待機WAVの120〜300秒。
    assert 154 <= body["queue_wait_min_seconds"] <= 156
    assert 469 <= body["queue_wait_max_seconds"] <= 471


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
    running = submit(client, editor=FAKE_EDITOR)
    queued = submit(client, editor=FAKE_EDITOR)
    time.sleep(0.1)  # 1件目が実行中になるのを待つ

    # 順番待ちのジョブは即座にcanceledになり、実行されない
    res = client.post(f"/api/jobs/{queued}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "canceled"
    queued_dir = client.app.state.manager.jobs[queued].dir
    assert [path.name for path in queued_dir.iterdir()] == [api_mod.STATUS_FILENAME]

    # 実行中のジョブは中断チェックで止まる
    client.post(f"/api/jobs/{running}/cancel")
    body = wait_done(client, running)
    assert body["status"] == "canceled"
    running_dir = client.app.state.manager.jobs[running].dir
    assert [path.name for path in running_dir.iterdir()] == [api_mod.STATUS_FILENAME]
    assert client.get(f"/api/jobs/{queued}").json()["status"] == "canceled"
    # 完了済みジョブへのcancelは何もしない
    assert client.post(f"/api/jobs/{running}/cancel").json()["status"] == "canceled"


def test_failed_job_removes_uploaded_and_intermediate_files(tmp_path, monkeypatch):
    def failing_pipeline(job, config):
        (job.dir / "analyze").mkdir()
        (job.dir / "analyze" / "private.tmp").write_bytes(b"intermediate")
        raise RuntimeError("analysis failed")

    monkeypatch.setattr(api_mod, "run_pipeline", failing_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    job_id = submit(client, editor=FAKE_EDITOR)
    body = wait_done(client, job_id)
    assert body["status"] == "error"
    job_dir = client.app.state.manager.jobs[job_id].dir
    assert [path.name for path in job_dir.iterdir()] == [api_mod.STATUS_FILENAME]


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


def test_runproc_kill_current_stops_parallel_processes():
    import threading
    import time as _time

    from soramimic_video import runproc

    results = []

    def target():
        results.append(runproc.run(["sleep", "5"], capture_output=True))

    threads = [threading.Thread(target=target) for _ in range(2)]
    for thread in threads:
        thread.start()
    _time.sleep(0.2)
    assert runproc.kill_current()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len(results) == 2
    assert all(proc.returncode != 0 for proc in results)


@pytest.mark.parametrize("backend", ["neutrino", "voicevox"])
def test_run_pipeline_builds_silent_video_in_parallel(tmp_path, monkeypatch, backend):
    import threading

    from soramimic_video import editor_io, xfparse
    from soramimic_video import mix as mix_mod
    from soramimic_video import video as video_mod
    from soramimic_video.project import Note, Project, SongInfo

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.mid").write_bytes(FAKE_MIDI)
    (job_dir / "editor.json").write_text("{}", encoding="utf-8")
    project = Project(
        song=SongInfo(midi_path=str(job_dir / "input.mid"), ticks_per_beat=480),
        notes=[Note(0, 60, 0, 480, 0.0, 1.0, 0, "ラ", "ラ", "ラ")],
    )
    job = api_mod.Job(
        id="parallel", dir=job_dir,
        params={"model": "MERROW", "synthesizer": backend, "wordlist": "stations",
                "song_credit": "オトノケ - Creepy Nuts", "thumbnail_wordlist_label": "好きな駅",
                "title_words": [{"id": "1", "surface": "駅名"}]},
    )
    audio_started = threading.Event()
    visual_started = threading.Event()

    monkeypatch.setattr(xfparse, "analyze_midi", lambda path: project)
    monkeypatch.setattr(editor_io, "import_editor", lambda *a, **k: None)

    def fake_synthesize(job, config, project, synthesize, octave_keys=None):
        with api_mod._stage(job, "synthesize"):
            audio_started.set()
            assert visual_started.wait(1)
            from soramimic_video.musicxml import build_musicxml
            # Only the actual NEUTRINO backend may export this transposed score.
            score = job.dir / "neutrino/score.musicxml"
            score.parent.mkdir(exist_ok=True)
            score.write_text(build_musicxml(project, {0: "ラ"}, transpose=-12))
            path = job.dir / "generated-vocal.wav"
            path.write_bytes(fake_wav())
            return path

    def fake_prepare(*args, **kwargs):
        assert kwargs["song_credit"] == "オトノケ - Creepy Nuts"
        assert kwargs["thumbnail_wordlist_label"] == "好きな駅"
        assert kwargs["title_words"] == [{"id": "1", "surface": "駅名"}]
        visual_started.set()
        assert audio_started.wait(1)
        return type("Prepared", (), {"audio_delay_sec": 1.0})()

    def fake_encode(prepared):
        path = job_dir / "video" / "video-only.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"silent")
        return path

    def fake_mix(*args, **kwargs):
        path = job_dir / "mix" / "song.wav"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"wav")
        return path

    def fake_attach(silent, audio, total, out=None, audio_delay_sec=0.0):
        assert silent.read_bytes() == b"silent"
        assert audio.read_bytes() == b"wav"
        assert total == 10.0
        assert audio_delay_sec == 1.0
        out.write_bytes(b"mp4")
        return out

    monkeypatch.setattr(api_mod, "_run_synthesize", fake_synthesize)
    monkeypatch.setattr(mix_mod, "mix", fake_mix)
    monkeypatch.setattr(
        video_mod, "planned_video_total_sec", lambda project, *args: 10.0
    )
    monkeypatch.setattr(
        video_mod, "actual_video_total_sec", lambda project, audio, *args: 9.0
    )
    monkeypatch.setattr(video_mod, "prepare_video", fake_prepare)
    monkeypatch.setattr(video_mod, "encode_silent_video", fake_encode)
    monkeypatch.setattr(video_mod, "attach_audio", fake_attach)
    monkeypatch.setattr(
        video_mod, "make_video",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("serial fallback")),
    )

    out = api_mod.run_pipeline(job, {"parallel_video": True, "video_fps": 30})
    assert out.read_bytes() == b"mp4"
    assert (job_dir / "exports/vocal.wav").read_bytes() == fake_wav()
    xml = (job_dir / "exports/score.musicxml").read_text()
    if backend == "neutrino":
        assert xml == (job_dir / "neutrino/score.musicxml").read_text()
    else:
        assert "<octave>4</octave>" in xml
    assert (job_dir / "exports/lyrics.mid").is_file()
    assert not (job_dir / "video" / "video-only.mp4").exists()
    assert [stage["name"] for stage in job.stages] == [
        "analyze", "import-editor", "synthesize", "mix", "video",
    ]


def test_run_pipeline_cleans_silent_video_when_audio_fails(tmp_path, monkeypatch):
    import threading

    from soramimic_video import (
        editor_io,
        xfparse,
    )
    from soramimic_video import (
        mix as mix_mod,
    )
    from soramimic_video import (
        video as video_mod,
    )
    from soramimic_video.project import Note, Project, SongInfo

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.mid").write_bytes(FAKE_MIDI)
    (job_dir / "editor.json").write_text("{}", encoding="utf-8")
    project = Project(
        song=SongInfo(midi_path=str(job_dir / "input.mid"), ticks_per_beat=480),
        notes=[Note(0, 60, 0, 480, 0.0, 1.0, 0, "ラ", "ラ", "ラ")],
    )
    job = api_mod.Job(
        id="parallel-fail", dir=job_dir,
        params={"model": "MERROW", "synthesizer": "neutrino", "wordlist": "stations"},
    )
    encoded = threading.Event()

    monkeypatch.setattr(xfparse, "analyze_midi", lambda path: project)
    monkeypatch.setattr(editor_io, "import_editor", lambda *a, **k: None)
    monkeypatch.setattr(api_mod, "_run_synthesize", lambda *a, **k: None)
    monkeypatch.setattr(
        video_mod, "planned_video_total_sec", lambda project, *args: 10.0
    )
    monkeypatch.setattr(
        video_mod,
        "prepare_video",
        lambda *a, **k: type("Prepared", (), {"audio_delay_sec": 0.0})(),
    )

    def fake_encode(prepared):
        path = job_dir / "video" / "video-only.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"silent")
        encoded.set()
        return path

    def failing_mix(*args, **kwargs):
        assert encoded.wait(1)
        raise RuntimeError("mix failed")

    monkeypatch.setattr(video_mod, "encode_silent_video", fake_encode)
    monkeypatch.setattr(mix_mod, "mix", failing_mix)

    with pytest.raises(RuntimeError, match="mix failed"):
        api_mod.run_pipeline(job, {"parallel_video": True, "video_fps": 30})
    assert not (job_dir / "video" / "video-only.mp4").exists()


def test_rejects_non_midi(client):
    res = client.post("/api/jobs", files={"midi": ("x.mid", b"not midi", "audio/midi")})
    assert res.status_code == 400


def test_config_lists_layouts(client):
    conf = client.get("/api/config").json()
    assert "default" in conf["layouts"] and "caption" in conf["layouts"]


def test_config_lists_only_active_analysis_capability(client, monkeypatch):
    from soramimic_video import audio_melody

    monkeypatch.setattr(
        audio_melody,
        "configured_capabilities",
        lambda: {"sheetsage2": True},
    )

    assert client.get("/api/config").json()["audio_analysis"] == {"sheetsage2": True}


def test_config_lists_wordlist_phrases_for_text_previews(client):
    phrases = client.get("/api/config").json()["wordlist_phrases"]
    assert phrases["stations"] == "駅名"
    assert phrases["baseball"] == "野球選手名"


def test_config_has_wordlist_layouts(client):
    conf = client.get("/api/config").json()
    wl = conf["wordlist_layouts"]
    assert wl["scientist"] == "scientist_card"
    # 値はすべて組み込みレイアウト名(UIがそのまま#layoutに入れるため)
    assert set(wl.values()) <= set(conf["layouts"])


def test_config_has_vtuber_image_policy(client):
    conf = client.get("/api/config").json()
    assert "youtuber" not in conf["wordlist_image_policies"]
    policy = conf["wordlist_image_policies"]["vtuber"]
    assert policy["usage"] == "noncommercial_fanwork"
    assert policy["terms"] == "https://hololivepro.com/terms/"
    by_url = {term["url"]: term for term in policy["terms_pages"]}
    assert len(by_url) == len(policy["terms_pages"])
    assert by_url["https://hololivepro.com/terms/"]["people"]
    fsp = by_url["https://firststage-pro.com/guideline/"]
    assert "FIRST STAGE PRODUCTION" in fsp["label"]
    assert len(fsp["people"]) == 27



@pytest.mark.parametrize(("name", "label"), [("youtuber", "YouTuber"), ("vtuber", "VTuber")])
def test_creator_wordlists_are_separate_in_ui_and_editor(client, name, label):
    from soramimic_video.editor_io import (
        SETTING_JSON,
        _flatten_wordlists,
        named_wordlist_entry,
        wordlist_phrase_name,
    )

    if not SETTING_JSON.is_file():
        pytest.skip("submodule未取得")
    config = client.get("/api/config").json()
    assert config["wordlist_layouts"][name] == "youtuber_card"
    assert wordlist_phrase_name(name) == f"{label}名"

    response = client.get("/editor/conf/setting.json")
    assert response.status_code == 200
    entries = _flatten_wordlists(response.json()["wordlist"])
    entry = next(item for item in entries if item.get("value") == name.upper())
    assert entry == named_wordlist_entry(name)
    assert entry["text"] == label
    assert {facet["column"] for facet in entry["facets"]} == {"type", "status"}

    response = client.get(f"/editor/{entry['filepath']}")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows
    assert {row["category"] for row in rows} == {name}


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


def test_editor_job_records_wordlist(client):
    # editor JSON側の単語リスト指定がフォーム選択より優先されて履歴(params)に残る
    from soramimic_video.convert import resolve_wordlist

    try:
        resolve_wordlist("stations")
    except FileNotFoundError:
        pytest.skip("external/soramimic-wordlists のsubmoduleが無い環境")
    editor = json.dumps({**json.loads(FAKE_EDITOR),
                         "wordlist": {"filepath": "wordlists/stations.csv"}}).encode()
    job_id = submit(client, editor=editor, wordlist="pokemon")
    body = wait_done(client, job_id)
    assert body["params"]["wordlist"] == "stations"


def test_download_filename_includes_song_and_wordlist(client):
    job_id = submit(client, wordlist="stations")
    body = wait_done(client, job_id)
    res = client.get(body["video_url"])
    assert f"soramimic_song_stations_{job_id}.mp4" in res.headers["content-disposition"]


def test_download_filename_sanitizes():
    job = api_mod.Job(
        id="abc", dir=Path("/tmp"),
        params={"midi_filename": "ふる/さと.mid", "wordlist": "pokemon"},
    )
    job.video = Path("out.mp4")
    assert api_mod._download_filename(job) == "soramimic_ふる_さと_pokemon_abc.mp4"
    job.video = Path("out.wav")  # プレビューは単語リストを使わない
    assert api_mod._download_filename(job) == "preview_ふる_さと_abc.wav"
    job.params = {}
    job.video = Path("out.mp4")
    assert api_mod._download_filename(job) == "soramimic_abc.mp4"


def test_video_not_ready(client, monkeypatch):
    # 実行前に取りに来たら409
    slow = api_mod.run_pipeline

    def slow_pipeline(job, config):
        time.sleep(0.3)
        return slow(job, config)

    monkeypatch.setattr(api_mod, "run_pipeline", slow_pipeline)
    job_id = submit(client, editor=FAKE_EDITOR)
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

    files["editor"] = ("editor.json", FAKE_EDITOR, "application/json")
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
    job_id = submit(client, editor=FAKE_EDITOR)
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


# ---- サムネ画像 ----

FAKE_PNG = b"\x89PNG\r\n\x1a\n-fake"


@pytest.fixture
def thumb_client(tmp_path, monkeypatch):
    """パイプラインが動画とサムネ(thumbnail.png)を両方作るクライアント。"""

    def fake_pipeline(job, config):
        out = job.dir / "video" / "song.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_MP4)
        (job.dir / "thumbnail.png").write_bytes(FAKE_PNG)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def test_thumbnail_download(thumb_client):
    job_id = submit(thumb_client, wordlist="stations")
    body = wait_done(thumb_client, job_id)
    assert body["thumbnail_url"] == f"/api/jobs/{job_id}/thumbnail"
    res = thumb_client.get(body["thumbnail_url"])
    assert res.status_code == 200
    assert res.content == FAKE_PNG
    assert res.headers["content-type"] == "image/png"
    assert f"{job_id}.png" in res.headers["content-disposition"]


def test_thumbnail_missing_is_404(client):
    # 既定のfake_pipelineはサムネを作らない(旧ジョブ・生成失敗と同じ状態)
    job_id = submit(client, wordlist="stations")
    body = wait_done(client, job_id)
    assert "thumbnail_url" not in body
    assert client.get(f"/api/jobs/{job_id}/thumbnail").status_code == 404


def test_thumbnail_unknown_job_is_404(client):
    assert client.get("/api/jobs/nosuchjob/thumbnail").status_code == 404


def test_song_title_is_stored(client):
    # UIはサンプル曲なら samples.json の曲名、自分のMIDIならファイル名を送る
    job_id = submit(
        client, wordlist="stations", song_title=" うっせぇわ(確認用) ",
        original_credit=" 作詞: ○○ ", credit_notice=" © 2026 権利者 ",
    )
    body = wait_done(client, job_id)
    assert body["params"]["song_title"] == "うっせぇわ(確認用)"
    assert body["params"]["song_label"] == "アップロードした曲"
    assert body["song_label"] == "アップロードした曲"
    assert body["params"]["original_credit"] == "作詞: ○○"
    assert body["params"]["credit_notice"] == "© 2026 権利者"


def test_history_display_labels_are_saved_and_listed(
    client, tmp_path, monkeypatch
):
    from soramimic_video import editor_io

    setting = tmp_path / "setting.json"
    setting.write_text(
        json.dumps({
            "wordlist": [{
                "value": "STATIONS", "text": "駅名",
                "filepath": "wordlists/stations.csv",
            }]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(editor_io, "SETTING_JSON", setting)
    files = {"midi": ("furusato.mid", FAKE_MIDI, "audio/midi")}
    response = client.post(
        "/api/jobs", files=files,
        data={"wordlist": "stations", "song_title": "ふるさと"},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    body = wait_done(client, job_id)
    assert body["song_label"] == "ふるさと"
    assert body["wordlist_label"] == "駅名"
    assert body["params"]["song_label"] == "ふるさと"
    assert body["params"]["wordlist_label"] == "駅名"
    listed = client.get("/api/jobs").json()[0]
    assert listed["song_label"] == "ふるさと"
    assert listed["wordlist_label"] == "駅名"
    status = json.loads(
        (client.app.state.manager.jobs[job_id].dir / api_mod.STATUS_FILENAME)
        .read_text(encoding="utf-8")
    )
    assert status["params"]["song_label"] == "ふるさと"
    assert status["params"]["wordlist_label"] == "駅名"


def test_old_job_display_labels_use_safe_fallbacks(tmp_path, monkeypatch):
    from soramimic_video import editor_io

    setting = tmp_path / "setting.json"
    setting.write_text(
        json.dumps({
            "wordlist": [{
                "value": "STATIONS", "text": "駅名",
                "filepath": "wordlists/stations.csv",
            }]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(editor_io, "SETTING_JSON", setting)
    known = api_mod.Job(
        id="old-known", dir=tmp_path,
        params={"midi_filename": "furusato.mid", "wordlist": "stations"},
    ).to_dict(with_log=False)
    assert known["song_label"] == "ふるさと"
    assert known["wordlist_label"] == "駅名"

    unknown = api_mod.Job(
        id="old-unknown", dir=tmp_path,
        params={
            "midi_filename": "個人名_発表会.mid",
            "song_title": "個人名_発表会",
            "wordlist": "private_raw_filename",
            "where": "secret = true",
        },
    ).to_dict(with_log=False)
    assert unknown["song_label"] == "曲"
    assert unknown["wordlist_label"] == "単語リスト"
    assert "個人名" not in unknown["song_label"]
    assert "private_raw_filename" not in unknown["wordlist_label"]


def test_preview_and_custom_wordlist_have_safe_display_labels():
    assert api_mod.job_wordlist_label({"preview": 20}) == "歌声プレビュー"
    assert api_mod.job_wordlist_label({
        "wordlist": "someone_private_list", "wordlist_csv": "someone_private_list.csv"
    }) == "自作リスト"


def test_song_title_falls_back_to_midi_filename():
    # 曲名の指定があればそれ、無ければアップロード時のファイル名
    assert api_mod.song_title_of(
        {"song_title": "うっせぇわ", "midi_filename": "ussewa.mid"}
    ) == "うっせぇわ"
    assert api_mod.song_title_of(
        {"song_title": "", "midi_filename": "ussewa.mid"}
    ) == "ussewa.mid"
    assert api_mod.song_title_of({}) == ""


def test_song_title_kana_of_resolves_sample_reading(tmp_path, monkeypatch):
    # サンプル曲のジョブは midi_filename が <サンプルID>.mid なので、
    # サーバー側で samples.json を引いて読み(title_kana)を解決できる
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps([{"id": "momiji", "title": "紅葉", "title_kana": "モミジ"}]),
        encoding="utf-8",
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))

    assert api_mod.song_title_kana_of(
        {"song_title": "紅葉", "midi_filename": "momiji.mid"}
    ) == "モミジ"
    # 曲名の指定が無くてもファイル名だけで引ける
    assert api_mod.song_title_kana_of({"midi_filename": "momiji.mid"}) == "モミジ"
    # 自分のMIDIは読みが分からない(従来どおり曲名から推定させる)
    assert api_mod.song_title_kana_of(
        {"song_title": "うっせぇわ", "midi_filename": "ussewa.mid"}
    ) == ""
    assert api_mod.song_title_kana_of({}) == ""
    # サンプルと同じファイル名でも曲名が違えば自分のMIDI。読みは使わない
    assert api_mod.song_title_kana_of(
        {"song_title": "紅葉(自作)", "midi_filename": "momiji.mid"}
    ) == ""


def test_song_title_kana_of_without_reading_in_manifest(tmp_path, monkeypatch):
    # title_kana の無い(古い・差し替えの)samples.json でも落ちない
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps([{"id": "momiji", "title": "紅葉"}]), encoding="utf-8"
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    assert api_mod.song_title_kana_of({"midi_filename": "momiji.mid"}) == ""


def test_sample_credits_are_resolved_from_manifest(tmp_path, monkeypatch):
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps(
            [
                {
                    "id": "licensed",
                    "title": "権利曲",
                    "title_kana": "ケンリキョク",
                    "original_display_credit": "作者",
                    "original_credit": "作詞・作曲: 作者",
                    "credit_notice": "指定表記",
                    "midi_end_credit": "MIDI: 制作者",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    params = {
        "midi_filename": "licensed.mid",
        "song_title": "権利曲",
        "original_credit": "",
        "credit_notice": "",
    }

    assert api_mod.original_credit_of(params) == "作詞・作曲: 作者"
    assert api_mod.original_display_credit_of(params) == "作者"
    assert api_mod.credit_notice_of(params) == "指定表記"
    assert api_mod.midi_end_credit_of(params) == "MIDI: 制作者"
    assert api_mod.midi_end_credit_of(
        {"midi_filename": "uploaded.mid", "midi_end_credit": "偽の指定"}
    ) == ""


def test_sample_midi_credit_uses_server_snapshot_after_manifest_changes(
    tmp_path, monkeypatch
):
    d = tmp_path / "samples"
    d.mkdir()
    manifest = d / "samples.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "id": "licensed",
                    "title": "権利曲",
                    "midi_end_credit": "MIDI: 制作者",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    params = {"sample_id": "licensed"}
    params["sample_midi_end_credit"] = api_mod.midi_end_credit_of(params)

    # 受付後にmanifestが更新されても、処理中ジョブの必須表記は変わらない。
    manifest.write_text("[]", encoding="utf-8")
    assert api_mod.midi_end_credit_of(params) == "MIDI: 制作者"
    # 持ち込みMIDIが同名キーを紛れ込ませても帰属表記としては採用しない。
    assert api_mod.midi_end_credit_of(
        {"midi_filename": "uploaded.mid", "sample_midi_end_credit": "偽の指定"}
    ) == ""


def test_sample_manifest_credits_cannot_be_omitted_or_overridden(tmp_path, monkeypatch):
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps(
            [
                {
                    "id": "licensed",
                    "title": "権利曲",
                    "original_credit": "正式なクレジット",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))

    assert api_mod.original_credit_of(
        {
            "midi_filename": "licensed.mid",
            "song_title": "権利曲",
            "original_credit": "誤ったクレジット",
        }
    ) == "正式なクレジット"


def test_uploaded_song_keeps_explicit_credits_when_filename_matches_sample(
    tmp_path, monkeypatch
):
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps(
            [{"id": "licensed", "title": "権利曲", "original_credit": "サンプル作者"}]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    params = {
        "midi_filename": "licensed.mid",
        "song_title": "自作曲",
        "original_credit": "自作曲の作者",
        "credit_notice": "自作曲の指定表記",
    }

    assert api_mod.original_credit_of(params) == "自作曲の作者"
    assert api_mod.original_display_credit_of(params) == ""
    assert api_mod.credit_notice_of(params) == "自作曲の指定表記"


def test_load_samples_tolerates_missing_or_broken_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(tmp_path / "nope"))
    assert api_mod.load_samples() == []
    assert api_mod.sample_entry("momiji") is None
    d = tmp_path / "broken"
    d.mkdir()
    (d / "samples.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    assert api_mod.load_samples() == []


def test_load_samples_adds_local_overlay_manifest(tmp_path, monkeypatch):
    d = tmp_path / "samples"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps([{"id": "pd", "title": "公開曲"}]), encoding="utf-8"
    )
    (d / "samples.local.json").write_text(
        json.dumps([{"id": "local", "title": "ローカル曲"}]), encoding="utf-8"
    )
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))

    assert [entry["id"] for entry in api_mod.load_samples()] == ["pd", "local"]


def test_bundled_samples_all_have_a_reading():
    # サムネの曲名変換は読みを使う。同梱サンプルは全曲ぶん揃っていること
    from soramimic_video.api import STATIC_DIR

    manifest = json.loads(
        (STATIC_DIR / "sample" / "samples.json").read_text(encoding="utf-8")
    )
    assert manifest
    for entry in manifest:
        assert entry.get("title_kana"), entry


def test_synth_credit_of_voicevox_uses_character_name(monkeypatch):
    # VOICEVOXは規約上キャラ名込みの表記が要るので、スタイルIDから名前を引く
    from soramimic_video import voicevox as vv_mod

    monkeypatch.setattr(
        vv_mod, "list_singers",
        lambda url, timeout=5.0: [
            {"name": "四国めたん", "style_name": "ノーマル", "style_id": 3003, "type": "sing"},
            {"name": "春日部つむぎ", "style_name": "ノーマル", "style_id": 3008, "type": "sing"},
        ],
    )
    config = {"voicevox_url": "http://localhost:50021"}
    assert api_mod.synth_credit_of(
        {"synthesizer": "voicevox", "voicevox_style": 3008}, config
    ) == "VOICEVOX:春日部つむぎ"
    # 一覧に無いスタイルIDなら名前なしで表記する
    assert api_mod.synth_credit_of(
        {"synthesizer": "voicevox", "voicevox_style": 9999}, config
    ) == "VOICEVOX"


def test_synth_credit_of_voicevox_engine_down(monkeypatch):
    # エンジンが落ちていても表記自体は落とさない(名前なしのVOICEVOX)
    from soramimic_video import voicevox as vv_mod

    def boom(url, timeout=5.0):
        raise RuntimeError("engine down")

    monkeypatch.setattr(vv_mod, "list_singers", boom)
    assert api_mod.synth_credit_of(
        {"synthesizer": "voicevox", "voicevox_style": 3003}, {"voicevox_url": "http://x"}
    ) == "VOICEVOX"


def test_synth_credit_of_neutrino_is_empty():
    # NEUTRINOは公式FAQで名称の記載が任意なので焼き込まない
    assert api_mod.synth_credit_of({"synthesizer": "neutrino", "model": "MERROW"}, {}) == ""
    assert api_mod.synth_credit_of({}, {}) == ""


def test_synth_credit_of_prettypitch_names_engine_voice_and_provider():
    assert api_mod.synth_credit_of(
        {"synthesizer": "prettypitch"}, {}
    ) == "PrettyPitch / 波音リツ（カノン）"


def test_ogp_image_is_public_versioned_png(client):
    from PIL import Image

    # v1はimmutable URLとして公開済みなので、既存SNSキャッシュ向けに残す。
    for version in ("v1", "v2", "v3", "v4", "v5"):
        response = client.get(f"/ogp-soramimic-{version}.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(response.content) < 5 * 1024 * 1024
        with Image.open(
            api_mod.STATIC_DIR / f"ogp-soramimic-{version}.png"
        ) as image:
            assert image.size == (1200, 630)

    for version in ("v4", "v5"):
        with Image.open(api_mod.STATIC_DIR / f"ogp-soramimic-{version}.png") as image:
            # 旧ロゴを隠していた単色矩形の境界を、新版へ持ち込まない。
            image = image.convert("RGB")
            for y in (55, 294):
                for x in (160, 1040):
                    before = image.getpixel((x - 1, y))
                    after = image.getpixel((x, y))
                    assert max(abs(a - b) for a, b in zip(before, after, strict=True)) <= 1


def test_brand_logo_is_public_versioned_transparent_png(client):
    from PIL import Image

    response = client.get("/logo-soramimic-v1.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(response.content) < 1024 * 1024
    with Image.open(api_mod.STATIC_DIR / "logo-soramimic-v1.png") as image:
        assert image.mode == "RGBA"
        assert image.size == (873, 133)
        assert image.getpixel((0, 0))[3] == 0


def test_brand_symbols_are_public_versioned_transparent_png(client):
    from PIL import Image

    for version in ("v1", "v2", "v3"):
        response = client.get(f"/logo-soramimic-symbol-{version}.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(response.content) < 1024 * 1024
        with Image.open(
            api_mod.STATIC_DIR / f"logo-soramimic-symbol-{version}.png"
        ) as image:
            assert image.mode == "RGBA"
            assert image.size == (512, 512)
            assert image.getpixel((0, 0))[3] == 0

    with Image.open(api_mod.STATIC_DIR / "logo-soramimic-symbol-v2.png") as image:
        # 顔なし版の正本。頭中央に目や口の色を混ぜない。
        face_area = image.crop((230, 420, 282, 470))
        assert all(
            face_area.getpixel((x, y)) == (255, 255, 255, 255)
            for y in range(face_area.height)
            for x in range(face_area.width)
        )


def test_index_declares_versioned_brand_favicon(client):
    response = client.get("/")

    assert response.status_code == 200
    assert '<link rel="icon" href="/logo-soramimic-symbol-v3.png">' in response.text


def test_web_analytics_beacon_only_on_production_host(client):
    production = client.get("/", headers={"host": "video.soramimic.com"})
    assert production.status_code == 200
    assert api_mod.WEB_ANALYTICS_SNIPPET in production.text

    for hostname in ("dev-video.soramimic.com", "preview-video.soramimic.com"):
        response = client.get("/", headers={"host": hostname})
        assert response.status_code == 200
        assert "static.cloudflareinsights.com/beacon.min.js" not in response.text


def test_designer_wordmarks_are_public_versioned_transparent_png(client):
    from PIL import Image

    for version, expected_size in (("v1", (1397, 199)), ("v2", (1929, 276))):
        response = client.get(f"/logo-soramimic-wordmark-{version}.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(response.content) < 1024 * 1024
        with Image.open(
            api_mod.STATIC_DIR / f"logo-soramimic-wordmark-{version}.png"
        ) as image:
            assert image.mode == "RGBA"
            assert image.size == expected_size
            assert image.getpixel((0, 0))[3] == 0


def test_canva_horizontal_logos_are_public_transparent_pngs(client):
    from PIL import Image

    expected = {
        "logo-soramimic-horizontal-v1.png": (1937, 350),
        "logo-soramimic-horizontal-v2.png": (1859, 394),
        "logo-soramimic-video-v1.png": (1937, 373),
        "logo-soramimic-video-v2.png": (1900, 467),
    }
    for filename, expected_size in expected.items():
        response = client.get(f"/{filename}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(response.content) < 1024 * 1024
        with Image.open(api_mod.STATIC_DIR / filename) as image:
            assert image.mode == "RGBA"
            assert image.size == expected_size
            assert image.getpixel((0, 0))[3] == 0


# ---- レイアウト名とlayout.jsonの食い違い(別リストのカードで生成される事故) ----


def test_resolve_layout_uses_name_without_layout_json(tmp_path):
    # レイアウト名だけのジョブは名前がそのままvideoステージに渡る(仕様の明文化)
    job = api_mod.Job(id="j1", dir=tmp_path, params={"layout": "caption"})
    assert api_mod.resolve_layout(job, {}) == ("caption", "name:caption")


def test_resolve_layout_json_overrides_name_and_warns(tmp_path, caplog):
    # layout.json はレイアウト名より優先される(従来仕様)。ただし黙って
    # 名前を無視すると事故の追跡ができないのでWARNINGを残す
    (tmp_path / "layout.json").write_text("{}", encoding="utf-8")
    job = api_mod.Job(id="j2", dir=tmp_path, params={"layout": "scientist_card"})
    with caplog.at_level(logging.WARNING, logger="soramimic_video.api"):
        layout, source = api_mod.resolve_layout(job, {})
    assert layout == str(tmp_path / "layout.json")
    assert source == "json:layout.json"
    assert "scientist_card" in caplog.text and "layout.json" in caplog.text


def test_resolve_layout_falls_back_to_server_default(tmp_path):
    job = api_mod.Job(id="j3", dir=tmp_path, params={})
    assert api_mod.resolve_layout(job, {"layout": "caption"}) == (
        "caption",
        "server-default:caption",
    )
    assert api_mod.resolve_layout(job, {}) == (None, "builtin-default")


def test_layout_name_only_job_has_no_layout_json(client):
    # layout_json を送らないジョブには layout.json が作られず、レイアウト名が使われる
    job_id = submit(client, wordlist="stations", layout="caption")
    wait_done(client, job_id)
    job = client.app.state.manager.jobs[job_id]
    assert not (job.dir / "layout.json").exists()
    assert api_mod.resolve_layout(job, {}) == ("caption", "name:caption")


def _index_html() -> str:
    return (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )


def test_job_credits_download_and_missing_history(client):
    from soramimic_video.credits import write_credit_files

    job_id = submit(client, wordlist="stations")
    body = wait_done(client, job_id)
    url = body["credits_url"]
    assert client.get(url).status_code == 404
    job = client.app.state.manager.jobs[job_id]
    write_credit_files([{"original": "素材", "image_page": "https://youtu.be/a?t=42",
                         "image_terms_page": "https://example.com/terms",
                         "image_credit": "非公式"}],
                       job.dir / "video")
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["items"][0]["image_credit"] == "非公式"
    response = client.get(url, params={"download": "true"})
    assert response.status_code == 200
    assert 'filename="credits.md"' in response.headers["content-disposition"]
    assert "https://youtu.be/a?t=42" in response.text
    assert "https://example.com/terms" in response.text
    job.status = "running"
    assert client.get(url).status_code == 409


def test_image_sources_restricts_paths_and_preserves_fsp_metadata(client):
    for name in ["../vtuber", "/etc/passwd", "vtuber.csv"]:
        assert client.get("/api/image-sources", params={"wordlist": name}).status_code == 404
    response = client.get("/api/image-sources", params={"wordlist": "vtuber"})
    assert response.status_code == 200
    rows = response.json()
    fsp = [row for row in rows if row["image_terms_page"] == "https://firststage-pro.com/guideline/"]
    assert len(fsp) == 27
    assert all(row["image"] and row["image_page"] and row["image_credit"] for row in fsp)
    assert all(row["image_usage"] == "noncommercial_fanwork" for row in fsp)


def test_audio_analysis_detail_is_live_and_cleared_between_stages(tmp_path):
    job = api_mod.Job(id="analysis-detail", dir=tmp_path, params={"input_kind": "audio"})
    job.status = "running"
    with api_mod._stage(job, "analyze"):
        job.stage_detail = "歌詞の時刻合わせ · 12/30行"
        job.stage_progress = 66
        body = job.to_dict()
        assert body["stage_detail"] == "歌詞の時刻合わせ · 12/30行"
        assert body["stage_progress"] == 66
        assert "stage_eta_seconds" not in body
    with api_mod._stage(job, "synthesize"):
        assert job.stage_detail is None
        assert "stage_detail" not in job.to_dict()
    job.status = "done"
    assert "stage_detail" not in job.to_dict()


def test_hybrid_credit_names_both_engines_and_voice_provider():
    assert api_mod.synth_credit_of({"synthesizer": "hybrid"}, {}) == (
        "VOICEVOX・PrettyPitch：波音リツ（カノン）"
    )


@pytest.mark.parametrize("credit", ["オトノケ - Creepy Nuts\n© 指定表記", ""])
def test_custom_song_text_is_stored_without_changing_wordlist(client, credit):
    job_id = submit(
        client, wordlist="stations", song_title="別の曲名",
        song_credit=credit, thumbnail_wordlist_label=" 好きな駅 ",
    )
    params = wait_done(client, job_id)["params"]
    assert params["song_credit"] == credit
    assert params["thumbnail_wordlist_label"] == "好きな駅"
    assert params["wordlist"] == "stations"


@pytest.mark.parametrize("field,length", [("song_credit", 2001), ("thumbnail_wordlist_label", 101)])
def test_custom_song_text_has_size_limits(client, field, length):
    res = client.post(
        "/api/jobs", files={"midi": ("song.mid", FAKE_MIDI, "audio/midi")},
        data={"wordlist": "stations", field: "あ" * length},
    )
    assert res.status_code == 422


def test_audio_json_overrides_recognition_adjustment_and_plain_lyrics(client):
    payload = {"phrases": ["生"], "unitsList": [[{"pronunciation": "ナマ"}]],
               "results": [[]], "wordlist": {"filepath": "stations.csv"}}
    response = client.post("/api/jobs", files={
        "audio": ("input.wav", fake_wav(), "audio/wav"),
        "editor": ("lyrics.json", json.dumps(payload).encode(), "application/json"),
    }, data={"lyrics": "古い歌詞", "auto_lyrics": "true", "adjust_lyrics": "true",
             "auto_phrase_lyrics": "true", "wordlist": "pokemon"})
    assert response.status_code == 200, response.text
    job = client.app.state.manager.jobs[response.json()["id"]]
    assert (job.dir / "lyrics.txt").read_text() == "生"
    assert json.loads((job.dir / "editor.json").read_text()) == payload
    assert job.params["auto_lyrics"] is False
    assert job.params["adjust_lyrics"] is False
    assert job.params["auto_phrase_lyrics"] is False
    assert job.params["wordlist"] == "stations"


def test_audio_json_missing_readings_is_rejected_before_job_creation(client):
    response = client.post("/api/jobs", files={
        "audio": ("input.wav", fake_wav(), "audio/wav"),
        "editor": ("lyrics.json", json.dumps({"phrases": ["生"], "unitsList": [[{}]],
                                              "results": [[]]}).encode(), "application/json"),
    }, data={"wordlist": "stations"})
    assert response.status_code == 422
    assert "読み" in response.json()["detail"]
    assert not client.app.state.manager.jobs


def test_audio_pipeline_passes_json_to_fixed_alignment(tmp_path, monkeypatch):
    from soramimic_video import score_audio

    class ReachedAnalyzer(Exception):
        pass

    editor = tmp_path / "editor.json"
    editor.write_text("{}")

    def analyze(*args, **kwargs):
        assert kwargs["editor_path"] == editor
        raise ReachedAnalyzer

    monkeypatch.setattr(score_audio, "analyze_audio", analyze)
    job = api_mod.Job(id="json", dir=tmp_path, params={"input_kind": "audio"})
    with pytest.raises(ReachedAnalyzer):
        api_mod.run_pipeline(job, {})
