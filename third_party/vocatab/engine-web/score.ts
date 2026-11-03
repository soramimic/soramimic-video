// 楽譜 → 句（engine/pipeline.py の PrettyPitchLeapSinger.phrases_from_notes・_lyric_phonemes と、
// PrettyPitch の svs/score_io.py の score_to_phrases・_apply_g2p・_resolve_phonemes・_tokenize_lyric・_as_phoneme_list と同じ）。
// 歌詞 → 音素・休符・メリスマ・句の切れ目を Python と同じ規則で決める（ここがずれると後の段がすべてずれる）。
import { lyricSyllables, syllablesToPhonemes } from "./g2p_ko";
import { PhoneSet } from "./phoneset";
import type { PNote, Phrase, ScoreNote, VoicebankWeb } from "./types";

/** 画面の 1 音符（刻みの位置・長さは 4 分音符 = tpq 刻み） */
export interface EditorNote {
  id: number;
  onset: number;
  dur: number;
  pitch: number;
  lyric?: string | null;
}

// ── Python の文字列の扱いに合わせる道具 ──

// Python の str.split()・str.strip() が空白とみなす文字（JS の \s とは少し違う: \x1c〜\x1f・\x85 を含み、﻿ を含まない）
const PY_WS = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const WS_RUN = new RegExp(`[${PY_WS}]+`);
const WS_EDGE = new RegExp(`^[${PY_WS}]+|[${PY_WS}]+$`, "g");

/** str.split()（引数なし）と同じ: 空白の並びで切り、空の切れ端は捨てる */
export const pySplit = (s: string): string[] => s.split(WS_RUN).filter((t) => t !== "");
/** str.strip() と同じ */
export const pyStrip = (s: string): string => s.replace(WS_EDGE, "");
/** str.isascii() と同じ（空文字も真） */
const isAscii = (s: string): boolean => /^[\x00-\x7f]*$/.test(s);

/** 文字列の repr（エラーの文面を Python と揃えるため。印字できる文字はそのまま） */
function pyRepr(s: string): string {
  const q = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = q;
  for (const ch of s) {
    const c = ch.codePointAt(0)!;
    if (ch === "\\") out += "\\\\";
    else if (ch === q) out += "\\" + q;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (c < 0x20 || c === 0x7f) out += "\\x" + c.toString(16).padStart(2, "0");
    else out += ch;
  }
  return out + q;
}

// ── ボイスバンクごとの表（毎回作り直さないよう取り置く） ──

interface ScoreTables {
  ps: PhoneSet;
  known: Set<string>; // 音素表の音素（_as_phoneme_list）
  lyricSet: Set<string>; // 歌詞に直接書ける音素（F0 と音響の両方が知っているもの）
  lyricGreedy: string[]; // 空白で区切っていない所を切るときの候補（長い順）
  mora: Map<string, string[]>;
  maxlen: number; // モーラ表の鍵の最長（コードポイント数）
  melisma: Set<string>;
  melismaVowel: string;
}

const tablesCache = new WeakMap<VoicebankWeb, ScoreTables>();

function tables(vb: VoicebankWeb): ScoreTables {
  let t = tablesCache.get(vb);
  if (t) return t;
  const ps = new PhoneSet(vb.phoneset);
  // PrettyPitchLeapSinger.__init__ の lyric_set・lyric_greedy と同じ
  const ph2id = new Set(vb.speakers.phonemes);
  const f0Vocab = new Set<string>(vb.f0.phonemes as string[]);
  const lyricList = ps.phonemes.filter((p) => ph2id.has(p) && f0Vocab.has(p));
  // 長さの同じ候補が同じ位置で両方当たることは無い（同じ文字列になる）ので、同じ長さの中の順は結果に効かない
  const lyricGreedy = lyricList.filter((p) => p.length <= 3 && p !== "EP").sort((a, b) => b.length - a.length);
  const mora = new Map(Object.entries(vb.phoneset.mora ?? {}));
  let maxlen = 0;
  for (const k of mora.keys()) maxlen = Math.max(maxlen, Array.from(k).length);
  if (mora.size === 0) maxlen = 1; // max(..., default=1)
  const sc = ps.score ?? {};
  t = {
    ps, known: new Set(ps.phonemes), lyricSet: new Set(lyricList), lyricGreedy, mora, maxlen,
    melisma: new Set(sc.melisma_lyrics ?? []),
    melismaVowel: sc.melisma_vowel || "a",
  };
  tablesCache.set(vb, t);
  return t;
}

// ── 歌詞 → 音素 ──

/**
 * PrettyPitchLeapSinger._lyric_phonemes: 英数字だけの歌詞を音素の列にする（仮名を含む歌詞は null。モーラ表に任せる）。
 * 空白で区切った記号が音素ならそのまま使い、区切っていない所（「kakiku」「shu」）は音素の名前の最長一致で切る。
 */
export function lyricPhonemes(vb: VoicebankWeb, lyric: string): string[] | null {
  if (!isAscii(lyric)) return null;
  const t = tables(vb);
  const out: string[] = [];
  for (const tok of pySplit(lyric)) {
    if (t.lyricSet.has(tok)) {
      out.push(tok);
      continue;
    }
    let i = 0;
    while (i < tok.length) {
      const m = t.lyricGreedy.find((p) => tok.startsWith(p, i));
      if (m === undefined) throw new Error(`歌詞「${lyric}」の「${tok.slice(i)}」を音素として読めません`);
      out.push(m);
      i += m.length;
    }
  }
  return out.length ? out : null;
}

/** score_io._last_nucleus: 最後の核音素 */
function lastNucleus(ps: PhoneSet, phs: string[]): string | null {
  for (let i = phs.length - 1; i >= 0; i--) if (ps.isNucleus(phs[i])) return phs[i];
  return null;
}

/** score_io._tokenize_lyric: モーラ表の最長一致で切り、途中の伸ばし棒は直前の核を繰り返す */
function tokenizeLyric(t: ScoreTables, lyric: string, prevVowel: string): string[] {
  const chars = Array.from(lyric); // Python の添字はコードポイント単位
  const n = chars.length;
  const phs: string[] = [];
  let i = 0;
  while (i < n) {
    let matched: [string, number] | null = null;
    for (let L = Math.min(t.maxlen, n - i); L > 0; L--) {
      const sub = chars.slice(i, i + L).join("");
      if (t.mora.has(sub)) {
        matched = [sub, L];
        break;
      }
    }
    if (matched === null && t.melisma.has(chars[i])) {
      phs.push(lastNucleus(t.ps, phs) || prevVowel); // 伸ばし棒 = 直前の核を伸ばす
      i += 1;
      continue;
    }
    if (matched === null) {
      throw new Error(`歌詞 ${pyRepr(lyric)} の ${pyRepr(chars[i])} 付近がモーラ表にありません`
        + "（phonemes を明示するか表に追加してください）");
    }
    phs.push(...t.mora.get(matched[0])!);
    i += matched[1];
  }
  return phs;
}

/** score_io._as_phoneme_list: 空白で割った全部が音素表にあれば、その列 */
function asPhonemeList(t: ScoreTables, lyric: string): string[] | null {
  const toks = pySplit(lyric);
  if (!toks.length) return null;
  return toks.every((x) => t.known.has(x)) ? toks : null;
}

/** score_io._resolve_phonemes */
function resolvePhonemes(t: ScoreTables, sn: ScoreNote, prevVowel: string): string[] {
  if (sn.phonemes !== null) return sn.phonemes.slice();
  const lyr = sn.lyric;
  if (lyr === null || t.melisma.has(lyr)) return [prevVowel]; // メリスマ = 前の音節の母音を引き延ばす
  const direct = asPhonemeList(t, lyr); // 歌詞欄に音素を直接書いた場合
  if (direct !== null) return direct;
  return tokenizeLyric(t, lyr, prevVowel);
}

/**
 * score_io._apply_g2p: 言語パックが g2p を持つ言語（韓国語）で、歌詞の音素を先に決めておく。
 * 休符を挟まずに続く音符の並び（休符・音素を直接書いた音符・ハングルの無い歌詞で区切る）ごとに G2P をかけ、
 * 音符ごとの音素を phonemes に入れる。1 音符に複数の音節があってもよい。
 * メリスマ（歌詞なし・伸ばし記号）の音符は、直前の音節の母音を伸ばし、その音節の終声はメリスマの最後の音符へ移す
 * （「밤 -」→ b a | a M。終声は音を伸ばし終えてから閉じる）。
 */
function applyG2p(t: ScoreTables, score: ScoreNote[]): ScoreNote[] {
  if (t.ps.g2p !== "ko") return score;
  const out = score.slice();
  const kind = (sn: ScoreNote): "break" | "mel" | "syl" => {
    if (sn.is_rest) return "break";
    if (sn.phonemes !== null) return "break";
    if (sn.lyric === null || t.melisma.has(sn.lyric) || sn.lyric === "") return "mel";
    if (asPhonemeList(t, sn.lyric) !== null || !lyricSyllables(sn.lyric).length) return "break";
    return "syl";
  };
  const runG2p = (idx: number[]) => {
    const syls: string[] = [];
    const owner: number[] = [];
    for (const i of idx) {
      if (kind(out[i]) === "syl") {
        const s = lyricSyllables(out[i].lyric!);
        syls.push(...s);
        for (let q = 0; q < s.length; q++) owner.push(i);
      }
    }
    if (!syls.length) return;
    const per = syllablesToPhonemes(syls);
    const phs = new Map<number, string[]>(); // Python の dict と同じく、入れた順（音符の順）に並ぶ
    for (const i of idx) if (kind(out[i]) === "syl") phs.set(i, []);
    owner.forEach((i, q) => phs.get(i)!.push(...per[q]));
    let lastSyl: number | null = null; // メリスマが伸ばす直前の音節の音符
    let k = 0;
    while (k < idx.length) {
      const i = idx[k];
      if (kind(out[i]) === "syl") {
        lastSyl = i;
        k += 1;
        continue;
      }
      let j = k; // メリスマが続く範囲 [k, j)
      while (j < idx.length && kind(out[idx[j]]) === "mel") j += 1;
      if (lastSyl !== null) {
        const src = phs.get(lastSyl)!;
        let nuc = -1;
        src.forEach((p, m) => { if (t.ps.isNucleus(p)) nuc = m; });
        if (nuc >= 0) {
          const vowel = src[nuc];
          const coda = src.slice(nuc + 1);
          phs.set(lastSyl, src.slice(0, nuc + 1));
          for (let m = k; m < j; m++) phs.set(idx[m], [vowel, ...(m === j - 1 ? coda : [])]);
        }
      }
      k = j;
    }
    for (const [i, p] of phs) out[i] = { ...out[i], phonemes: p };
  };
  let run: number[] = [];
  out.forEach((sn, i) => {
    if (kind(sn) === "break") {
      runG2p(run);
      run = [];
    } else {
      run.push(i);
    }
  });
  runG2p(run);
  return out;
}

export interface ScoreToPhrasesOptions {
  splitSilenceSec?: number;
  pauLeadSec?: number;
  pauTrailSec?: number;
}

/**
 * score_io.score_to_phrases: ScoreNote の列 → 句の列。
 * 休符が splitSilenceSec 以上なら句を切り、未満なら直前の音符の gap_after_sec に足す（句の頭の休符は捨てる）。
 */
export function scoreToPhrases(vb: VoicebankWeb, score: ScoreNote[], bpm: number,
                               opts: ScoreToPhrasesOptions = {}): Phrase[] {
  const t = tables(vb);
  const split = opts.splitSilenceSec ?? 0.5;
  const lead = opts.pauLeadSec ?? 0.25;
  const trail = opts.pauTrailSec ?? 0.25;
  score = applyG2p(t, score); // 韓国語など、規則の G2P を持つ言語だけ
  const b2s = 60.0 / bpm;
  const phrases: Phrase[] = [];
  let cur: PNote[] = [];
  let prevVowel = t.melismaVowel;
  let cursor = 0.0; // 楽譜の上の絶対時刻（休符も含めて積む）
  let phraseStart = 0.0;
  const flush = () => {
    if (cur.length) {
      cur[cur.length - 1].gap_after_sec = 0.0; // 句の終わりは後ろの pau が受け持つ
      phrases.push({ notes: cur, leading_pau_sec: lead, trailing_pau_sec: trail, start_sec: phraseStart });
      cur = [];
    }
  };
  for (const sn of score) {
    const durS = sn.dur_beats * b2s;
    if (sn.is_rest) {
      if (durS >= split) flush();
      else if (cur.length) cur[cur.length - 1].gap_after_sec += durS;
      cursor += durS;
      continue;
    }
    if (!cur.length) phraseStart = cursor;
    const phs = resolvePhonemes(t, sn, prevVowel);
    cur.push({ phonemes: phs, pitch_midi: sn.midi, dur_sec: durS, gap_after_sec: 0.0 });
    const nuc = lastNucleus(t.ps, phs);
    if (nuc !== null) prevVowel = nuc;
    cursor += durS;
  }
  flush();
  return phrases;
}

/**
 * PrettyPitchLeapSinger.phrases_from_notes: 画面の音符 → 句の列と、句ごとの音符の id。
 * 音符の隙間は休符にする（0.5 秒以上の休符で句を切る）。重なった音符は前の音符を後ろの頭で切る。
 */
export function phrasesFromNotes(vb: VoicebankWeb, notes: EditorNote[], bpm: number,
                                 tpq = 480): Array<{ phrase: Phrase; ids: number[] }> {
  const ns = notes.slice().sort((a, b) => Number(a.onset) - Number(b.onset)); // sorted と同じく安定
  const score: ScoreNote[] = [];
  const ids: number[] = [];
  let cursor = 0;
  for (let k = 0; k < ns.length; k++) {
    const n = ns[k];
    let onset = Math.trunc(Number(n.onset));
    let end = onset + Math.trunc(Number(n.dur));
    if (k + 1 < ns.length) end = Math.min(end, Math.trunc(Number(ns[k + 1].onset)));
    if (onset > cursor) score.push({ dur_beats: (onset - cursor) / tpq, midi: 0, lyric: null, is_rest: true, phonemes: null });
    onset = Math.max(onset, cursor);
    if (end <= onset) continue;
    const lyric = pyStrip(String(n.lyric || ""));
    const melisma = lyric === "" || lyric === "-" || lyric === "ー" || lyric === "+";
    score.push({
      dur_beats: (end - onset) / tpq, midi: Math.trunc(Number(n.pitch)),
      lyric: melisma ? null : lyric, is_rest: false,
      phonemes: melisma ? null : lyricPhonemes(vb, lyric),
    });
    ids.push(n.id);
    cursor = end;
  }
  const phrases = scoreToPhrases(vb, score, bpm);
  const out: Array<{ phrase: Phrase; ids: number[] }> = [];
  let i = 0;
  for (const ph of phrases) {
    out.push({ phrase: ph, ids: ids.slice(i, i + ph.notes.length) });
    i += ph.notes.length;
  }
  return out;
}
