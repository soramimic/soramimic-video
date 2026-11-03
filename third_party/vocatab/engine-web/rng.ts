// 種から決まる正規乱数（F0 の拡散の雑音・音響モデルの励振の雑音・ボコーダの雑音）と、AI リテイクの雑音の差し替え。
// Python のエンジン（numpy の PCG64）とは別の乱数の列なので、同じ種でも Python 版と同じテイクにはならない。
// ブラウザ版の中では、同じ種なら毎回同じ音になる。

/** 種の並び（例: [句の種, 1]）を 128 ビットの状態にする（cyrb128 の考え方）。 */
function seedState(parts: number[]): Uint32Array {
  const s = parts.join(",");
  let h1 = 1779033703, h2 = 3144134277, h3 = 1013904242, h4 = 2773480762;
  for (let i = 0; i < s.length; i++) {
    const k = s.charCodeAt(i);
    h1 = h2 ^ Math.imul(h1 ^ k, 597399067);
    h2 = h3 ^ Math.imul(h2 ^ k, 2869860233);
    h3 = h4 ^ Math.imul(h3 ^ k, 951274213);
    h4 = h1 ^ Math.imul(h4 ^ k, 2716044179);
  }
  h1 = Math.imul(h3 ^ (h1 >>> 18), 597399067);
  h2 = Math.imul(h4 ^ (h2 >>> 22), 2869860233);
  h3 = Math.imul(h1 ^ (h3 >>> 17), 951274213);
  h4 = Math.imul(h2 ^ (h4 >>> 19), 2716044179);
  const st = new Uint32Array([(h1 ^ h2 ^ h3 ^ h4) >>> 0, (h2 ^ h1) >>> 0, (h3 ^ h1) >>> 0, (h4 ^ h1) >>> 0]);
  if (!(st[0] | st[1] | st[2] | st[3])) st[0] = 1; // 全部 0 だと止まる
  return st;
}

/** xoshiro128**（速くて、周期が十分に長い）。 */
class Xoshiro {
  private s: Uint32Array;
  constructor(parts: number[]) {
    this.s = seedState(parts);
  }
  next(): number {
    const s = this.s;
    const r = Math.imul(rotl(Math.imul(s[1], 5), 7), 9) >>> 0;
    const t = s[1] << 9;
    s[2] ^= s[0];
    s[3] ^= s[1];
    s[1] ^= s[2];
    s[0] ^= s[3];
    s[2] ^= t;
    s[3] = rotl(s[3], 11);
    return r;
  }
  /** (0, 1) の一様乱数（0 にならない。log に渡すため） */
  uniform(): number {
    return ((this.next() >>> 8) + 0.5) / 16777216;
  }
}
const rotl = (x: number, k: number): number => ((x << k) | (x >>> (32 - k))) >>> 0;

/** 標準正規乱数 n 個（Box–Muller）。種の並びが同じなら同じ列。 */
export function normalNoise(n: number, ...seed: number[]): Float32Array {
  const r = new Xoshiro(seed);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i += 2) {
    const u1 = r.uniform();
    const u2 = r.uniform();
    const m = Math.sqrt(-2 * Math.log(u1));
    out[i] = m * Math.cos(2 * Math.PI * u2);
    if (i + 1 < n) out[i + 1] = m * Math.sin(2 * Math.PI * u2);
  }
  return out;
}

/**
 * AI リテイク（engine/pipeline.py の _retake と同じ考え方）: 雑音の最後の軸（長さ frameNote.length）のうち、
 * 音符 k の所だけを、種 [retakes[k], salt] の雑音に差し替える（その場で書き換える）。種 0 は差し替えない。
 */
export function applyRetakes(noise: Float32Array, frameNote: Int32Array, retakes: Record<number, number>, salt: number): void {
  const L = frameNote.length;
  const rows = Math.floor(noise.length / L);
  for (const [k, s] of Object.entries(retakes)) {
    const note = Number(k);
    if (!s) continue;
    let any = false;
    for (let j = 0; j < L; j++) if (frameNote[j] === note) { any = true; break; }
    if (!any) continue;
    const alt = normalNoise(noise.length, Number(s), salt);
    for (let r = 0; r < rows; r++) {
      const o = r * L;
      for (let j = 0; j < L; j++) if (frameNote[j] === note) noise[o + j] = alt[o + j];
    }
  }
}
