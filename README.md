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

# 1''. モーラのタイミングを手直しする(任意。ピアノロールGUI)
#      音高と長さが見える画面で、モーラの位置・長さ・読みを直せる。project.jsonを直接書き換える
#      (保存時に project.json.bak-* を残す)。LANの別端末から開くなら --host 0.0.0.0
uv run soramimic-video edit-timing --project work/song
#      → http://127.0.0.1:8765/ をブラウザで開く

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

手元だけで使う権利曲などは、`src/soramimic_video/static/sample/samples.local.json` に
サンプル情報を置くと、公開用の `samples.json` へ混ぜずに一覧へ追加できる。このファイルと
権利曲のMIDI・歌詞はgitignore対象。既存の `SORAMIMIC_SAMPLES_DIR` を使えば、別ディレクトリの
`samples.json` と `<id>.mid` / `<id>_lyrics.txt` に差し替えることもできる。

曲と単語リストを選び、その場に出るサムネ枠をタップするだけで生成が始まる。
カードの右上には **🎲(曲と単語リストをランダムに選ぶ)** と **⚙(替え歌を編集)**。
単語リストの絞り込みと変換のしかたの調整は、⚙ で開く同梱エディタの⚙モーダル
(「変換のしかた」)にまとめてある(そこで選び直した単語リストはカードの
プルダウンにも反映される)。
自分のMIDI・歌声・レイアウトは「⚙️ 詳細設定」から入れる。
`SORAMIMIC_VIDEO_API_KEY` を設定すると全APIで `X-API-Key` を必須にできる(LAN外公開時)。

### 自作の単語リストを使う

用意されているリストに無いものに空耳させたいときは、**替え歌エディタの⚙モーダル**
(カードの⚙ → 「変換のしかた」)の単語リストで「自作リスト」を選び、単語をその場に
書いて再変換する。書いた内容は書き出しJSONの中(`csvText`)に入るので、そのまま
動画生成にも使われる(video側に保存する操作は要らない)。

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

エディタの中で自作リストに切り替えると、書き出しJSONの単語リストが
`{"value": "ORIGINAL", "text": "自作リスト", "csvText": "<正規化済みtidy CSV>"}` に
なる。単語データがJSON自体に入っている自己完結の形なので、サーバー側のセッションは
要らない。投入(`POST /api/jobs`)・レイアウトプレビューはこの `csvText` から
そのまま単語行を引く(`id` 列は書き換えない。JSONの `results` の id と対応するため)。
取り込み時は `<ジョブディレクトリ>/original-wordlist.csv` に置く。

制限と非対応:

- **単語画像は付けられない**(`image` 列はこの経路では常に落とす。サーバーのファイルや
  URLを外から指させないため)。画像つきの単語リストは当面、サーバーに同梱している
  名前付きリストだけ。将来ホスト移譲方式(サーバーに置いたリストをエディタにも
  見せる)で自作リストの画像対応を戻す予定
- 絞り込み(where)は効かない(名前付きリストのファセットが対象)
- サムネのプレビュー・🎲ランダムの抽選は非対応(名前で引けないため)。カードの
  単語リストのプルダウンには「自作リスト(替え歌エディタ)」が選ばれた状態で出る

#### 自作リストをAPIから渡す(画像つき)

画面の導線は撤去したが、**サーバー側のAPIはそのまま残している**(将来の画像対応の
再利用と、API利用者の互換のため)。`POST /api/wordlist-check` で列・行数・読みを
検査でき、`POST /api/jobs` へは2通りで渡せる。

- 書いた内容 + 画像 → `wordlist_text` / `wordlist_name` / `wordlist_images`(画像は
  1枚ずつ。ファイル名で行に結びつく)
- 画像入りzip → `wordlist_csv`(CSV単体のアップロードも同じフィールド)

渡した内容は**そのジョブのディレクトリにだけ**保存されて変換に使われる(単語リストDBの
共有キャッシュには載せない)。画像の結びつけ方は次のとおりで、列に書いたほうが優先される。

- CSVの `image` 列に渡した画像の**ファイル名**を書く(`tanaka.jpg`)。URLは受け付けない
- 何も書かなければ、`original`(正式名称。かんたん形式では表記)と**同じ名前**の画像を
  自動で当てる(`田中太郎` の行に `田中太郎.jpg`)

画像は magic で PNG/JPEG/WebP だけを通し、Pillow で開き直してから保存する(EXIFの
位置情報などは落ちる)。上限は全体 30MB / 1枚 10MB / 1,000枚で、
`SORAMIMIC_MAX_WORDLIST_ZIP_BYTES` / `SORAMIMIC_MAX_WORDLIST_IMAGE_BYTES` /
`SORAMIMIC_MAX_WORDLIST_IMAGES` で変えられる。画像もCSVと同じくそのジョブの
ディレクトリ(`<ジョブ>/wordlist/images/`)にだけ置かれる。CSV自体のサイズ上限は
10MB / 10,000行(`SORAMIMIC_MAX_WORDLIST_BYTES` / `SORAMIMIC_MAX_WORDLIST_ROWS`)。

`POST /api/editor-session` も自作リストを受け取れる。正規化済みCSVを
`<ジョブディレクトリ>/editor-sessions/<sid>/wordlist.csv`(`sid` は中身の指紋)に
置いてから、editorに `/editor/session-wordlists/<sid>.csv` として引かせる形で、
書き出しJSONの単語リストは `{"value": "custom:<sid>", ...}` になる。この形の
JSONは画面でも従来どおり「自作リスト」として読める。

### 🎲ランダムのプレビュー画像を隠す単語リスト

カードのサムネプレビューが作れないとき(変換待ち・失敗)は単語リストの代表画像に
落ちるが、
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
見たい人はその場で表示できる(組み合わせを変えるとまた隠れる)。
**動画やサムネの画像は対象外**で、従来どおり画像が出る。

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
同梱すると、Web UI のカード右上の⚙(替え歌を編集)でその場のエディタ(iframe)が
開き、セットアップ画面(曲・単語リスト・変換のしかた)→ 変換 → 単語の差し替え・
再生成 → その編集内容がそのまま動画生成に使える(JSONの手動書き出し・
アップロードも、取り込み操作も不要になる)。
自動で使うのは「実際に編集したとき」だけで、しかも編集を作ったときの入力
(曲・単語リスト・変換パラメータ)が生成時の選択と一致する場合に限る。
食い違うときは古い編集を使わず、その場の選択で自動変換して生成する。

⚙を押したときサーバーがやるのは**解析だけ**(`POST /api/editor-session` に
`convert=0`)で、返すのは行ごとの読みカナ(`phrases`)と初期設定
(`wordlist` / `param` / `song` / ノート長生重み `noteLengthRawList` /
ノート長設定 `noteLengthAlpha` / 元歌詞 `lyrics`)。
絞り込み(where)は `wordlist` エントリと、シードのトップレベルの両方に載せる。
エディタはトップレベルの where からファセットのチェック状態を復元する
(`restoreFacets`)ので、式の形はエディタの `facetClause` + `compileWhere` と
そろえてある(`((type=family) or (type=full))` の形。正本は
`src/soramimic_video/facets.py`、3実装の一致は `tests/test_facets.py` が
実際にJSを走らせて固定)。チェックボックスで表せない形の where(自作リストや
手書きの条件)はトップレベルに載せない——載せるとチェックが1つも当たらず、
エディタが組み直した時点で絞り込みが消えて、送った条件より広くなる。
元歌詞(字幕用)はシードの `lyrics`(生テキスト。ルビ記法も素通し)で渡し、
エディタは編集後の `lyrics` を書き出しJSONに載せて返す。エディタで直した
`lyrics` は Web UI が正本(元歌詞欄)へ書き戻し、字幕の行対応づけは従来どおり
video 側の `align_lines` が行う。エディタも行ごとの対応づけ `originalLines` を
書き出すが**採用しない**——ブラウザ側の対応づけは境界がずれる・対応づかない行が
出るなど `align_lines` より精度が低く、字幕が劣化するため(あちらはエディタの
表示用)。フォームに元歌詞が無いまま editor.json だけを持ち込んだときだけ、
JSONの `lyrics` を `align_lines` にかけて字幕の元歌詞を埋める。
変換そのものはエディタがブラウザ内で行う。`convert` を送らなければ従来どおり
サーバーで変換した `results` 入りのJSONが返る(「続きから再開」で開き直す
保存済みの編集はこの形なので、編集画面から始まる)。

```sh
scripts/build-editor.sh                 # external/soramimic/frontend/dist を生成
uv run soramimic-video serve            # dist があれば自動で /editor/ に同梱配信
```

ビルドには Node が必要(`scripts/build-editor.sh` が `npm ci` と
`vite build --base=/editor/` を実行する)。dist を別の場所に置く場合は
`serve --editor-dist <path>` で指定する。dist が無ければボタンは表示されず、
従来どおり editor の書き出しJSONをファイルアップロードして使える。

単語リストの選択・絞り込みと変換のしかた(プリセット・音の合わせ方・文節の区切り・
単語の長さ・単語重複・ノート長重視α)は、**エディタの中の「変換のしかた」に
一本化**している。ノート長の生重みはvideo側が曲から作って渡し、αの設定と
指数計算はsoramimic側が担う。CLI/API向けの `NOTE_LENGTH_WEIGHT` は後方互換として
引き続き利用できる。指定しなかった他のパラメータはサーバー既定
(本家のバランス相当・単語重複なし)になる。

エディタの中で単語リストを選び直すと、その結果は親画面の正本(リスト名と絞り込み)へ
書き戻される。カードの状態表示・サムネプレビュー・生成時の単語画像とレイアウトの
解決はすべてこの値を見ているので、エディタで替えたリストがそのまま画面と生成に反映される
(そのとき編集内容が「別の入力から作られたもの」として捨てられないよう、来歴も
同時に付け替える)。

## ブラウザ+Colabで使う(ローカル環境不要)

1. [soramimic.com](https://soramimic.com) で「MIDIから取り込み」→ 変換 → 編集ツールで調整 → 「書き出し」(JSON)
2. [notebooks/colab_render.ipynb](notebooks/colab_render.ipynb) をGoogle Colabで開き、
   MIDIと書き出したJSONをアップロードして実行 → 替え歌動画(out.mp4)ができる

Colab側の事前準備(NEUTRINOをGoogle Driveに置く等)はノート内の手順を参照。

## 開発

### ブランチ運用

- `main`: 本番へデプロイ可能な確定版。通常の開発PRは直接入れない。
- `preview`: 公開候補だけを載せる常設ブランチ。`preview-video.soramimic.com`で確認する。
- `dev`: 開発中の変更を集約する常設ブランチ。`dev-video.soramimic.com`で統合状態を確認する。
- feature/fix PRは`dev`宛てにし、CI成功後に自動マージする。自動マージしない場合は`no-automerge`ラベルを付ける。
- 公開する変更は最新`preview`から`promote/<内容>`ブランチを作り、対象dev PRのcommitだけをcherry-pickして`preview`へPRを出す。`dev`全体はマージしない。
- `preview` PRはCIとプレビュー環境で確認し、操作者本人の明示承認後に手動マージする。AIエージェントや自動化は、CI成功や一般的な完了指示を承認とみなしてはならない。途中の変更や公開しない変更はpreviewへ入れない。
- 毎週月曜日または手動の`release` workflowは、その時点の`preview` SHAを固定して再テストし、成功時はjob summaryに`preview`→`main`のPR作成リンクを表示する。ActionsのtokenではPRを作成せず、`main`へ直接pushもしない。
- summaryの検証済みSHAと現在の`preview` SHAが一致することを確認してrelease PRを作成する。検証中にpreviewが進んだ場合はworkflowを再実行する。
- release PRは自動マージしない。最終的にPRの**現在のhead SHA**と`main`とのmerge結果に対する必須チェックが成功していることを確認し、preview確認後に操作者本人が明示承認してからマージする。AIエージェントによるPR作成・CI監視は承認の代わりにならない。
- 公開時はrelease PRのマージcommit SHAを本番へデプロイし、health/readinessと主要動線を確認する。問題があれば直前に公開したSHAへロールバックする。

`main` のbranch protectionでは、少なくとも通常CIの `test / test`（`main` とのmerge結果）と
`release / test-release-pr-head / test`（PRのhead SHAそのもの）を必須にする。この2つは
意図的に別々に実行し、統合後の状態と公開候補そのものを両方確認する。

3ブランチ方式へ初めて移行するときは、次の順序でbootstrapする。

1. 旧 `release` workflowは実行しない。
2. `main`から`preview`を作成し、branch protectionを設定する。
3. 新workflowと文書を通常どおり`dev`へ入れ、promotion PRで`preview`へ移す。
4. bootstrap時だけ`preview`→`main` PRへ`emergency`ラベルを付け、旧retarget workflowを回避して手動マージする。
5. main上で新workflowが有効になったら`emergency`ラベルは通常releaseでは使わず、Actionsの`release`を手動実行する。

緊急修正だけは次の手順で `main` へ直接PRを出す。

1. 修正ブランチのPRをいったん `dev` 宛てで作る。
2. `emergency` ラベルを付けてから、baseを `main` に変更する（ラベルなしで `main` にすると自動的に `dev` へ戻される）。
3. 必須チェックと差分を確認して手動マージし、本番へデプロイする。
4. 公開後すぐに最新の`main`から同期ブランチを作り、まず`preview`、続いて`dev`への同期PRを作成してマージする（`main`自体をheadにしない）。これにより緊急修正が次の公開候補や開発版から欠落しない。

promotion例:

```sh
git fetch origin dev preview
git switch -c promote/example origin/preview
git cherry-pick <dev PRで取り込んだcommit>
git push -u origin promote/example
# GitHubで promote/example -> preview のPRを作成する
```

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
- 動画フレームの左下には「lyrics & video by Soramimic」を小さく焼き込む。VOICEVOXで
  歌わせたジョブは規約に合わせて「lyrics & video by Soramimic / VOICEVOX:キャラ名」になる。
  レイアウトの `"app_credit": false` で無効化、text要素で `{app_credit}` を
  自前配置すれば位置・見た目を変えられる(無効化した場合、VOICEVOXのクレジットは
  動画の説明欄などで自分で表記すること)。
- エンドクレジットは「元曲名 — 表記」の1行に簡潔にまとめる。Web UIの
  「元曲クレジット」で著作者と権利者指定の表記を入力でき、指定表記が
  ある場合は改変せずそちらを優先する。CLIでは
  `--song-title`、`--original-credit`、`--credit-notice`を使う。
