"""Video generation requires a wordlist unless edited lyrics supply the result."""

import shutil

import pytest

from ui_helpers import DOM, _functions, _run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


def _harness():
    return DOM + r"""
let simpleMode=false, songInputMode='upload', customList=null;
let submitBusy=false, lyricsFileReading=false, recording=false, submitAbort=null;
let builderState='preview', turnstileSiteKey='', verification=null, message='', focused='';
const songRecordingBusy=()=>recording, activeCustomList=()=>simpleMode?null:customList;
const requests=[], audio=new File(['audio'],'song.wav');
const ownAudioFile=()=>audio, ownSongFile=()=>audio, ownSongKind=()=>'audio';
const samplePending=null, midiChecking=null, midiSampleId='', MAX_EDITOR_BYTES=4*1024*1024;
const ensureSelectedSampleMidi=async()=>true, parodyMismatch=()=>false;
const lyricReviewCoverage=()=>({unmatched:false}), apiKey=()=>'';
const fixedSynthesizer='voicevox', fixedVoicevoxStyle=1;
const selectedTitleWords=()=>'', songTitleOf=()=>'song', buildConvertParams=()=>'';
const showsEditorWordlist=()=>false, appendSongLyrics=()=>{};
const appendCustomWordlist=form=>{if(customList)form.set('wordlist_text',customList.text);};
const showSubmitMsg=text=>{message=text;}, showProgress=()=>{}, setJobStatus=()=>{};
const hideTurnstilePrompt=()=>{}, resetTurnstile=()=>{}, setCancelPending=()=>{};
const ensureTurnstileToken=()=>{verification=deferred();return verification.promise;};
const turnstileToken=()=>'verified';
const setBusy=value=>{submitBusy=value;updateBuilderOverlay();};
const watch=()=>setBusy(false);
const postJobWithTurnstileRetry=async form=>{requests.push(form);
  return {ok:true,status:200,json:async()=>({id:'job'})};};
$('builder-figure').hidden=false; $('builder-loading').hidden=true;
$('auto-lyrics').checked=true;
$('builder-wordlist-trigger').focus=()=>{focused='wordlist';};
const refreshTitleChoices=()=>{}, builderLive=true;
const OWN_SONG_VALUE='__own_song__';
const currentCombo=()=>({sampleId:'__own_song__',wordlistName:$('wordlist').value});
const currentWordlistWhere=()=>'', showBuilderMsg=()=>{}, renderBuilder=()=>{};
let previewComboKey='', previewKey='', previewShowImages=false, previewTimer=null;
const PREVIEW_DEBOUNCE_MS=300;
const setTimeout=()=>1, clearTimeout=()=>{};
""" + _functions(
        "function hasVideoWordlist(", "function updateBuilderOverlay()",
        "function schedulePreview(", "function automaticLyricsEnabled()",
        "function lyricsInput()", "function activeLyricsJson()", "function readParodyText(",
        "function editorSourceForSubmit()", "async function readParodyFile(",
        "async function submitJob(", "function captureGenerationThumbnail()",
    )


def test_wordlist_changes_update_generation_controls_before_thumbnail_loading():
    _run(_harness(), r"""
schedulePreview();
assert($('builder-play').disabled);
assert.equal($('builder-play').hidden,false);
assert.match($('builder-play-label').textContent,/単語リストを選んで/);
assert.match($('builder-play')['aria-label'],/単語リストを選んで/);
for(const id of ['preview','preview-high','preview-low'])assert.equal($(id).disabled,false);
for(const value of ['stations','','  ']) {
  $('wordlist').value=value; schedulePreview();
  assert.equal($('builder-play').disabled,!value.trim());
}
customList={name:'自作',text:'ねこ,ネコ'}; schedulePreview();
assert.equal($('builder-play').disabled,false);
customList=null; schedulePreview(); assert($('builder-play').disabled);
$('lyrics').value=JSON.stringify({results:[[]],unitsList:[[]]});
$('auto-lyrics').checked=false; schedulePreview();
assert.equal($('builder-play').disabled,false,'edited lyrics need no selection');
assert.equal($('builder-play-label').textContent,'タップで動画を生成');
$('auto-lyrics').checked=true; schedulePreview();
assert($('builder-play').disabled,'automatic recognition ignores stored JSON');
$('auto-lyrics').checked=false; simpleMode=true; schedulePreview();
assert($('builder-play').disabled,'simple mode cannot submit edited JSON');
simpleMode=false; $('lyrics').value='普通の歌詞'; schedulePreview();
assert($('builder-play').disabled,'plain lyrics cannot replace a wordlist');
$('wordlist').value='stations';
for(const busy of ['submit','lyrics','recording','none']) {
  submitBusy=busy==='submit'; lyricsFileReading=busy==='lyrics'; recording=busy==='recording';
  schedulePreview(); assert.equal($('builder-play').disabled,busy!=='none');
}
""")


def test_submission_blocks_missing_wordlist_but_accepts_custom_json_and_voice_preview():
    _run(_harness(), r"""
await submitJob(0);
assert.equal(requests.length,0); assert.equal(submitBusy,false);
assert.match(message,/単語リストを選んで/); assert.equal(focused,'wordlist');
$('wordlist').value='stations'; await submitJob(0);
assert.equal(requests.length,1); assert.equal(requests.at(-1).get('wordlist'),'stations');
$('wordlist').value=''; customList={name:'自作',text:'ねこ,ネコ'};
await submitJob(0); assert.equal(requests.length,2);
assert.equal(requests.at(-1).get('wordlist_text'),customList.text);
customList=null;
const payload={results:[[]],unitsList:[[]],wordlist:{textOnly:true}};
$('lyrics').value=JSON.stringify(payload); $('auto-lyrics').checked=false;
await submitJob(0); assert.equal(requests.length,3);
assert.deepEqual(JSON.parse(await requests.at(-1).get('editor').text()),payload);
$('auto-lyrics').checked=true; await submitJob(0);
assert.equal(requests.length,3,'automatic recognition requires a selected list');
$('lyrics').value='普通の歌詞'; $('auto-lyrics').checked=false;
await submitJob(0); assert.equal(requests.length,3);
$('auto-lyrics').checked=true;
for(const mode of ['head','high','low'])await submitJob(20,mode);
assert.equal(requests.length,6,'voice previews do not convert lyrics');
assert.equal(submitBusy,false);
""")


def test_clearing_wordlist_during_verification_stops_submission():
    _run(_harness(), r"""
turnstileSiteKey='site'; $('wordlist').value='stations';
const pending=submitJob(0); await tick();
assert(verification); assert(submitBusy);
$('wordlist').value=''; verification.resolve(true); await pending;
assert.equal(requests.length,0); assert.equal(submitBusy,false);
assert.match(message,/単語リストを選んで/); assert.equal(focused,'wordlist');
// A missing list must be caught before requesting verification again.
verification=null; await submitJob(0);
assert.equal(verification,null); assert.equal(requests.length,0);
""")
