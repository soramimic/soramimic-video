# 公開サービス運用

`systemd/soramimic-video-public.service` は、専用OSユーザーで公開APIを
動かすためのsystem unit雛形です。実環境のreleaseパスとenv fileを準備し、
staging portで検証してから適用します。

`rollback-public.sh` は新しいsystem unitを停止し、従来のuser unitへ戻すための
ロールバック補助です。旧unitの実行ユーザーを必ず指定します。

```sh
sudo SORAMIMIC_ROLLBACK_USER=<old-service-user> deploy/rollback-public.sh
```

必要な場合は、次の環境変数で実環境に合わせます。

- `SORAMIMIC_NEW_UNIT`
- `SORAMIMIC_OLD_UNIT`
- `SORAMIMIC_LISTEN_PORT`
- `SORAMIMIC_HEALTH_URL`

本番適用前に、実際のstaging unitでロールバック手順を演習します。
