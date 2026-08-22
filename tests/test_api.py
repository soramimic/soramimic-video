"""APIサーバー(api.py)のテスト。

パイプライン本体はモックし、ジョブの受付→実行→動画取得の流れと
APIキー認証を確認する。NEUTRINO実行込みのE2Eは手動(serve)で行う。
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import wave
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402

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
    assert res.headers["content-disposition"].startswith("attachment;")
    playback = client.get(body["playback_url"])
    assert playback.status_code == 200
    assert playback.content == FAKE_MP4
    assert playback.headers["content-type"] == "video/mp4"
    assert playback.headers["content-disposition"].startswith("inline;")
    assert playback.headers["cache-control"] == "private, no-store"


def test_job_flow_accepts_wav_and_keeps_existing_playback(client):
    wav = fake_wav()
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", wav, "audio/wav")},
        data={"wordlist": "stations", "lyrics": "あ"},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["input_kind"] == "audio"
    assert body["song_label"] == "アップロードした曲"
    job = client.app.state.manager.jobs[job_id]
    assert (job.dir / "input.wav").read_bytes() == wav
    assert not (job.dir / "input.mid").exists()
    assert client.get(body["playback_url"]).content == FAKE_MP4


@pytest.mark.parametrize(
    ("filename", "content", "detail"),
    [
        ("voice.mp3", fake_wav(), "WAVファイルを選んでください"),
        ("voice.wav", b"not a wav", "WAVファイルではありません"),
    ],
)
def test_rejects_invalid_wav(client, filename, content, detail):
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
    assert "WAV入力を利用できません" in res.json()["detail"]


def test_wav_upload_limit_is_configurable(client, monkeypatch):
    monkeypatch.setenv(api_mod.MAX_AUDIO_UPLOAD_BYTES_ENV, "64")
    res = client.post(
        "/api/jobs",
        files={"audio": ("voice.wav", fake_wav(), "audio/wav")},
        data={"wordlist": "stations"},
    )
    assert res.status_code == 413


def test_run_pipeline_dispatches_wav_to_audio_analyzer(tmp_path, monkeypatch):
    from soramimic_video import analyze_audio as analyze_audio_mod

    class ReachedAnalyzer(Exception):
        pass

    audio = tmp_path / "input.wav"
    audio.write_bytes(fake_wav())
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("あ", encoding="utf-8")

    def fake_analyze(audio_path, project_dir, **kwargs):
        assert audio_path == audio
        assert project_dir == tmp_path
        assert kwargs["lyrics_path"] == lyrics
        assert kwargs["whisper_model"] == "small"
        raise ReachedAnalyzer

    monkeypatch.setattr(analyze_audio_mod, "analyze_audio", fake_analyze)
    job = api_mod.Job(
        id="wavtest",
        dir=tmp_path,
        params={"input_kind": "audio"},
    )
    with pytest.raises(ReachedAnalyzer):
        api_mod.run_pipeline(job, {})


def test_noncommercial_fanwork_is_explicit_and_persisted(client):
    ordinary = submit(client, editor=b'{"format": "soramimic-editor/1"}')
    assert wait_done(client, ordinary)["params"]["allow_noncommercial_fanwork"] is False

    opted_in = submit(
        client,
        editor=b'{"format": "soramimic-editor/1"}',
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


def test_index_html_forwards_wordlist_filter_to_the_editor():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert '<input type="hidden" id="where">' in html
    assert 'form.append("where", $("where").value.trim());' in html


def test_note_length_weight_setting_moved_to_soramimic():
    # 移設先の確認はsubmoduleが要る。CIではsoramimic本体を取得しないので飛ばす
    editor_path = (
        Path(api_mod.__file__).parents[2] / "external/soramimic/frontend/editor.html"
    )
    if not editor_path.is_file():
        pytest.skip("submodule未取得")
    editor_html = editor_path.read_text(encoding="utf-8")
    assert 'id="editor-note-length-alpha"' in editor_html
    assert 'min="0" max="2" step="0.05" value="0.25"' in editor_html


def test_index_html_keeps_note_length_default_for_non_editor_flow():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'return "NOTE_LENGTH_WEIGHT=0.25";' in html

def test_index_html_model_layout_use_select_not_datalist():
    # iOS Safari が datalist を表示しない問題への対応:
    # 歌声モデル(#model)・レイアウト(#layout)は select + 手入力 + 隠しvalue に置換。
    # 送信フィールド名(#model / #layout の hidden)は据え置きでAPI互換を保つ。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # iOS Safariでも選択肢を表示できることを、現在のコントロールで確認する。
    assert "<datalist" not in html
    assert 'list="model-list"' not in html and 'list="layout-list"' not in html
    # 各コントロールの select と送信用hiddenが揃っている
    for base in ("model", "layout"):
        assert f'<select id="{base}-select">' in html
        assert f'<input type="hidden" id="{base}"' in html
    # 手入力欄を持つのは #model だけ(#layout はファイル読み込みに置き換えた)
    assert '<input type="text" id="model-other"' in html
    # 「その他(手入力)」の選択肢が存在する
    assert 'その他(手入力)' in html


def test_index_html_gates_neutrino_by_config():
    """NEUTRINO未設定のサーバーではNEUTRINOを選ばせない(選ぶとジョブが必ず失敗する)。"""
    html = _index_html()
    # /api/config の neutrino で選択肢を無効化する
    assert 'id="synth-neutrino-opt"' in html
    assert "setupNeutrino(conf.neutrino)" in html
    assert 'opt.disabled = !ok;' in html
    # 選択がneutrinoになる経路(既定値・VOICEVOX未起動フォールバック・状態復元)は
    # すべて「NEUTRINOが使えるとき」に限る
    assert '$("synthesizer").value === "voicevox" && neutrinoAvailable()' in html
    assert 'state.synthesizer === "neutrino" && neutrinoAvailable()' in html
    # 両方使えないときは案内を出す(空の選択肢のまま黙らせない)
    assert 'id="synth-unavailable"' in html


def test_index_html_explains_missing_custom_wordlist_preview():
    html = _index_html()
    assert "自作リストはプレビューに対応していません。" in html
    assert "const custom = showsEditorWordlist();" in html


def test_index_html_hides_preview_for_sensitive_wordlists():
    """ビルダーカードのサムネプレビューで、昆虫などの画像を初期非表示にする。

    黙って出さないのではなく「隠している理由」と「画像を表示する」ボタンを出す。
    対象はこのプレビューだけで、動画・サムネの画像は従来どおり。
    """
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
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


def test_index_html_builder_card_has_selects():
    """カードで曲・単語リストを選べる。隠しの正本と写しを双方向に同期する。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    card = html.split('<section class="card" id="lucky-card">')[1].split("</section>")[0]
    # プルダウンは2つ横並び。サムネ枠より上に置く(選ぶ → 下に絵が出る)
    assert '<div class="builder-selects">' in card
    assert '<select id="builder-sample" aria-label="曲(サンプル曲)"></select>' in card
    assert (
        '<select id="builder-wordlist" aria-label="単語リスト(何に空耳させるか)"></select>'
        in card
    )
    assert card.index('class="builder-selects"') < card.index('id="builder-figure"')
    # 写し同期(選択肢と値)。正本は #sample-select / #wordlist-select のまま
    assert "function syncBuilderOptions() {" in html
    assert "function syncBuilderValues() {" in html
    assert '$("builder-sample").innerHTML = $("sample-select").innerHTML;' in html
    # カードで選んだら正本へ書いて change を発火する(既存ロジックがそのまま動く)
    assert '$("sample-select").value = $("builder-sample").value;' in html
    assert 'sel.dispatchEvent(new Event("change", { bubbles: true }));' in html
    # 正本が変わればカードのプルダウンとプレビューが追従する
    assert (
        '$("sample-select").addEventListener("change", '
        "() => { syncBuilderValues(); schedulePreview(); });" in html
    )
    assert (
        '$("wordlist-select").addEventListener("change", () => {\n'
        '  showFanworkError("");\n'
        "  syncBuilderValues();\n"
        "  schedulePreview();\n"
        "});" in html
    )
    advanced = _advanced_html()
    assert re.findall(r'<select id="([^"]+)"', advanced) == [
        "synthesizer", "model-select", "voicevox-style",
    ]
    # 🎲ランダムはカードの選択を差し替える
    assert '$("lucky").addEventListener("click", () => pickCombo(luckyRandomCombo()));' in html
    # 進捗・結果は畳んである詳細(ビルダーカードの中の控えめなテキストリンク)
    assert '<details class="sub-details" id="job-card" hidden>' in html


def test_index_html_builder_card_has_gear_and_editor_status():
    """カードの右上は⚙(替え歌を編集)と🎲(ランダム)。替え歌の状態表示もカードに置く。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    card = html.split('<section class="card" id="lucky-card">')[1].split("</section>")[0]
    # ⚙は🎲と同じ topbar に置き、押すと従来と同じ導線(openEditorFlow)で開く
    assert '<button type="button" id="builder-edit" class="btn-sm"' in card
    assert 'aria-label="替え歌を編集" title="替え歌を編集"' in card
    assert card.index('id="builder-edit"') < card.index('id="lucky"')
    assert '$("builder-edit").addEventListener("click", openEditorFlow);' in html
    # エディタを同梱していないサーバーでは⚙ごと出さない
    assert card.index('id="editor-embed-controls"') < card.index('id="builder-edit"')
    # 替え歌の状態表示もカードに移した(エディタの導線がカードにあるので)
    assert 'id="parody-status"' in card
    # 「保存済みの替え歌があります」の選択はカード内に展開せず(サムネ枠が押し
    # 下がる)、他のモーダルと同じく .wrap の外のモーダルにする
    assert 'id="editor-resume"' not in card
    assert 'id="editor-resume"' in html


def test_index_html_job_card_is_collapsed_by_default():
    """ジョブの詳細は既定で畳み、エラー・中断のときだけ自動で開く。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # ビルダーカードの最下部に置く小さなテキストリンク(.sub-details)。
    assert '<details class="sub-details" id="job-card" hidden>' in html
    assert "<summary>生成の詳細(ステージ・ログ)</summary>" in html
    assert '<pre id="log"></pre>' in html
    # 開閉は保存しない。投入のたびに閉じた状態から始める
    assert '$("job-card").open = false;' in html
    # 失敗・中断で終わったときだけ自動で開く
    assert '$("job-card").open = true;' in html
    assert html.count("openJobDetails();") == 5


def test_index_html_job_card_lives_in_the_builder_card():
    """「生成の詳細」はビルダーカードの中(最下部)にあり、エラー時だけ目立たせる。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    builder = html.split('<section class="card" id="lucky-card">')[1].split("</section>")[0]
    assert 'id="job-card"' in builder
    # 最下部(サムネ枠・上限や確認の表示のあと)
    assert builder.index('id="job-card"') > builder.index('id="public-limits"')
    # 控えめな置き場のぶん、エラー・中断で開いたときだけ警告色にして見つけやすくする
    assert '#job-card.attention > summary { color: var(--danger);' in html
    assert '$("job-card").classList.add("attention");' in html
    assert '$("job-card").classList.remove("attention");' in html


def test_index_html_keeps_form_restore_and_file_hint():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "function saveForm()" in html
    assert "async function doRestoreForm()" in html
    assert "localStorage.setItem(FORM_KEY" in html
    assert 'id="midi-restore-hint"' in html


def test_index_html_builder_card_has_labeled_topbar_actions():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # ⚙と🎲はカードの右上に小さく置くだけ。ただしアイコンだけだと気づかれない
    # ので、文字ラベルを添えたピルにする(絵文字は読み上げ対象から外す)
    assert '<div class="builder-topbar">' in html
    assert '<button type="button" id="lucky" class="btn-sm"' in html
    assert '<span class="lucky-icon" aria-hidden="true">🎲</span>ランダム</button>' in html
    assert '<span class="lucky-icon" aria-hidden="true">⚙</span>編集</button>' in html
    assert "border-radius: 999px; background: var(--panel-2);" in html


def test_index_html_builder_frame_runs_the_whole_flow():
    """サムネ枠がそのまま「生成ボタン → 進捗 → 動画プレイヤー」に変わる。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # 枠のタップで生成が始まる(これが唯一の生成導線)
    assert '<button type="button" class="builder-play" id="builder-play"' in html
    assert '$("builder-play").addEventListener("click", () => submitJob(0));' in html
    assert "タップで動画を生成" in html
    # 進捗は同じ枠の中に重ねる
    assert '<div class="builder-progress" id="builder-progress" hidden>' in html
    assert 'setBuilderState("running");' in html
    # 中断は生成中も枠の中から押せる
    assert '$("builder-cancel").addEventListener("click", cancelJob);' in html
    # 完成したら同じ枠が動画プレイヤーになり、シェアは枠の直下に出る
    assert '<video id="builder-video" controls playsinline preload="none" hidden></video>' in html
    assert "video.poster = qs(job.thumbnail_url);" in html
    assert '$("builder-share").innerHTML = SHARE_HTML;' in html
    # 選び直したら枠は新しいプレビューに戻る
    assert 'setBuilderState("preview");   // 完成した動画が出ていれば' in html


def test_index_html_builder_submit_is_gated_while_busy():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "let submitBusy = false;" in html
    assert "submitBusy = busy;" in html
    assert "&& $(\"builder-loading\").hidden && !submitBusy);" in html
    assert '上の「曲」から選ぶか🎲で選び直してください' in html
    assert '<p class="error" id="submit-msg" hidden></p>' in html


def test_index_html_settings_change_returns_frame_to_preview():
    """完成した動画が出ていても、設定を触れば枠はプレビュー(=タップで生成)に戻る。

    生成の導線が枠のタップだけになったので、詳細設定(歌声・変換パラメータ・
    レイアウトなど)を変えたあとに再生成できなくなる状態を作ってはいけない。
    """
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "function releaseBuilderDone() {" in html
    assert 'if (builderState !== "done") return;' in html
    # 入力の変化は文書全体でまとめて拾う(詳細設定の中のどれでも戻る)
    assert 'for (const type of ["change", "input"]) {' in html
    assert "document.addEventListener(type, (ev) => {" in html
    # ドラッグでのレイアウト編集は change を発火しないので自分で呼ぶ
    assert "releaseBuilderDone();   // 同じ理由で" in html
    # サムネが用意できなくても枠は残す(押せる場所が無くならないように)
    assert "指で押せる高さを自分で持つ" in html
    assert "fig.hidden = false;\n    updateBuilderOverlay();\n  };" in html


def test_index_html_polling_survives_background():
    """iOSでアプリを切り替えて戻ったとき、進捗ポーリングが再開すること(#139)。

    バックグラウンド中はタイマーが凍り、fetchも失敗する。以前は失敗すると
    setTimeoutのチェーンが張り直されず、戻っても進捗が止まったままだった。
    """
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
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


def test_index_html_elapsed_seconds_come_from_server():
    """経過秒はクライアントで加算せず、サーバーの値をそのまま出す。

    バックグラウンドから戻ったとき、1回ポーリングするだけで正しい値に戻る。
    """
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # ステージ名と経過秒を組み立てるのはこの1か所だけ
    assert "`${label}${elapsed}`" in html
    assert "const elapsed = job.stage_elapsed ? ` (${Math.round(job.stage_elapsed)}秒経過)`" in html
    # 経過秒を進めるためだけのタイマーは持たない
    assert "elapsedTimer" not in html


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


def test_run_pipeline_builds_silent_video_in_parallel(tmp_path, monkeypatch):
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
        params={"model": "MERROW", "synthesizer": "neutrino", "wordlist": "stations"},
    )
    audio_started = threading.Event()
    visual_started = threading.Event()

    monkeypatch.setattr(xfparse, "analyze_midi", lambda path: project)
    monkeypatch.setattr(editor_io, "import_editor", lambda *a, **k: None)

    def fake_synthesize(job, config, project, synthesize, octave_keys=None):
        with api_mod._stage(job, "synthesize"):
            audio_started.set()
            assert visual_started.wait(1)

    def fake_prepare(*args, **kwargs):
        visual_started.set()
        assert audio_started.wait(1)
        return object()

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

    def fake_attach(silent, audio, total, out=None):
        assert silent.read_bytes() == b"silent"
        assert audio.read_bytes() == b"wav"
        assert total == 9.0
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
    monkeypatch.setattr(video_mod, "prepare_video", lambda *a, **k: object())

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


def test_config_has_wordlist_layouts(client):
    conf = client.get("/api/config").json()
    wl = conf["wordlist_layouts"]
    assert wl["scientist"] == "scientist_card"
    # 値はすべて組み込みレイアウト名(UIがそのまま#layoutに入れるため)
    assert set(wl.values()) <= set(conf["layouts"])


def test_config_has_youtuber_image_policy(client):
    conf = client.get("/api/config").json()
    assert conf["wordlist_image_policies"]["youtuber"] == {
        "usage": "noncommercial_fanwork",
        "terms": "https://hololivepro.com/terms/",
        "terms_pages": [
            {
                "url": "https://hololivepro.com/terms/",
                "label": "ホロライブプロダクション二次創作ガイドライン",
            },
            {
                "url": "https://www.anycolor.co.jp/guidelines/",
                "label": "ANYCOLOR二次創作ガイドライン",
            },
            {
                "url": "https://vhs-city.com/aogirihighschool/guidelines/fanfic",
                "label": "あおぎり高校二次創作ガイドライン",
            },
        ],
    }


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
    editor = b'{"wordlist": {"filepath": "wordlists/stations.csv"}}'
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


def test_index_html_has_platform_appropriate_save_share_buttons():
    # モバイルは保存・共有1ボタン、PCはダウンロードと共有を分ける。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="share-save"' in html
    assert 'id="download-video"' in html
    assert "const FILE_SHARE_SUPPORTED = supportsVideoFileShare();" in html
    assert 'const DESKTOP_SHARE_UI = mobilePlatform() === "other";' in html
    support = html[html.index("function supportsVideoFileShare()") :]
    support = support[: support.index("\n}\n")]
    # iOS系ブラウザはcanShareが無い/誤判定することがある。共有APIとFileが
    # あれば実ファイルを再生と共有で共用し、クリック時にshareを直接試す。
    assert "navigator.canShare" not in support and "probe" not in support
    assert 'typeof navigator.share === "function"' in support
    assert 'typeof File !== "undefined"' in support
    assert 'typeof URL.createObjectURL === "function"' in support
    assert 'typeof URL.revokeObjectURL === "function"' in support
    assert "VIDEO_SHARE_PREPARE_TIMEOUT_MS" in html
    assert "navigator.canShare" not in html
    assert "navigator.share({ files: [prepared.file], text: SHARE_TEXT })" in html
    assert "#Soramimic" in html
    assert "#そらみみっく" in html
    click = html[html.index("function bindShare(videoUrl)") :]
    click = click[: click.index("\n}\n")]
    assert "fetch(" not in click and "await " not in click
    assert 'const downloadBtn = $("download-video");' in click
    assert "downloadVideo(videoUrl)" in click
    assert "AbortError" in click


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


def test_index_html_share_hint_matches_platform_capability():
    """モバイルは保存・共有、PCはダウンロードと共有の案内に切り替える。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert '/iPhone|iPad|iPod/i.test(platform)' in html
    assert 'return "ios";' in html and 'return "android";' in html
    assert '「ビデオを保存」「ファイルに保存」' in html
    assert "ボタンを押すと、動画ファイルをダウンロードします。" in html
    assert '"動画をダウンロード"' in html
    assert "動画をダウンロードするか、共有メニューから共有先を選べます。" in html


def test_index_html_shows_neutrino_configuration_warning():
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "NEUTRINO_ROOT未設定" in html


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


def test_index_html_sends_layout_json_only_when_edited():
    # 事故の本命: レイアウト名を切り替えてもテキストエリアの古いJSONが送られ、
    # サーバー側で名前より優先されて別リストのカードになっていた。
    # エディタを編集したとき(leDirty)だけ layout_json を送る。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'if (leDirty && $("layout-json").value.trim()) {' in html
    assert '    form.append("layout_json", $("layout-json").value);' in html
    # 読み込んだだけのJSONは編集扱いにしない。leToJson が dirty のまま保存して
    # いるので、降ろしたあとに保存し直さないとリロードで編集扱いに戻ってしまう
    assert (
        "  leDirty = false;   // 読み込んだだけなので「ユーザーの編集」ではない\n"
        '  leLayoutFor = $("layout").value.trim();\n'
        "  saveForm();" in html
    )


def test_index_html_layout_load_clears_on_fetch_error():
    # leLoad の fetch は try/catch する。失敗したら編集中のレイアウトを捨てて
    # メッセージを出す(握りつぶすと前のリストのJSONが焼き付く)
    import re

    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    body = re.search(r"async function leLoad\(\) \{.*?\n\}", html, re.S).group(0)
    assert "try {" in body and "} catch (err) {" in body
    assert "leClearLayout();" in body
    assert "編集中のレイアウトは破棄しました" in body


def test_index_html_wordlist_layout_switch_clears_layout_json():
    # applyWordlistLayout はレイアウト名を入れた直後に「同期で」JSONを捨てる。
    # 非同期の leLoad 頼みだと、fetchが失敗したとき古いJSONが残る
    import re

    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    body = re.search(r"function applyWordlistLayout\(\) \{.*?\n\}", html, re.S).group(0)
    assert (
        body.index('setChoice("layout", next);')
        < body.index("leClearLayout();")
        < body.index('$("layout").dispatchEvent(new Event("change"')
    )


def test_index_html_restores_layout_json_only_when_layout_matches():
    # 保存したJSONの出どころ(layoutJsonFor)が復元するレイアウト名と一致する
    # ときだけ復元する。ズレたJSONをリロードのたびに再生産しないため
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "layoutJsonFor: leLayoutFor," in html
    assert "layoutDirty: leDirty," in html
    # ベースの素性とクリーンな内容も保存する(リロード後も「〈ベース名〉の編集」に戻す)
    assert "layoutBaseKey: leBaseKey," in html
    assert "layoutBaseline: leBaseline," in html
    assert 'if (state.layoutJson && state.layoutJsonFor === (state.layout || "")) {' in html


# ---- 曲・元歌詞の正本は非表示、操作はカードとエディタに一本化 ----


def _index_html() -> str:
    return (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )


def _advanced_html() -> str:
    """詳細設定(#advanced)の中身だけ。後ろのモーダル類は含めない。"""
    body = _index_html().split('<details class="card" id="advanced">')[1]
    return body.split('<details class="card" id="history">')[0]


def test_index_html_song_values_are_hidden_canonicals():
    """曲・MIDI・元歌詞の正本はDOMに残すが、詳細設定には表示しない。

    選ぶ操作はビルダーカードと替え歌エディタに移譲済み。送信・保存・復元・MIDI検証・
    親子同期はこのIDを読むので、要素そのものは hidden の正本として温存する。
    """
    html = _index_html()
    store = html.split('<div id="song-store" hidden>')[1].split("<!-- 2.")[0]
    assert '<input type="file" id="midi" accept=".mid,.midi">' in store
    assert '<select id="sample-select" aria-label="サンプル曲"></select>' in store
    assert '<textarea id="lyrics"></textarea>' in store
    # 隠しのまま置いてよいのは、中身が別の見える場所へ中継されるか、
    # 読まれなくてもユーザーが困らないものだけ(理由はマークアップのコメント)
    for dynamic_id in (
        "midi-restore-hint",
        "midi-error",
        "sample-status",
    ):
        assert f'id="{dynamic_id}"' in store

    # 正本をhiddenへ移してもカード・🎲・エディタとの既存経路は維持する
    assert '$("sample-select").value = c.sampleId;' in html
    assert '$("sample-select").addEventListener("change", () => {' in html
    assert "trackSample(applySample()).then((ok) => {" in html
    assert (
        '$("sample-select").addEventListener("change", '
        "() => { syncBuilderValues(); schedulePreview(); });" in html
    )
    assert '$("sample-details").hidden = true;' in html


def test_index_html_song_warnings_stay_reachable_without_section_one():
    """詳細設定①に出していた警告が、正本をhiddenにしたことで黙って消えていない。

    行き先は警告の役割で分ける:
    - 歌詞なしMIDIの拒否は生成も編集も止めるので、隠しの #midi-error に入れたうえで
      見える #submit-msg へ中継する
    - 元歌詞とMIDI歌詞の食い違いは生成を止めない助言で中継先が無いため、要素ごと
      ビルダーカードへ移して見える場所に置く
    """
    html = _index_html()
    assert '$("midi-error").hidden = false;' in html
    assert "showSubmitMsg(text);" in html   # 拒否の文面をカードの生成導線の下へ

    card = html.split('<section class="card" id="lucky-card">')[1].split("</section>")[0]
    assert '<p class="hint" id="lyrics-midi-warn" hidden></p>' in card
    # 書き込む側(renderLyricsMidiWarning)は移設後もそのままIDを読む
    assert 'const el = $("lyrics-midi-warn");' in html


# ---- 詳細設定(#advanced)の情報整理: 役割ごとのグループを使用頻度順に並べる ----


def _opt_groups() -> dict[str, str]:
    """詳細設定のグループ見出し → そのグループのHTML。"""
    import re

    out: dict[str, str] = {}
    for part in _advanced_html().split('<section class="opt-group">')[1:]:
        body = part.split("</section>")[0]
        title = re.search(r'<h3 class="opt-group-title">(.*?)</h3>', body)
        assert title, body[:200]
        out[title.group(1)] = body
    return out


def test_index_html_advanced_is_grouped_by_role():
    """フラットな長い並びをやめ、役割ごとのグループを使用頻度順に置く。

    「空耳のもと」(単語リスト選択・エディタの導線)は、選ぶ操作がエディタの⚙と
    カードの⚙に移ったのでグループごと廃止した。
    """
    assert list(_opt_groups()) == [
        "① 歌声",
        "② 見た目",
        "③ 元曲クレジット",
    ]
    # どのグループにも1行の説明を添える
    assert _advanced_html().count('class="hint opt-group-lead"') == 3
    # 「⑤ その他」はAPIキーだけになったのでグループごとやめ、キー欄は先頭へ移した
    advanced = _advanced_html()
    assert advanced.index('id="auth"') < advanced.index('<section class="opt-group">')
    # 区切りは見た目にも出す(小見出し + 罫線)
    html = _index_html()
    assert ".opt-group + .opt-group {" in html
    assert ".opt-group-title { font-size: .9rem;" in html


def test_index_html_advanced_groups_hold_the_right_fields():
    """詳細設定には動画側の歌声と見た目だけを残す。"""
    g = _opt_groups()
    voice = g["① 歌声"]
    assert 'id="synthesizer"' in voice
    assert 'id="auto-octave"' in voice and 'id="transpose"' in voice
    assert 'id="preview"' in voice
    look = g["② 見た目"]
    # プリセット選択も編集も全画面モーダル(#le-modal)へ寄せたので、グループに残るのは
    # 送信値の正本(隠しinput)・モーダルを開くボタン・カスタム編集中の目印だけ。
    # レイアウトは単語リストに連動して自動で決まるものなので、ふだんは選ばせない
    assert 'id="layout"' in look and 'id="le-open"' in look
    assert 'id="le-status"' in look
    credit = g["③ 元曲クレジット"]
    assert 'id="original-credit"' in credit
    assert 'id="credit-notice"' in credit


def test_index_html_layout_base_select_lives_in_the_editor_modal():
    """ベースレイアウトのプルダウンはモーダルのツールバーに置き、選んだ即時に反映する。"""
    html = _index_html()
    modal = html.split('<div class="editor-modal" id="le-modal"')[1].split("<script>")[0]
    assert 'id="layout-select"' in modal
    # ベースからの差分は ● で示し、↺ でベースの内容へ戻せる
    assert 'id="le-modified"' in modal and 'id="le-revert"' in modal
    # ファイル入力そのものは隠し、label ボタンの for= で開く(.click() は使わない)
    assert 'id="layout-file" accept=".json" hidden' in modal
    handler = html.split('$("layout-select").addEventListener("change", async () => {')[1]
    handler = handler.split("\n});")[0]
    assert '$("layout-file").click();' not in html
    # 同じベースに戻ってきたら、退避しておいた編集を復元する
    assert "if (leStash && leStash.baseKey === key) { leRestoreStash(); return; }" in handler
    # 編集していないときも既存の退避を持ち越す(A編集→B→Cと見比べてもAへ戻れる)
    assert "const stash = leCaptureCustom() || leStash;" in handler
    assert "leStash = stash;" in handler
    # 新しい編集・レイアウトの破棄では退避も捨てる
    assert "leStash = null;" in html.split("function leToJson() {")[1].split("\n}")[0]
    assert "leStash = null;" in html.split("function leClearLayout() {")[1].split("\n}")[0]


def test_index_html_layout_base_values_never_reach_the_hidden_input():
    """ファイル系の選択値(センチネル・📄項目)は送信値#layoutに書かない。"""
    html = _index_html()
    assert 'const LE_FILE_PREFIX = "file:";' in html
    sync = html.split("function syncChoice(base) {")[1].split("\n}")[0]
    assert "if (leIsPresetValue(sel.value)) hidden.value =" in sync
    # ● はベース読み込み時の内容(leBaseline)との比較で決める
    modified = html.split("function leModified() {")[1].split("\n}")[0]
    assert "JSON.stringify(leLayout) !== leBaseline" in modified


def test_index_html_wordlist_values_are_hidden_canonicals():
    """単語リストの選択UIは無くし、値(名前・絞り込み)だけを隠して持つ。"""
    html = _index_html()
    store = html.split('<div id="wordlist-store" hidden>')[1].split("</div>")[0]
    # 選択肢の一覧(🎲の抽選・表示名の引き当てに使う)と、送信値の正本
    assert '<select id="wordlist-select" hidden></select>' in store
    assert '<input type="hidden" id="wordlist">' in store
    assert '<input type="hidden" id="where">' in store
    # 詳細設定の中には出さない(カードの外・折りたたみの外の隠し置き場)
    assert 'id="wordlist-store"' not in _advanced_html()
    # プルダウンの選択がその値を書き込む経路は維持する
    assert "setWhere(facetDefaultWhere(g));" in html


def test_index_html_keeps_the_conf_default_filters():
    """チェックボックスUIを畳んでも、editorと同じfacet既定の絞り込みは残す。

    駅名は現存駅だけ・流行はセンシティブ除外…といった既定が消えると、UIの整理が
    そのまま出力の変化になってしまう。組み立てた式が本当にエディタ側
    (convertControls.js の facetClause + compileWhere)と同じ形かは、実物のJSを
    走らせて突き合わせる tests/test_facets.py が見る(ここは骨格だけ)。
    """
    html = _index_html()
    body = html.split("function facetDefaultWhere(g) {")[1].split("\n}")[0]
    # default:true があればその値、ひとつも無ければeditorと同じく全値を選ぶ
    assert 'const defaults = values.filter((v) => v.default === true);' in body
    assert 'const selected = defaults.length ? defaults : values;' in body
    # 値の述語は where 優先、無ければ col=v の or(複数列は全列)を括弧でくくる
    assert '(v.where ? v.where : "(" + cols.map((c) => c + "=" + v.v).join(" or ") + ")")' in body
    # ファセットごとにも括弧をつけ、facetをまたぐと and(エディタの compileWhere と同形)
    assert 'clauses.push("(" + frags.join(" or ") + ")")' in body
    assert 'return clauses.join(" and ");' in body
    # facets の無いエントリは従来どおり entry.where
    assert 'if (!facets.length) return (e && e.where) || "";' in body


def test_index_html_parody_json_io_lives_in_the_editor_modal():
    """替え歌JSONの読み書きはエディタの全画面モーダルのフッタに置く。"""
    html = _index_html()
    modal = html.split('<div class="editor-modal" id="editor-frame-wrap"')[1]
    modal = modal.split("</div>\n\n<script>")[0]
    assert 'id="parody-io"' in modal
    assert 'id="editor" accept=".json"' in modal
    assert 'id="download-editor"' in modal
    # 生成時の #editor 参照・自動取り込みはそのまま(IDで引いているので位置は無関係)
    assert 'const f = $("editor").files[0];' in html


def test_index_html_transpose_is_greyed_out_under_auto_octave():
    """自動オクターブ調整ONのあいだは移調欄をdisabled+ミュート表示にする。"""
    html = _index_html()
    assert "function updateTransposeEnabled()" in html
    assert '$("transpose").disabled = auto;' in html
    assert '$("transpose-row").classList.toggle("is-disabled", auto);' in html
    assert '$("auto-octave").addEventListener("change", updateTransposeEnabled);' in html
    # 復元後にも状態を合わせる(保存済みのチェック状態を反映する)
    restore = html.split("async function doRestoreForm() {")[1]
    assert "updateTransposeEnabled();" in restore
    assert "#transpose-row.is-disabled { opacity: .5; }" in html
    # 値は消さない(OFFに戻せば前の半音数がそのまま使える)
    assert '$("transpose").value = ""' not in html


def test_index_html_layout_file_input_loads_a_custom_base():
    html = _index_html()
    assert 'id="layout-file" accept=".json"' in html
    # 読み込みの導線はプルダウンの隣の label ボタン(for= の標準挙動でピッカーが
    # 開くのでスクリプトの .click() が要らず、iOS Safari でも確実)。
    # ユーザー提供物の呼び名は単語リスト側の「自作リスト」と揃える
    assert '<label for="layout-file" class="le-file-btn"' in html
    assert ">インポート</label>" in html   # エクスポートと対の呼び名にする
    # 手入力欄を持つ base(=model)だけ「その他」を選択肢に足す
    assert 'if ($(base + "-other")) add(CHOICE_OTHER, "その他(手入力)…");' in html
    # 読み込んだ内容はユーザーの編集扱い(leToJson が leDirty を立てる)なので
    # 生成時に layout_json として送られる
    layout_file = html.split('$("layout-file").addEventListener("change"')[1]
    layout_file = layout_file.split("\n});")[0]
    # 読み込んだ内容はベースとして登録し(項目に残して選び直せる)、そのまま適用する
    assert "leFiles.set(f.name, parsed);" in layout_file
    assert "leAddFileOption(f.name);" in layout_file
    assert "leLoadBase(LE_FILE_PREFIX + f.name)" in layout_file
    assert "leToJson();" in html.split("function leLoadBase(key) {")[1].split("\n}")[0]
    # 来歴ガード: レイアウト名が入れ替わったら読み込んだファイルごと捨てる
    clear = html.split("function leClearLayout() {")[1].split("\n}")[0]
    assert '$("layout-file").value = "";' in clear


def test_index_html_layout_editor_has_element_and_fallback_tabs():
    html = _index_html()
    assert 'id="le-tab-elements"' in html and 'id="le-tab-fallback"' in html


def test_index_html_api_key_is_reachable_when_auth_is_required():
    """APIキー欄は詳細設定の先頭。キー待ちのときはそこまでスクロールする。"""
    html = _index_html()
    assert 'if (conf.auth_required && !apiKey()) {' in html
    assert '$("advanced").open = true;' in html
    assert '$("auth").scrollIntoView({ block: "center" });' in html


# ---- 替え歌エディタ: 画面全面のモーダルで開く ----


def test_index_html_editor_opens_as_fullscreen_modal():
    """エディタは詳細設定の中に展開せず、画面全面のモーダルで開く。"""
    html = _index_html()
    # モーダル本体は .wrap の外(bodyの直下)。position:fixed が親のスタッキング
    # 文脈に巻き込まれないようにするため、詳細設定より後ろに置く
    assert (
        '<div class="editor-modal" id="editor-frame-wrap" hidden role="dialog" '
        'aria-modal="true"' in html
    )
    assert html.index('id="editor-frame-wrap"') > html.index('id="public-footer"')
    # 全面に広げる(モバイルでも同じ)。iframeが残りの高さを全部使う
    assert ".editor-modal {\n    position: fixed; inset: 0; z-index: 50;" in html
    assert "flex: 1 1 auto; width: 100%; min-height: 0; border: 0;" in html


def test_index_html_editor_modal_uses_the_embedded_back_navigation():
    """親ヘッダーに閉じるボタンを重複させず、editor内の戻る導線を使う。"""
    html = _index_html()
    head = html.split('<div class="editor-modal-head">')[1].split("</div>")[0]
    assert "<button" not in head
    assert '$("editor-frame").focus();' in html
    # ヘッダは縮まず、下のiframeだけがスクロール領域になる
    assert ".editor-modal-head {\n    flex: 0 0 auto;" in html
    # 閉じても編集は生きていることと、残した導線をその場に書く
    assert "「動画作成に戻る」またはEscで閉じられます。" in html


def test_index_html_editor_modal_closes_with_escape():
    """Escで閉じる。iframeにフォーカスがあるときのために子documentにも付ける。"""
    html = _index_html()
    assert "function onEditorModalKeydown(ev) {" in html
    assert (
        'if (ev.key !== "Escape" || ev.defaultPrevented'
        ' || $("editor-frame-wrap").hidden) return;' in html
    )
    assert 'document.addEventListener("keydown", onEditorModalKeydown);' in html
    assert 'doc.addEventListener("keydown", onEditorModalKeydown);' in html


def test_index_html_editor_modal_locks_background_scroll():
    """モーダルを開いているあいだは裏のページをスクロールさせない。"""
    html = _index_html()
    assert "body.modal-open { overflow: hidden; }" in html
    assert 'document.body.classList.add("modal-open");' in html
    assert 'document.body.classList.remove("modal-open");' in html


# ---- 替え歌エディタ: 取り込み操作なしで最新の編集を使う(来歴ガード付き) ----


def test_index_html_editor_edits_are_used_without_back_navigation():
    """戻る操作をしなくても、編集内容が生成に使われる。"""
    html = _index_html()
    # 生成時に出どころを決める(#editor のファイル固定ではない)
    assert "const editorSrc = editorSourceForSubmit();" in html
    assert 'if (editorSrc.file) form.append("editor", editorSrc.file);' in html
    assert 'if ($("editor").files[0]) form.append("editor"' not in html


def test_index_html_editor_auto_import_requires_actual_edit():
    """開いただけ・眺めただけの内容は送らない(dirtyのときだけ)。"""
    import re

    html = _index_html()
    body = re.search(r"function liveEditorEdit\(\) \{.*?\n\}", html, re.S).group(0)
    # シード(=変換直後)と同じ指紋なら「編集していない」
    assert 'if (!sig || sig === meta.sig) return { state: "none" };' in body
    # シードはエディタを開くたびに記録する(変換直後・再編集の書き戻しの両方)
    assert "markEditorSeed(seed);" in html
    assert html.count("markEditorSeed(seed);") == 2
    # 指紋は編集で変わる部分だけを見る(paramの正規化や履歴で誤検知しない)
    assert (
        "return JSON.stringify([data.results, data.tokensList, data.unitsList]);" in html
    )


def test_index_html_editor_auto_import_checks_provenance():
    """来歴(曲×単語リスト×パラメータ)が食い違う編集は使わない。"""
    import re

    html = _index_html()
    prov = re.search(r"function editorProvenance\(\) \{.*?\n\}", html, re.S).group(0)
    assert "midiSampleId ? `sample:${midiSampleId}`" in prov
    assert "(midi ? `${midi.name}:${midi.size}` : \"\")" in prov
    assert 'wordlist: $("wordlist").value.trim(),' in prov
    assert 'where: $("where").value.trim(),' in prov
    assert "params: buildConvertParams()," in prov
    # 撤去した自作リスト(画像つき)の指紋キーは書かない。ただし古いシードを
    # 「別の入力」として弾けるよう、比較キーの並びからは外さない
    assert "customWordlist" not in prov
    assert (
        'const PROVENANCE_KEYS = '
        '["song", "wordlist", "customWordlist", "where", "params"];' in html
    )
    # 食い違えば stale。生成では使わず自動変換(convert)に落とす
    live = re.search(r"function liveEditorEdit\(\) \{.*?\n\}", html, re.S).group(0)
    assert (
        "if (!sameProvenance(from, editorProvenance())) return "
        '{ state: "stale", from, sig };' in live
    )
    src = re.search(r"function editorSourceForSubmit\(\) \{.*?\n\}", html, re.S).group(0)
    assert 'if (live.state === "ready") {' in src
    assert 'if (live.state !== "stale") return { file: f };' in src
    # 取り込み済みJSONがその来歴違いの編集そのものなら、それも使わない
    assert (
        "const sameAsFile = !!f && !!editorFileSig && editorFileSig === live.sig;" in src
    )
    assert "return { file: sameAsFile ? null : f, dropped: live.from };" in src
    # 自動取り込みぶんは来歴を確かめてあるので、単語リスト不一致の確認は挟まない
    assert "if (editorSrc.file && !editorSrc.live && parodyMismatch()" in html


# ---- 進捗表示: ステージ名とプログレスバー ----


def test_index_html_progress_uses_the_active_stage_plan():
    import re

    html = _index_html()
    plan = re.search(r"function stagePlan\(job\) \{.*?\n\}", html, re.S).group(0)
    # 走らないステージは分母に入れない(preview は変換・ミックス・動画を作らない)
    assert 'if (Number(p.preview || 0) > 0) return ["analyze", "synthesize"];' in plan
    # convert / import-editor は排他(parody_source で決まる)
    assert 'const parody = p.parody_source === "editor" ? "import-editor" : "convert";' in plan
    assert 'return ["analyze", parody, "synthesize", "mix", "video"];' in plan
    assert 'setJobStatus(`実行中: ${job.stage || "…"}${elapsed}`, `${label}${elapsed}`);' in html
    assert "setJobStatus(`歌唱合成${tail}`, `歌唱合成${tail}`);" in html


def test_index_html_stage_chips_match_the_step_count():
    """「生成の詳細」のステージchipsも、走らないステージは出さない。"""
    import re

    html = _index_html()
    body = re.search(r"function renderStages\(job\) \{.*?\n\}", html, re.S).group(0)
    assert "const plan = stagePlan(job);" in body
    assert "li.hidden = !plan.includes(name) && !doneNames.has(name);" in body
    # 枠内のバーの分母も同じ数え方にそろえる
    assert "const total = stagePlan(job).length || 6;" in html
