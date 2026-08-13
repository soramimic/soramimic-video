"""トップ画面(static/index.html)の構造テスト。

ブラウザを起動せずに守れる約束だけを見る。
- 詳細設定の「読まなくても操作できる説明」は data-info で ⓘ に畳まれている
- 状態・警告・エラーの動的メッセージは畳まれていない(常時表示のまま)
- エディタへの導線はカードの⚙ボタン1つで、保存済みの選択UIがある
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "src/soramimic_video/static/index.html"


class _Collector(HTMLParser):
    """開始タグを (tag, attrs dict) の列で集める。"""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _tags() -> list[tuple[str, dict[str, str | None]]]:
    p = _Collector()
    p.feed(INDEX.read_text(encoding="utf-8"))
    return p.tags


def _markup() -> str:
    """<script> より前(=画面のマークアップ)だけを返す。"""
    text = INDEX.read_text(encoding="utf-8")
    return text[: text.index("<script>")]


def test_info_hints_cover_the_static_explanations():
    ids = {a.get("id") for tag, a in _tags() if "data-info" in a}
    # 各グループの1行説明(opt-group-lead)はすべて ⓘ の中
    leads = [a for tag, a in _tags() if "opt-group-lead" in (a.get("class") or "")]
    assert len(leads) == 3
    assert all("data-info" in a for a in leads)
    # 動的メッセージ(状態・警告・エラー)は畳まない
    always_visible = {
        "midi-restore-hint",    # 前回のファイルを復元: ...
        "layout-file-msg",      # 読み込みました: xxx.json / 読めませんでした
        "midi-error",           # 歌詞なしMIDIの拒否
        "lyrics-midi-warn",     # 元歌詞とMIDI歌詞の食い違い
        "sample-status",        # ✓ ...をセットしました
        "parody-status",        # 編集済みの替え歌を使用します
        "voicevox-credit",
        "auto-octave-hint",
        "le-cue-hint",
        "editor-resume-note",
    }
    assert not (ids & always_visible)


def test_head_has_complete_public_ogp_metadata():
    """LINE等が操作UIを説明文にせず、専用画像と説明を取得できる。"""
    text = INDEX.read_text(encoding="utf-8")
    head = text[: text.index("</head>")]
    assert "<title>Soramimic | 替え歌動画メーカー</title>" in head

    metas = [attrs for tag, attrs in _tags() if tag == "meta"]
    by_name = {attrs["name"]: attrs.get("content") for attrs in metas if attrs.get("name")}
    by_property = {
        attrs["property"]: attrs.get("content")
        for attrs in metas
        if attrs.get("property")
    }
    description = "曲と単語リストを選ぶだけ。空耳で置き換えた替え歌動画を作れます。"
    image_url = "https://video.soramimic.com/ogp-soramimic-v5.png"
    assert by_name["description"] == description
    assert by_name["twitter:card"] == "summary_large_image"
    assert by_name["twitter:title"] == "Soramimic | 替え歌動画メーカー"
    assert by_name["twitter:description"] == description
    assert by_name["twitter:image"] == image_url
    assert by_property == {
        "og:type": "website",
        "og:site_name": "Soramimic",
        "og:title": "Soramimic | 替え歌動画メーカー",
        "og:description": description,
        "og:url": "https://video.soramimic.com/",
        "og:locale": "ja_JP",
        "og:image": image_url,
        "og:image:type": "image/png",
        "og:image:width": "1200",
        "og:image:height": "630",
        "og:image:alt": "Soramimic — 曲と単語リストから替え歌動画を作成",
    }
    links = [attrs for tag, attrs in _tags() if tag == "link"]
    assert {attrs.get("rel"): attrs.get("href") for attrs in links}["canonical"] == (
        "https://video.soramimic.com/"
    )


def test_header_uses_versioned_soramimic_video_logo():
    text = INDEX.read_text(encoding="utf-8")
    assert 'class="brand-lockup"' in text
    assert 'class="brand-logo"' in text
    assert 'src="/logo-soramimic-video-v2.png"' in text
    assert 'alt="Soramimic video"' in text


def test_static_hints_in_advanced_are_all_folded():
    """詳細設定の中に、ⓘ にもidにも属さない裸の説明文を残さない。"""
    markup = _markup()
    body = markup[markup.index('<details class="card" id="advanced">') :]
    body = body[: body.index("</details>\n\n<details class=\"card\" id=\"history\"")]
    for m in re.finditer(r"<p class=\"hint[^\"]*\"([^>]*)>", body):
        attrs = m.group(1)
        assert "data-info" in attrs or "id=" in attrs, m.group(0)


def test_editor_entry_point_is_a_single_button():
    ids = [a.get("id") for tag, a in _tags() if tag == "button" and a.get("id")]
    # エディタを開く導線はこれ1つ(モーダル側の閉じる/取り込みは別物)
    assert ids.count("builder-edit") == 1
    # 保存済みがあるときの2択+右上の×。「やめる」の文字ボタンは置かない
    # (×・背景クリック・Escに吸収)
    for btn in ("editor-resume-continue", "editor-resume-regen", "editor-resume-close"):
        assert btn in ids


def test_simple_ui_hides_advanced_and_filters_wordlists():
    """初回公開版は詳細設定を隠し、サーバーが返したカタログだけ出す。"""
    script = _script()
    assert '$("advanced").hidden = simpleMode;' in script
    assert "loadWordlistSelect(conf.wordlist_config ?? conf.editor)" in script
    assert "const allowed = new Set(launchWordlists);" in script
    assert "return allowed.has(name);" in script
    defaults = _function_body(script, "function applySimpleDefaults()")
    assert '$("synthesizer").value = "voicevox"' in defaults
    assert '$("auto-octave").checked = true' in defaults
    assert '$("transpose").value = "0"' in defaults
    assert 'wordlistLayouts[$("wordlist").value.trim()]' in defaults


def test_simple_ui_hides_the_irrelevant_song_length_limit():
    """同梱曲しか選べない初回公開版では、持ち込みMIDI向けの曲長案内を出さない。"""
    body = _function_body(_script(), "function setupPublicMode(conf)")
    assert "if (conf.max_song_seconds && !simpleMode)" in body
    # 日次上限や検証アカウントの案内まで一緒に消さない。
    assert "if (conf.quota_exempt === true)" in body
    assert "else if (conf.daily_quota)" in body
    assert "混雑時は順番待ちになります" in body
    assert "updatePublicCredit();" in body


def test_editor_resume_panel_is_hidden_by_default():
    # カード内のパネルからモーダルに変えた(インライン展開だとサムネ枠が押し下がる)
    panel = next(a for tag, a in _tags() if a.get("id") == "editor-resume")
    assert "hidden" in panel
    assert panel.get("role") == "dialog"
    assert panel.get("aria-modal") == "true"
    assert panel.get("aria-labelledby") == "editor-resume-title"


def test_editor_resume_dialog_only_asks_when_provenance_is_stale():
    """⚙を押す期待は「押したら編集画面」。来歴が一致していれば聞かずに再開し、
    モーダルが出るのは引き継ぐ/捨てるを本人にしか決められないstaleのときだけ。"""
    body = _function_body(_script(), "async function openEditorFlow()")
    assert "if (!saved.stale) { await resumeEditor(); return; }" in body
    markup = _markup()
    assert '<button type="button" id="editor-resume-regen" class="btn-primary btn-sm">' in markup


def test_parody_status_does_not_repeat_the_same_wordlist_name():
    """絞り込みだけが違うとき、同じリスト名を2回並べる意味不明な警告にしない。"""
    body = _function_body(_script(), "function renderParodyStatus()")
    assert "選択中の絞り込みは使われません" in body


def _script() -> str:
    """<script> 以降(=画面のふるまい)だけを返す。"""
    text = INDEX.read_text(encoding="utf-8")
    return text[text.index("<script>") :]


def _function_body(script: str, head: str) -> str:
    """head で始まる関数の中身(次の行頭 } まで)を返す。"""
    body = script[script.index(head) :]
    return body[: body.index("\n}\n")]


def test_submit_takes_the_midi_from_the_current_song_choice():
    """投入は「いま選ばれている曲」のMIDIで行う(古い・別の曲のMIDIで生成しない)。"""
    script = _script()
    submit = _function_body(script, "async function submitJob(")
    # 曲の突き合わせ・取り直しは FormData を組み立てるより前に済ませる
    assert (submit.index("ensureSelectedSampleMidi()")
            < submit.index('form.append("midi"'))
    guard = _function_body(script, "async function ensureSelectedSampleMidi(")
    # 来歴(midiSampleId)が選択と揃わなければ投入させない
    assert 'midiSampleId === $("sample-select").value' in guard
    assert "return false;" in guard
    # 曲名も同じ来歴から決める(MIDIは別の曲・曲名は選んだ曲、を作らない)
    assert "midiSampleId" in _function_body(script, "function songTitleOf(file)")


def test_turnstile_interaction_scrolls_to_inline_prompt():
    """追加操作が必要なときはカード内の確認欄まで自動スクロールする。"""
    markup = _markup()
    script = _script()
    assert 'id="turnstile-title">合成前に確認してください' in markup
    assert 'class="turnstile-copy" role="status" aria-live="polite"' in markup
    assert 'id="turnstile-cancel"' not in markup
    assert 'turnstile-ui' not in script
    assert 'turnstile-overlay' not in markup
    prompt = _function_body(script, "function showTurnstilePrompt()")
    assert 'wrap.scrollIntoView({ behavior: "smooth", block: "center" })' in prompt


def test_submit_never_posts_without_a_turnstile_token():
    """確認待ちの画面を生成進捗に見せず、失敗時は空tokenをAPIへ送らない。"""
    submit = _function_body(_script(), "async function submitJob(")
    verification = 'if (!await ensureTurnstileToken()) {'
    assert verification in submit
    assert submit.index(verification) < submit.index("const form = new FormData()")
    guard = submit[submit.index(verification) : submit.index("const form = new FormData()")]
    assert "return;" in guard
    assert "showProgress();" not in guard
    assert submit.index("if (submitBusy) return;") < submit.index(
        "if (samplePending) await samplePending;"
    )
    ensure = _function_body(_script(), "async function ensureTurnstileToken(")
    assert "!turnstileFailed && !!turnstileToken()" in ensure
    assert "rebuildTurnstileWidget()" in ensure
    assert "if (turnstileWidget === null && window.turnstile) return false;" in ensure
    assert "timeoutMs = 120000" in ensure


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_turnstile_errors_rebuild_widget():
    """失敗・2分timeoutのwidgetは世代ごと破棄し、古いcallbackを無効化する。"""
    ensure = _function_body(_script(), "async function ensureTurnstileToken(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let token = "";
        let turnstileSiteKey = "site";
        let turnstileWidget = {{}};
        let turnstileNeedsInteraction = false;
        let turnstileWaiting = false;
        let turnstileFailed = false;
        let rebuilds = 0;
        let now = 0;
        let waitMode = "error";
        let window = {{turnstile: {{execute() {{}} }}}};
        function turnstileToken() {{ return token; }}
        function showTurnstilePrompt() {{}}
        function hideTurnstilePrompt() {{}}
        function rebuildTurnstileWidget() {{ rebuilds += 1; token = ""; }}
        Date.now = () => now;
        global.setTimeout = (fn) => {{
          if (waitMode === "error") turnstileFailed = true;
          else now += 200;
          fn();
          return 1;
        }};
        {ensure}
        (async () => {{
          assert.equal(await ensureTurnstileToken(100), false);
          assert.equal(rebuilds, 1, "an errored widget must rebuild before the next attempt");
          waitMode = "timeout";
          now = 0;
          assert.equal(await ensureTurnstileToken(100), false);
          assert.equal(rebuilds, 2, "a timed-out widget must also rebuild before retry");
        }})().catch((err) => {{ console.error(err); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True)


def test_turnstile_old_widget_callbacks_are_ignored_after_rebuild():
    """破棄済みwidgetの遅延successが次回投入用tokenとして復活しない。"""
    script = _script()
    render = _function_body(script, "function renderTurnstileWidget()")
    rebuild = _function_body(script, "function rebuildTurnstileWidget()")
    assert "const epoch = ++turnstileEpoch;" in render
    assert "if (epoch !== turnstileEpoch) return;" in render
    assert "turnstileEpoch += 1;" in rebuild
    assert "window.turnstile.remove(oldWidget)" in rebuild
    assert '$("turnstile-widget").replaceChildren();' in rebuild
    assert 'console.warn("Turnstileの再描画に失敗しました", err);' in rebuild


def test_random_button_always_changes_both_choices():
    """ランダム抽選は現在の曲と現在の単語リストを同時に選び直す。"""
    body = _function_body(_script(), "function luckyRandomCombo()")
    assert "luckyCandidatePools()" in body
    # 片方でも別候補がなければ、現在値を再選択して条件を破らない
    assert 'if (!samples.length || !alternatives.length) return null;' in body
    assert 'pickRandom(samples)' in body
    assert 'pickRandom(pool)' in body


def test_random_button_is_disabled_until_both_choices_can_change():
    """候補不足や初期化中に、押せるのに何も変わらない状態を作らない。"""
    lucky = next(a for tag, a in _tags() if a.get("id") == "lucky")
    assert "disabled" in lucky
    pools = _function_body(_script(), "function luckyCandidatePools()")
    assert 'o.value !== currentSampleId' in pools
    assert 'nameOf(o) !== currentWordlist' in pools
    availability = _function_body(_script(), "function syncLuckyAvailability()")
    assert '$("lucky").disabled = !samples.length || !alternatives.length;' in availability
    sync = _function_body(_script(), "function syncBuilderValues()")
    assert "syncLuckyAvailability();" in sync


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_desktop_video_has_separate_download_and_share_buttons():
    """MacなどPCでは直接保存と共有シートを別の明示操作にする。"""
    script = _script()
    share = script[script.index("const SHARE_TEXT =") :]
    share = share[: share.index("// ジョブを投入できない状態か")]
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let elements = {{}};
        let downloads = 0;
        let shareCalls = [];
        let fetchCalls = 0;
        const navigator = {{
          platform: "MacIntel", userAgent: "Chrome", maxTouchPoints: 0,
          share(data) {{ shareCalls.push(data); return Promise.resolve(); }}
        }};
        class File {{
          constructor(parts, name, options) {{
            this.parts = parts; this.name = name; this.type = options.type;
          }}
        }}
        const location = {{ origin: "https://video.example" }};
        const URL = {{
          createObjectURL(file) {{ return "blob:desktop/" + file.name; }},
          revokeObjectURL() {{}}
        }};
        const $ = (id) => elements[id] || null;
        const qs = (value) => value;
        const headers = () => ({{}});
        const fetch = async () => {{
          fetchCalls += 1;
          return {{ ok: true, blob: async () => ({{ type: "video/mp4" }}) }};
        }};
        const document = {{
          body: {{ appendChild() {{}} }},
          createElement() {{
            return {{ click() {{ downloads += 1; }}, remove() {{}} }};
          }}
        }};
        const showBuilderMsg = () => {{}};
        {share}

        function button() {{
          return {{
            disabled: false, hidden: false, textContent: "", handlers: {{}},
            addEventListener(type, fn) {{ this.handlers[type] = fn; }}
          }};
        }}

        function videoElement() {{
          return {{
            src: "", handlers: {{}}, load() {{}}, pause() {{}},
            removeAttribute() {{}},
            addEventListener(type, fn) {{ this.handlers[type] = fn; }},
            removeEventListener(type, fn) {{
              if (this.handlers[type] === fn) delete this.handlers[type];
            }}
          }};
        }}

        (async () => {{
          assert.equal(supportsVideoFileShare(), true,
            "the desktop fixture intentionally supports Web Share");
          assert.equal(FILE_SHARE_SUPPORTED, true);
          assert.equal(DESKTOP_SHARE_UI, true);
          assert.match(SHARE_HTML, /id="download-video"/);
          assert.match(SHARE_HTML, /id="share-save"/);
          elements = {{
            "download-video": button(), "share-save": button(),
            "share-hint": {{ textContent: "" }}
          }};
          bindShare("/video.mp4");
          await prepareVideoShare("/video.mp4", videoElement());
          assert.equal(fetchCalls, 1, "desktop prepares the File before one-click sharing");
          assert.equal(elements["share-save"].textContent, "共有");
          assert.equal(elements["share-save"].hidden, false);

          elements["download-video"].handlers.click();
          assert.equal(downloads, 1);
          assert.equal(shareCalls.length, 0, "download must not open the share sheet");

          elements["share-save"].handlers.click();
          await Promise.resolve();
          assert.equal(downloads, 1, "share must not start a download");
          assert.equal(shareCalls.length, 1);
          assert.equal(shareCalls[0].files[0].name, "video.mp4");
          assert.equal(shareCalls[0].text.includes("#Soramimic"), true);
          assert.equal(shareCalls[0].text.includes("#そらみみっく"), true);

          navigator.share = () => Promise.reject({{ name: "NotAllowedError" }});
          elements["share-save"].handlers.click();
          await new Promise((resolve) => setImmediate(resolve));
          assert.equal(elements["share-save"].hidden, true,
            "a rejected desktop share leaves the separate download available");
          assert.equal(downloads, 1);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_video_share_and_playback_share_one_prepared_file_before_click():
    """再生と共有でMP4を二重取得せず、iOSの一時的な操作権限を失わない。

    共有後もPromiseが完了しないことがあるので、イベントを実行して状態も固定する。
    """
    script = _script()
    share = script[script.index("const SHARE_TEXT =") :]
    share = share[: share.index("// ジョブを投入できない状態か")]
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let elements = {{}};
        let fetchCalls = 0;
        let downloads = 0;
        let messages = [];
        let shareCalls = [];
        let activation = false;
        let fetchSignal = null;
        let fetchOk = true;
        let revoked = [];
        let objectUrlFile = null;
        const navigator = {{
          platform: "iPhone", userAgent: "CriOS", maxTouchPoints: 1,
          share(data) {{
            assert.equal(activation, true, "share must run directly in click activation");
            shareCalls.push(data);
            return new Promise(() => {{}});
          }}
        }};
        class File {{
          constructor(parts, name, options) {{
            this.parts = parts; this.name = name; this.type = options.type;
          }}
        }}
        const location = {{ origin: "https://video.example" }};
        const URL = {{
          createObjectURL(file) {{
            objectUrlFile = file;
            return "blob:shared-video/" + file.name;
          }},
          revokeObjectURL(url) {{ revoked.push(url); }}
        }};
        const $ = (id) => elements[id] || null;
        const qs = (value) => value;
        const headers = () => ({{}});
        const fetch = async (_url, options) => {{
          fetchCalls += 1;
          fetchSignal = options.signal;
          return {{ ok: fetchOk, blob: async () => ({{ type: "video/mp4" }}) }};
        }};
        const document = {{
          body: {{ appendChild() {{}} }},
          createElement() {{
            return {{ click() {{ downloads += 1; }}, remove() {{}} }};
          }}
        }};
        const showBuilderMsg = (message) => messages.push(message);
        {share}

        function button() {{
          return {{
            disabled: false, textContent: "", handlers: {{}},
            addEventListener(type, fn) {{ this.handlers[type] = fn; }}
          }};
        }}

        function videoElement() {{
          return {{
            src: "", loadCalls: 0, pauseCalls: 0, handlers: {{}},
            load() {{ this.loadCalls += 1; }},
            pause() {{ this.pauseCalls += 1; }},
            removeAttribute(name) {{ if (name === "src") this.src = ""; }},
            addEventListener(type, fn) {{ this.handlers[type] = fn; }},
            removeEventListener(type, fn) {{
              if (this.handlers[type] === fn) delete this.handlers[type];
            }},
            emit(type) {{ if (this.handlers[type]) this.handlers[type](); }}
          }};
        }}

        (async () => {{
          assert.equal(FILE_SHARE_SUPPORTED, true, "iPhone keeps native file sharing");
          assert.equal(DESKTOP_SHARE_UI, false,
            "iPhone keeps the combined save and share button");
          assert.doesNotMatch(SHARE_HTML, /id="download-video"/,
            "mobile must not add a second download button");

          elements = {{ "share-save": button(), "share-hint": {{ textContent: "" }} }};
          bindShare("/video.mp4");
          const video = videoElement();
          await prepareVideoShare("/video.mp4", video);
          assert.equal(fetchCalls, 1);
          assert.ok(fetchSignal, "share preparation must be abortable");
          assert.equal(video.src, "blob:shared-video/video.mp4");
          assert.equal(video.loadCalls, 1);
          assert.equal(elements["share-save"].textContent, "動画を保存・共有");
          assert.equal(elements["share-save"].disabled, false);

          // 最初のタップではfetchを挟まず、操作権限内でshareする。
          activation = true;
          elements["share-save"].handlers.click();
          activation = false;
          await Promise.resolve();
          assert.equal(fetchCalls, 1, "click must not fetch the video again");
          assert.equal(shareCalls.length, 1);
          assert.equal(shareCalls[0].files[0], objectUrlFile,
            "playback and share must use the same File");
          assert.deepEqual(Object.keys(shareCalls[0]), ["files", "text"]);
          assert.equal(shareCalls[0].text.includes("#Soramimic #そらみみっく\\n"), true);
          assert.equal(shareCalls[0].text.includes("#soramimic"), false);
          assert.equal(shareCalls[0].text.endsWith("https://video.example"), true);
          assert.equal(elements["share-save"].disabled, false,
            "a pending iOS share promise must not disable later taps");
          assert.equal(downloads, 0);

          navigator.share = () => Promise.reject({{ name: "AbortError" }});
          activation = true;
          elements["share-save"].handlers.click();
          activation = false;
          await new Promise((resolve) => setImmediate(resolve));
          assert.equal(downloads, 0, "cancel must not start a download");
          assert.equal(elements["share-save"].textContent, "動画を保存・共有");

          navigator.share = () => Promise.reject({{ name: "NotAllowedError" }});
          activation = true;
          elements["share-save"].handlers.click();
          activation = false;
          await new Promise((resolve) => setImmediate(resolve));
          assert.equal(downloads, 0, "share failure must not auto-download");
          assert.equal(elements["share-save"].textContent, "動画をダウンロード");
          assert.match(messages.at(-1), /共有メニュー/);

          elements["share-save"].handlers.click();
          assert.equal(downloads, 1, "the explicit save-mode click downloads");

          resetVideoSharePreparation();
          assert.deepEqual(revoked, ["blob:shared-video/video.mp4"]);

          // Blob URLの非同期media errorでは再生だけdirect URLへ戻し、File共有は残す。
          elements = {{ "share-save": button(), "share-hint": {{ textContent: "" }} }};
          const mediaFallbackVideo = videoElement();
          fetchOk = true;
          await prepareVideoShare("/video.mp4", mediaFallbackVideo);
          const fallbackShareFile = preparedVideoShare.file;
          mediaFallbackVideo.emit("error");
          assert.equal(mediaFallbackVideo.src, "/video.mp4");
          assert.equal(mediaFallbackVideo.handlers.error, undefined,
            "direct playback must not recursively install the Blob fallback");
          assert.equal(preparedVideoShare.file, fallbackShareFile,
            "media fallback must preserve one-tap file sharing");
          assert.deepEqual(revoked,
            ["blob:shared-video/video.mp4", "blob:shared-video/video.mp4"]);
          resetVideoSharePreparation();

          elements = {{ "share-save": button(), "share-hint": {{ textContent: "" }} }};
          fetchOk = false;
          const fallbackVideo = videoElement();
          await prepareVideoShare("/video.mp4", fallbackVideo);
          assert.equal(fallbackVideo.src, "/video.mp4",
            "failed share preparation must preserve direct playback");
          assert.equal(fallbackVideo.loadCalls, 2,
            "fallback detaches a possible Blob source before loading the direct URL");
          assert.equal(elements["share-save"].textContent, "動画をダウンロード");
          assert.equal(downloads, 1, "preparation failure must not auto-download");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_completed_video_prepares_one_shared_playback_file_and_reset_aborts_fetch():
    """完成時に共有Fileを準備して再生にも共用し、離脱時は取得を止める。"""
    script = _script()
    shown = _function_body(script, "function showBuilderVideo(job)")
    assert "prepareVideoShare(job.video_url, video)" in shown
    assert "if (!FILE_SHARE_SUPPORTED) video.src = qs(job.video_url);" in shown
    clicked = _function_body(script, "function bindShare(videoUrl)")
    assert "prepareVideoShare" not in clicked
    reset = _function_body(script, "function resetVideoSharePreparation()")
    assert "sharePreparationAbort.abort()" in reset
    assert "revokeSharePlaybackUrl()" in reset
    state = _function_body(script, "function setBuilderState(state)")
    assert state.index('video.removeAttribute("src")') < state.index(
        "resetVideoSharePreparation()"
    )


def test_editor_opens_from_the_setup_screen():
    """⚙はサーバーに変換させず(convert=0)、セットアップ画面から開く。

    いま選ばれている単語リスト・絞り込み・変換パラメータ・曲名は初期値として
    送る(エディタのセットアップ画面がそれを初期選択にする)。単語リストが
    空でも呼べる——リストはセットアップ画面で選べるため。
    """
    script = _script()
    body = _function_body(script, "async function convertAndOpenEditor()")
    assert 'form.append("convert", "0")' in body
    assert 'form.append("wordlist", $("wordlist").value.trim())' in body
    assert 'form.append("where", $("where").value.trim())' in body
    # 変換パラメータも初期値として送る(エディタの初期表示と生成条件を揃える)
    assert 'form.append("convert_params", buildConvertParams())' in body
    # セットアップ画面に出す曲名(投入時と同じ来歴から決める)
    assert 'form.append("song_title", songTitleOf(midi))' in body


def test_regenerate_button_says_it_starts_from_the_setup_screen():
    """「再生成」は実態がセットアップ画面からのやり直しなので、そう名乗る。"""
    markup = _markup()
    assert "設定から作り直す" in markup
    # 説明文も揃える(押すとどこから始まるかが分かること)
    script = _script()
    note = _function_body(script, "async function openEditorFlow()")
    assert "「設定から作り直す」" in note


def test_setup_seed_has_no_results_so_viewing_alone_is_not_an_edit():
    """未変換シードは results 等を持たないので、来歴ガードは「編集なし」と見る。

    セットアップ画面で変換されると results が入って指紋が変わり、
    その結果が(取り込み操作なしで)そのまま生成に使われる。
    """
    script = _script()
    sig = _function_body(script, "function editorContentSig(data)")
    assert "data.results" in sig and "data.tokensList" in sig and "data.unitsList" in sig
    live = _function_body(script, "function liveEditorEdit()")
    assert "sig === meta.sig" in live and 'state: "none"' in live


def test_restored_sample_data_is_refetched_unless_lyrics_were_edited():
    """復元サンプルのMIDIと未編集歌詞は取り直し、手編集歌詞だけ残す。"""
    init = _function_body(_script(), "async function initBuilder()")
    assert "sampleLyricsId === restoredId" in init
    assert "sampleLyricsBaseline !== null" in init
    assert '$("lyrics").value !== sampleLyricsBaseline' in init
    assert "applySample({ midiOnly: editedLyrics })" in init


def test_sample_lyrics_baseline_is_saved_and_restored():
    """標準歌詞の更新と手編集の保護はリロードを跨いで判定できる。"""
    script = _script()
    apply_sample = _function_body(script, "async function applySample(")
    assert "sampleLyricsId = sid;" in apply_sample
    assert "sampleLyricsBaseline = sampleLyrics;" in apply_sample
    save = _function_body(script, "function saveForm()")
    assert "sampleLyricsId," in save
    assert "sampleLyricsBaseline," in save
    restore = _function_body(script, "async function doRestoreForm()")
    assert 'sampleLyricsId = state.sampleLyricsId || "";' in restore
    assert 'typeof state.sampleLyricsBaseline === "string"' in restore


def test_sample_midi_fetch_bypasses_the_browser_cache():
    """同梱サンプルは作り直されるので、キャッシュ済みの古い版を使わない。"""
    apply_sample = _function_body(_script(), "async function applySample(")
    assert 'cache: "no-store"' in apply_sample


def test_info_toggle_sets_aria_state():
    """ⓘ ボタンは aria-expanded / aria-controls を持つ(setupInfoHints)。"""
    script = INDEX.read_text(encoding="utf-8")
    for needle in ('setAttribute("aria-expanded"', 'setAttribute("aria-controls"',
                   'setAttribute("aria-label"'):
        assert needle in script


def test_info_hint_opens_as_floating_popover():
    """説明はその場に展開せず、ⓘ の近くに重ねるフロート表示にする。

    その場に開くと周りのフォームが押し下がるので、position:fixed +
    JS の座標計算(placeInfoPop)にしている。閉じる導線は ⓘ 再タップ・
    外側タップ・Esc の3つ。
    """
    text = INDEX.read_text(encoding="utf-8")
    rule = text[text.index(".hint[data-info] {"):]
    rule = rule[: rule.index("}")]
    assert "position: fixed" in rule
    assert "max-width: min(" in rule           # スマホで画面幅を超えない
    for needle in ("function placeInfoPop", "function openInfoPop", "function closeInfoPop",
                   'ev.key !== "Escape"'):
        assert needle in text


def test_editor_wordlist_is_written_back_to_the_form():
    """エディタの⚙で単語リストを変えたら、親の正本(#wordlist / #where)へ書き戻す。

    書き戻さないと、カードのプルダウン・サムネプレビュー・生成時の単語画像/
    レイアウトの解決が古いリストのままになる(どれも正本を見ている)。
    """
    script = _script()
    body = _function_body(script, "function applyEditorWordlist()")
    # 名前付きリスト(filepath) → stem を既存の選択経路へ流し、where はエディタ優先
    assert r'String(w.filepath || "").replace(/.*\//, "").replace(/\.csv$/, "")' in body
    assert "selectWordlist(name);" in body
    assert "if (wantWhere) setWhere(wantWhere);" in body
    # 名前で引けないリスト(ORIGINAL / 古い custom:<sid>)は名前も絞り込みも空にする
    assert 'const own = value === "ORIGINAL" || value.startsWith("custom:");' in body
    assert 'if (!sel.hidden) sel.selectedIndex = -1;' in body
    assert '$("wordlist").value = "";' in body
    assert 'setWhere("");' in body
    # 書き戻しで来歴が食い違って編集が捨てられないよう、シードの来歴も付け替える
    assert "adoptEditorSeedProvenance();" in body
    # 変わっていなければ何もしない(毎周期で来歴を付け替えると曲の差し替えを
    # 見逃してしまう)
    assert "return;   // すでに一致している" in body
    # 拾うのは監視ポーリングと「閉じる」の2経路
    assert "if (on) editorWatchTimer = setInterval(syncEditorSession, 1500);" in script
    close = _function_body(script, "function closeEditor()")
    assert "syncEditorSession();" in close


def test_editor_lyrics_are_written_back_to_the_form():
    """エディタの中で元歌詞を直したら、親の正本(#lyrics)へ書き戻す。

    元歌詞は生成時に lyrics.txt として送られ、字幕の元歌詞になる。書き戻さない
    と、エディタで直した元歌詞が動画に反映されない(将来この欄を画面から外して
    も、hidden の正本としてそのまま機能する)。
    """
    script = _script()
    body = _function_body(script, "function applyEditorLyrics()")
    assert 'if (!data || typeof data.lyrics !== "string") return;' in body
    # 変わっていなければ何もしない(毎周期で change を撒かない)
    assert 'if (data.lyrics === $("lyrics").value) return;' in body
    assert '$("lyrics").value = data.lyrics;' in body
    assert '$("lyrics").dispatchEvent(new Event("change", { bubbles: true }));' in body
    # 単語リストの書き戻しと同じ経路(ポーリングと「閉じる」)で拾う
    sync = _function_body(script, "function syncEditorSession()")
    assert "applyEditorLyrics();" in sync
    # 来歴(editorProvenance)は元歌詞を見ないので、書き戻しで編集が捨てられない
    prov = _function_body(script, "function editorProvenance()")
    assert "lyrics" not in prov
    # 元歌詞はエディタへのシードにも載る(サーバーが入れるので送るだけ)
    for fn in ("async function convertAndOpenEditor()", "async function reseedEditorSong()"):
        assert 'form.append("lyrics", $("lyrics").value);' in _function_body(script, fn)


def test_card_selects_mirror_the_canonical_form():
    """カードのプルダウンは正本の写し。選択肢も値も片方向に写して二重管理しない。"""
    script = _script()
    opts = _function_body(script, "function syncBuilderOptions()")
    # 選択肢は optgroup ごとそのまま複製する(表示名の付け直しをしない)
    assert '$("builder-sample").innerHTML = $("sample-select").innerHTML;' in opts
    assert '$("builder-wordlist").innerHTML = wl.innerHTML;' in opts
    # 単語リストのセレクトが出ない構成(editor conf 無し)ではカード側も出さない
    assert '$("builder-wordlist-field").hidden = wl.hidden;' in opts
    assert "syncBuilderValues();" in opts
    # カード → 正本 → change の順(既存の applySample / applyWordlistSelection を通す)
    wiring = script[script.index('$("builder-sample").addEventListener'):]
    wiring = wiring[: wiring.index('$("sample-select").addEventListener')]
    assert '$("sample-select").value = $("builder-sample").value;' in wiring
    assert 'sel.dispatchEvent(new Event("change", { bubbles: true }));' in wiring


def test_card_wordlist_select_shows_the_editor_own_list():
    """エディタの中で自作リストを使っているあいだは、合成の選択肢で選択済みに見せる。

    自作リスト(ORIGINAL/csvText)のときは正本 #wordlist が空になるので、その
    ままだとカードのプルダウンが「未選択」になって何に空耳させているか分からない。
    """
    script = _script()
    assert 'const EDITOR_WORDLIST_VALUE = "__editor__";' in script
    assert 'const EDITOR_WORDLIST_LABEL = "自作リスト(替え歌エディタ)";' in script
    # 正本に名前が入っていればそちらが優先(古い ORIGINAL セッションに引きずられない)
    shows = _function_body(script, "function showsEditorWordlist()")
    assert "return !currentWordlistName() && usesEditorWordlist();" in shows
    body = _function_body(script, "function syncBuilderValues()")
    assert "const own = showsEditorWordlist();" in body
    assert "card.appendChild(o);" in body     # 自作リストのあいだだけ足す
    assert "synth.remove();" in body          # 名前付きリストに戻ったら取り除く
    assert 'card.value = own ? EDITOR_WORDLIST_VALUE : (wl.hidden ? "" : wl.value);' in body
    # 選び直されても正本は触らない(「何も選ばない」に落とさない)
    assert "if (v === EDITOR_WORDLIST_VALUE) { syncBuilderValues(); return; }" in script
def test_layout_preview_image_needs_a_wordlist_name():
    """レイアウトプレビューの代表画像は、単語リスト名が空なら取りに行かない。

    自作リスト(ORIGINAL/csvText)のあいだは正本 #wordlist が空。空の名前で
    /api/wordlist-image を叩くと404になるので、loadWordlistImage と同じ空ガードを
    置いてプレースホルダに落とす。
    """
    script = _script()
    body = _function_body(script, "function leContent(e)")
    assert 'const name = $("wordlist").value.trim();' in body
    assert 'if (name) src = "/api/wordlist-image?wordlist=" + encodeURIComponent(name);' in body
    # 代表画像のもう一方の経路(サムネ)も同じ流儀の空ガードを持つ
    thumb = _function_body(script, "function loadWordlistImage(name, seq)")
    assert "if (!name || hiddenPreviewReason(name)) { hide(); return; }" in thumb


# ---- エディタからの「曲を変えたい」依頼(hostRequest)にホストが応える ----


def test_editor_seed_advertises_the_song_choices():
    """シードに host.songs / host.canUploadSong / song.id を載せる。

    エディタ側は MIDI の実体も解析も持たないので、選べる曲は親が教える。
    一覧は正本(#sample-select)の写しで、カードの曲プルダウンと同じもの。
    """
    script = _script()
    body = _function_body(script, "function withHostInfo(payload)")
    assert "data.host = { songs: hostSongList(), canUploadSong: true };" in body
    # 曲のIDは来歴(midiSampleId)から決める。自分で選んだMIDIには付けない
    assert "if (midiSampleId) song.id = midiSampleId;" in body
    assert "else delete song.id;" in body
    songs = _function_body(script, "function hostSongList()")
    assert '[...$("sample-select").options]' in songs
    assert "id: o.value, title: o.textContent" in songs
    # セットアップ画面のシードはこれを通してから書く
    opened = _function_body(script, "async function convertAndOpenEditor()")
    assert "const seed = JSON.stringify(withHostInfo(body));" in opened


def test_host_request_is_polled_and_handled_once():
    """依頼の処理は監視ポーリングの中で、開いているあいだだけ、1件ずつ。"""
    script = _script()
    sync = _function_body(script, "function syncEditorSession()")
    assert "handleHostRequest();" in sync
    body = _function_body(script, "async function handleHostRequest()")
    # 多重処理を防ぐ(処理中フラグと、応えた nonce の記録)
    assert "if (hostRequestBusy) return;" in body
    assert "hostRequestSeen = key;" in body
    assert "if (key === hostRequestSeen) { clearHostRequest(key); return; }" in body
    # 閉じているあいだは応えない
    assert 'if ($("editor-frame-wrap").hidden) return;' in body
    # 成否によらず依頼は消す(残すとエディタが待ち続ける)
    assert "clearHostRequest(key);" in body and "hostRequestBusy = false;" in body
    # 応えているあいだに来た別の依頼は消さない(次の周期で応える)
    clear = _function_body(script, "function clearHostRequest(key)")
    assert "if (key && hostRequestKey(req) !== key) return;" in clear
    # 失敗は親のエディタ用メッセージ欄に出す
    assert '$("open-editor-msg").textContent = "曲を変えられませんでした: "' in body
    # 閉じたら止める(監視を切り、応えないまま残った依頼も持ち越さない)
    close = _function_body(script, "function closeEditor()")
    assert "watchEditorSession(false);" in close and "clearHostRequest();" in close
    show = _function_body(script, "function showEditorFrame()")
    assert 'hostRequestSeen = "";' in show and "hostRequestBusy = false;" in show


def test_embedded_editor_brand_returns_through_the_host_shell():
    """editor内の戻る操作はiframeを遷移させず、親で編集を取り込んで閉じる。"""
    script = _script()
    show = _function_body(script, "function showEditorFrame()")
    assert '$("editor-frame").src = "/editor/editor.html?embed=video";' in show

    handler = _function_body(script, "function onEditorHostMessage(ev)")
    assert "ev.origin !== location.origin" in handler
    assert 'ev.source !== $("editor-frame").contentWindow' in handler
    assert 'ev.data.type !== "soramimic:request-close"' in handler
    assert 'if ($("editor-frame-wrap").hidden) return;' in handler
    assert "importEditor();" in handler
    assert 'window.addEventListener("message", onEditorHostMessage);' in script


def test_host_song_request_moves_the_canonical_form_first():
    """曲の差し替えは正本(#sample-select / #midi)を動かしてから解析し直す。

    カードの曲プルダウン・サムネプレビュー・生成はすべて正本を見ているので、
    正本を動かさないとエディタの中だけ別の曲になってしまう。
    """
    script = _script()
    body = _function_body(script, "async function hostChangeSong(sid)")
    assert '$("sample-select").value = sid;' in body
    assert '$("sample-select").dispatchEvent(new Event("change", { bubbles: true }));' in body
    # 取得の失敗・古いMIDIの残りは既存の関門で止める
    assert "ensureSelectedSampleMidi(" in body
    assert body.index("ensureSelectedSampleMidi(") < body.index("reseedEditorSong()")
    up = _function_body(script, "async function hostUploadSong()")
    # キャンセルは何も変えずに戻る(依頼だけが消える)
    assert "if (!await pickMidiFile()) return;" in up
    # 自分のMIDIにしたらサンプルの選択は外す(投入前の突き合わせに上書きされる)
    assert '$("sample-select").value = "";' in up
    # midi-check のXF歌詞を、専用モーダルへ返す前に元歌詞の下敷きにする
    assert 'prefillOwnMidiLyrics($("midi").files[0]);' in up
    assert up.index("prefillOwnMidiLyrics") < up.index("reseedEditorSong()")
    picker = _function_body(script, "function pickMidiFile()")
    for needle in ('input.addEventListener("change", onChange);',
                   'input.addEventListener("cancel", onCancel);',
                   'window.addEventListener("focus", onFocus);',
                   "input.click();"):
        assert needle in picker


def test_own_midi_lyrics_prefill_preserves_existing_text_by_choice():
    """XF歌詞は空の元歌詞へ入れ、既存値は勝手に消さない。"""
    script = _script()
    body = _function_body(script, "function prefillOwnMidiLyrics(file)")
    assert "lastMidiCheck.file !== file" in body
    assert 'lastMidiCheck.lines.join("\\n")' in body
    assert "current.trim() && !confirm(" in body
    assert '$("lyrics").value = text;' in body
    assert '$("lyrics").dispatchEvent(new Event("change", { bubbles: true }));' in body


def test_midi_check_rejects_a_non_midi_400_response():
    """拡張子だけMIDIのファイルを検証失敗として握りつぶさない。"""
    body = _function_body(_script(), "async function checkMidi()")
    assert "notMidi = res.status === 400 && !!body.detail;" in body
    assert "rejectMidi(body.detail, notMidi)" in body
    assert "lastMidiCheck = { file: f, lines: body.midi_lines || [] };" in body


def test_host_song_request_keeps_the_wordlist_and_drops_the_results():
    """曲だけを差し替える。単語リストと変換のしかたは残し、変換結果は捨てる。"""
    script = _script()
    body = _function_body(script, "async function reseedEditorSong()")
    assert 'form.append("convert", "0")' in body        # 解析のみ(既存の経路)
    for keep in ("data.phrases = fresh.phrases;", "data.host = fresh.host;",
                 "if (fresh.song) data.song = fresh.song; else delete data.song;"):
        assert keep in body
    for drop in ("delete data.results;", "delete data.tokensList;", "delete data.unitsList;",
                 # 行ごとの元歌詞は前の曲の phrases への対応づけなので一緒に捨てる
                 "delete data.originalLines;"):
        assert drop in body
    # 元歌詞そのものは正本の値へ揃え直す(曲とセットの情報)
    assert "if (fresh.lyrics) data.lyrics = fresh.lyrics; else delete data.lyrics;" in body
    # 単語リスト・パラメータには触らない(エディタ側の今の設定のまま)
    assert "data.wordlist" not in body and "data.param" not in body
    # 新しいシードの指紋と来歴を控える。付け替えないと、曲を変えた直後の編集が
    # 「別の入力から作られたもの」として捨てられてしまう
    assert "markEditorSeed(text);" in body
    # 前の曲で取り込んだ替え歌JSONは外す
    assert "clearEditorFile();" in body
