"""Keep iOS media previews independent of a second authenticated HTTP request."""

import shutil
import subprocess

import pytest

from test_index_ui import _function_body, _script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_media_download_authenticates_once_and_retains_the_local_file_for_show():
    code = _function_body(_script(), "async function downloadResultMedia(") + "\n}"
    setup = r'''
        const assert = require('node:assert/strict');
        let resultDownloadObjectUrl = '', resultDownloadAbort = null;
        let mode = 'ok', mime = 'video/mp4';
        let disposition = "attachment; filename*=UTF-8''%E6%9B%B2.mp4";
        const requests = [], clicks = [], revoked = [], files = [], timers = new Set();
        const headers = () => ({'X-API-Key': 'test-key'});
        const URL = {
            createObjectURL(file) {files.push(file); return 'blob:' + files.length;},
            revokeObjectURL(url) {revoked.push(url);},
        };
        const setTimeout = (fn, ms) => {assert.equal(ms, 120000); timers.add(fn); return fn;};
        const clearTimeout = fn => timers.delete(fn);
        const document = {body: {appendChild() {}}, createElement() {
            return {click() {clicks.push({href: this.href, name: this.download});}, remove() {}};
        }};
        const fetch = async (url, options) => {
            requests.push({url, options});
            if (mode === 'pending') return new Promise((resolve, reject) => {
                options.signal.addEventListener('abort', () => reject(new Error('aborted')));
            });
            return {
                ok: mode !== 'denied', headers: {get: () => disposition},
                blob: async () => new Blob(mode === 'empty' ? [] : ['media'], {type: mime}),
            };
        };
        const link = {href: '/api/jobs/test/video?api_key=test-key', attributes: {},
            getAttribute(key) {return this.attributes[key];},
            setAttribute(key, value) {this.attributes[key] = value;},
            removeAttribute(key) {delete this.attributes[key];}};
        const status = {hidden: true, textContent: ''};
    '''
    checks = r'''
        (async () => {
            let pending = downloadResultMedia(link, 'video/mp4', status);
            assert.equal(link.getAttribute('aria-busy'), 'true');
            assert.equal(status.hidden, false);
            await downloadResultMedia(link, 'video/mp4', status);
            assert.equal(requests.length, 1, 'ignore a second click during preparation');
            await pending;
            assert.deepEqual(requests[0].options.headers, {'X-API-Key': 'test-key'});
            assert.equal(requests[0].options.credentials, 'same-origin');
            assert.equal(requests[0].url, link.href);
            assert.deepEqual(clicks, [{href: 'blob:1', name: '曲.mp4'}]);
            assert.equal(files[0].type, 'video/mp4');
            assert.equal(await files[0].text(), 'media');
            assert.deepEqual(revoked, [], 'Safari Show needs the URL after click returns');
            assert.equal(status.hidden, true);
            assert.equal(link.getAttribute('aria-busy'), undefined);
            for (const failure of ['denied', 'empty', 'html']) {
                mode = failure;
                mime = failure === 'html' ? 'text/html' : 'video/mp4';
                await downloadResultMedia(link, 'video/mp4', status);
                assert.equal(clicks.length, 1, 'failed responses must never open a video view');
                assert.equal(status.hidden, false);
                assert.match(status.textContent, /もう一度/);
                assert.equal(link.getAttribute('aria-busy'), undefined);
                assert.deepEqual(revoked, [], 'an unsuccessful retry preserves the previous file');
            }
            mode = 'pending';
            pending = downloadResultMedia(link, 'video/mp4', status);
            [...timers][0]();
            await pending;
            assert.equal(status.hidden, false);
            assert.equal(link.getAttribute('aria-busy'), undefined);
            assert.equal(clicks.length, 1);
            mode = 'ok'; mime = 'audio/wav'; disposition = 'attachment; filename="vocal.wav"';
            await downloadResultMedia(link, 'audio/wav', status);
            assert.deepEqual(clicks[1], {href: 'blob:2', name: 'vocal.wav'});
            assert.equal(files[1].type, 'audio/wav');
            assert.equal(status.hidden, true);
            assert.deepEqual(revoked, ['blob:1']);
            disposition = "attachment; filename*=UTF-8''%invalid";
            await downloadResultMedia(link, 'audio/wav', status);
            assert.equal(clicks[2].name, 'soramimic.wav');
            assert.equal(timers.size, 0);
            assert.equal(resultDownloadAbort, null);
        })().catch(error => {console.error(error); process.exit(1);});
    '''
    subprocess.run(["node", "-e", setup + code + checks], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("platform", ["iPhone", "MacIntel", "Win32"])
def test_save_menu_routes_media_to_authenticated_files_without_requiring_share(platform):
    code = "\n".join(_function_body(_script(), name) + "\n}" for name in (
        "function appendSaveMenu(", "function prefersNativeVideoSave(", "function isIOSPlatform(",
    ))
    setup = r'''
        const assert = require('node:assert/strict');
        const navigator = {platform: PLATFORM, maxTouchPoints: PLATFORM === 'MacIntel' ? 5 : 1};
        const qs = url => url;
        function element(tag) {
            return {tag, children: [], handlers: {}, setAttribute() {},
                addEventListener(type, fn) {this.handlers[type] = fn;},
                appendChild(node) {this.children.push(node);}};
        }
        const document = {createElement: element};
        const downloads = [];
        const downloadResultMedia = (link, mime, status) => downloads.push({link, mime, status});
        const linksIn = node => [
            ...(node.tag === 'a' ? [node] : []), ...node.children.flatMap(linksIn)];
    '''.replace("PLATFORM", repr(platform))
    checks = r'''
        const root = element('div');
        appendSaveMenu(root, {video_url: '/video', export_urls: {vocal: '/wav', midi: '/mid'}},
                       () => {throw Error('sharing is unavailable');});
        let prevented = 0;
        const links = linksIn(root);
        for (const link of links) link.handlers.click({preventDefault() {prevented++;}});
        assert.equal(downloads.length, navigator.platform === 'Win32' ? 0 : 2);
        assert.equal(prevented, downloads.length);
        if (downloads.length) {
            assert.deepEqual(downloads.map(item => item.mime), ['video/mp4', 'audio/wav']);
            assert.equal(downloads[0].status, downloads[1].status);
        }
        const audio = element('div');
        appendSaveMenu(audio, {video_url: '/preview', result_kind: 'audio'});
        linksIn(audio)[0].handlers.click({preventDefault() {}});
        if (navigator.platform !== 'Win32') assert.equal(downloads[2].mime, 'audio/wav');
    '''
    subprocess.run(["node", "-e", setup + code + checks], check=True)
