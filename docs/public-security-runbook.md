# 一般公開前セキュリティrunbook

対象は Cloudflare Tunnel 配下の `video.soramimic.com`（origin `127.0.0.1:8301`）。
修正・検証中は Cloudflare Access のメール認証を維持する。ここにある手順は
Cloudflare設定を変更する許可を意味しない。

## アプリ境界

`SORAMIMIC_PUBLIC=1` では匿名セッションに加えて、HMAC化した接続元IPを
日次quotaと高コストGETのバックストップに使う。生IPはジョブ状態へ保存しない。

- 高コストGETのキャッシュミスはセッション枠と接続元IP枠の両方で制限する。
  既定IP枠はNATを考慮して60秒90回、画像取得は最大4並列、サムネ変換は1並列。
  キャッシュヒットは生成miss枠を消費せず、別の緩い120/600枠だけを使う。

関連設定:

| 変数 | 既定 | 用途 |
|---|---:|---|
| `SORAMIMIC_GET_RATE_LIMIT` | 15 | セッションあたりの画像系cache miss回数 |
| `SORAMIMIC_GET_RATE_WINDOW` | 60 | 上記の秒窓 |
| `SORAMIMIC_GET_IP_RATE_LIMIT` | 90 | IPバックストップの回数 |
| `SORAMIMIC_GET_IP_RATE_WINDOW` | 60 | 上記の秒窓 |
| `SORAMIMIC_GET_CACHE_HIT_RATE_LIMIT` | 120 | cache hitのセッション枠 |
| `SORAMIMIC_GET_CACHE_HIT_IP_RATE_LIMIT` | 600 | cache hitのIPバックストップ |
| `SORAMIMIC_IP_DAILY_QUOTA` | 30 | POST jobsの接続元IPごとの24時間枠 |
| `SORAMIMIC_IP_HASH_KEY` | 未設定 | IPを永続HMAC化するsecret（public readiness必須） |
| `SORAMIMIC_OPS_TOKEN` | 未設定 | proxy経由の運用endpoint専用トークン |
| `SORAMIMIC_TRUSTED_PROXY_IPS` | 未設定 | localhost以外の信頼proxy CIDR（カンマ区切り） |
| `SORAMIMIC_CF_ACCESS_TEAM_DOMAIN` | 未設定 | quota免除用の完全なHTTPS Access issuer URL（末尾`/`は1つまで可） |
| `SORAMIMIC_CF_ACCESS_AUD` | 未設定 | quota免除用Access audience |
| `SORAMIMIC_QUOTA_EXEMPT_EMAILS` | 未設定 | 検証済みメールallowlist（値はログや記録へ出さない） |
| `SORAMIMIC_ALLOW_LOCAL_OPS` | 未設定 | public版でも直接localhost運用アクセスを許可 |
| `SORAMIMIC_EXPOSE_OPS` | 未設定 | 明示的に運用endpointを無認証公開する非常用設定 |

`/healthz` は常に `{"status":"ok"}` のみを返し、セッションcookieを発行しない。
`/readyz`、`/metrics`、`/docs`、`/redoc`、`/openapi.json` は、private版の直接localhost、
信頼proxy経由かつ `X-Soramimic-Ops-Token` 一致、`SORAMIMIC_ALLOW_LOCAL_OPS=1` の
直接localhost、または `SORAMIMIC_EXPOSE_OPS=1` の場合だけ返す。public版では
cloudflaredもlocalhostに見えるためlocalhostを自動許可しない。未認可時は404にする。
トークンをquery stringやログへ入れない。proxy経由のSwagger/ReDocはブラウザの後続requestへ
トークンを安全に引き回せないため、通常は直接localhostでだけ使う。
`/readyz` の `checks` はjobs directory、永続IP hash設定、allowlist設定時のAccess設定を
booleanだけで返す。issuer、audience、メール、secretは返さない。Access認証済みでも運用endpoint
の認可にはならず、従来どおりops tokenが必要。

quota免除は、loopbackの直近peerが `SORAMIMIC_TRUSTED_PROXY_IPS` に明示され、Access JWTを
固定JWKS URL、RS256、完全一致issuer/audience、必須claimで検証でき、canonical emailがallowlist
に完全一致した場合だけ有効。`CF-Access-Authenticated-User-Email` は信頼しない。免除後も
Turnstile、queue、曲長、GET backstop、concurrency、owner分離、ops認可をsmoke
testする。アプリ起動はUvicornのproxy header解釈を無効化し、socket peerを保持する。

## 依存関係・native parser監査

2026-08-09のローカル監査:

- `uv lock --check`: 成功
- 公開API標準依存とdev依存: `uv audit --locked` で既知脆弱性なし
- audio extra: setuptools 81.0.0（修正版83.0.0）とtorch 2.12.1（修正版2.13.0）に
  advisoryあり。公開APIの標準依存ではないため、この変更では更新せず、audio互換試験後に更新する
- OS package: ffmpeg 6.1.1、FluidSynth 2.3.4、libcairo 1.18.0、libpng 1.6.43、
  libtiff 4.5.1。ローカルのinstalled/candidateは一致するが、ffmpeg
  `7:6.1.1-3ubuntu5` にはUbuntu Noble向け修正版がESM Appsでのみ提供されている既知問題がある。
  2026-05-28のUSN-8329-1は細工したCAFでDoSになり得る問題と`+esm8`を示している。
  2026-08-10にユーザーがUbuntu Pro attachの見送りを決定した。公開MIDIの元バイトは
  FFmpeg/ffprobeに渡されず、対象のCAF decoderへ到達しないため、本CVEはDoSの残存リスクとして
  記録し、通常版packageの更新を監視する。この例外はAccess解除の承認を意味しない。
  `apt-cache policy`のcandidate一致だけを「安全」と判定しない

MIDI/XF（xfmido/mido）、画像（Pillow）、SVG/XML（CairoSVG/defusedxml）、音声・動画
（ffmpeg）、soundfont（FluidSynth）をparser攻撃面として扱う。依存を更新する場合は、
壊れた/過大MIDI、画像形式偽装・画素数、
SVG外部参照、壊れた音声、VOICEVOX/ffmpeg/FluidSynthの最小E2Eを追加してから切り替える。

## Cloudflare公開前チェック（設定変更は別途承認）

- `video.soramimic.com` のみ一般公開候補。preview/staging hostnameはAccessを維持
- WAF managed rulesと、`POST /api/jobs` のIP/セッションrate limitを確認
- `/api/jobs`、Turnstile検証、運用endpoint、個人レスポンスがcache bypassであることを確認
- Security Analyticsで拒否/許可の想定を確認し、急増・5xx・queue飽和の通知先を確認
- Turnstile hostname allowlistがproduction hostnameを含み、検証用quota免除を維持することを確認
- Accessを外す前に上記アプリ試験・8301差分確認・ロールバック演習を完了する

Cloudflare dashboard/APIの設定値は、このチェックで発見事項を記録した後、明示承認を得て変更する。
