# 入力方法

soramimic-video は、利用できる楽譜・音源・歌詞に応じて次の入力方法を選べます。

| 入力 | command | 特徴 |
|---|---|---|
| XF MIDI + 任意の元歌詞 | `analyze` | XF に含まれる読みと音符 timing を利用する確定的な経路 |
| 歌唱音源 + メロディ MIDI + 歌詞 | `analyze-audio --melody-midi` | MIDI の音高・構造を優先し、音源に timing を合わせる |
| 歌唱音源 + 歌詞 | `analyze-audio` | timing と pitch の推定を含む |
| 歌唱音源のみ | `analyze-audio` | 音声認識した歌詞と推定結果を使うため、手直しを推奨 |
| メロディ MIDI + 歌詞 | `analyze-midi` | 楽譜の音高・timing を使い、歌詞を音符へ割り当てる |

## 入力情報の優先順位

- XF MIDI に読みと歌詞 timing がある場合は、その情報を優先します。
- メロディ MIDI がある場合は、MIDI の音高と音符構造を優先します。
- 歌詞の読みを明示できる場合は、自動読み推定より優先します。
- 歌唱音源だけの経路は推定を含むため、timing editor で結果を確認・修正できます。

## Output

どの入力方法も、後続の `convert`、`synthesize`、`mix`、`video` が利用できる
`project.json` を作ります。入力によって伴奏の作り方は異なります。

- MIDI 入力では、対応する SoundFont を使って伴奏を render できます。
- 歌唱音源入力では、分離した伴奏を利用できます。
- 伴奏を用意しない場合は、歌唱だけの output を作ることもできます。

## 制約

- XF MIDI 以外では読み、timing、pitch の一部に推定が含まれる場合があります。
- 複雑な tempo 変化、和音と旋律が分離されていない MIDI、音質の低い歌唱音源では、
  手動修正が必要になる場合があります。
- 入力素材を利用・変換できる権利は、利用者が確認してください。
