"""Exercise result deletion against the rendered history DOM and asynchronous requests."""

import shutil

import pytest

from ui_helpers import _functions, _run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


HARNESS = r"""
const assert = require('node:assert/strict');
const {parseHTML} = require('linkedom');
const {document, window} = parseHTML(`<html><body>
 <details id="history"><summary>過去のジョブ</summary>
 <p id="history-message" hidden></p><div id="history-body"></div></details>
 <button id="share-save"></button><video id="builder-video"></video>
</body></html>`);
const $ = id => document.getElementById(id);
let focused;
window.HTMLElement.prototype.focus = function() { focused = this; };
const navigator = {}, headers = () => ({'X-API-Key':'key'}), qs = url => url;
const historyCreatedLabel = () => '', historyStatusLabel = job => job.status;
const appendSaveMenu = () => null, appendEditActions = () => {};
const historyReviewBlocked = () => false, resumeHistoryReview = () => {};
const WORDLIST_EXHAUSTED_NOTICE = 'notice';
let historyJobs = [], activeHistoryPlayback = null, preparedHistorySave = null;
let preparedHistoryShare = null, resultDownloadAbort = null, resultDownloadObjectUrl = '';
const historyDeleteStates = new Map(), historyDeletedIds = new Set();
let played = 0, saved = 0, shared = 0, builderReset = 0, builderReleased = 0;
const disposeHistoryPlayback = () => {played++; activeHistoryPlayback = null;};
const resetHistorySave = () => {saved++; preparedHistorySave = null;};
const resetHistoryShare = () => {shared++; preparedHistoryShare = null;};
function showBuilderVideo() {}
const freshBuilderVideo = () => {builderReset++;};
const releaseBuilderDone = () => {builderReleased++;};
const calls = [], pending = [];
const fetch = (url, options) => {
 calls.push({url, options});
 return new Promise((resolve, reject) => pending.push({resolve, reject}));
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const job = (id, status='done') => ({id, status, song_label:'曲 ' + id, result_kind:'video'});
const mount = (...jobs) => {
 renderHistoryJobs(jobs);
 return [...$('history-body').querySelectorAll('.history-item')];
};
const ask = card => {card.querySelector('.history-delete').click();};
""" + _functions(
    "function historyButton(", "function historyCard(", "function appendHistoryDelete(",
    "async function deleteHistoryResult(", "function renderHistoryJobs(",
    "async function fetchHistory()",
)


def test_confirmation_cancels_and_protects_active_jobs_and_labels():
    _run(HARNESS, r"""
const [done,failed,canceled,queued,running,review] = mount(
 job('a'),job('b','error'),job('c','canceled'),job('d','queued'),
 job('e','running'),job('f','awaiting_review'));
for(const card of [done,failed,canceled]) assert(card.querySelector('.history-delete'));
for(const card of [queued,running,review]) assert(!card.querySelector('.history-delete'));
const button=done.querySelector('.history-delete');
ask(done);const state=historyDeleteStates.get('a');
assert(state.panel.isConnected);assert.equal(focused,state.cancel);
assert.equal(button.getAttribute('aria-expanded'),'true');
assert.equal(calls.length,0);
state.cancel.click();assert.equal(calls.length,0);
assert.equal(historyDeleteStates.size,0);assert(!state.panel.isConnected);
assert.equal(focused,button);assert.equal(button.disabled,false);
const unsafe=job('x');unsafe.song_label='<img src=x onerror=alert(1)>';
const [card]=mount(unsafe);ask(card);
assert.equal(card.querySelectorAll('img').length,0);
assert(card.querySelector('.history-delete-confirm').textContent.includes(unsafe.song_label));
""")


def test_delete_retries_without_duplicates_and_keeps_confirmation_during_refresh():
    _run(HARNESS, r"""
const a=job('a'),b=job('b');const [card]=mount(a,b);ask(card);
const state=historyDeleteStates.get('a');state.confirm.click();
assert(state.busy);assert(state.confirm.disabled);assert(state.cancel.disabled);
await deleteHistoryResult(a,state);assert.equal(calls.length,1);
assert.equal(calls[0].url,'/api/jobs/a');
assert.equal(calls[0].options.method,'DELETE');
assert.equal(calls[0].options.headers['X-API-Key'],'key');
assert(calls[0].options.signal);
pending.shift().resolve({ok:false,status:500});await tick();
assert(!state.busy);assert(!state.confirm.disabled);assert(!state.error.hidden);
assert(state.card.isConnected);assert.equal(focused,state.confirm);
renderHistoryJobs([b,a]);
assert.equal($('history-body').querySelectorAll('.history-item')[1],card);
assert.equal(historyDeleteStates.get('a'),state);
state.confirm.click();assert.equal(calls.length,2);
pending.shift().resolve({ok:false,status:409});await tick();
assert(state.error.textContent.includes('作成が終了'));
state.confirm.click();pending.shift().reject(new DOMException('timed out','TimeoutError'));
await tick();assert(state.error.textContent.includes('完了を確認できません'));
state.confirm.click();pending.shift().resolve({ok:true,status:200});await tick();
assert.deepEqual(historyJobs.map(job=>job.id),['b']);assert(!card.isConnected);
assert(historyDeletedIds.has('a'));assert(!historyDeleteStates.has('a'));
assert.equal($('history-message').hidden,false);
// An earlier in-flight list response cannot put the deleted card back.
const refresh=fetchHistory();pending.shift().resolve({ok:true,json:async()=>[a,b]});
await refresh;assert.deepEqual(historyJobs.map(job=>job.id),['b']);
""")


@pytest.mark.parametrize("response_status", [200, 202, 404])
def test_deletion_releases_only_the_deleted_results_media_and_share_state(response_status):
    _run(HARNESS + f"const responseStatus={response_status};\n", r"""
const a=job('a'),b=job('b');const [card,other]=mount(a,b);
activeHistoryPlayback={jobId:'a'};
preparedHistorySave={jobId:'b',button:other.querySelector('.history-delete')};
preparedHistoryShare={jobId:'a'};
const keep=preparedHistorySave;
showBuilderVideo.jobId='a';let aborted=0,canceled=0;
resultDownloadAbort={abort:()=>aborted++};
globalThis.VideoOperations={cancelAction:button=>{assert.equal(button,$('share-save'));canceled++;}};
ask(card);historyDeleteStates.get('a').confirm.click();
pending.shift().resolve({ok:responseStatus<300,status:responseStatus});await tick();
assert.deepEqual(historyJobs.map(job=>job.id),['b']);
assert.equal(activeHistoryPlayback,null);assert.equal(preparedHistoryShare,null);
assert.equal(preparedHistorySave,keep);
assert.equal(aborted,1);assert.equal(canceled,1);
assert.equal(builderReset,1);assert.equal(builderReleased,1);
assert.equal(showBuilderVideo.jobId,null);
assert.equal(focused,$('history-body').querySelector('.history-delete'));
if(responseStatus===202)assert($('history-message').textContent.includes('処理中'));
""")


def test_deleting_last_result_focuses_history():
    _run(HARNESS, r"""
const a=job('a');const [card]=mount(a);ask(card);
const state=historyDeleteStates.get('a');state.confirm.click();
pending.shift().resolve({ok:true,status:200});await tick();
assert.equal(historyJobs.length,0);assert.equal(focused,$('history').querySelector('summary'));
assert.equal(builderReset,0);
""")
