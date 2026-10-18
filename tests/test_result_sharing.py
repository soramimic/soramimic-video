"""Exercise asynchronous sharing without relying on an OS share sheet in CI."""

import json
import shutil
import subprocess

import pytest

from test_index_ui import _function_body, _script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("surface", ["builder", "history"])
@pytest.mark.parametrize("activation", [
    "active", "expired", "unknown-rejected", "abandoned", "denied", "canceled",
])
def test_preparation_opens_share_when_possible_and_reuses_file_for_retry(surface, activation):
    script = _script()
    code = script[script.index("const SHARE_TEXT ="):]
    code = code[:code.index("// ジョブを投入できない状態か")]
    code += _function_body(script, "function resetHistorySave()") + "\n}"
    code += _function_body(script, "async function saveHistoryVideo(") + "\n}"
    setup = r'''
        const assert = require('node:assert/strict');
        let fetchCalls = 0, shareCalls = [], messages = [], release;
        let preparedHistorySave = null;
        let historyJobs = [{video_url: '/video.mp4'}];
        const button = {disabled: false, textContent: '共有', isConnected: true, handlers: {},
                        addEventListener(type, fn) {this.handlers[type] = fn;}};
        const location = {origin: 'https://video.example'};
        const navigator = {platform: 'iPhone', userActivation: {isActive: true}, share(data) {
            shareCalls.push(data);
            if (activation === 'canceled') return Promise.reject({name: 'AbortError'});
            if (activation === 'denied' ||
                (activation === 'unknown-rejected' && shareCalls.length === 1)) {
                return Promise.reject({name: 'NotAllowedError'});
            }
            return new Promise(() => {}); // iOS may leave its share Promise pending.
        }};
        class File {constructor(parts, name, options) {this.name = name; this.type = options.type;}}
        const URL = {createObjectURL: () => 'blob:video', revokeObjectURL() {}};
        const qs = url => url, headers = () => ({});
        const $ = id => id === 'share-save' ? button : {textContent: ''};
        const showBuilderMsg = text => messages.push(text);
        const fetch = async () => {
            fetchCalls++;
            await new Promise(resolve => {release = resolve;});
            return {ok: true, blob: async () => ({type: 'video/mp4'})};
        };
        const video = {load() {}, pause() {}, setAttribute() {}, removeAttribute() {},
                       addEventListener() {}, removeEventListener() {}};
    '''
    checks = r'''
        (async () => {
            if (activation === 'unknown-rejected') delete navigator.userActivation;
            let pending;
            if (surface === 'builder') {
                bindShare('/video.mp4');
                videoSharePreparation = prepareVideoShare('/video.mp4', video);
                pending = button.handlers.click();
            } else {
                pending = saveHistoryVideo(0, button);
            }
            assert.equal(fetchCalls, 1);
            assert.equal(button.disabled, true);
            assert.equal(shareCalls.length, 0);
            if (activation === 'abandoned') {
                if (surface === 'builder') resetVideoSharePreparation();
                else resetHistorySave();
                release();
                await pending;
                assert.equal(shareCalls.length, 0, 'leaving the result must cancel late sharing');
                return;
            }
            if (activation === 'expired') navigator.userActivation.isActive = false;
            release();
            await pending;
            await new Promise(resolve => setImmediate(resolve));
            assert.equal(button.disabled, false);
            assert.equal(shareCalls.length, activation === 'expired' ? 0 : 1);
            if (activation === 'canceled') {
                if (surface === 'history') assert.equal(button.hidden, true);
                assert.deepEqual(messages, []);
                return;
            }
            if (activation !== 'active') {
                const label = surface === 'builder' ? '共有を開く' : '保存を続ける';
                assert.equal(button.textContent, label);
                navigator.userActivation = {isActive: true};
                const before = shareCalls.length;
                if (surface === 'builder') button.handlers.click();
                else saveHistoryVideo(0, button);
                assert.equal(shareCalls.length, before + 1, 'retry opens synchronously');
            }
            assert.equal(fetchCalls, 1, 'a retry must reuse the already prepared file');
            assert.equal(button.disabled, false, 'pending share Promise must not lock the button');
            assert.equal(shareCalls[0].files[0].name.endsWith('.mp4'), true);
            if (surface === 'history') assert.equal('text' in shareCalls[0], false);
            else assert.equal(shareCalls[0].text, SHARE_TEXT);
            await new Promise(resolve => setImmediate(resolve));
            if (activation === 'denied') {
                const state = surface === 'builder' ? preparedVideoShare : preparedHistorySave;
                assert.equal(state.shareable, false, 'Save may fall back to a direct download');
                assert.equal(!!state.file, true,
                             'explicit sharing may still retry the cached file');
                if (surface === 'history') {
                    assert.equal(button.hidden, false);
                    assert.equal(button.textContent, '保存を再試行');
                }
            } else assert.deepEqual(messages, []);
        })().catch(error => {console.error(error); process.exit(1);});
    '''
    source = f"const surface = {json.dumps(surface)}, activation = {json.dumps(activation)};\n"
    subprocess.run(["node", "-e", source + setup + code + checks], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_history_save_fetch_failure_can_retry_and_release_its_control():
    code = "\n".join(_function_body(_script(), name) + "\n}" for name in (
        "async function saveHistoryVideo(", "function resetHistorySave()",
    ))
    setup = r'''
        const assert = require('node:assert/strict');
        const navigator = {userActivation: {isActive: false}};
        const prefersNativeVideoSave = () => true, headers = () => ({});
        const historyJobs = [{video_url: '/video.mp4'}];
        let preparedHistorySave = null, fetches = 0, saves = 0;
        const button = {hidden: true, disabled: false, isConnected: true};
        class File {constructor(parts, name) {this.name = name;}}
        const fetch = async () => ({
            ok: ++fetches > 1, blob: async () => ({type: 'video/mp4'}),
        });
        const openFileShare = () => {saves++;};
    '''
    checks = r'''
        (async () => {
            await saveHistoryVideo(0, button);
            assert.equal(button.textContent, '保存を再試行');
            assert.equal(button.disabled, false);
            assert.equal(button.hidden, false);
            assert.equal(preparedHistorySave.button, button);
            await saveHistoryVideo(0, button);
            assert.equal(fetches, 2);
            assert.equal(button.textContent, '保存を続ける');
            assert.equal(saves, 0);
            navigator.userActivation.isActive = true;
            await saveHistoryVideo(0, button);
            assert.equal(fetches, 2);
            assert.equal(saves, 1);
            assert.equal(button.hidden, true);
            resetHistorySave();
            assert.equal(preparedHistorySave, null);
            assert.equal(button.hidden, true);
        })().catch(error => {console.error(error); process.exit(1);});
    '''
    subprocess.run(["node", "-e", setup + code + checks], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("platform,touches,native", [
    ("iPhone", 1, True), ("MacIntel", 5, True),
    ("MacIntel", 0, False), ("Linux armv8l", 1, False),
])
def test_video_save_keeps_photos_route_only_on_ios(platform, touches, native):
    code = "\n".join(_function_body(_script(), name) + "\n}" for name in (
        "function appendSaveMenu(", "function prefersNativeVideoSave(",
    ))
    setup = f"""
        const assert = require('node:assert/strict');
        const navigator = {{platform: {json.dumps(platform)}, maxTouchPoints: {touches},
                            share() {{}}}};
        class File {{}}
        const qs = url => url;
        function element(tag) {{
            return {{tag, children: [], handlers: {{}},
                addEventListener(type, fn) {{this.handlers[type] = fn;}},
                appendChild(node) {{this.children.push(node);}}}};
        }}
        const document = {{createElement: element}};
        const offerNativeVideoSave = (link, save) => save() !== false;
        const root = element('div');
        const linksIn = node => [
            ...(node.tag === 'a' ? [node] : []), ...node.children.flatMap(linksIn)];
    """
    checks = r'''
        let shared = 0, prevented = 0, available = true;
        appendSaveMenu(root, {video_url: '/video.mp4', export_urls: {midi: '/lyrics.mid'}},
                       () => {shared++; return available;});
        const menu = root.children[0], links = linksIn(menu);
        const event = {preventDefault() {prevented++;}};
        links[0].handlers.click(event);
        assert.equal(shared, NATIVE ? 1 : 0);
        assert.equal(prevented, NATIVE ? 1 : 0, 'iOS saves use the MP4 file share sheet');
        links[1].handlers.click(event);
        assert.equal(prevented, NATIVE ? 1 : 0, 'MIDI always keeps its direct download');
        available = false;
        links[0].handlers.click(event);
        assert.equal(prevented, NATIVE ? 1 : 0,
                     'failed preparation leaves direct saving available');
    '''.replace("NATIVE", str(native).lower())
    subprocess.run(["node", "-e", setup + code + checks], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("storage_available", [True, False])
def test_native_save_help_cancel_opt_out_and_synchronous_confirmation(storage_available):
    code = _function_body(_script(), "function offerNativeVideoSave(") + "\n}"
    setup = r'''
        const assert = require('node:assert/strict');
        let nativeVideoSaveHelpSkipped = false, saved = 0, downloads = 0, stored = null;
        let active = false;
        const localStorage = {
            getItem() {if (!STORAGE) throw Error('blocked'); return stored;},
            setItem(key, value) {if (!STORAGE) throw Error('blocked'); stored = value;},
        };
        const dialog = {open: false, showModal() {this.open = true;}, close() {
            this.open = false;
            queueMicrotask(() => this.onclose?.());
        }};
        const skip = {checked: false}, proceed = {}, cancel = {};
        const nodes = {'video-save-help': dialog, 'video-save-help-skip': skip,
                       'video-save-help-proceed': proceed, 'video-save-help-cancel': cancel};
        const $ = id => nodes[id];
        const document = {visibilityState: 'visible', body: {appendChild() {}},
                          createElement() {return {click() {downloads++;}, remove() {}};}};
        const link = {isConnected: true, href: '/video.mp4?key=secret'};
        const save = () => {assert.equal(active, true); saved++; return true;};
    '''.replace("STORAGE", str(storage_available).lower())
    checks = r'''
        (async () => {
            assert.equal(offerNativeVideoSave(link, save), true);
            assert.equal(dialog.open, true);
            assert.equal(saved, 0);
            skip.checked = true;
            cancel.onclick();
            await Promise.resolve();
            assert.equal(stored, null, 'cancel must not persist the preference');
            assert.equal(nativeVideoSaveHelpSkipped, false);
            offerNativeVideoSave(link, save);
            assert.equal(skip.checked, false);
            active = true;
            proceed.onclick();
            active = false;
            assert.equal(saved, 1, 'confirmation starts sharing in the same click');
            await Promise.resolve();
            offerNativeVideoSave(link, save);
            assert.equal(dialog.open, true, 'unchecked help appears on the next save');
            skip.checked = true;
            active = true;
            proceed.onclick();
            active = false;
            await Promise.resolve();
            assert.equal(saved, 2);
            active = true;
            offerNativeVideoSave(link, save);
            active = false;
            assert.equal(saved, 3);
            assert.equal(dialog.open, false, 'opt-out skips the dialog, even without storage');
            assert.equal(downloads, 0);
            if (STORAGE) {
                nativeVideoSaveHelpSkipped = false;
                active = true;
                offerNativeVideoSave(link, save);
                active = false;
                assert.equal(saved, 4, 'stored opt-out survives page state reset');
            }
            nativeVideoSaveHelpSkipped = false;
            stored = null;
            offerNativeVideoSave(link, () => false);
            proceed.onclick();
            assert.equal(downloads, 1, 'unavailable sharing keeps an explicit download route');
            await Promise.resolve();
            offerNativeVideoSave(link, save);
            link.isConnected = false;
            proceed.onclick();
            assert.equal(saved, STORAGE ? 4 : 3, 'obsolete results never open a share sheet');
        })().catch(error => {console.error(error); process.exit(1);});
    '''.replace("STORAGE", str(storage_available).lower())
    subprocess.run(["node", "-e", setup + code + checks], check=True)
