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
`--priority-wordlist NAME`を複数指定すると、そのリスト群だけを先に完全同期してactive化し、
続けて残りの全リストを同期します。後半の同期中・失敗時も優先リストのactive manifestは
維持されます。systemd unitは公開カタログの5リストをこの方式で先行処理します。
`--download-workers`は1または2を指定でき、月次の長時間同期はCommonsの継続的な429を
避けるため1並列にしています。

定期同期には`deploy/systemd/soramimic-video-assets-sync.{service,timer}`を
`/etc/systemd/system/`へ設置し、`systemctl enable --now soramimic-video-assets-sync.timer`
を実行します。同期unitだけがstoreへ書き込み、dev/preview/public unitには同じ
`SORAMIMIC_VIDEO_ASSET_STORE`と`ReadOnlyPaths`を設定します。同期元をpreview/publicへ
切り替えず、全組み込みCSVを含むdevの固定releaseを使うことで3環境の共有内容を一意にします。
`/etc/soramimic-video/{dev,preview,public}.env`にはそれぞれ次の同じ値を追加します。

```sh
SORAMIMIC_VIDEO_ASSET_STORE=/var/lib/soramimic-video-assets
```

### repositoryへ含めない曲プリセット

権利確認済みでもMIDI・歌詞を公開repositoryで再配布しない曲は、環境ごとの
`/var/lib/soramimic-video-<environment>/work/private-samples` に置きます。releaseを
切り替えても残り、別環境とは共有しません。`samples.json`には同梱PD曲と同じmanifest、
`samples.local.json`には外部曲だけを置き、素材ごとの出所・利用条件・取得日・必要な
クレジットを運用記録に残してください。

Simple UIでは選択肢も環境外へ分離します。同梱`launch_catalog.json`を
`private-samples/launch_catalog.json`へコピーし、その環境で確認済みのIDだけを
`samples`へ追加して、env fileに次を設定します。

```sh
SORAMIMIC_SAMPLES_DIR=/var/lib/soramimic-video-preview/work/private-samples
SORAMIMIC_LAUNCH_CATALOG=/var/lib/soramimic-video-preview/work/private-samples/launch_catalog.json
```

`SORAMIMIC_SAMPLES_DIR`はディレクトリを丸ごと差し替えるため、PD曲のmanifest・MIDI・
歌詞も同じディレクトリへコピーします。配置後はservice userからread可能であること、
`/api/samples`が意図したIDだけを返すこと、各MIDI・歌詞の取得と代表ジョブの生成が成功
することを確認します。previewで確認した素材をpublicへ移すときも、public専用ディレクトリ
へ別途配置し、`public.env`を明示的に変更します。previewの設定だけで本番に露出することは
ありません。

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

`main`も同じ入口を使用でき、`main`はpublic環境（port 8301、
`/opt/soramimic-video-public`）へ固定で対応します。

Tunnel ingressの正本例は`deploy/cloudflared/config.yml.example`です。Accessで両hostnameが
認証要求（未認証HTTP 302）になることを確認してから、ingressを有効化します。

## ブランチ先端の自動デプロイ

ホストの`systemd` timerが1分ごとにGitHubの3ブランチを確認し、先端SHAが変わった環境だけを
`prepare → verify → activate`します。

| branch | environment | URL |
| --- | --- | --- |
| `dev` | dev | `dev-video.soramimic.com` |
| `preview` | preview | `preview-video.soramimic.com` |
| `main` | public | `video.soramimic.com` |

GitHub Actionsのself-hosted runnerは使用しません。ブランチ上で変更できるworkflowやcheckoutを
ホスト上で実行すると、特にdevブランチのコードがホスト権限を得るためです。代わりに、監査した
デプロイコントローラーとエンジンをroot所有の`/usr/local`へ固定して実行します。アプリの
checkout・依存導入・ビルドは環境別の非rootユーザーで行われます。

初回導入は、CIを通過してdevへマージされた監査済みcheckoutで次を実行します。

```sh
SHA=$(git rev-parse HEAD)
sudo deploy/install-auto-deploy.sh --confirm "$SHA"
systemctl status soramimic-video-auto-deploy.timer
journalctl -u soramimic-video-auto-deploy.service
```

installerは次も同時に行います。

- `soramimic-video-dev`、`soramimic-video-preview`、`soramimic-video-public`へservice UIDを分離
- 既存stateの所有者を対応UIDへ移行
- 共有の画像asset・NEUTRINOを読むためだけに従来の`soramimic-video` groupを補助groupとして維持
- root所有の固定controller/deploy engineとtimer unitを配置

固定controller/deploy engineは意図的に自動更新しません。デプロイ権限を持つコードの変更は、
該当PRのCI・レビュー完了後に、このinstallerを監査済みcheckoutから再実行して反映します。

安全上の約束は以下です。

- controllerが受け入れるbranchと環境の対応は上表の3通りだけ
- 各branchの先端SHAに対するGitHubの`test` check成功後だけbuild・activate。devのauto-mergeは
  GitHub Actions `test`を含むPRの全check成功と、検証済みmerge-refと実mergeのtree/parents一致を
  確認してから、その検証結果をmerge SHAの`test` checkとして記録
- 40桁SHA、remote branch head、現在版からのfast-forwardを検証
- build中にbranchが進んだ場合は古いSHAをactivateせず、次回timerで最新を処理
- manifest・staging PID/port・health/readiness・公開surfaceを検証後にatomic切替
- activate後の検証失敗時は既存のprevious releaseへ自動復帰
- 失敗SHAは`/var/lib/soramimic-video-deployer/failed/`へ隔離し、毎分の再起動ループを防止
- `preview`と`main`へのマージ承認は従来どおり。timerはマージ後の配信だけを自動化

timerを止める場合は次を使用します。停止中も現在releaseは動き続けます。

```sh
sudo systemctl disable --now soramimic-video-auto-deploy.timer
```

修正を加えずに同じSHAを再試行するときは、原因を直してから該当markerを明示的に削除します。
手動rollbackを維持するときは、先にtimerを停止してください。
