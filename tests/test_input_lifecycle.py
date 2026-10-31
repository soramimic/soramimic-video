"""Behavioral regressions for input replacement and asynchronous completion."""

import shutil
import subprocess

import pytest

from test_index_ui import _function_body, _script, _song_input_node_harness

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


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


def test_midi_response_cannot_reject_a_replacement_before_its_check_starts():
    harness = DOM + r"""
let midiCheckSeq=0, midiSampleId='', midiRejecting=false, lastMidiCheck=null;
let lyrics='', message='', previews=0;
const ownSongFile=()=>$('midi').files[0];
const ownSongKind=()=>ownSongFile()?.name.endsWith('.wav')?'audio':'midi';
const selectedSampleIsAudio=()=>false, songLyricsForRequest=()=>lyrics;
const headers=()=>({}), renderLyricsMidiWarning=()=>{};
const clearMidiNotices=()=>{$('midi-error').textContent='';};
const syncBuilderValues=()=>{
  $('song-upload-filename').textContent=ownSongFile()?.name||'';
  $('song-upload-button').hidden=!!ownSongFile();
};
const schedulePreview=()=>{previews++;}, showSubmitMsg=text=>{message=text;};
let response;
const fetch=()=>response.promise;
""" + _functions("async function checkMidi(", "function rejectMidi(") + _listener(
        '$("midi").addEventListener("change", () => {\n  const file = ownSongFile();'
    )
    _run(harness, r"""
for (const replacement of [new File(['b'],'new.mid'), new File(['wav'],'new.wav'), null]) {
  const old=new File(['bad'],'old.mid'); $('midi').files=[old];
  response=deferred(); const checking=checkMidi();
  $('midi').files=replacement?[replacement]:[];
  // No newer check has started yet: generation number alone cannot protect this input.
  response.resolve({ok:false,status:400,json:async()=>({detail:'not MIDI'})});
  await checking;
  assert.equal(ownSongFile(),replacement||undefined);
  assert.equal(message,'');
}
const current=new File(['good'],'current.mid'); $('midi').files=[current];
response=deferred(); let checking=checkMidi();
response.resolve({ok:true,json:async()=>({has_lyrics:true,midi_lines:['あいうえお']})});
await checking; assert.equal(lastMidiCheck.file,current);
for (const reply of [
  {ok:true,json:async()=>({has_lyrics:false})},
  {ok:false,status:400,json:async()=>({detail:'not MIDI'})},
]) {
  $('midi').files=[current]; response=deferred(); checking=checkMidi();
  response.resolve(reply); await checking;
  assert.equal(ownSongFile(),undefined,'unchanged invalid input is still rejected');
  assert.match(message,/取り消しました/);
  assert.equal($('song-upload-filename').textContent,'');
  assert.equal($('song-upload-button').hidden,false,'upload entry returns after rejection');
}
assert.ok(previews>0);
""")


def test_replacing_rejected_midi_clears_its_visible_error_only():
    harness = DOM + r"""
let midiCheckSeq=0, midiSampleId='', midiRejecting=false, lastMidiCheck=null;
let kind='midi', audioSample=false, valid=false;
const ownSongKind=()=>kind, selectedSampleIsAudio=()=>audioSample;
const songLyricsForRequest=()=>'', headers=()=>({}), renderLyricsMidiWarning=()=>{};
const fetch=async()=>({ok:true,json:async()=>({has_lyrics:valid,
  detail:valid?'':'歌詞を音符に対応付けられません',midi_lines:['あいうえお']})});
""" + _functions(
        "async function checkMidi(", "function rejectMidi(",
        "function clearMidiNotices()", "function showSubmitMsg(",
    )
    _run(harness, r"""
for (const replacement of ['midi','sample','audio','audio-sample','clear','invalid']) {
  kind='midi'; audioSample=false; valid=false; midiSampleId='';
  $('midi').files=[new File(['bad'],'bad.mid')];
  await checkMidi();
  assert.equal($('midi').files.length,0);
  assert.match($('submit-msg').textContent,/取り消しました/);
  assert.equal($('submit-msg').hidden,false,'rejection reason remains visible');
  valid=replacement!=='invalid';
  if (['midi','invalid'].includes(replacement)) $('midi').files=[new File(['next'],'next.mid')];
  if (replacement==='sample') midiSampleId='furusato';
  if (replacement==='audio') kind='audio';
  if (replacement==='audio-sample') audioSample=true;
  await checkMidi();
  if (replacement==='invalid') {
    assert.equal($('submit-msg').hidden,false,'a new rejection is still shown');
  } else {
    assert.equal($('submit-msg').textContent,'',replacement+' clears the relayed error');
    assert.equal($('submit-msg').hidden,true);
    assert.equal($('midi-error').hidden,true);
  }
}
// A newer submission message must survive cleanup of the old MIDI notice.
showSubmitMsg('別の処理の案内');
clearMidiNotices();
assert.equal($('submit-msg').textContent,'別の処理の案内');
assert.equal($('submit-msg').hidden,false);
// An empty MIDI notice does not own the submission message either.
clearMidiNotices();
assert.equal($('submit-msg').textContent,'別の処理の案内');
""")


def test_starting_generation_clears_previous_stage_results_and_log():
    harness = DOM + r"""
const chips=['analyze','convert','synthesize','mix','video'].map(name=>({
  dataset:{stage:name},textContent:name+' 29秒',classes:new Set(['done']),
  classList:{toggle(key,on){
    if(on)this.owner.classes.add(key);else this.owner.classes.delete(key);
  }},
}));
for(const chip of chips)chip.classList.owner=chip;
const document={querySelectorAll:()=>chips};
const ownAudioFile=()=>null, selectedSampleIsAudio=()=>false;
const showBuilderMsg=()=>{}, setBuilderState=()=>{};
$('job-card').classList={remove(){}};
""" + _functions("function showProgress()", "function renderStages(", "function stagePlan(")
    _run(harness, r"""
$('log').textContent='previous generation log';
showProgress();
assert.equal($('log').textContent,'');
for(const chip of chips){
  assert.equal(chip.classes.has('done'),false);
  assert.equal(chip.classes.has('now'),false);
  assert.ok(!chip.textContent.includes('29秒'));
}
""")


def test_saved_midi_cannot_be_overwritten_by_an_obsolete_file_read():
    harness = DOM + r"""
const MIDI_KEY='midiFile', stored=new Map(), readers=[];
const localStorage={setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
const ownSongFile=()=>$('midi').files[0];
const ownSongKind=f=>f.name.endsWith('.mid')?'midi':'audio';
class FileReader {
  readAsDataURL(file) {readers.push(()=>{this.result='data:'+file.name; this.onload();});}
}
""" + _listener('$("midi").addEventListener("change", async () => {')
    _run(harness, r"""
const choose=file=>{
  $('midi').files=file?[file]:[]; return $('midi').dispatchEvent(new Event('change'));
};
for (const kind of ['midi','audio','clear']) {
  readers.length=0;
  const old=choose({name:'old.mid'});
  let next;
  if (kind==='midi') {
    next=choose({name:'current.mid'}); readers[1](); await next;
  } else await choose(kind==='audio'?{name:'current.wav'}:null);
  readers[0](); await old;
  if (kind==='midi') assert.equal(JSON.parse(stored.get(MIDI_KEY)).name,'current.mid');
  else assert.equal(stored.has(MIDI_KEY),false);
}
""")


def test_pending_sample_preserves_lyrics_imported_or_typed_after_selection():
    harness = _song_input_node_harness() + r"""
let finish;
const fetch=async()=>({ok:true,text:()=>new Promise(resolve=>{finish=resolve;})});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
"""
    _run(harness, r"""
for (const input of ['{"results":[[{}]],"unitsList":[[{}]]}','新しく入力した歌詞']) {
  $('sample-select').value='furusato'; $('lyrics').value='old'; editorImportedFile=null;
  const loading=applySample(); await tick();
  $('lyrics').value=input; lyricsFileReadSequence++;
  editorImportedFile=input.startsWith('{')?{name:'current.json'}:null;
  const editor=editorImportedFile;
  finish('old sample lyrics'); assert.equal(await loading,true);
  assert.equal(midiSampleId,'furusato','song selection itself still completes');
  assert.equal($('lyrics').value,input);
  assert.equal(editorImportedFile,editor);
}
$('sample-select').value='harugakita'; $('lyrics').value=''; editorImportedFile=null;
const loading=applySample(); await tick(); finish('current sample lyrics');
assert.equal(await loading,true); assert.equal($('lyrics').value,'current sample lyrics');
assert.equal(sampleLyricsBaseline,'current sample lyrics');
""")


def test_canceling_lyrics_import_allows_automatic_recognition_without_stale_results():
    harness = DOM + r"""
let lyricsFileReadSequence=0, lyricsFileReading=false;
const MAX_EDITOR_BYTES=4*1024*1024;
const syncLyricsInput=()=>{}, syncLyricsRecognition=()=>{};
const saveForm=()=>{}, scheduleMidiCheck=()=>{}, songRecordingBusy=()=>false;
const syncRecordingUi=()=>syncSongInputControls();
""" + _functions("async function loadLyricsFile(", "function syncSongInputControls()") + _listener(
        '$("auto-lyrics").addEventListener("change", () => {'
    )
    _run(harness, r"""
$('lyrics').value='current lyrics'; const bytes=deferred();
const loading=loadLyricsFile({name:'old.json',size:10,arrayBuffer:()=>bytes.promise});
assert.equal(lyricsFileReading,true);
assert.equal($('auto-lyrics').disabled,true);
await loadLyricsFile(null);
assert.equal($('auto-lyrics').disabled,false);
$('auto-lyrics').checked=true; $('auto-lyrics').dispatchEvent(new Event('change'));
assert.equal(lyricsFileReading,false);
bytes.resolve(new TextEncoder().encode('obsolete file lyrics').buffer); await loading;
assert.equal($('auto-lyrics').checked,true);
assert.equal($('lyrics').value,'current lyrics');
assert.equal($('lyrics-file-name').hidden,true);
""")


def test_submit_rechecks_lyrics_after_verification_before_building_the_form():
    harness = DOM + r"""
let submitBusy=false, lyricsFileReading=false, automatic=false, submitAbort=null;
const songRecordingBusy=()=>false;
let samplePending=null, midiChecking=null, midiSampleId='', submitMessage='', gate;
const songInputMode='upload', simpleMode=false, fixedSynthesizer='voicevox', fixedVoicevoxStyle=1;
const turnstileSiteKey='site', turnstileScriptFailed=false;
const requests=[], file=new File(['midi'],'current.mid');
const ownAudioFile=()=>null, ownSongFile=()=>file, ownSongKind=()=>'midi';
const automaticLyricsEnabled=()=>automatic, lyricsInput=()=>({error:''});
const ensureSelectedSampleMidi=async()=>true;
const editorSourceForSubmit=()=>({file:!automatic&&$('lyrics').value.startsWith('{')
  ?new File([$('lyrics').value],'editor.json'):null});
const readParodyFile=async f=>JSON.parse(await f.text());
const lyricReviewCoverage=()=>({unmatched:false}), parodyMismatch=()=>false, apiKey=()=>'';
const ensureTurnstileToken=()=>{gate=deferred();return gate.promise;};
const turnstileToken=()=>'test', hideTurnstilePrompt=()=>{}, showTurnstileFailure=()=>{};
const setBusy=v=>{submitBusy=v;}, showSubmitMsg=v=>{submitMessage=v;};
const songTitleOf=()=>'song', buildConvertParams=()=>'', selectedTitleWords=()=>'';
const activeCustomList=()=>null, showsEditorWordlist=()=>false;
const appendSongLyrics=form=>form.append('lyrics',automatic?'':$('lyrics').value);
const appendCustomWordlist=()=>{}, showProgress=()=>{}, setJobStatus=()=>{};
const setCancelPending=()=>{}, resetTurnstile=()=>{}, watch=()=>setBusy(false);
const postJobWithTurnstileRetry=async form=>{requests.push(form);
  return {ok:true,json:async()=>({id:'job'})};};
""" + _functions("async function submitJob(", "function captureGenerationThumbnail()")
    _run(harness, r"""
for (const change of ['json','plain','auto','reading','unchanged']) {
  automatic=false; lyricsFileReading=false; $('lyrics').value='{"lyrics":"original"}';
  const count=requests.length, pending=submitJob(0,''); await tick();
  assert.ok(gate); assert.equal(submitBusy,true);
  if(change==='json')$('lyrics').value='{"lyrics":"changed"}';
  if(change==='plain')$('lyrics').value='changed lyrics';
  if(change==='auto')automatic=true;
  if(change==='reading')lyricsFileReading=true;
  gate.resolve(true); await pending;
  assert.equal(submitBusy,false);
  if(change==='unchanged') {
    assert.equal(requests.length,count+1);
    assert.equal(JSON.parse(await requests.at(-1).get('editor').text()).lyrics,'original');
  } else {
    assert.equal(requests.length,count,'changed input requires a fresh submit');
    assert.match(submitMessage,/歌詞の入力が変わりました/);
  }
}
""")


def test_history_failure_times_out_without_locking_startup_and_can_retry():
    harness = DOM + r"""
const document={createElement:element}, headers=()=>({});
let historyLoadPending=null, historyJobs=[], loads=0, mode='http', controller, timeout;
let activeHistoryPlayback=null, preparedHistorySave=null, preparedHistoryShare=null;
let disposals=0, restored=[];
const disposeHistoryPlayback=()=>{disposals++;}, resetHistorySave=()=>{}, resetHistoryShare=()=>{};
const restoreJobProgress=jobs=>{restored=jobs;};
const AbortSignal={timeout:ms=>{
  timeout=ms; controller=new AbortController(); return controller.signal;
}};
const jobs=[{status:'done',song_label:'current'}], historyCard=job=>({job});
const fetch=async (_url,{signal})=>{
  loads++;
  if(mode==='http')return {ok:false};
  if(mode==='network')throw new Error('offline');
  const waitForAbort=()=>new Promise((_,reject)=>{
    signal.addEventListener('abort',()=>reject(signal.reason));
  });
  if(mode==='hang')return waitForAbort();
  if(mode==='body')return {ok:true,json:waitForAbort};
  return {ok:true,json:async()=>jobs};
};
""" + _functions(
        "async function loadHistory()", "async function fetchHistory()", "function historyButton(",
        "function renderHistoryJobs(",
    )
    _run(harness, r"""
for (const failure of ['http','network','hang','body']) {
  mode=failure; const before=loads;
  const first=loadHistory(), duplicate=loadHistory(); await tick();
  assert.equal(loads,before+1,'overlapping opens share one history request');
  assert.equal(timeout,10000);
  if(mode==='hang'||mode==='body')controller.abort(new DOMException('timeout','TimeoutError'));
  assert.deepEqual(await first,[]); await duplicate;
  assert.equal(historyLoadPending,null,'failure releases request lock');
  const [message,retry]=$('history-body').children;
  assert.match(message.textContent,/履歴を取得できません/);
  assert.equal(retry.textContent,'再試行');
  mode='ok'; await retry.listeners.click();
  assert.deepEqual(historyJobs,jobs); assert.deepEqual(restored,jobs);
  assert.equal($('history-body').children[0].className,'history-list');
}
assert.ok(disposals>=4);
""")


def test_save_shares_only_the_file_while_share_keeps_the_caption():
    harness = DOM + r"""
const SHARE_TEXT='caption', sharePreparationSeq=1;
const file=new File(['video'],'soramimic.mp4'), calls=[];
const navigator={share:data=>{calls.push(data);},userActivation:{isActive:true}};
const preparedVideoShare={file,videoUrl:'/video.mp4',shareable:true};
const videoSharePreparation=null, showBuilderMsg=()=>{};
""" + _functions(
        "function openFileShare(", "async function sharePreparedVideo(", "function bindShare("
    )
    _run(harness, r"""
bindShare('/video.mp4'); await $('share-save').listeners.click();
await sharePreparedVideo('/video.mp4',$('share-save'),'');
assert.equal(calls.length,2); assert.equal(calls[0].text,SHARE_TEXT);
assert.equal(calls[1].text,undefined); assert.equal(calls[1].files[0],file);
""")


def test_recording_blocks_uploads_until_recording_has_finished():
    harness = DOM + r"""
let recordingRequestPending=false, lyricsFileReading=false;
const updateBuilderOverlay=()=>{}, syncHistoryReviewControls=()=>{};
let activeSongRecording=null, audioInputReady=true, message='', uploads=0, songDragDepth=0;
const recordingSupported=()=>true, ownSongFile=()=>null;
const hasDraggedFiles=()=>true;
const songUploadEntry=$('song-upload-entry');
songUploadEntry.classList={add(){},remove(){}};
songUploadEntry.closest=()=>null;
$('song-record-button').classList={toggle(){}};
const showBuilderMsg=text=>{message=text;};
const setOwnSongFile=file=>{uploads++; $('midi').files=[file];};
const waitForSelectedMidiCheck=()=>{};
let pickerOpened=0;
$('midi').click=()=>{pickerOpened++;};
""" + _functions("function syncRecordingUi()", "function songRecordingBusy()",
                 "function syncSongInputControls()") + _listener(
        '$("song-upload-button").addEventListener("click",'
    ) + _listener('songUploadEntry.addEventListener("drop",') + _listener(
        'songUploadEntry.addEventListener("dragover",'
    )
    _run(harness, r"""
const file=new File(['midi'],'new.mid');
const dropped={type:'drop',preventDefault(){},dataTransfer:{files:[file]}};
const dragged={type:'dragover',preventDefault(){},dataTransfer:{dropEffect:'copy'}};
for (const state of ['recording','inactive']) {
  // The inactive phase is still busy finalizing the captured recording.
  activeSongRecording={recorder:{state},stopping:state==='inactive'};
  syncRecordingUi();
  assert.equal($('midi').disabled,true);
  assert.equal($('song-upload-button').disabled,true);
  assert.equal($('song-source-switch').disabled,true);
  $('song-upload-button').dispatchEvent(new Event('click'));
  assert.equal(pickerOpened,0);
  songUploadEntry.dispatchEvent(dragged);
  assert.equal(dragged.dataTransfer.dropEffect,'none');
  songUploadEntry.dispatchEvent(dropped);
  assert.equal(uploads,0,'a drop must not replace an active recording');
  assert.match(message,/録音を停止してから/);
  assert.equal(activeSongRecording.recorder.state,state);
}
activeSongRecording=null;
syncRecordingUi();
assert.equal($('midi').disabled,false);
assert.equal($('song-upload-button').disabled,false);
assert.equal($('song-source-switch').disabled,false);
$('song-upload-button').dispatchEvent(new Event('click'));
assert.equal(pickerOpened,1);
songUploadEntry.dispatchEvent(dragged);
assert.equal(dragged.dataTransfer.dropEffect,'copy');
songUploadEntry.dispatchEvent(dropped);
assert.equal(uploads,1,'upload works again after recording finishes');
assert.equal($('midi').files[0],file);
""")


def test_recording_blocks_song_source_switch_without_discarding_audio():
    _run(_song_input_node_harness(), r"""
$('lyrics').value='入力済みの歌詞';
for (const state of ['recording','inactive']) {
  activeSongRecording={recorder:{state},stopping:state==='inactive'};
  const recording=activeSongRecording;
  switchSongInputMode('sample');
  assert.equal(songInputMode,'upload');
  assert.equal(activeSongRecording,recording);
  assert.equal($('lyrics').value,'入力済みの歌詞');
}
activeSongRecording=null;
switchSongInputMode('sample');
assert.equal(songInputMode,'sample','switch is available once recording finishes');
""")
