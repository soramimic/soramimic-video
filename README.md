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
- libcairo — 単語リストのSVG画像(ポケモンの型色カード・選手カード・YouTuberカード)を
  PNGに焼くのに使う `cairosvg`(`uv sync --extra api` で入る)のシステム依存。
  macOSは `brew install cairo`、Debian/Ubuntuは `apt install libcairo2`。
  無い場合はSVGの単語画像だけが「画像なし」になる(警告ログのみでジョブは通る)。
  SVGカードのフォント指定(Hiragino/Noto)を活かすには日本語フォント
  (Noto Sans CJK JP など)もインストールしておくこと

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

曲と単語リストを選び、その場に出るサムネ枠をタップするだけで生成が始まる。
自分のMIDIや editor の書き出しJSONを使うときは「⚙️ 詳細設定」から入れる。
`SORAMIMIC_VIDEO_API_KEY` を設定すると全APIで `X-API-Key` を必須にできる(LAN外公開時)。

### 自作の単語リストを使う

単語リストのプルダウンで「自作リストを使う」を選ぶと専用の画面が開き、単語をその場に
書いてそのまま変換に使える(用意されているリストに無いものに空耳させたいとき用。
以前あった「その他(名前を入力)」の代わり)。手元の `.csv`/`.txt` を「ファイルから
読み込む」で流し込んでもよい。単語画像も付けたいときは、画像を選ぶか、CSVと画像を
まとめたzipを上げる(後述)。

いちばん簡単な書き方は **1行に1語ずつ「表記,読み」**。

```csv
高輪ゲートウェイ,タカナワゲートウェイ
茅ヶ崎,チガサキ
海,ウミ,カイ            # 同じ語に読みを複数書ける
ネコ                     # 読みを省くと表記から自動で推定する
# シャープ以降は行末までコメント
```

- 読みは**カタカナかひらがな**。漢字混じりの読みはエラーになる(自動推定させたい
  ときは読みの欄ごと省く)
- 字幕・画像枠に出るのは**表記のほう**(読みは音符への当てはめだけに使う)

`external/soramimic-wordlists` と同じ**表形式(tidy CSV)**でもよい。1行目に列名を
書き、`surface`(表記)列があればこちらとして読む。

```csv
id,original,surface,pronunciation,team
1,阪神タイガース,阪神,ハンシン,セ
1,阪神タイガース,タイガース,タイガース,セ
```

- 必須は `surface` だけ。`pronunciation` は空なら表記から推定、`id`/`original` は
  無ければ補完する。それ以外の列(上の `team` など)はレイアウトの `{列名}` から
  参照できる
- 列名は BOM・前後の空白・大文字小文字を無視して照合し、`単語`/`表記`(=surface)、
  `読み`/`カナ`/`ふりがな`(=pronunciation)といった日本語の列名も受け付ける
- 文字コードは UTF-8(BOM付き可)と Shift_JIS(Excelの「CSV(カンマ区切り)」)

書き換えるたびにサーバー(`POST /api/wordlist-check`)が列・行数・読みを検査し、
読めた語数を出すか、駄目なら理由をその場に出す。通った内容は投入時に
`POST /api/jobs` へ送られ、**そのジョブのディレクトリにだけ**保存されて変換に
使われる(単語リストDBの共有キャッシュには載せない)。送り方は2通り:

- 書いた内容 + 画像 → `wordlist_text` / `wordlist_name` / `wordlist_images`(画像は
  1枚ずつ。ファイル名で行に結びつく)
- 画像入りzip → `wordlist_csv`(CSV単体のアップロードも同じフィールド)

#### 単語画像も付ける

自作リストでも単語画像を出せる。画像は**画面で選ぶ**か、**CSVと画像を1つにまとめた
zip** をアップロードする(zipの中身はCSV1枚(`.csv` か `.txt`)と画像
(PNG/JPEG/WebP)だけ)。どちらも画像の結びつけ方は同じで、列に書いたほうが優先される。

- CSVの `image` 列に選んだ画像の**ファイル名**を書く(`tanaka.jpg`)。URLは受け付けない
- 何も書かなければ、`original`(正式名称。かんたん形式では表記)と**同じ名前**の画像を
  自動で当てる(`田中太郎` の行に `田中太郎.jpg`)

画像は magic で PNG/JPEG/WebP だけを通し、Pillow で開き直してから保存する(EXIFの
位置情報などは落ちる)。上限は全体 30MB / 1枚 10MB / 1,000枚で、
`SORAMIMIC_MAX_WORDLIST_ZIP_BYTES` / `SORAMIMIC_MAX_WORDLIST_IMAGE_BYTES` /
`SORAMIMIC_MAX_WORDLIST_IMAGES` で変えられる。画像もCSVと同じくそのジョブの
ディレクトリ(`<ジョブ>/wordlist/images/`)にだけ置かれる。

制限と非対応:

- サイズ上限は 2MB / 10,000行。`SORAMIMIC_MAX_WORDLIST_BYTES` /
  `SORAMIMIC_MAX_WORDLIST_ROWS` で変えられる
- `image` 列を受け付けるのは画像を一緒に渡したときだけ(実体を渡した画像しか指せない)。
  画像なしのCSVでは `image` / `image_page` 列は落とし、**文字だけ**の動画になる
  (サーバーが任意のURLを取りに行かないため)。レイアウトはどちらもサーバー既定
- 画面に書いた内容とリスト名はブラウザに残るが、**選んだ画像は残らない**
  (リロード後は選び直す)
- サムネのプレビュー・🎲ランダムの抽選は非対応
- 同梱editorでの替え歌編集はできる。「✏️ 替え歌を編集」を押すと
  `POST /api/editor-session` が自作リストも受け取り、正規化済みCSVを
  `<ジョブディレクトリ>/editor-sessions/<sid>/wordlist.csv`(`sid` は中身の指紋)に
  置いてから、editorに `/editor/session-wordlists/<sid>.csv` として引かせる。
  ただし絞り込み(where)は自作リストでは効かない(投入時も空で送る)
- editorの中(⚙)で自作リストに切り替えたときは、書き出しJSONの単語リストが
  `{"value": "ORIGINAL", "text": "自作リスト", "csvText": "<正規化済みtidy CSV>"}` に
  なる。単語データがJSON自体に入っている自己完結の形なので、サーバー側のセッションは
  要らない。投入(`POST /api/jobs`)・レイアウトプレビューはこの `csvText` から
  そのまま単語行を引く(`id` 列は書き換えない。JSONの `results` の id と対応するため)。
  取り込み時は `<ジョブディレクトリ>/original-wordlist.csv` に置く。
  `image` 列はこの経路では常に落とす(サーバーのファイルやURLを外から指させない)

### 🎲ランダムのプレビュー画像を隠す単語リスト

「🎲 ランダム」の確認モーダルには単語リストの代表画像がプレビューとして出るが、
昆虫のように「不意に見たくない」人がいるリストは初期非表示にできる。対象は
`src/soramimic_video/static/index.html` の `HIDDEN_PREVIEW_WORDLISTS` 定数
(`SLOW_WORDLISTS` と同じ流儀)で、`{単語リスト名: "隠している理由の文言"}` を
1行足すだけで増やせる。

```js
const HIDDEN_PREVIEW_WORDLISTS = {
  insect: "昆虫の画像が苦手な方への配慮で、プレビューを隠しています。",
};
```

隠しているときは黙って出さず、理由の文言と「画像を表示する」ボタンを出すので、
見たい人はその場で表示できる(引き直すとまた隠れる)。**動画やサムネの画像、
自分で単語リストを選んだときの表示は対象外**で、従来どおり画像が出る。

### 単語リストごとの既定レイアウト

単語リストを選ぶと、その列構成に合う組み込みレイアウトが自動で「レイアウト」欄に入る。
対応表は `src/soramimic_video/wordlist_layouts.json` で、`{"単語リスト名": "組み込みレイアウト名"}`
(単語リスト名は `external/soramimic-wordlists/*.csv` のファイル名から `.csv` を除いたもの、
レイアウト名は `src/soramimic_video/layouts/*.json` の同じくstem)の1階層のJSON。
行を足す・消すだけで反映される(サーバー再起動が必要。`/api/config` の `wordlist_layouts`
としてUIに渡る)。組み込みレイアウトに無い名前を書いたエントリは警告ログを出して無視されるので、
レイアウトを追加してから対応表に足すこと。載っていない単語リストはサーバー既定のレイアウトのまま。
自動で入った値はユーザーがレイアウトを選び直せば上書きされない。

### 一般公開する(公開モード)

`SORAMIMIC_PUBLIC=1` を設定すると、匿名セッション(cookie)ごとのジョブ分離と
投入制限(キュー上限・日次クォータ・曲長上限)、完了ジョブの自動削除、
Cloudflare Turnstile が有効になる。環境変数を設定しなければ従来と同じ挙動なので、
自分専用インスタンスの設定はそのままでよい。環境変数の一覧は
[docs/public-mode.md](docs/public-mode.md) を参照。

### soramimic editor を同梱して画面内で替え歌を編集する(任意)

soramimic の編集ツール(submodule `external/soramimic/frontend`)を静的ビルドして
同梱すると、Web UI から「この場でeditor編集」ボタンで替え歌変換 → その場の
エディタ(iframe)で単語の差し替え・再生成 → その編集内容がそのまま動画生成に
使える(JSONの手動書き出し・アップロードも、取り込み操作も不要になる)。
自動で使うのは「実際に編集したとき」だけで、しかも編集を作ったときの入力
(曲・単語リスト・変換パラメータ)が生成時の選択と一致する場合に限る。
食い違うときは古い編集を使わず、その場の選択で自動変換して生成する。

```sh
scripts/build-editor.sh                 # external/soramimic/frontend/dist を生成
uv run soramimic-video serve            # dist があれば自動で /editor/ に同梱配信
```

ビルドには Node が必要(`scripts/build-editor.sh` が `npm ci` と
`vite build --base=/editor/` を実行する)。dist を別の場所に置く場合は
`serve --editor-dist <path>` で指定する。dist が無ければボタンは表示されず、
従来どおり editor の書き出しJSONをファイルアップロードして使える。

変換のしかた(プリセット・音の合わせ方・文節の区切り・単語の長さ・単語重複)と
単語リストの絞り込みは、**エディタの中の「変換のしかた」に一本化**している。
トップ画面の「② 空耳のもと」に残しているのは、単語リストの選択・「✏️ 替え歌を編集」・
状態表示と、エディタにも本家にも無い soramimic-video 独自の「ノート長重視 α」
(`NOTE_LENGTH_WEIGHT`。α>0 のときだけ `convert_params` に載る)だけ。
指定しなかったパラメータはサーバー既定(本家のバランス相当・単語重複なし)になる。

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
  ライセンス表記に従うこと。クレジット表記が必要な画像(Wikimedia Commonsで
  作者表示が求められるもの)は出典文言を動画フレームに自動で焼き込む
  (レイアウトの `"credit": false` で無効化できるが、その場合は自分で表記すること)。
- 動画フレームの左下には「lyrics by Soramimic」を小さく焼き込む。VOICEVOXで
  歌わせたジョブは規約に合わせて「lyrics by Soramimic / VOICEVOX:キャラ名」になる。
  レイアウトの `"app_credit": false` で無効化、text要素で `{app_credit}` を
  自前配置すれば位置・見た目を変えられる(無効化した場合、VOICEVOXのクレジットは
  動画の説明欄などで自分で表記すること)。
