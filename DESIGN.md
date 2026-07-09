# soramimic-video 設計

XF MIDI(または歌唱音源)と元歌詞テキストを入力に、替え歌歌唱音源と替え歌動画を
生成するパイプライン。

## 全体像

```
XF MIDI ──┐
          ├─ 1. analyze ──────> project.json(歌唱モーラ+タイミング+元歌詞アライメント)
元歌詞 ────┘
歌唱音源 ──┬─ 1'. analyze-audio ─> project.json(同上。元歌詞はオプショナル)
[元歌詞] ──┘
              2. convert ──> project.json に替え歌案を追記(soramimic変換)
              3. export-edit / import-edit ──> 人手編集(将来: soramimic editor連携)
              4. synthesize ──> MusicXML → NEUTRINO → vocal.wav
              5. mix ──> 伴奏(元MIDIメロディ消音 or demucs分離) + vocal.wav → song.wav
              6. video ──> 画像(単語リスト由来) + 字幕(元歌詞/替え歌) + song.wav → out.mp4
```

各ステージは CLI サブコマンド。中間成果物はすべてプロジェクトディレクトリの
`project.json` に集約し、ステージ間の受け渡しはこのファイルだけで行う。

## 入力

- **XF MIDI**: XFKM チャンクに歌唱タイミング付き歌詞(`表記[読み` 形式、`/`=改行、
  `<`=改ページ)が入っている。発音(読み)と音符タイミングの正解はこちら。
  解析には [xfmido](https://github.com/jiroshimaya/xfmido) を使う。
- **元歌詞テキスト**: 字幕表示用。XF MIDI の歌詞は発音主体で、また元歌詞の全行が
  歌われているとは限らないため、XF の歌唱モーラを基本としつつ元歌詞と
  アライメントして持つ。

## project.json スキーマ(v1)

```jsonc
{
  "version": 1,
  "song": {
    "midi_path": "song.mid",  // 音源プロジェクト(analyze-audio)では ""
    "ticks_per_beat": 480,
    "melody_channel": 1,      // $Lyrc ヘッダ由来
    "time_offset": 0,
    "language": "JP",
    "tempo_map": [[0, 500000], ...],  // [tick, us/beat]。音源入力では固定BPM1本
    // 以下は analyze-audio(歌唱音源入力)のときのみ
    "audio_path": "song.wav",
    "vocals_path": "separation/vocals.wav",
    "accompaniment_path": "separation/no_vocals.wav"  // mixがそのまま伴奏に使う
  },
  "notes": [                  // 歌唱モーラ(=歌詞イベントが付いたメロディ音符)
    {
      "id": 0, "midi_note": 65,
      "start_tick": 5260, "end_tick": 5500,
      "start_sec": 3.27, "end_sec": 3.42,
      "line": 0,
      "surface": "沈", "kana": "シ", "raw": "沈[し"
    }
  ],
  "lines": [                  // XF の行(`/` 区切り)
    {
      "id": 0,
      "xf_surface": "沈むように",
      "xf_kana": "シズムヨウニ",
      "note_ids": [0, 1, 2, 3, 4, 5],
      "original_text": "沈むように溶けてゆくように"  // アライメント結果(無ければnull)
    }
  ],
  "parody": {
    "wordlist": "stations", "where": null,
    "params": {"REPEAT": "100", ...},
    "lines": [
      {
        "line_id": 0,
        "words": [
          {
            "surface": "静岡", "kana": "シズオカ",
            "original": "静岡駅",          // 単語リストの original 列
            "wordlist_row": {...},          // 画像URL等を含む行データ
            "original_surface": "沈むよう", "originalkana": "シズムヨウ",
            "note_ids": [0, 1, 2, 3],       // この単語が歌われる音符
            "locked": false
          }
        ]
      }
    ]
  }
}
```

## 各ステージ

### 1. analyze(XF解析+元歌詞アライメント)

- XFKM の歌詞イベント列(デルタ時間つき)とメロディチャンネルの note_on/off を
  tick で突き合わせ、「歌唱モーラ=音符+表記+読み」の列を作る。
- tempo map で tick → 秒に変換。
- `/` で行に分割。行の XF 表記(`沈むように` 等)と元歌詞テキストの行を
  文字ベースの DP(difflib)でアライメントする。歌われていない元歌詞行は
  どの XF 行にも対応しない。XF 行が元歌詞に無い場合は original_text=null。

### 1'. analyze-audio(歌唱音源からの解析) — issue #1

XF MIDI が手に入らない曲向けに、歌唱音源(wav/mp3)から project.json を作る。
以降のステージはそのまま流用できる(mix のみ伴奏の扱いが変わる)。
重い依存(torch/demucs/whisper等)は extras `audio` に分離。

1. **音源分離(demucs)**: vocals.wav / no_vocals.wav に分離。
   no_vocals.wav は `song.accompaniment_path` に記録し、mix がそのまま伴奏に使う。
2. **歌詞行の決定**: `--lyrics` があればその行構成を使う。無ければ
   Whisper(faster-whisper)の認識結果をセグメント=行として元歌詞に使う。
3. **モーラタイミング(forced alignment)**: 歌詞をMeCabでカナ化しモーラ分割、
   カナ1文字ずつを reazon wav2vec2(rs35kh)のトークンに落として
   `torchaudio.functional.forced_align` でアライメント。長い音源は
   チャンク分割で logits を連結。語彙外文字のモーラは近傍から補間。
4. **ピッチと音符終端**: pyin の f0 をモーラ区間で切り出し中央値 → midi_note。
   CTC スパンはスパイク状で短いため、音符終端は有声区間が途切れるか
   次のモーラが始まるまで伸長する。v1 は 1モーラ=1音符(メリスマは将来課題)。
5. **tick 換算**: テンポ復元はせず固定BPM(既定120)のテンポマップを1本置き、
   実測秒から直接換算。NEUTRINO 用 MusicXML は音楽的に正しい音価を
   要求しないのでこれで足りる。start_sec/end_sec は実測値を保持するので
   video / mix に換算誤差は影響しない。

検証用に `analyze_audio/moras.srt` / `lines.srt` を書き出す
(プレーヤーで字幕表示してタイミングを目視確認する)。

**メロディMIDI併用(`--melody-midi`, issue #3)**: 普通のSMFがあれば
「採譜」でなく「楽譜と演奏のアライメント」にできる。
方針は「MIDIを基準にし、補正は大域線形写像(テンポスケール+オフセット)と
移調だけ」。モーラ開始列と音符開始列の一致度の格子探索で線形写像を推定し、
CTCのモーラ開始時刻と写像済み音符開始時刻を時刻+音高コストの単調DP
(多対一: MIDIが同音連打を1音符にまとめた箇所はモーラが音符を共有)で対応づける。
ピッチはMIDIそのまま、開始時刻は両者が近ければCTC(実際の歌い出し)、
余り音符はメリスマ(kana="ー")、余りモーラは f0 フォールバック。
f0とMIDIの音高差の中央値で移調も自動補正。メロディチャンネルは
「被覆率+音高輪郭MAD」の照合スコアで自動選択(`--melody-channel` で上書き)し、
和音混じりのチャンネルは skyline 法で最高音の旋律線を取り出す。
ゲート由来の音符間の隙間(0.15秒以下)はレガート接続する。

当初はクロマDTWで写像を推定していたが、フレーム粒度(約0.2秒)がモーラ間隔と
同オーダーで対応付けを壊した(XF正解評価でピッチ一致50%)。大域線形写像への
置き換えで99%(同曲)。テンポ変化のある曲への対応(区分線形化)は将来課題。

**評価(`eval-audio`)**: XF MIDIがある曲では analyze の出力を正解として、
音源経路の出力をカナ列DP対応付けで突き合わせ、ピッチ一致率・タイミング残差
(時間軸オフセット補正後)を数値評価できる。試聴に頼らず改善を判定するための
ハーネス。

### 2. convert(soramimic変換)

- 替え歌生成は Python パッケージ [soramimic](https://github.com/soramimic/soramimic-python)
  (soramimic 本体 `frontend/src/lib` の挙動互換移植)を `soramimic_engine.py` から
  直接呼ぶ。トークナイザは fugashi + ipadic(`soramimic[mecab]`)。以前は Node
  ブリッジ(kuromoji)経由だったが、Python 完結にして node/npm 依存を無くした。
  app(辞書データ + トークナイザ)の構築は重いのでモジュールに遅延キャッシュする。
- 変換の入力は **XF の読み(カナ)を行ごとに連結した文字列**。表記でなく読みを
  使うのは、変換結果の period(音節区間)を文字オフセット経由で確実に
  音符列へ写像するため。
- エンジンは行ごとに `{units, words}`(と editor 再生成用の `tokensList`)を返し、
  Python 側で 音節区間 → カナ文字区間 → 音符ID列 に変換して project.json に格納する。
- 単語リストは submodule `external/soramimic-wordlists` の CSV。
  `--wordlist stations --where "status=current"` のように editor の
  conf/setting.json と同じ流儀で指定。

### 3. edit(人手編集)

- v1 はファイルベース: `export-edit` が編集用 JSON を出力し、人が
  surface/kana を書き換えて `import-edit` で取り込む。読みのモーラ数が
  音符区間と一致しない場合はエラーにする(歌わせ方が決められないため)。
- 将来: soramimic editor(#17)と直接連携する。editor の
  sessionStorage スキーマ(results/tokensList/unitsList)への変換は
  このリポジトリの exchange フォーマットから可能な設計にしてある。

### 4. synthesize(NEUTRINO歌唱合成)

- メロディ音符+替え歌カナから MusicXML を生成(先頭から休符を入れて
  曲頭からの絶対時間を保つ → ミックス時のタイミング合わせが不要になる)。
- NEUTRINO は `NEUTRINO_ROOT` 環境変数で場所を指定(未同梱。
  https://studio-neutrino.com/ から取得)。
  `bin/musicXMLtoLabel → bin/NEUTRINO → bin/NSF` の順に呼ぶ。
  `--dry-run` でコマンド列の確認のみも可能。

### 5. mix(伴奏+歌唱)

- 元 MIDI からメロディチャンネルの note イベントを除いた伴奏 MIDI を作り、
  fluidsynth(要 `brew install fluidsynth` + サウンドフォント)で wav 化。
  音源プロジェクト(`song.accompaniment_path` あり)は demucs 分離済みの
  伴奏 wav をそのまま使う(fluidsynth 不要)。
- vocal.wav は曲頭からレンダリングされているので、ffmpeg の amix で重ねるだけ。

### 6. video(替え歌動画)

- 各替え歌単語の歌唱区間 [start_sec, end_sec] に、単語リスト行の `image`
  (例: stations.csv の駅写真)を表示。画像はローカルにキャッシュし、
  `image_page` からクレジット一覧(credits.md)を生成する(CC BY-SA対応)。
- 字幕: ASS を生成し、画面下部に「替え歌歌詞(上)/元歌詞(下)」を
  行の歌唱区間で表示。
- ffmpeg 一発(背景+overlay enable=between(t,a,b)+subtitles)で合成。

## リポジトリ構成

```
src/soramimi_video/   Python パッケージ(CLI: soramimic-video)
                      soramimic_engine.py が soramimic ライブラリで替え歌変換を実行
external/soramimic            (submodule, devブランチ) editor連携用の conf/setting.json
external/soramimic-wordlists   (submodule) 単語リスト
tests/                pytest(XF MIDIはテスト用に合成したフィクスチャを使う)
```

## 制約・注意

- 著作権のある楽曲の MIDI・歌詞・音源はリポジトリにコミットしない
  (テストは合成フィクスチャで行う)。リポジトリは当面 private。
- NEUTRINO・fluidsynth・サウンドフォントは環境依存の外部ツールとして扱い、
  実行時に存在チェックして分かりやすいエラーを出す。
- wordlists の画像利用時は image_page のライセンス表記に従う(credits.md 自動生成)。
