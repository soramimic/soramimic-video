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
const repository = VideoCustomWordlists.createRepository(storage);
"""


def run_node(script: str) -> None:
    source = STATIC / "custom-wordlists.js"
    result = subprocess.run(
        ["node", "-e", f"require({json.dumps(str(source))});\n{HARNESS}\n{script}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def frontend_function(name: str) -> str:
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    match = re.search(rf"^function {name}\([^\n]*\) \{{.*?^\}}", source, re.M | re.S)
    assert match is not None, f"Frontend function {name} is missing"
    return match.group()


def test_multiple_lists_with_same_name_keep_identity_after_edit_and_reload():
    run_node(r"""
assert.deepEqual(repository.lists(), []);
const first = repository.save({ name: ' 好きなもの ', text: ' りんご,リンゴ\nなし,ナシ ' });
const second = repository.save({ name: '好きなもの', text: 'ねこ,ネコ' });
assert.ok(first.id);
assert.ok(second.id);
assert.notEqual(first.id, second.id);
assert.equal(first.name, '好きなもの');
assert.equal(first.text, 'りんご,リンゴ\nなし,ナシ');
assert.deepEqual(repository.lists(), [first, second]);
const changed = repository.save({ id: first.id, name: '果物', text: 'みかん,ミカン' });
assert.equal(changed.id, first.id);
assert.equal(changed.createdAt, first.createdAt);
assert.deepEqual(repository.lists(), [changed, second]);
const reloaded = VideoCustomWordlists.createRepository(storage);
assert.deepEqual(reloaded.lists(), [changed, second]);
assert.equal(reloaded.lists()[0].text, 'みかん,ミカン');
""")


def test_delete_preserves_other_lists_and_stale_edit_cannot_recreate_deleted_list():
    run_node("""
const first = repository.save({ name: '果物', text: 'りんご,リンゴ' });
const second = repository.save({ name: '動物', text: 'ねこ,ネコ' });
repository.remove(first.id);
assert.deepEqual(repository.lists(), [second]);
const reloaded = VideoCustomWordlists.createRepository(storage);
assert.deepEqual(reloaded.lists(), [second]);
assert.throws(() => reloaded.save({ ...first, text: 'なし,ナシ' }));
assert.deepEqual(reloaded.lists(), [second]);
reloaded.remove(second.id);
assert.deepEqual(repository.lists(), []);
""")


@pytest.mark.parametrize(
    ("name", "text"),
    [("", "ねこ"), (" \n\t", "ねこ"), ("動物", ""), ("動物", " \n\t"), ("a" * 101, "ねこ")],
)
def test_invalid_list_does_not_replace_existing_data(name: str, text: str):
    candidate = json.dumps({"name": name, "text": text})
    run_node(f"""
const existing = repository.save({{ name: '果物', text: 'りんご,リンゴ' }});
const before = Array.from(values.entries());
assert.throws(() => repository.save({candidate}));
assert.throws(() => repository.save({{ ...{candidate}, id: existing.id }}));
assert.deepEqual(Array.from(values.entries()), before);
assert.deepEqual(repository.lists(), [existing]);
""")


@pytest.mark.parametrize(
    "corrupted",
    [
        "{broken json",
        "null",
        '{"version":2,"lists":[]}',
        '{"version":1,"lists":{}}',
        '{"version":1,"lists":[null]}',
        '{"version":1,"lists":[{"id":"x","name":"a","text":9}]}',
        json.dumps({"version": 1, "lists": [dict(id="x", name="a", text="b")] * 2}),
    ],
)
def test_corrupt_storage_is_rejected_without_overwriting_it(corrupted: str):
    run_node(f"""
repository.save({{ name: '果物', text: 'りんご,リンゴ' }});
const key = Array.from(values.keys())[0];
values.set(key, {json.dumps(corrupted)});
const reloaded = VideoCustomWordlists.createRepository(storage);
assert.throws(() => reloaded.lists());
assert.throws(() => reloaded.save({{ name: '動物', text: 'ねこ,ネコ' }}));
assert.throws(() => reloaded.remove('x'));
assert.equal(values.get(key), {json.dumps(corrupted)});
""")


def test_storage_quota_failure_preserves_lists_on_create_edit_and_delete():
    run_node("""
const existing = repository.save({ name: '果物', text: 'りんご,リンゴ' });
const before = Array.from(values.entries());
storage.setItem = () => { throw new Error('QuotaExceededError'); };
assert.throws(() => repository.save({ name: '動物', text: 'ねこ,ネコ' }));
assert.throws(() => repository.save({ ...existing, text: 'なし,ナシ' }));
assert.throws(() => repository.remove(existing.id));
assert.deepEqual(Array.from(values.entries()), before);
assert.deepEqual(repository.lists(), [existing]);
assert.deepEqual(VideoCustomWordlists.createRepository(storage).lists(), [existing]);
""")


def test_submission_uses_selected_list_and_clears_builtin_filters():
    functions = frontend_function("activeCustomList") + frontend_function("appendCustomWordlist")
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
assert.equal(form.get('wordlist_text'), 'いぬ,イヌ');
assert.equal(form.get('wordlist_name'), '同名');
activeCustomListId = 'one';
appendCustomWordlist(form);
assert.equal(form.get('wordlist_text'), 'ねこ,ネコ');
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
const first = repository.save({ name: '動物', text: 'ねこ' });
const other = repository.save({ name: '動物', text: 'いぬ' });
let customLists = repository.lists();
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
applyEditorWordlist();
assert.equal(repository.lists()[0].text, 'ねこ');
assert.equal(saves, 0);

const editedCsv = 'text,yomi\nいぬ,イヌ';
seed.wordlist.csvText = editedCsv;
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
applyEditorWordlist();
assert.equal(repository.lists()[0].id, first.id);
assert.equal(repository.lists()[0].name, first.name);
assert.equal(repository.lists()[0].text, editedCsv);
assert.deepEqual(repository.lists()[1], other);
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
applyEditorWordlist();
assert.equal(saves, 1);

// The child retains its initial metadata when undo writes the original CSV again.
assert.equal(seed.videoCustomListCsvText, originalCsv);
assert.equal(seed.videoCustomListText, first.text);
seed.wordlist.csvText = originalCsv;
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
applyEditorWordlist();
assert.equal(repository.lists()[0].text, originalCsv);
assert.deepEqual(repository.lists()[1], other);
assert.equal(activeCustomList().text, originalCsv);
assert.equal(saves, 2);
const undoMetadata = JSON.parse(sessionStorage.getItem(EDITOR_SEED_KEY));
assert.equal(undoMetadata.customListCsvText, originalCsv);
assert.deepEqual(undoMetadata.from, editorProvenance());
assert.equal(undoMetadata.sig, originalMetadata.sig);
applyEditorWordlist();
assert.equal(saves, 2);

// Later note edits can serialize stale child metadata without changing its CSV.
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(seed));
applyEditorWordlist();
assert.equal(saves, 2);
assert.equal(JSON.parse(sessionStorage.getItem(EDITOR_KEY)).videoCustomListText, originalCsv);

// An unrelated editor session cannot update a registered list with the same display name.
live.videoCustomListId = other.id;
live.wordlist.csvText = 'うさぎ,ウサギ';
sessionStorage.setItem(EDITOR_KEY, JSON.stringify(live));
const before = repository.lists();
applyEditorWordlist();
assert.deepEqual(repository.lists(), before);
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
