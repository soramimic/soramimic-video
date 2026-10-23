"""Completed results and history expose the same lyric editing behavior."""

import shutil
import subprocess

import pytest

from test_index_ui import _function_body, _script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_completed_video_edits_the_current_jobs_lyrics_and_preserves_action_order():
    code = "\n".join(_function_body(_script(), head) + "\n}" for head in (
        "function showBuilderVideo(", "function appendLyricEditAction(",
        "function historyButton(", "async function editResultLyrics(",
    ))
    setup = r'''
        const assert = require('node:assert/strict');
        class Element {
            constructor() {
                this.children = []; this.handlers = {}; this.className = '';
                this.classList = {contains: name => this.className.split(' ').includes(name)};
            }
            setAttribute(name, value) {this[name] = value;}
            appendChild(child) {this.children.push(child);}
            prepend(child) {
                this.children = this.children.filter(node => node !== child);
                this.children.unshift(child);
            }
            addEventListener(type, callback) {this.handlers[type] = callback;}
            querySelector(selector) {
                return this.children.find(child => child.classList.contains(selector.slice(1)));
            }
        }
        const document = {createElement: () => new Element()};
        const builder = new Element();
        let actions;
        Object.defineProperty(builder, 'innerHTML', {set() {
            actions = new Element();
            const share = new Element(); share.className = 'action-share';
            actions.appendChild(share); builder.children = [actions];
        }});
        const $ = id => id === 'builder-share' ? builder : actions;
        const SHARE_HTML = '', FILE_SHARE_SUPPORTED = true, previewUrl = '';
        const freshBuilderVideo = () => ({}), bindShare = () => {}, setBuilderState = () => {};
        const prepareVideoShare = () => Promise.resolve();
        const appendSaveMenu = container => {
            const menu = new Element(); menu.className = 'save-menu';
            container.appendChild(menu);
            container.appendChild(new Element()); // Download status follows the save menu.
        };
        let videoSharePreparation, editorPeer = {name: 'editor'}, fail = false;
        const requested = [], transferred = [];
        const headers = () => ({Authorization: 'test'});
        const fetch = async (url, options) => {
            requested.push({url, options});
            return {ok: !fail, json: async () => ({source: url})};
        };
        const SoramimicBridge = {send: async (peer, load) => {
            assert.equal(peer, editorPeer); transferred.push(await load());
        }};
    '''
    checks = r'''
        (async () => {
            for (const name of ['first', 'second']) {
                showBuilderVideo({video_url: '/' + name + '.mp4', editor_url: '/lyrics/' + name});
                const edit = actions.querySelector('.result-edit-lyrics');
                assert.equal(actions.children[0].className, 'save-menu');
                assert.equal(actions.children[1].className, 'action-share');
                assert.equal(actions.children.at(-1), edit);
                assert.equal(edit.textContent, '歌詞を編集 ↗');
                assert.match(edit['aria-label'], /新しいタブ/);
                await edit.handlers.click();
                assert.equal(requested.at(-1).url, '/lyrics/' + name);
                assert.equal(requested.at(-1).options.cache, 'no-store');
                assert.equal(transferred.at(-1).source, '/lyrics/' + name);
                assert.equal(edit.disabled, false);
            }
            fail = true;
            const edit = actions.querySelector('.result-edit-lyrics');
            await edit.handlers.click();
            assert.equal(edit.disabled, false);
            assert.match(builder.children.at(-1).textContent, /取得できませんでした/);
            showBuilderVideo({video_url: '/legacy.mp4'});
            assert.equal(actions.querySelector('.result-edit-lyrics'), undefined);
            editorPeer = null;
            showBuilderVideo({video_url: '/video.mp4', editor_url: '/lyrics'});
            assert.equal(actions.querySelector('.result-edit-lyrics'), undefined);
        })().catch(error => {console.error(error); process.exit(1);});
    '''
    subprocess.run(["node", "-e", setup + code + checks], check=True)
