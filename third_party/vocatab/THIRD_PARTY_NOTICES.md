# Third-party notices

The source code of this repository is released under the MIT license (see [`LICENSE`](LICENSE)).
The items below are **not** covered by it, or are not ours. If you redistribute this
repository, carry these notices with it.

| What | Where | Terms |
|---|---|---|
| Trained models and voicebank (acoustic, F0, consonant duration, breath bank) | `voicebanks/leapsinger_ja_3singer/` | personal, non-commercial, **no redistribution** — each singing database's terms (see §1) |
| NHVSing vocoder | `vocoders/nhvsing_v3_2_1/vocoder.onnx` | NHVSing project; weights are non-commercial (see §2) |
| Growl effect | `engine/voiceparams.py` `growl()`, `ui/src/engine-web/voiceparams.ts` | follows hifisampler `util/growl.py`, Apache-2.0 (see §3) |
| onnxruntime-web 1.20.1 | loaded at run time from jsDelivr, **not** in this repo | MIT, Microsoft (see §4) |
| esbuild | used at build time only (`tools/build.py`), **not** in this repo | MIT |

Everything else in this repository is our own work and is MIT.

---

## 1. The models and the voicebank

`voicebanks/leapsinger_ja_3singer/` contains the acoustic model (LeapSinger, ONNX), the F0 and
consonant-duration models (PrettyPitch, ONNX), the speaker vectors, and the breath bank
(`breaths.json` / `breaths.bin`: log-mel spectrograms of breaths, resynthesised by the vocoder
at run time — no recording is shipped). All of them were trained on, or derived from, the
following singing-voice databases, and every one of them is bound by the databases' terms of
use. They are provided for **personal, non-commercial** use and **may not be redistributed**
on their own or inside another product.

- Oniku Kurumi singing database (御丹宮くるみ) — <https://onikuru.info/db-download/>
- Natsume Yuuri (夏目悠李; database production: アマノケイ / voice provider: 霧野蒼太) —
  <https://ksdcm1ng.wixsite.com/njksofficial/enunu-nnsvs>. The terms for the output audio are
  bundled as [`voicebanks/leapsinger_ja_3singer/夏目悠李_利用規約.txt`](https://github.com/wavtechyukky/vocatab/blob/35870f397427b85f56e6828690f94d2df0e31ea4/voicebanks/leapsinger_ja_3singer/%E5%A4%8F%E7%9B%AE%E6%82%A0%E6%9D%8E_%E5%88%A9%E7%94%A8%E8%A6%8F%E7%B4%84.txt)
  and must stay with anything made from this voice.
- Namine Ritsu (波音リツ), recorded by Canon (カノン) — <https://www.canon-voice.com/voicebanks/>.
  Both names, 波音リツ and カノン, must be credited.

The model code itself is ours and MIT:
[LeapSinger](https://github.com/wavtechyukky/LeapSinger) (acoustic model),
[PrettyPitch](https://github.com/wavtechyukky/PrettyPitch) (F0 and consonant duration).

## 2. NHVSing vocoder (`vocoders/nhvsing_v3_2_1/vocoder.onnx`)

A built artifact of the [NHVSing](https://github.com/wavtechyukky/NHVSing/) project (also ours,
MIT code). Its distributed weights are **non-commercial** and governed by the terms of the data
they were trained on. The vocoder follows

> **Neural Homomorphic Vocoder** — Zhijun Liu, Kuan Chen, Kai Yu, Interspeech 2020
> <https://www.isca-archive.org/interspeech_2020/liu20_interspeech.html>

## 3. Growl effect (Apache-2.0)

`growl()` in `engine/voiceparams.py`, and its line-by-line TypeScript port in
`ui/src/engine-web/voiceparams.ts`, reproduce the growl of

> **hifisampler** — `util/growl.py` — Apache License, Version 2.0

(a 80 Hz square-wave LFO that warps the band above 400 Hz in time by ±100·strength cents and
restores the band's RMS). The Apache-2.0 text is in [`LICENSES/Apache-2.0.txt`](Apache-2.0.txt).

## 4. onnxruntime-web

The in-browser engine loads `onnxruntime-web` 1.20.1 (MIT, © Microsoft Corporation) at run time
from `https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/`
(`ui/src/engine-web/worker.ts`). Nothing from it is checked into this repository.

---

## Not third-party: our own implementations

For the avoidance of doubt, the following are our own code, written from the published
descriptions; no code from the named works is present here.

| Our file | Design follows |
|---|---|
| `engine/voiceparams.py`, `ui/src/engine-web/voiceparams.ts` (tension, gender, resonance, breathiness) | the usual spectral-envelope and harmonic/noise-branch manipulations of singing synthesizers |
| `ui/src/fx.ts` (reverb) | a plain convolution reverb with a synthetic impulse response |
