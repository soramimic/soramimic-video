# soramimic-video

XF MIDI（カラオケ歌詞入り MIDI）または歌唱音源と元歌詞を入力に、
[soramimic](https://github.com/soramimic/soramimic) の単語リストで替え歌歌詞を生成し、
歌唱音源と画像・字幕付き動画を作るツールです。

## セットアップ

```sh
git clone --recursive https://github.com/soramimic/soramimic-video.git
cd soramimic-video
uv sync
```

利用する機能に応じて次の外部ツールが必要です。

- [NEUTRINO](https://studio-neutrino.com/)（歌唱合成）
- FluidSynth と SoundFont（MIDI 伴奏のレンダリング）
- ffmpeg（音声・動画処理）
- libcairo と日本語フォント（SVG 単語画像の描画）

歌唱音源から解析する場合は audio extra、Web API を使う場合は api extra を追加してください。

## CLI

```sh
# XF MIDI と元歌詞を解析
uv run soramimic-video analyze \
  --midi song.mid --lyrics lyrics.txt --project work/song

# または歌唱音源を解析（--lyrics を省くと歌詞も自動認識）
uv run soramimic-video analyze-audio \
  --audio song.wav --project work/song

# 替え歌へ変換
uv run soramimic-video convert \
  --project work/song --wordlist stations

# 必要に応じて編集用 JSON を書き出し・再取込
uv run soramimic-video export-edit --project work/song
uv run soramimic-video import-edit --project work/song

# 歌唱合成、ミックス、動画化
NEUTRINO_ROOT=/path/to/NEUTRINO \
  uv run soramimic-video synthesize --project work/song --model MERROW
uv run soramimic-video mix \
  --project work/song --soundfont /path/to/soundfont.sf2
uv run soramimic-video video --project work/song --layout caption
```

モーラの位置・長さ・読みは timing editor で調整できます。

```sh
uv run soramimic-video edit-timing --project work/song
```

各 command と option の詳細は `uv run soramimic-video --help` および各 subcommand の
`--help` を参照してください。

## Web UI

```sh
uv sync --extra api
uv run soramimic-video serve
```

Web UI では、曲と単語リストの選択、替え歌編集、動画生成、進捗確認、完成動画の保存・共有が
できます。公開 instance では、混雑防止や不正利用防止のため、投稿数・入力サイズ・曲長などが
制限される場合があります。画面に表示された案内に従ってください。

音源を入力するサーバーは、音源解析用の依存もインストールして起動します。

```sh
uv sync --extra api --extra audio
uv run soramimic-video serve
```

画面上部の「曲をアップロード」から、XF MIDI（`.mid` / `.midi`）
またはWAV、MP3、M4A/AAC、FLAC、OGG/Opus音声を選びます。PCでは同じ欄へ
1ファイルをドラッグ＆ドロップでき、WebM音声も入力できます。同梱曲は「サンプル曲で試す」で入力方法を切り替えて選べます。
「曲をアップロード」で戻れます。入力方法を切り替えると前の曲選択は解除されます。
形式は拡張子とファイル内容から自動で判定し、
圧縮音声は解析前にPCM WAVへ変換します。持ち込み音源では正式な元歌詞を
画面へ入力するか、UTF-8のテキストファイルとして同時にアップロードできます。歌詞を指定しない場合は
自動認識し、指定した正式歌詞は正解文字列としてforced alignmentしてASRで書き換えません。
音源分離・タイミング推定・音高推定を
サーバーで行うため、初回はモデルの取得が発生し、通常のMIDI入力より時間と保存容量を
使います。float WAVには対応していません。

公開モードでは、アップロードされた音源・歌詞と解析中間物を処理終了時に削除し、
失敗時は1分間隔で再試行します。完成動画・サムネイル・出典情報だけを設定された
保存期間中保持し、期間を過ぎたものから同じ間隔で削除します。音源解析は
同一ホスト上のサービスで行い、入力内容をモデル学習には使用しません。

音源入力には次の設定が適用されます。

| 環境変数 | 既定値 | 内容 |
|---|---:|---|
| `SORAMIMIC_MAX_AUDIO_UPLOAD_BYTES` | 200MB | 音声1ファイルの最大容量 |
| `SORAMIMIC_MAX_SONG_SECONDS` | 420秒 | MIDI/音声共通の曲長上限 |
| `SORAMIMIC_JOB_TTL_HOURS` | 0（自動削除なし） | 完了後に動画・サムネイル・出典情報を自動削除するまでの時間（入力・中間物は処理終了時に削除） |
| `SORAMIMIC_REQUIRE_PUBLIC` | 0 | `1`なら`SORAMIMIC_PUBLIC=1`が無い状態での起動を拒否（公開サービスの設定漏れ防止） |
| `SORAMIMIC_SHEETSAGE_MODEL_DIR` | 未設定 | ローカルSheetSage2モデル（設定時に主ノートとして使用） |
| `SORAMIMIC_SHEETSAGE_BASE_DIR` | 未設定 | ローカルMERT-v2-FullSong親モデル |

SheetSage2/MERT2のweightはCC BY-NC 4.0です。アプリはモデルを自動取得せず、設定した
ローカルディレクトリだけをofflineで読みます。音源解析の推定音高候補は
SheetSage2だけから取得します。前後をSheetSage2ノートに挟まれたラップ・台詞調の内部空白は、
歌詞を無音化しないためCTCのモーラ時刻を保持し、近い側のノート音高を合成専用の
`spoken` 値として使います。この値は推定音高とは扱わず、解析結果に由来を記録します。

音源解析は `wav-to-xf` パッケージを使用します。利用可能なローカル
チェックアウトを `uv pip install <checkout>` で導入し、`uv run --no-sync` で実行してください。
全SheetSageノート候補と各モーラのかなCTC中心を
境界なし設定のStage 3へ渡し、モーラ→ノート対応を決定します。母音・子音境界は入力せず、
CTC中心を含む後続ノートがある場合、その
モーラを直前ノートのスタックやmelismaへ隠しません。SheetSage2を必須とし、
未設定時に別方式へ黙ってフォールバックしません。
未知歌詞では、隣接するWhisper行の境界を近傍のSheetSage2ノート間休符へ補正してから、
行外から始まるノートへ小さな所有コストを加えます。ノート内のCTC位置やXF正解データは
この境界補正に使用しません。

```sh
uv run --no-sync soramimic-video analyze-audio --audio song.wav --project work/song
uv run soramimic-video apply-lyric-layers --project work/song --layers work/realization.json
uv run soramimic-video export-xf --project work/song --output work/song/selected.mid
```

未知歌詞では原音mixをWhisper large-v3（既定）のVADなし・前セグメント文脈なしの単一パスで認識します。
対応区間にSheetSage2ノートがなく、かつ認識全文が限定的な視聴案内・字幕・クレジット文型に
一致するときだけ除外します。ノートがないだけでは除外せず、非旋律の声も未解決として診断に残します。
確定した表層から soramimic-yomi と UniDic N-best の読み候補を作ります。soramimic-yomiは
数字の通常読み／桁読み、英字の単語読み／文字名読み、短い英語句の連結発音を上限付きで返し、
この層では限定的な記号読みを加えます。音響選択へ渡すのは母音列が異なる候補に絞ります。
候補はKanaWhisperの原音mix／分離ボーカル結果で保守的に
再順位付けします。KanaWhisperの自由認識結果を
歌詞として採用することはありません。ReazonかなCTCは、選択済みの読みを変更せずモーラ時刻だけを
推定します。`analyze_audio/recognition.json` に通常Whisperの歌詞認識を、
`analyze_audio/reading.json` に読み候補・KanaWhisper根拠・選択結果を保存します。対応範囲は日本語の
主旋律で、英語・会話・コーラスが完全に復元される保証はありません。正式歌詞の指定時は
通常Whisperによる表層認識を行わず、指定文字列も書き換えません。生成JSON・MIDI・試聴音源は作業用ディレクトリへ保存してください。
Demucs、Whisper、SheetSage2は入力取得後に独立ジョブとして投入されます。共有推論サービスは
優先度付きワーカーとGPU容量ゲートで必要な実行を安全に直列化します。KanaWhisperは歌詞行区間の
確定後にだけ実行し、同じモデルを共有プロセス内で再利用します。

公開運用では `SORAMIMIC_JOB_TTL_HOURS` を設定し、音源解析モデルを事前に取得してから
受付を開始してください。リバースプロキシを使う場合は、同じ音声上限までmultipart requestを
通せるようbody sizeとtimeoutも設定します。`audio` extraがないサーバーでも共通の
アップロードボタンからXF MIDIを選べます。音声を選んだ場合だけ「準備中」と案内します。

同梱の音源解析サンプルを再生成する場合は、FluidR3 GM SoundFontとfluidsynthを
用意して次を実行します。必要な第三者ライセンスは `THIRD_PARTY_NOTICES.md` に記載しています。

```sh
uv sync --extra sample-audio
uv run python examples/gen_audio_samples.py
```

手元だけで使う素材は公開 manifest へ追加せず、gitignore 対象の local sample 設定または
`SORAMIMIC_SAMPLES_DIR` で指定できます。権利を確認できない素材を repository へ
commit しないでください。

## 自作単語リスト

Web UI の替え歌 editor では、1 行に `表記,読み` を書いた自作リストを利用できます。
読みを省略した場合は表記から推定します。

```csv
高輪ゲートウェイ,タカナワゲートウェイ
茅ヶ崎,チガサキ
海,ウミ,カイ
```

`external/soramimic-wordlists` と同じ tidy CSV も利用できます。必須列は `surface` です。
`pronunciation`、`id`、`original` と任意の表示用列を追加できます。文字コードは UTF-8
（BOM 付き可）または Shift_JIS に対応します。

画像を付ける場合は API の file upload を使います。受け付ける画像形式と容量・件数には
制限があります。外部 URL を画像として指定することはできません。

## Editor の同梱

submodule の editor を Web UI 内で使う場合は、次の手順で静的 asset を作成します。

```sh
scripts/build-editor.sh
uv run soramimic-video serve
```

## ブラウザ + Colab

1. [soramimic.com](https://soramimic.com) で MIDI を取り込み、編集結果を JSON で書き出す
2. [notebooks/colab_render.ipynb](notebooks/colab_render.ipynb) を Google Colab で開き、
   MIDI と JSON を upload して動画を生成する

必要な外部ソフトウェアの準備は notebook 内の説明を参照してください。

## 権利・クレジット

- 同梱サンプル曲の根拠と作成方法は [docs/sample-rights.md](docs/sample-rights.md) に記録しています。
- 著作権のある楽曲の MIDI・歌詞・音源・動画を repository へ commit しないでください。
- 単語リスト画像を使う場合は、自動生成される `credits.md` のライセンス表示に従ってください。
- Web UI では、対象の単語リストに二次利用規約の案内を表示します。制作した動画の内容に応じて、必要な規約を遵守してください。
- CLI で `image_usage=noncommercial_fanwork` の画像を使う場合は、利用条件を確認して `video --noncommercial-fanwork` で有効化してください。
- 画像に作者表示が必要な場合、既定では動画フレームへ出典を表示します。表示を無効にする場合は、
  別の適切な場所で必要な表示を行ってください。
- VOICEVOX を使う動画には、選択したキャラクターを含む必要なクレジットを表示してください。
- 元曲について権利者指定の表記がある場合は改変せず優先してください。Web UI の
  「元曲クレジット」、または CLI の `--song-title`、`--original-credit`、
  `--credit-notice` を利用できます。

## 開発

```sh
uv sync --group dev --extra api
uv run pytest -q
```

設計上の公開 interface は [DESIGN.md](DESIGN.md) を参照してください。
