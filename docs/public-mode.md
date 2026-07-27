# 公開モード(一般公開インスタンス向け)

`soramimic-video serve` を不特定多数に公開するときの設定。自分専用インスタンス
とは環境変数だけで挙動を分ける。**環境変数を何も設定しなければ従来と完全に同じ
挙動**(ジョブは全員から見え、投入制限もなし)なので、自宅サーバーの設定は変えなくてよい。

```sh
SORAMIMIC_PUBLIC=1 \
SORAMIMIC_QUEUE_LIMIT=5 \
SORAMIMIC_DAILY_QUOTA=5 \
SORAMIMIC_MAX_SONG_SECONDS=420 \
SORAMIMIC_JOB_TTL_HOURS=24 \
TURNSTILE_SITE_KEY=0x... TURNSTILE_SECRET_KEY=0x... \
uv run soramimic-video serve --host 0.0.0.0
```

## 環境変数

| 環境変数 | 既定 | 効果 |
|---|---|---|
| `SORAMIMIC_PUBLIC` | 未設定 | `1`(`0`/`false`/`no`/空 以外)で公開モード。以下のセッション分離と投入制限が有効になる |
| `SORAMIMIC_QUEUE_LIMIT` | 5 | 待機中+実行中ジョブの合計上限(サーバー全体)。超過した投入は429 |
| `SORAMIMIC_DAILY_QUOTA` | 5 | セッションごとの直近24時間の投入本数上限。超過は429 |
| `SORAMIMIC_MAX_SONG_SECONDS` | 420 | 入力MIDIの演奏時間の上限(秒)。超過は400 |
| `SORAMIMIC_JOB_TTL_HOURS` | 0(無効) | 完了・失敗・中断から何時間でジョブのディレクトリと履歴を消すか。正の値で1時間ごとに掃除する |
| `SORAMIMIC_SAMPLES_DIR` | 未設定 | 同梱サンプル曲(`static/sample`)の差し替え先ディレクトリ。`samples.json` と `<id>.mid` / `<id>_lyrics.txt` を置く |
| `SORAMIMIC_WARMUP_WORDLISTS` | 未設定 | カンマ区切りの単語リスト名。起動時にバックグラウンドで前処理(parse_tidy)を済ませ、キャッシュに載せておく |
| `TURNSTILE_SITE_KEY` | 未設定 | Cloudflare Turnstileのサイトキー。秘密鍵と両方揃うとフロントにウィジェットが出る |
| `TURNSTILE_SECRET_KEY` | 未設定 | 同・秘密鍵。設定するとジョブ投入時にトークンを検証し、失敗は403 |

`SORAMIMIC_VIDEO_API_KEY`(X-API-Keyによる全API認証)は公開モードとは独立で、
併用も単独利用もできる。公開インスタンスでは通常は設定しない。

## 単語リストのウォームアップ

単語リストの前処理(読み推定 → 音節バリエーション展開)は行数の多いリストだと重く、
`sekitsui`(16,731行)で約3分かかる。結果はプロセス内にキャッシュされるので2回目以降の
変換は数秒で済むが、初回だけは待たされる。`SORAMIMIC_WARMUP_WORDLISTS` を設定すると、
サーバー起動直後にdaemonスレッドが指定順にキャッシュを構築するので、ユーザーから見た
初回変換も速くなる。

```
SORAMIMIC_WARMUP_WORDLISTS=pokemon,nations,sekitsui uv run soramimic-video serve
```

- 起動はブロックしない。構築の開始・完了・所要秒は `soramimic_video.soramimic_engine`
  のINFOログに出る
- 絞り込み条件は空(全行)で構築する。同梱Web UIはファセットが全ONのとき `where=""`
  を送るので、これが最も当たりやすいキーになる。ファセットを絞った変換は別キーになり、
  その分は改めて構築が要る
- 未知のリスト名やエラーは警告ログを出してスキップし、残りの構築は続く
- **メモリに注意**: DBは大きい。`sekitsui` 1本で約6.6GB(172万バリエーション)を保持する。
  キャッシュはバリエーション総数200万(約8GB)を上限にLRUで捨てるので、大きいリストを
  複数指定しても最後の1本しか残らない。ウォームアップ対象は搭載メモリと相談して選ぶこと

各上限は `SORAMIMIC_PUBLIC` が有効なときだけ効く(`0` 以下を指定すると個別に無効化)。
上限に触れた投稿には日本語の理由文がそのままフォームに表示される。

## セッション分離

公開モードでは、初回アクセス時にサーバーが匿名セッションID(uuid4)を
HttpOnly cookie `sv_session`(有効期限30日)で発行する。ジョブにはこの
セッションIDが持ち主(owner)として記録され、

- `GET /api/jobs` は自分のジョブだけを返す
- `GET /api/jobs/{id}` / `GET /api/jobs/{id}/video` / `POST /api/jobs/{id}/cancel` は
  持ち主が違えば404(存在しないものとして扱う)

持ち主はジョブディレクトリの `status.json` にも保存されるので、サーバーを
再起動しても同じブラウザからは履歴が見える。非公開モードでは cookie を
発行せず、従来どおり全ジョブが誰からも見える。

ログインではなく cookie ベースなので、ブラウザを変えたり cookie を消したりすると
自分のジョブは見えなくなる(その場合も生成自体は動く)。

## クレジット表記

公開モードではフッターに歌声合成エンジンのクレジット(VOICEVOXは選択中の
キャラクター名を含む)を表示する。動画への焼き込みは行っていないので、
生成物を配布する場合は利用者側での表記が必要。
