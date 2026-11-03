// 波形 → ln-mel（engine/melspec.py の wav_to_mel と同じ）と、そのための実数 FFT。
// wav_to_mel: 事前の reflect pad (n_fft − hop)//2 ＋ librosa.stft（center=False・hann）＋ librosa の mel フィルタ（slaney）
// @ |STFT| ＋ ln(max(1e-5, ·))。Python はすべて float64 で計算し、最後に float32 にするので、ここも倍精度で計算する。

// ── 実数 FFT（numpy.fft.rfft / irfft と同じ定義。irfft は 1/n で割る） ──
// n/2 点の複素 FFT（基数 2）に偶数・奇数の標本を詰めて回し、前後で組み替える。
// 長さは 2 の冪だけ（mel の 2048・ボイスパラメータの 1024）。

export class RealFFT {
  readonly n: number;
  private readonly m: number;
  private readonly rev: Uint32Array;
  private readonly cosT: Float64Array; // cos(2πk/m)（k < m/2）
  private readonly sinT: Float64Array;
  private readonly wr: Float64Array; // cos(2πk/n)（k ≤ m）。組み替えの回転
  private readonly wi: Float64Array; // sin(2πk/n)
  private readonly zr: Float64Array;
  private readonly zi: Float64Array;

  constructor(n: number) {
    if (n < 4 || (n & (n - 1)) !== 0) throw new Error(`RealFFT: 長さは 4 以上の 2 の冪（${n}）`);
    this.n = n;
    const m = n >> 1;
    this.m = m;
    let bits = 0;
    while ((1 << bits) < m) bits++;
    this.rev = new Uint32Array(m);
    for (let i = 0; i < m; i++) {
      let r = 0;
      for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b);
      this.rev[i] = r;
    }
    this.cosT = new Float64Array(m >> 1);
    this.sinT = new Float64Array(m >> 1);
    for (let k = 0; k < m >> 1; k++) {
      this.cosT[k] = Math.cos((2 * Math.PI * k) / m);
      this.sinT[k] = Math.sin((2 * Math.PI * k) / m);
    }
    this.wr = new Float64Array(m + 1);
    this.wi = new Float64Array(m + 1);
    for (let k = 0; k <= m; k++) {
      this.wr[k] = Math.cos((2 * Math.PI * k) / n);
      this.wi[k] = Math.sin((2 * Math.PI * k) / n);
    }
    this.zr = new Float64Array(m);
    this.zi = new Float64Array(m);
  }

  /** m 点の複素 FFT（その場で書き換える）。sign = −1 で順方向、+1 で逆方向（割らない） */
  private cfft(re: Float64Array, im: Float64Array, sign: number): void {
    const m = this.m;
    const rev = this.rev;
    for (let i = 0; i < m; i++) {
      const j = rev[i];
      if (j > i) {
        let t = re[i]; re[i] = re[j]; re[j] = t;
        t = im[i]; im[i] = im[j]; im[j] = t;
      }
    }
    for (let size = 2; size <= m; size <<= 1) {
      const half = size >> 1;
      const step = m / size;
      for (let start = 0; start < m; start += size) {
        for (let k = 0; k < half; k++) {
          const wr = this.cosT[k * step];
          const wi = sign * this.sinT[k * step];
          const a = start + k;
          const b = a + half;
          const tr = re[b] * wr - im[b] * wi;
          const ti = re[b] * wi + im[b] * wr;
          re[b] = re[a] - tr;
          im[b] = im[a] - ti;
          re[a] += tr;
          im[a] += ti;
        }
      }
    }
  }

  /** rfft: x[0..n) → X[0..n/2]（outRe・outIm は長さ n/2+1） */
  forward(x: ArrayLike<number>, outRe: Float64Array, outIm: Float64Array): void {
    const m = this.m;
    const zr = this.zr;
    const zi = this.zi;
    for (let k = 0; k < m; k++) {
      zr[k] = x[2 * k];
      zi[k] = x[2 * k + 1];
    }
    this.cfft(zr, zi, -1);
    for (let k = 0; k <= m; k++) {
      const a = k % m;
      const b = (m - k) % m;
      const ar = zr[a], ai = zi[a], br = zr[b], bi = zi[b];
      // 偶数の標本の DFT（E）と奇数の標本の DFT（O）に分け、X = E + e^{−2πik/n}·O
      const er = 0.5 * (ar + br);
      const ei = 0.5 * (ai - bi);
      const or = 0.5 * (ai + bi);
      const oi = -0.5 * (ar - br);
      const wr = this.wr[k];
      const wi = -this.wi[k];
      outRe[k] = er + (wr * or - wi * oi);
      outIm[k] = ei + (wr * oi + wi * or);
    }
  }

  /** irfft: X[0..n/2] → x[0..n)。X[0]・X[n/2] の虚部は使わない（pocketfft の c2r と同じ） */
  inverse(re: ArrayLike<number>, im: ArrayLike<number>, out: Float64Array): void {
    const m = this.m;
    const zr = this.zr;
    const zi = this.zi;
    for (let k = 0; k < m; k++) {
      const ar = re[k];
      const ai = k === 0 ? 0 : im[k];
      const br = re[m - k];
      const bi = m - k === m ? 0 : im[m - k];
      // E = (X[k] + conj(X[m−k]))/2、O = (X[k] − conj(X[m−k]))/2 · e^{+2πik/n}、Z = E + i·O
      const er = 0.5 * (ar + br);
      const ei = 0.5 * (ai - bi);
      const dr = 0.5 * (ar - br);
      const di = 0.5 * (ai + bi);
      const wr = this.wr[k];
      const wi = this.wi[k];
      const or = dr * wr - di * wi;
      const oi = dr * wi + di * wr;
      zr[k] = er - oi;
      zi[k] = ei + or;
    }
    this.cfft(zr, zi, 1);
    const s = 1 / m;
    for (let k = 0; k < m; k++) {
      out[2 * k] = zr[k] * s;
      out[2 * k + 1] = zi[k] * s;
    }
  }
}

/** scipy.signal.get_window("hann", N)（fftbins=True の周期的な窓）。general_cosine と同じ式・同じ順で作る */
export function hannPeriodic(N: number): Float64Array {
  // general_cosine(N+1, [0.5, 0.5]) の頭の N 点: fac = linspace(−π, π, N+1)、w = 0.5 + 0.5·cos(fac)
  const step = (Math.PI - -Math.PI) / N;
  const w = new Float64Array(N);
  for (let i = 0; i < N; i++) w[i] = 0.5 + 0.5 * Math.cos(i * step + -Math.PI);
  return w;
}

// ── wav_to_mel ──
const SR = 44_100;
const N_FFT = 2048;
const HOP = 256;
const N_MELS = 128;
const FMIN = 40.0;
const FMAX = 16000.0;

// librosa の slaney の mel 尺度（hz_to_mel・mel_to_hz の htk=False）
const F_SP = 200.0 / 3;
const MIN_LOG_HZ = 1000.0;
const MIN_LOG_MEL = (MIN_LOG_HZ - 0.0) / F_SP;
const LOGSTEP = Math.log(6.4) / 27.0;
const hzToMel = (f: number): number => (f >= MIN_LOG_HZ ? MIN_LOG_MEL + Math.log(f / MIN_LOG_HZ) / LOGSTEP : (f - 0.0) / F_SP);
const melToHz = (m: number): number => (m >= MIN_LOG_MEL ? MIN_LOG_HZ * Math.exp(LOGSTEP * (m - MIN_LOG_MEL)) : 0.0 + F_SP * m);

/** 帯ごとの、値が 0 でない bin の範囲と重み（librosa の重みは float32 で作られ、melspec.py で float64 に戻す） */
interface MelBasis { lo: Int32Array; w: Float64Array[] }
let BASIS: MelBasis | null = null;

/** librosa.filters.mel(sr=44100, n_fft=2048, n_mels=128, fmin=40, fmax=16000)（htk=False・norm="slaney"・float32） */
function melBasis(): MelBasis {
  if (BASIS) return BASIS;
  const nb = N_FFT / 2 + 1;
  // fft_frequencies = np.fft.rfftfreq(n_fft, 1/sr) = arange · (1/(n·d))
  const val = 1.0 / (N_FFT * (1.0 / SR));
  const fft = new Float64Array(nb);
  for (let k = 0; k < nb; k++) fft[k] = k * val;
  // mel_frequencies(n_mels + 2): np.linspace(min_mel, max_mel, n) → mel_to_hz（最後は max_mel そのもの）
  const n = N_MELS + 2;
  const lo = hzToMel(FMIN);
  const hi = hzToMel(FMAX);
  const step = (hi - lo) / (n - 1);
  const melF = new Float64Array(n);
  for (let i = 0; i < n; i++) melF[i] = melToHz(i === n - 1 ? hi : i * step + lo);
  const loIdx = new Int32Array(N_MELS);
  const ws: Float64Array[] = [];
  const row = new Float64Array(nb);
  for (let i = 0; i < N_MELS; i++) {
    const fd0 = melF[i + 1] - melF[i];
    const fd1 = melF[i + 2] - melF[i + 1];
    const enorm = 2.0 / (melF[i + 2] - melF[i]);
    let a = -1;
    let b = -1;
    for (let k = 0; k < nb; k++) {
      const lower = -(melF[i] - fft[k]) / fd0;
      const upper = (melF[i + 2] - fft[k]) / fd1;
      // weights は float32 の配列: 代入で float32 に丸め、slaney の正規化（float64 の enorm を掛ける）でもう一度丸める
      const w0 = Math.fround(Math.max(0, Math.min(lower, upper)));
      row[k] = Math.fround(w0 * enorm);
      if (row[k] !== 0) {
        if (a < 0) a = k;
        b = k;
      }
    }
    loIdx[i] = Math.max(a, 0);
    ws.push(a < 0 ? new Float64Array(0) : row.slice(a, b + 1));
  }
  BASIS = { lo: loIdx, w: ws };
  return BASIS;
}

let FFT2048: RealFFT | null = null;
let WIN2048: Float64Array | null = null;

/** numpy の mode="reflect"（端の値を繰り返さない折り返し）の添字。pad が長さを超えても折り返し続ける */
function reflectIndex(i: number, n: number): number {
  if (n === 1) return 0;
  const p = 2 * (n - 1);
  let j = i % p;
  if (j < 0) j += p;
  return j < n ? j : p - j;
}

/** engine/melspec.py の wav_to_mel: mono 44.1 kHz → ln-mel [128, frames]（行優先。frames ≈ len / 256） */
export function wavToMel(wav: Float32Array): { mel: Float32Array; frames: number } {
  const pad = (N_FFT - HOP) / 2;
  const n = wav.length;
  const L = n + 2 * pad;
  const frames = n > 0 && L >= N_FFT ? 1 + Math.floor((L - N_FFT) / HOP) : 0;
  const mel = new Float32Array(N_MELS * frames);
  if (!frames) return { mel, frames };
  const y = new Float64Array(L);
  for (let i = 0; i < L; i++) y[i] = wav[reflectIndex(i - pad, n)];
  FFT2048 ??= new RealFFT(N_FFT);
  WIN2048 ??= hannPeriodic(N_FFT);
  const fft = FFT2048;
  const win = WIN2048;
  const basis = melBasis();
  const nb = N_FFT / 2 + 1;
  const fr = new Float64Array(N_FFT);
  const re = new Float64Array(nb);
  const im = new Float64Array(nb);
  const mag = new Float64Array(nb);
  for (let t = 0; t < frames; t++) {
    const off = t * HOP;
    for (let j = 0; j < N_FFT; j++) fr[j] = win[j] * y[off + j];
    fft.forward(fr, re, im);
    for (let k = 0; k < nb; k++) mag[k] = Math.hypot(re[k], im[k]);
    for (let b = 0; b < N_MELS; b++) {
      const w = basis.w[b];
      const lo = basis.lo[b];
      let s = 0;
      for (let k = 0; k < w.length; k++) s += w[k] * mag[lo + k];
      mel[b * frames + t] = Math.log(Math.max(1e-5, s));
    }
  }
  return { mel, frames };
}
