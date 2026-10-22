"""Background history refresh preserves playback, transfers and their job identities."""

import pytest

from test_input_lifecycle import DOM, _functions, _run


def test_background_refresh_keeps_media_connected_and_actions_bound_to_the_same_job():
    harness = r"""
const assert=require('node:assert/strict');
const {File}=require('node:buffer');
let pauses=0;
class Element extends EventTarget {
  constructor(tag='div') {
    super();this.tagName=tag.toUpperCase();this.children=[];this.parentElement=null;
    this.className='';this.attributes={};this.dataset={};this.textContent='';
    this.classList={add:name=>{this.className+=' '+name;}};
  }
  get isConnected(){return this===document.body || !!this.parentElement?.isConnected;}
  append(...children){children.forEach(child=>this.appendChild(child));}
  prepend(child){this.insertBefore(child,this.children[0]||null);}
  appendChild(child){this.insertBefore(child,null);return child;}
  insertBefore(child,before){
    if(child.parentElement)child.remove();
    const index=before?this.children.indexOf(before):this.children.length;
    assert(index>=0);this.children.splice(index,0,child);child.parentElement=this;
  }
  replaceChildren(...children){[...this.children].forEach(child=>child.remove());this.append(...children);}
  remove(){
    const mediaNodes=this.all().filter(x=>['AUDIO','VIDEO'].includes(x.tagName));
    if(this.isConnected)for(const media of mediaNodes) {
      media.pause();
    }
    if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(x=>x!==this);
    this.parentElement=null;
  }
  all(){return [this,...this.children.flatMap(child=>child.all())];}
  matches(selector){return this.className.split(' ').includes(selector.slice(1));}
  closest(selector){return this.matches(selector)?this:this.parentElement?.closest(selector);}
  querySelector(selector){return this.all().slice(1).find(x=>x.matches(selector));}
  setAttribute(k,v){this.attributes[k]=v;}
  removeAttribute(k){delete this.attributes[k];}
  load(){}
  play(){this.paused=false;return Promise.resolve();}
  pause(){this.paused=true;pauses++;}
}
const document={body:new Element('body'),createElement:tag=>new Element(tag)};
const historyBody=new Element();document.body.appendChild(historyBody);
const $=id=>{assert.equal(id,'history-body');return historyBody;};
const navigator={share(){}}, apiKey=()=>'', qs=x=>x, headers=()=>({});
const historyCreatedLabel=()=>'', historyStatusLabel=()=>'', appendLyricEditAction=()=>{};
const prefersNativeVideoSave=()=>true, historyReviewBlocked=()=>false;
const saves=[], shares=[];
const saveHistoryVideo=index=>saves.push(historyJobs[index].id);
const shareHistoryResult=index=>shares.push(historyJobs[index].id);
const appendSaveMenu=(actions,_job,save)=>{
  const button=new Element('button'),status=new Element();
  button.className='save';actions.appendChild(button);actions.save=save;
  return {button,status};
};
let activeHistoryPlayback=null, preparedHistorySave=null, preparedHistoryShare=null, historyJobs=[];
let fetch=async()=>({ok:false});
const job=id=>({id,status:'done',result_kind:'video',
  playback_url:`/${id}/playback`,video_url:`/${id}/video`});
""" + _functions(
        "function renderHistoryJobs(", "async function fetchHistory()",
        "function historyCard(", "function historyButton(",
        "async function startHistoryPlayback(", "function disposeHistoryPlayback()",
        "function resetHistorySave()", "function resetHistoryShare()",
    )
    _run(harness, r"""
const a=job('a'),b=job('b'),c=job('c');renderHistoryJobs([a,b]);
const list=historyBody.querySelector('.history-list');
const [cardA,cardB]=list.children;
await startHistoryPlayback(0,cardA.querySelector('.history-thumb'));
const playing=activeHistoryPlayback;playing.media.currentTime=12;
const save=cardB.querySelector('.save'),share=cardA.querySelector('.history-share');
preparedHistorySave={index:1,jobId:'b',url:b.video_url,button:save,status:new Element(),file:{}};
preparedHistoryShare={index:0,jobId:'a',url:a.video_url,button:share,file:{}};
const pendingSave=preparedHistorySave,pendingShare=preparedHistoryShare;
renderHistoryJobs([c,a,b]);
assert.equal(historyBody.querySelector('.history-list'),list);
assert.equal(list.children[1],cardA);assert.equal(list.children[2],cardB);
assert.equal(activeHistoryPlayback,playing);assert.equal(playing.media.isConnected,true);
assert.equal(playing.media.currentTime,12);assert.equal(playing.media.paused,false);assert.equal(pauses,0);
assert.equal(preparedHistorySave,pendingSave);assert.equal(preparedHistorySave.index,2);
assert.equal(preparedHistoryShare,pendingShare);assert.equal(preparedHistoryShare.index,1);
cardB.querySelector('.history-actions').save();assert.deepEqual(saves,['b']);
share.dispatchEvent(new Event('click'));assert.deepEqual(shares,['a']);
await fetchHistory();await fetchHistory();
assert.equal(activeHistoryPlayback,playing);assert.equal(pauses,0);
assert.deepEqual(historyJobs,[c,a,b],'failed refresh must preserve old indices and actions');
assert.equal(historyBody.children.filter(x=>x.className==='history-load-error').length,1);
renderHistoryJobs([c,b]);
assert.equal(activeHistoryPlayback,null);assert(playing.media.paused);
assert.equal(preparedHistorySave,pendingSave);assert.equal(pendingSave.index,1);
assert.equal(preparedHistoryShare,null);
renderHistoryJobs([]);assert.equal(preparedHistorySave,null);
assert.equal(save.disabled,false);assert.equal(historyBody.textContent,'まだありません');
""")


@pytest.mark.parametrize("surface", ["save", "share"])
@pytest.mark.parametrize("phase", ["headers", "body"])
def test_stalled_history_transfer_releases_its_button_for_retry(surface, phase):
    harness = DOM + f"const surface={surface!r},phase={phase!r};\n" + r"""
let historyJobs=[{id:'one',video_url:'/one/video'}];
let preparedHistorySave=null,preparedHistoryShare=null,requests=0,hang=true;
const timers=new Map();let sequence=0;
const setTimeout=(fn,ms)=>{assert(ms>0 && ms<=120000);timers.set(++sequence,fn);return sequence;};
const clearTimeout=id=>timers.delete(id);
const headers=()=>({}),prefersNativeVideoSave=()=>true;
const navigator={userActivation:{isActive:false},share(){}};
const openFileShare=()=>{throw Error('expired user activation requires another tap');};
const fetch=async (_url,{signal})=>{
  requests++;
  const stalled=()=>new Promise((_,reject)=>{
    signal.addEventListener('abort',()=>reject(signal.reason));
  });
  if(hang && phase==='headers')return stalled();
  return {ok:true,blob:()=>hang?stalled():Promise.resolve(new Blob(['video'],{type:'video/mp4'}))};
};
const button=$('save'),status=$('status');button.isConnected=true;
button.removeAttribute=key=>{delete button[key];};
const start=()=>surface==='save'?saveHistoryVideo(0,button,status):shareHistoryResult(0,button);
""" + _functions(
        "async function saveHistoryVideo(", "async function shareHistoryResult(",
        "function resetHistorySave()", "function resetHistoryShare()",
    )
    _run(harness, r"""
const pending=start();await tick();assert(button.disabled);assert.equal(requests,1);
await start();assert.equal(requests,1,'pending preparation stays exclusive');
assert.equal(timers.size,1);[...timers.values()][0]();await pending;
assert.equal(button.disabled,false);assert.equal(timers.size,0);
const state=surface==='save'?preparedHistorySave:preparedHistoryShare;
assert.equal(state.abort,null);assert.equal(state.file,null);
hang=false;await start();assert.equal(requests,2);assert.equal(button.disabled,false);
const ready=surface==='save'?preparedHistorySave:preparedHistoryShare;
assert(ready.file);assert.equal(timers.size,0);
""")
