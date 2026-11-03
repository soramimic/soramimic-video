// 音素の判定（PrettyPitch の duration_model/phoneset.py の PhoneSet と同じ規則）。
import type { PhoneSetData } from "./types";

export class PhoneSet {
  readonly lang: string;
  readonly phonemes: string[];
  readonly pau: string;
  readonly nuclei: Set<string>;
  readonly silence: Set<string>;
  readonly consSet: Set<string>;
  readonly consonants: string[];
  readonly ph2id: Map<string, number>;
  readonly score: PhoneSetData["score"];
  readonly mora: Record<string, string[]>;
  readonly finalConsonants: boolean;
  readonly g2p: string | null;

  constructor(readonly d: PhoneSetData) {
    this.lang = d.lang;
    this.phonemes = d.phonemes;
    this.pau = d.phonemes[0];
    this.nuclei = new Set(d.nuclei);
    this.silence = new Set(d.silence);
    this.consonants = d.consonants;
    this.consSet = new Set(d.consonants);
    this.ph2id = new Map(d.phonemes.map((p, i) => [p, i]));
    this.score = d.score;
    this.mora = d.mora;
    this.finalConsonants = !!d.final_consonants;
    this.g2p = d.g2p ?? null;
  }

  /** 表記ゆれの吸収（大小文字・ストレス数字）。 */
  fold(ph: string): string {
    let s = String(ph);
    if (this.d.strip_stress_digits) s = s.replace(/[0-9]/g, "");
    if (this.d.case_fold === "lower") s = s.toLowerCase();
    else if (this.d.case_fold === "upper") s = s.toUpperCase();
    return s;
  }

  /** 無音 → pau、無声化母音など → 有声母音。 */
  normalize(ph: string): string {
    if (this.silence.has(ph)) return this.pau;
    const f = this.fold(ph);
    if (this.silence.has(f)) return this.pau;
    return this.d.vowel_normalize[ph] ?? this.d.vowel_normalize[f] ?? f;
  }

  toId(ph: string): number {
    const id = this.ph2id.get(this.normalize(ph));
    if (id === undefined) throw new Error(`音素表に無い音素: ${ph}`);
    return id;
  }

  isPau(ph: string): boolean {
    return this.normalize(ph) === this.pau;
  }

  isNucleus(ph: string): boolean {
    if (this.isPau(ph)) return false;
    return this.nuclei.has(this.normalize(ph));
  }

  isConsonant(ph: string): boolean {
    return this.consSet.has(ph);
  }
}
