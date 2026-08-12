# ADR 00002: 曲プリセットの権利と配布場所を分離する

- Status: accepted
- Date: 2026-08-12
- Supersedes: none
- Superseded by: none

## Context

曲プリセットには、楽曲、歌詞、編曲・打込みMIDIという別々の権利がある。楽曲本体の
利用が許可されていても、第三者のMIDIを再配布できるとは限らない。また、公開Git
repositoryへ収録しないだけでは十分ではない。現在のWeb UIはプリセットMIDIをブラウザへ
返すため、Web環境で有効にすること自体がMIDIの配布になる。

過去には権利曲を公開manifestから外し、`samples.local.json`と
`SORAMIMIC_SAMPLES_DIR`を導入した。しかし、素材の判定基準とdev・preview・publicの
昇格方法が一か所に記録されていなかった。

## Decision

プリセット素材を次の4区分で扱う。

1. 楽曲・歌詞がPDで、MIDIも自作または再配布可能: 公開repositoryへ同梱できる。
   `docs/sample-rights.md`へ根拠とMIDIの作成方法を記録する。
2. 楽曲がオープンライセンス等で、MIDI・歌詞も再配布可能: 公開repositoryへ同梱できる。
   各層の作者、出所、ライセンス、変更内容、必要な表示をmanifestまたはNOTICEへ記録する。
3. サービス内利用は許可されるが素材の再配布は許可されない: 現在のプリセットAPIでは
   公開しない。MIDIをクライアントへ返さずサーバー内だけで処理する方式を実装してから扱う。
4. いずれかの権利・出所が不明: プリセット化しない。利用者自身のアップロードに限る。

repositoryへ含めないが再配布可能な素材は、環境ごとのrelease外ディレクトリへ置く。
`SORAMIMIC_SAMPLES_DIR`で素材manifestを、`SORAMIMIC_LAUNCH_CATALOG`でSimple UIの
選択肢を指定する。dev・preview・publicはディレクトリとカタログを共有せず、各環境への
追加を独立した昇格判断にする。

外部素材の運用記録には最低限、曲名、楽曲・歌詞・MIDIそれぞれの作者とsource URL、
ライセンスまたは許諾条件、取得日、改変内容、必要クレジット、再配布可否を残す。

## Consequences

- PD曲は従来どおり再現可能な公開assetとして配布できる。
- 権利曲のバイナリや歌詞をGit履歴へ入れず、previewだけで先行確認できる。
- コードをmainへ昇格しても、public専用カタログと素材を配置しない限り外部曲は露出しない。
- 外部素材はGitだけでは復元できないため、運用側で出所記録、backup、checksum、配置確認が
  必要になる。
- 素材再配布不可の曲をプリセット化するには、サーバー内部参照型の追加設計が必要になる。
