"""Shared JavaScript extraction and Node harnesses for UI regression tests."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src/soramimic_video/static/index.html"


def _script() -> str:
    """<script> 以降(=画面のふるまい)だけを返す。"""
    text = INDEX.read_text(encoding="utf-8")
    return text[text.index("<script>") :]


def _function_body(script: str, head: str) -> str:
    """head で始まる関数の中身(次の行頭 } まで)を返す。"""
    body = script[script.index(head) :]
    return body[: body.index("\n}\n")]


def _song_input_node_harness() -> str:
    script = _script()
    functions = "\n".join(
        _function_body(script, head) + "\n}"
        for head in (
            "function ownSongFile()",
            "function syncLyricsRecognition()",
            "function showSongInputMode(",
            "function syncSongInputControls()",
            "function switchSongInputMode(",
            "async function loadLyricsFile(",
            "function syncBuilderValues()", "function songRecordingBusy()",
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
        let editorImportedFile = null, lyricsFileReadSequence = 0, lyricsFileReading = false;
        const MAX_EDITOR_BYTES = 4 * 1024 * 1024;
        const updateBuilderOverlay = () => {}, syncLyricsInput = () => {};
        const originalLyricsFile = () => null;
        let recordedSongFile = null, activeSongRecording = null, recordingRequestPending = false;
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
        const syncRecordingUi = () => { syncSongInputControls(); };
        const clearEditorFile = () => { $("editor").files = []; };
        let previews = 0, saves = 0, builderMessage = "";
        const schedulePreview = () => { previews += 1; };
        const saveForm = () => { saves += 1; };
        const showBuilderMsg = (message) => { builderMessage = message; };
        const sampleTitleOf = (id) => id;
        const document = { querySelector: () => $("analyze-stage") };
        """
    ) + functions + "\n" + sample_change


def _functions(*heads):
    return "\n".join(_function_body(_script(), head) + "\n}" for head in heads)


def _listener(prefix):
    block = _script()[_script().index(prefix):]
    return block[:block.index("\n});") + len("\n});")]


def _run(harness, scenario):
    # Keep Node alive until assertions finish; unresolved promises must fail the test.
    node = harness + """
const deadline = require('node:timers').setTimeout(() => {
  console.error('UI scenario did not finish'); process.exit(1);
}, 4000);
(async () => {
""" + scenario + """
})().then(() => require('node:timers').clearTimeout(deadline)).catch(error => {
  require('node:timers').clearTimeout(deadline); console.error(error); process.exitCode = 1;
});
"""
    subprocess.run(["node", "-e", node], check=True, text=True, capture_output=True, timeout=8)


DOM = r"""
const assert = require('node:assert/strict');
const {File} = require('node:buffer');
const elements = new Map();
const element = () => ({
  _value: '', files: [], children: [], listeners: {}, hidden: false, checked: false,
  textContent: '',
  get value() {return this._value;},
  set value(v) {this._value=v; if (!v) this.files=[];},
  addEventListener(type, fn) {this.listeners[type]=fn;},
  dispatchEvent(e) {return this.listeners[e.type]?.(e);},
  replaceChildren(...items) {this.children=items;},
  appendChild(child) {this.children.push(child);},
  setAttribute(key, value) {this[key]=value;}, focus() {},
});
const $ = id => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => {resolve=done; reject=fail;});
  return {promise, resolve, reject};
};
"""
