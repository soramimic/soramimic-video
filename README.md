# soramimic-video

XF MIDI(カラオケ歌詞入りMIDI)または歌唱音源(wav/mp3)と元歌詞テキストを入力に、
[soramimic](https://github.com/soramimic/soramimic) の単語リストで替え歌歌詞を生成し、
NEUTRINO で歌わせて、画像+字幕つきの替え歌動画まで作るパイプライン。

設計の詳細は [DESIGN.md](DESIGN.md) を参照。

## セットアップ

```sh
git clone --recursive https://github.com/soramimic/soramimic-video.git
cd soramimic-video
uv sync                          # Python側(替え歌変換の soramimic ライブラリも入る)
```

`--recursive` で取得する submodule は単語リスト(`external/soramimic-wordlists`)と
editor 連携の設定(`external/soramimic/conf/setting.json`)に使う。替え歌変換ロジック
自体は Python パッケージ [soramimic](https://github.com/soramimic/soramimic-python) を
直接利用するため、Node は不要。

外部ツール(使うステージだけでよい):

- [NEUTRINO](https://studio-neutrino.com/) — `synthesize` に必要。
  展開先を環境変数 `NEUTRINO_ROOT` で指定
- fluidsynth + サウンドフォント — `mix` の伴奏レンダリングに必要
  (`brew install fluidsynth`)
- ffmpeg — `mix` / `video` に必要

## 使い方

```sh
# 1. XF MIDI解析+元歌詞アライメント
uv run soramimic-video analyze --midi song.mid --lyrics lyrics.txt --project work/song

# 1'. XF MIDIが無い場合: 歌唱音源から解析(要 uv sync --extra audio)
#     --lyrics 省略時はWhisperの認識結果を元歌詞として使う
#     --melody-midi でメロディ入りMIDI(非XFでよい)を渡すとピッチ・タイミングが楽譜に寄って大幅に良くなる
uv run soramimic-video analyze-audio --audio song.wav --lyrics lyrics.txt \
  --melody-midi song.mid --project work/song

# 2. 替え歌単語歌詞に変換(soramimic)
uv run soramimic-video convert --project work/song --wordlist stations

# 3. 人手編集(任意)
uv run soramimic-video export-edit --project work/song
#    work/song/edit.json を編集して…
uv run soramimic-video import-edit --project work/song

# 4. NEUTRINOで歌唱合成
NEUTRINO_ROOT=~/NEUTRINO uv run soramimic-video synthesize --project work/song --model MERROW

# 5. 伴奏とミックス(音源入力のプロジェクトは分離済み伴奏を使うのでsoundfont不要)
uv run soramimic-video mix --project work/song --soundfont /path/to/GeneralUser.sf2

# 6. 替え歌動画(単語リストの画像+元歌詞/替え歌字幕)
#    --layout で画像と列情報(説明文など)の配置を変えられる
#    (組み込み: default/caption。JSONで自作可、examples/layouts/ 参照)
uv run soramimic-video video --project work/song --layout caption
```

## Web UI(APIサーバー)で使う

ローカル/自宅サーバーでAPIサーバーを立て、ブラウザから投入・進捗確認・動画取得ができる。

```sh
uv run soramimic-video serve            # http://127.0.0.1:8300/
```

MIDIと単語リスト(または editor の書き出しJSON)を入れて「動画を生成」するだけ。
`SORAMIMIC_VIDEO_API_KEY` を設定すると全APIで `X-API-Key` を必須にできる(LAN外公開時)。

### soramimic editor を同梱して画面内で替え歌を編集する(任意)

soramimic の編集ツール(submodule `external/soramimic/frontend`)を静的ビルドして
同梱すると、Web UI から「この場でeditor編集」ボタンで替え歌変換 → その場の
エディタ(iframe)で単語の差し替え・再生成 → 「取り込んで閉じる」で編集結果を
そのまま動画生成に使える(JSONの手動書き出し・アップロードが不要になる)。

```sh
scripts/build-editor.sh                 # external/soramimic/frontend/dist を生成
uv run soramimic-video serve            # dist があれば自動で /editor/ に同梱配信
```

ビルドには Node が必要(`scripts/build-editor.sh` が `npm ci` と
`vite build --base=/editor/` を実行する)。dist を別の場所に置く場合は
`serve --editor-dist <path>` で指定する。dist が無ければボタンは表示されず、
従来どおり editor の書き出しJSONをファイルアップロードして使える。

## ブラウザ+Colabで使う(ローカル環境不要)

1. [soramimic.com](https://soramimic.com) で「MIDIから取り込み」→ 変換 → 編集ツールで調整 → 「書き出し」(JSON)
2. [notebooks/colab_render.ipynb](notebooks/colab_render.ipynb) をGoogle Colabで開き、
   MIDIと書き出したJSONをアップロードして実行 → 替え歌動画(out.mp4)ができる

Colab側の事前準備(NEUTRINOをGoogle Driveに置く等)はノート内の手順を参照。

## 開発

```sh
uv run pytest        # テスト(楽曲データは使わず合成フィクスチャで実行)
uv run ruff check .
uv run mypy src
```

## 注意

- 著作権のある楽曲のMIDI・歌詞・音源・動画はコミットしないこと(`work/` は gitignore 済み)。
- 単語リスト画像(駅写真など)を動画で使う際は `credits.md`(自動生成)の
  ライセンス表記に従うこと。
