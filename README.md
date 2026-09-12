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

# または歌唱音源を解析（任意でメロディ MIDI を併用）
uv run soramimic-video analyze-audio \
  --audio song.wav --lyrics lyrics.txt --melody-midi song.mid \
  --project work/song

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

## 画像URLの検査

```sh
uv run soramimic-video audit-image-links --report-dir work/image-link-audit --max-urls 2500
```

既定では、全単語リストのHTTP(S)画像URLを対象にします。同じURLはまとめて検査します。
`--max-urls` で1回の件数を制限すると、未検査のURL、最後の検査が古いURLの順に巡回します。
同じ保存先で繰り返し実行すれば、実行日が空いても保存済みの結果から再開できます。
上限を省略するか0にすると一度に全件を検査します。

`latest.json` には、検査済みの全URLについて名前、HTTPステータス、検査日時、連続失敗回数を
保存します。`coverage` で全URL数と未検査件数、`known_findings` で未解消の検出件数を確認できます。
当日検査しなかったURLの問題も保持し、次の検査で正常になれば解消します。
日時付きJSONにはその回に検査した結果だけを保存し、直近30回分を保持します。
`--scope external-fanwork` は外部配信の非営利ファン活動向け画像だけに絞る指定です。

404・410は `broken`、空の応答やHTMLは `invalid`、アクセス制限やタイムアウトは
`unavailable` として区別し、異常は一度再試行します。検査は画像URLの疎通確認で、
画像全体の破損検査やURLの自動修正は行いません。終了コードは正常0、検出あり1、
実行失敗2です。終了コード0でも未検査URLが残ることがあるため、定期実行時は
終了コードに加えて `coverage` と検査日時を確認してください。

## Web UI

```sh
uv sync --extra api
uv run soramimic-video serve
```

Web UI では、曲と単語リストの選択、替え歌編集、動画生成、進捗確認、完成動画の保存・共有が
できます。公開 instance では、混雑防止や不正利用防止のため、投稿数・入力サイズ・曲長などが
制限される場合があります。画面に表示された案内に従ってください。

メイン画面の単語リストのプルダウンにある「＋ 新しいリスト」から、名前を付けた単語リストを複数保存できます。
単語は手入力するかCSV・テキストファイルから読み込み、単語リストの選択欄で切り替えます。
「選択中のリストを編集」で内容・名前の変更や削除ができます。保存先は使用中のブラウザです。

音源を入力するサーバーは、音源解析用の依存もインストールして起動します。

```sh
uv sync --extra api --extra audio
uv run soramimic-video serve
```

画面上部の「曲をアップロード」から、XF MIDI（`.mid` / `.midi`）
またはWAV、MP3、M4A/AAC、FLAC、OGG/Opus、WebM音声を選びます。PCでは同じ欄へ
1ファイルをドラッグ＆ドロップできます。同梱曲は「サンプル曲で試す」で入力方法を切り替えて選べます。
「曲をアップロード」で戻れます。入力方法を切り替えると前の曲選択は解除されます。
形式は拡張子とファイル内容から自動で判定し、
圧縮音声は解析前にPCM WAVへ変換します。持ち込み音源には正式な元歌詞を
画面へ入力するか、UTF-8のTXT/Markdownで同時にアップロードします。正式歌詞は
正解文字列としてforced alignmentし、ASRで書き換えません。音源分離・タイミング推定・音高推定を
サーバーで行うため、初回はモデルの取得が発生し、通常のMIDI入力より時間と保存容量を
使います。float WAVには対応していません。

音源入力には次の設定が適用されます。

| 環境変数 | 既定値 | 内容 |
|---|---:|---|
| `SORAMIMIC_MAX_AUDIO_UPLOAD_BYTES` | 200MB | 音声1ファイルの最大容量 |
| `SORAMIMIC_MAX_SONG_SECONDS` | 420秒 | MIDI/音声共通の曲長上限 |
| `SORAMIMIC_JOB_TTL_HOURS` | 0（自動削除なし） | 完了後に入力・中間物・動画を自動削除するまでの時間 |
| `SORAMIMIC_SHEETSAGE_MODEL_DIR` | 未設定 | ローカルSheetSage2モデル（設定時に主ノートとして使用） |
| `SORAMIMIC_SHEETSAGE_BASE_DIR` | 未設定 | ローカルMERT-v2-FullSong親モデル |
| `SORAMIMIC_RMVPE_ROOT` | 未設定 | ローカルRMVPE実装root（SheetSage2空白補完） |
| `SORAMIMIC_RMVPE_CHECKPOINT` | 未設定 | ローカルRMVPE checkpoint |
| `SORAMIMIC_FCPE_CHECKPOINT` | package同梱値 | 任意のローカルFCPE checkpoint |
| `SORAMIMIC_LYRIC_PIPELINE` | `cplus` | `evidence` で複数候補認識と歌詞レイヤーを有効化 |

SheetSage2/MERT2のweightはCC BY-NC 4.0です。アプリはモデルを自動取得せず、設定した
ローカルディレクトリだけをofflineで読みます。完全構成ではSheetSage2ノートを保持し、
歌詞のある空白だけをRMVPE主・FCPE確認で補います。両者が一致しないモーラは
`spoken`として歌詞と字幕に残します。モデル未設定時は既存pYIN実経路となり、画面に明示されます。

`evidence` は任意追加の `wav-to-xf` パッケージを使用します。利用可能なローカル
チェックアウトを `uv pip install <checkout>` で導入し、`uv run --no-sync` で実行してください。
通常の `cplus` 経路と既存プロジェクトは追加パッケージなしで使用できます。
`evidence` とSheetSage2を併用すると、全SheetSageノート候補と各モーラのかなCTC中心を
Stage 3へ渡し、モーラ→ノート対応を決定します。CTC中心を含む後続ノートがある場合、その
モーラを直前ノートのスタックへ隠しません。`evidence` 経路はSheetSage2を必須とし、
未設定時に別方式へ黙ってフォールバックしません。従来方式は明示的な `cplus` 経路です。

```sh
uv run --no-sync soramimic-video analyze-audio --audio song.wav --project work/song --lyric-pipeline evidence
uv run soramimic-video apply-lyric-layers --project work/song --layers work/realization.json
uv run soramimic-video export-xf --project work/song --output work/song/selected.mid
```

未知歌詞では分離ボーカル・原音の認識候補をカナCTCと照合し、必要時にVADなしで再認識します。
`analyze_audio/recognition.json` に候補と未解決箇所を保存します。対応範囲は日本語の主旋律で、
曖昧な箇所や英語・会話・コーラスが完全に復元される保証はありません。正式歌詞の指定時は
認識による書き換えを行いません。生成JSON・MIDI・試聴音源は作業用ディレクトリへ保存してください。

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
