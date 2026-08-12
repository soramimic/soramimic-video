# 公開版のデプロイ

公開版は `main` の明示commit SHAから再現可能なreleaseを作り、staging確認後に
`/opt/soramimic-video-public/current` を原子的に切り替えます。通常の開発は`dev`に集約し、
公開可能な変更だけを`preview`へ昇格して確認し、`preview -> main`のrelease PRをマージしてから
この手順を行います。

## 前提

- systemd system unit: `soramimic-video-public.service`
- 専用ユーザー/グループ: `soramimic-video`
- release: `/opt/soramimic-video-public/releases/<UTC timestamp>-<short SHA>`
- 永続データ: `/var/lib/soramimic-video-public/work`
- secrets/config: `/etc/soramimic-video/public.env`
- 必要コマンド: Git、uv、Node/npm、jq、curl、systemd-run

`deploy/systemd/soramimic-video-public.service` を `/etc/systemd/system/` に設置・更新した場合は、
先に `sudo systemctl daemon-reload` を実行します。このunit更新後はhostからの
`/readyz` 確認も有効になります。releaseディレクトリにsecretはコピーしません。

## リリース手順

GitHub上で`preview -> main`の差分と必須CIを確認してマージし、`main`の40桁SHAを控えます。
以下の3段階は意図的に分離されています。`prepare` と `verify` は本番の `current` を変更せず、
`activate` だけが本番を切り替えます。

```sh
SHA=<mainの40桁commit SHA>

# user home配下のuvは専用ユーザーから辿れないため、root所有の固定pathへ一度だけ配置
sudo install -o root -g root -m 0755 /home/jiro/.local/bin/uv /usr/local/bin/uv

# 実行内容とパスの事前確認（変更しない）
sudo SORAMIMIC_UV_BIN=/usr/local/bin/uv \
  deploy/deploy-public.sh --dry-run prepare "$SHA" --source "$PWD"

# origin/mainに含まれるSHAであることを検証し、submodule、uv.lock、editorを固定して構築
# private submoduleを使う場合は、operatorが認証済みのclean checkoutをsourceにする
git fetch origin main
git submodule update --init --recursive
sudo SORAMIMIC_UV_BIN=/usr/local/bin/uv \
  deploy/deploy-public.sh prepare "$SHA" --source "$PWD"

# localhost:18301で一時起動しhealth/readiness/UI/config/editorを確認
sudo deploy/deploy-public.sh verify "$SHA"

# 差分・メタデータ・staging結果を人が確認後、SHAを再入力して本番切替
sudo deploy/deploy-public.sh activate "$SHA" --confirm "$SHA"
```

従来方式で作った最初の `current` に `deploy-release.json` がない場合だけ、現在稼働中の
完全なcommit SHAを確認し、`activate` に `--previous-sha <40桁SHA>` を追加します。これで
初回切替後もその版へrollbackできます。旧releaseに `.git` または `REVISION` があれば
指定SHAと照合します。推測したSHAやshort SHAは使いません。

`activate` は `current` をatomic symlink置換してsystem unitを再起動し、`/healthz` と
`/readyz` を確認します。失敗時は直前のreleaseへ自動で戻して再起動します。
prepare/verify/activateの記録は `/opt/soramimic-video-public/deployments/*.json` に残ります。
全deploy/rollback操作は `/run/lock/soramimic-video-public-deploy.lock` で排他されます。

prepare後のreleaseは全ユーザーに対して書込不可です。ファイルmanifestとsymlink digestを
verify前とactivate前に再検証し、verifyしたartifactから1 byteでも変わっていれば切り替えません。
venvの絶対shebangが壊れないよう最終release path上で直接buildし、途中で失敗したreleaseは
削除します。buildとactivateは別コマンドなので、未完成releaseへ`current`が向くことはありません。
verifyはstaging portが事前に空であることに加え、transient unitのActiveState/MainPID、
MainPIDのworking directory、実際のlistener PIDが対象releaseと一致することを確認します。
surface smokeは `/api/config` に従い、通常UIではeditorの200、simple UIでは
`editor=false` とeditor URLの404（意図せず公開されていないこと）を確認します。

ポートやパスを変える場合は `SORAMIMIC_APP_ROOT`、`SORAMIMIC_STATE_ROOT`、
`SORAMIMIC_ENV_FILE`、`SORAMIMIC_STAGING_PORT`、`SORAMIMIC_LISTEN_PORT` 等を指定できます。
`SORAMIMIC_REPO_URL` は既定で公開GitHub repositoryです。
`SORAMIMIC_UV_BIN` は `sudo` の `secure_path` に依存しない絶対パスを指定します。この値だけを
専用ユーザーのビルドへ渡し、operatorの `PATH` 全体は引き継ぎません。operator home配下は
通常そのユーザー以外が辿れないため、上記のようにroot所有のsystem pathへコピーします。
`--source` はtracked fileだけを再帰的にarchiveし、GitHub credentialをreleaseや
service userへ渡しません。source本体と全submoduleがcommitどおりで、tracked変更がないことを
検査します。全repositoryが匿名clone可能な場合だけ `--source` を省略できます。

## 切替後確認

```sh
readlink -f /opt/soramimic-video-public/current
curl -fsS http://127.0.0.1:8301/healthz | jq .
curl -fsS http://127.0.0.1:8301/readyz | jq .
sudo systemctl status soramimic-video-public.service
sudo journalctl -u soramimic-video-public.service --since '-10 min'
```

Cloudflare側のAccess/WAF/Tunnel設定変更はこの手順に含みません。外部公開URLでもトップ、
ロゴ、editor、代表的な動画生成を確認し、問題がなければrelease tag/GitHub Releaseに
デプロイ済みSHAを記録します。

## ロールバック

最新activationの直前版へ戻す場合、まず表示されたtarget SHAを確認し、同じSHAを再入力します。

```sh
sudo deploy/rollback-public.sh release --confirm <targetの40桁SHA>
```

記録から自動判定できない場合や、さらに前のreleaseを指定する場合:

```sh
sudo deploy/rollback-public.sh release --to <40桁SHA> --confirm <同じ40桁SHA>
```

rollback先でもhealth/readinessが通らなければ、スクリプトは開始時のreleaseへ復帰します。
専用system unit導入そのものを戻して従来user unitへ戻す非常用手順だけは次を使います。

```sh
sudo SORAMIMIC_ROLLBACK_USER=<old-service-user> \
  deploy/rollback-public.sh legacy-user-unit
```

本番適用前にstaging環境で、activate失敗時の自動復帰と手動rollbackの両方を演習します。

## 共有画像・クレジットasset store

組み込み単語リストの全画像とCommonsクレジットは、release外の
`/var/lib/soramimic-video-assets`へ事前同期できます。API 3環境は同じmanifestを
read-onlyで参照するため、同期成功後の動画生成では組み込み画像について外部通信しません。
カスタム単語リストのURLはmanifestに無いので、従来どおりジョブ共有キャッシュへ取得します。

初回同期と確認:

```sh
sudo install -o soramimic-video -g soramimic-video -m 0750 -d /var/lib/soramimic-video-assets
sudo -u soramimic-video /opt/soramimic-video-dev/current/.venv/bin/soramimic-video \
  sync-assets --wordlists-dir /opt/soramimic-video-dev/current/external/soramimic-wordlists \
  --asset-store /var/lib/soramimic-video-assets
sudo -u soramimic-video /opt/soramimic-video-dev/current/.venv/bin/soramimic-video \
  asset-status --asset-store /var/lib/soramimic-video-assets
```

`--dry-run`は新規・更新候補・削除候補だけを表示し、`--revalidate`は既存URLを
ETag/Last-Modifiedで再検証します。失敗時は既存のlast-good画像を維持し、単語リストから
消えたURLも`orphaned_at`を付けるだけで削除しません。manifestは全同期完了後にatomicに
切り替わり、並行同期はlockで拒否されます。失敗を含む同期結果は
`manifest.pending.json`へ保存して次回再開し、正常な既存`manifest.json`は維持します。
初回同期が不完全ならactive manifestは作りません。`asset-status`はpendingの件数と失敗状態も
表示し、pending・未取得画像・不明クレジットがあれば終了コード1です。

定期同期には`deploy/systemd/soramimic-video-assets-sync.{service,timer}`を
`/etc/systemd/system/`へ設置し、`systemctl enable --now soramimic-video-assets-sync.timer`
を実行します。同期unitだけがstoreへ書き込み、dev/preview/public unitには同じ
`SORAMIMIC_VIDEO_ASSET_STORE`と`ReadOnlyPaths`を設定します。同期元をpreview/publicへ
切り替えず、全組み込みCSVを含むdevの固定releaseを使うことで3環境の共有内容を一意にします。
`/etc/soramimic-video/{dev,preview,public}.env`にはそれぞれ次の同じ値を追加します。

```sh
SORAMIMIC_VIDEO_ASSET_STORE=/var/lib/soramimic-video-assets
```

## dev・preview常設環境

本番とは別に、同じホストで次を常設します。どちらもloopbackだけで待ち受け、外部入口は
Cloudflare Tunnel + Accessに限定します。

| 環境    | branch    | URL                           | port | app root                       | unit                              |
| ------- | --------- | ----------------------------- | ---: | ------------------------------ | --------------------------------- |
| dev     | `dev`     | `dev-video.soramimic.com`     | 8311 | `/opt/soramimic-video-dev`     | `soramimic-video-dev.service`     |
| preview | `preview` | `preview-video.soramimic.com` | 8312 | `/opt/soramimic-video-preview` | `soramimic-video-preview.service` |

unitを配置し、`/etc/soramimic-video/dev.env`と`preview.env`を0600で用意します。previewは
本番相当設定、devは必要に応じて詳細UIを有効にします。ジョブ・release・deploy記録・lockは
環境ごとに分離します。

`deploy-environment.sh`がbranch、パス、port、unit、lockを環境ごとに固定して既存の
デプロイスクリプトを呼び出します。次はdevの例です（previewは第1引数を`preview`にします）。

```sh
SHA=$(git rev-parse origin/dev)
sudo deploy/deploy-environment.sh dev prepare "$SHA" --source "$PWD"
sudo deploy/deploy-environment.sh dev verify "$SHA"
sudo deploy/deploy-environment.sh dev activate "$SHA" --confirm "$SHA"
```

Tunnel ingressの正本例は`deploy/cloudflared/config.yml.example`です。Accessで両hostnameが
認証要求（未認証HTTP 302）になることを確認してから、ingressを有効化します。
