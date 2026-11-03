// File exchange for the client-side piano roll. This module performs no I/O.
const FORMAT = 'soramimic-score-editor/1';
const clone = value => JSON.parse(JSON.stringify(value));
const fail = message => { throw new Error(message); };

export function validateMoras(input) {
  if (!Array.isArray(input) || input.length > 100000) fail('音符データが不正です。');
  return input.map((note, index) => {
    if (!note || !Number.isFinite(note.start) || !Number.isFinite(note.end)
        || note.start < 0 || note.end <= note.start
        || !Number.isInteger(note.pitch) || note.pitch < 0 || note.pitch > 127
        || !Number.isInteger(note.line) || note.line < 0
        || typeof note.text !== 'string' || !note.text.trim() || note.text.length > 256
        || /[\r\n\u0000]/u.test(note.text)) {
      fail(`${index + 1}音目の時刻・音程・歌詞を確認してください。`);
    }
    return { ...note };
  }).sort((a, b) => a.start - b.start);
}

export function tickToSec(song, tick) {
  let seconds = 0, previous = 0, tempo = 500000;
  for (const [at, value] of song.tempo_map || []) {
    if (at >= tick) break;
    seconds += (at - previous) * tempo / song.ticks_per_beat / 1e6;
    previous = at; tempo = value;
  }
  return seconds + (tick - previous) * tempo / song.ticks_per_beat / 1e6;
}

export function secToTick(song, seconds) {
  let elapsed = 0, previous = 0, tempo = 500000;
  for (const [at, value] of song.tempo_map || []) {
    const span = (at - previous) * tempo / song.ticks_per_beat / 1e6;
    if (elapsed + span >= seconds) break;
    elapsed += span; previous = at; tempo = value;
  }
  return Math.round(previous + (seconds - elapsed) * 1e6 / tempo * song.ticks_per_beat);
}

function validateSong(input = {}) {
  const song = clone(input);
  song.ticks_per_beat ??= 480;
  if (!Number.isInteger(song.ticks_per_beat) || song.ticks_per_beat < 1
      || song.ticks_per_beat > 32767) fail('MIDIの分解能が不正です。');
  song.tempo_map ??= [[0, 500000]];
  song.time_signatures ??= [[0, 4, 4]];
  let previous = -1;
  for (const row of song.tempo_map) {
    if (!Array.isArray(row) || row.length !== 2 || !Number.isInteger(row[0])
        || row[0] < previous || row[0] < 0 || !Number.isInteger(row[1])
        || row[1] < 1 || row[1] > 0xffffff) fail('テンポデータが不正です。');
    previous = row[0];
  }
  for (const row of song.time_signatures) {
    if (!Array.isArray(row) || row.length !== 3 || !Number.isInteger(row[0]) || row[0] < 0
        || !Number.isInteger(row[1]) || row[1] < 1 || row[1] > 32
        || ![1, 2, 4, 8, 16, 32, 64].includes(row[2])) fail('拍子データが不正です。');
  }
  song.time_signatures.sort((a, b) => a[0] - b[0]);
  return song;
}

export function gridLines(song, until) {
  const measures = [], beats = [];
  const signatures = song.time_signatures.length ? song.time_signatures : [[0, 4, 4]];
  let tick = 0, index = 0;
  for (let measure = 1; measure <= 10000; measure++) {
    while (index + 1 < signatures.length && signatures[index + 1][0] <= tick) index++;
    const [, numerator, denominator] = signatures[index];
    const seconds = tickToSec(song, tick);
    if (seconds > until) break;
    measures.push([seconds, measure]);
    const step = song.ticks_per_beat * 4 / denominator;
    for (let beat = 1; beat < numerator; beat++) beats.push(tickToSec(song, tick + beat * step));
    tick = Math.min(tick + numerator * step, signatures[index + 1]?.[0] ?? Infinity);
  }
  return { measures, beats };
}

export function openDocument(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) fail('楽譜JSONを選んでください。');
  let source, song, payload;
  if (input.format === FORMAT) {
    source = clone(input.source ?? null);
    song = validateSong(input.song);
    payload = clone(input.payload);
  } else if (input.version === 1 && input.song && Array.isArray(input.notes) && Array.isArray(input.lines)) {
    source = clone(input);
    song = validateSong(input.song);
    payload = {
      moras: input.notes.map(note => ({ id: note.id, line: note.line, text: note.kana,
        start: note.start_sec, end: note.end_sec, pitch: note.midi_note })),
      line_texts: Object.fromEntries(input.lines.map(line => [line.id, line.original_text ?? line.xf_surface])),
    };
  } else if (input.format === 'soramimic-score' && input.schema_version === 1 && input.score) {
    source = clone(input);
    song = validateSong();
    const lines = input.score.canonical;
    const byID = new Map(lines.map((line, index) => [line.utterance_id, index]));
    payload = {
      moras: input.score.synthesis_plan.map((slot, id) => ({ id, line: byID.get(slot.utterance_id),
        text: slot.kana, start: slot.start_sec, end: slot.end_sec, pitch: slot.midi_pitch })),
      line_texts: Object.fromEntries(lines.map((line, index) => [index, line.text])),
    };
  } else if (Array.isArray(input.moras)) {
    source = null; song = validateSong(); payload = clone(input);
  } else {
    fail('project.json、Soramimic Score JSON、またはこの画面で保存したJSONを選んでください。');
  }
  payload.moras = validateMoras(payload.moras);
  payload.reference ??= payload.moras.map(note => [note.start, note.end, note.pitch]);
  payload.line_texts ??= {};
  payload.ignore_ranges ??= [];
  if (!Array.isArray(payload.reference) || payload.reference.length > 100000
      || payload.reference.some(row => !Array.isArray(row) || row.length !== 3
        || !Number.isFinite(row[0]) || !Number.isFinite(row[1]) || row[0] < 0 || row[1] <= row[0]
        || !Number.isInteger(row[2]) || row[2] < 0 || row[2] > 127)) fail('参照音符が不正です。');
  if (!payload.line_texts || typeof payload.line_texts !== 'object' || Array.isArray(payload.line_texts)
      || Object.values(payload.line_texts).some(text => typeof text !== 'string')) fail('歌詞行が不正です。');
  if (!Array.isArray(payload.ignore_ranges) || payload.ignore_ranges.some(row => !row
      || !Number.isFinite(row.start) || !Number.isFinite(row.end) || row.start < 0
      || row.end <= row.start)) fail('除外区間が不正です。');
  payload.grid = gridLines(song, payload.moras.reduce((end, note) => Math.max(end, note.end), 0) + 5);
  // Audio paths from a saved project are never fetched. The user opens the file explicitly.
  payload.has_audio = false;
  payload.rms = null;
  return { source, song, payload };
}

export function saveDocument(document, payload) {
  return { format: FORMAT, source: clone(document.source), song: clone(document.song),
    payload: { ...clone(payload), moras: validateMoras(payload.moras), has_audio: false, rms: null } };
}

export function projectDocument(document, input) {
  const source = document.source;
  if (source?.version !== 1 || !source.song || !Array.isArray(source.notes)) {
    fail('project.jsonから開いた楽譜だけを、この形式で保存できます。MIDIで保存してください。');
  }
  const moras = validateMoras(input);
  const old = new Map(source.notes.map(note => [note.id, note]));
  const stable = moras.length === source.notes.length && moras.every((note, i) =>
    note.id === source.notes[i].id && note.line === source.notes[i].line && note.text === source.notes[i].kana);
  if (source.lyric_layers && !stable) {
    fail('歌詞レイヤーのある楽譜で分割・結合・歌詞を変更した場合は、MIDIで保存して取り込んでください。');
  }
  const result = clone(source), used = new Set(), ids = new Map();
  result.notes = moras.map((note, index) => {
    const base = used.has(note.id) ? {} : (old.get(note.id) ?? {});
    if (!used.has(note.id)) ids.set(note.id, index);
    used.add(note.id);
    return { ...clone(base), id: index, line: note.line, kana: note.text, midi_note: note.pitch,
      start_sec: note.start, end_sec: note.end, start_tick: secToTick(document.song, note.start),
      end_tick: Math.max(secToTick(document.song, note.start) + 1, secToTick(document.song, note.end)),
      surface: base.surface ?? '', raw: base.raw ?? note.text };
  });
  result.lines = source.lines.map(line => ({ ...clone(line),
    note_ids: result.notes.filter(note => note.line === line.id).map(note => note.id),
    xf_kana: result.notes.filter(note => note.line === line.id).map(note => note.kana).join('') }));
  if (!stable) result.parody = null;
  else if (result.parody) {
    for (const line of result.parody.lines) for (const word of line.words) {
      word.note_ids = word.note_ids.map(id => ids.get(id));
    }
  }
  if (result.lyric_layers?.synthesis_plan) {
    if (result.lyric_layers.synthesis_plan.length !== moras.length) fail('合成ノートの対応が不正です。');
    result.lyric_layers.synthesis_plan.forEach((slot, index) => {
      slot.start_sec = moras[index].start; slot.end_sec = moras[index].end; slot.midi_pitch = moras[index].pitch;
    });
  }
  return result;
}

const be16 = n => [n >>> 8 & 255, n & 255];
const be32 = n => [n >>> 24 & 255, n >>> 16 & 255, n >>> 8 & 255, n & 255];
function variable(n) {
  if (!Number.isSafeInteger(n) || n < 0 || n > 0xfffffff) fail('MIDIの時刻が長すぎます。');
  const bytes = [n & 127];
  while ((n = Math.floor(n / 128))) bytes.unshift((n & 127) | 128);
  return bytes;
}
const textBytes = text => [...new TextEncoder().encode(text)];
const meta = (type, bytes) => [255, type, ...variable(bytes.length), ...bytes];
function track(events) {
  events.sort((a, b) => a.tick - b.tick || a.order - b.order);
  const bytes = []; let tick = 0;
  for (const event of events) {
    bytes.push(...variable(event.tick - tick), ...event.bytes); tick = event.tick;
  }
  bytes.push(0, 255, 47, 0);
  return [...textBytes('MTrk'), ...be32(bytes.length), ...bytes];
}

export function lyricMidi(document, input) {
  const moras = validateMoras(input);
  if (!moras.length) fail('保存する音符がありません。');
  const song = validateSong(document.song);
  const events = [];
  for (const [tick, tempo] of song.tempo_map) {
    events.push({ tick, order: 0, bytes: meta(81, be32(tempo).slice(1)) });
  }
  for (const [tick, numerator, denominator] of song.time_signatures) {
    events.push({ tick, order: 0, bytes: meta(88, [numerator, Math.log2(denominator), 24, 8]) });
  }
  let previousLine = null, previousEnd = -1;
  for (const note of moras) {
    const start = secToTick(song, note.start);
    const end = Math.max(start + 1, secToTick(song, note.end));
    if (start < previousEnd) fail('音符が重なっています。開始と終了を調整してからMIDIを保存してください。');
    const lyric = (previousLine !== null && previousLine !== note.line ? '\n' : '') + note.text;
    events.push({ tick: start, order: 2, bytes: meta(5, textBytes(lyric)) },
      { tick: start, order: 3, bytes: [144, note.pitch, 96] },
      { tick: end, order: 1, bytes: [128, note.pitch, 0] });
    previousEnd = end; previousLine = note.line;
  }
  return new Uint8Array([...textBytes('MThd'), ...be32(6), ...be16(0), ...be16(1),
    ...be16(song.ticks_per_beat), ...track(events)]);
}

export function lyricText(input) {
  const rows = new Map();
  for (const note of validateMoras(input)) rows.set(note.line, (rows.get(note.line) ?? '') + note.text);
  return [...rows.values()].join('\n') + '\n';
}

// Decode standard lyrics MIDI. XF uses a separate text/event contract and is
// rejected explicitly, so a second melody or accompaniment is never guessed.
export function openMidi(buffer) {
  const bytes = new Uint8Array(buffer), view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 0;
  const need = count => { if (offset + count > bytes.length) fail('MIDIファイルが途中で切れています。'); };
  const byte = () => { need(1); return bytes[offset++]; };
  const u16 = () => { need(2); const n = view.getUint16(offset); offset += 2; return n; };
  const u32 = () => { need(4); const n = view.getUint32(offset); offset += 4; return n; };
  const tag = () => String.fromCharCode(byte(), byte(), byte(), byte());
  const vlq = () => {
    let value = 0;
    for (let i = 0; i < 4; i++) { const b = byte(); value = value * 128 + (b & 127); if (b < 128) return value; }
    fail('MIDIの可変長数値が不正です。');
  };
  if (tag() !== 'MThd') fail('MIDIファイルを選んでください。');
  const length = u32();
  if (length < 6) fail('MIDIヘッダーが不正です。');
  const format = u16(), count = u16(), division = u16();
  if (format > 1 || division === 0 || division & 0x8000) fail('このMIDIの時間形式には対応していません。');
  need(length - 6); offset += length - 6;
  const tempos = [], signatures = [], notes = [], lyrics = [];
  for (let tr = 0; tr < count; tr++) {
    if (tag() !== 'MTrk') fail('MIDIトラックが不正です。');
    const size = u32(); need(size); const stop = offset + size;
    let tick = 0, running = 0;
    const active = new Map();
    while (offset < stop) {
      tick += vlq(); let status = byte();
      if (status < 128) { offset--; status = running; }
      if (status >= 128 && status < 240) running = status;
      else running = 0;
      if (status === 255) {
        const type = byte(), n = vlq(); need(n);
        const data = bytes.slice(offset, offset + n); offset += n;
        if (type === 81 && n === 3) tempos.push([tick, data[0] * 65536 + data[1] * 256 + data[2]]);
        if (type === 88 && n === 4) signatures.push([tick, data[0], 2 ** data[1]]);
        if (type === 5) lyrics.push({ tick, tr, data });
      } else if (status === 240 || status === 247) {
        const n = vlq(); need(n); offset += n;
      } else if (status >= 128 && status < 240) {
        const a = byte(), b = [192, 208].includes(status & 240) ? 0 : byte();
        if (a > 127 || b > 127) fail('MIDIイベントが不正です。');
        const key = `${status & 15}:${a}`;
        if ((status & 240) === 144 && b > 0) {
          if (active.has(key)) fail('同じ高さの音符が重なっています。単旋律MIDIを選んでください。');
          active.set(key, { start_tick: tick, pitch: a, tr, channel: status & 15 });
        } else if ((status & 240) === 128 || (status & 240) === 144) {
          const note = active.get(key);
          if (note) { notes.push({ ...note, end_tick: tick }); active.delete(key); }
        }
      } else fail('MIDIイベントが不正です。');
      if (offset > stop) fail('MIDIトラックが途中で切れています。');
    }
    if (active.size) fail('終了のない音符があります。MIDIを書き出し直してください。');
  }
  if (!lyrics.length) fail('標準の歌詞入りMIDIを選んでください。XFの場合はVideoで歌詞入りMIDIを書き出してください。');
  const lyricTicks = new Set(lyrics.map(lyric => lyric.tick));
  const lyricTracks = new Set(lyrics.map(lyric => lyric.tr));
  const channels = new Set(notes.filter(note => lyricTicks.has(note.start_tick))
    .map(note => `${note.tr}:${note.channel}`));
  // Prefer the track carrying the lyrics; a dedicated lyric track may instead
  // address the only note channel. Ambiguous ensembles need a melody export.
  const local = [...channels].filter(key => lyricTracks.has(+key.split(':')[0]));
  const candidates = local.length ? local : [...channels];
  if (candidates.length !== 1) fail('メロディを一つに絞った歌詞入りMIDIを選んでください。');
  const selected = notes.filter(note => `${note.tr}:${note.channel}` === candidates[0]).sort((a, b) => a.start_tick - b.start_tick);
  const song = validateSong({ ticks_per_beat: division,
    tempo_map: tempos.length ? tempos.sort((a, b) => a[0] - b[0]) : [[0, 500000]],
    time_signatures: signatures.length ? signatures : [[0, 4, 4]] });
  const chosenLyrics = lyrics.filter(lyric => local.length ? lyric.tr === +candidates[0].split(':')[0] : true);
  let encoding = 'utf-8';
  try { const decoder = new TextDecoder('utf-8', { fatal: true }); chosenLyrics.forEach(lyric => decoder.decode(lyric.data)); }
  catch { encoding = 'shift_jis'; }
  const decoder = new TextDecoder(encoding, { fatal: true });
  const byTick = new Map();
  for (const lyric of chosenLyrics) {
    const text = decoder.decode(lyric.data);
    if (byTick.has(lyric.tick)) fail('同じ時刻に複数の歌詞があります。単旋律MIDIを選んでください。');
    byTick.set(lyric.tick, text);
  }
  let line = 0, previousEnd = -1;
  const moras = selected.map((note, id) => {
    let text = byTick.get(note.start_tick) ?? 'ー';
    if (/^[\r\n/\\]/u.test(text) && id) line++;
    text = text.replace(/^[\r\n/\\]+/u, '') || 'ー';
    if (note.start_tick < previousEnd) fail('メロディの音符が重なっています。単旋律MIDIを選んでください。');
    previousEnd = note.end_tick;
    return { id, line, text, start: tickToSec(song, note.start_tick), end: tickToSec(song, note.end_tick), pitch: note.pitch };
  });
  return openDocument({ format: FORMAT, song, source: null, payload: { moras } });
}
