import assert from 'node:assert/strict';
import test from 'node:test';
import { openDocument, openMidi, saveDocument, projectDocument, lyricMidi, lyricText,
  secToTick, tickToSec, validateMoras, gridLines, soramimicSeed } from '../src/soramimic_video/static/score-editor-io.mjs';
import { validateEditorData } from '../external/soramimic/frontend/src/editorData.js';

const project = () => ({ version: 1, custom: { retained: 'source' },
  song: { midi_path: 'do-not-fetch.mid', audio_path: 'do-not-fetch.wav', ticks_per_beat: 480,
    tempo_map: [[0, 500000], [960, 400000]], time_signatures: [[0, 4, 4]], custom: true },
  notes: [
    { id: 10, line: 0, kana: 'ソ', midi_note: 60, start_sec: .5, end_sec: 1,
      start_tick: 480, end_tick: 960, surface: '空', raw: '空[そ', source: 'sheetsage_note', pitch_confidence: .8, custom: 'note' },
    { id: 20, line: 0, kana: 'ラ', midi_note: 64, start_sec: 1, end_sec: 1.4,
      start_tick: 960, end_tick: 1440, surface: '', raw: 'ら]' },
    { id: 30, line: 1, kana: 'ミ', midi_note: 67, start_sec: 2, end_sec: 2.4,
      start_tick: 2160, end_tick: 2640, surface: '実', raw: '実[み]' },
  ],
  lines: [{ id: 0, xf_surface: '空', original_text: '青い空', xf_kana: 'ソラ', note_ids: [10, 20], canonical_mora_ids: ['m0', 'm1'] },
    { id: 1, xf_surface: '実', xf_kana: 'ミ', note_ids: [30] }],
  parody: { lines: [{ line_id: 0, words: [{ note_ids: [10, 20], note_kana: ['ソ', 'ラ'] }] }] },
});

test('project roundtrip preserves source, unknown fields, note provenance and canonical lyrics', () => {
  const source = project(), before = structuredClone(source);
  const document = openDocument(source);
  assert.deepEqual(source, before);
  assert.equal(document.payload.has_audio, false);
  assert.equal(document.payload.rms, null);
  assert.equal(document.payload.line_texts[0], '青い空');
  document.payload.moras[0].pitch = 61;
  document.payload.moras[0].start = .52;
  const exported = projectDocument(document, document.payload.moras);
  assert.deepEqual(exported.custom, source.custom);
  assert.equal(exported.notes[0].custom, 'note');
  assert.equal(exported.notes[0].source, 'manual_edit');
  assert.equal(exported.notes[0].pitch_confidence, null);
  assert.equal(exported.score_editor_edits[0].before.source, 'sheetsage_note');
  assert.equal(exported.score_editor_edits[0].before.pitch_confidence, .8);
  assert.equal(exported.score_editor_edits[0].after.midi_note, 61);
  assert.equal(exported.notes[0].start_tick, 499);
  assert.deepEqual(exported.lines[0].canonical_mora_ids, ['m0', 'm1']);
  assert.deepEqual(exported.parody.lines[0].words[0].note_ids, [0, 1]);
  assert.deepEqual(source, before);
  const reopened = openDocument(saveDocument(document, document.payload));
  assert.deepEqual(reopened.payload.moras, document.payload.moras);
  assert.deepEqual(reopened.source, source);
});

test('structural edits drop stale parody and retain metadata on the first surviving note', () => {
  const document = openDocument(project());
  const first = document.payload.moras[0];
  first.end = .75;
  document.payload.moras.splice(1, 0, { ...first, start: .75, end: 1, text: 'ー' });
  const result = projectDocument(document, document.payload.moras);
  assert.equal(result.parody, null);
  assert.equal(result.notes[0].custom, 'note');
  assert.equal(result.notes[1].surface, '');
  assert.deepEqual(result.lines[0].note_ids, [0, 1, 2]);
});

test('layered project updates synthesis timing/pitch while retaining original observations', () => {
  const source = project();
  source.lyric_layers = { canonical: [{ kana: 'ソラミ', mora_ids: ['a', 'b', 'c'] }],
    synthesis_plan: source.notes.map(note => ({ start_sec: note.start_sec, end_sec: note.end_sec,
      midi_pitch: note.midi_note, kana: note.kana, singing_unit_id: 'u' + note.id })),
    evidence: [{ id: 'source', confidence: .8 }] };
  const document = openDocument(source);
  document.payload.moras[0].pitch = 62;
  document.payload.moras[0].start = .51;
  const result = projectDocument(document, document.payload.moras);
  assert.equal(result.lyric_layers.synthesis_plan[0].midi_pitch, 62);
  assert.equal(result.lyric_layers.synthesis_plan[0].start_sec, .51);
  assert.deepEqual(result.lyric_layers.synthesis_plan[0].pitch_sources, ['manual_edit']);
  assert.equal(result.lyric_layers.synthesis_plan[0].pitch_confidence, null);
  assert.deepEqual(result.lyric_layers.evidence, source.lyric_layers.evidence);
  assert.deepEqual(result.lyric_layers.canonical, source.lyric_layers.canonical);
  document.payload.moras[0].text = 'ア';
  assert.throws(() => projectDocument(document, document.payload.moras), /MIDIで保存/);
  assert.doesNotThrow(() => lyricMidi(document, document.payload.moras));
});

test('tempo changes and shortened measures retain musical positions', () => {
  const song = project().song;
  for (const tick of [0, 480, 959, 960, 1440, 3100]) assert.equal(secToTick(song, tickToSec(song, tick)), tick);
  assert.equal(tickToSec(song, 1440), 1.4);
  const grid = gridLines({ ...song, time_signatures: [[0, 4, 4], [960, 3, 4]] }, 4);
  assert.deepEqual(grid.measures.slice(0, 3), [[0, 1], [1, 2], [2.2, 3]]);
});

test('lyrics MIDI retains pitch, tempo, leading silence, line boundaries and note durations', () => {
  const document = openDocument(project());
  const result = openMidi(lyricMidi(document, document.payload.moras));
  assert.deepEqual(result.payload.moras, document.payload.moras.map((note, id) => ({ ...note, id })));
  assert.deepEqual(result.song.tempo_map, document.song.tempo_map);
  assert.equal(lyricText(result.payload.moras), 'ソラ\nミ\n');
});

test('Score observation document is retained separately from editable synthesis notes', () => {
  const input = { format: 'soramimic-score', schema_version: 1, observations: { source: 'retained' },
    score: { canonical: [{ utterance_id: 'line-a', text: '空' }], synthesis_plan: [
      { utterance_id: 'line-a', kana: 'ソ', start_sec: .5, end_sec: 1, midi_pitch: 60 },
    ] } };
  const document = openDocument(input);
  assert.equal(document.payload.line_texts[0], '空');
  assert.deepEqual(document.source, input);
  document.payload.moras[0].start = .6;
  const saved = saveDocument(document, document.payload);
  assert.equal(saved.source.score.synthesis_plan[0].start_sec, .5);
  assert.equal(saved.payload.moras[0].start, .6);
});

test('invalid note edits and overlapping monophonic MIDI never silently clamp or omit lyrics', () => {
  const document = openDocument(project());
  for (const mutation of [{ start: -1 }, { start: NaN }, { end: Infinity }, { end: .5 },
    { pitch: 128 }, { pitch: 60.5 }, { text: '' }, { text: 'a\nb' }, { line: -1 }]) {
    assert.throws(() => validateMoras([{ ...document.payload.moras[0], ...mutation }]));
  }
  document.payload.moras[0].end = 1.1;
  assert.throws(() => lyricMidi(document, document.payload.moras), /重なって/);
});

test('truncated MIDI never reads past chunk bounds', () => {
  const document = openDocument(project());
  const midi = lyricMidi(document, document.payload.moras);
  for (let size = 0; size < midi.length; size++) assert.throws(() => openMidi(midi.slice(0, size)));
});

test('large songs export without spread argument limits', () => {
  const document = openDocument(project());
  const moras = Array.from({ length: 10000 }, (_, id) => ({ id, start: id / 2, end: (id + 1) / 2,
    pitch: 60, line: Math.floor(id / 20), text: 'ア' }));
  const result = openMidi(lyricMidi(document, moras));
  assert.equal(result.payload.moras.length, 10000);
  assert.equal(result.payload.moras.at(-1).text, 'ア');
});

test('Soramimic accepts a setup JSON with edited readings and original surface, without fabricated results', () => {
  const document = openDocument(project());
  document.payload.moras[0].text = 'ア';
  const seed = soramimicSeed(document, document.payload.moras);
  assert.equal(validateEditorData(seed), null);
  assert.deepEqual(seed.phrases, ['アラ', 'ミ']);
  assert.deepEqual(seed.tokensList.map(row => row[0].pronunciation), seed.phrases);
  assert.deepEqual(seed.originalLines, ['青い空', '実']);
  assert.equal(seed.lyrics, '青い空\n実');
  assert.equal(seed.setupFirst, true);
  assert.deepEqual(seed.results, []);
  assert.deepEqual(seed.unitsList, []);
  assert.equal(seed.song, undefined);
  assert.equal(seed.host, undefined);
  assert.doesNotMatch(JSON.stringify(seed), /do-not-fetch|sheetsage/);
  assert.throws(() => soramimicSeed(document, []), /歌詞がありません/);
});

test('SVS roundtrip carries only score fields and preserves parody when readings are unchanged', () => {
  const document = openDocument(project());
  const first = soramimicSeed(document, document.payload.moras);
  assert.equal(first.scoreContext.song.audio_path, undefined);
  assert.equal(first.scoreContext.moras[0].source, undefined);
  const previous = {...first, setupFirst:undefined, results:[[{surface:'採用済み',locked:true}],[]], unitsList:[[],[]]};
  document.soramimic = previous;
  const edited = structuredClone(document.payload.moras);
  edited[0].pitch++;
  const resumed = soramimicSeed(document, edited);
  assert.deepEqual(resumed.results, previous.results);
  assert.equal(resumed.scoreContext.moras[0].pitch, edited[0].pitch);
  edited[0].text = 'ア';
  const revised = soramimicSeed(document, edited);
  assert.equal(revised.setupFirst, true);
  assert.deepEqual(revised.results, []);
  assert.deepEqual(revised.scorePrevious.results, previous.results);
  const reopened = openDocument(saveDocument(document, document.payload));
  assert.deepEqual(reopened.soramimic.results, previous.results);
});
