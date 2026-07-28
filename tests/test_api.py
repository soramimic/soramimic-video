"""APIサーバー(api.py)のテスト。

パイプライン本体はモックし、ジョブの受付→実行→動画取得の流れと
APIキー認証を確認する。NEUTRINO実行込みのE2Eは手動(serve)で行う。
"""

from __future__ import annotations

import json
import logging
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


def test_index_html_param_sliders():
    # 変換パラメータのスライダーが本家(external/soramimic 4443a7b frontend/src/app.js
    # の createSliderItem 呼び出し)と同じ範囲・既定値であることを固定する。
    import re

    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    expected = {
        "p-sound": ("0.1", "0.9", "0.1", "0.8"),  # 音の合わせ方(vowelRatio)
        "p-phrase": ("0", "8", "1", "1"),         # 文節の区切り
        "p-wordnum": ("0", "6", "1", "2"),        # 単語の長さ
    }
    for sid, (mn, mx, step, val) in expected.items():
        m = re.search(rf'<input type="range" id="{sid}"([^>]*)>', html)
        assert m, f"{sid} のスライダーが見つからない"
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        assert (attrs["min"], attrs["max"], attrs["step"], attrs["value"]) == (
            mn, mx, step, val,
        ), sid


def test_index_html_preset_mapping():
    # プリセットが本家 app.js の PRESETS(バランス/音そっくり/文節重視/長い単語、
    # 全プリセット r=0.8)と同一であることを固定する。既定はバランス。
    import re

    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    m = re.search(r'<select id="p-preset">(.*?)</select>', html, re.DOTALL)
    assert m, "p-preset のセレクトが見つからない"
    opts = re.findall(r'<option value="([^"]*)"', m.group(1))
    assert opts == ["バランス", "音そっくり", "文節重視", "長い単語", ""]
    assert re.search(r'<option value="バランス" selected>', m.group(1))

    expected = {
        "バランス": {"p-sound": "0.8", "p-phrase": "1", "p-wordnum": "2"},
        "音そっくり": {"p-sound": "0.8", "p-phrase": "0", "p-wordnum": "0"},
        "文節重視": {"p-sound": "0.8", "p-phrase": "8", "p-wordnum": "2"},
        "長い単語": {"p-sound": "0.8", "p-phrase": "1", "p-wordnum": "6"},
    }
    body = re.search(r"const PRESETS = \{(.*?)\n\};", html, re.DOTALL)
    assert body, "PRESETS が見つからない"
    for name, params in expected.items():
        row = re.search(rf'"{name}":\s*\{{([^}}]*)\}}', body.group(1))
        assert row, f"プリセット {name} が見つからない"
        got = dict(re.findall(r'"(p-[a-z]+)":\s*"([^"]*)"', row.group(1)))
        assert got == params, f"{name}: {got} != {params}"


def test_index_html_convert_params_new_model():
    # buildConvertParams が本家 getParam と同じ新パラメータモデルで送ることを固定する
    # (VOWEL_RATIO / VARIATION_COST=20r / SAME_PHRASE_BREAK_REWARD=0 /
    #  MID_PHRASE_BREAK_PENALTY=文節×20 / WORD_NUMBER_PENALTY=単語長×10)。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    for needle in (
        '"VOWEL_RATIO=" + r',
        '"VARIATION_COST=" + 20 * r',
        '"SAME_PHRASE_BREAK_REWARD=0"',
        '"MID_PHRASE_BREAK_PENALTY=" + Number($("p-phrase").value) * 20',
        '"WORD_NUMBER_PENALTY=" + Number($("p-wordnum").value) * 10',
    ):
        assert needle in html, needle
    # 旧モデルの掛け算ハック(#102で撤廃)を送っていないこと
    assert "SAME_VOWEL_REWARD" not in html
    assert "SAME_CONSONANT_REWARD" not in html


def test_index_html_note_length_weight_input():
    # soramimic-video 独自の「ノート長重視 α」の数値入力(0〜2 / 0.05刻み / 既定0.25)。
    # 既定0.25はタイブレーク運用(2曲のスイープ実験で単語長・短ノートへの
    # 副作用がほぼゼロのまま長ノートの母音一致だけ改善する点として選定)。
    # 本家準拠のプリセット・スライダーとは別枠なので PARAM_SLIDER_IDS には入れない。
    import re

    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    m = re.search(r'<input type="number" id="p-notelen"([^>]*)>', html)
    assert m, "p-notelen の数値入力が見つからない"
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    assert (attrs["min"], attrs["max"], attrs["step"], attrs["value"]) == (
        "0", "2", "0.05", "0.25",
    )
    # 本家由来のスライダー群には混ぜない(プリセット選択で上書きされないこと)
    assert 'const PARAM_SLIDER_IDS = ["p-sound", "p-phrase", "p-wordnum"];' in html
    # video独自であることがUI上わかる注記
    assert "soramimic-video独自" in html


def test_index_html_note_length_weight_sent_only_when_positive():
    # α>0 のときだけ NOTE_LENGTH_WEIGHT を convert_params に追記する
    # (0=既定では送らない → 本家と完全に同じパラメータになる)。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'const noteLen = Number($("p-notelen").value);' in html
    assert 'if (noteLen > 0) out.push("NOTE_LENGTH_WEIGHT=" + noteLen);' in html


def test_index_html_model_layout_use_select_not_datalist():
    # iOS Safari が datalist を表示しない問題への対応:
    # 歌声モデル(#model)・レイアウト(#layout)は select + 手入力 + 隠しvalue に置換。
    # 送信フィールド名(#model / #layout の hidden)は据え置きでAPI互換を保つ。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # datalist 方式は撤去(iOS Safari で候補が出ないため)
    assert "<datalist" not in html
    assert 'list="model-list"' not in html and 'list="layout-list"' not in html
    # 各コントロールの select・手入力・送信用hiddenが揃っている
    for base in ("model", "layout"):
        assert f'<select id="{base}-select">' in html
        assert f'<input type="text" id="{base}-other"' in html
        assert f'<input type="hidden" id="{base}"' in html
    # 「その他(手入力)」の選択肢が存在する
    assert 'その他(手入力)' in html


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


def test_index_html_builder_card_syncs_with_advanced():
    """トップのビルダーカードと詳細設定の曲・単語リストは双方向に同期する。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # カードのプルダウン
    assert '<select id="builder-sample"' in html
    assert '<select id="builder-wordlist"' in html
    # カード → 詳細設定(正本へ流して change を発火する)
    assert '$("sample-select").value = $("builder-sample").value;' in html
    # 詳細設定 → カード(写し戻し)
    assert (
        '$("sample-select").addEventListener("change", '
        "() => { syncBuilderValues(); schedulePreview(); });" in html
    )
    assert (
        '$("wordlist-select").addEventListener("change", '
        "() => { syncBuilderValues(); schedulePreview(); });" in html
    )
    # 確認モーダルは廃止し、🎲ランダムはカードの選択を差し替えるだけになった
    assert "lucky-modal" not in html
    assert '$("lucky").addEventListener("click", () => pickCombo(luckyRandomCombo()));' in html
    # 進捗・結果は畳んである詳細(ビルダーカードの中の控えめなテキストリンク)
    assert '<details class="sub-details" id="job-card" hidden>' in html


def test_index_html_job_card_is_collapsed_by_default():
    """ジョブの詳細は既定で畳み、エラー・中断のときだけ自動で開く。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # 独立したカードにはしない(「⚙️ 詳細設定」「過去のジョブ」と同じ重さだと目立ちすぎる)。
    # ビルダーカードの最下部に置く小さなテキストリンク(.sub-details)。
    assert '<details class="card" id="job-card"' not in html
    assert '<details class="sub-details" id="job-card" hidden>' in html
    assert "<summary>生成の詳細(ステージ・ログ)</summary>" in html
    # ログの入れ子の折りたたみは廃止(開けばそのままログまで見える)
    assert "<details><summary>ログ</summary>" not in html
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
    # 最下部(サムネ枠・案内文のあと)
    assert builder.index('id="job-card"') > builder.index('id="lucky-status"')
    # 控えめな置き場のぶん、エラー・中断で開いたときだけ警告色にして見つけやすくする
    assert '#job-card.attention > summary { color: var(--danger);' in html
    assert '$("job-card").classList.add("attention");' in html
    assert '$("job-card").classList.remove("attention");' in html


def test_index_html_restore_notice_is_inside_advanced():
    """「前回の入力を復元しました」は詳細設定の中(先頭)に置く。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    advanced = html.split('<details class="card" id="advanced">')[1]
    notice = advanced.split('id="restore-notice"')[0]
    # 詳細設定の中にあり、しかも中身(APIキー欄・曲の欄)より先頭
    assert 'id="restore-notice"' in advanced
    assert 'id="auth"' not in notice
    assert 'id="sample-select"' not in notice
    # ビルダーカードには残っていない
    builder = html.split('<section class="card" id="lucky-card">')[1].split("</section>")[0]
    assert "restore-notice" not in builder
    # 復元が1つでもあったときだけ出す既存の条件は維持
    assert 'if (any) $("restore-notice").hidden = false;' in html
    assert 'id="clear-form"' in advanced


def test_index_html_builder_card_is_bare():
    """ビルダーカードは見出し・説明文・例示ボタンを置かない(見れば分かる形にする)。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # 見出しと導入文は削除済み
    assert "まずはここから" not in html
    assert "lucky-lead" not in html
    # ラベルはプルダウンと同じ行。補足「何に空耳させるか」は title に退避する
    assert ".builder-selects .field { display: flex; align-items: center;" in html
    assert (
        '<label for="builder-wordlist" title="何に空耳させるか">単語リスト</label>' in html
    )
    # 例示コンボのボタンは廃止(組み立てるコードごと消えている)
    assert "renderLuckyCombos" not in html
    assert "lucky-combo" not in html
    # 🎲ランダムはカードの右上に小さく置くだけ。ただしアイコンだけだと気づかれない
    # ので、文字ラベルを添えたピルにする(絵文字は読み上げ対象から外す)
    assert '<div class="builder-topbar">' in html
    assert '<button type="button" id="lucky" class="btn-sm"' in html
    assert '<span class="lucky-icon" aria-hidden="true">🎲</span>ランダム</button>' in html
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
    # 進捗は同じ枠の中に重ねる。別の場所へスクロールさせない
    assert '<div class="builder-progress" id="builder-progress" hidden>' in html
    assert 'setBuilderState("running");' in html
    assert "$(\"job-card\").scrollIntoView" not in html
    # 中断は生成中も枠の中から押せる
    assert '$("builder-cancel").addEventListener("click", cancelJob);' in html
    # 完成したら同じ枠が動画プレイヤーになり、シェアは枠の直下に出る
    assert '<video id="builder-video" controls playsinline hidden></video>' in html
    assert "video.poster = qs(job.thumbnail_url);" in html
    assert '$("builder-share").innerHTML = SHARE_HTML;' in html
    # 選び直したら枠は新しいプレビューに戻る
    assert 'setBuilderState("preview");   // 完成した動画が出ていれば' in html


def test_index_html_has_no_separate_submit_button():
    """生成ボタンは枠のタップ1本に絞る(カード下の青い「🎬 動画を生成」は廃止)。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert '<button id="submit"' not in html
    assert "🎬 動画を生成" not in html
    assert '$("submit")' not in html          # setBusy などの参照も残さない
    assert ".btn-lg" not in html              # このボタン専用のスタイルも消す
    # 投入できるかは submit ボタンの disabled ではなくフラグで持つ
    assert "let submitBusy = false;" in html
    assert "submitBusy = busy;" in html
    assert "&& $(\"builder-loading\").hidden && !submitBusy);" in html
    # 詳細設定への導線と、曲が未セットのときの案内は残す
    assert "「⚙️ 詳細設定」から。" in html
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
    # ステージ名と経過秒(+全体の中での位置)を組み立てるのはこの1か所だけ
    assert "`${label}${step}${elapsed}`" in html
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


def test_config_has_wordlist_layouts(client):
    conf = client.get("/api/config").json()
    wl = conf["wordlist_layouts"]
    assert wl["scientist"] == "scientist_card"
    # 値はすべて組み込みレイアウト名(UIがそのまま#layoutに入れるため)
    assert set(wl.values()) <= set(conf["layouts"])


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
    job_id = submit(client, wordlist="stations", song_title=" うっせぇわ(確認用) ")
    body = wait_done(client, job_id)
    assert body["params"]["song_title"] == "うっせぇわ(確認用)"


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


def test_load_samples_tolerates_missing_or_broken_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(tmp_path / "nope"))
    assert api_mod.load_samples() == []
    assert api_mod.sample_entry("momiji") is None
    d = tmp_path / "broken"
    d.mkdir()
    (d / "samples.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv(api_mod.SAMPLES_DIR_ENV, str(d))
    assert api_mod.load_samples() == []


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


def test_index_html_share_buttons_are_separated():
    # 完成後の導線は「Xでポスト(必ずweb intent)」と「動画を保存(シェアシート
    # またはダウンロード)」の2本。1つのボタンにまとめ直さない。
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="share-x"' in html and 'id="share-save"' in html
    # Xボタンは intent を開くだけ(ファイル添付シェアは保存ボタン側)
    assert 'xBtn.addEventListener("click", () => openXIntent());' in html
    assert "navigator.share({ files: [file], text: SHARE_TEXT })" in html
    # シェアシートが使えない環境は動画のダウンロードに落とす
    assert "if (!navigator.share) return downloadVideo(videoUrl);" in html


def test_index_html_share_buttons_follow_the_actual_order():
    """実際の手順どおり「保存」が先(左)、「Xでポスト」が後(右)。案内文も同じ順。"""
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    share_html = html.split("const SHARE_HTML =")[1].split("// ファイル添付付き")[0]
    assert share_html.index('id="share-save"') < share_html.index('id="share-x"')
    # X風の黒いボタンのスタイルは維持
    assert 'id="share-x" class="btn-x btn-sm"' in share_html
    # 案内文は ①保存 → ②ポスト の順で読める
    assert "①「${SAVE_LABEL}」で端末に保存 → ②「Xでポスト」で投稿画面を開いて添付" in share_html


def test_index_html_no_server_host_line():
    # 接続先はページのURLと同じなので出さない(NEUTRINO未設定の警告だけ残す)
    html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "location.host" not in html
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
    assert 'if (state.layoutJson && state.layoutJsonFor === (state.layout || "")) {' in html


# ---- 詳細設定(#advanced)の並べ替え: 曲=MIDIアップロードが主役 ----


def _index_html() -> str:
    return (Path(api_mod.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )


def _advanced_html() -> str:
    return _index_html().split('<details class="card" id="advanced">')[1]


def test_index_html_advanced_song_field_leads_with_midi_upload():
    """詳細設定の「曲」はMIDIファイル選択が主役で、サンプル選択は従属UI。"""
    advanced = _advanced_html()
    # MIDIファイル入力は折りたたみの外(直に見える)
    assert '<label for="midi">曲(XF MIDIファイル)</label>' in advanced
    assert '<input type="file" id="midi" accept=".mid,.midi">' in advanced
    # 旧構成(サンプルが主役・MIDIが折りたたみ)は残っていない
    assert 'id="midi-details"' not in advanced
    assert "自分の曲を使う(XF MIDIファイル)" not in advanced
    # サンプル選択は「サンプルから選ぶ」の折りたたみへ格下げし、MIDIより後に置く
    assert '<details class="sub-details" id="sample-details">' in advanced
    assert "<summary>サンプルから選ぶ</summary>" in advanced
    assert advanced.index('id="midi"') < advanced.index('id="sample-select"')


def test_index_html_sample_select_stays_the_source_of_truth():
    """格下げしても #sample-select はDOMに残す(ビルダーカードとの同期の正本)。"""
    html = _index_html()
    assert '<select id="sample-select" aria-label="サンプル曲"></select>' in html
    # カード → 詳細設定 → applySample の経路はそのまま
    assert '$("sample-select").value = $("builder-sample").value;' in html
    assert (
        '$("sample-select").addEventListener("change", () => trackSample(applySample()));'
        in html
    )
    assert (
        '$("sample-select").addEventListener("change", '
        "() => { syncBuilderValues(); schedulePreview(); });" in html
    )
    # サンプルが取れない環境では折りたたみごと隠す(消えた #midi-details は参照しない)
    assert '$("sample-details").hidden = true;' in html
    assert '$("midi-details")' not in html


def test_index_html_lyrics_follows_the_song_and_is_recommended():
    """元歌詞は曲のすぐ後ろに置き、「推奨」と無いときの影響を書く。"""
    advanced = _advanced_html()
    assert '<label for="lyrics">元歌詞(推奨・字幕用)</label>' in advanced
    assert "元歌詞(任意・字幕用)" not in advanced
    # 曲(MIDI)の直後・単語リストより前(歌声より後ろだった旧位置から移動)
    assert (
        advanced.index('id="midi"')
        < advanced.index('id="lyrics"')
        < advanced.index('id="wordlist-select"')
        < advanced.index('id="synthesizer"')
    )
    # 無いときに何が起きるかを書く
    assert "字幕に元歌詞の行が出ません" in advanced


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
    # 旧構成(詳細設定の中に72vhのiframeをインライン展開)は残っていない
    assert "height: 72vh" not in html
    assert '$("editor-frame-wrap").scrollIntoView' not in html


def test_index_html_editor_modal_close_controls_are_pinned_to_the_head():
    """閉じる導線(取り込んで閉じる/閉じる)はモーダル上部に固定する。"""
    html = _index_html()
    head = html.split('<div class="editor-modal-head">')[1].split("</div>")[0]
    assert 'id="editor-import"' in head
    assert 'id="editor-close"' in head
    # ヘッダは縮まず、下のiframeだけがスクロール領域になる
    assert ".editor-modal-head {\n    flex: 0 0 auto;" in html
    # 閉じても編集は生きている(自動取り込み)ことをその場に書く
    assert "編集はエディタ内で自動保存され、閉じても生成に使われます(Escでも閉じます)。" in html


def test_index_html_editor_modal_closes_with_escape():
    """Escで閉じる。iframeにフォーカスがあるときのために子documentにも付ける。"""
    html = _index_html()
    assert "function onEditorModalKeydown(ev) {" in html
    assert (
        'if (ev.key !== "Escape" || $("editor-frame-wrap").hidden) return;' in html
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


def test_index_html_editor_edits_are_used_without_import_click():
    """「取り込んで閉じる」を押さなくても、編集内容が生成に使われる。"""
    html = _index_html()
    # 生成時に出どころを決める(#editor のファイル固定ではない)
    assert "const editorSrc = editorSourceForSubmit();" in html
    assert 'if (editorSrc.file) form.append("editor", editorSrc.file);' in html
    assert 'if ($("editor").files[0]) form.append("editor"' not in html
    # ボタンは残す(押したときの挙動は従来どおり)
    assert (
        '<button type="button" id="editor-import" class="btn-primary btn-sm">'
        "編集内容を取り込んで閉じる</button>" in html
    )


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
    assert "song: midi ? `${midi.name}:${midi.size}` : \"\"," in prov
    assert 'wordlist: $("wordlist").value.trim(),' in prov
    assert 'where: $("where").value.trim(),' in prov
    assert "params: buildConvertParams()," in prov
    assert 'const PROVENANCE_KEYS = ["song", "wordlist", "where", "params"];' in html
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
    # 落としたことはユーザーに分かる形で出す(詳細設定の中と生成ボタンの近くの両方)
    assert "if (editorSrc.dropped) {" in html
    assert '<p class="hint" id="editor-auto-status" hidden></p>' in html
    assert "別の入力(曲・単語リストなど)から" in html
    # 自動取り込みぶんは来歴を確かめてあるので、単語リスト不一致の確認は挟まない
    assert "if (editorSrc.file && !editorSrc.live && parodyMismatch()" in html


# ---- 進捗表示: 全体の中での位置(あと何段階か) ----


def test_index_html_progress_shows_step_position():
    """サムネ枠の進捗に「いま何段階目か」を出す。"""
    import re

    html = _index_html()
    plan = re.search(r"function stagePlan\(job\) \{.*?\n\}", html, re.S).group(0)
    # 走らないステージは分母に入れない(preview は変換・ミックス・動画を作らない)
    assert 'if (Number(p.preview || 0) > 0) return ["analyze", "synthesize"];' in plan
    # convert / import-editor は排他(parody_source で決まる)
    assert 'const parody = p.parody_source === "editor" ? "import-editor" : "convert";' in plan
    assert 'return ["analyze", parody, "synthesize", "mix", "video"];' in plan
    step = re.search(r"function stageStepText\(job\) \{.*?\n\}", html, re.S).group(0)
    assert "return i < 0 ? \"\" : ` ${i + 1}/${plan.length}`;" in step
    # 枠内の文言に添える(詳細側の文言は従来どおり)
    assert "const step = stageStepText(job);" in html
    assert (
        'setJobStatus(`実行中: ${job.stage || "…"}${elapsed}`, '
        "`${label}${step}${elapsed}`);" in html
    )
    assert "setJobStatus(`歌唱合成${tail}`, `歌唱合成${step}${tail}`);" in html


def test_index_html_stage_chips_match_the_step_count():
    """「生成の詳細」のステージchipsも、走らないステージは出さない。"""
    import re

    html = _index_html()
    body = re.search(r"function renderStages\(job\) \{.*?\n\}", html, re.S).group(0)
    assert "const plan = stagePlan(job);" in body
    assert "li.hidden = !plan.includes(name) && !doneNames.has(name);" in body
    # 枠内のバーの分母も同じ数え方にそろえる
    assert "const total = stagePlan(job).length || 6;" in html
