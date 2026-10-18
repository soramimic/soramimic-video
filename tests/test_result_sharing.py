"""Exercise asynchronous sharing without relying on an OS share sheet in CI."""

import json
import shutil
import subprocess

import pytest

from test_index_ui import _function_body, _script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("surface", ["builder", "history"])
@pytest.mark.parametrize("activation", [
    "active", "expired", "unknown-rejected", "abandoned", "denied",
])
def test_preparation_opens_share_when_possible_and_reuses_file_for_retry(surface, activation):
    script = _script()
    code = script[script.index("const SHARE_TEXT ="):]
    code = code[:code.index("// ジョブを投入できない状態か")]
    code += _function_body(script, "function resetHistoryShare()") + "\n}"
    code += _function_body(script, "async function shareHistoryResult(") + "\n}"
    setup = r'''
        const assert = require('node:assert/strict');
        let fetchCalls = 0, shareCalls = [], messages = [], release;
        let preparedHistoryShare = null;
        let historyJobs = [{video_url: '/video.mp4'}];
        const button = {disabled: false, textContent: '共有', isConnected: true, handlers: {},
                        addEventListener(type, fn) {this.handlers[type] = fn;}};
        const location = {origin: 'https://video.example'};
        const navigator = {userActivation: {isActive: true}, share(data) {
            shareCalls.push(data);
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
                pending = shareHistoryResult(0, button);
            }
            assert.equal(fetchCalls, 1);
            assert.equal(button.disabled, true);
            assert.equal(shareCalls.length, 0);
            if (activation === 'abandoned') {
                if (surface === 'builder') resetVideoSharePreparation();
                else resetHistoryShare();
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
            if (activation !== 'active') {
                assert.equal(button.textContent, '共有を開く');
                navigator.userActivation = {isActive: true};
                const before = shareCalls.length;
                if (surface === 'builder') button.handlers.click();
                else shareHistoryResult(0, button);
                assert.equal(shareCalls.length, before + 1, 'retry opens synchronously');
            }
            assert.equal(fetchCalls, 1, 'a retry must reuse the already prepared file');
            assert.equal(button.disabled, false, 'pending share Promise must not lock the button');
            assert.equal(shareCalls[0].files[0].name.endsWith('.mp4'), true);
            await new Promise(resolve => setImmediate(resolve));
            if (activation === 'denied') {
                const state = surface === 'builder' ? preparedVideoShare : preparedHistoryShare;
                assert.equal(state.shareable, false, 'Save may fall back to a direct download');
                assert.equal(!!state.file, true,
                             'explicit sharing may still retry the cached file');
            } else assert.deepEqual(messages, []);
        })().catch(error => {console.error(error); process.exit(1);});
    '''
    source = f"const surface = {json.dumps(surface)}, activation = {json.dumps(activation)};\n"
    subprocess.run(["node", "-e", source + setup + code + checks], check=True)


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
