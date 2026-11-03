// 韓国語の G2P: ハングルの音節の並び → Team CODA の音素（PrettyPitch の svs/g2p_ko.py と同じ規則・同じ結果）。
//
// 韓国語は前後の音節で発音が変わるので、1 音節ずつ表で引くことはできない（日本語のモーラ表のようにはいかない）。
// 休みを挟まずに続けて歌う音節の並びをまとめて受け取り、音節ごとの音素の列を返す。
// 扱う規則（標準発音法の主なもの。語の切れ目や品詞を見る規則は扱わない）:
//   - 終声の 7 代表音（K N T L M P NG）、二重パッチムの一方だけを読む
//   - 連音: 終声 + ㅇ で始まる音節 → 終声が次の音節の頭へ移る（음악 → 으막）。二重パッチムは後ろだけが移る（읽어 → 일거）
//   - 口蓋音化: ㄷ・ㅌ + 이 → 지・치（굳이 → 구지、같이 → 가치）
//   - ㅎ: 終声 ㅎ + ㅇ は落ちる（좋아 → 조아）、ㅎ + ㄱㄷㅈ は激音（좋다 → 조타）、ㅎ + ㅅ → ㅆ、ㅎ + ㄴ → ㄴㄴ。
//         終声 ㄱㄷㅂㅈ + ㅎ も激音（축하 → 추카）
//   - 鼻音化: K T P + ㄴ ㅁ → NG N M（국물 → 궁물）、ㅁ ㅇ + ㄹ → ㄴ（종로 → 종노）、K P + ㄹ → NG M + ㄴ（국립 → 궁닙）
//   - 流音化: ㄴ + ㄹ・ㄹ + ㄴ → ㄹㄹ（신라 → 실라、칼날 → 칼랄）
//   - 濃音化: K T P + ㄱㄷㅂㅅㅈ → 濃音（학교 → 학꾜）。ㄺ ㄼ ㄾ の後も濃音（맑게 → 말께）
//   - ㅈ ㅉ ㅊ の後の y は落とす（져 → 저）。의 は頭子音が無ければ eu i、あれば i（희망 → 히망）
// 表記の約束（学習のラベルと同じ）: ㄹㄹ は `L r`、母音の間の ㄹ は `r`、ㅐ・ㅔ は `e`、ㅚ・ㅙ・ㅞ は `w e`。

const BASE = 0xac00;
const N_JUNG = 21;
const N_JONG = 28;
const CHO = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"];
const JUNG = ["ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ", "ㅙ", "ㅚ", "ㅛ", "ㅜ", "ㅝ", "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ"];
const JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ",
  "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"];

// 頭子音として読むときの音素（ㅇ は頭子音なし）
const ONSET: Record<string, string | null> = {
  "ㄱ": "g", "ㄲ": "kk", "ㄴ": "n", "ㄷ": "d", "ㄸ": "tt", "ㄹ": "r", "ㅁ": "m", "ㅂ": "b", "ㅃ": "pp", "ㅅ": "s",
  "ㅆ": "ss", "ㅇ": null, "ㅈ": "j", "ㅉ": "jj", "ㅊ": "ch", "ㅋ": "k", "ㅌ": "t", "ㅍ": "p", "ㅎ": "h",
};
// 母音（わたり音 + 母音）
const VOWEL: Record<string, string[]> = {
  "ㅏ": ["a"], "ㅐ": ["e"], "ㅑ": ["y", "a"], "ㅒ": ["y", "e"], "ㅓ": ["eo"], "ㅔ": ["e"], "ㅕ": ["y", "eo"],
  "ㅖ": ["y", "e"], "ㅗ": ["o"], "ㅘ": ["w", "a"], "ㅙ": ["w", "e"], "ㅚ": ["w", "e"], "ㅛ": ["y", "o"],
  "ㅜ": ["u"], "ㅝ": ["w", "eo"], "ㅞ": ["w", "e"], "ㅟ": ["w", "i"], "ㅠ": ["y", "u"], "ㅡ": ["eu"],
  "ㅢ": ["eu", "i"], "ㅣ": ["i"],
};
// 終声の代表音
const CODA: Record<string, string> = {
  "ㄱ": "K", "ㄲ": "K", "ㄳ": "K", "ㄴ": "N", "ㄵ": "N", "ㄶ": "N", "ㄷ": "T", "ㄹ": "L", "ㄺ": "K", "ㄻ": "M",
  "ㄼ": "L", "ㄽ": "L", "ㄾ": "L", "ㄿ": "P", "ㅀ": "L", "ㅁ": "M", "ㅂ": "P", "ㅄ": "P", "ㅅ": "T", "ㅆ": "T",
  "ㅇ": "NG", "ㅈ": "T", "ㅊ": "T", "ㅋ": "K", "ㅌ": "T", "ㅍ": "P", "ㅎ": "T",
};
// 二重パッチム → [残る方の代表音, 次の音節へ移る方の字母]
const DOUBLE: Record<string, [string, string]> = {
  "ㄳ": ["K", "ㅅ"], "ㄵ": ["N", "ㅈ"], "ㄶ": ["N", "ㅎ"], "ㄺ": ["L", "ㄱ"], "ㄻ": ["L", "ㅁ"], "ㄼ": ["L", "ㅂ"],
  "ㄽ": ["L", "ㅅ"], "ㄾ": ["L", "ㅌ"], "ㄿ": ["L", "ㅍ"], "ㅀ": ["L", "ㅎ"], "ㅄ": ["P", "ㅅ"],
};
const FORTIS: Record<string, string> = { g: "kk", d: "tt", b: "pp", s: "ss", j: "jj" };
const ASPIRATE: Record<string, string> = { g: "k", d: "t", b: "p", j: "ch" };
const NASAL: Record<string, string> = { K: "NG", T: "N", P: "M" };
const PALATAL_Y_DROP = new Set(["j", "jj", "ch"]);
const ASPIRATING_CODAS = new Set(["ㄱ", "ㄲ", "ㄷ", "ㅂ", "ㅈ", "ㅅ", "ㅊ", "ㅌ", "ㅍ", "ㅋ", "ㄺ", "ㄼ", "ㄵ"]);

export function isHangulSyllable(ch: string): boolean {
  const c = ch.codePointAt(0)!;
  return Array.from(ch).length === 1 && c >= 0xac00 && c <= 0xd7a3;
}

function split(ch: string): [string, string, string] {
  const k = ch.codePointAt(0)! - BASE;
  return [CHO[Math.floor(k / (N_JUNG * N_JONG))], JUNG[Math.floor(k / N_JONG) % N_JUNG], JONG[k % N_JONG]];
}

/** 続けて歌う音節（ハングル 1 文字ずつ）の並び → 音節ごとの音素の列。 */
export function syllablesToPhonemes(syllables: string[]): string[][] {
  const sy = syllables.map(split);
  const n = sy.length;
  const onset: Array<string | null> = sy.map(([c]) => ONSET[c]);
  const coda: Array<string | null> = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    const jong = sy[i][2];
    const nxt = i + 1 < n ? sy[i + 1] : null;
    if (!jong) continue;
    if (nxt === null) { // 句末: 代表音
      coda[i] = CODA[jong];
      continue;
    }
    const [ncho, njung] = nxt;
    if (ncho === "ㅇ") { // ── 連音 ──
      if (jong === "ㅇ") {
        coda[i] = "NG";
      } else if (jong === "ㅎ") { // 좋아 → 조아
        // 落ちる
      } else if (jong in DOUBLE) {
        const [keep, move] = DOUBLE[jong];
        if (move === "ㅎ") { // 않아 → 아나、싫어 → 시러
          onset[i + 1] = keep === "N" ? "n" : "r";
        } else {
          coda[i] = keep;
          let mv = ONSET[move]!;
          if (move === "ㅌ" && njung === "ㅣ") mv = "ch"; // 핥이다 → 할치다
          if ((keep === "K" || keep === "P") && mv in FORTIS) mv = FORTIS[mv]; // 넋이 → 넉씨、값이 → 갑씨
          onset[i + 1] = mv;
        }
      } else {
        let mv = ONSET[jong];
        if (njung === "ㅣ" && (jong === "ㄷ" || jong === "ㅌ")) mv = jong === "ㄷ" ? "j" : "ch"; // 口蓋音化
        onset[i + 1] = mv;
      }
      continue;
    }
    const nb = onset[i + 1];
    if (ncho === "ㅎ" && ASPIRATING_CODAS.has(jong)) {
      // 激音化: 축하 → 추카、입학 → 이팍、맞히다 → 마치다。二重パッチムは前が残る（읽히다 → 일키다）
      const rep = jong in DOUBLE ? DOUBLE[jong][0] : CODA[jong];
      const lsrc = jong === "ㄺ" ? "k" : jong === "ㄼ" ? "p" : "t";
      let src = ({ K: "k", T: "t", P: "p", N: "ch", L: lsrc } as Record<string, string>)[rep];
      if (jong === "ㅈ" || jong === "ㅊ") src = "ch";
      if (jong in DOUBLE) coda[i] = DOUBLE[jong][0];
      onset[i + 1] = src;
      continue;
    }
    if (jong === "ㅎ" || jong === "ㄶ" || jong === "ㅀ") { // 終声の ㅎ + 子音
      const keep = jong === "ㅎ" ? null : jong === "ㄶ" ? "N" : "L";
      if (nb !== null && nb in ASPIRATE) { // 좋다 → 조타、많고 → 만코
        coda[i] = keep;
        onset[i + 1] = ASPIRATE[nb];
      } else if (nb === "s") { // 좋소 → 조쏘
        coda[i] = keep;
        onset[i + 1] = "ss";
      } else if (nb === "n") { // 놓는 → 논는、않는 → 안는、뚫는 → 뚤른
        if (keep === "L") {
          coda[i] = "L";
          onset[i + 1] = "r";
        } else {
          coda[i] = "N";
        }
      } else {
        coda[i] = keep ? keep : "T";
      }
      continue;
    }
    // ── 代表音にしてから、次の頭子音と同化 ──
    let c = CODA[jong];
    if (jong === "ㄺ" && nb === "g") c = "L"; // 맑게 → 말께
    if (c in NASAL && (nb === "n" || nb === "m")) { // 鼻音化: 국물 → 궁물
      c = NASAL[c];
    } else if (nb === "r") {
      if (c === "M" || c === "NG") { // 종로 → 종노
        onset[i + 1] = "n";
      } else if (c === "K" || c === "P") { // 국립 → 궁닙
        c = NASAL[c];
        onset[i + 1] = "n";
      } else if (c === "T") {
        c = "N";
        onset[i + 1] = "n";
      } else if (c === "N") { // 신라 → 실라
        c = "L";
      }
      // c === "L": 흘러 → L r（そのまま）
    } else if (c === "L" && nb === "n") { // 칼날 → 칼랄
      onset[i + 1] = "r";
    } else if ((c === "K" || c === "T" || c === "P") && nb !== null && nb in FORTIS) { // 濃音化: 학교 → 학꾜
      onset[i + 1] = FORTIS[nb];
    } else if (c === "L" && (jong === "ㄺ" || jong === "ㄼ" || jong === "ㄾ") && nb !== null && nb in FORTIS) {
      onset[i + 1] = FORTIS[nb]; // 넓게 → 널께、핥다 → 할따
    }
    coda[i] = c;
  }
  const out: string[][] = [];
  for (let i = 0; i < n; i++) {
    const jung = sy[i][1];
    let v = VOWEL[jung].slice();
    if (jung === "ㅢ" && onset[i] !== null) v = ["i"]; // 희망 → 히망
    if (v[0] === "y" && onset[i] !== null && PALATAL_Y_DROP.has(onset[i]!)) v = v.slice(1); // 져 → 저
    out.push([...(onset[i] ? [onset[i]!] : []), ...v, ...(coda[i] ? [coda[i]!] : [])]);
  }
  return out;
}

/** 歌詞の文字列からハングルの音節だけを取り出す（空白・記号は捨てる）。 */
export function lyricSyllables(lyric: string): string[] {
  return Array.from(lyric).filter(isHangulSyllable);
}
