"""Conflicting operations stay exclusive without blocking independent work."""

from test_input_lifecycle import DOM, _functions, _run


def test_custom_list_opens_a_busy_modal_before_reading_storage():
    harness = DOM + r"""
let customListBusy=false, customLists=[], editingCustomListId='', customListInitialValues;
let customListOpener=null, recording=false, message='', syncs=0;
const songRecordingBusy=()=>recording, gate=deferred();
const document={activeElement:{},body:{classList:{add(){}}}};
const setCustomListBusy=v=>{customListBusy=v;};
let refreshCustomLists=()=>gate.promise;
const showsEditorWordlist=()=>false;
const showBuilderMsg=text=>{message=text;}, showCustomListMessage=()=>{};
const syncBuilderValues=()=>{syncs++;};
$('custom-wordlist-panel').showModal=function(){this.open=true;};
const closeCustomList=()=>{$('custom-wordlist-panel').open=false;};
""" + _functions("async function openCustomList(")
    _run(harness, r"""
recording=true; await openCustomList(); assert.equal(customListBusy,false);
assert.equal($('custom-wordlist-panel').open,undefined);
recording=false;
const opening=openCustomList();
assert.equal($('custom-wordlist-panel').open,true,'modal blocks background before storage read');
assert(customListBusy,'pending storage read must disable list editing');
await openCustomList(); assert.equal(syncs,0,'duplicate opens must not initialize another draft');
gate.resolve(); await opening;
assert.equal(customListBusy,false); assert.equal(syncs,1);
assert.equal($('custom-wordlist-panel').open,true);
$('custom-wordlist-panel').open=false;
refreshCustomLists=async()=>{throw Error('storage offline');};
await openCustomList(); assert.equal(customListBusy,false);
assert.equal($('custom-wordlist-panel').open,false);
assert.match(message,/storage offline/);
""")


def test_cancel_timeout_restores_the_control_and_can_retry_the_same_job():
    harness = DOM + r"""
let currentJob='job', submitAbort=null, cancelPendingJob=null, status='', message='';
const controllers=[], watched=[], headers=()=>({});
const AbortSignal={timeout:ms=>{
  assert(ms>0 && ms<=20000); const c=new AbortController();controllers.push(c);return c.signal;
}};
let hang=true, requests=0;
const fetch=(_url,{signal})=>{
  requests++;
  if(hang)return new Promise((_,reject)=>{
    signal?.addEventListener('abort',()=>reject(signal.reason));
  });
  return Promise.resolve({ok:true});
};
const setCancelPending=v=>{$('builder-cancel').disabled=v;};
const setJobStatus=v=>{status=v;}, showBuilderMsg=v=>{message=v;};
const restartPolling=id=>watched.push(id);
""" + _functions("async function cancelJob()")
    _run(harness, r"""
const pending=cancelJob(); assert($('builder-cancel').disabled);
await cancelJob(); assert.equal(requests,1,'pending cancellation remains exclusive');
assert.equal(controllers.length,1,'cancel must not wait indefinitely');
controllers[0].abort(); await pending;
assert.equal(cancelPendingJob,null); assert.equal($('builder-cancel').disabled,false);
assert.match(message,/もう一度/); assert.equal(currentJob,'job');
hang=false; await cancelJob(); assert.deepEqual(watched,['job']);
""")


def test_review_loading_disables_competing_open_controls_and_recovers_from_timeout():
    harness = DOM + r"""
let lyricsFileReading=false;
let currentJob='job', reviewAction='';
let customListBusy=false;
let reviewOpenedJob=null, submitBusy=true, recording=false, message='';
const songRecordingBusy=()=>recording, headers=()=>({});
const review=$('history-review'); review.dataset={jobId:'job'};
const document={querySelectorAll:()=>[review]}, controllers=[];
const AbortSignal={timeout:ms=>{
  assert(ms>0 && ms<=20000);const c=new AbortController();controllers.push(c);return c.signal;
}};
const fetch=(_url,{signal})=>new Promise((_,reject)=>{
  signal?.addEventListener('abort',()=>reject(signal.reason));
});
const showBuilderMsg=v=>{message=v;};
""" + _functions(
        "async function openLyricReview(", "function setReviewAction(",
        "function syncHistoryReviewControls()",
        "function historyReviewBlocked(",
    )
    _run(harness, r"""
const pending=openLyricReview('job'); assert.equal(reviewAction,'opening');
assert.equal($('builder-review').disabled,true); assert.equal(review.disabled,true);
assert.equal($('song-record-button').disabled,true,'review loading excludes a new recording');
assert.equal(controllers.length,1);
controllers[0].abort(); await pending;
assert.equal(reviewAction,''); assert.equal($('builder-review').disabled,false);
assert.equal($('song-record-button').disabled,false);
assert.equal(review.disabled,false); assert.match(message,/再試行/);
""")


def test_history_resume_preserves_the_active_job_and_independent_controls():
    harness = DOM + r"""
let reviewAction='';
let customListBusy=false;
let submitBusy=false, currentJob=null, recording=false, resumed=false, reviewOpenedJob=null;
let builderState='preview', lyricsFileReading=false, watched=null;
const songRecordingBusy=()=>recording;
const same=$('same-review'), other=$('other-review');
same.dataset={jobId:'current'}; other.dataset={jobId:'other'};
const document={querySelectorAll:()=>[same,other]};
const showProgress=()=>{}, watch=id=>{watched=id;currentJob=id;};
$('builder-figure').hidden=false; $('builder-loading').hidden=true;
""" + _functions(
        "function historyReviewBlocked(", "function syncHistoryReviewControls()",
        "function resumeHistoryReview(", "function setBusy(", "function updateBuilderOverlay()",
        "async function openLyricReview(", "function setReviewAction(",
    )
    _run(harness, r"""
setBusy(true); // Submission is still preparing; it must not be replaced by history.
assert(same.disabled && other.disabled);
resumeHistoryReview({id:'other'}); assert.equal(watched,null);
currentJob='current'; syncHistoryReviewControls();
assert.equal(same.disabled,false); assert.equal(other.disabled,true);
resumeHistoryReview({id:'other'}); assert.equal(currentJob,'current');
assert.equal($('song-source-switch').disabled,undefined);
assert.equal($('builder-wordlist-trigger').disabled,undefined);
assert.equal($('history-playback').disabled,undefined);
assert.equal($('history-save').disabled,undefined);
resumeHistoryReview({id:'current'}); assert.equal(watched,'current');
setBusy(false); syncHistoryReviewControls(); assert.equal(other.disabled,false);
for (const flag of ['closing','opening','submitting','recording']) {
  reviewAction=flag==='recording'?'':flag; recording=flag==='recording'; watched=null;
  syncHistoryReviewControls(); assert(other.disabled);
  resumeHistoryReview({id:'other'}); assert.equal(watched,null);
  await openLyricReview('other'); // Review must not cover recording/permission controls.
}
reviewAction=''; recording=false;
resumeHistoryReview({id:'other'}); assert.equal(watched,'other');
""")


def test_review_close_freezes_edits_and_saves_after_an_earlier_autosave():
    harness = DOM + r"""
const EDITOR_KEY='editor', REVIEW_JOB_KEY='job';
let reviewJobId='one', reviewAction='';
const syncHistoryReviewControls=()=>{};
let reviewSavedRaw='', reviewSavePending=null, raw='first', writes=[], gates=[], closed=0;
const headers=()=>({}), loadHistory=async()=>{}, closeEditor=()=>{closed++;};
const sessionStorage={getItem:key=>key===EDITOR_KEY?raw:'one'};
const fetch=async(url,options)=>{
  const gate=deferred(); gates.push(gate);
  writes.push(await options.body.get('editor').text());
  return gate.promise;
};
const ok={ok:true,json:async()=>({})};
""" + _functions(
        "async function saveLyricReview(", "async function closeLyricReview(",
        "async function confirmLyricReview(", "function setReviewAction(",
    )
    _run(harness, r"""
$('close-lyric-review').textContent='閉じる';
const autosave=saveLyricReview(); await tick(); raw='latest before close';
const closing=closeLyricReview();
assert.equal(reviewAction,'closing'); assert($('editor-frame').inert);
assert.equal($('close-lyric-review').textContent,'保存中…');
assert($('confirm-lyric-review').disabled && $('close-lyric-review').disabled);
await closeLyricReview(); await confirmLyricReview();
assert.equal(writes.length,1,'duplicate close and confirm must not submit');
gates[0].resolve(ok); await autosave; await tick();
assert.deepEqual(writes,['first','latest before close']); assert.equal(closed,0);
gates[1].resolve(ok); await closing;
assert.equal(closed,1); assert.equal(reviewSavedRaw,raw);
await saveLyricReview(); assert.equal(writes.length,2,'unchanged edits need no request');
assert.equal($('close-lyric-review').textContent,'閉じる');
assert.equal(reviewAction,''); assert.equal($('editor-frame').inert,false);
raw='failed save'; const failed=closeLyricReview(); await tick();
gates[2].resolve({ok:false,json:async()=>({detail:'offline'})}); await failed;
assert.equal(closed,1); assert.match($('lyric-review-error').textContent,/offline/);
assert.equal($('confirm-lyric-review').disabled,false);
assert.equal($('close-lyric-review').disabled,false); assert.equal($('editor-frame').inert,false);
const retry=closeLyricReview(); await tick(); gates[3].resolve(ok); await retry;
assert.equal(closed,2); assert.equal($('lyric-review-error').textContent,'');
""")


def test_microphone_permission_wait_can_be_canceled_without_affecting_a_new_request():
    harness = DOM + r"""
let activeSongRecording=null, recordingRequestPending=false, recordingStartSequence=0;
let audioInputReady=true, songInputMode='upload', maxSongSeconds=0, lyricsFileReading=false;
let reviewAction='', customListBusy=false, submitBusy=false, currentJob=null;
let requests=[], stopped=0, pauses=0;
const document={querySelectorAll:selector=>selector==='audio, video'?[{pause(){pauses++;}}]:[]};
const recordingSupported=()=>true, ownSongFile=()=>null;
const updateBuilderOverlay=()=>{};
const preferredRecordingMimeType=()=>'';
$('song-record-button').classList={toggle(){}};
const navigator={mediaDevices:{getUserMedia:()=>{
  const gate=deferred();requests.push(gate);return gate.promise;
}}};
const stream=()=>({getTracks:()=>[{stop(){stopped++;}}]});
const window={MediaRecorder:class {
  constructor(){this.state='inactive';this.listeners={};}
  addEventListener(type,fn){this.listeners[type]=fn;}
  start(){this.state='recording';}
  stop(){this.state='inactive';this.listeners.stop();}
}};
""" + _functions(
        "function songRecordingBusy()", "function syncRecordingUi()",
        "function syncSongInputControls()",
        "function setRecordingStatus(", "function stopSongRecording(",
        "async function startSongRecording(", "function releaseSongRecording(",
        "function finishSongRecording(",
        "function setReviewAction(", "function syncHistoryReviewControls()",
        "function historyReviewBlocked(",
    )
    _run(harness, r"""
for (const action of ['opening','closing','submitting']) {
  setReviewAction(action);
  // A UI refresh must also preserve the exclusion at the recording entry point.
  syncRecordingUi(); await startSongRecording();
  assert.equal(requests.length,0,'review actions prevent microphone permission requests');
  assert($('builder-review').disabled);
}
setReviewAction('');
const old=startSongRecording();
assert(recordingRequestPending); assert($('song-source-switch').disabled);
assert.equal(pauses,1);
for (const id of ['advanced','builder-wordlist-field','history','lyrics-correction-panel']) {
  assert($(id).inert,'conflicting controls stay unavailable until capture completes');
}
assert.equal($('song-record-button').disabled,false,'pending permission remains cancelable');
await startSongRecording(); // A second tap cancels the permission wait.
assert.equal(songRecordingBusy(),false); assert.equal($('midi').disabled,false);
assert.equal($('advanced').inert,false); assert.equal($('history').inert,false);
const next=startSongRecording(); const status=$('song-record-status').textContent;
requests[0].resolve(stream()); await old;
assert.equal(stopped,1); assert(recordingRequestPending);
assert.equal($('song-record-status').textContent,status);
requests[1].resolve(stream()); await next;
assert.equal(activeSongRecording.recorder.state,'recording');
assert($('midi').disabled); stopSongRecording(true);
assert.equal(activeSongRecording,null); assert.equal(stopped,2);
assert.equal($('song-source-switch').disabled,false);
const denied=startSongRecording(); requests[2].reject({name:'NotAllowedError'}); await denied;
assert.equal(songRecordingBusy(),false); assert.equal($('midi').disabled,false);
assert.equal($('advanced').inert,false); assert.equal($('history').inert,false);
assert.match($('song-record-status').textContent,/マイクを使用できません/);
""")


def test_lyrics_loading_disables_generation_until_completion_cancel_or_error():
    harness = DOM + r"""
let lyricsFileReadSequence=0, lyricsFileReading=false, submitBusy=false, builderState='preview';
const songRecordingBusy=()=>false, MAX_EDITOR_BYTES=4*1024*1024;
const syncLyricsInput=()=>{}, syncLyricsRecognition=()=>{};
const syncRecordingUi=()=>{updateBuilderOverlay();syncSongInputControls();};
$('builder-figure').hidden=false; $('builder-loading').hidden=true;
""" + _functions("async function loadLyricsFile(", "function updateBuilderOverlay()",
                 "function syncSongInputControls()")
    _run(harness, r"""
const bytes=deferred();
const pending=loadLyricsFile({name:'lyrics.txt',size:10,arrayBuffer:()=>bytes.promise});
assert($('builder-play').disabled); assert.equal($('builder-play').hidden,false);
assert($('preview').disabled && $('preview-high').disabled && $('preview-low').disabled);
await loadLyricsFile(null); assert.equal($('builder-play').disabled,false);
const current=deferred();
const next=loadLyricsFile({name:'new.txt',size:10,arrayBuffer:()=>current.promise});
bytes.resolve(new TextEncoder().encode('obsolete').buffer); await pending;
assert($('builder-play').disabled,'obsolete completion cannot unlock a newer read');
current.resolve(new TextEncoder().encode('current').buffer); await next;
assert.equal($('lyrics').value,'current'); assert.equal($('builder-play').disabled,false);
await loadLyricsFile({size:1,arrayBuffer:async()=>{throw Error('bad file');}});
assert.equal($('builder-play').disabled,false);
submitBusy=true; await loadLyricsFile(null); assert($('builder-play').disabled);
""")
