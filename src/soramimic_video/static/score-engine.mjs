import { validateMoras } from './score-editor-io.mjs';

const BASE = 'https://raw.githubusercontent.com/wavtechyukky/vocatab/35870f397427b85f56e6828690f94d2df0e31ea4/';
const VOICEBANK = 'leapsinger_ja_3singer', VOCODER = 'nhvsing_v3_2_1';
export const SAMPLE_RATE = 44100;
export const CREDIT = '歌声: 波音リツ / 音源収録: カノン\nPrettyPitch / LeapSinger / NHVSing / VocaTab: wavtechyukky';

export function synthesisNotes(input) {
  const notes = validateMoras(input);
  if (!notes.length) throw new Error('合成する音符がありません。');
  if (notes.at(-1).end > 900) throw new Error('試聴できる楽譜は15分までです。');
  return notes.map((note, id) => {
    if (note.end - note.start > 30) throw new Error('30秒を超える音符を分割してください。');
    if (id && notes[id - 1].end > note.start + .001) throw new Error('音符が重なっています。長さ・開始位置を修正してください。');
    // VocaTab uses 480 ticks/beat. At 60 BPM these ticks preserve seconds.
    const onset = Math.round(note.start * 480), end = Math.round(note.end * 480);
    if (end <= onset) throw new Error('短すぎる音符があります。長さを確認してください。');
    // In the Japanese voicebank upper-case katakana denotes devoicing. Normal
    // recognized katakana must become hiragana so vowels are actually voiced.
    const lyric = note.text.normalize('NFKC').replace(/[ァ-ヶ]/gu, c => String.fromCharCode(c.charCodeAt(0) - 0x60));
    return { id, onset, dur: end - onset, pitch: note.pitch, lyric, line: note.line };
  });
}

export function wavBlob(samples) {
  const buffer = new ArrayBuffer(44 + samples.length * 2), view = new DataView(buffer);
  const text = (at, value) => { for (let i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i)); };
  text(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, SAMPLE_RATE, true); view.setUint32(28, SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    if (!Number.isFinite(samples[i])) throw new Error('合成音声に不正な値が含まれています。');
    view.setInt16(44 + i * 2, Math.round(Math.max(-1, Math.min(1, samples[i])) * 32767), true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

export class BrowserSinger {
  constructor(onProgress = () => {}) { this.onProgress = onProgress; this.requests = new Map(); this.sequence = 0; }
  call(type, fields) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.close(new Error('合成がタイムアウトしました。もう一度お試しください。')), 300000);
      this.requests.set(id, { resolve, reject, timer });
      this.worker.postMessage({ type, id, ...fields });
    });
  }
  async ready() {
    if (!this.initializing) {
      this.worker = new Worker(new URL('./score-worker.js', import.meta.url));
      this.worker.onmessage = ({ data }) => {
        if (data.type === 'progress') return this.onProgress(`モデルを読み込んでいます… ${Math.round(data.loaded / 1024 / 1024)} MB`);
        const request = this.requests.get(data.id);
        if (!request) return;
        clearTimeout(request.timer); this.requests.delete(data.id);
        if (data.type === 'error') request.reject(new Error(data.error)); else request.resolve(data.result);
      };
      this.worker.onerror = () => this.close(new Error('合成エンジンを読み込めません。接続とブラウザをご確認ください。'));
      this.initializing = this.call('init', { base: BASE, gpu: false }).catch(error => { this.close(error); throw error; });
    }
    return this.initializing;
  }
  close(reason = new DOMException('合成を中止しました。', 'AbortError')) {
    this.worker?.terminate(); this.worker = null; this.initializing = null;
    for (const { reject, timer } of this.requests.values()) { clearTimeout(timer); reject(reason); }
    this.requests.clear();
  }
  async synthesize(input) {
    const notes = synthesisNotes(input);
    await this.ready();
    const groups = [];
    for (const note of notes) {
      const group = groups.at(-1);
      if (!group || group[0].line !== note.line || note.onset + note.dur - group[0].onset > 15 * 480) groups.push([note]);
      else group.push(note);
    }
    const end = notes.reduce((last, n) => Math.max(last, (n.onset + n.dur) / 480), 0);
    const samples = new Float32Array(Math.ceil((end + .6) * SAMPLE_RATE));
    for (let i = 0; i < groups.length; i++) {
      this.onProgress(`歌声を合成しています… ${i + 1}/${groups.length}`);
      const analyzed = await this.call('analyze', { req: { voicebank: VOICEBANK, singer: 2, bpm: 60, seed: 1234,
        notes: groups[i].map(({ line, ...note }) => note) } });
      for (const phrase of analyzed.phrases) {
        const f0 = Float32Array.from(phrase.plain, (value, j) => value + phrase.vib[j]);
        const result = await this.call('synthesize', { req: { voicebank: VOICEBANK, singer: 2, key: phrase.key,
          f0_log2: f0, vocoder: VOCODER, params: {}, mel_out: false, mel_plain: false, retake_mel: {} } });
        const offset = Math.round(phrase.start_sec * SAMPLE_RATE);
        for (let j = Math.max(0, -offset); j < result.wav.length && j + offset < samples.length; j++) samples[j + offset] += result.wav[j];
      }
    }
    return wavBlob(samples);
  }
}
