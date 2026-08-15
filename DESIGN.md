# soramimic-video 設計

## 目的

soramimic-video は、XF MIDI または歌唱音源を解析し、替え歌の歌唱音源と画像・字幕付き動画を
生成します。各処理段階は同じ project directory を介して接続され、CLI と Web API から
同じ変換処理を利用します。

## Pipeline

```text
XF MIDI + 元歌詞 ── analyze ─┐
歌唱音源 + 任意の MIDI ─ analyze-audio ─┤
                                      ├─ convert ─ edit ─ synthesize ─ mix ─ video
メロディ MIDI + 歌詞 ─ analyze-midi ────┘
```

- `analyze`: XF の歌詞・読み・音符 timing を解析し、元歌詞と対応付けます。
- `analyze-audio`: 歌唱音源から読み、timing、pitch を推定します。メロディ MIDI を併用できます。
- `analyze-midi`: メロディ MIDI と歌詞から音符・モーラ対応を作ります。
- `convert`: soramimic の単語リストを使って替え歌候補を作ります。
- `export-edit` / `import-edit`: 人手編集用 JSON を書き出し・取り込みます。
- `synthesize`: MusicXML を経由して歌唱音源を作ります。
- `mix`: 歌唱と伴奏を合成します。
- `video`: 単語画像、字幕、音声、クレジットから MP4 を作ります。

## Project data

project directory の `project.json` が処理段階間の公開 exchange format です。主な情報は
次のとおりです。

- 入力曲の tempo、音符、歌唱モーラ、元歌詞との行対応
- 選択した単語リスト、変換 parameter、採用した替え歌語
- 生成済み音声・動画 asset への project 内相対 path
- 元曲、画像、歌唱 engine に必要な credit

具体的な field は実際に生成される JSON と CLI の export 結果を正本とします。外部 tool が
編集する場合は未知 field を保持してください。

## Input methods

入力ごとの対応状況は [docs/input-methods.md](docs/input-methods.md) を参照してください。
XF MIDI は読みと音符 timing を保持するため最も確定的です。歌唱音源だけの場合は推定を含み、
メロディ MIDI または読みを併用すると入力情報を優先できます。

## Word lists and images

単語リストは tidy CSV を基本形式とし、`surface` を必須列とします。`pronunciation`、
`original`、`image` と表示用の任意列を利用できます。

画像を動画へ使用する場合、出典・作者・ライセンス等の credit 情報を保持し、必要な表示を
生成物へ反映します。外部素材の再配布可否は、アプリで利用できるかどうかとは別に確認します。

## Layouts and output

layout JSON が画像、文字、字幕、credit の配置を定義します。動画は替え歌字幕と元歌詞字幕を
表示でき、前奏・間奏・後奏には layout の指定に従った画面を使います。

生成物は MP4 を基本とします。Web UI は対応端末で保存・共有を提供し、利用できない場合は
直接 download へ fallback します。

## Public API behavior

Web API は job 単位で処理し、状態、thumbnail、完成動画を返します。公開 instance では
入力 size、曲長、同時実行数、利用回数等の制限が適用される場合があります。制限に達した場合は
API response と UI で利用者に通知します。

外部から渡された file は形式と size を検証します。位置情報等の不要な metadata は、対応する
画像形式の再保存時に除去されます。

## Rights

同梱 sample の権利根拠は [docs/sample-rights.md](docs/sample-rights.md)、曲 preset の判断基準は
[docs/adr/00002-song-preset-rights.md](docs/adr/00002-song-preset-rights.md) を参照してください。
必要な attribution や license notice を無効化する場合は、利用者が別の適切な方法で表示する必要が
あります。
