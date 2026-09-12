# Third-party notices

## Optional audio-analysis models

The application can use operator-provided local copies of SheetSage2 and its
MERT-v2-FullSong parent. Their checkpoint weights are licensed under Creative
Commons Attribution-NonCommercial 4.0 and are neither bundled nor downloaded by
this repository. Identify SheetSage2, MERT2, and their source when using them:
https://huggingface.co/m-a-p/SheetSage2 and
https://huggingface.co/m-a-p/MERT-v2-FullSong.

Optional gap recovery can use RMVPE (MIT) and torchfcpe/FCPE (MIT). Their model
code and checkpoints must be supplied or installed separately; this repository
does not redistribute RMVPE assets.

## PJS pitch corrector

The WAV pitch corrector parameters were learned from the PJS: Phoneme-balanced
Japanese Singing-voice corpus.

- Contributors: Junya Koguchi (Meiji University), Shinnosuke Takamichi (University of Tokyo)
- License: [Creative Commons Attribution-ShareAlike 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- Project: [PJS corpus](https://sites.google.com/site/shinnosuketakamichi/research-topics/pjs_corpus)

The distributed model parameters are licensed under CC BY-SA 4.0. The surrounding
inference code remains licensed under this repository's MIT license.

## HTS Voice "Mei"

The WAV analysis samples use speech synthesized with HTS Voice "Mei".

- Copyright: 2009–2013 Nagoya Institute of Technology, Department of Computer Science
- License: [Creative Commons Attribution 3.0](https://creativecommons.org/licenses/by/3.0/)
- Voice license: [LICENSE_mei_normal.htsvoice](https://github.com/r9y9/pyopenjtalk/blob/master/pyopenjtalk/htsvoice/LICENSE_mei_normal.htsvoice)

The recordings were generated for this project and mixed with accompaniments generated
from this repository's MIDI data.

## FluidR3 GM

The accompaniment in the WAV analysis samples was rendered with FluidR3 GM.

- Copyright © 2000–2002, 2008 Frank Wen
- Copyright © 2008 Toby Smithe
- License: MIT

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
THE USE OR OTHER DEALINGS IN THE SOFTWARE.
