"""The real submission and cancellation handlers keep tracking uncertain acceptance."""
import shutil
from pathlib import Path

import pytest

from test_input_lifecycle import DOM, _functions, _run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


@pytest.mark.parametrize(('outcome', 'audio_input'), [
    ('before', False), ('accepted', False), ('done', False),
    ('lost-cancel', False), ('lost-submit', False), ('accepted', True), ('quota-retry', True),
])
def test_pending_submission_cancel_reconciles_before_releasing_new_generation(outcome, audio_input):
    harness = DOM + Path('src/soramimic_video/static/submissions.js').read_text()
    harness += Path('src/soramimic_video/static/audio-upload.js').read_text() + r'''
let submitBusy=false, submitAbort=null, currentJob=null, cancelPendingJob=null;
let leases=0;
const begin=()=>{leases++;let active=true;return {end(){if(active){active=false;leases--;}}};};
globalThis.VideoOperations={begin,beginAction(){
  const lease=begin();return {finish:()=>lease.end()};
}};
const songRecordingBusy=()=>false, lyricsFileReading=false, songInputMode='sample';
const simpleMode=true, samplePending=null, midiChecking=null, midiSampleId='sample';
const file=new File(['audio data'],'song.wav');
const ownAudioFile=()=>audioInput?file:null, ownSongKind=()=>'', ownSongFile=()=>null;
const resumableAudioUploads=true;
const automaticLyricsEnabled=()=>true, lyricsInput=()=>({error:''});
const ensureSelectedSampleMidi=async()=>true, editorSourceForSubmit=()=>({file:null});
const apiKey=()=>'', turnstileSiteKey='', fixedSynthesizer='voicevox', fixedVoicevoxStyle=1;
const songTitleOf=()=>'', selectedTitleWords=()=>'', buildConvertParams=()=>'';
const activeCustomList=()=>null, showsEditorWordlist=()=>false;
const appendSongLyrics=()=>{}, appendCustomWordlist=()=>{}, headers=()=>({});
const showSubmitMsg=()=>{}, showProgress=()=>{}, hideTurnstilePrompt=()=>{};
const resetTurnstile=()=>{}, openJobDetails=()=>{}, loadHistory=()=>{};
let state='preview', message='', status='', posts=0, cancellations=0, posted=false;
let reservations=0, offset=0;
const watched=[], urls=[];
const setBusy=v=>{submitBusy=v;}, setBuilderState=v=>{state=v;};
const setJobStatus=v=>{status=v;}, showBuilderMsg=v=>{message=v;};
const syncHistoryReviewControls=()=>{}, restartPolling=id=>watched.push(id);
const response=body=>({ok:true,status:200,json:async()=>body});
const fetch=async(url, options)=>{
  urls.push(url);
  if(url==='/api/submissions'){reservations++;return response({id:'a'.repeat(32)});}
  if(url==='/api/audio-uploads')return response({id:'a'.repeat(32),size:file.size,
    offset,chunk_bytes:524288});
  if(options.method==='DELETE')return response({removed:true});
  if(options.method==='PUT'){offset+=options.body.size;return response({id:'a'.repeat(32),
    size:file.size,offset,chunk_bytes:524288});}
  if(url==='/api/jobs'){
    posts++;posted=true;
    assert.equal(options.body.get('registered_submission'),'true');
    assert.equal(options.body.get('submission_id'),'a'.repeat(32));
    if(outcome==='quota-retry'&&posts===1)return {ok:false,status:429,json:async()=>({})};
    if(outcome==='lost-submit')throw new TypeError('acknowledgement lost');
    return new Promise((_,reject)=>options.signal.addEventListener('abort',
      ()=>reject(new DOMException('aborted','AbortError')),{once:true}));
  }
  assert.equal(url,'/api/submissions/'+ 'a'.repeat(32)+'/cancel');
  cancellations++;
  if(outcome==='lost-cancel'&&cancellations===1)throw new TypeError('cancel acknowledgement lost');
  return response(outcome==='before'?{id:null,status:'canceled'}
    :{id:'1234abcd',status:outcome==='done'?'done':'canceled'});
};
''' + _functions('async function submitJob(', 'function captureGenerationThumbnail()',
                'async function postJobWithTurnstileRetry(',
                'async function cancelJob()', 'async function reconcileSubmissionCancellation(',
                'function setCancelPending(', 'function watch(')
    harness += '\nconst outcome=' + repr(outcome) + ';const audioInput='
    harness += str(audio_input).lower() + ';\n'
    _run(harness, r'''
let pending=submitJob(0,'');
while(!posted)await tick();
if(outcome==='quota-retry'){
  await pending;assert.equal(submitBusy,false);pending=submitJob(0,'');
  while(posts<2)await tick();
}
if(outcome==='lost-submit'){
  await pending;
  assert(submitBusy);assert.match(message,/受付を確認できません/);
}
await cancelJob();await pending;
if(outcome==='lost-cancel'){
  assert(submitBusy,'uncertain cancellation cannot enable a duplicate generation');
  assert.equal(leases,0,'failed confirmation does not freeze the whole page indefinitely');
  assert.match(message,/中断の完了を確認できません/);
  assert.equal($('builder-cancel').disabled,false);
  await submitJob(0,'');assert.equal(posts,1);
  await cancelJob();assert.equal(cancellations,2);
}
assert.equal(posts,outcome==='quota-retry'?2:1);
assert.equal(reservations,1,'resumed audio keeps the original registered submission ID');
assert(!status.includes('音源の送信を中断しました'));
if(outcome==='before'){
  assert.equal(submitBusy,false);assert.equal(leases,0);assert.equal(status,'中断しました');
  assert.equal(watched.length,0);
}else{
  assert.equal(currentJob,'1234abcd');assert.deepEqual(watched,['1234abcd']);
  assert.equal(cancelPendingJob,currentJob);
  assert($('builder-cancel').disabled,'wait for terminal job status');
  assert(leases>0,'retain cancellation ownership through terminal polling');
  assert.notEqual(status,'中断しました','an acknowledgement is not terminal confirmation');
  // The normal terminal poll invokes finish(), which releases this operation.
  setCancelPending(false);assert.equal(leases,0);
}
''')
