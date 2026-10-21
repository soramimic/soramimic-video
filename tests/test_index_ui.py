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


def test_builder_omits_editor_entry_button():
    ids = [a.get("id") for tag, a in _tags() if tag == "button" and a.get("id")]
    assert "builder-edit" not in ids
    assert "lucky" not in ids
    # 保存済みがあるときの2択+右上の×。「やめる」の文字ボタンは置かない
    # (×・背景クリック・Escに吸収)
    for btn in ("editor-resume-continue", "editor-resume-regen", "editor-resume-close"):
        assert btn in ids


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_lyric_review_opens_once_preserves_edits_and_resumes_the_same_job():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function poll(", "async function openLyricReview(",
            "function restoreJobProgress(",
            "async function confirmLyricReview(", "function closeEditor()",
            "function lyricReviewCoverage(",
            "async function saveLyricReview(",
            "async function syncEditorSessionOnce()",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const elements = new Map();
        const $ = (id) => {
          if (!elements.has(id)) elements.set(id, {hidden: true, textContent: "", focus() {}});
          return elements.get(id);
        };
        const stored = new Map();
        const sessionStorage = {
          getItem: (key) => stored.get(key) || null,
          setItem: (key, value) => stored.set(key, value),
          removeItem: (key) => stored.delete(key),
        };
        const EDITOR_KEY = "editor", REVIEW_JOB_KEY = "review", EDITOR_SEED_KEY = "seed";
        let currentJob = "job1", reviewJobId = null, reviewOpenedJob = null;
        let reviewOpening = false, reviewSubmitting = false, pollSeq = 1, pollLastAt = 0;
        let resumed = false;
        let reviewSavedRaw = "", reviewSavePending = null;
        let pollFailures = 0, cancelPendingJob = null, openCount = 0, requests = [];
        const POLL_FETCH_TIMEOUT_MS = 1000, POLL_FATAL_STATUS = [404], POLL_INTERVAL_MS = 2000;
        const STAGE_LABELS = {}, document = {body: {classList: {remove() {}}}};
        const headers = () => ({});
        const renderStages = () => {}, renderBuilderBar = () => {}, schedulePoll = () => {};
        const setJobStatus = () => {}, watchEditorSession = () => {};
        const setBusy = () => {}, showProgress = () => {};
        let message = "", watched = null, rejectResume = true;
        const showBuilderMsg = (text) => { message = text; };
        const showEditorFrame = () => { openCount++; $("editor-frame-wrap").hidden = false; };
        const editorSessionData = () => JSON.parse(sessionStorage.getItem(EDITOR_KEY) || "null");
        const watch = (id) => { watched = id; pollSeq++; };
        const seed = {format: "soramimic-editor/1", phrases: ["シズム"], results: [["original"]]};
        const fetch = async (url, options = {}) => {
          requests.push(url);
          if (url.endsWith("/review")) return {ok: true, json: async () => seed};
          if (url.endsWith("/resume")) {
            assert.deepEqual(JSON.parse(await options.body.get("editor").text()),
              {...seed, results: [["edited"]]});
            return {ok: !rejectResume, json: async () => ({detail: "再試行してください"})};
          }
          return {ok: true, status: 200, json: async () => ({
            status: "awaiting_review", params: {}, stages: [],
          })};
        };
        """
    ) + functions + textwrap.dedent(
        """
        (async () => {
          await poll("job1", pollSeq);
          assert.equal(openCount, 1);
          assert.equal(reviewJobId, "job1");
          assert.equal($("builder-review").hidden, false);
          sessionStorage.setItem(EDITOR_KEY, JSON.stringify({...seed, results: [["edited"]]}));
          await syncEditorSessionOnce(); // Review must not change the next song's form.
          await closeEditor();
          assert.equal($("editor-frame-wrap").hidden, true);
          await poll("job1", pollSeq);
          assert.equal(openCount, 1); // Closing does not auto-open or auto-resume.
          assert.equal(requests.filter((url) => url.endsWith("/resume")).length, 0);
          reviewJobId = null; reviewOpenedJob = null; // Reload with the same tab storage.
          restoreJobProgress([{id: "job1", status: "awaiting_review"}]);
          await poll("job1", pollSeq);
          await poll("job1", pollSeq);
          assert.equal(openCount, 1);
          assert.equal($("editor-frame-wrap").hidden, true);
          assert.equal($("builder-review").hidden, false);
          assert.deepEqual(editorSessionData().results, [["edited"]]);
          await openLyricReview("job1"); // Explicitly resume the saved edits.
          await poll("job1", pollSeq);
          assert.equal(openCount, 2);
          assert.deepEqual(editorSessionData().results, [["edited"]]);
          await confirmLyricReview();
          assert.equal(reviewJobId, "job1");
          assert.equal($("editor-frame-wrap").hidden, false);
          assert.match($("lyric-review-error").textContent, /再試行してください/);
          rejectResume = false;
          await confirmLyricReview();
          assert.equal(watched, "job1");
          assert.equal(reviewJobId, null);
          assert.equal($("editor-frame-wrap").hidden, true);
          assert.equal($("confirm-lyric-review").disabled, false);
          resumed = false;
          currentJob = "job2";
          restoreJobProgress([{id: "job2", status: "running"}]);
          await poll("job2", pollSeq); // A newly completed conversion still opens review.
          assert.equal(openCount, 3);
          assert.equal(reviewJobId, "job2");
        })().catch((error) => { console.error(error); process.exitCode = 1; });
        """
    )
    subprocess.run(["node", "-e", node], check=True)


def test_review_autosave_serializes_edits_and_keeps_failed_close_open():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function saveLyricReview(", "async function closeLyricReview(",
            "function resumeHistoryReview(",
        )
    )
    node = textwrap.dedent("""
        const assert = require("node:assert/strict");
        const EDITOR_KEY = "editor", REVIEW_JOB_KEY = "job";
        let reviewJobId = "one", reviewSubmitting = false;
        let reviewSavePending = null, reviewSavedRaw = "";
        let raw = "first", reject = true, closeCount = 0, active = 0, maxActive = 0;
        let pause = null, release = null;
        const writes = [], elements = new Map();
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {disabled: false, textContent: ""});
          return elements.get(id);
        };
        const headers = () => ({});
        const sessionStorage = {getItem: key => key === EDITOR_KEY ? raw : "one"};
        const closeEditor = () => { closeCount++; };
        let historyLoads = 0, resumed = false, reviewOpening = false;
        let reviewOpenedJob = 'one', watched = null, busy = false, progress = false;
        const loadHistory = async () => { historyLoads++; };
        const setBusy = value => { busy = value; };
        const showProgress = () => { progress = true; };
        const watch = id => { watched = id; };
        const fetch = async (url, options) => {
          active++; maxActive = Math.max(maxActive, active);
          writes.push(await options.body.get("editor").text());
          if (pause) await pause;
          active--;
          return {ok: !reject, json: async () => ({detail: "保存エラー"})};
        };
    """) + functions + textwrap.dedent("""
        (async () => {
          await closeLyricReview();
          assert.equal(closeCount, 0);
          assert.equal($("close-lyric-review").disabled, false);
          assert.match($("lyric-review-error").textContent, /保存エラー/);
          reject = false;
          pause = new Promise(resolve => { release = resolve; });
          const first = saveLyricReview();
          raw = "latest";
          const next = saveLyricReview();
          release(); pause = null;
          await Promise.all([first, next]);
          assert.equal(maxActive, 1);
          assert.deepEqual(writes, ["first", "first", "latest"]);
          assert.equal(reviewSavedRaw, "latest");
          await closeLyricReview();
          assert.equal(closeCount, 1);
          assert.equal(writes.length, 3);
          assert.equal($("lyric-review-error").textContent, "");
          assert.equal(historyLoads, 1);
          reviewSubmitting = true;
          resumeHistoryReview({id: 'other'});
          assert.equal(watched, null);
          reviewSubmitting = false;
          resumeHistoryReview({id: 'other'});
          assert.equal(watched, 'other');
          assert.equal(reviewOpenedJob, null);
          assert(resumed && busy && progress);
        })().catch(error => {console.error(error); process.exitCode = 1;});
    """)
    subprocess.run(["node", "-e", node], check=True)


def test_review_regenerates_unmatched_lyrics_before_resuming_and_remembers_confirmation():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function lyricReviewCoverage(", "function confirmReviewRegeneration(",
            "async function confirmLyricReview(", "function onEditorModalKeydown(",
        )
    )
    node = textwrap.dedent("""
        const assert = require('node:assert/strict');
        const EDITOR_KEY = 'editor', REVIEW_JOB_KEY = 'job', REVIEW_REGENERATE_KEY = 'auto';
        const UNMATCHED_LYRICS_KEY = 'unmatched';
        const store = new Map(), preferences = new Map(), elements = new Map();
        const $ = id => {
          if (!elements.has(id)) {
            elements.set(id, {disabled: false, hidden: false, textContent: '', focus() {}});
          }
          return elements.get(id);
        };
        const sessionStorage = {getItem: key => store.get(key) || null};
        let denyStorage = false;
        const localStorage = {
          getItem: key => {if (denyStorage) throw Error('storage'); return preferences.get(key);},
          setItem: (key, value) => {
            if (denyStorage) throw Error('storage'); preferences.set(key, value);
          },
        };
        let reviewJobId = 'one', reviewSubmitting = false, reviewSavePending = null;
        let dialogCount = 0, regenerateCount = 0, watched = null, closed = false;
        const resumePayloads = [];
        let regenerateError = null, regenerateResult = null, closeListener = null;
        const dialog = $('lyric-review-confirm');
        dialog.addEventListener = (event, listener) => {
          assert.equal(event, 'close'); closeListener = listener;
        };
        dialog.showModal = () => {dialog.open = true; dialogCount++;};
        dialog.close = value => {dialog.open = false; dialog.returnValue = value; closeListener();};
        const complete = {unitsList: [[{}, {}, {}]], results: [[
          {surface: '固定', period: [0, 1], locked: true}, {surface: '補完', period: [1, 3]},
        ]]};
        const partial = {...complete, results: [[complete.results[0][0]]]};
        const remaining = {...complete, results: [[
          complete.results[0][0], {...complete.results[0][1], filler: true},
        ]]};
        $('editor-frame').contentWindow = {soramimicVideoReview: {
          async regenerateUnlocked() {
            regenerateCount++;
            assert.equal($('editor-frame').inert, true);
            assert.equal($('close-lyric-review').disabled, true);
            if (regenerateError) throw Error(regenerateError);
            store.set(EDITOR_KEY, JSON.stringify(regenerateResult));
          },
        }};
        const headers = () => ({}), showBuilderMsg = () => {}, setJobStatus = () => {};
        const closeEditor = () => {closed = true;}, watch = id => {watched = id;};
        const fetch = async (url, options) => {
          assert.equal(url, '/api/jobs/one/resume');
          resumePayloads.push(JSON.parse(await options.body.get('editor').text()));
          return {ok: true, json: async () => ({})};
        };
        const reset = payload => {
          store.set(EDITOR_KEY, JSON.stringify(payload)); store.set(REVIEW_JOB_KEY, 'one');
          reviewJobId = 'one'; regenerateResult = complete; regenerateError = null;
          closed = false; watched = null;
        };
        const tick = () => new Promise(setImmediate);
    """) + functions + textwrap.dedent("""
        (async () => {
          assert.deepEqual(lyricReviewCoverage(partial), {unmatched: true, gaps: true});
          assert.deepEqual(lyricReviewCoverage(remaining), {unmatched: true, gaps: false});
          assert.deepEqual(lyricReviewCoverage(complete), {unmatched: false, gaps: false});
          assert.deepEqual(lyricReviewCoverage({unitsList: [[]], results: [[]]}),
            {unmatched: false, gaps: false});
          reset(complete);
          await confirmLyricReview();
          assert.equal(dialogCount, 0); assert.equal(regenerateCount, 0);
          assert.deepEqual(resumePayloads, [complete]);

          reset(partial);
          let pending = confirmLyricReview();
          assert(dialog.open);
          await confirmLyricReview(); // Repeated clicks do not open another confirmation.
          assert.equal(dialogCount, 1);
          $('lyric-review-skip').checked = true;
          let prevented = false;
          onEditorModalKeydown({key: 'Escape', preventDefault() {prevented = true;}});
          assert.equal(prevented, false, 'Escape must reach the native confirmation dialog');
          dialog.close('cancel');
          await pending;
          assert.equal(preferences.size, 0); assert.equal(regenerateCount, 0);
          assert.equal(resumePayloads.length, 1); assert.equal(closed, false);
          assert.equal($('editor-frame').inert, false);

          pending = confirmLyricReview();
          $('lyric-review-skip').checked = true;
          dialog.close('generate');
          await pending;
          assert.equal(preferences.get(REVIEW_REGENERATE_KEY), '1');
          assert.equal(regenerateCount, 1); assert.deepEqual(resumePayloads[1], complete);
          assert.equal(watched, 'one'); assert.equal(closed, true);

          reset(partial);
          await confirmLyricReview();
          assert.equal(dialogCount, 2, 'remembered consent skips only the first confirmation');
          assert.equal(regenerateCount, 2); assert.equal(resumePayloads.length, 3);

          reset(partial); regenerateError = '再生成エラー';
          await confirmLyricReview();
          assert.match($('lyric-review-error').textContent, /再生成エラー/);
          assert.equal(resumePayloads.length, 3); assert.equal(closed, false);
          assert.equal($('confirm-lyric-review').disabled, false);

          reset(partial); regenerateResult = remaining;
          pending = confirmLyricReview(); await tick();
          assert(dialog.open); assert.equal($('lyric-review-skip-label').hidden, false);
          $('lyric-review-skip').checked = true;
          dialog.close('cancel'); await pending;
          assert.equal(preferences.has(UNMATCHED_LYRICS_KEY), false);
          assert.equal(resumePayloads.length, 3); assert.equal(closed, false);
          assert.deepEqual(JSON.parse(store.get(EDITOR_KEY)), remaining);
          pending = confirmLyricReview(); await tick();
          assert.equal($('lyric-review-skip').checked, false);
          $('lyric-review-skip').checked = true;
          dialog.close('generate'); await pending;
          assert.deepEqual(resumePayloads[3], remaining);
          assert.equal(preferences.get(UNMATCHED_LYRICS_KEY), '1');

          reset(partial); regenerateResult = partial;
          await confirmLyricReview();
          assert.equal(resumePayloads.length, 4);
          assert.match($('lyric-review-error').textContent, /対応づけられない/);
          assert.equal(reviewSubmitting, false); assert.equal($('editor-frame').inert, false);

          preferences.clear(); denyStorage = true; reset(partial);
          pending = confirmLyricReview();
          assert(dialog.open); $('lyric-review-skip').checked = true;
          dialog.close('generate'); await pending;
          assert.equal(resumePayloads.length, 5, 'storage denial must not prevent regeneration');
        })().catch(error => {console.error(error); process.exitCode = 1;});
    """)
    subprocess.run(["node", "-e", node], check=True)


def test_video_submit_confirms_unmatched_lyrics_and_remembers_only_acceptance():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function submitJob(", "function lyricReviewCoverage(",
            "function confirmReviewRegeneration(", "async function readParodyFile(",
            "function readParodyText(",
        )
    )
    node = textwrap.dedent("""
        const assert = require('node:assert/strict');
        const REVIEW_REGENERATE_KEY = 'regenerate', UNMATCHED_LYRICS_KEY = 'unmatched';
        const MAX_EDITOR_BYTES = 4 * 1024 * 1024;
        const preferences = new Map(), elements = new Map(), requests = [];
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {value: '', hidden: false, focus() {}});
          return elements.get(id);
        };
        let denyStorage = false, dialogs = 0, closeListener;
        const localStorage = {
          getItem(key) {if (denyStorage) throw Error('storage'); return preferences.get(key);},
          setItem(key, value) {
            if (denyStorage) throw Error('storage'); preferences.set(key, value);
          },
        };
        const dialog = $('lyric-review-confirm');
        dialog.addEventListener = (_event, listener) => {closeListener = listener;};
        dialog.showModal = () => {dialog.open = true; dialogs++;};
        dialog.close = value => {dialog.open = false; dialog.returnValue = value; closeListener();};
        let submitBusy = false, submitAbort = null, lyricsFileReading = false;
        const setBusy = busy => {submitBusy = busy;};
        const songInputMode = 'sample', simpleMode = false;
        const automaticLyricsEnabled = () => false, lyricsInput = () => ({error: ''});
        const ownAudioFile = () => null, ownSongKind = () => 'sample';
        const samplePending = null, midiChecking = null, midiSampleId = 'sample';
        const ensureSelectedSampleMidi = async () => true;
        let editorFile = null;
        const editorSourceForSubmit = () => ({file: editorFile});
        const parodyMismatch = () => false, apiKey = () => '', turnstileSiteKey = '';
        const fixedSynthesizer = 'voicevox', fixedVoicevoxStyle = 1;
        const songTitleOf = () => 'sample', buildConvertParams = () => '{}';
        const selectedTitleWords = () => '';
        const activeCustomList = () => null, showsEditorWordlist = () => false;
        const appendSongLyrics = () => {}, appendCustomWordlist = () => {};
        const showSubmitMsg = () => {}, showProgress = () => {}, setJobStatus = () => {};
        const setCancelPending = () => {}, resetTurnstile = () => {};
        const watch = () => setBusy(false);
        const postJobWithTurnstileRetry = async form => {
          requests.push(form);
          return {ok: true, status: 200, json: async () => ({id: 'new-job'})};
        };
        const complete = {unitsList: [[{}, {}]], results: [[{period: [0, 2]}]]};
        const gap = {...complete, results: [[{period: [0, 1], locked: true}]]};
        const filler = {...complete, results: [[{period: [0, 2], filler: true}]]};
        const select = payload => {editorFile = payload
          ? new Blob([JSON.stringify(payload)], {type: 'application/json'}) : null;};
        const tick = () => new Promise(setImmediate);
    """) + functions + textwrap.dedent("""
        (async () => {
          // No edited lyrics, complete lyrics, and voice previews need no warning.
          for (const payload of [null, complete]) {
            select(payload); await submitJob(0, '');
          }
          select(filler); await submitJob(10, 'head');
          assert.equal(dialogs, 0); assert.equal(requests.length, 3);

          // Both explicit fillers and uncovered ranges stop before uploading.
          // An existing regeneration preference does not accept singing original lyrics.
          preferences.set(REVIEW_REGENERATE_KEY, '1');
          for (const payload of [filler, gap]) {
            select(payload);
            const pending = submitJob(0, ''); await tick();
            assert(dialog.open); assert.equal(submitBusy, true);
            assert.equal($('lyric-review-skip-label').hidden, false);
            assert.match($('lyric-review-confirm-hint').textContent, /未対応の部分だけ/);
            assert.match($('lyric-review-confirm-hint').textContent, /元の読み/);
            assert.equal(requests.length, 3);
            const count = dialogs;
            await submitJob(0, ''); assert.equal(dialogs, count, 'double click opens once');
            $('lyric-review-skip').checked = true;
            dialog.close('cancel'); await pending;
            assert.equal(submitBusy, false); assert.equal(requests.length, 3);
            assert.equal(preferences.has(UNMATCHED_LYRICS_KEY), false);
          }

          select(filler);
          let pending = submitJob(0, ''); await tick();
          assert.equal($('lyric-review-skip').checked, false);
          dialog.close('generate'); await pending;
          assert.equal(requests.length, 4);
          assert.deepEqual(JSON.parse(await requests.at(-1).get('editor').text()), filler);
          assert.equal(preferences.has(UNMATCHED_LYRICS_KEY), false);

          // Confirming with the checkbox persists across a new input/job.
          pending = submitJob(0, ''); await tick();
          assert(dialog.open); $('lyric-review-skip').checked = true;
          dialog.close('generate'); await pending;
          assert.equal(preferences.get(UNMATCHED_LYRICS_KEY), '1');
          const count = dialogs;
          select(gap); await submitJob(0, '');
          assert.equal(dialogs, count); assert.equal(requests.length, 6);

          // Storage failures keep confirmation usable and do not save cancellation.
          denyStorage = true;
          pending = submitJob(0, ''); await tick();
          assert(dialog.open); $('lyric-review-skip').checked = true;
          dialog.close('generate'); await pending;
          assert.equal(requests.length, 7); assert.equal(submitBusy, false);
          pending = submitJob(0, ''); await tick();
          assert(dialog.open); dialog.close('cancel'); await pending;
          assert.equal(requests.length, 7); assert.equal(submitBusy, false);
        })().catch(error => {console.error(error); process.exitCode = 1;});
    """)
    subprocess.run(["node", "-e", node], check=True)


def test_public_ui_filters_wordlists_and_simple_ui_hides_advanced():
    """公開UIはカタログだけを出し、簡易UIではさらに詳細設定を隠す。"""
    script = _script()
    assert '$("advanced").hidden = simpleMode;' in script
    assert "loadWordlistSelect(conf.wordlist_config ?? conf.editor)" in script
    assert "const allowed = new Set(launchWordlists);" in script
    assert "if (launchWordlists.length)" in script
    assert "return allowed.has(name);" in script
    defaults = _function_body(script, "function applyFixedUiDefaults()")
    assert '$("synthesizer").value = "voicevox"' in defaults
    assert '$("auto-octave").checked = true' in defaults
    assert '$("transpose").value = "0"' in defaults
    assert 'wordlistLayouts[$("wordlist").value.trim()]' in defaults
    assert "if (!simpleMode) return;" not in defaults


def test_web_ui_only_exposes_fixed_position_song_text_fields():
    """dev UIでも歌声・レイアウト操作を隠し、元曲由来の文字だけ残す。"""
    markup = _markup()
    advanced = markup[markup.index('<details class="card" id="advanced">') :]
    advanced = advanced[: advanced.index("</details>")]
    assert '<section class="opt-group" hidden>\n    <h3 class="opt-group-title">① 歌声' in advanced
    assert '<section class="opt-group" hidden>\n    <!-- 中身がレイアウトだけ' in advanced
    assert '<h3 class="opt-group-title">曲情報・クレジット</h3>' in advanced
    for field in ("song-title", "song-credit", "thumbnail-wordlist-label"):
        assert f'id="{field}"' in advanced

    script = _script()
    submit = _function_body(script, "async function submitJob(previewSec, previewMode)")
    assert 'form.append("synthesizer", fixedSynthesizer)' in submit
    assert 'form.append("voicevox_style", String(fixedVoicevoxStyle))' in submit
    assert 'form.append("auto_octave", "true")' in submit
    assert 'form.append("transpose", "0")' in submit
    assert 'form.append("layout_json"' not in submit
    assert "let fixedVoicevoxStyle = 6000;" in script
    assert 'let fixedSynthesizer = "voicevox";' in script

    title = _function_body(script, "function songTitleOf(file)")
    assert '$("song-title").value.trim() || base' in title
    assert 'songTitle: $("song-title").value' in script
    assert '$("song-title").value = state.songTitle || ""' in script
    assert '$("song-title").value = sampleTitleOf(sid)' in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_uploaded_xf_midi_title_defaults_to_song_file_stem():
    """XFを表す複合拡張子は曲名として使うファイル名へ含めない。"""
    function = _function_body(_script(), "function songFileStem(file)") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        {function}
        assert.equal(songFileStem({{ name: "夜に駆ける.xf.mid" }}), "夜に駆ける");
        assert.equal(songFileStem({{ name: "夜に駆ける.XF.MIDI" }}), "夜に駆ける");
        assert.equal(songFileStem({{ name: "夜に駆ける.mid" }}), "夜に駆ける");
        assert.equal(songFileStem({{ name: "夜に.駆ける.mp3" }}), "夜に.駆ける");
        """
    )
    subprocess.run(["node", "-e", node], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_reselecting_song_keeps_the_active_editor_title():
    script = _script()
    listener = script[script.index(
        '$("midi").addEventListener("change", () => {\n  const file = ownSongFile();'
    ):]
    listener = listener[:listener.index("\n});") + len("\n});")]
    stem = _function_body(script, "function songFileStem(file)") + "\n}"
    metadata = _function_body(script, "async function updateUploadedSongMetadata(file)") + "\n}"
    clear = _function_body(script, "async function clearSelectedSong()") + "\n}"
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const elements = new Map();
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {
            _value: "", files: [], listeners: {},
            get value() { return this._value; },
            set value(value) {
              this._value = value;
              if (id === "midi" && value === "") this.files = [];
            },
            focus() {},
            addEventListener(type, handler) { this.listeners[type] = handler; },
            dispatchEvent(event) { this.listeners[event.type]?.(event); },
          });
          return elements.get(id);
        };
        const ownSongFile = () => $("midi").files[0];
        const ownSongKind = file => file.name.endsWith(".wav") ? "audio" : "midi";
        let active = null, restoring = false, midiSampleId = "";
        let editorImportedFile = null, midiFromSample = false;
        const VideoMetadata = {notifyChange: async () => {}};
        const activeLyricsJson = () => active;
        const audioInputReady = true, maxAudioUploadBytes = 1000;
        const document = {querySelector: () => ({textContent: ""})};
        const setRecordingStatus = () => {};
        const rejectOwnSongFile = message => { throw new Error(message); };
        const clearEditorFile = () => {}, clearAudioPresentation = () => {};
        const recordedSongFile = null;
        const clearAudioInput = () => {};
        const syncLyricsRecognition = () => {}, syncBuilderOptions = () => {};
        const syncBuilderValues = () => {}, showBuilderMsg = () => {};
        const schedulePreview = () => {};
        """
    ) + stem + "\n" + metadata + "\n" + clear + "\n" + listener + textwrap.dedent(
        """
        for (const name of ["upload.wav", "upload.xf.mid"]) {
          $("midi").files = [{name, size: 10}];
          active = {song: {title: "テスト曲"}};
          $("song-title").value = "テスト曲";
          $("midi").dispatchEvent(new Event("change"));
          assert.equal($("song-title").value, "テスト曲");
          $("song-title").value = "";
          $("midi").dispatchEvent(new Event("change"));
          assert.equal($("song-title").value, "", "upload must leave the default title blank");
          $("song-title").value = "手入力した曲名";
          $("midi").dispatchEvent(new Event("change"));
          assert.equal($("song-title").value, "手入力した曲名");
          for (const payload of [null, {}, {song: {title: " "}}, {song: {title: 1}}]) {
            active = payload;
            $("song-title").value = "前のファイルの曲名";
            $("midi").dispatchEvent(new Event("change"));
            assert.equal($("song-title").value, "前のファイルの曲名");
            $("song-title").value = "";
            $("midi").dispatchEvent(new Event("change"));
            assert.equal($("song-title").value, "", "upload must leave the default title blank");
          }
          restoring = true;
          $("song-title").value = "復元した曲名";
          $("midi").dispatchEvent(new Event("change"));
          assert.equal($("song-title").value, "復元した曲名");
          restoring = false;
          $("song-credit").value = "手入力したクレジット";
          clearSelectedSong();
          assert.equal(ownSongFile(), undefined);
          assert.equal($("song-title").value, "復元した曲名");
          assert.equal($("song-credit").value, "手入力したクレジット");
          $("midi").files = [{name: "replacement." + name.split(".").pop(), size: 10}];
          $("midi").dispatchEvent(new Event("change"));
          assert.equal($("song-title").value, "復元した曲名");
          assert.equal($("song-credit").value, "手入力したクレジット");
        }
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_song_text_previews_follow_title_credits_and_wordlist():
    """3つのプレビューは入力値と本番のクレジット優先順位に即時追随する。"""
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function songFileStem(file)",
            "function songTitleOf(file)",
            "function updateSongTextPreviews()",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const elements = new Map();
        const element = (id) => {
          if (!elements.has(id)) elements.set(id, {
            value: "", hidden: false, attrs: {}, textContent: "",
            getAttribute(name) { return this.attrs[name] || ""; },
            setAttribute(name, value) { this.attrs[name] = value; },
            removeAttribute(name) { delete this.attrs[name]; },
          });
          return elements.get(id);
        };
        const $ = element;
        const fitSongCreditPreview = () => {};
        const ownSongFile = () => ({ name: "upload.mid" });
        let midiSampleId = "";
        const sampleTitleOf = () => "サンプル曲";
        const sampleCredits = {};
        const activeCustomList = () => null;
        const selectedWordlistGroup = () => ({ text: "駅名" });
        const showsEditorWordlist = () => false;
        let wordlist = "stations";
        const wordlistPhrases = { stations: "駅名", vtuber: "VTuber名" };
        const currentWordlistName = () => wordlist;
        let fixedSynthesizer = "voicevox";
        let fixedVoicevoxStyle = 6000;
        $("voicevox-style").options = [
          { value: "6000", dataset: { name: "波音リツ" } },
          { value: "6001", dataset: { name: "四国めたん" } },
        ];
        $("builder-image").setAttribute("src", "blob:thumbnail");
        $("song-title").value = "夜に駆ける";
        $("song-credit").value = "オトノケ - Creepy Nuts";
        """
    ) + functions + textwrap.dedent(
        """
        updateSongTextPreviews();
        assert.equal($("song-preview-thumb-caption").textContent,
          "夜に駆ける を 駅名 で歌ってみた");
        assert.equal($("song-preview-footer").textContent,
          "lyrics & video by Soramimic / Original: オトノケ - Creepy Nuts");
        assert.equal($("song-preview-credits-original").textContent,
          "Original　オトノケ - Creepy Nuts");
        assert.equal($("song-preview-thumb-bg").getAttribute("src"), "blob:thumbnail");
        assert.equal($("song-preview-thumb-bg").hidden, false);

        assert.equal($("song-preview-thumb-credit").textContent,
          $("song-preview-footer").textContent);
        assert.equal($("song-preview-credits-synth").textContent,
          "Vocal Synthesis　VOICEVOX:波音リツ");
        assert.equal($("song-preview-credits-synth").hidden, false);
        fixedVoicevoxStyle = 6001;
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-synth").textContent,
          "Vocal Synthesis　VOICEVOX:四国めたん");
        fixedVoicevoxStyle = 9999;
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-synth").textContent, "Vocal Synthesis　VOICEVOX");
        fixedSynthesizer = "prettypitch";
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-synth").textContent,
          "Vocal Synthesis　PrettyPitch / 波音リツ（カノン）");
        assert.equal($("song-preview-credits-synth").hidden, false);
        assert.doesNotMatch($("song-preview-thumb-credit").textContent, /VOICEVOX|PrettyPitch/);
        assert.doesNotMatch($("song-preview-footer").textContent, /VOICEVOX|PrettyPitch/);
        fixedSynthesizer = "neutrino";
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-synth").textContent, "");
        assert.equal($("song-preview-credits-synth").hidden, true);

        $("thumbnail-wordlist-label").value = "好きな駅";
        updateSongTextPreviews();
        assert.equal($("song-preview-thumb-caption").textContent,
          "夜に駆ける を 好きな駅 で歌ってみた");
        assert.equal(wordlist, "stations");
        $("song-credit").value = "";
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-original").textContent,
          "Original　夜に駆ける");
        assert.equal($("song-preview-footer").textContent,
          "lyrics & video by Soramimic / Original: 夜に駆ける");
        assert.equal($("song-preview-thumb-credit").textContent,
          $("song-preview-footer").textContent);
        $("song-credit").value = " \\t\\n　";
        $("song-title").value = "三原色";
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-original").textContent,
          "Original　三原色");
        assert.equal($("song-preview-footer").textContent,
          "lyrics & video by Soramimic / Original: 三原色");
        $("song-title").value = "";
        updateSongTextPreviews();
        assert.equal($("song-preview-credits-original").textContent,
          "Original　upload");
        assert.equal($("song-preview-footer").textContent,
          "lyrics & video by Soramimic / Original: upload");

        // VTuberカードの必須表記もフッターへ反映する。
        wordlist = "vtuber";
        updateSongTextPreviews();
        assert.match($("song-preview-footer").textContent, /非公式・ファンメイド/);
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_advanced_settings_work_before_selection_and_stay_open_during_changes():
    """曲がなくても設定を開け、曲の解除・入力方法の切り替えで閉じない。"""
    advanced = _markup().split('<details class="card" id="advanced">', 1)[1]
    summary = advanced.split("</summary>", 1)[0]
    assert "aria-disabled" not in summary
    assert "曲と単語リストを選ぶと開けます" not in summary
    node = _song_input_node_harness() + textwrap.dedent(
        """
        $("advanced").open = true;
        syncBuilderValues();
        assert.equal($("advanced").open, true, "settings remain open before selecting anything");
        $("song-title").value = "先に入力した曲名";
        $("song-credit").value = "先に入力したクレジット";
        $("midi").files = [{ name: "first.mid" }];
        syncBuilderValues();
        assert.equal($("advanced").open, true);
        $("midi").value = "";
        syncBuilderValues();
        assert.equal($("advanced").open, true, "clearing a song must keep details open");
        assert.equal($("song-title").value, "先に入力した曲名");
        assert.equal($("song-credit").value, "先に入力したクレジット");
        $("midi").files = [{ name: "replacement.mid" }];
        syncBuilderValues();
        assert.equal($("advanced").open, true);
        switchSongInputMode("sample");
        assert.equal($("advanced").open, true, "switching input modes must keep details open");
        $("advanced").open = false;
        syncBuilderValues();
        assert.equal($("advanced").open, false, "manual closure is preserved");
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_plant_wordlist_is_available_in_every_selection_ui():
    """品質確認済みの植物を、通常UIと簡易UIの候補へ戻す。"""
    script = _script()
    assert "const HIDDEN_UI_WORDLISTS = new Set();" in script
    body = _function_body(script, "async function loadWordlistSelect(editorAvailable)")
    assert "return !HIDDEN_UI_WORDLISTS.has(name);" in body
    restore = _function_body(script, "async function doRestoreForm()")
    assert 'HIDDEN_UI_WORDLISTS.has(state.wordlist || "")' in restore
    assert 'new Set(["plant"])' not in script


def test_simple_ui_hides_the_irrelevant_song_length_limit():
    """同梱曲しか選べない初回公開版では、持ち込みMIDI向けの曲長案内を出さない。"""
    body = _function_body(_script(), "function setupPublicMode(conf)")
    assert "if (conf.max_song_seconds && !simpleMode)" in body
    # 日次上限や検証アカウントの案内まで一緒に消さない。
    assert "if (conf.quota_exempt === true)" in body
    assert "else if (conf.daily_quota)" in body
    assert "混雑時は順番待ちになります" in body
    assert "updatePublicCredit" not in _script()


def test_history_uses_cards_and_only_safe_display_fields():
    markup = _markup()
    assert 'id="history-body"' in markup
    assert "#history table" not in markup
    script = _script()
    load = _function_body(script, "async function loadHistory()")
    card = _function_body(script, "function historyCard(job, index)")
    assert 'document.createElement("div")' in load
    assert 'list.className = "history-list"' in load
    assert 'mediaSlot.className = "history-media"' in card
    assert "history-player" not in card
    assert "<table" not in load
    assert "job.params" not in card
    assert "job.id" not in card
    for private_name in ("midi_filename", "where", "convert_params"):
        assert private_name not in card
    assert "job.song_label" in card
    assert "job.wordlist_label" in card
    assert 'title.textContent = job.song_label || "曲"' in card
    assert "historyStatusLabel(job)" in card
    for status, label in {
        "queued": "開始待ち", "running": "作成中", "done": "完成",
        "error": "失敗", "canceled": "中止",
    }.items():
        assert f'{status}: "{label}"' in script


def test_queued_progress_shows_position_and_rounded_start_range():
    script = _script()
    label = _function_body(script, "function queueWaitLabel(job)")
    assert 'return "開始待ち：まもなく開始します"' in label
    assert "job.queue_ahead" in label
    assert "job.queue_wait_min_seconds" in label
    assert "job.queue_wait_max_seconds" in label
    assert "開始まで約" in label
    progress = _function_body(script, "function renderBuilderBar(job)")
    assert 'job?.status === "queued"' in progress
    assert 'style.width = "0%"' in progress


def test_initial_audio_analysis_uses_live_indeterminate_progress():
    script = _script()
    starting = _function_body(script, "function audioAnalysisIsStarting(job)")
    progress = _function_body(script, "function renderBuilderBar(job)")
    poll = _function_body(script, "async function poll(id, seq = pollSeq)")
    assert 'job.params?.input_kind === "audio"' in starting
    assert "Number(job.stage_progress || 0) <= 1" in starting
    assert 'fill.classList.toggle("indeterminate", analysisStarting)' in progress
    assert 'fill.style.width = "32%"' in progress
    assert "audioAnalysisIsStarting(job)" in poll
    assert "const text = `${job.stage_detail || label}${elapsed}`;" in poll
    html = INDEX.read_text(encoding="utf-8")
    assert ".builder-bar span.indeterminate" in html
    assert "@keyframes builder-progress-slide" in html


def test_history_does_not_create_or_fetch_media_until_an_explicit_action():
    script = _script()
    load = _function_body(script, "async function loadHistory()")
    card = _function_body(script, "function historyCard(job, index)")
    assert 'createElement("video")' not in load + card
    assert 'createElement("audio")' not in load + card
    assert "fetch(job.video_url" not in load + card
    playback = _function_body(script, "async function startHistoryPlayback(")
    assert 'document.createElement(job.result_kind === "audio" ? "audio" : "video")' in playback
    assert 'media.controls = true' in playback
    assert 'media.preload = "none"' in playback
    assert 'media.setAttribute("playsinline", "")' in playback
    assert "media.src = job.playback_url" in playback
    sharing = _function_body(script, "async function saveHistoryVideo(")
    assert "fetch(job.video_url" in sharing
    assert sharing.index("fetch(job.video_url") > sharing.index("resetHistorySave()")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
@pytest.mark.parametrize("ios", [False, True])
def test_history_thumbnail_plays_and_keeps_save_share_and_edit_actions(ios):
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function historyCard(", "function historyButton(",
            "function appendLyricEditAction(",
            "async function startHistoryPlayback(", "function disposeHistoryPlayback()",
            "function appendSaveMenu(", "function prefersNativeVideoSave(",
            "function isIOSPlatform(",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require('node:assert/strict');
        class Element extends EventTarget {
          constructor(tag) {
            super(); this.tagName = tag.toUpperCase(); this.children = [];
            this.attributes = {}; this.className = ''; this.textContent = '';
            this.classList = {add: name => { this.className += ' ' + name; }};
          }
          setAttribute(name, value) { this.attributes[name] = value; }
          removeAttribute(name) { delete this.attributes[name]; }
          append(...children) { children.forEach(child => this.appendChild(child)); }
          appendChild(child) {
            if (child.parent) {
              child.parent.children = child.parent.children.filter(x => x !== child);
            }
            this.children.push(child); child.parent = this;
          }
          prepend(child) { this.children.unshift(child); child.parent = this; }
          replaceChildren(child) { this.children = []; this.appendChild(child); }
          closest(selector) { return this.parent.closest(selector); }
          querySelector(selector) {
            const name = selector.slice(1);
            return this.children.find(child => child.className.split(' ').includes(name))
              || this.children.map(child => child.querySelector(selector)).find(Boolean);
          }
          load() {}
          play() { this.paused = false; return Promise.resolve(); }
          pause() { this.paused = true; this.dispatchEvent(new Event('pause')); }
          remove() { this.removed = true; }
        }
        const document = {createElement: tag => new Element(tag)};
        const navigator = {platform: IOS ? 'iPhone' : 'Linux', share() {}};
        class File {}
        const qs = url => url, apiKey = () => '';
        const resetHistorySave = () => {}, resetHistoryShare = () => {};
        const historyCreatedLabel = () => '', historyStatusLabel = () => '完成';
        const editorPeer = {}, saveCalls = [], editCalls = [];
        const saveHistoryVideo = (index, button, status) => saveCalls.push({index, button, status});
        const editResultLyrics = job => editCalls.push(job);
        const shareCalls = [], shareHistoryResult = index => shareCalls.push(index);
        const offerNativeVideoSave = (link, save) => save() !== false;
        const nodesIn = node => [node, ...node.children.flatMap(nodesIn)];
        let activeHistoryPlayback = null, preparedHistorySave = null;
        const historyJobs = ['video', 'audio'].map(result_kind => ({
          status: 'done', song_label: '曲名', result_kind, playback_url: '/playback',
          video_url: '/download', editor_url: '/editor',
          thumbnail_url: '/thumbnail',
        }));
        """
    ) + functions + textwrap.dedent(
        """
        (async () => {
          for (const [index, job] of historyJobs.entries()) {
            const card = historyCard(job, index);
            card.closest = () => card;
            const thumb = card.querySelector('.history-thumb');
            assert.equal(thumb.tagName, 'BUTTON');
            assert.equal(thumb.attributes['aria-label'], '曲名を再生');
            const icon = thumb.querySelector('.history-play-icon');
            assert.equal(icon.textContent, '▶');
            assert.equal(icon.attributes['aria-hidden'], 'true');
            const actions = card.querySelector('.history-actions');
            assert.equal(actions.querySelector('.save-menu').tagName, 'DETAILS');
            assert.equal(actions.querySelector('.history-play'), undefined);
            const share = actions.querySelector('.history-share');
            assert.equal(share.textContent, '共有');
            share.dispatchEvent(new Event('click'));
            assert.equal(shareCalls.at(-1), index);
            const edit = actions.querySelector('.history-edit-lyrics');
            assert.ok(edit.className.includes('result-action action-edit'));
            edit.dispatchEvent(new Event('click'));
            assert.equal(editCalls.at(-1), job);
            assert.equal(actions.querySelector('.history-save-retry'), undefined);
            if (IOS && job.result_kind === 'video') {
              const saveButton = nodesIn(actions).find(node => node.tagName === 'SUMMARY');
              const status = card.querySelector('.save-status');
              const mp4 = nodesIn(actions).find(node => node.tagName === 'A');
              const saveEvent = new Event('click', {cancelable: true});
              mp4.dispatchEvent(saveEvent);
              assert.equal(saveEvent.defaultPrevented, true);
              assert.equal(saveCalls.at(-1).button, saveButton);
              assert.equal(saveCalls.at(-1).status, status);
              assert.equal(saveCalls.at(-1).index, index);
              preparedHistorySave = {index, file: {}, shareable: true};
              const retry = new Event('click', {cancelable: true});
              const before = saveCalls.length;
              saveButton.dispatchEvent(retry);
              assert.equal(retry.defaultPrevented, true);
              assert.equal(saveCalls.length, before + 1);
              assert.equal(saveCalls.at(-1).button, saveButton);
              preparedHistorySave.needsTap = false;
              const afterSheet = new Event('click', {cancelable: true});
              saveButton.dispatchEvent(afterSheet);
              assert.equal(afterSheet.defaultPrevented, false,
                           'opening the sheet restores access to every format');
              preparedHistorySave.needsTap = true;
              preparedHistorySave.shareable = false;
              const fallback = new Event('click', {cancelable: true});
              saveButton.dispatchEvent(fallback);
              assert.equal(fallback.defaultPrevented, false, 'fallback keeps format menu');
              assert.equal(saveCalls.length, before + 1);
              preparedHistorySave = null;
            }
            const previous = activeHistoryPlayback;
            thumb.dispatchEvent(new Event('click'));
            const active = activeHistoryPlayback;
            assert.equal(active.media.tagName, job.result_kind.toUpperCase());
            assert.equal(active.media.controls, true);
            assert.equal(active.media.paused, false);
            if (previous) assert.equal(previous.media.removed, true);
            active.media.currentTime = 12;
            active.media.pause();
            assert.equal(activeHistoryPlayback, active);
            assert.equal(active.media.currentTime, 12);
            await startHistoryPlayback(index, thumb); // Repeated click never closes it.
            assert.equal(activeHistoryPlayback, active);
            await active.media.play();
            active.media.dispatchEvent(new Event('ended'));
            assert.equal(activeHistoryPlayback, active);
          }
          disposeHistoryPlayback();
          assert.equal(activeHistoryPlayback, null);
          const pending = historyCard({status: 'running'}, 2).querySelector('.history-thumb');
          assert.equal(pending.tagName, 'DIV');
          assert.equal(pending.querySelector('.history-play-icon'), undefined);
        })().catch(error => { console.error(error); process.exitCode = 1; });
        """
    )
    subprocess.run(
        ["node", "-e", "const IOS = " + str(ios).lower() + ";\n" + node],
        check=True, text=True, capture_output=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_history_player_disposal_stops_detaches_and_releases_media():
    dispose = _function_body(_script(), "function disposeHistoryPlayback()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const calls = [];
        const media = {{
          pause() {{ calls.push("pause"); }},
          removeAttribute(name) {{ calls.push("remove:" + name); }},
          load() {{ calls.push("load"); }},
          remove() {{ calls.push("remove-element"); }},
        }};
        const thumb = {{ name: "thumbnail" }};
        const slot = {{
          replaceChildren(child) {{ calls.push("restore:" + child.name); }},
        }};
        const URL = {{ revokeObjectURL(url) {{ calls.push("revoke:" + url); }} }};
        let aborted = 0;
        let activeHistoryPlayback = {{
          media, slot, thumb, objectUrl: "blob:history",
          abort: {{ abort() {{ aborted += 1; }} }},
        }};
        {dispose}
        disposeHistoryPlayback();
        assert.equal(aborted, 1);
        assert.deepEqual(calls.slice(0, 5),
          ["pause", "remove:src", "remove:poster", "load", "remove-element"]);
        assert.ok(calls.indexOf("revoke:blob:history") > calls.indexOf("remove-element"));
        assert.ok(calls.indexOf("restore:thumbnail") > calls.indexOf("revoke:blob:history"));
        assert.equal(activeHistoryPlayback, null);
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_history_switch_disposes_before_making_a_fresh_media_element():
    playback = _function_body(_script(), "async function startHistoryPlayback(")
    assert playback.index("disposeHistoryPlayback();") < playback.index(
        'document.createElement(job.result_kind === "audio" ? "audio" : "video")'
    )
    assert "freshBuilderVideo" not in playback
    assert "prepareVideoShare" not in playback
    assert "resetVideoSharePreparation" not in playback
    assert "sharePlaybackUrl" not in playback
    assert 'slot.replaceChildren(media)' in playback
    dispose = _function_body(_script(), "function disposeHistoryPlayback()")
    assert 'active.slot.replaceChildren(active.thumb)' in dispose


def test_history_player_replaces_the_thumbnail_in_the_same_aspect_ratio_slot():
    css = _markup()
    assert ".history-media {" in css
    assert "aspect-ratio: 16 / 9" in css
    assert ".history-media video {" in css
    assert ".history-media audio {" in css
    assert ".history-player {" not in css


def test_completed_audio_uses_the_inline_playback_endpoint():
    poll = _function_body(_script(), "async function poll(")
    assert 'src="${qs(job.playback_url)}"' in poll
    assert 'src="${qs(job.video_url)}"' not in poll


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


def test_generation_progress_has_no_wordlist_specific_duration_notice():
    """生成時間の要因を単語リストだけに帰属する注意や判定を持たない。"""
    script = _script()
    assert "数分かかります" not in script
    assert "SLOW_WORDLISTS" not in script
    assert "builderStageStatus" not in script


def _script() -> str:
    """<script> 以降(=画面のふるまい)だけを返す。"""
    text = INDEX.read_text(encoding="utf-8")
    return text[text.index("<script>") :]


def _function_body(script: str, head: str) -> str:
    """head で始まる関数の中身(次の行頭 } まで)を返す。"""
    body = script[script.index(head) :]
    return body[: body.index("\n}\n")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_title_search_opens_choices_and_discards_dismissed_or_stale_responses():
    script = _script()
    block = script[script.index("function titleChoiceContext()"):
                   script.index("// 単語リストの代表画像(サムネプレビュー")]
    harness = r'''
      const assert = require('node:assert/strict');
      const elements = new Map();
      const element = () => ({
        value: '', textContent: '', disabled: false, options: [], children: [], listeners: {},
        add(option) {this.options.push(option);},
        replaceChildren(...items) {this.options = items; this.children = items; this.value = '';},
        append(child) {this.children.push(child);},
        setAttribute(name, value) {this[name] = value;},
        addEventListener(event, fn) {this.listeners[event] = fn;},
        focus() {document.activeElement = this;},
        showModal() {this.open = true;},
        close() {this.open = false; this.listeners.close?.();},
      });
      const document = {createElement: element, activeElement: null};
      const $ = id => {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      };
      class Option {constructor(text, value) {this.text = text; this.value = value;}}
      const OWN_SONG_VALUE = 'own', headers = () => ({});
      let filename = 'song.mid';
      const ownSongFile = () => combo.sampleId === OWN_SONG_VALUE ? {name: filename} : null;
      let title = 'サンゲンショク', restoring = false, songInputMode = 'upload';
      const songTitleOf = () => title || ownSongFile()?.name.replace(/\.[^.]+$/, '') || '';
      let combo = {sampleId: 'own', wordlistName: 'scientist', customList: null};
      const currentCombo = () => combo;
      let titleChoice = null, titleCandidates = [], titleCandidatesContext = '';
      let titleCandidatesAbort = null, titleCandidatesSeq = 0, saved, previews = 0;
      const saveForm = () => {saved = titleChoice;};
      const schedulePreview = () => {previews++;};
      const setTimeout = () => 1, clearTimeout = () => {};
      let resolveFetch, sent, signal;
      const fetch = async (_url, options) => {
        sent = JSON.parse(options.body);
        signal = options.signal;
        return await new Promise(resolve => {resolveFetch = resolve;});
      };
    ''' + block + r'''
      (async () => {
        const choice = {words: [{id: '1', surface: 'ハンゼン'}, {id: '2', surface: 'ホフ'}],
          label: 'ハンゼン ＋ ホフ', reading: 'ハンゼン・ホフ',
          originals: ['ハンゼン氏', 'ホフ氏']};
        const load = () => $('title-candidates-load').listeners.click();
        const select = () => $('title-words').listeners.change();
        const dialog = $('title-candidates-dialog');
        const list = $('title-candidates-list');
        $('where').value = 'field=天文学';
        const pending = load();
        assert.equal(dialog.open, true, 'search opens the dialog before the request completes');
        assert.equal(list.children.length, 0);
        assert.match($('title-candidates-dialog-status').textContent, /探しています/);
        assert.equal($('title-candidates-load').disabled, true);
        assert.deepEqual(sent, {title, wordlist: 'scientist', where: 'field=天文学'});
        resolveFetch({ok: true, json: async () => ({candidates: [choice]})});
        await pending;
        assert.equal($('title-candidates-load').disabled, false);
        assert.equal(dialog.open, true);
        assert.equal(list.children.length, 2, 'candidate and automatic choice are visible');
        assert.equal(document.activeElement, list.children[0]);
        assert.equal(list.children[0].children[0].textContent, choice.label);
        assert.match(list.children[0].children[1].textContent, /ハンゼン氏 ＋ ホフ氏/);
        list.children[0].listeners.click();
        assert.equal(dialog.open, false, 'selecting a candidate closes the dialog');
        assert.equal(selectedTitleWords(), JSON.stringify(choice.words));
        assert.equal(saved.label, 'ハンゼン ＋ ホフ');
        assert.equal(previews, 1);
        assert.match($('title-words-detail').textContent, /ハンゼン氏 ＋ ホフ氏/);
        // Clearing only the song file must retain the chosen words, including after reload.
        combo.sampleId = ''; refreshTitleChoices();
        assert.equal(ownSongFile(), null);
        assert.equal(selectedTitleWords(), JSON.stringify(choice.words));
        assert.equal($('title-words').value, JSON.stringify(choice.words));
        assert.equal($('title-candidates-load').disabled, true, 'a new search needs a song');
        titleCandidates = []; titleChoice = saved; refreshTitleChoices();
        assert.equal($('title-words').value, JSON.stringify(choice.words));
        combo.sampleId = OWN_SONG_VALUE; refreshTitleChoices();
        assert.equal(selectedTitleWords(), JSON.stringify(choice.words));
        assert.equal($('title-candidates-load').disabled, false);
        // Re-rendering and restoring a saved choice retain its words.
        titleCandidates = []; titleChoice = saved; renderTitleChoices();
        assert.equal($('title-words').value, JSON.stringify(choice.words));
        $('title-words').value = ''; select();
        assert.equal(selectedTitleWords(), '');
        // Dismissing while loading cancels the request and never reopens the dialog.
        const dismissed = load();
        $('title-candidates-close').listeners.click();
        assert.equal(signal.aborted, true);
        assert.equal($('title-candidates-load').disabled, false);
        resolveFetch({ok: true, json: async () => ({candidates: [choice]})});
        await dismissed;
        assert.equal(dialog.open, false);
        assert.deepEqual(list.children, []);
        assert.equal($('title-candidates-status').textContent, '');
        // A slow response for a previous title must not replace the new context.
        const stale = load();
        title = '別の曲'; refreshTitleChoices();
        resolveFetch({ok: true, json: async () => ({candidates: [choice]})});
        await stale;
        assert.equal(dialog.open, false);
        assert.deepEqual(titleCandidates, []);
        assert.equal($('title-candidates-status').textContent, '');
        titleChoice = {...choice, context: titleChoiceContext()};
        combo.sampleId = ''; refreshTitleChoices();
        $('where').value = ''; refreshTitleChoices();
        assert.equal(titleChoice, null);
        // A title edit while waiting for a replacement also invalidates the choice.
        titleChoice = {...choice, context: titleChoiceContext()};
        title = 'さらに別の曲'; refreshTitleChoices();
        assert.equal(titleChoice, null);
        // A different dictionary still invalidates the choice while no file is selected.
        titleChoice = {...choice, context: titleChoiceContext()};
        combo.sampleId = ''; refreshTitleChoices();
        combo.wordlistName = 'stations'; refreshTitleChoices();
        assert.equal(titleChoice, null);
        assert.equal(selectedTitleWords(), '');
        combo.wordlistName = 'scientist'; combo.sampleId = OWN_SONG_VALUE;
        // Switching to sample input must not carry an uploaded song's title selection.
        titleChoice = {...choice, context: titleChoiceContext()};
        combo.sampleId = ''; songInputMode = 'sample'; refreshTitleChoices();
        assert.equal(titleChoice, null);
        songInputMode = 'upload'; combo.sampleId = OWN_SONG_VALUE;
        // Empty results and failures remain visible inside the open dialog.
        const empty = load();
        resolveFetch({ok: true, json: async () => ({candidates: []})});
        await empty;
        assert.equal(dialog.open, true);
        assert.match($('title-candidates-dialog-status').textContent, /見つかりません/);
        list.children[0].listeners.click();
        assert.equal(dialog.open, false);
        const failed = load();
        resolveFetch({ok: false, json: async () => ({detail: 'しばらく待ってください'})});
        await failed;
        assert.equal(dialog.open, true);
        assert.match($('title-candidates-dialog-status').textContent, /しばらく待って/);
        $('title-candidates-close').listeners.click();
        assert.equal(dialog.open, false);
        // With no explicit title, clearing/reloading/reselecting the same filename is safe.
        title = ''; titleChoice = {...choice, context: titleChoiceContext()};
        combo.sampleId = ''; refreshTitleChoices();
        assert.equal(selectedTitleWords(), JSON.stringify(choice.words));
        titleChoice = JSON.parse(JSON.stringify(titleChoice)); refreshTitleChoices();
        assert.equal($('title-words').value, JSON.stringify(choice.words));
        combo.sampleId = OWN_SONG_VALUE; refreshTitleChoices();
        assert.equal(selectedTitleWords(), JSON.stringify(choice.words));
        filename = 'different.mid'; refreshTitleChoices();
        assert.equal(titleChoice, null, 'a different filename changes the implicit title');
        // Editing custom CSV content also invalidates the selected row identities.
        combo.customList = {text: 'one'};
        titleChoice = {...choice, context: titleChoiceContext()};
        combo.sampleId = ''; refreshTitleChoices();
        combo.customList.text = 'two'; refreshTitleChoices();
        assert.equal(selectedTitleWords(), '');
        assert.equal(titleChoice, null);
        titleChoice = {...choice, context: '{invalid'};
        assert.doesNotThrow(refreshTitleChoices, 'corrupt saved context cannot break the UI');
        assert.equal(titleChoice, null);
      })().catch(error => {console.error(error); process.exitCode = 1;});
    '''
    subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_superseded_midi_check_keeps_submit_waiter_live():
    """復元中に検証予約が重なっても、先のPromiseを待つ生成操作を置き去りにしない。"""
    schedule = _function_body(_script(), "function scheduleMidiCheck()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let midiRejecting = false;
        let midiCheckTimer = null;
        let midiChecking = null;
        let midiCheckResolve = null;
        let timerSeq = 0;
        const timers = new Map();
        global.setTimeout = (fn) => {{
          const id = ++timerSeq;
          timers.set(id, fn);
          return id;
        }};
        global.clearTimeout = (id) => timers.delete(id);
        let checks = 0;
        const ownSongKind = () => "";
        const selectedSampleIsAudio = () => false;
        const clearMidiNotices = () => {{}};
        async function runScheduledMidiCheck() {{ checks += 1; }}
        {schedule}
        (async () => {{
          scheduleMidiCheck();
          const firstWaiter = midiChecking;
          scheduleMidiCheck();
          const latestWaiter = midiChecking;
          assert.notEqual(firstWaiter, latestWaiter);
          assert.equal(timers.size, 1, "debounce must leave only the latest check scheduled");
          const runLatest = [...timers.values()][0];
          timers.clear();
          runLatest();
          await Promise.all([firstWaiter, latestWaiter]);
          assert.equal(checks, 1);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_duplicate_sample_midi_check_reuses_in_flight_request():
    """同じサンプルの検証が実行中なら、復元処理が重なっても二重解析しない。"""
    script = _script()
    key_fn = _function_body(script, "function currentMidiCheckKey()") + "\n}"
    run_fn = _function_body(script, "function runScheduledMidiCheck()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const midiFile = {{ name: "yorunikakeru.mid", size: 12345 }};
        const elements = {{
          midi: {{ files: [midiFile] }},
          lyrics: {{ value: "夜に駆ける" }}
        }};
        const $ = (id) => elements[id];
        let midiSampleId = "yorunikakeru";
        let midiCheckInFlight = null;
        let midiCheckInFlightKey = "";
        let midiCheckInFlightFile = null;
        let midiCheckInFlightLyrics = "";
        let checks = 0;
        let finishCheck;
        function checkMidi() {{
          checks += 1;
          return new Promise((resolve) => {{ finishCheck = resolve; }});
        }}
        {key_fn}
        {run_fn}
        (async () => {{
          const first = runScheduledMidiCheck();
          const second = runScheduledMidiCheck();
          assert.equal(first, second);
          assert.equal(checks, 1, "same sample validation must share the active request");
          finishCheck();
          await Promise.all([first, second]);
          assert.equal(midiCheckInFlight, null);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_builder_restores_only_explicit_song_choices():
    """初回は曲を選ばず、保存サンプルを復元しても持ち込みMIDIを上書きしない。"""
    initialize = _function_body(_script(), "async function initBuilder()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const elements = {{
          "sample-select": {{ value: "", options: [
            {{ value: "" }}, {{ value: "furusato" }},
            {{ value: "previous" }}, {{ value: "unavailable", disabled: true }}
          ] }},
          lyrics: {{ value: "edited lyrics" }}
        }};
        const $ = (id) => elements[id];
        let builderReady = false, builderLive = false;
        let restoredSampleId = "", file = null, wordlist = "";
        let sampleLyricsId = "previous", sampleLyricsBaseline = "original lyrics";
        const samplesReady = Promise.resolve();
        const DEFAULT_COMBO = {{ sample: "furusato", wordlist: "baseball" }};
        const ownSongFile = () => file;
        const currentWordlistName = () => wordlist;
        const usesEditorWordlist = () => false;
        let customList = null;
        const activeCustomList = () => customList;
        const selectWordlist = (name) => {{ wordlist = name; }};
        const syncBuilderOptions = () => {{}};
        const syncBuilderValues = () => {{}};
        let previews = 0;
        const schedulePreview = () => {{ previews += 1; }};
        let applied = [];
        const applySample = (options) => {{
          applied.push({{ id: $("sample-select").value, ...options }});
          return Promise.resolve(true);
        }};
        const trackSample = (pending) => pending;
        {initialize}
        async function start(savedId, ownFile = null) {{
          builderReady = false;
          builderLive = false;
          restoredSampleId = savedId;
          file = ownFile;
          $("sample-select").value = "";
          applied = [];
          await initBuilder();
          assert.equal(builderLive, true);
        }}
        (async () => {{
          await start("");
          assert.equal($("sample-select").value, "", "fresh start needs an explicit song choice");
          assert.deepEqual(applied, []);
          assert.equal(wordlist, "baseball", "wordlist defaults remain available");
          await start("previous");
          assert.equal($("sample-select").value, "previous");
          assert.deepEqual(applied, [{{ id: "previous", keepLyrics: true }}]);
          const restoredMidi = {{ name: "my-song.mid" }};
          await start("previous", restoredMidi);
          assert.equal(file, restoredMidi);
          assert.equal($("sample-select").value, "");
          assert.deepEqual(applied, [], "stale sample state must not replace the restored upload");
          await start("unavailable");
          assert.equal($("sample-select").value, "");
          assert.deepEqual(applied, []);
          assert.equal(previews, 4);
          await initBuilder();
          assert.equal(previews, 4, "reloading configuration must not repeat initialization");
          wordlist = "";
          customList = {{ id: "saved-list", text: "ねこ,ネコ" }};
          await start("previous");
          assert.equal(wordlist, "", "restored custom selection must not become the default list");
          assert.equal(customList.id, "saved-list");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def _song_input_node_harness() -> str:
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function ownSongFile()",
            "function syncLyricsRecognition()",
            "function showSongInputMode(",
            "function switchSongInputMode(",
            "function syncBuilderValues()",
            "async function applySample(",
            "function trackSample(",
        )
    )
    sample_change = script[script.index('$("sample-select").addEventListener("change", () => {'):]
    sample_change = sample_change[:sample_change.index("\n});") + len("\n});")]
    return textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const elements = new Map();
        let focused = "";
        function $(id) {
          if (!elements.has(id)) elements.set(id, {
            _value: "", hidden: false, textContent: "", files: [], options: [],
            selectedOptions: [{ textContent: "Sample song" }], listeners: new Map(),
            classList: { toggle() {} },
            get value() { return this._value; },
            set value(value) {
              this._value = value;
              if (id === "midi" && value === "") this.files = [];
            },
            focus() { focused = id; },
            addEventListener(type, callback) { this.listeners.set(type, callback); },
            dispatchEvent(event) { this.listeners.get(event.type)?.(event); },
            replaceChildren() {},
            setAttribute(name, value) { this[name] = value; }
          });
          return elements.get(id);
        }
        let midiSampleId = "", sampleApplySeq = 0, songInputMode = "upload";
        let editorImportedFile = null;
        const originalLyricsFile = () => null;
        let recordedSongFile = null;
        let midiFromSample = false, samplePending = null, restoring = false;
        let sampleLyricsId = "", sampleLyricsBaseline = null;
        const sampleCredits = {}, sampleLicenseUrls = {}, sampleDescriptions = {};
        const EDITOR_WORDLIST_VALUE = "__editor__";
        const showsEditorWordlist = () => false;
        const activeCustomList = () => null;
        const simpleMode = false;
        const renderCustomListMenu = () => {};
        const syncJsonInputControls = () => {};
        const activeLyricsJson = () => null;
        const updateWordlistUsageNotice = () => {};
        const selectedSampleIsAudio = () => false;
        const clearAudioPresentation = () => { $("audio-input-panel").hidden = true; };
        const clearAudioInput = clearAudioPresentation;
        const stopSongRecording = () => {};
        const clearRecordedSongPlayback = () => {};
        const syncRecordingUi = () => {};
        const clearEditorFile = () => { $("editor").files = []; };
        let previews = 0, saves = 0, builderMessage = "";
        const schedulePreview = () => { previews += 1; };
        const saveForm = () => { saves += 1; };
        const showBuilderMsg = (message) => { builderMessage = message; };
        const sampleTitleOf = (id) => id;
        const document = { querySelector: () => $("analyze-stage") };
        """
    ) + functions + "\n" + sample_change


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_song_input_switch_clears_hidden_sources_and_focuses_visible_input():
    """入力元を切り替えると隠れた曲を解除し、表示中の入力へフォーカスを移す。"""
    node = _song_input_node_harness() + textwrap.dedent(
        """
        $("midi").files = [{ name: "my-song.mid" }];
        $("editor").files = [{ name: "parody.json" }];
        for (const id of ["lyrics", "song-credit"]) {
          $(id).value = "old song data";
        }
        syncBuilderValues();
        assert.equal($("song-upload-panel").hidden, false);
        assert.equal($("sample-picker").hidden, true);
        assert.equal($("song-title").disabled, false);
        switchSongInputMode("sample");
        assert.deepEqual($("midi").files, [], "a hidden MIDI must not remain active");
        assert.deepEqual($("editor").files, []);
        for (const id of ["lyrics", "song-credit"]) {
          assert.equal($(id).value, "");
        }
        assert.equal($("song-upload-panel").hidden, true);
        assert.equal($("sample-picker").hidden, false);
        for (const id of ["song-title", "song-credit"]) {
          assert.equal($(id).disabled, true, "sample metadata must be read-only");
        }
        assert.equal($("song-upload-selection").hidden, true);
        assert.equal(focused, "builder-sample");
        syncBuilderValues();
        assert.equal(songInputMode, "sample", "an empty picker must stay open");
        $("sample-select").value = "previous";
        midiSampleId = "previous";
        $("audio-sample-credit").hidden = false;
        const before = sampleApplySeq;
        switchSongInputMode("upload");
        assert.equal($("sample-select").value, "");
        assert.equal(midiSampleId, "", "the hidden sample must no longer generate");
        assert.ok(sampleApplySeq > before, "switching invalidates pending sample requests");
        assert.equal($("audio-sample-credit").hidden, true);
        assert.equal($("song-upload-panel").hidden, false);
        assert.equal($("sample-picker").hidden, true);
        for (const id of ["song-title", "song-credit"]) {
          assert.equal($(id).disabled, false, "own-song metadata must be editable");
        }
        assert.equal(focused, "song-upload-button");
        assert.equal(previews, 2);
        switchSongInputMode("upload");
        assert.equal(previews, 2, "switching to the current mode is a no-op");
        editorImportedFile = { name: "soramimic-lyrics.json" };
        $("editor").files = [editorImportedFile];
        $("lyrics").value = "受け取った歌詞";
        $("auto-lyrics").checked = false;
        switchSongInputMode("sample");
        assert.deepEqual($("editor").files, [editorImportedFile]);
        assert.equal($("lyrics").value, "受け取った歌詞");
        assert.equal($("auto-lyrics").checked, false);
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_sample_body_finishing_after_source_switch_cannot_replace_current_song():
    """切替前の歌詞本文が遅れて届いても、再選択した同じIDや持ち込み曲を上書きしない。"""
    node = _song_input_node_harness() + textwrap.dedent(
        """
        let finishBody, bodyStarted;
        const readingBody = new Promise((resolve) => { bodyStarted = resolve; });
        const fetch = async () => ({ ok: true, text: () => {
          bodyStarted();
          return new Promise((resolve) => { finishBody = resolve; });
        }});
        (async () => {
          showSongInputMode("sample");
          $("sample-select").value = "previous";
          $("sample-select").dispatchEvent(new Event("change"));
          const pending = samplePending;
          await readingBody;
          switchSongInputMode("upload");
          const ownMidi = { name: "my-song.mid" };
          $("midi").files = [ownMidi];
          $("lyrics").value = "my lyrics";
          // The same sample ID can be selected again before its old response arrives.
          // Keep that ID equal so this exercises request invalidation independently.
          $("sample-select").value = "previous";
          const savedBefore = saves;
          finishBody("obsolete sample lyrics");
          assert.equal(await pending, false);
          assert.equal($("midi").files[0], ownMidi);
          assert.equal(midiSampleId, "");
          assert.equal($("lyrics").value, "my lyrics");
          assert.equal(sampleLyricsId, "");
          assert.equal(sampleLyricsBaseline, null);
          assert.equal($("sample-status").textContent, "");
          assert.equal($("song-credit").value, "");
          assert.equal(saves, savedBefore);
          assert.equal(builderMessage, "", "a canceled request must not show a fetch error");
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
@pytest.mark.parametrize("filename,sample_id,migrated", [
    ("katatsumuri.mid", "", False),
    ("katatsumuri.mid", "furusato", False),
    ("katatsumuri.mid", "katatsumuri", True),
    ("my-song.mid", "", False),
    ("removed.mid", "removed", False),
    (".mid", "", False),
])
def test_saved_midi_restoration_requires_an_explicit_matching_sample(filename, sample_id, migrated):
    restore = _function_body(_script(), "async function doRestoreForm()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const FORM_KEY="form", MIDI_KEY="midi", EDITOR_FILE_KEY="editor";
        const saved = {{name: {filename!r}, dataUrl: "data:audio/midi;base64,dXBsb2Fk"}};
        const values = new Map([[MIDI_KEY, JSON.stringify(saved)]]);
        const localStorage = {{getItem: key => values.get(key) ?? null,
          removeItem: key => values.delete(key)}};
        let restoredSampleId = {sample_id!r}, parodyRestored = false;
        const samplesReady = Promise.resolve();
        const elements = {{"sample-select": {{options: [
          {{value:""}}, {{value:"katatsumuri"}}, {{value:"furusato"}}]}},
          midi: {{files: []}}, "midi-restore-hint": {{hidden: true}}}};
        const $ = id => elements[id];
        const injectFile = (input, file, dispatch) => {{
          assert.equal(dispatch, true); input.files = [file];
        }};
        const syncLyricsInput = () => {{}}, syncLyricsRecognition = () => {{}};
        {restore}
        (async () => {{
          await doRestoreForm();
          assert.equal(values.has(MIDI_KEY), {str(not migrated).lower()});
          if ({str(migrated).lower()}) {{
            assert.equal($("midi").files.length, 0);
            assert.equal(restoredSampleId, "katatsumuri");
          }} else {{
            assert.equal($("midi").files[0].name, saved.name);
            assert.equal(await $("midi").files[0].text(), "upload",
              "the saved upload must not be replaced by the bundled sample");
            assert.equal(restoredSampleId, {sample_id!r});
            assert.equal($("midi-restore-hint").hidden, false);
          }}
        }})().catch(error => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_completed_video_requests_no_preload_hint():
    """共有Fileは維持しつつ、完成直後の自動デコードを抑えるhintを指定する。"""
    video = next(a for tag, a in _tags() if a.get("id") == "builder-video")
    assert video.get("preload") == "none"


def test_video_generation_hides_the_sensitive_image_notice_immediately():
    """昆虫画像への配慮は、声プレビューでなく動画生成を選んだ時点で閉じる。"""
    submit = _function_body(_script(), "async function submitJob(")
    hide = 'if (previewSec === 0) $("builder-image-hidden").hidden = true;'
    assert hide in submit
    assert submit.index(hide) < submit.index("if (samplePending) await samplePending;")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_job_post_retries_one_server_rejected_turnstile_token():
    """tokenがサーバーで拒否されたとき、新しいtokenで同じ投入を一度だけ再送する。"""
    post = _function_body(_script(), "async function postJobWithTurnstileRetry(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let calls = 0;
        let rebuilds = 0;
        let turnstileSiteKey = "site";
        let turnstileNeedsInteraction = true;
        let turnstileWaiting = true;
        let turnstileFailed = true;
        const form = {{ values: {{}}, set(key, value) {{ this.values[key] = value; }} }};
        const headers = () => ({{}});
        const fetch = async () => {{ calls += 1; return {{ status: calls === 1 ? 403 : 200 }}; }};
        function hideTurnstilePrompt() {{}}
        function rebuildTurnstileWidget() {{ rebuilds += 1; }}
        async function ensureTurnstileToken() {{ return true; }}
        function turnstileToken() {{ return "fresh-token"; }}
        {post}
        (async () => {{
          const response = await postJobWithTurnstileRetry(form);
          assert.equal(response.status, 200);
          assert.equal(calls, 2);
          assert.equal(rebuilds, 1);
          assert.equal(form.values.turnstile_token, "fresh-token");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_audio_job_post_retries_one_mobile_network_failure():
    """iOSのLoad failedは同じ送信IDのまま一度だけ再送する。"""
    post = _function_body(_script(), "async function postJobWithTurnstileRetry(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let calls = 0;
        const statuses = [];
        let turnstileSiteKey = "";
        const form = {{ has: (key) => key === "audio" }};
        const headers = () => ({{}});
        const setJobStatus = (value) => {{ statuses.push(value); }};
        const setTimeout = (callback, delay) => {{ if (delay === 750) callback(); return 1; }};
        const clearTimeout = () => {{}};
        class XMLHttpRequest {{
          constructor() {{ this.upload = {{}}; this.requestHeaders = {{}}; }}
          open(method, url) {{ assert.equal(method, "POST"); assert.equal(url, "/api/jobs"); }}
          setRequestHeader(name, value) {{ this.requestHeaders[name] = value; }}
          abort() {{ if (this.onabort) this.onabort(); }}
          send(body) {{
            calls += 1;
            assert.equal(body, form);
            assert.equal(this.requestHeaders["X-Soramimic-Audio-Upload"], "1");
            if (calls === 1) this.onerror();
            else {{
              this.upload.onprogress({{ lengthComputable: true, loaded: 50, total: 100 }});
              this.status = 200;
              this.responseText = "{{}}";
              this.onload();
            }}
          }}
        }}
        const fetch = async () => assert.fail("audio uploads must use XMLHttpRequest");
        {post}
        (async () => {{
          const response = await postJobWithTurnstileRetry(form);
          assert.equal(response.status, 200);
          assert.equal(calls, 2);
          assert.ok(statuses.some((value) => /音源を再送/.test(value)));
          assert.equal(statuses.at(-1), "音源を送信中… 50%");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_stalled_audio_upload_is_aborted_and_retried():
    """エラーを返さず停止するiOS送信もwatchdogで回復する。"""
    post = _function_body(_script(), "async function postJobWithTurnstileRetry(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let calls = 0;
        let status = "";
        let turnstileSiteKey = "";
        const form = {{ has: (key) => key === "audio" }};
        const headers = () => ({{}});
        const setJobStatus = (value) => {{ status = value; }};
        const watchdogs = [];
        const setTimeout = (callback, delay) => {{
          if (delay === 750) callback();
          else watchdogs.push(callback);
          return watchdogs.length;
        }};
        const clearTimeout = () => {{}};
        class XMLHttpRequest {{
          constructor() {{ this.upload = {{}}; }}
          open() {{}}
          setRequestHeader() {{}}
          abort() {{ if (this.onabort) this.onabort(); }}
          send() {{
            calls += 1;
            if (calls === 2) {{
              this.status = 200;
              this.responseText = "{{}}";
              this.onload();
            }}
          }}
        }}
        const fetch = async () => assert.fail("audio uploads must use XMLHttpRequest");
        {post}
        (async () => {{
          const pending = postJobWithTurnstileRetry(form);
          assert.equal(calls, 1);
          watchdogs.shift()();
          const response = await pending;
          assert.equal(response.status, 200);
          assert.equal(calls, 2);
          assert.match(status, /音源を再送/);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_audio_upload_retry_can_be_canceled_before_job_creation():
    """再送中はジョブIDがまだ無くても、中断ボタンでXHRを止められる。"""
    post = _function_body(_script(), "async function postJobWithTurnstileRetry(") + "\n}"
    cancel = _function_body(_script(), "async function cancelJob()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let calls = 0;
        let aborted = 0;
        let currentJob = null;
        let cancelPendingJob = null;
        let turnstileSiteKey = "";
        const controller = new AbortController();
        let submitAbort = controller;
        const form = {{ has: (key) => key === "audio" }};
        const headers = () => ({{}});
        const statuses = [];
        const setJobStatus = (value) => statuses.push(value);
        const showBuilderMsg = () => {{}};
        const setCancelPending = () => {{}};
        const setTimeout = (callback, delay) => {{
          if (delay === 750) callback();
          return 1;
        }};
        const clearTimeout = () => {{}};
        class XMLHttpRequest {{
          constructor() {{ this.upload = {{}}; }}
          open() {{}}
          setRequestHeader() {{}}
          abort() {{ aborted += 1; if (this.onabort) this.onabort(); }}
          send() {{
            calls += 1;
            if (calls === 1) this.onerror();
          }}
        }}
        const fetch = async () => assert.fail("cancel must not call the job API before creation");
        {post}
        {cancel}
        (async () => {{
          const pending = postJobWithTurnstileRetry(form, controller.signal);
          await Promise.resolve();
          await Promise.resolve();
          assert.equal(calls, 2, "the retry upload must be active");
          await cancelJob();
          await assert.rejects(pending, (error) => error.name === "AbortError");
          assert.equal(aborted, 1);
          assert.equal(controller.signal.aborted, true);
          assert.equal(statuses.at(-1), "中断しています…");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_audio_submission_uses_an_idempotency_key():
    submit = _function_body(_script(), "async function submitJob(")
    assert 'form.append("submission_id", submissionId);' in submit
    assert "globalThis.crypto?.randomUUID" in submit


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_submission_rechecks_song_selection_after_verification_wait():
    submit = _function_body(_script(), "async function submitJob(") + "\n}"
    harness = r'''
        const assert = require("node:assert/strict");
        const {File} = require("node:buffer");
        const audio = new File(["original audio"], "first.wav");
        const otherAudio = new File(["different audio"], "second.wav");
        const midi = new File(["midi"], "first.mid");
        let selectedAudio = null, selectedMidi = null, midiSampleId = "";
        let submitBusy = false, submitAbort = null, releaseVerification;
        const elements = new Map(), requests = [], messages = [];
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {value: "", hidden: false});
          return elements.get(id);
        };
        const ownAudioFile = () => selectedAudio;
        const ownSongFile = () => selectedAudio || selectedMidi;
        const ownSongKind = () => selectedMidi ? "midi" : selectedAudio ? "audio" : "";
        const setBusy = value => { submitBusy = value; };
        const showSubmitMsg = message => messages.push(message);
        const lyricsFileReading = false, songInputMode = "upload";
        const automaticLyricsEnabled = () => true;
        const lyricsInput = () => ({error: "", data: null});
        const samplePending = null, midiChecking = null;
        const ensureSelectedSampleMidi = async () => true;
        const editorSourceForSubmit = () => ({file: null});
        const simpleMode = true, apiKey = () => "";
        const turnstileSiteKey = "test", turnstileScriptFailed = false;
        const ensureTurnstileToken = () => new Promise(resolve => {releaseVerification = resolve;});
        const turnstileToken = () => "verified";
        const fixedSynthesizer = "hybrid", fixedVoicevoxStyle = 6000;
        const appendSongLyrics = () => {}, buildConvertParams = () => "";
        const selectedTitleWords = () => "";
        const activeCustomList = () => null, showsEditorWordlist = () => false;
        const appendCustomWordlist = () => {};
        const songTitleOf = () => midiSampleId || ownSongFile()?.name || "";
        const showProgress = () => {}, setJobStatus = () => {}, resetTurnstile = () => {};
        const setCancelPending = () => {}, hideTurnstilePrompt = () => {};
        const setBuilderState = () => {}, showBuilderMsg = () => {}, openJobDetails = () => {};
        const watch = () => { submitBusy = false; };
        const postJobWithTurnstileRetry = async form => {
          requests.push(form);
          return {ok: true, status: 200, json: async () => ({id: "job"})};
        };
    ''' + submit + r'''
        (async () => {
          const choices = {
            audio: () => {selectedAudio = audio; selectedMidi = null; midiSampleId = "";},
            midi: () => {selectedAudio = null; selectedMidi = midi; midiSampleId = "";},
            sample: () => {selectedAudio = null; selectedMidi = null; midiSampleId = "first";},
          };
          for (const initial of Object.keys(choices)) {
            for (const change of [false, true]) {
              choices[initial]();
              messages.length = 0;
              releaseVerification = null;
              const count = requests.length;
              const pending = submitJob(0, "");
              while (!releaseVerification) await new Promise(setImmediate);
              if (change) {
                if (initial === "audio") selectedAudio = otherAudio;
                else choices.audio();
              }
              releaseVerification(true);
              await pending;
              assert.equal(requests.length, count + (change ? 0 : 1),
                `${initial}: changing the song must require a new generation tap`);
              assert.equal(submitBusy, false);
              if (change) assert.ok(messages.some(text => text.includes("曲")));
              else if (initial === "audio") assert.equal(requests.at(-1).get("audio"), audio);
              else if (initial === "midi") assert.equal(requests.at(-1).get("midi"), midi);
              else assert.equal(requests.at(-1).get("sample_id"), "first");
            }
          }
        })().catch(error => {console.error(error); process.exitCode = 1;});
    '''
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_stale_progress_rejection_cannot_overwrite_a_newer_result(status):
    poll = _function_body(_script(), "async function poll(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let currentJob = "job", pollSeq = 1, pollLastAt = 0, resolve;
        const POLL_FETCH_TIMEOUT_MS = 8000, POLL_FATAL_STATUS = [401, 403, 404, 410];
        const headers = () => ({{}});
        const fetch = () => new Promise(done => {{resolve = done;}});
        const stopPolling = () => assert.fail("stale rejection must be ignored");
        const setJobStatus = () => assert.fail("preserve the newer completion message");
        {poll}
        (async () => {{
          const pending = poll("job");
          pollSeq++; // A focus/online event starts a newer polling chain.
          currentJob = null; // The newer chain has already displayed completion.
          resolve({{status: {status}, ok: false}});
          await pending;
        }})().catch(error => {{console.error(error); process.exit(1);}});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_turnstile_interaction_scrolls_to_inline_prompt():
    """追加操作が必要なときはカード内の確認欄まで自動スクロールする。"""
    markup = _markup()
    script = _script()
    assert 'id="turnstile-title">人間かどうかの確認が必要です' in markup
    assert 'class="turnstile-copy" role="status" aria-live="polite"' in markup
    assert 'class="turnstile-panel" tabindex="-1"' in markup
    assert 'id="turnstile-cancel"' not in markup
    assert 'turnstile-ui' not in script
    assert 'turnstile-overlay' not in markup
    prompt = _function_body(script, "function showTurnstilePrompt(")
    assert 'wrap.scrollIntoView({ behavior: "smooth", block: "center" })' in prompt
    assert "if (!alreadyActive || forceScroll)" in prompt
    assert "if (focusPanel)" in prompt
    challenge = _function_body(script, "function showTurnstileChallenge()")
    assert 'setTurnstilePromptCopy("チェックして動画生成を続ける", "")' in challenge
    assert 'classList.add("turnstile-challenge")' in challenge
    assert "showTurnstilePrompt(true, false)" in challenge
    render = _function_body(script, "function renderTurnstileWidget()")
    assert 'execution: "execute"' in render
    assert "scheduleTurnstileInteractionPrompt(epoch)" in render
    assert "cancelTurnstileInteractionReveal();" in render
    failure = _function_body(script, "function showTurnstileFailure(")
    assert failure.index("hideTurnstilePrompt();") < failure.index("setTurnstilePromptCopy(")


def test_turnstile_widget_is_visible_only_while_prompt_needs_attention():
    """自動確認のiframeだけを点滅させず、完了後もチェック欄を残さない。"""
    markup = _markup()
    assert "#turnstile-wrap:not(.turnstile-active) #turnstile-widget" in markup
    assert "#turnstile-wrap.turnstile-complete #turnstile-widget" in markup
    assert "inset-inline-start: -10000px" in markup
    assert "visibility: hidden" in markup
    assert "#turnstile-wrap.turnstile-challenge .turnstile-copy { display: block; }" in markup
    assert "#turnstile-wrap.turnstile-challenge .turnstile-copy .hint { display: none; }" in markup
    assert "#turnstile-wrap.turnstile-challenge .turnstile-panel" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_turnstile_interaction_prompt_is_delayed_and_cancelable():
    """自動成功は表示せず、操作開始通知なしの確認待ちでも欄を表示する。"""
    script = _script()
    schedule = _function_body(script, "function scheduleTurnstileInteractionPrompt(") + "\n}"
    cancel = _function_body(script, "function cancelTurnstileInteractionReveal()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const TURNSTILE_INTERACTION_REVEAL_MS = 700;
        let turnstileInteractionRevealTimer = null;
        let turnstileEpoch = 3;
        let turnstileWaiting = true;
        let turnstileNeedsInteraction = true;
        let turnstileFailed = false;
        let token = "";
        let timerCallback = null;
        let timerDelay = null;
        let shows = 0;
        let waitingShows = 0;
        let clears = 0;
        const setTimeout = (fn, delay) => {{ timerCallback = fn; timerDelay = delay; return 1; }};
        const clearTimeout = () => {{ clears += 1; }};
        const turnstileToken = () => token;
        const showTurnstileChallenge = () => {{ shows += 1; }};
        const showTurnstilePrompt = () => {{ waitingShows += 1; }};
        {cancel}
        {schedule}

        scheduleTurnstileInteractionPrompt(3);
        assert.equal(timerDelay, 700);
        token = "auto-token";
        turnstileNeedsInteraction = false;
        cancelTurnstileInteractionReveal();
        timerCallback();
        assert.equal(clears, 1);
        assert.equal(shows, 0);
        assert.equal(waitingShows, 0);

        token = "";
        scheduleTurnstileInteractionPrompt(3);
        timerCallback();
        assert.equal(waitingShows, 1, "pending verification must show without a callback");

        turnstileFailed = true;
        scheduleTurnstileInteractionPrompt(3);
        timerCallback();
        assert.equal(waitingShows, 1, "a failed challenge must not reopen");
        turnstileFailed = false;

        scheduleTurnstileInteractionPrompt(2);
        timerCallback();
        assert.equal(waitingShows, 1, "an old widget must not reveal the new prompt");

        token = "";
        turnstileNeedsInteraction = true;
        scheduleTurnstileInteractionPrompt(3);
        timerCallback();
        assert.equal(shows, 1);
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_turnstile_pending_widget_reappears_during_completion_display():
    """実際の表示・完了タイマーを通して、通知なしの初回と再試行を確認する。"""
    script = _script()
    turnstile = script[script.index('let turnstileSiteKey = "";') :]
    turnstile = turnstile[: turnstile.index("// エンジン選択に応じて")]
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const classes = new Set();
        const copy = {{textContent: ""}};
        const wrap = {{
          classList: {{
            contains: name => classes.has(name),
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
          }},
          querySelector: () => ({{focus() {{}}, ...copy}}),
          scrollIntoView() {{}},
        }};
        const $ = id => id === "turnstile-wrap" ? wrap : copy;
        const requestAnimationFrame = fn => fn();
        let options;
        const window = {{turnstile: {{
          render(_selector, config) {{ options = config; return "widget"; }},
          execute() {{}},
          getResponse() {{ return ""; }},
          reset() {{}},
        }}}};
        {turnstile}
        (async () => {{
          turnstileSiteKey = "site";
          renderTurnstileWidget();
          assert.equal(options.appearance, "execute");
          const first = ensureTurnstileToken(3000);
          await new Promise(resolve => setTimeout(resolve, 900));
          assert.equal(classes.has("turnstile-active"), true);
          options.callback("first-token");
          assert.equal(await first, true);
          assert.equal(classes.has("turnstile-complete"), true);

          resetTurnstile(false);
          const retry = ensureTurnstileToken(3000);
          assert.equal(classes.has("turnstile-complete"), false);
          assert.equal(turnstilePromptCompletionTimer, null);
          await new Promise(resolve => setTimeout(resolve, 900));
          assert.equal(classes.has("turnstile-active"), true);
          assert.equal(classes.has("turnstile-complete"), false);
          options.callback("retry-token");
          assert.equal(await retry, true);
          hideTurnstilePrompt();
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True, timeout=10)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_fast_turnstile_completion_stays_readable_then_disappears():
    """短時間の自動確認は完了文を残し、長い手動確認は完了直後に閉じる。"""
    script = _script()
    hide = _function_body(script, "function hideTurnstilePrompt()") + "\n}"
    complete = _function_body(script, "function completeTurnstilePrompt()") + "\n}"
    failure = _function_body(script, "function showTurnstileFailure(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const TURNSTILE_PROMPT_MIN_MS = 1400;
        let turnstilePromptShownAt = 0;
        let turnstilePromptCompletionTimer = null;
        let now = 0;
        let active = true;
        let completeClass = false;
        let timerDelay = null;
        let timerCalls = 0;
        let clears = 0;
        let copy = null;
        const wrap = {{
          classList: {{
            contains(name) {{ return name === "turnstile-active" ? active : completeClass; }},
            add(name) {{ if (name === "turnstile-complete") completeClass = true; }},
            remove(...names) {{
              if (names.includes("turnstile-active")) active = false;
              if (names.includes("turnstile-complete")) completeClass = false;
            }},
          }},
        }};
        const $ = () => wrap;
        Date.now = () => now;
        const setTurnstilePromptCopy = (title, hint) => {{ copy = [title, hint]; }};
        const showTurnstilePrompt = () => {{ active = true; }};
        const cancelTurnstileInteractionReveal = () => {{}};
        const setTimeout = (_fn, delay) => {{ timerCalls += 1; timerDelay = delay; return 1; }};
        const clearTimeout = () => {{ clears += 1; }};
        {hide}
        {complete}
        {failure}

        turnstilePromptShownAt = 0;
        now = 200;
        completeTurnstilePrompt();
        assert.equal(copy[0], "確認できました");
        assert.equal(copy[1], "動画生成を始めます。");
        assert.equal(completeClass, true);
        assert.equal(active, true);
        assert.equal(timerDelay, 1200);
        assert.equal(timerCalls, 1);
        completeTurnstilePrompt();
        assert.equal(timerCalls, 1);

        showTurnstileFailure(false);
        assert.equal(clears, 1);
        assert.equal(turnstilePromptCompletionTimer, null);
        assert.equal(completeClass, false);
        assert.equal(active, true);
        assert.equal(copy[0], "人間かどうかの確認をやり直してください");

        turnstilePromptCompletionTimer = null;
        completeClass = false;
        active = true;
        timerDelay = null;
        turnstilePromptShownAt = 0;
        now = 1500;
        completeTurnstilePrompt();
        assert.equal(active, false);
        assert.equal(timerDelay, null);
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_turnstile_prompt_can_rescroll_after_retry():
    """表示済みでも再試行時は再スクロールでき、通常pollでは繰り返さない。"""
    prompt = _function_body(_script(), "function showTurnstilePrompt(") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        let active = false;
        let scrolls = 0;
        let focuses = 0;
        let turnstilePromptShownAt = 0;
        const frames = [];
        const wrap = {{
          classList: {{
            contains() {{ return active; }},
            add() {{ active = true; }},
            remove() {{}},
          }},
          scrollIntoView() {{ scrolls += 1; }},
          querySelector() {{ return {{ focus() {{ focuses += 1; }} }}; }},
        }};
        const $ = () => wrap;
        const requestAnimationFrame = (fn) => frames.push(fn);
        {prompt}
        // 成功callbackが次のframeより先に案内を隠した場合は、画面を戻さない。
        showTurnstilePrompt();
        active = false;
        frames.shift()();
        assert.equal(scrolls, 0);

        showTurnstilePrompt();
        frames.shift()();
        showTurnstilePrompt();
        assert.equal(frames.length, 0);
        showTurnstilePrompt(true);
        frames.shift()();
        assert.equal(scrolls, 2);
        assert.equal(focuses, 2);
        """
    )
    subprocess.run(["node", "-e", node], check=True)


def test_submit_never_posts_without_a_turnstile_token():
    """確認待ちの画面を生成進捗に見せず、失敗時は空tokenをAPIへ送らない。"""
    submit = _function_body(_script(), "async function submitJob(")
    verification = 'if (!await ensureTurnstileToken()) {'
    assert verification in submit
    assert submit.index(verification) < submit.index("const form = new FormData()")
    guard = submit[submit.index(verification):].split("\n    }", 1)[0]
    assert "return;" in guard
    assert "showProgress();" not in guard
    assert submit.index("if (submitBusy) return;") < submit.index(
        "if (samplePending) await samplePending;"
    )
    ensure = _function_body(_script(), "async function ensureTurnstileToken(")
    assert "const verified = !failed && !!turnstileToken()" in ensure
    assert "rebuildTurnstileWidget()" in ensure
    assert "const loadDeadline = Math.min(deadline, Date.now() + 10000);" in ensure
    assert "Date.now() - loadStarted > 800" not in ensure
    assert "Date.now() - t0 > 800" not in ensure
    assert "if (turnstileWidget === null || turnstileScriptFailed)" in ensure
    assert "window.turnstile.execute(turnstileWidget)" in ensure
    assert ensure.index("hideTurnstilePrompt();") < ensure.index("setTurnstilePromptCopy(")
    assert "if (turnstileNeedsInteraction) scheduleTurnstileInteractionPrompt();" not in ensure
    assert "timeoutMs = 120000" in ensure
    assert "for (let attempt = 0; attempt < 2; attempt += 1)" in ensure
    assert "if (turnstilePromptCompletionTimer === null) completeTurnstilePrompt();" in ensure
    assert "showTurnstileFailure(turnstileScriptFailed);" in guard
    assert "hideTurnstilePrompt();" not in guard
    assert "resetTurnstile(!keepTurnstilePrompt);" in submit
    assert "if (!keepTurnstilePrompt) hideTurnstilePrompt();" in submit


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_turnstile_errors_rebuild_and_retry_widget():
    """一時エラーは同じ操作で再試行し、timeoutは待ち時間を延長しない。"""
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
        let turnstileScriptFailed = false;
        let turnstilePromptCompletionTimer = null;
        let rebuilds = 0;
        let now = 0;
        let waitMode = "error-then-success";
        let window = {{turnstile: {{execute() {{}} }}}};
        function turnstileToken() {{ return token; }}
        function showTurnstilePrompt() {{}}
        function showTurnstileFailure() {{}}
        function setTurnstilePromptCopy() {{}}
        function hideTurnstilePrompt() {{}}
        function completeTurnstilePrompt() {{}}
        function scheduleTurnstileInteractionPrompt() {{}}
        const $ = () => ({{classList: {{contains() {{ return false; }}}}}});
        function rebuildTurnstileWidget() {{
          rebuilds += 1;
          token = "";
          if (waitMode === "error-then-success") waitMode = "success";
        }}
        Date.now = () => now;
        global.setTimeout = (fn) => {{
          if (waitMode === "error-then-success") turnstileFailed = true;
          else if (waitMode === "success") token = "fresh-token";
          else now += 200;
          fn();
          return 1;
        }};
        {ensure}
        (async () => {{
          assert.equal(await ensureTurnstileToken(100), true);
          assert.equal(rebuilds, 1, "an errored widget must rebuild and retry immediately");
          waitMode = "timeout";
          token = "";
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
    assert 'appearance: "execute"' in render
    assert "if (epoch !== turnstileEpoch) return;" in render
    assert "turnstileEpoch += 1;" in rebuild
    assert "window.turnstile.remove(oldWidget)" in rebuild
    assert '$("turnstile-widget").replaceChildren();' in rebuild
    assert 'console.warn("Turnstileの再描画に失敗しました", err);' in rebuild


def test_random_button_and_behavior_are_removed():
    script = _script()
    assert not any(a.get("id") == "lucky" for _tag, a in _tags())
    assert "function luckyRandomCombo()" not in script
    assert "function luckyCandidatePools()" not in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_desktop_video_prepares_one_tap_share_without_starting_download():
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
            setAttribute() {{}}, removeAttribute() {{}},
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
          assert.match(SHARE_HTML, /id="result-actions"/);
          assert.match(SHARE_HTML, /id="share-save"/);
          elements = {{
            "share-save": button(),
            "share-hint": {{ textContent: "" }}
          }};
          bindShare("/video.mp4");
          await prepareVideoShare("/video.mp4", videoElement());
          assert.equal(fetchCalls, 1, "desktop prepares the File before one-click sharing");
          assert.equal(elements["share-save"].textContent, "共有");
          assert.equal(elements["share-save"].hidden, false);

          assert.equal(downloads, 0);
          assert.equal(shareCalls.length, 0, "download must not open the share sheet");

          elements["share-save"].handlers.click();
          await Promise.resolve();
          assert.equal(downloads, 0, "share must not start a download");
          assert.equal(shareCalls.length, 1);
          assert.equal(shareCalls[0].files[0].name, "video.mp4");
          assert.equal(shareCalls[0].text.includes("#Soramimic"), true);
          assert.equal(shareCalls[0].text.includes("#そらみみっく"), true);

          navigator.share = () => Promise.reject({{ name: "NotAllowedError" }});
          elements["share-save"].handlers.click();
          await new Promise((resolve) => setImmediate(resolve));
          assert.equal(elements["share-save"].hidden, false,
            "sharing remains separate from saving after a rejected attempt");
          assert.equal(downloads, 0);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_video_share_preparation_blocks_controls_until_source_is_ready():
    """sourceのない準備中は操作させず、準備後は同じFileで再生・共有する。

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
        let blockFetch = false;
        let releaseFetch = null;
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
          if (blockFetch) await new Promise((resolve) => {{ releaseFetch = resolve; }});
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
            src: "", controls: true, loadCalls: 0, pauseCalls: 0,
            handlers: {{}}, attributes: {{}},
            load() {{ this.loadCalls += 1; }},
            pause() {{ this.pauseCalls += 1; }},
            setAttribute(name, value) {{ this.attributes[name] = value; }},
            removeAttribute(name) {{
              if (name === "src") this.src = "";
              delete this.attributes[name];
            }},
            addEventListener(type, fn) {{ this.handlers[type] = fn; }},
            removeEventListener(type, fn) {{
              if (this.handlers[type] === fn) delete this.handlers[type];
            }},
            emit(type) {{ if (this.handlers[type]) this.handlers[type](); }}
          }};
        }}

        (async () => {{
          assert.equal(FILE_SHARE_SUPPORTED, true, "iPhone keeps native file sharing");
          assert.match(SHARE_HTML, /id="result-actions"/);
          assert.doesNotMatch(SHARE_HTML, /動画を保存・共有/);

          elements = {{ "share-save": button(), "share-hint": {{ textContent: "" }} }};
          bindShare("/video.mp4");
          const video = videoElement();
          blockFetch = true;
          const preparation = prepareVideoShare("/video.mp4", video);
          assert.equal(video.controls, false,
            "controls must stay hidden while the video has no source");
          assert.equal(video.attributes["aria-busy"], "true");
          releaseFetch();
          await preparation;
          blockFetch = false;
          assert.equal(fetchCalls, 1);
          assert.ok(fetchSignal, "share preparation must be abortable");
          assert.equal(video.src, "blob:shared-video/video.mp4");
          assert.equal(video.loadCalls, 1);
          assert.equal(video.controls, true,
            "controls become available only after the source is loaded");
          assert.equal(video.attributes["aria-busy"], undefined);
          assert.equal(elements["share-save"].textContent, "共有");
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
          assert.equal(elements["share-save"].textContent, "共有");

          navigator.share = () => Promise.reject({{ name: "NotAllowedError" }});
          activation = true;
          elements["share-save"].handlers.click();
          activation = false;
          await new Promise((resolve) => setImmediate(resolve));
          assert.equal(downloads, 0, "share failure must not auto-download");
          assert.equal(elements["share-save"].textContent, "共有");
          assert.match(messages.at(-1), /共有メニュー/);

          assert.equal(preparedVideoShare.file, objectUrlFile,
            "a rejected attempt must retain the File for another explicit share");
          assert.equal(preparedVideoShare.shareable, false,
            "the Save menu may download directly after a rejected share");

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
          assert.equal(mediaFallbackVideo.controls, true);
          assert.equal(mediaFallbackVideo.attributes["aria-busy"], undefined);
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
          assert.equal(fallbackVideo.controls, true);
          assert.equal(fallbackVideo.attributes["aria-busy"], undefined);
          assert.equal(elements["share-save"].textContent, "共有");
          assert.equal(downloads, 0, "preparation failure must not auto-download");
          assert.equal(elements["share-save"].hidden, true);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_completed_video_prepares_one_shared_playback_file_and_reset_aborts_fetch():
    """完成時に共有Fileを準備して再生にも共用し、離脱時は取得を止める。"""
    script = _script()
    shown = _function_body(script, "function showBuilderVideo(job)")
    assert "prepareVideoShare(job.video_url, video)" in shown
    assert "if (!FILE_SHARE_SUPPORTED) {" in shown
    assert "video.src = qs(job.video_url);" in shown
    clicked = _function_body(script, "function bindShare(videoUrl)")
    assert "prepareVideoShare" not in clicked
    reset = _function_body(script, "function resetVideoSharePreparation()")
    assert "sharePreparationAbort.abort()" in reset
    assert "revokeSharePlaybackUrl()" in reset
    prepared = _function_body(script, "async function prepareVideoShare(videoUrl, video)")
    assert prepared.index("video.controls = false") < prepared.index("await fetch(")
    assert prepared.index("video.src = sharePlaybackUrl") < prepared.index(
        "video.controls = true"
    )
    state = _function_body(script, "function setBuilderState(state)")
    assert state.index('video.removeAttribute("src")') < state.index(
        "resetVideoSharePreparation()"
    )
    assert state.index('video.removeAttribute("aria-busy")') < state.index(
        "resetVideoSharePreparation()"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_each_completed_video_gets_fresh_paused_native_controls():
    """2回目の完成動画が前回の停止アイコンを引き継がない。"""
    fresh = _function_body(_script(), "function freshBuilderVideo()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");

        function videoElement(generation) {{
          return {{
            generation, paused: generation === 0 ? false : true,
            pauseCalls: 0, loadCalls: 0,
            pause() {{ this.pauseCalls += 1; this.paused = true; }},
            load() {{ this.loadCalls += 1; this.paused = true; }},
            removeAttribute() {{}},
            cloneNode() {{ return videoElement(this.generation + 1); }},
            replaceWith(next) {{ currentVideo = next; }},
          }};
        }}

        let currentVideo = videoElement(0);
        let shareResets = 0;
        const $ = () => currentVideo;
        const resetVideoSharePreparation = () => {{ shareResets += 1; }};
        {fresh}

        const first = freshBuilderVideo();
        assert.notEqual(first.generation, 0);
        assert.equal(first.paused, true);
        const second = freshBuilderVideo();
        assert.notEqual(second, first,
          "a repeated generation must not reuse native media controls");
        assert.equal(second.paused, true,
          "the second completed video must initially show the play control");
        assert.equal(shareResets, 2);
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_completed_video_refreshes_controls_before_loading_its_source():
    """直接URL・共有Blobのどちらも新しいvideo要素へ読み込む。"""
    shown = _function_body(_script(), "function showBuilderVideo(job)")
    assert shown.index("freshBuilderVideo()") < shown.index("video.src")
    assert 'if (!FILE_SHARE_SUPPORTED) {' in shown
    assert shown.index("video.src = qs(job.video_url)") < shown.index("video.load()")
    assert shown.index("freshBuilderVideo()") < shown.index(
        "prepareVideoShare(job.video_url, video)"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_cancel_job_shows_pending_feedback_and_recovers_from_request_failure():
    """中断POSTの応答待ちでも即座に反応し、失敗時だけ再操作可能に戻す。"""
    script = _script()
    set_pending = _function_body(script, "function setCancelPending(pending)") + "\n}"
    cancel = _function_body(script, "async function cancelJob()") + "\n}"
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const elements = {{
          cancel: {{ disabled: false, textContent: "中断" }},
          "builder-cancel": {{ disabled: false, textContent: "中断" }},
        }};
        const $ = (id) => elements[id];
        const headers = () => ({{}});
        const statuses = [];
        const messages = [];
        const restarted = [];
        const setJobStatus = (text) => statuses.push(text);
        const showBuilderMsg = (text) => messages.push(text);
        const restartPolling = (id) => restarted.push(id);
        let currentJob = "job-1";
        let cancelPendingJob = null;
        let fetchCalls = 0;
        let resolveFetch;
        let fetch = () => {{
          fetchCalls += 1;
          return new Promise((resolve) => {{ resolveFetch = resolve; }});
        }};
        {set_pending}
        {cancel}

        (async () => {{
          const pending = cancelJob();
          assert.equal(fetchCalls, 1);
          assert.equal(statuses.at(-1), "中断しています…");
          for (const button of Object.values(elements)) {{
            assert.equal(button.disabled, true);
            assert.equal(button.textContent, "中断中…");
          }}
          await cancelJob();
          assert.equal(fetchCalls, 1, "pending cancellation must ignore a second click");

          resolveFetch({{ ok: true, status: 200 }});
          await pending;
          assert.deepEqual(restarted, ["job-1"]);
          assert.equal(elements.cancel.disabled, true,
            "successful request stays disabled until polling reaches a terminal state");

          currentJob = "job-2";
          cancelPendingJob = null;
          setCancelPending(false);
          fetch = async () => ({{ ok: false, status: 503 }});
          await cancelJob();
          assert.equal(statuses.at(-1), "中断要求を送れませんでした");
          assert.match(messages.at(-1), /もう一度お試しください/);
          for (const button of Object.values(elements)) {{
            assert.equal(button.disabled, false);
            assert.equal(button.textContent, "中断");
          }}

          fetch = async () => ({{ ok: true, status: 200 }});
          await cancelJob();
          assert.equal(messages.at(-1), "",
            "retry must clear the previous cancellation request error");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)

    poll = _function_body(script, "async function poll(")
    assert 'cancelPendingJob === id' in poll
    assert 'setJobStatus("中断しています…")' in poll
    finish = _function_body(script, "function finish()")
    assert "cancelPendingJob = null;" in finish
    assert "setCancelPending(false);" in finish
    assert 'showBuilderMsg("");' in _function_body(script, "function showProgress()")


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


def test_restored_sample_id_refreshes_only_unedited_lyrics():
    """復元したサンプルIDを選び直し、手編集済みの歌詞だけ残す。"""
    init = _function_body(_script(), "async function initBuilder()")
    assert "sampleLyricsId === restoredId" in init
    assert "sampleLyricsBaseline !== null" in init
    assert '$("lyrics").value !== sampleLyricsBaseline' in init
    assert "applySample({ keepLyrics: editedLyrics })" in init


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


def test_sample_selection_never_fetches_or_injects_midi():
    """同梱サンプルはIDだけ保持し、MIDIバイナリをブラウザへ配らない。"""
    apply_sample = _function_body(_script(), "async function applySample(")
    assert "/midi" not in apply_sample
    assert "midiSampleId = sid;" in apply_sample
    assert '$("midi").value = "";' in apply_sample
    assert "injectFile" not in apply_sample


def test_sample_requests_send_id_while_uploads_send_midi():
    """生成・検証・エディタはサンプルIDと持ち込みMIDIを排他的に送る。"""
    script = _script()
    submit = _function_body(script, "async function submitJob(")
    check = _function_body(script, "async function checkMidi()")
    editor = _function_body(script, "async function convertAndOpenEditor()")
    reseed = _function_body(script, "async function reseedEditorSong()")
    for body in (submit, editor, reseed):
        assert 'if (midiSampleId) form.append("sample_id", midiSampleId);' in body or (
            'if (sampleId) form.append("sample_id", sampleId);' in body
        )
        assert 'else form.append("midi", midi);' in body
    assert 'if (sampleId) form.append("sample_id", sampleId);' in check
    assert 'else form.append("midi", f);' in check


def test_sample_id_is_saved_without_sample_midi_binary():
    script = _script()
    save = _function_body(script, "function saveForm()")
    apply_sample = _function_body(script, "async function applySample(")
    assert "sampleId: midiSampleId," in save
    assert "saveForm();" in apply_sample
    assert 'localStorage.setItem(MIDI_KEY' not in apply_sample


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
    body = _function_body(script, "async function applyEditorWordlist()")
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
    assert 'JSON.stringify(data, null, 2)' in body
    assert 'text === $("lyrics").value' in body
    assert '$("lyrics").value = text;' in body
    assert 'syncLyricsInput(false);' in body
    assert '$("lyrics").dispatchEvent(new Event("change", { bubbles: true }));' in body
    # 単語リストの書き戻しと同じ経路(ポーリングと「閉じる」)で拾う
    sync = _function_body(script, "async function syncEditorSessionOnce()")
    assert "applyEditorLyrics();" in sync
    # 来歴(editorProvenance)は元歌詞を見ないので、書き戻しで編集が捨てられない
    prov = _function_body(script, "function editorProvenance()")
    assert "lyrics" not in prov
    # 元歌詞はエディタへのシードにも載る(サーバーが入れるので送るだけ)
    for fn in ("async function convertAndOpenEditor()", "async function reseedEditorSong()"):
        assert "appendSongLyrics(form);" in _function_body(script, fn)


def test_card_selects_mirror_the_canonical_form():
    """カードのプルダウンは正本の写し。選択肢も値も片方向に写して二重管理しない。"""
    script = _script()
    opts = _function_body(script, "function syncBuilderOptions()")
    # 選択肢は optgroup ごとそのまま複製する(表示名の付け直しをしない)
    assert '$("builder-sample").innerHTML = $("sample-select").innerHTML;' in opts
    assert '$("builder-wordlist").innerHTML = wl.innerHTML;' in opts
    # 同梱リストの設定が無くても通常UIでは自作リストを選べる
    assert '$("builder-wordlist-field").hidden = wl.hidden && simpleMode;' in opts
    assert "syncBuilderValues();" in opts
    # カード → 正本 → change の順(既存の applySample / applyWordlistSelection を通す)
    wiring = script[script.index('$("builder-sample").addEventListener'):]
    wiring = wiring[: wiring.index('$("sample-select").addEventListener')]
    assert '$("sample-select").value = $("builder-sample").value;' in wiring
    assert 'sel.dispatchEvent(new Event("change", { bubbles: true }));' in wiring


def test_custom_wordlists_use_the_soramimic_row_menu():
    """統合メニューに組み込みリストと、自作リストの作成・行操作をまとめる。"""
    html = INDEX.read_text(encoding="utf-8")
    script = _script()
    assert 'id="custom-wordlist-menu" class="custom-wordlist-menu" role="menu"' in html
    assert 'id="custom-wordlist-edit"' not in html

    assert 'id="builder-wordlist-trigger" class="builder-wordlist-trigger"' in html
    assert '<select id="builder-wordlist" hidden aria-hidden="true" tabindex="-1"></select>' in html

    render = _function_body(script, "function renderCustomListMenu()")
    assert 'for (const child of $("builder-wordlist").children)' in render
    assert "appendBuilderWordlistOption(menu, option, true);" in render
    assert 'appendCustomListMenuHeading(menu, "自作リスト");' in render
    assert render.index('for (const child of $("builder-wordlist").children)') < render.index(
        'appendCustomListMenuHeading(menu, "自作リスト");'
    )
    assert 'add.textContent = "＋ 新しいリスト";' in render
    assert (
        'row.className = "custom-wordlist-menu-row custom-wordlist-menu-subitem";' in render
    )
    assert 'choose.setAttribute("role", "menuitemradio");' in render
    assert 'choose.setAttribute("aria-checked", String(list.id === activeCustomListId));' in render
    assert 'edit.setAttribute("aria-label", `「${list.name}」を編集`);' in render
    assert 'remove.setAttribute("aria-label", `「${list.name}」を削除`);' in render

    opening = _function_body(script, "async function openCustomListMenu(")
    assert 'menu.querySelector("[role=\'menuitemradio\'][aria-checked=\'true\']")' in opening
    assert '$("builder-wordlist-trigger").setAttribute("aria-expanded", "true");' in opening
    wiring = script[script.index('$("builder-wordlist-trigger").addEventListener("click"') :]
    wiring = wiring[: wiring.index('// 正本(詳細設定の曲')]
    assert 'openCustomListMenu({ focus: false });' in wiring
    assert 'event.key !== "ArrowDown" && event.key !== "ArrowUp"' in wiring
    assert 'if (event.key === "Escape")' in script
    assert 'if (event.key === "ArrowDown")' in script


def test_public_mode_shows_private_use_notice():
    setup = _function_body(_script(), "function setupPublicMode(conf)")
    assert 'publicMode = !!conf.public;' in setup
    assert 'if (!publicMode) return;' in setup
    assert '$("upload-use-notice").hidden = false;' in setup


def test_wav_input_reuses_the_builder_and_mobile_player():
    html = INDEX.read_text(encoding="utf-8")
    script = _script()
    assert 'id="song-upload-button"' in html
    assert 'id="song-record-button"' in html
    assert 'id="song-record-status" role="status" hidden' in html
    assert 'id="song-record-playback" controls playsinline preload="metadata" hidden' in html
    assert html.index('id="song-upload-filename"') < html.index('id="song-record-playback"')
    assert html.index('id="song-record-playback"') < html.index('id="song-upload-clear"')
    assert html.index('id="song-upload-button"') < html.index('id="builder-sample"')
    assert '曲をアップロード' in html
    assert 'id="song-upload-selection" hidden' in html
    assert 'id="upload-use-notice" hidden' in html
    assert "生成された動画は原則、" in html
    assert ">私的利用</a>の範囲でご利用ください。" in html
    assert 'href="https://www.bunka.go.jp/seisaku/chosakuken/taisetsu/point/"' in html
    assert 'id="song-upload-filename" role="status"' in html
    assert 'id="song-upload-clear" aria-label="選択した曲を解除"' in html
    assert 'id="recording-delete-dialog"' in html
    assert "録音を削除しますか？" in html
    assert "録音した音声は削除すると元に戻せません。" in html
    assert 'id="recording-delete-hide-warning"' in html
    assert "次から表示しない" in html
    assert 'id="audio-filename"' not in html
    assert '曲ファイルを解除' not in html
    assert '$("song-upload-button").disabled' not in script
    assert ': "歌詞入りMIDIに対応（XF・標準歌詞イベント）";' in script
    assert 'XF MIDIに対応（このサーバーでは音声入力を準備中です）' not in script
    assert 'このサーバーでは音声入力を準備中です' in script
    assert 'accept=".mid,.midi,.wav,.mp3,.m4a,.aac,.flac,.ogg,.oga,.opus,audio/midi"' in html
    assert 'function ownSongKind(' in script
    assert '[".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".webm"]' in script
    assert 'name.endsWith(".mid") || name.endsWith(".midi")' in script
    assert '$("song-upload-button").addEventListener("click", () => $("midi").click());' in script
    assert '$("song-record-button").addEventListener("click", startSongRecording);' in script
    assert 'id="audio-input-panel"' not in html
    assert 'id="auto-lyrics"' in html
    assert (
        'id="auto-lyrics" aria-controls="lyrics-correction-panel lyrics-file-field" checked'
        in html
    )
    assert 'id="lyrics-correction-panel" hidden' in html
    assert '<textarea id="lyrics"' in html
    assert (
        'id="lyrics-file" accept=".txt,.md,.json,text/plain,text/markdown,application/json"'
        in html
    )
    assert "歌詞を自動認識し、音源からメロディーとタイミングを推定します" not in html
    assert "音源解析 → 空耳変換 → 歌声/映像 → 完成" not in html
    assert 'id="audio-analysis-mode"' not in html
    assert "SheetSage2採譜" not in script
    assert 'id="audio-lyrics"' not in html
    assert 'songUploadEntry.addEventListener("drop"' in script
    assert 'files.length !== 1' in script
    assert 'setOwnSongFile(files[0]);' in script
    assert 'songUploadEntry.classList.add("drag-over")' in script
    assert '<video id="builder-video" controls playsinline' in html
    submit = _function_body(script, "async function submitJob(")
    assert 'if (audio) form.append("audio", audio);' in submit
    assert "if (audio && !automaticLyricsEnabled()" in submit
    assert "appendSongLyrics(form);" in submit
    append = _function_body(script, "function appendSongLyrics(")
    assert 'const lyrics = songLyricsForRequest();' in append
    assert 'form.append("lyrics", lyrics);' in append
    assert 'form.append("lyrics_file"' not in append
    assert 'form.append("auto_lyrics", automaticLyricsEnabled() ? "true" : "false");' in append
    automatic = _function_body(script, "function automaticLyricsEnabled()")
    assert 'ownSongKind() !== "audio"' not in automatic
    recognition = _function_body(script, "function syncLyricsRecognition()")
    assert 'const automatic = $("auto-lyrics").checked;' in recognition
    assert '$("auto-lyrics").disabled = false;' in recognition
    assert '$("auto-lyrics-toggle").hidden = false;' in recognition
    audio_change = script[script.index('$("midi").addEventListener("change"') :]
    audio_change = audio_change[: audio_change.index('$("song-upload-clear").addEventListener')]
    assert '$("auto-lyrics").checked = false;' not in audio_change
    clear = _function_body(script, "async function clearSelectedSong()")
    assert '$("midi").value = "";' in clear
    assert '$("song-upload-button").focus();' in clear
    assert 'recordedSongFile && !recordingDeleteWarningHidden()' in clear
    assert '!await confirmRecordedSongDeletion()' in clear
    assert '$("song-upload-clear").addEventListener("click", clearSelectedSong);' in script
    sync = _function_body(script, "function syncBuilderValues()")
    assert '$("song-upload-selection").hidden = !file;' in sync
    assert '$("song-upload-button").hidden = !!file;' in sync
    assert '$("song-upload-hint").hidden = !!file;' in sync
    # 大きなWAVをlocalStorageへ複製しない。保存対象は従来のMIDIだけ。
    assert 'localStorage.setItem("audioFile"' not in script
    save = script[script.index('// 持ち込みMIDIはバイナリ') :]
    assert 'if (ownSongKind(f) !== "midi")' in save


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_browser_recording_becomes_an_m4a_song_file_and_releases_the_microphone():
    """Safari形式の録音を既存の音源入力へ渡し、停止時にマイクも解放する。"""
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function ownSongFile()",
            "function recordingSupported()",
            "function preferredRecordingMimeType()",
            "function recordingExtension(",
            "function recordingFilename(",
            "function recordedFile(",
            "function clearRecordedSongPlayback()",
            "function showRecordedSongPlayback(",
            "function syncRecordingUi()",
            "function setRecordingStatus(",
            "function releaseSongRecording(",
            "function finishSongRecording(",
            "function stopSongRecording(",
            "async function startSongRecording(",
            "function setRecordedSongFile(",
        )
    )
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const elements = new Map();
        function $(id) {{
          if (!elements.has(id)) elements.set(id, {{
            hidden: false, disabled: false, textContent: "", value: "", files: [],
            attrs: {{}}, classList: {{ toggle() {{}} }},
            setAttribute(name, value) {{ this.attrs[name] = value; }},
            removeAttribute(name) {{ delete this.attrs[name]; }},
            pause() {{}}, load() {{}},
            dispatchEvent() {{}},
          }});
          return elements.get(id);
        }}
        class FakeRecorder {{
          static isTypeSupported(type) {{ return type.startsWith("audio/mp4"); }}
          constructor(stream, options) {{
            this.stream = stream;
            this.mimeType = options?.mimeType || "audio/mp4";
            this.state = "inactive";
            this.listeners = new Map();
          }}
          addEventListener(type, listener) {{
            if (!this.listeners.has(type)) this.listeners.set(type, []);
            this.listeners.get(type).push(listener);
          }}
          emit(type, event = {{}}) {{
            for (const listener of this.listeners.get(type) || []) listener(event);
          }}
          start() {{ this.state = "recording"; }}
          stop() {{ this.state = "inactive"; this.emit("stop"); }}
        }}
        const track = {{ stopped: false, stop() {{ this.stopped = true; }} }};
        const stream = {{ getTracks() {{ return [track]; }} }};
        global.window = {{ isSecureContext: true, MediaRecorder: FakeRecorder }};
        const revokedUrls = [];
        global.URL = {{
          createObjectURL() {{ return "blob:recording-preview"; }},
          revokeObjectURL(url) {{ revokedUrls.push(url); }},
        }};
        Object.defineProperty(global, "navigator", {{
          value: {{
            mediaDevices: {{ getUserMedia: async (constraints) => {{
              assert.deepEqual(constraints, {{ audio: true }});
              return stream;
            }} }},
          }},
          configurable: true,
        }});
        global.Event = class {{ constructor(type) {{ this.type = type; }} }};
        let audioInputReady = true;
        let maxSongSeconds = 420;
        let songInputMode = "upload";
        let recordedSongFile = null;
        let recordedSongPreviewUrl = "";
        let preserveRecordedSongFileOnChange = false;
        let activeSongRecording = null;
        let recordingStartSequence = 0;
        {functions}

        (async () => {{
          assert.equal(recordingSupported(), true);
          assert.equal(preferredRecordingMimeType(), "audio/mp4;codecs=mp4a.40.2");
          await startSongRecording();
          assert.equal(activeSongRecording.recorder.state, "recording");
          const recorder = activeSongRecording.recorder;
          recorder.emit("dataavailable", {{ data: new Blob([new Uint8Array([1, 2, 3])]) }});
          stopSongRecording();
          assert.equal(activeSongRecording, null);
          assert.equal(track.stopped, true);
          assert.match(recordedSongFile.name, /^recording-\\d{{8}}-\\d{{6}}\\.m4a$/);
          assert.equal(recordedSongFile.type, "audio/mp4;codecs=mp4a.40.2");
          assert.equal(recordedSongFile.size, 3);
          assert.equal($("song-record-playback").src, "blob:recording-preview");
          assert.equal($("song-record-playback").hidden, false);
          assert.equal($("song-record-status").textContent,
            "録音した音声を曲としてセットしました。");
          clearRecordedSongPlayback();
          assert.equal($("song-record-playback").hidden, true);
          assert.deepEqual(revokedUrls, ["blob:recording-preview"]);
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_recording_delete_requires_confirmation_and_can_hide_future_warnings():
    """録音だけは誤削除を確認し、確定時の選択を次回以降へ保存する。"""
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function recordingDeleteWarningHidden()",
            "function confirmRecordedSongDeletion()",
            "async function clearSelectedSong()",
        )
    )
    node = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const elements = new Map();
        let dialogShows = 0, clearCalls = 0, previewCalls = 0, focused = "";
        function element(id) {{
          const listeners = new Map();
          return {{
            id, value: "", checked: false, returnValue: "", files: [],
            addEventListener(type, callback, options = {{}}) {{
              if (!listeners.has(type)) listeners.set(type, []);
              listeners.get(type).push({{ callback, once: !!options.once }});
            }},
            emit(type) {{
              const current = [...(listeners.get(type) || [])];
              for (const item of current) item.callback();
              listeners.set(type, (listeners.get(type) || []).filter(
                (item) => !item.once || !current.includes(item)));
            }},
            dispatchEvent() {{}},
            showModal() {{ dialogShows += 1; }},
            focus() {{ focused = id; }},
          }};
        }}
        function $(id) {{
          if (!elements.has(id)) elements.set(id, element(id));
          return elements.get(id);
        }}
        const values = new Map();
        const localStorage = {{
          getItem(key) {{ return values.get(key) ?? null; }},
          setItem(key, value) {{ values.set(key, value); }},
        }};
        global.Event = class {{ constructor(type) {{ this.type = type; }} }};
        const RECORDING_DELETE_WARNING_KEY = "soramimic-recording-delete-warning-hidden";
        let recordedSongFile = {{ name: "recording.m4a" }};
        const clearAudioInput = () => {{ clearCalls += 1; recordedSongFile = null; }};
        const syncBuilderValues = () => {{}};
        const schedulePreview = () => {{ previewCalls += 1; }};
        const setRecordingStatus = () => {{}};
        {functions}

        (async () => {{
          const canceled = clearSelectedSong();
          assert.equal(dialogShows, 1);
          $("recording-delete-hide-warning").checked = true;
          $("recording-delete-dialog").returnValue = "cancel";
          $("recording-delete-dialog").emit("close");
          assert.equal(await canceled, false);
          assert.equal(clearCalls, 0);
          assert.equal(values.size, 0, "canceling must not hide the next warning");

          const confirmed = clearSelectedSong();
          assert.equal(dialogShows, 2);
          $("recording-delete-hide-warning").checked = true;
          $("recording-delete-dialog").returnValue = "delete";
          $("recording-delete-dialog").emit("close");
          assert.equal(await confirmed, true);
          assert.equal(clearCalls, 1);
          assert.equal(values.get(RECORDING_DELETE_WARNING_KEY), "1");
          assert.equal(previewCalls, 1);
          assert.equal(focused, "song-upload-button");

          recordedSongFile = {{ name: "another-recording.m4a" }};
          assert.equal(await clearSelectedSong(), true);
          assert.equal(dialogShows, 2, "saved preference must skip later warnings");
          assert.equal(clearCalls, 2);

          values.clear();
          recordedSongFile = null;
          assert.equal(await clearSelectedSong(), true);
          assert.equal(dialogShows, 2, "uploaded files must keep the existing immediate clear");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_audio_upload_keeps_auto_lyrics_checked_until_user_disables_it():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function ownSongFile()",
            "function automaticLyricsEnabled()", "function activeLyricsJson()",
            "function songLyricsForRequest()",
            "function lyricsInput()",
            "function readParodyText(",
            "function parodySourceLyrics(",
            "function appendSongLyrics(form)",
            "function syncLyricsRecognition()",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const elements = new Map();
        function $(id) {
          if (!elements.has(id)) elements.set(id, {
            checked: false, disabled: false, hidden: false, required: false,
            files: [], value: "", textContent: "",
            setAttribute(name, value) { this[name] = value; },
          });
          return elements.get(id);
        }
        let songInputMode = "upload";
        $("midi").files = [{ name: "voice.wav" }];
        $("auto-lyrics").checked = true;
        $("lyrics").value = "manual lyrics";
        """
    ) + functions + textwrap.dedent(
        """
        syncLyricsRecognition();
        assert.equal($("auto-lyrics").checked, true);
        assert.equal($("auto-lyrics").disabled, false);
        assert.equal($("auto-lyrics-toggle").hidden, false);
        assert.equal($("lyrics-correction-panel").hidden, true);
        assert.equal($("lyrics-file-field").hidden, true);
        assert.equal($("lyrics").required, false);
        assert.equal(automaticLyricsEnabled(), true);
        assert.equal(songLyricsForRequest(), "");
        assert.equal($("adjust-lyrics").disabled, true);
        $("auto-lyrics").checked = false;
        syncLyricsRecognition();
        assert.equal($("lyrics-correction-panel").hidden, false);
        assert.equal($("lyrics-file-field").hidden, false);
        assert.equal($("lyrics").required, true);
        assert.equal(automaticLyricsEnabled(), false);
        assert.equal(songLyricsForRequest(), "manual lyrics");
        assert.equal($("adjust-lyrics-panel").hidden, false);
        assert.equal($("adjust-lyrics").disabled, false);
        let values = new Map();
        const form = {append: (key, value) => values.set(key, value)};
        appendSongLyrics(form);
        assert.equal(values.get("adjust_lyrics"), "false");
        $("adjust-lyrics").checked = true;
        appendSongLyrics(form);
        assert.equal(values.get("adjust_lyrics"), "true");
        $("auto-lyrics").checked = true;
        syncLyricsRecognition();
        assert.equal($("lyrics-file-field").hidden, true);
        assert.equal($("lyrics").value, "manual lyrics",
          "hiding manual input preserves its contents");
        appendSongLyrics(form);
        assert.equal(values.get("adjust_lyrics"), "false");
        $("auto-lyrics").checked = false;
        $("midi").files = [{name: "song.mid"}];
        syncLyricsRecognition();
        appendSongLyrics(form);
        assert.equal($("adjust-lyrics-panel").hidden, true);
        assert.equal(values.get("adjust_lyrics"), "false");
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_lyrics_file_loads_into_editable_textarea_as_paste_alternative():
    script = _script()
    loader = _function_body(script, "async function loadLyricsFile(") + "\n}"
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const elements = new Map();
        function $(id) {
          if (!elements.has(id)) elements.set(id, {
            files: [], value: "", textContent: "", hidden: false,
            dispatchEvent(event) { this.lastEvent = event; },
          });
          return elements.get(id);
        }
        function syncLyricsRecognition() {}
        const sessionStorage = {removeItem() {}};
        const EDITOR_KEY = "editor", EDITOR_SEED_KEY = "seed";
        let editorClears = 0;
        function clearEditorFile() { ++editorClears; }
        class Event {
          constructor(type, options) { this.type = type; this.bubbles = options?.bubbles; }
        }
        let lyricsFileReadSequence = 0, lyricsFileReading = false;
        const MAX_EDITOR_BYTES = 4 * 1024 * 1024;
        const syncLyricsInput = () => clearEditorFile();
        """
    ) + loader + textwrap.dedent(
        """
        (async () => {
          const file = {
            name: "lemon.lyrics.txt",
            size: 128,
            async arrayBuffer() {
              return new TextEncoder().encode(
                "\\uFEFF夢ならばどれほどよかったでしょう\\n"
                  + "未だにあなたのことを夢にみる",
              ).buffer;
            },
          };
          $("lyrics-file").files = [file];
          await loadLyricsFile(file);
          assert.equal(
            $("lyrics").value,
            "夢ならばどれほどよかったでしょう\\n未だにあなたのことを夢にみる",
          );
          assert.equal($("lyrics-file").files[0], file);
          assert.match($("lyrics-file-name").textContent, /歌詞欄に読み込みました/);
          assert.equal($("lyrics-file-name").hidden, false);
          assert.equal($("lyrics").lastEvent.type, "change");
          assert.equal($("lyrics").lastEvent.bubbles, true);
          assert.equal(editorClears, 1);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_lyrics_content_controls_json_and_text_without_stale_imports():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function loadLyricsFile(", "function lyricsInput()",
            "function readParodyText(", "async function readParodyFile(",
            "function parodySourceLyrics(", "function syncLyricsInput(",
            "async function updateParodyStatus(", "function clearEditorFile(",
            "function automaticLyricsEnabled()", "function activeLyricsJson()",
            "function songLyricsForRequest()",
            "function appendSongLyrics(", "function editorSourceForSubmit()",
            "function applyEditorLyrics()",
        )
    )
    input_listener = script[script.index('$("lyrics").addEventListener("input", () => {'):]
    input_listener = input_listener[:input_listener.index("\n});") + len("\n});")]
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const {File} = require("node:buffer");
        const elements = new Map();
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {
            files: [], _value: "", textContent: "", hidden: false, checked: false,
            listeners: new Map(),
            get value() {return this._value;},
            set value(value) {this._value=value; if (!value) this.files=[];},
            addEventListener(type, fn) {this.listeners.set(type, fn);},
            setCustomValidity(message) {this.validationMessage=message;},
            dispatchEvent(event) {
              this.listeners.get(event.type)?.(event);
              if (id === "editor") updateParodyStatus();
            },
          });
          return elements.get(id);
        };
        const stored = new Map();
        const sessionStorage = {
          removeItem:key=>stored.delete(key), setItem:(key,value)=>stored.set(key,value),
        };
        const EDITOR_KEY="editor", EDITOR_SEED_KEY="seed", MAX_EDITOR_BYTES=4*1024*1024;
        let lyricsFileReadSequence=0, lyricsFileReading=false, editorImportedFile=null;
        let editorWordlist=null, editorFileSig="", simpleMode=false, songInputMode="upload";
        let parodyWasRestored=false, parodyRestored=false;
        const syncLyricsRecognition=()=>{}, saveForm=()=>{}, renderParodyStatus=()=>{};
        const syncBuilderValues=()=>{};
        const editorContentSig=data=>JSON.stringify(data.results), editorWhereOf=()=>"";
        const liveEditorEdit=()=>({state:"none"});
        const editorSessionData=()=>JSON.parse(stored.get(EDITOR_KEY)||"null");
        function injectFile(input,file,dispatch) {
          input.files=[file]; if(dispatch)input.dispatchEvent(new Event("change"));
        }
        const settle=()=>new Promise(resolve=>setImmediate(resolve));
        async function choose(text,name) {
          const file=new File([text],name);
          $("lyrics-file").files=[file]; await loadLyricsFile(file); await settle();
        }
        async function type(text) {
          $("lyrics").value=text; $("lyrics").dispatchEvent(new Event("input")); await settle();
        }
        """
    ) + functions + "\n" + input_listener + textwrap.dedent(
        """
        (async()=>{
          const payload={results:[[{surface:"青空"}]],unitsList:[["ア"]],
            phrases:["青い空","白い雲"]};
          const raw=JSON.stringify(payload,null,2);
          for(const name of ["lyrics.json","lyrics.txt","lyrics.md"]) {
            await choose(raw,name);
            assert.equal($("lyrics").value,raw);
            assert.equal($("auto-lyrics").checked,false);
            assert.deepEqual(JSON.parse(await editorSourceForSubmit().file.text()),payload);
            assert.equal(songLyricsForRequest(),"青い空\\n白い雲");
            assert.equal($("lyrics-file").files[0].name,name);
          }
          await type(JSON.stringify({...payload,lyrics:"編集した元歌詞",
            results:[[{surface:"白雲"}]]}));
          assert.equal(songLyricsForRequest(),"編集した元歌詞");
          const changed=JSON.parse(await editorSourceForSubmit().file.text());
          assert.equal(changed.results[0][0].surface,"白雲");
          assert.equal($("lyrics-file").files.length,0);
          // File selection is only an input method: extensions never select the behavior.
          await choose("[Chorus] 通常の歌詞","plain.json");
          assert.equal(songLyricsForRequest(),"[Chorus] 通常の歌詞");
          assert.equal(editorSourceForSubmit().file,null);
          assert.equal(stored.has(EDITOR_KEY),false);
          await type(raw);
          await type('{"broken":');
          assert.equal($("lyrics").value,'{"broken":');
          assert.match($("lyrics").validationMessage,/歌詞JSON/);
          assert.equal(editorSourceForSubmit().file,null);
          assert.match(editorSourceForSubmit().error,/歌詞JSON/);
          assert.equal(songLyricsForRequest(),"");
          await type("普通の歌詞に戻す");
          assert.equal($("lyrics").validationMessage,"");
          assert.equal(editorSourceForSubmit().file,null);
          assert.equal(songLyricsForRequest(),"普通の歌詞に戻す");
          await type(raw);
          $("auto-lyrics").checked=true; syncLyricsInput();
          assert.equal($("lyrics").value,raw);
          assert.equal(editorSourceForSubmit().file,null);
          assert.equal(songLyricsForRequest(),"");
          $("auto-lyrics").checked=false; syncLyricsInput();
          assert.deepEqual(JSON.parse(await editorSourceForSubmit().file.text()),payload);
          const fields=new Map(); appendSongLyrics({append:(key,value)=>fields.set(key,value)});
          assert.equal(fields.get("lyrics"),"青い空\\n白い雲");
          assert.equal(fields.has("lyrics_file"),false);
          // Clearing the picker does not discard the text that it loaded.
          await loadLyricsFile(null);
          assert.equal($("lyrics").value,raw);
          assert(editorSourceForSubmit().file);
          // Slow file reads cannot replace typing or a more recent file.
          let finish;
          const pending=loadLyricsFile({size:10,
            arrayBuffer:()=>new Promise(resolve=>{finish=resolve;})});
          await type("新しい入力");
          finish(new TextEncoder().encode("古い入力").buffer); await pending;
          assert.equal($("lyrics").value,"新しい入力");
          assert.equal(lyricsFileReading,false);
          await choose("{}","invalid.txt");
          assert.equal($("lyrics").value,"{}");
          assert.match($("lyrics").validationMessage,/変換済み/);
          // Editor writeback retains the full converted payload.
          stored.set(EDITOR_KEY,raw); applyEditorLyrics(); await settle();
          assert.deepEqual(JSON.parse($("lyrics").value),payload);
          stored.set(EDITOR_KEY,JSON.stringify({lyrics:"未変換の元歌詞"})); applyEditorLyrics();
          assert.equal($("lyrics").value,"未変換の元歌詞");
          assert.equal(editorSourceForSubmit().file,null);
          simpleMode=true; await type(raw);
          assert.match($("lyrics").validationMessage,/この画面では/);
          assert.equal(editorSourceForSubmit().file,null);
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_editor_transfer_populates_json_and_recognition_mode():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function receiveEditorLyrics()",
            "async function readParodyFile(",
            "function parodySourceLyrics(",
            "function syncLyricsRecognition()",
            "function lyricsInput()",
            "function readParodyText(",
            "function parodySourceLyrics(",
            "function automaticLyricsEnabled()", "function activeLyricsJson()",
            "function songLyricsForRequest()",
            "function appendSongLyrics(",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const {File} = require("node:buffer");
        const elements = new Map();
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {
            files: [], value: "", textContent: "", hidden: false, checked: false,
            setCustomValidity(value) {this.validationMessage=value;},
            dispatchEvent() {}, setAttribute(key, value) {this[key] = value;},
          });
          return elements.get(id);
        };
        const stored = new Map();
        const sessionStorage = {
          setItem: (key, value) => stored.set(key, value),
          removeItem: key => stored.delete(key),
        };
        const location = {href: "https://dev-video.soramimic.com/?lyrics-transfer=test"};
        const EDITOR_KEY = "editor", EDITOR_SEED_KEY = "seed";
        const MAX_EDITOR_BYTES = 4 * 1024 * 1024;
        const editorPeer = new URL("https://dev.soramimic.pages.dev/editor.html");
        let bridgeReceiving = false, simpleMode = false, activeCustomListId = "old";
        let payload, imported, songInputMode = "sample", cleared = 0, saved = 0;
        const SoramimicBridge = {receive: async (peer, apply) => {
          assert.equal(peer, editorPeer); await apply(payload);
        }};
        const clearAudioInput = () => {cleared++;};
        const ownSongFile = () => null;
        const showSongInputMode = mode => {songInputMode = mode;};
        const applyEditorWordlist = async () => {
          assert.deepEqual(JSON.parse(stored.get(EDITOR_KEY)), payload);
        };
        const injectFile = (input, file) => {input.files = [file];};
        const updateParodyStatus = async () => {
          imported = JSON.parse(await $("editor").files[0].text());
        };
        const syncBuilderOptions = () => {}, syncBuilderValues = () => {};
        const updateSongTextPreviews = () => {};
        const saveForm = () => {saved++;};
        """
    ) + functions + textwrap.dedent(
        """
        (async () => {
          const base = {results: [[{surface:"青森", period:[0,1]}]],
            unitsList:[[{pronunciation:"ア"}]]};
          const variants = [
            [{phrases:["青い空", "", "白い雲"]}, "青い空\\n\\n白い雲"],
            [{lyrics:"元の表記\\nを保つ", originalLines:["別の表記"],
              phrases:["ベツノヒョーキ"]}, "元の表記\\nを保つ"],
            [{originalLines:["字幕の歌詞", "次の行"], phrases:["ヨミ"]}, "字幕の歌詞\\n次の行"],
            [{}, ""],
          ];
          for (const [fields, expected] of variants) {
            elements.clear(); stored.clear(); bridgeReceiving = false;
            payload = {...base, ...fields, song:{title:"受け渡しテスト"},
              videoMetadata:{songTitle:"表示用の曲名", songCredit:"作詞・作曲: テスト",
                thumbnailWordlistLabel:"表示用のリスト名"}};
            $("lyrics").value = "前回の歌詞";
            $("auto-lyrics").checked = true;
            $("midi").value = "previous.mid";
            $("sample-select").value = "previous-sample";
            stored.set(EDITOR_SEED_KEY, "old seed");
            await receiveEditorLyrics();
            assert.deepEqual(JSON.parse($("lyrics").value), payload);
            assert.equal($("auto-lyrics").checked, false);
            assert.equal($("lyrics-correction-panel").hidden, false);
            assert.equal($("lyrics-file-field").hidden, false);
            assert.equal($("midi").value, "");
            assert.equal($("sample-select").value, "");
            assert.equal(songInputMode, "upload");
            assert.equal(stored.has(EDITOR_SEED_KEY), false);
            assert.deepEqual(imported, payload, "keep the converted assignments and readings");
            assert.equal($("song-title").value, payload.videoMetadata.songTitle);
            assert.equal($("song-credit").value, payload.videoMetadata.songCredit);
            assert.equal($("thumbnail-wordlist-label").value,
              payload.videoMetadata.thumbnailWordlistLabel);
            const form = new Map();
            appendSongLyrics({append:(key,value) => form.set(key,value)});
            assert.equal(form.get("lyrics"), expected);
            assert.equal(form.get("auto_lyrics"), "false");
            assert.match($("bridge-status").textContent, /歌詞を読み込みました/);
          }
          assert.equal(cleared, variants.length);
          assert.equal(saved, variants.length);
        })().catch(error => {console.error(error); process.exit(1);});
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_sample_picker_uses_each_manifest_title_as_is():
    """一番版とフル版の別項目名をsamples.jsonどおりプルダウンへ出す。"""
    load = _function_body(_script(), "async function loadSamples()")
    render = _function_body(_script(), "function renderSampleOptions(")
    assert "sample.title" in render
    assert 'audioGroup.label = "音源から解析するサンプル";' in render
    assert "option.disabled = audio && !audioInputReady;" in render
    assert "s.edition" not in load


def test_audio_sample_skips_midi_check():
    script = _script()
    check = _function_body(script, "async function checkMidi(")
    assert 'selectedSampleIsAudio() || ownSongKind() === "audio"' in check
    apply = _function_body(script, "async function applySample(")
    assert '$("builder-edit")' not in script
    assert 'link.textContent = "ライセンス";' in apply
    thumbnail = _function_body(script, "function loadThumbnailPreview(")
    assert 'sampleInputKinds[combo.sampleId] === "audio"' not in thumbnail
    assert '? { title: songTitleOf(ownSongFile()), wordlist: combo.wordlistName }' in thumbnail
    assert ': { sample: combo.sampleId, wordlist: combo.wordlistName };' in thumbnail
    host_songs = _function_body(script, "function hostSongList(")
    assert 'sampleInputKinds[o.value] !== "audio"' in host_songs
    init = _function_body(script, "async function initBuilder(")
    assert "available.has(restoredId)" in init


def test_card_wordlist_select_shows_the_editor_own_list():
    """エディタの中で自作リストを使っているあいだも、トリガーとメニューに表示する。

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
    assert "const own = !selectedCustom && showsEditorWordlist();" in body
    assert "if (selectedCustom || own) card.selectedIndex = -1;" in body
    assert 'selectedCustom?.name || (own ? EDITOR_WORDLIST_LABEL : "")' in body
    assert '$("builder-wordlist-trigger-label").textContent = selectedLabel;' in body
    render = _function_body(script, "function renderCustomListMenu()")
    assert "if (showsEditorWordlist())" in render
    assert 'editorList.textContent = EDITOR_WORDLIST_LABEL;' in render
def test_layout_preview_image_needs_a_wordlist_name():
    """レイアウトプレビューの代表画像は、単語リスト名が空なら取りに行かない。

    自作リスト(ORIGINAL/csvText)のあいだは正本 #wordlist が空。空の名前で
    /api/asset-preview を叩くと404になるので、loadWordlistImage と同じ空ガードを
    置いてプレースホルダに落とす。
    """
    script = _script()
    body = _function_body(script, "function leContent(e)")
    assert 'const name = $("wordlist").value.trim();' in body
    assert 'if (name) src = "/api/asset-preview?wordlist=" + encodeURIComponent(name);' in body
    assert "/api/wordlist-image?" not in script
    # 代表画像のもう一方の経路(サムネ)も同じ流儀の空ガードを持つ
    thumb = _function_body(script, "function loadWordlistImage(name, seq)")
    assert "if (!name || hiddenPreviewReason(name)) { hide(); return; }" in thumb


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_fanwork_notice_allows_generation_and_images_without_confirmation():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function updateWordlistUsageNotice()",
            "function editorUsesTextOnly(",
            "function wordlistWhereContains(where, frag)",
            "function loadWordlistImage(name, seq)",
            "function automaticLyricsEnabled()", "function activeLyricsJson()",
            "function songLyricsForRequest()",
            "function lyricsInput()",
            "function readParodyText(",
            "function parodySourceLyrics(",
            "function appendSongLyrics(form)",
            "async function submitJob(previewSec, previewMode)",
            "async function readParodyFile(file)",
            "function lyricReviewCoverage(",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        let selected = "fanwork", songInputMode = "sample";
        let wordlistUsageRequest = null, wordlistUsageAbort = null;
        const currentPreviewCustomList = () => null;
        const headers = () => ({});
        const fetch = async (url, options) => ({ ok: true, json: async () => ({
          required: options.body.get("wordlist") === "vtuber", terms: [],
        }) });
        const currentWordlistName = () => selected;
        const elements = new Map();
        const $ = (id) => {
          if (id === "noncommercial-fanwork") throw new Error("No checkbox exists");
          if (!elements.has(id)) elements.set(id, {
            value: "", hidden: false,
            replaceChildren(...children) { this.children = children; },
          });
          return elements.get(id);
        };
        const document = { createElement: () => ({}) };
        let imageUrl;
        class Image { set src(url) { imageUrl = url; } }
        const previewSeq = 1, hiddenPreviewReason = () => "", apiKey = () => "";
        let submitBusy = false, lyricsFileReading = false;
        const setBusy = (value) => { submitBusy = value; };
        const ownAudioFile = () => null, ownSongKind = () => "sample";
        const samplePending = null, midiChecking = null, midiSampleId = "sample";
        const ensureSelectedSampleMidi = async () => true;
        const MAX_EDITOR_BYTES = 4 * 1024 * 1024;
        let editorFile = null;
        const editorSourceForSubmit = () => ({ file: editorFile, live: false });
        const parodyMismatch = () => true, confirm = () => true;
        const editorWordlist = { name: "fanwork" }, leDirty = true;
        let simpleMode = true;
        const fixedVoicevoxStyle = 6000, fixedSynthesizer = "voicevox", turnstileSiteKey = "";
        const songTitleOf = () => "sample", buildConvertParams = () => "{}";
        const selectedTitleWords = () => '';
        const appendCustomWordlist = () => {}, showSubmitMsg = () => {};
        const activeCustomList = () => selected === "custom" ? {} : null;
        const showsEditorWordlist = () => false;
        let submitAbort = null;
        const showProgress = () => {}, setJobStatus = () => {}, resetTurnstile = () => {};
        const setCancelPending = () => {};
        const watch = () => { submitBusy = false; };
        const requests = [];
        const postJobWithTurnstileRetry = async (form) => {
          requests.push(form);
          return { ok: true, status: 200, json: async () => ({ id: "job" }) };
        };
        """
    ) + functions + textwrap.dedent(
        """
        (async () => {
          for (const name of [
            "vtuber", "scientist", "pokemon", "youtuber", "fanwork", "ordinary", "legacy", "custom",
          ]) {
            selected = name;
            $("wordlist").value = name;
            await updateWordlistUsageNotice();
            assert.equal($("builder-fanwork-notice").hidden, name !== "vtuber");
            const guidance = new URL($("builder-fanwork-guidelines").href, "https://example.com");
            assert.equal(guidance.pathname, "/guidelines");
            assert.equal(guidance.searchParams.get("wordlist"), name);
            loadWordlistImage(name, previewSeq);
            const query = new URL(imageUrl, "https://example.com").searchParams;
            assert.equal(query.get("noncommercial_fanwork"), "true");
            const before = requests.length;
            await submitJob(0, "");
            assert.equal(requests.length, before + 1, "generation must proceed without a check");
            assert.equal(requests.at(-1).get("allow_noncommercial_fanwork"), "true");
          }
          // Uploaded edits can use a different list from the current selection.
          selected = "ordinary";
          simpleMode = false;
          editorFile = new Blob([JSON.stringify({results: [[]], unitsList: [[]]})],
            { type: "application/json" });
          const before = requests.length;
          await submitJob(0, "");
          assert.equal(requests.length, before + 1);
          assert.ok(requests.at(-1).get("editor"));
          assert.equal(requests.at(-1).get("allow_noncommercial_fanwork"), "true");
          $("layout").value = "caption";
          $("layout-json").value = '{"elements": []}';
          for (const name of ["custom", "ordinary"]) {
            selected = name;
            await submitJob(0, "");
            assert.equal(requests.at(-1).get("layout"), name === "custom" ? "" : "caption");
            assert.equal(requests.at(-1).has("layout_json"), false);
            assert.equal(requests.at(-1).get("synthesizer"), "voicevox");
            assert.equal(requests.at(-1).get("voicevox_style"), "6000");
            assert.equal(requests.at(-1).get("auto_octave"), "true");
            assert.equal(requests.at(-1).get("transpose"), "0");
          }

        })().catch((error) => { console.error(error); process.exitCode = 1; });
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_text_only_lyrics_skip_usage_lookup_and_discard_pending_notices():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function updateWordlistUsageNotice()", "function editorUsesTextOnly(",
            "function currentPreviewCustomList()", "function editorTextPreviewList(",
            "function currentWordlistName()",
        )
    )
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        let data = null, simpleMode = false;
        let completed = false;
        process.on("beforeExit", () => assert.equal(completed, true, "finish async checks"));
        let wordlistUsageRequest = null, wordlistUsageAbort = null;
        const editorWordlists = new Set(["vtuber"]);
        const activeLyricsJson = () => data, editorWhereOf = () => "";
        const activeCustomList = () => null, showsEditorWordlist = () => false;
        const apiKey = () => "", headers = () => ({});
        const setCustomWordlistText = (form, text) => form.set("wordlist_text", text);
        const elements = new Map();
        const $ = id => {
          if (!elements.has(id)) elements.set(id, {
            value: id === "wordlist" ? "vtuber" : "", hidden: true, children: [],
            replaceChildren(...children) { this.children = children; },
            appendChild(child) { this.children.push(child); },
          });
          return elements.get(id);
        };
        const document = {createElement: () => ({appendChild() {}})};
        const pending = [];
        const fetch = (url, options) => new Promise((resolve, reject) => {
          pending.push({options, resolve, reject});
        });
        const response = () => ({ok: true, json: async () => ({required: true,
          terms: [{url: "https://example.com/terms", label: "Guidelines"}],
        })});
        """
    ) + functions + textwrap.dedent(
        """
        (async () => {
          for (const simple of [false, true]) {
            simpleMode = simple;
            for (const wordlist of [
              {filepath: "school.csv", text: "学校名"},
              {filepath: "vtuber.csv", textOnly: true},
            ]) {
              data = null;
              const normal = updateWordlistUsageNotice();
              pending.at(-1).resolve(response());
              await normal;
              assert.equal($("builder-fanwork-notice").hidden, false);
              assert.equal($("builder-usage-links").hidden, false);
              data = {wordlist, results: [[{surface: "学校", kana: "ガッコウ"}]]};
              const count = pending.length;
              const textOnly = updateWordlistUsageNotice();
              assert.equal(pending.length, count, "text-only JSON needs no usage request");
              await textOnly;
              assert.equal($("builder-fanwork-notice").hidden, true);
              assert.equal($("builder-usage-status").hidden, true);
              assert.equal($("builder-usage-links").hidden, true);
              assert.equal($("builder-usage-terms").children.length, 0);
            }
          }
          for (const failure of [false, true]) {
            data = null;
            const old = updateWordlistUsageNotice(), request = pending.at(-1);
            data = {wordlist: {filepath: "school.csv"}, results: []};
            await updateWordlistUsageNotice();
            assert.equal(request.options.signal.aborted, true);
            if (failure) request.reject(new Error("network failure"));
            else request.resolve(response());
            await old;
            assert.equal($("builder-fanwork-notice").hidden, true,
              "a stale response must not restore a notice");
          }
          data = {wordlist: {filepath: "vtuber.csv"}};
          const supported = updateWordlistUsageNotice();
          pending.at(-1).reject(new Error("network failure"));
          await supported;
          assert.equal($("builder-fanwork-notice").hidden, false);
          assert.equal($("builder-usage-status").hidden, false,
            "supported image lists must retain lookup failure warnings");
          completed = true;
        })().catch(error => { console.error(error); process.exitCode = 1; });
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


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
    sync = _function_body(script, "async function syncEditorSessionOnce()")
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
    assert 'ev.data.type === "soramimic:request-close"' in handler
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
    assert "lastMidiCheck = f ? { file: f, lines: body.midi_lines || [] } : null;" in body


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


def test_footer_labels_guidance_links():
    html = INDEX.read_text(encoding="utf-8")
    assert '<a href="/guidelines">利用ガイドライン</a>' in html
    assert '<a href="/guidelines#contact-title">画像の権利をお持ちの方へ</a>' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_song_reseed_replaces_original_lyric_tokens():
    reseed = "\n".join(_function_body(_script(), head) + "\n}" for head in (
        "async function reseedEditorSong()", "function parodySourceLyrics(",
    ))
    node = """
        const assert = require('node:assert/strict');
        const EDITOR_KEY = 'editor', midiSampleId = 'sample';
        const ownSongKind = () => 'midi', ownSongFile = () => null;
        const $ = () => ({value: ''}), headers = () => ({});
        const appendSongLyrics = () => {}, appendCustomWordlist = () => {};
        const buildConvertParams = () => '', songTitleOf = () => '';
        const withHostInfo = body => body;
        const markEditorSeed = () => {}, clearEditorFile = () => {};
        const syncBuilderValues = () => {}, renderParodyStatus = () => {}, saveForm = () => {};
        let state, fresh;
        const editorSessionData = () => state;
        const sessionStorage = {setItem: (key, value) => {state = JSON.parse(value);}};
        const fetch = async () => ({ok: true, json: async () => fresh});
    """ + reseed + """
        (async () => {
            state = {phrases: ['フルイ'], tokensList: [['old']], results: [['old']],
                     unitsList: [['old']], originalLines: ['old'],
                     wordlist: {value: 'STATION'}, param: {VOWEL_RATIO: .8}};
            const tokens = [[{surface_form: '手', pronunciation: 'テ', phrase: 0},
                             {surface_form: 'を', pronunciation: 'オ', phrase: 0},
                             {surface_form: '打つ', pronunciation: 'ウツ', phrase: 1}]];
            fresh = {phrases: ['テオウツ'], tokensList: tokens, lyrics: '手を打つ'};
            await reseedEditorSong();
            assert.deepEqual(state.tokensList, tokens);
            assert.equal(state.wordlist.value, 'STATION');
            assert.equal(state.param.VOWEL_RATIO, .8);
            assert.ok(!state.results && !state.unitsList && !state.originalLines);
            fresh = {phrases: ['ツギ']};
            await reseedEditorSong();
            assert.ok(!state.tokensList, 'stale tokens survived a token-less seed');
        })().catch(error => {console.error(error); process.exit(1);});
    """
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_difficult_audio_result_links_to_song_generation_tips():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="generation-quality-warning"' in html
    assert "全体的にうまく生成できていない可能性があります。" in html
    assert 'href="/guidelines#generation-tips"' in html
    assert '$("generation-quality-warning").hidden = !job.generation_quality_warning;' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_wordlist_exhaustion_notice_follows_the_completed_result_and_resets():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "async function poll(", "function setBuilderState(",
            "function showWordlistExhaustionNotice(", "function historyCard(",
            "function appendLyricEditAction(",
            "function appendSaveMenu(", "function prefersNativeVideoSave(",
            "function isIOSPlatform(",
        )
    )
    message = next(line for line in script.splitlines()
                   if line.startswith("const WORDLIST_EXHAUSTED_NOTICE ="))
    node = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        function element() {
          return {hidden: true, textContent: "", children: [],
            getAttribute() { return null; }, removeAttribute() {}, setAttribute() {},
            pause() {}, load() {}, addEventListener() {},
            append(...children) { this.children.push(...children); },
            prepend(child) { this.children.unshift(child); },
            appendChild(child) { this.children.push(child); }};
        }
        const elements = new Map();
        const $ = (id) => {
          if (!elements.has(id)) elements.set(id, element());
          return elements.get(id);
        };
        const document = {createElement: element}, navigator = {};
        const historyButton = element;
        const historyCreatedLabel = () => "", historyStatusLabel = () => "";
        let builderState = "preview", pollSeq = 1, currentJob = "job", pollLastAt = 0;
        let pollFailures = 0, reviewJobId = null;
        const POLL_FETCH_TIMEOUT_MS = 1000, POLL_FATAL_STATUS = [404], STAGE_LABELS = {};
        const headers = () => ({}), qs = (url) => url;
        const renderStages = () => {}, renderBuilderBar = () => {}, finish = () => {};
        const setJobStatus = () => {}, resetVideoSharePreparation = () => {};
        const setPreviewPending = () => {}, updateBuilderOverlay = () => {};
        const showBuilderVideo = () => setBuilderState("done");
        let result = {status: "done", params: {}, total_seconds: 1,
          result_kind: "video", playback_url: "/video", wordlist_exhausted: true};
        const fetch = async () => ({ok: true, status: 200, json: async () => result});
        const texts = (node) => [node.textContent, ...node.children.flatMap(texts)];
        """
    ) + message + functions + textwrap.dedent(
        """
        (async () => {
          await poll("job");
          const notice = $("wordlist-exhausted-notice");
          assert.equal(notice.hidden, false);
          assert.ok(notice.textContent.length > 0);
          assert.ok(texts(historyCard(result, 0)).includes(notice.textContent));
          for (const state of ["preview", "running"]) {
            setBuilderState(state);
            assert.equal(notice.hidden, true);
            assert.equal(notice.textContent, "");
            await poll("job");
            assert.equal(notice.hidden, false);
          }
          delete result.wordlist_exhausted; // Next result and older saved jobs.
          await poll("job");
          assert.equal(notice.hidden, true);
          assert.equal(notice.textContent, "");
          assert.ok(!texts(historyCard(result, 0)).includes(WORDLIST_EXHAUSTED_NOTICE));
        })().catch((error) => { console.error(error); process.exitCode = 1; });
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_image_credit_cards():
    subprocess.run(
        ["node", "tests/image-credits.mjs"], cwd=INDEX.parents[3],
        check=True, text=True, capture_output=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_legacy_song_credit_keeps_both_saved_fields():
    function = _function_body(_script(), "function legacySongCredit(state)") + "\n}"
    node = function + textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        assert.equal(legacySongCredit({
          songTitle: "曲", originalCredit: "作者", creditNotice: "© 指定表記",
        }), "曲\\n作者\\n© 指定表記");
        assert.equal(legacySongCredit({
          songTitle: "曲", originalCredit: "同じ表記", creditNotice: "同じ表記",
        }), "曲\\n同じ表記");
        assert.equal(legacySongCredit({}), "");
        """
    )
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_json_lyrics_lock_wordlist_and_adjustment_without_changing_plain_selection():
    script = _script()
    functions = "\n".join(_function_body(script, name) + "\n}" for name in (
        "function automaticLyricsEnabled()", "function lyricsInput()",
        "function readParodyText(", "function activeLyricsJson()",
        "function syncJsonInputControls()", "function syncLyricsRecognition()",
        "function currentWordlistName()", "function currentPreviewCustomList()",
        "function editorUsesTextOnly(", "function editorTextPreviewList(",
    ))
    node = textwrap.dedent('''
        const assert = require("node:assert/strict");
        const elements = new Map(), facet = {disabled:false};
        function $(id) {
          if(!elements.has(id)) elements.set(id, {
            value:"", checked:false, hidden:false, selectedOptions:[],
            querySelectorAll:()=>[facet], setAttribute(){},
          });
          return elements.get(id);
        }
        const MAX_EDITOR_BYTES=4*1024*1024, simpleMode=false, wordlistGroups=[];
        const editorWordlists=new Set(["stations","food"]);
        let songInputMode="upload", customListBusy=false, menuClosed=false;
        const ownSongFile=()=>({name:"song.wav"});
        const closeCustomListMenu=focus=>{assert.equal(focus,false);menuClosed=true;};
        const activeCustomList=()=>null, showsEditorWordlist=()=>false;
        const editorSessionWordlist=()=>null;
        $("wordlist").value="stations";
        $("wordlist-select").hidden=true;
    ''') + functions + textwrap.dedent('''
        const data={phrases:["歌詞"],results:[[]],unitsList:[[]],
          wordlist:{text:"食べ物",filepath:"wordlists/food.csv"}};
        $("lyrics").value=JSON.stringify(data);
        $("adjust-lyrics").checked=true;
        syncJsonInputControls();syncLyricsRecognition();
        assert.equal($("builder-wordlist-trigger").disabled,true);
        assert.equal($("wordlist-select").disabled,true);
        assert.equal($("builder-wordlist-trigger-label").textContent,"食べ物（歌詞JSON）");
        assert.equal(facet.disabled,true);
        assert.equal($("adjust-lyrics").disabled,true);
        assert.equal(currentWordlistName(),"food");
        assert.equal(currentPreviewCustomList(),null);
        assert.equal($("builder-text-only-notice").hidden,true);
        const before=JSON.stringify(data);
        editorWordlists.delete("food");
        syncJsonInputControls();
        assert.equal($("builder-text-only-notice").hidden,false);
        assert.equal(currentWordlistName(),"");
        assert.equal(currentPreviewCustomList().name,"食べ物");
        assert.equal(currentPreviewCustomList().text,"id,original,surface,pronunciation");
        assert.equal(JSON.stringify(data),before,"keep the imported JSON unchanged");
        assert(menuClosed);
        assert.equal($("wordlist").value,"stations");
        data.wordlist={value:"ORIGINAL",text:"自作",csvText:"ねこ,ネコ"};
        $("lyrics").value=JSON.stringify(data);
        assert.equal(currentWordlistName(),"");
        assert.deepEqual(currentPreviewCustomList(),{name:"自作",text:"ねこ,ネコ"});
        $("lyrics").value="通常の歌詞";
        $("wordlist").value="stations";
        syncJsonInputControls();syncLyricsRecognition();
        assert.equal($("builder-text-only-notice").hidden,true);
        assert.equal($("builder-wordlist-trigger").disabled,false);
        assert.equal(facet.disabled,false);
        assert.equal($("adjust-lyrics").disabled,false);
        assert.equal(currentWordlistName(),"stations");
        customListBusy=true;syncJsonInputControls();
        assert.equal($("builder-wordlist-trigger").disabled,true);
        customListBusy=false;$("lyrics").value=JSON.stringify(data);
        $("auto-lyrics").checked=true;syncJsonInputControls();
        $("wordlist").value="stations";
        assert.equal($("builder-wordlist-trigger").disabled,false);
        assert.equal(currentWordlistName(),"stations");
    ''')
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)


def test_save_menu_groups_available_formats_and_closes_after_selection():
    functions = '\n'.join(_function_body(_script(), name) + '\n}' for name in (
        'function appendSaveMenu(', 'function prefersNativeVideoSave(', 'function isIOSPlatform(',
    ))
    node = textwrap.dedent('''
        const assert = require('node:assert/strict');
        function element(tag) {
          return {tag, children: [], handlers: {}, open: false, setAttribute() {},
                  addEventListener(type, fn) {this.handlers[type] = fn;},
                  appendChild(child) { this.children.push(child); }};
        }
        const document = {createElement: element};
        const navigator = {};
        const qs = url => url + '?api_key=test';
        const linksIn = node => [
          ...(node.tag === 'a' ? [node] : []), ...node.children.flatMap(linksIn),
        ];
    ''') + functions + textwrap.dedent('''
        const root = element('div');
        appendSaveMenu(root, {});
        assert.equal(root.children.length, 0);
        const urls = {musicxml: '/score', midi: '/mid', xf: '/xf', vocal: '/wav'};
        appendSaveMenu(root, {video_url: '/video.mp4',
                              export_urls: {...urls, unknown: '/unknown'}});
        const menu = root.children[0];
        assert.equal(menu.open, false, 'singing data stays inside the closed Save menu');
        assert.equal(menu.children[0].textContent, '保存');
        assert.deepEqual(menu.children[1].children.map(section => section.children[0].textContent),
                         ['動画', '歌唱データ']);
        const links = linksIn(menu);
        assert.equal(links.length, 5);
        assert.deepEqual(links.map(link => link.href),
                         ['/video.mp4', '/wav', '/score', '/mid', '/xf'].map(url => qs(url)));
        assert.deepEqual(links.map(link => link.textContent),
                         ['MP4', '歌声WAV（伴奏なし）', '楽譜（MusicXML）',
                          '歌詞入りMIDI', 'XF MIDI']);
        assert(links.every(link => link.download === ''));
        menu.open = true;
        links[1].handlers.click({preventDefault() {throw Error('must download');}});
        assert.equal(menu.open, false);
        const old = element('div');
        appendSaveMenu(old, {video_url: '/old.mp4'});
        assert.equal(linksIn(old).length, 1);
        const audio = element('div');
        appendSaveMenu(audio, {video_url: '/audio', result_kind: 'audio'});
        assert.equal(linksIn(audio)[0].textContent, '歌声プレビュー（WAV）');
        assert.equal(audio.children[0].children[1].children[0].children[0].textContent,
                     '歌唱データ');
    ''')
    subprocess.run(['node', '-e', node], check=True)


def test_initial_song_and_lyrics_controls_are_disabled_before_scripts_run():
    from lxml import html

    page = html.fromstring(INDEX.read_text(encoding="utf-8"))
    for control_id in (
        "song-upload-button", "song-record-button", "song-upload-clear", "auto-lyrics",
        "lyrics", "lyrics-file", "clear-lyrics-file", "builder-sample", "song-source-switch",
        "builder-wordlist-trigger", "song-title", "song-credit", "title-words",
    ):
        control = page.get_element_by_id(control_id)
        assert control.xpath("ancestor::fieldset[@data-startup-inputs and @disabled]"), control_id
    assert page.get_element_by_id("builder-play").get("disabled") is not None
    assert not page.get_element_by_id("api-key").xpath("ancestor::fieldset[@disabled]")
    assert not page.get_element_by_id("startup-retry").xpath("ancestor::fieldset[@disabled]")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_initialization_waits_for_restored_inputs_and_keeps_failures_locked():
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in ("async function initializePage()", "async function loadConfig()")
    )
    node = r'''
      const assert = require('node:assert/strict');
      const elements = new Map();
      const $ = id => {
        if (!elements.has(id)) elements.set(id, {
          value: '', hidden: false, disabled: false, textContent: '', options: [],
          scrollIntoView() {},
        });
        return elements.get(id);
      };
      const fieldsets = [{disabled: true}, {disabled: true}];
      const document = {querySelectorAll: () => fieldsets};
      const headers = () => ({});
      const SoramimicBridge = {peer: () => ({})};
      let key = '', fetches = 0, fail = '', authenticated = false;
      const apiKey = () => key;
      let editorPeer, simpleMode, launchWordlists, editorWordlists, fixedVoicevoxStyle,
        fixedSynthesizer, maxAudioUploadBytes, maxSongSeconds, audioInputReady,
        resumableAudioUploads, wordlistLayouts, wordlistPhrases;
      let initializationPending = null;
      const deferred = () => {
        let resolve;
        const promise = new Promise(done => {resolve = done;});
        return {promise, resolve};
      };
      let configGate = deferred(), restoreGate = deferred(), sampleGate = deferred();
      let restoring = false, received = false, samplePending = sampleGate.promise;
      const fetch = async () => {
        fetches++;
        await configGate.promise;
        if (fail === 'network') throw new Error('offline');
        if (fail === 'timeout') throw new DOMException('timed out', 'TimeoutError');
        return {ok: fail !== 'http', json: async () => ({
          auth_required: authenticated, models: [], layouts: [], public: true,
        })};
      };
      const refreshCustomLists = async () => {};
      const syncRecordingUi = () => {}, renderSampleOptions = () => {};
      const fillChoiceOptions = () => {}, setChoice = () => {};
      const setupNeutrino = () => {}, setupVoicevox = () => {}, setupPrettyPitch = () => {};
      const toggleSynthOpts = () => {}, setupPublicMode = () => {}, setupTurnstile = () => {};
      const loadWordlistSelect = async () => {}, updateParodyStatus = () => {};
      const applyFixedUiDefaults = () => {}, leUpdateStatus = () => {};
      const initBuilder = async () => {}, updateSongTextPreviews = () => {};
      const loadHistory = async () => [], restoreJobProgress = () => {};
      const showBuilderMsg = () => {};
      const restoreForm = async () => {
        restoring = true;
        await restoreGate.promise;
        $('lyrics').value = 'previous lyrics';
        restoring = false;
      };
      const receiveEditorLyrics = async () => {received = true;};
      __FUNCTIONS__
      const tick = () => new Promise(done => setImmediate(done));
      const locked = () => fieldsets.every(fieldset => fieldset.disabled);
      (async () => {
        $('song-title').disabled = true; // e.g. a sample's read-only metadata
        const first = initializePage(), duplicate = initializePage();
        assert.equal(fetches, 1, 'initialization cannot overlap');
        assert.ok(locked());
        assert.equal($('startup-status').hidden, false);
        assert.equal($('startup-retry').hidden, true);
        configGate.resolve(); await tick();
        assert.equal(restoring, true);
        assert.ok(locked(), 'restoration may not receive edits');
        restoreGate.resolve(); await tick();
        assert.ok(locked(), 'restored sample lyrics must also finish');
        sampleGate.resolve(true); await Promise.all([first, duplicate]);
        assert.equal(received, true);
        assert.equal(locked(), false);
        assert.equal($('startup-status').hidden, true);
        assert.equal($('song-title').disabled, true, 'preserve input-specific restrictions');
        $('lyrics').value = 'new lyrics';
        await tick();
        assert.equal($('lyrics').value, 'new lyrics');

        for (const failure of ['network', 'http', 'timeout']) {
          fail = failure;
          await initializePage();
          assert.ok(locked(), 'failed initialization must not expose stale inputs');
          assert.equal($('startup-status').hidden, false);
          assert.equal($('startup-retry').hidden, false);
          assert.match($('startup-message').textContent, /再試行/);
          assert.equal($('lyrics').value, 'new lyrics');
        }
        fail = ''; authenticated = true;
        await initializePage();
        assert.ok(locked());
        assert.match($('startup-message').textContent, /APIキー/);
        assert.equal($('api-key').disabled, false, 'authentication remains accessible');
        key = 'provided';
        await initializePage();
        assert.equal(locked(), false, 'retry after authentication can finish initialization');
        samplePending = Promise.resolve(false);
        await initializePage();
        assert.ok(locked(), 'failed sample restoration must not unlock the inputs');
        assert.equal($('startup-retry').hidden, false);
      })().catch(error => {console.error(error); process.exit(1);});
    '''.replace("__FUNCTIONS__", functions)
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True)
