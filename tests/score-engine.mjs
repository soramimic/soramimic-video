import assert from 'node:assert/strict';
import test from 'node:test';
import { BrowserSinger, synthesisNotes, wavBlob, SAMPLE_RATE } from '../src/soramimic_video/static/score-engine.mjs';
const note = {line:0, text:'キャー', start:.5, end:1, pitch:60};

test('synthesis preserves absolute seconds and pitch, voices recognized kana, and rejects overlapping notes', () => {
  assert.deepEqual(synthesisNotes([note]), [{id:0,line:0,lyric:'きゃー',onset:240,dur:240,pitch:60}]);
  assert.throws(()=>synthesisNotes([note,{...note,start:.75,end:1.5}]),/重なって/);
  assert.throws(()=>synthesisNotes([{...note,end:40}]),/30秒/);
  assert.throws(()=>synthesisNotes([{...note,start:901,end:902}]),/15分/);
});

test('worker synthesis combines predicted plain pitch and vibrato, aligns pre-roll, and emits a playable WAV', async t => {
  const previous = globalThis.Worker; t.after(()=>{globalThis.Worker=previous;});
  const calls=[];
  globalThis.Worker=class {
    postMessage(message) {
      calls.push(message);
      const result=message.type==='init'?{}:message.type==='analyze'?
        {phrases:[{key:'p',plain:new Float32Array([8,9]),vib:new Float32Array([.1,.2]),start_sec:-1/SAMPLE_RATE}]}:
        {wav:new Float32Array([.1,.2,.3,.4])};
      queueMicrotask(()=>this.onmessage({data:{type:'result',id:message.id,result}}));
    }
    terminate() {}
  };
  const singer=new BrowserSinger();
  const blob=await singer.synthesize([note]);
  const view=new DataView(await blob.arrayBuffer());
  assert.equal(new TextDecoder().decode(new Uint8Array(view.buffer,0,4)),'RIFF');
  assert.equal(view.getUint32(24,true),44100);
  assert(Math.abs(view.getInt16(44,true)/32767-.2)<.0001);
  assert.equal(calls[1].req.notes[0].onset,240);
  assert.equal(calls[2].req.singer,2);
  assert(Math.abs(calls[2].req.f0_log2[0]-8.1)<.00001);
  singer.close();
});

test('cancel terminates the worker and rejects pending synthesis calls', async t => {
  const previous=globalThis.Worker;t.after(()=>{globalThis.Worker=previous;});let terminated=false;
  globalThis.Worker=class {postMessage(){} terminate(){terminated=true;}};
  const singer=new BrowserSinger(), pending=singer.ready();singer.close();
  await assert.rejects(pending,{name:'AbortError'});assert.equal(terminated,true);
  assert.equal(singer.requests.size,0);
  assert.throws(()=>wavBlob(new Float32Array([NaN])),/不正/);
});
