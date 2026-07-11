# examples

パイプラインを試すための同梱サンプル(すべてこのリポジトリのための自作データ)。

- `sample_song.mid` — 合成したXF形式のサンプル曲(16音符・2行)。
  メロディ・歌詞ともオリジナル(tests/helpers.py の合成ロジックで生成)
- `sample_editor.json` — 上の曲を駅名リストで変換した結果
  (soramimic編集ツールの書き出し形式)。`import-editor` の入力に使える
- `gen_furusato.py` — 童謡「故郷(ふるさと)」のXF MIDIを生成するスクリプト。
  詞(高野辰之)・曲(岡野貞一)ともパブリックドメインで、メロディは公知の楽譜の手打ち。
  出力は `src/soramimic_video/static/sample/` に置かれ、Web UIの
  「サンプル曲をセット」ボタンと `/api/sample/*` が配信する

```sh
uv run soramimic-video analyze --midi examples/sample_song.mid --project work/sample
uv run soramimic-video import-editor --project work/sample --file examples/sample_editor.json
# 以降 synthesize / mix / video へ
```
