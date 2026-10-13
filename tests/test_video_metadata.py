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
  let p=window.VideoMetadata.notifyChange('song'); await tick();
  assert.equal(opened,1); assert.match($('metadata-change-hint').textContent,/保持/);
  close('',true); await p;
  assert.equal(values.size,0,'Escape does not save suppression');
  p=window.VideoMetadata.notifyChange('song'); await tick();
  close('acknowledge',true); await p;
  await window.VideoMetadata.notifyChange('song'); assert.equal(opened,2);
  p=window.VideoMetadata.notifyChange('wordlist'); await tick();
  assert.equal(opened,3,'wordlist preference independent of song');
  close('update',true); assert.equal(await p,'update');
  assert.equal(await window.VideoMetadata.notifyChange('wordlist'),'update');
  assert.equal(opened,3);
  blocked=true;
  p=window.VideoMetadata.notifyChange('song'); await tick();
  assert.equal(opened,4); close('acknowledge',true); await p;
  const first=window.VideoMetadata.notifyChange('song');
  let current=true;
  const stale=window.VideoMetadata.notifyChange('wordlist',()=>current);
  await tick(); assert.equal(opened,5,'only one notice at a time');
  current=false; close('acknowledge'); await first; await stale;
  assert.equal(opened,5,'superseded selection never opens a stale notice');
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
  assert.deepEqual(notices,['song']);
  notices=[]; $('song-title').value=''; $('song-credit').value='';
  await updateUploadedSongMetadata(file); assert.equal($('song-title').value,'new');
  assert.deepEqual(notices,[]);
  $('song-title').value=''; active={song:{title:'連携した曲'}};
  await updateUploadedSongMetadata(file); assert.equal($('song-title').value,'連携した曲');
  notices=[]; $('song-title').value=''; $('song-credit').value='連携した表記';
  await updateUploadedSongMetadata(file); assert.equal($('song-credit').value,'連携した表記');
  assert.deepEqual(notices,['song']);
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
