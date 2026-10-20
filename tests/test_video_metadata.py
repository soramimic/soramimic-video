"""Display metadata survives song/list changes and browser transfers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/soramimic_video/static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def run_node(code: str) -> None:
    result = subprocess.run(
        ["node", "-e", code], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def function(name: str) -> str:
    source = (STATIC / "index.html").read_text()
    match = re.search(rf"^(?:async )?function {name}\([^\n]*\) \{{.*?^\}}", source, re.M | re.S)
    assert match
    return match.group()


def test_notice_dismissal_preferences_queue_and_storage_failure():
    run_node(r'''
const assert = require('node:assert/strict');
const values = new Map(), elements = new Map();
let closes = [], opened = 0;
const dialog = {returnValue:'', addEventListener: (_, fn) => closes.push(fn),
  showModal: () => {opened++;}};
const $ = id => {
  if (!elements.has(id)) elements.set(id, {checked:false, textContent:'', focus(){}});
  return elements.get(id);
};
const window = {};
const document = {getElementById: id => id === 'metadata-change-dialog' ? dialog : $(id)};
let blocked = false;
const localStorage = {
  getItem: key => {if (blocked) throw Error(); return values.get(key);},
  setItem: (key, value) => {if (blocked) throw Error(); values.set(key,value);},
};
const tick = () => new Promise(resolve => setImmediate(resolve));
function close(value, checked=false) {
  dialog.returnValue=value; $('metadata-change-hide-warning').checked=checked;
  closes.shift()();
}
''' + (STATIC / "metadata-change.js").read_text() + r'''
(async () => {
  values.set('soramimic-metadata-notice-hidden-song','1'); // Legacy notice suppression.
  let p=window.VideoMetadata.notifyChange('song-replacement'); await tick();
  assert.equal(opened,1,'legacy notice suppression does not hide the new choice');
  assert.match($('metadata-change-update').textContent,/ファイル名を使う/);
  assert.match($('metadata-change-hint').textContent,/クレジット表記は保持/);
  close('',true); assert.equal(await p,'keep');
  assert.equal(values.size,1,'Escape does not save a choice');
  p=window.VideoMetadata.notifyChange('song-replacement'); await tick();
  close('keep',true); assert.equal(await p,'keep');
  assert.equal(await window.VideoMetadata.notifyChange('song-replacement'),'keep');
  assert.equal(opened,2);
  p=window.VideoMetadata.notifyChange('wordlist'); await tick();
  assert.equal(opened,3,'wordlist preference is independent');
  close('update',true); assert.equal(await p,'update');
  assert.equal(await window.VideoMetadata.notifyChange('wordlist'),'update');
  assert.equal(opened,3);
  blocked=true;
  p=window.VideoMetadata.notifyChange('song-replacement'); await tick();
  assert.equal(opened,4); close('keep',true); await p;
  const first=window.VideoMetadata.notifyChange('song-replacement');
  let current=true;
  const stale=window.VideoMetadata.notifyChange('wordlist',()=>current);
  await tick(); assert.equal(opened,5,'only one dialog at a time');
  current=false; close('keep'); await first; await stale;
  assert.equal(opened,5,'a queued stale choice never opens');
  blocked=false;
  values.delete('soramimic-metadata-notice-hidden-song-replacement');
  p=window.VideoMetadata.notifyChange('song-replacement'); await tick();
  close('update',true); assert.equal(await p,'update');
  assert.equal(await window.VideoMetadata.notifyChange('song-replacement'),'update');
  assert.equal(opened,6);
})().catch(err=>{console.error(err);process.exit(1);});
''')


def test_song_and_wordlist_changes_preserve_inputs_and_only_notify_when_needed():
    run_node(r'''
const assert=require('node:assert/strict');
const elements=new Map();
const $=id=>{
  if(id==='wordlist') return {get value(){return name;}};
  if(!elements.has(id)) elements.set(id,{value:'',dispatchEvent(){}});
  return elements.get(id);
};
let file={name:'new.wav'}, active=null, notices=[], choice='keep';
const ownSongFile=()=>file, activeLyricsJson=()=>active;
const VideoMetadata={notifyChange:async(kind,current)=>{
  if(current()) notices.push(kind); return choice;
}};
let metadataWordlistKey='', metadataWordlistVersion=0;
let activeCustomListId='', name='', restoring=false;
const currentWordlistName=()=>name, showsEditorWordlist=()=>false;
const updateSongTextPreviews=()=>{};
const location={href:'https://dev-video.soramimic.com/'};
''' + function("songFileStem") + function("updateUploadedSongMetadata")
             + function("updateSelectedWordlistMetadata") + r'''
(async()=>{
  $('song-title').value='手入力'; $('song-credit').value='作詞・作曲';
  await updateUploadedSongMetadata(file);
  assert.equal($('song-title').value,'手入力'); assert.equal($('song-credit').value,'作詞・作曲');
  assert.deepEqual(notices,['song-replacement']);
  notices=[]; $('song-title').value=''; $('song-credit').value='';
  await updateUploadedSongMetadata(file); assert.equal($('song-title').value,'');
  assert.deepEqual(notices,[]);
  $('song-title').value=''; active={song:{title:'連携した曲'}};
  await updateUploadedSongMetadata(file);
  assert.equal($('song-title').value,'','upload does not copy an implicit JSON title');
  notices=[]; $('song-title').value=''; $('song-credit').value='連携した表記';
  await updateUploadedSongMetadata(file); assert.equal($('song-credit').value,'連携した表記');
  assert.deepEqual(notices,[],'credit alone does not require a title choice');
  notices=[]; $('thumbnail-wordlist-label').value='表示名'; name='stations';
  await updateSelectedWordlistMetadata();
  assert.equal($('thumbnail-wordlist-label').value,'表示名');
  assert.deepEqual(notices,['wordlist']);
  notices=[]; await updateSelectedWordlistMetadata(); assert.deepEqual(notices,[]);
  name='pokemon'; restoring=true; await updateSelectedWordlistMetadata();
  assert.deepEqual(notices,[]);
  restoring=false; activeCustomListId='custom'; await updateSelectedWordlistMetadata();
  assert.deepEqual(notices,['wordlist']);
  choice='update'; activeCustomListId='updated'; await updateSelectedWordlistMetadata();
  assert.equal($('thumbnail-wordlist-label').value,'','update follows selected list name');
  notices=[]; $('thumbnail-wordlist-label').value=''; activeCustomListId='another';
  await updateSelectedWordlistMetadata(); assert.deepEqual(notices,[]);
  $('thumbnail-wordlist-label').value='保持'; name='new'; activeCustomListId='';
  location.href+='?lyrics-transfer=nonce'; await updateSelectedWordlistMetadata();
  assert.deepEqual(notices,[]);
})().catch(err=>{console.error(err);process.exit(1);});
''')


def test_bridge_only_carries_known_display_metadata():
    run_node('const assert=require("node:assert/strict"); const window={};\n'
             + (STATIC / "app-bridge.js").read_text() + r'''
const payload={results:[[]],unitsList:[[]],videoMetadata:{
 songTitle:'題名',songCredit:'表記',thumbnailWordlistLabel:'表示名',secret:'private'}};
assert.deepEqual(window.SoramimicBridge.portable(payload).videoMetadata,{
 songTitle:'題名',songCredit:'表記',thumbnailWordlistLabel:'表示名'});
payload.videoMetadata={songTitle:123,songCredit:'x'.repeat(3000),thumbnailWordlistLabel:''};
assert.deepEqual(window.SoramimicBridge.portable(payload).videoMetadata,{
 songCredit:'x'.repeat(2000),thumbnailWordlistLabel:''});
''')


def test_metadata_notice_script_is_served(tmp_path):
    from fastapi.testclient import TestClient

    from soramimic_video.api import create_app

    # Script delivery uses the same static route in every UI mode.
    with TestClient(create_app(jobs_dir=tmp_path / "jobs")) as client:
        response = client.get("/metadata-change.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert "notifyChange" in response.text
        assert json.dumps("metadata-change-dialog") in response.text


def test_upload_choices_update_only_title_and_ignore_stale_answers():
    run_node(r'''
const assert=require('node:assert/strict');
const elements=new Map();
const $=id=>{
  if(!elements.has(id)) elements.set(id,{value:'',dispatchEvent(){}});
  return elements.get(id);
};
let file={name:'new.xf.mid'}, choice='keep', notices=[], pending=null;
const ownSongFile=()=>file;
const activeLyricsJson=()=>({song:{title:'前の曲'}});
const VideoMetadata={notifyChange:async(kind,current)=>{
  notices.push(kind); if(pending) return pending; return choice;
}};
''' + function("songFileStem") + function("updateUploadedSongMetadata") + r'''
(async()=>{
  $('song-title').value='連携した曲'; $('song-credit').value='連携したクレジット';
  choice='keep'; await updateUploadedSongMetadata(file);
  assert.equal($('song-title').value,'連携した曲');
  assert.equal($('song-credit').value,'連携したクレジット');
  assert.deepEqual(notices,['song-replacement']);
  choice='update'; await updateUploadedSongMetadata(file);
  assert.equal($('song-title').value,'','use the filename by clearing the override');
  assert.equal($('song-credit').value,'連携したクレジット','a title choice never clears credits');
  let resolve; pending=new Promise(r=>{resolve=r;});
  $('song-title').value='入力済み'; $('song-credit').value='表記';
  let update=updateUploadedSongMetadata(file);
  file={name:'newer.wav'}; resolve('update'); await update;
  assert.equal($('song-title').value,'入力済み','late choice cannot overwrite a newer source');
  pending=new Promise(r=>{resolve=r;}); update=updateUploadedSongMetadata(file);
  $('song-title').value='新しく編集した曲名'; resolve('update'); await update;
  assert.equal($('song-title').value,'新しく編集した曲名');
  pending=null; notices=[]; $('song-title').value=''; $('song-credit').value='';
  await updateUploadedSongMetadata(file);
  assert.equal($('song-title').value,'','upload leaves a blank override untouched');
  assert.deepEqual(notices,[],'blank fields need no confirmation');
})().catch(err=>{console.error(err);process.exit(1);});
''')


def test_every_explicit_upload_checks_title_choices_but_restore_and_invalid_files_do_not():
    script = (STATIC / "index.html").read_text()
    start = script.index(
        '$("midi").addEventListener("change", () => {\n  const file = ownSongFile();'
    )
    end = script.index('\n});', start) + len('\n});')
    run_node(r'''
const assert=require('node:assert/strict');
const elements=new Map();
const $=id=>{
  if(!elements.has(id)) elements.set(id,{value:'',files:[],listeners:{},
    addEventListener(type,fn){this.listeners[type]=fn;},
    dispatchEvent(event){this.listeners[event.type]?.(event);}});
  return elements.get(id);
};
let file=null, restoring=false, midiFromSample=false;
let editorImportedFile=null, midiSampleId='', calls=[];
const ownSongFile=()=>file, ownSongKind=f=>f.name.endsWith('.txt')?'':'midi';
const updateUploadedSongMetadata=f=>calls.push(f.name);
const setRecordingStatus=()=>{}, clearEditorFile=()=>{}, clearAudioPresentation=()=>{};
const syncLyricsRecognition=()=>{}, syncBuilderOptions=()=>{}, syncBuilderValues=()=>{};
const showBuilderMsg=()=>{}, schedulePreview=()=>{};
const audioInputReady=true, maxAudioUploadBytes=1000;
const document={querySelector:()=>({textContent:''})};
const rejectOwnSongFile=()=>{file=null; $('midi').dispatchEvent(new Event('change'));};
''' + script[start:end] + r'''
function select(f){file=f; $('midi').dispatchEvent(new Event('change'));}
select({name:'first.mid'}); select({name:'replacement.mid'});
assert.deepEqual(calls,['first.mid','replacement.mid']);
select(null); select({name:'after-clear.mid'});
assert.equal(calls.at(-1),'after-clear.mid');
const before=calls.length;
restoring=true; select({name:'restored.mid'}); restoring=false;
assert.equal(calls.length,before);
select({name:'after-restore.mid'}); assert.equal(calls.at(-1),'after-restore.mid');
select({name:'invalid.txt'}); select({name:'after-rejection.mid'});
assert.equal(calls.at(-1),'after-rejection.mid');
''')
