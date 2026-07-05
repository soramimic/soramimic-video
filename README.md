# soramimic-video

XF MIDI(カラオケ歌詞入りMIDI)と元歌詞テキストを入力に、
[soramimic](https://github.com/soramimic/soramimic) の単語リストで替え歌歌詞を生成し、
NEUTRINO で歌わせて、画像+字幕つきの替え歌動画まで作るパイプライン。

設計の詳細は [DESIGN.md](DESIGN.md) を参照。

## セットアップ

```sh
git clone --recursive https://github.com/soramimic/soramimic-video.git
cd soramimic-video
uv sync                          # Python側
(cd bridge && npm ci)            # 変換ブリッジ(Node)側
```

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

# 2. 替え歌単語歌詞に変換(soramimic)
uv run soramimic-video convert --project work/song --wordlist stations

# 3. 人手編集(任意)
uv run soramimic-video export-edit --project work/song
#    work/song/edit.json を編集して…
uv run soramimic-video import-edit --project work/song

# 4. NEUTRINOで歌唱合成
NEUTRINO_ROOT=~/NEUTRINO uv run soramimic-video synthesize --project work/song --model MERROW

# 5. 伴奏(元MIDI、メロディ消音)とミックス
uv run soramimic-video mix --project work/song --soundfont /path/to/GeneralUser.sf2

# 6. 替え歌動画(単語リストの画像+元歌詞/替え歌字幕)
uv run soramimic-video video --project work/song
```

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
