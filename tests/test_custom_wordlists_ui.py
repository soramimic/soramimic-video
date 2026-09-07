"""Exercise browser custom-list persistence and generation input selection in Node."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/soramimic_video/static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")

HARNESS = """
const assert = require('node:assert/strict');
const values = new Map();
const storage = {
  getItem: (key) => values.has(key) ? values.get(key) : null,
  setItem: (key, value) => { values.set(key, value); },
};
const { IDBFactory } = require('fake-indexeddb');
const repository = VideoCustomWordlists.createRepository({
  indexedDB: new IDBFactory(), legacyStorage: storage,
});
"""


def run_node(script: str) -> None:
    source = STATIC / "custom-wordlists.js"
    result = subprocess.run(
        ["node", "-e", f"require({json.dumps(str(source))});\n"
         f"(async () => {{\n{HARNESS}\n{script}\n}})()"
         ".catch(err => { console.error(err); process.exitCode = 1; });"],
        cwd=STATIC.parents[2],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def frontend_function(name: str) -> str:
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    match = re.search(rf"^(?:async )?function {name}\([^\n]*\) \{{.*?^\}}", source, re.M | re.S)
    assert match is not None, f"Frontend function {name} is missing"
    return match.group()


def test_indexeddb_storage_and_migration():
    result = subprocess.run(
        ["node", "--test", "tests/custom-wordlists-storage.mjs"],
        cwd=STATIC.parents[2], capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_submission_uses_selected_list_and_clears_builtin_filters():
    functions = "\n".join(frontend_function(name) for name in [
        "activeCustomList", "setCustomWordlistText", "appendCustomWordlist",
    ])
    run_node(functions + """
let simpleMode = false;
const customLists = [
  { id: 'one', name: '同名', text: 'ねこ,ネコ' },
  { id: 'two', name: '同名', text: 'いぬ,イヌ' },
];
let activeCustomListId = 'two';
const form = new Map([['wordlist', 'pokemon'], ['where', 'generation = 1']]);
appendCustomWordlist(form);
assert.equal(form.get('wordlist'), '');
assert.equal(form.get('where'), '');
assert.ok(form.get('wordlist_text') instanceof Blob);
assert.equal(await form.get('wordlist_text').text(), 'いぬ,イヌ');
assert.equal(form.get('wordlist_name'), '同名');
activeCustomListId = 'one';
appendCustomWordlist(form);
assert.equal(await form.get('wordlist_text').text(), 'ねこ,ネコ');
for (const [id, simple] of [['missing', false], ['one', true], ['', false]]) {
  activeCustomListId = id;
  simpleMode = simple;
  const builtin = new Map([['wordlist', 'pokemon'], ['where', 'generation = 1']]);
  appendCustomWordlist(builtin);
  assert.deepEqual(Array.from(builtin.entries()),
    [['wordlist', 'pokemon'], ['where', 'generation = 1']]);
}
""")


def test_editor_provenance_changes_when_list_identity_or_contents_change():
    functions = frontend_function("activeCustomList") + frontend_function("editorProvenance")
    run_node(functions + """
let simpleMode = false;
const customLists = [
  { id: 'one', name: '同名', text: 'ねこ,ネコ' },
  { id: 'two', name: '同名', text: 'ねこ,ネコ' },
];
let activeCustomListId = 'one';
const midiSampleId = 'song';
const elements = { midi: { files: [] }, wordlist: { value: '' }, where: { value: '' } };
const $ = (id) => elements[id];
const buildConvertParams = () => '{}';
const original = editorProvenance();
assert.deepEqual(editorProvenance(), original);
activeCustomListId = 'two';
assert.notDeepEqual(editorProvenance(), original);
activeCustomListId = 'one';
assert.deepEqual(editorProvenance(), original);
customLists[0].text = 'いぬ,イヌ';
assert.notDeepEqual(editorProvenance(), original);
activeCustomListId = '';
assert.notDeepEqual(editorProvenance(), original);
""")


def test_imported_parody_must_match_selected_custom_list_identity_and_content():
    functions = frontend_function("activeCustomList") + frontend_function("parodyMismatch")
    run_node(functions + """
const simpleMode = false;
const customLists = [{ id: 'one', name: '動物', text: 'ねこ,ネコ' }];
const activeCustomListId = 'one';
const elements = {
  editor: { files: [{ name: 'editor.json' }] },
  wordlist: { value: '' }, where: { value: '' },
};
const $ = (id) => elements[id];
let editorWordlist = { name: 'pokemon', where: '' };
assert.equal(parodyMismatch(), true);
editorWordlist = { name: '動物', customId: 'different', customText: 'ねこ,ネコ' };
assert.equal(parodyMismatch(), true);
editorWordlist = { name: '動物' };
assert.equal(parodyMismatch(), true);
editorWordlist = { name: '動物', customId: 'one', customText: 'ねこ,ネコ' };
assert.equal(parodyMismatch(), false);
customLists[0].text = 'いぬ,イヌ';
assert.equal(parodyMismatch(), true);
elements.editor.files = [];
assert.equal(parodyMismatch(), false);
""")


@pytest.mark.parametrize("storage_fails", [False, True])
def test_editor_frame_receives_current_original_csv_before_opening(storage_fails: bool):
    functions = "\n".join(frontend_function(name) for name in [
        "parseJson", "editorSessionWordlist", "showEditorFrame",
    ])
    run_node(functions + f"const storageFails = {json.dumps(storage_fails)};\n" + r"""
const EDITOR_KEY = 'editor-session';
const currentCsv = 'text,yomi\nねこ,ネコ\nいぬ,イヌ';
const sessionStorage = {
  getItem: (key) => key === EDITOR_KEY
    ? JSON.stringify({ wordlist: { value: 'ORIGINAL', csvText: currentCsv } }) : null,
};
const localValues = new Map([['originalWordlist', 'text,yomi\nりんご,リンゴ']]);
const events = [];
const messages = [];
const localStorage = { setItem: (key, text) => {
  if (storageFails) throw new Error('QuotaExceededError');
  localValues.set(key, text);
  events.push('stored');
} };
const frame = {
  set src(value) {
    assert.equal(localValues.get('originalWordlist'), currentCsv);
    assert.equal(value, '/editor/editor.html?embed=video');
    events.push('opened');
  },
  focus: () => events.push('focused'),
};
const wrapper = { hidden: true };
const $ = (id) => ({ 'editor-frame': frame, 'editor-frame-wrap': wrapper })[id];
const document = { body: { classList: { add: () => events.push('modal') } } };
const showBuilderMsg = (text) => messages.push(text);
const hideEditorResume = () => events.push('resume-hidden');
const watchEditorSession = (enabled) => { assert.equal(enabled, true); events.push('watch'); };
let hostRequestSeen = 'old-request';
let hostRequestBusy = true;
showEditorFrame();
if (storageFails) {
  assert.equal(wrapper.hidden, true);
  assert.equal(localValues.get('originalWordlist'), 'text,yomi\nりんご,リンゴ');
  assert.deepEqual(events, []);
  assert.equal(messages.length, 1);
  assert.ok(messages[0]);
} else {
  assert.equal(wrapper.hidden, false);
  assert.deepEqual(events, ['stored', 'resume-hidden', 'opened', 'modal', 'focused', 'watch']);
  assert.equal(hostRequestSeen, '');
  assert.equal(hostRequestBusy, false);
  assert.deepEqual(messages, []);
}
""")


def test_editor_original_edits_update_registered_list_and_its_provenance():
    functions = "\n".join(frontend_function(name) for name in [
        "parseJson", "activeCustomList", "refreshCustomLists", "editorProvenance",
        "editorSessionData", "editorSessionWordlist", "editorWhereOf", "withHostInfo",
        "applyEditorWordlist", "adoptEditorSeedProvenance", "markEditorSeed", "editorContentSig",
    ])
    run_node(functions + r"""
const simpleMode = false;
const customListsRepository = repository;
const first = await repository.save({ name: '動物', text: 'ねこ' });
const other = await repository.save({ name: '動物', text: 'いぬ' });
let customLists = (await repository.lists());
const activeCustomListId = first.id;
const midiSampleId = 'sample-song';
const elements = { midi: { files: [] }, wordlist: { value: '' }, where: { value: '' } };
const $ = (id) => elements[id];
const buildConvertParams = () => '{}';
const hostSongList = () => [{ id: 'sample-song', title: '曲' }];
const showBuilderMsg = (message) => { throw new Error(message); };
let saves = 0;
const saveForm = () => { saves += 1; };
const EDITOR_KEY = 'editor-session';
const EDITOR_SEED_KEY = 'editor-seed';
const sessions = new Map();
const sessionStorage = {
  getItem: (key) => sessions.get(key) ?? null,
  setItem: (key, value) => sessions.set(key, value),
};
const originalCsv = 'text,yomi\nねこ,ネコ';
const payload = { wordlist: { value: 'ORIGINAL', csvText: originalCsv } };
const seed = withHostInfo(payload);
assert.equal(seed.videoCustomListId, first.id);
assert.equal(seed.videoCustomListText, first.text);
assert.equal(seed.videoCustomListCsvText, payload.wordlist.csvText);
assert.equal(payload.videoCustomListId, undefined);
const builtinSeed = withHostInfo({ wordlist: { value: 'pokemon', filepath: 'pokemon.csv' } });
assert.equal(builtinSeed.videoCustomListId, undefined);
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
const originalProvenance = editorProvenance();
markEditorSeed(JSON.stringify(seed));
const originalMetadata = JSON.parse(sessionStorage.getItem(EDITOR_SEED_KEY));
assert.equal(originalMetadata.customListCsvText, originalCsv);

// Merely reopening the normalized CSV does not overwrite the user's original input.
await applyEditorWordlist();
assert.equal((await repository.lists())[0].text, 'ねこ');
assert.equal(saves, 0);

const editedCsv = 'text,yomi\nいぬ,イヌ';
seed.wordlist.csvText = editedCsv;
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
await applyEditorWordlist();
assert.equal((await repository.lists())[0].id, first.id);
assert.equal((await repository.lists())[0].name, first.name);
assert.equal((await repository.lists())[0].text, editedCsv);
assert.deepEqual((await repository.lists())[1], other);
assert.equal(activeCustomList().text, editedCsv);
assert.equal(saves, 1);
const live = JSON.parse(sessionStorage.getItem(EDITOR_KEY));
assert.equal(live.videoCustomListId, first.id);
assert.equal(live.videoCustomListText, editedCsv);
assert.equal(live.videoCustomListCsvText, editedCsv);
const metadata = JSON.parse(sessionStorage.getItem(EDITOR_SEED_KEY));
assert.deepEqual(metadata.from, editorProvenance());
assert.notDeepEqual(metadata.from, originalProvenance);
assert.equal(metadata.sig, originalMetadata.sig);
assert.equal(metadata.customListCsvText, editedCsv);
await applyEditorWordlist();
assert.equal(saves, 1);

// The child retains its initial metadata when undo writes the original CSV again.
assert.equal(seed.videoCustomListCsvText, originalCsv);
assert.equal(seed.videoCustomListText, first.text);
seed.wordlist.csvText = originalCsv;
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
await applyEditorWordlist();
assert.equal((await repository.lists())[0].text, originalCsv);
assert.deepEqual((await repository.lists())[1], other);
assert.equal(activeCustomList().text, originalCsv);
assert.equal(saves, 2);
const undoMetadata = JSON.parse(sessionStorage.getItem(EDITOR_SEED_KEY));
assert.equal(undoMetadata.customListCsvText, originalCsv);
assert.deepEqual(undoMetadata.from, editorProvenance());
assert.equal(undoMetadata.sig, originalMetadata.sig);
await applyEditorWordlist();
assert.equal(saves, 2);

// Later note edits can serialize stale child metadata without changing its CSV.
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
await applyEditorWordlist();
assert.equal(saves, 2);
assert.equal(JSON.parse(sessionStorage.getItem(EDITOR_KEY)).videoCustomListText, originalCsv);

// An unrelated editor session cannot update a registered list with the same display name.
live.videoCustomListId = other.id;
live.wordlist.csvText = 'うさぎ,ウサギ';
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(live));
const before = (await repository.lists());
await applyEditorWordlist();
assert.deepEqual((await repository.lists()), before);
assert.equal(saves, 2);
""")


@pytest.mark.parametrize(
    ("initial", "current", "changed"),
    [
        (("", ""), ("", ""), False),
        (("", ""), ("動物", ""), True),
        (("", ""), ("", "ねこ"), True),
        (("動物", "ねこ"), ("動物", "ねこ"), False),
        (("動物", "ねこ"), ("猫", "ねこ"), True),
        (("動物", "ねこ"), ("動物", "いぬ"), True),
        (("動物", "ねこ"), ("", ""), True),
    ],
)
def test_custom_list_discard_detects_current_unsaved_values(initial, current, changed):
    run_node(f"""
const customListInitialValues = {{name: {json.dumps(initial[0])}, text: {json.dumps(initial[1])}}};
const fields = {{
  'custom-wordlist-name': {{value: {json.dumps(current[0])}}},
  'custom-wordlist-text': {{value: {json.dumps(current[1])}}},
}};
const $ = (id) => fields[id];
{frontend_function('customListHasChanges')}
assert.equal(customListHasChanges(), {json.dumps(changed)});
// Restoring the opening values also clears a prior change without an input event.
fields['custom-wordlist-name'].value = customListInitialValues.name;
fields['custom-wordlist-text'].value = customListInitialValues.text;
assert.equal(customListHasChanges(), false);
""")


def test_custom_list_close_requests_preserve_changes_until_discarded():
    run_node(f"""
let customListBusy = false;
let changed = false;
let focused = false;
const panel = {{open: true, close() {{ this.open = false; }} }};
const discard = {{
  open: false,
  showModal() {{ assert.equal(this.open, false); this.open = true; }},
  close() {{ this.open = false; }},
}};
const $ = (id) => ({{
  'custom-wordlist-panel': panel,
  'custom-wordlist-discard': discard,
  'custom-wordlist-keep': {{focus() {{ focused = true; }} }},
}})[id];
const customListHasChanges = () => changed;
{frontend_function('requestCloseCustomList')}
{frontend_function('closeCustomList')}
requestCloseCustomList();
assert.equal(panel.open, false);
panel.open = true;
changed = true;
customListBusy = true;
requestCloseCustomList();
assert.equal(panel.open, true);
assert.equal(discard.open, false);
customListBusy = false;
requestCloseCustomList();
assert.equal(panel.open, true);
assert.equal(discard.open, true);
assert.equal(focused, true);
requestCloseCustomList(); // Repeated close requests must not reopen the confirmation.
closeCustomList(); // Explicit discard, successful save, or successful deletion.
assert.equal(panel.open, false);
assert.equal(discard.open, false);
""")


def test_pending_editor_sync_drains_the_final_close_request():
    run_node(frontend_function("syncEditorSession") + """
let editorSyncPending = null;
let editorSyncRequested = false;
let finishFirst;
let passes = 0;
const firstCommit = new Promise(resolve => { finishFirst = resolve; });
const syncEditorSessionOnce = async () => {
  passes += 1;
  if (passes === 1) await firstCommit;
};
const showBuilderMsg = message => { throw new Error(message); };
const polling = syncEditorSession();
assert.equal(passes, 1);
const closing = syncEditorSession();
assert.equal(closing, polling);
finishFirst();
await closing;
assert.equal(passes, 2);
assert.equal(editorSyncPending, null);
await syncEditorSession();
assert.equal(passes, 3);
""")


def test_file_import_updates_fields_only_after_validation_and_preserves_typed_name():
    functions = "\n".join(frontend_function(name) for name in [
        "loadCustomListFile", "setCustomWordlistText",
    ])
    run_node(functions + """
let customListBusy = false;
const setCustomListBusy = value => { customListBusy = value; };
const fields = {
  'custom-wordlist-name': { value: '' }, 'custom-wordlist-text': { value: '前の入力' },
};
const $ = id => fields[id];
const messages = [];
const showCustomListMessage = (text, error = false) => messages.push({ text, error });
let rejectFile = false;
const checkCustomList = async form => {
  assert.equal(customListBusy, true);
  assert.equal(await form.get('wordlist_text').text(), 'ねこ,ネコ');
  if (rejectFile) throw new Error('読みを確認してください');
  return { rows: 1 };
};
const file = new File(['ねこ,ネコ'], '動物.csv');
await loadCustomListFile(file);
assert.equal(fields['custom-wordlist-name'].value, '動物');
assert.equal(fields['custom-wordlist-text'].value, 'ねこ,ネコ');
assert.equal(messages.at(-1).error, false);
assert.ok(messages.at(-1).text.includes('動物.csv'));
fields['custom-wordlist-name'].value = '自分の名前';
fields['custom-wordlist-text'].value = '残したい入力';
rejectFile = true;
await loadCustomListFile(file);
assert.equal(fields['custom-wordlist-name'].value, '自分の名前');
assert.equal(fields['custom-wordlist-text'].value, '残したい入力');
assert.equal(messages.at(-1).error, true);
assert.equal(customListBusy, false);
rejectFile = false;
await loadCustomListFile(file);
assert.equal(fields['custom-wordlist-name'].value, '自分の名前');
""")


def test_file_import_ignores_busy_drops_and_rejects_oversized_files_before_reading():
    run_node(frontend_function("loadCustomListFile") + """
let customListBusy = true;
const messages = [];
const showCustomListMessage = (text, error) => messages.push({ text, error });
const file = { size: 10 * 1024 * 1024 + 1, arrayBuffer: () => assert.fail('Must not read') };
await loadCustomListFile(file);
assert.deepEqual(messages, []);
customListBusy = false;
await loadCustomListFile(file);
assert.equal(messages.length, 1);
assert.equal(messages[0].error, true);
assert.ok(messages[0].text.includes('10MB'));
""")
