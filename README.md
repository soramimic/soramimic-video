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

## Web UI

```sh
uv sync --extra api
uv run soramimic-video serve
```

Web UI では、曲と単語リストの選択、替え歌編集、動画生成、進捗確認、完成動画の保存・共有が
できます。公開 instance では、混雑防止や不正利用防止のため、投稿数・入力サイズ・曲長などが
制限される場合があります。画面に表示された案内に従ってください。

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
