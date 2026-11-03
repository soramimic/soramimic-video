import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';
import { parseHTML } from 'linkedom';
import { start } from '../src/soramimic_video/static/score-editor.mjs';
import { openDocument, lyricMidi } from '../src/soramimic_video/static/score-editor-io.mjs';

const html = await readFile(new URL('../src/soramimic_video/static/timing_editor.html', import.meta.url), 'utf8');

async function fixture() {
  const { window, document } = parseHTML(html);
  const canvas = document.getElementById('cv');
  canvas.getContext = () => new Proxy({}, { get: () => () => {}, set: () => true });
  const context = vm.createContext({ window, document, innerWidth: 1200, innerHeight: 800,
    Audio: class {
      currentTime = 0; paused = true; src = '';
      addEventListener() {} pause() { this.paused = true; } load() {}
      play() { this.paused = false; return Promise.resolve(); }
    },
    location: { pathname: '/score-editor', search: '' }, URLSearchParams, structuredClone,
    addEventListener: window.addEventListener.bind(window), requestAnimationFrame: () => {},
    fetch: () => assert.fail('Editing and saving must not upload data'),
  });
  const source = html.match(/<script>([\s\S]*)<\/script>/u)[1].split("if(localMode){\n $('local-help')")[0];
  vm.runInContext(source + `\nwindow.editor = {
    load, select, undo, redo, setSave:f=>{localSave=f}, save,
    read:()=>structuredClone(moras), dirty:()=>dirty, saved:()=>{dirty=false}
  };`, context);
  await window.editor.load({ moras: [
    { id: 0, line: 0, start: 1, end: 1.5, pitch: 60, text: 'ソ' },
    { id: 1, line: 0, start: 1.5, end: 2, pitch: 64, text: 'ラ' },
  ], line_texts: { 0: '空' }, has_audio: false });
  return { window, document, editor: window.editor };
}

test('pitch/lyric edits, undo/redo and saving work without any server request', async () => {
  const { window, document, editor } = await fixture();
  editor.select(0);
  const pitch = document.getElementById('ppitch');
  pitch.value = '62'; pitch.dispatchEvent(new window.Event('change'));
  assert.equal(editor.read()[0].pitch, 62);
  assert.equal(editor.dirty(), true);
  editor.undo(); assert.equal(editor.read()[0].pitch, 60);
  editor.redo(); assert.equal(editor.read()[0].pitch, 62);
  editor.select(0);
  const text = document.getElementById('ptext');
  text.value = 'ア'; text.dispatchEvent(new window.Event('change'));
  assert.equal(editor.read()[0].text, 'ア');
  let saved;
  editor.setSave(() => { saved = editor.read(); editor.saved(); });
  await editor.save();
  assert.equal(saved[0].text, 'ア');
  assert.equal(editor.dirty(), false);
  editor.undo(); assert.equal(editor.read()[0].text, 'ソ');
  assert.equal(editor.dirty(), true);
});

test('invalid numeric edits keep the previous note and show an explanation', async () => {
  const { window, document, editor } = await fixture();
  editor.select(0);
  const pitch = document.getElementById('ppitch');
  for (const value of ['-1', '128', '60.5', 'NaN']) {
    pitch.value = value; pitch.dispatchEvent(new window.Event('change'));
    assert.equal(editor.read()[0].pitch, 60);
  }
  const start = document.getElementById('pstart');
  start.value = '2'; start.dispatchEvent(new window.Event('change'));
  assert.equal(editor.read()[0].start, 1);
  assert.match(document.getElementById('status').textContent, /確認してください/);
  assert.equal(editor.dirty(), false);
});

test('lyrics and line labels render as text, including imported markup', async () => {
  const { document, editor } = await fixture();
  await editor.load({ moras: [{ id: 0, line: 0, start: 1, end: 2, pitch: 60, text: '<img src=x>' }],
    line_texts: { 0: '<script>bad()</script>' }, has_audio: false });
  editor.select(0);
  assert.equal(document.querySelector('#pline img'), null);
  assert.equal(document.querySelector('#pline script'), null);
  assert.match(document.getElementById('pline').textContent, /<img src=x>/);
});

test('splitting and merging preserve the full lyric and remain undoable', async () => {
  const { document, editor } = await fixture();
  editor.select(0);
  document.getElementById('bsplit').onclick();
  assert.equal(editor.read().length, 3);
  assert.equal(editor.read().map(note => note.text).join(''), 'ソーラ');
  document.getElementById('bmerge').onclick();
  assert.equal(editor.read().length, 2);
  assert.equal(editor.read().map(note => note.text).join(''), 'ソラ');
  editor.undo(); assert.equal(editor.read().length, 3);
});

test('standalone file handlers import, edit and save locally without fetching source paths', async t => {
  const { window, document } = parseHTML(html);
  const previous = { document: globalThis.document, window: globalThis.window, location: globalThis.location };
  Object.assign(globalThis, { document, window, location: { href: 'http://localhost/score-editor' } });
  t.after(() => Object.assign(globalThis, previous));
  t.mock.method(globalThis, 'fetch', () => assert.fail('No upload, source path or synthesis request is allowed'));
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const downloaded = [];
  t.mock.method(URL, 'createObjectURL', blob => { downloaded.push(blob); return 'blob:test'; });
  t.mock.method(URL, 'revokeObjectURL', () => {});
  const format = document.getElementById('local-format');
  format.querySelector('option').selected = true;
  Object.defineProperty(format, 'selectedOptions', { get: () => [format.querySelector('option')] });
  let payload, saver, saved = false, status;
  await start({
    load: async value => { payload = structuredClone(value); }, setStatus: value => { status = value; },
    isDirty: () => false, markSaved: () => { saved = true; }, setSave: handler => { saver = handler; },
    read: () => payload, clearAudio: () => {},
  });
  assert.equal(document.getElementById('localbar').hidden, false);
  assert.equal(document.getElementById('bsave').disabled, true);
  const input = { version: 1, song: { ticks_per_beat: 480, audio_path: 'https://must-not-fetch.test/secret.wav' },
    notes: [{ id: 0, line: 0, kana: 'ア', midi_note: 60, start_sec: 0, end_sec: .5 }],
    lines: [{ id: 0, xf_surface: 'あ' }] };
  await document.getElementById('local-score-file').onchange({ target: {
    files: [new File([JSON.stringify(input)], 'song.json', { type: 'application/json' })],
  } });
  assert.equal(payload.moras[0].text, 'ア');
  assert.equal(payload.has_audio, false);
  assert.equal(document.getElementById('bsave').disabled, false);
  payload.moras[0].pitch = 61;
  await saver();
  assert.equal(saved, true);
  assert.match(status, /保存しました/);
  const exported = JSON.parse(await downloaded[0].text());
  assert.equal(exported.payload.moras[0].pitch, 61);
  assert.equal(exported.source.notes[0].midi_note, 60);
  t.mock.timers.tick(60000);
});

test('Video job entry reads only that job MIDI and keeps the API key out of URLs', async t => {
  const { window, document } = parseHTML(html);
  const previous = { document: globalThis.document, window: globalThis.window,
    location: globalThis.location, localStorage: globalThis.localStorage };
  Object.assign(globalThis, { document, window, location: { href: 'http://localhost/score-editor?job=score_1' },
    localStorage: { getItem: name => { assert.equal(name, 'apiKey'); return 'private-test-key'; } },
  });
  t.after(() => Object.assign(globalThis, previous));
  const format = document.getElementById('local-format');
  format.querySelector('option').selected = true;
  Object.defineProperty(format, 'selectedOptions', { get: () => [format.querySelector('option')] });
  const source = openDocument({ moras: [{ id: 0, line: 0, text: 'ア', start: 1, end: 2, pitch: 62 }] });
  const midi = lyricMidi(source, source.payload.moras);
  let payload, requests = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests++;
    assert.equal(url, '/api/jobs/score_1/exports/midi');
    assert.doesNotMatch(url, /private-test-key|api_key/);
    assert.deepEqual(options.headers, { 'X-API-Key': 'private-test-key' });
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    return new Response(midi, { headers: { 'Content-Type': 'audio/midi' } });
  });
  await start({ load: async value => { payload = value; }, setStatus: () => {},
    isDirty: () => false, setSave: () => {}, clearAudio: () => {},
  });
  assert.equal(requests, 1);
  assert.equal(payload.moras[0].pitch, 62);
  assert.equal(payload.moras[0].text, 'ア');
  assert.equal(payload.moras[0].start, 1);
});

test('SVS receives the original score and sends edited notes directly back to Soramimic', async t => {
  const { window, document } = parseHTML(html);
  const previous = { document: globalThis.document, window: globalThis.window, location: globalThis.location };
  Object.assign(globalThis, { document, window, location: { href: 'https://dev-video.soramimic.com/score-editor?lyrics-transfer=test' } });
  t.after(() => Object.assign(globalThis, previous));
  t.mock.method(globalThis, 'fetch', () => assert.fail('Handoff must not upload score or audio'));
  const format = document.getElementById('local-format');
  format.querySelector('option').selected = true;
  Object.defineProperty(format, 'selectedOptions', { get: () => [format.querySelector('option')] });
  const seed = {phrases:['ア'], results:[[{surface:'替え歌',locked:true}]], unitsList:[[{pronunciation:'ア'}]],
    scoreContext:{format:'soramimic-score-context/1', song:{ticks_per_beat:480}, lineTexts:{0:'あ'},
      moras:[{id:0,line:0,text:'ア',pitch:60,start:1,end:2}]}};
  let returned, payload;
  const peer = new URL('https://dev.soramimic.pages.dev/editor.html');
  window.SoramimicBridge = {
    peer: app => { assert.equal(app,'editor'); return peer; },
    receive: async (target, apply) => {assert.equal(target,peer); await apply(seed);},
    send: async (target, getData, options) => {assert.equal(target,peer); assert.equal(options.allowSetup,true); returned=getData();},
  };
  await start({load:async value=>{payload=value;},setStatus:()=>{},isDirty:()=>false,
    setSave:()=>{},clearAudio:()=>{},read:()=>payload});
  assert.equal(document.getElementById('local-soramimic').hidden,false);
  assert.equal(document.getElementById('local-soramimic').disabled,false);
  payload.moras[0].pitch=65;
  await document.getElementById('local-soramimic').onclick();
  assert.equal(returned.scoreContext.moras[0].pitch,65);
  assert.deepEqual(returned.results,seed.results);
});
