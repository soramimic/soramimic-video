"""A stalled response must not hold startup or song verification indefinitely."""

from test_index_ui import _song_input_node_harness
from test_input_lifecycle import DOM, _functions, _run

STALL = r"""
const controllers=[];
const AbortSignal={timeout:ms=>{
  assert(ms>0 && ms<=20000);
  const controller=new AbortController(); controllers.push(controller);
  return controller.signal;
}};
let phase='headers', requests=0;
const waitForAbort=signal=>new Promise((_,reject)=>{
  signal?.addEventListener('abort',()=>reject(signal.reason),{once:true});
});
const fetch=async (_url,options={})=>{
  requests++;
  if(phase==='headers') return waitForAbort(options.signal);
  const read=()=>phase==='body'?waitForAbort(options.signal):Promise.resolve(responseBody);
  return {ok:true,json:read,text:read};
};
const expire=()=>{
  assert(controllers.length>0,'request must have a deadline');
  controllers.at(-1).abort(new DOMException('timed out','TimeoutError'));
};
"""


def test_sample_catalog_stalls_settle_and_can_be_retried():
    harness = DOM + STALL + r"""
let sampleCatalog=[], sampleDescriptions={}, sampleCredits={};
let sampleInputKinds={}, sampleLicenseUrls={};
let responseBody=[], message='', renders=0;
const renderSampleOptions=()=>{renders++;}, showBuilderMsg=text=>{message=text;};
""" + _functions("async function loadSamples()")
    _run(harness, r"""
for(const failure of ['headers','body']) {
  phase=failure;
  const pending=loadSamples(); await tick(); expire(); await pending;
  assert($('builder-sample-field').hidden);
  assert.match(message,/曲一覧を読み込めません/);
  phase='ok'; responseBody=[{id:'current',description:'song'}];
  await loadSamples(); assert.equal(sampleCatalog[0].id,'current');
  assert.equal(sampleDescriptions.current,'song');
}
assert.equal(renders,2); assert.equal(requests,4);
""")


def test_wordlist_settings_stalls_release_startup():
    harness = DOM + STALL + r"""
let wordlistGroups=[], responseBody={wordlist:[]}, syncs=0;
const flattenWordlist=value=>value, HIDDEN_UI_WORDLISTS=new Set(), launchWordlists=[];
const syncBuilderOptions=()=>{syncs++;};
const headers=()=>({}), document={createElement:element}, applyWordlistSelection=()=>{};
""" + _functions("async function loadWordlistSelect(")
    _run(harness, r"""
for(const failure of ['headers','body']) {
  phase=failure; const pending=loadWordlistSelect(true);
  await tick(); expire(); await pending;
  assert.equal($('wordlist-select').hidden,true);
  assert.deepEqual(wordlistGroups,[]);
}
assert.equal(syncs,2);
phase='ok'; responseBody={wordlist:[{filepath:'baseball.csv',text:'野球選手名'}]};
await loadWordlistSelect(true); assert.equal(requests,3);
assert.equal($('wordlist-select').hidden,false);
assert.equal(wordlistGroups[0].name,'baseball');
""")


def test_midi_verification_stalls_release_waiters_and_preserve_selected_file():
    harness = DOM + STALL + r"""
let midiCheckSeq=0, midiSampleId='', lastMidiCheck=null, responseBody;
const ownSongKind=()=>'midi', selectedSampleIsAudio=()=>false, songLyricsForRequest=()=>'';
const headers=()=>({}), clearMidiNotices=()=>{}, renderLyricsMidiWarning=()=>{};
const rejectMidi=()=>{throw Error('network failure must not reject the song');};
""" + _functions("async function checkMidi(")
    _run(harness, r"""
const file=new File(['midi'],'song.mid'); $('midi').files=[file];
for(const failure of ['headers','body']) {
  phase=failure; const checking=checkMidi();
  await tick(); expire(); await checking;
  assert.equal($('midi').files[0],file);
  assert.equal(lastMidiCheck,null);
}
phase='ok'; responseBody={has_lyrics:true,midi_lines:['あいうえお']};
await checkMidi(); assert.equal(lastMidiCheck.file,file);
assert.deepEqual(lastMidiCheck.lines,['あいうえお']);
""")


def test_sample_lyrics_stalls_preserve_input_and_allow_reselection():
    harness = _song_input_node_harness() + STALL + r"""
let responseBody='new lyrics';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
"""
    _run(harness, r"""
$('sample-select').value='furusato'; $('lyrics').value='original lyrics';
for(const failure of ['headers','body']) {
  phase=failure; const pending=applySample();
  await tick(); expire(); assert.equal(await pending,false);
  assert.equal($('lyrics').value,'original lyrics');
  assert.equal(midiSampleId,'');
  assert($('sample-status').textContent);
}
phase='ok'; assert.equal(await applySample(),true);
assert.equal(midiSampleId,'furusato'); assert.equal($('lyrics').value,'new lyrics');
""")
