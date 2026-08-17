"""soramimic-video CLI。

各サブコマンドがパイプラインの1ステージ(DESIGN.md参照)。
プロジェクトディレクトリの project.json を介して受け渡しする。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .project import Project


def cmd_edit_timing(args: argparse.Namespace) -> int:
    from .timing_editor import serve

    serve(
        Path(args.project),
        host=args.host,
        port=args.port,
        audio=Path(args.audio) if args.audio else None,
        reference_midi=Path(args.reference_midi) if args.reference_midi else None,
        options={
            "synthesizer": args.synthesizer,
            "model": args.model,
            "soundfont": args.soundfont,
            "engine_url": args.voicevox_url,
            "style_id": args.voicevox_style,
            "transpose": args.transpose,
        },
    )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from .align import align_lines
    from .xfparse import analyze_midi

    project = analyze_midi(Path(args.midi))
    if args.lyrics:
        lyric_lines = Path(args.lyrics).read_text(encoding="utf-8").splitlines()
        align_lines(project, lyric_lines)
    path = project.save(Path(args.project))
    matched = sum(1 for ln in project.lines if ln.original_text)
    print(f"解析完了: {len(project.notes)}モーラ / {len(project.lines)}行 -> {path}")
    if args.lyrics:
        print(f"元歌詞アライメント: {matched}/{len(project.lines)}行が対応")
    return 0


def cmd_validate_samples(args: argparse.Namespace) -> int:
    from .sample_validation import validate_sample_directory

    results = validate_sample_directory(Path(args.samples_dir), local_only=args.local_only)
    for result in results:
        print(
            f"{result.sample_id}: {result.notes}モーラ / {result.lines}行 / "
            f"元歌詞 {result.matched_lines}/{result.lines}行対応"
        )
    print(f"サンプル検査完了: {len(results)}曲")
    return 0


def cmd_analyze_audio(args: argparse.Namespace) -> int:
    from .analyze_audio import ANALYZE_DIR, analyze_audio

    project = analyze_audio(
        Path(args.audio),
        Path(args.project),
        lyrics_path=Path(args.lyrics) if args.lyrics else None,
        melody_midi=Path(args.melody_midi) if args.melody_midi else None,
        melody_channel=args.melody_channel,
        bpm=args.bpm,
        whisper_model=args.whisper_model,
        skip_separation=args.no_separation,
        device=args.device,
    )
    path = project.save(Path(args.project))
    print(f"解析完了: {len(project.notes)}モーラ / {len(project.lines)}行 -> {path}")
    if not args.lyrics:
        print("元歌詞にWhisper認識結果を使用しました(誤認識は edit ステージで修正可能)")
    print(f"タイミングの目視検証用SRT: {Path(args.project) / ANALYZE_DIR}/")
    return 0


def cmd_eval_audio(args: argparse.Namespace) -> int:
    from .evaluate import evaluate

    truth = Project.load(Path(args.truth))
    est = Project.load(Path(args.project))
    print(evaluate(truth, est).summary())
    return 0


def cmd_analyze_midi(args: argparse.Namespace) -> int:
    from .midi_project import build_from_melody_midi

    lyrics = None
    if args.lyrics:
        lyrics = Path(args.lyrics).read_text(encoding="utf-8")
    project = build_from_melody_midi(
        Path(args.midi),
        Path(args.project),
        lyrics=lyrics,
        channel=args.melody_channel,
        render_backing=not args.no_backing,
        soundfont=args.soundfont,
    )
    path = project.save(Path(args.project))
    print(f"解析完了: {len(project.notes)}モーラ / {len(project.lines)}行 -> {path}")
    if not args.lyrics:
        print("ベース歌詞なし(ラで充填)。--lyrics で生成歌詞を渡すと空耳の元になります")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    from .convert import convert_project
    from .editor_io import save_raw

    project = Project.load(Path(args.project))
    raw = convert_project(
        project,
        wordlist=args.wordlist,
        where=args.where,
        params=dict(kv.split("=", 1) for kv in args.param or []),
    )
    save_raw(raw, Path(args.project))
    project.save(Path(args.project))
    n_words = sum(len(pl.words) for pl in project.parody.lines) if project.parody else 0
    print(f"変換完了: {n_words}単語 -> {Path(args.project) / 'project.json'}")
    return 0


def cmd_export_edit(args: argparse.Namespace) -> int:
    from .editing import export_edit

    project = Project.load(Path(args.project))
    path = export_edit(project, Path(args.project))
    print(f"編集用ファイルを書き出しました: {path}")
    print("surface / kana を編集して import-edit で取り込んでください")
    return 0


def cmd_import_edit(args: argparse.Namespace) -> int:
    from .editing import import_edit

    project = Project.load(Path(args.project))
    import_edit(project, Path(args.project))
    project.save(Path(args.project))
    print("編集内容を取り込みました")
    return 0


def cmd_export_editor(args: argparse.Namespace) -> int:
    from .editor_io import export_editor

    project = Project.load(Path(args.project))
    path = export_editor(project, Path(args.project))
    print(f"editor用ファイルを書き出しました: {path}")
    print("soramimic編集ツールの「読み込み」で開き、編集後に「書き出し」たファイルを")
    print(f"  soramimic-video import-editor --project {args.project} --file <書き出したJSON>")
    print("で取り込んでください")
    return 0


def cmd_import_editor(args: argparse.Namespace) -> int:
    from .editor_io import import_editor

    project = Project.load(Path(args.project))
    import_editor(project, Path(args.project), Path(args.file) if args.file else None)
    project.save(Path(args.project))
    n_words = sum(len(pl.words) for pl in project.parody.lines) if project.parody else 0
    print(f"editorの編集内容を取り込みました({n_words}単語)")
    return 0


def cmd_synthesize(args: argparse.Namespace) -> int:
    from .synthesize import synthesize

    project = Project.load(Path(args.project))
    wav = synthesize(
        project,
        Path(args.project),
        model=args.model,
        transpose=args.transpose,
        dry_run=args.dry_run,
        synthesizer=args.synthesizer,
        voicevox_url=args.voicevox_url,
        voicevox_style=args.voicevox_style,
        # 新名 --no-auto-octave を優先。旧名 --no-voicevox-auto-octave も後方互換で受ける
        # (どちらも「無効化」なので、いずれか指定されていればOFF)。
        auto_octave=not (args.no_auto_octave or args.no_voicevox_auto_octave),
    )
    # 自動調整が決めたキー変更(song.key_shift)を残す。次のmixが伴奏を同じだけ移調する
    project.save(Path(args.project))
    if project.song.key_shift:
        print(f"曲全体を{project.song.key_shift:+d}半音キー変更しました(伴奏も同じだけ移調されます)")
    if wav:
        print(f"歌唱音源: {wav}")
    return 0


def cmd_mix(args: argparse.Namespace) -> int:
    from .mix import mix

    project = Project.load(Path(args.project))
    out = mix(project, Path(args.project), soundfont=args.soundfont)
    print(f"ミックス完了: {out}")
    return 0


def cmd_video(args: argparse.Namespace) -> int:
    from .video import make_video

    project = Project.load(Path(args.project))
    out = make_video(
        project,
        Path(args.project),
        width=args.width,
        height=args.height,
        font=args.font,
        audio=args.audio,
        image_cache=Path(args.image_cache) if args.image_cache else None,
        layout=args.layout,
        synth_credit=args.synth_credit,
        fps=args.fps,
        song_title=args.song_title,
        song_title_kana=args.song_title_kana,
        original_credit=args.original_credit,
        credit_notice=args.credit_notice,
        image_lead_sec=args.image_lead_sec,
        allow_noncommercial_fanwork=args.noncommercial_fanwork,
    )
    print(f"動画完成: {out}")
    return 0


def cmd_prewarm_images(args: argparse.Namespace) -> int:
    import os

    from .prewarm import prewarm_images

    if args.cache_dir:
        cache_dir = Path(args.cache_dir)
    else:
        # 既定はAPIの共有画像キャッシュ(work/api-jobs/image-cache)に揃える。
        # SORAMIMIC_VIDEO_IMAGE_CACHE が設定されていればそちらを優先(video側と同じ挙動)
        cache_dir = Path(
            os.environ.get("SORAMIMIC_VIDEO_IMAGE_CACHE") or "work/api-jobs/image-cache"
        )
    summary = prewarm_images(
        [Path(p) for p in args.csv],
        cache_dir,
        delay=args.delay,
        revalidate=args.revalidate,
        allow_noncommercial_fanwork=args.noncommercial_fanwork,
    )
    print(
        f"prewarm完了: 取得 {summary['fetched']} / 更新確認 {summary['revalidated']} / "
        f"スキップ {summary['skipped']} / "
        f"失敗 {summary['failed']} (URL計 {summary['total']}) -> {cache_dir}"
    )
    return 0


def cmd_sync_assets(args: argparse.Namespace) -> int:
    import os

    from .asset_store import ASSET_STORE_ENV
    from .prewarm import sync_asset_store, wordlist_csv_paths

    store_value = args.asset_store or os.environ.get(ASSET_STORE_ENV, "")
    if not store_value:
        print(f"--asset-store または {ASSET_STORE_ENV} が必要です", file=sys.stderr)
        return 2
    wordlists = Path(args.wordlists_dir)
    csv_paths = wordlist_csv_paths(wordlists)
    if not csv_paths:
        print(f"単語リストCSVがありません: {wordlists}", file=sys.stderr)
        return 2
    priority_paths: list[Path] = []
    for name in args.priority_wordlist:
        if Path(name).name != name or not name:
            print(f"不正な優先単語リスト名です: {name}", file=sys.stderr)
            return 2
        path = wordlists / f"{name}.csv"
        if not path.is_file():
            print(f"優先単語リストCSVがありません: {path}", file=sys.stderr)
            return 2
        priority_paths.append(path)
    if priority_paths:
        priority_set = set(priority_paths)
        csv_paths = priority_paths + [path for path in csv_paths if path not in priority_set]
    mode = "full" if args.revalidate else args.mode
    try:
        summary = sync_asset_store(
            csv_paths, Path(store_value), wordlists_dir=wordlists,
            mode=mode, dry_run=args.dry_run,
            download_workers=args.download_workers,
            source_manifest_url=args.source_manifest_url,
            allow_noncommercial_fanwork=args.noncommercial_fanwork,
        )
    except (OSError, ValueError, RuntimeError) as e:
        print(f"asset sync失敗(last-goodを維持): {e}", file=sys.stderr)
        return 1
    prefix = "dry-run" if args.dry_run else "asset sync完了"
    print(
        f"{prefix}: 新規 {summary['new']} / 更新 {summary['updated']} / "
        f"変更なし {summary['unchanged']} / 失敗 {summary['failed']} / "
        f"クレジット取得失敗 {summary.get('credit_failed', 0)} / "
        f"クレジット不明 {summary['credit_unknown']} / "
        f"削除候補 {summary['orphaned']} / "
        f"active昇格 {bool(summary.get('promoted', 0))} "
        f"(URL計 {summary['total']}) -> {store_value}"
    )
    return 1 if (
        summary["failed"]
        or summary.get("credit_failed", 0)
        or summary["credit_unknown"]
        or (not args.dry_run and not summary.get("promoted", 0))
    ) else 0


def cmd_asset_status(args: argparse.Namespace) -> int:
    import os

    from .asset_store import ASSET_STORE_ENV
    from .prewarm import asset_store_status

    store_value = args.asset_store or os.environ.get(ASSET_STORE_ENV, "")
    if not store_value:
        print(f"--asset-store または {ASSET_STORE_ENV} が必要です", file=sys.stderr)
        return 2
    status = asset_store_status(Path(store_value))
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    healthy = (
        status["manifest_version"] == 1
        and status["active"]
        and not status["failed"]
        and not status["credit_unknown"]
        and not status["missing_files"]
        and not status["pending"]
    )
    return 0 if healthy else 1


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .api import API_KEY_ENV, create_app
    except ImportError:
        print(
            "APIサーバーの依存が足りません。`pip install -e '.[api]'` で入れてください",
            file=sys.stderr,
        )
        return 1
    import os

    from .asset_store import ASSET_STORE_ENV, load_manifest

    store_value = args.asset_store or os.environ.get(ASSET_STORE_ENV, "").strip()
    asset_store_summary = "asset storeなし"
    if store_value:
        store = Path(store_value).resolve()
        manifest = store / "manifest.json"
        if not manifest.is_file():
            print(f"asset store manifestがありません: {manifest}", file=sys.stderr)
            return 2
        manifest_data = load_manifest(store)
        assets = manifest_data.get("assets")
        if manifest_data.get("version") != 1 or not isinstance(assets, dict) or not assets:
            print(f"asset store manifestが有効ではありません: {manifest}", file=sys.stderr)
            return 2
        os.environ[ASSET_STORE_ENV] = str(store)
        asset_store_summary = f"asset store {len(assets):,}件"

    app = create_app(
        jobs_dir=Path(args.jobs_dir),
        soundfont=args.soundfont,
        font=args.font,
        threads=args.threads,
        layout=args.layout,
        editor_dist=Path(args.editor_dist) if args.editor_dist else None,
        voicevox_url=args.voicevox_url,
        video_fps=args.video_fps,
        video_image_lead_sec=args.video_image_lead_sec,
        parallel_video=not args.serial_video,
    )
    auth = "APIキー認証あり" if os.environ.get(API_KEY_ENV) else f"認証なし({API_KEY_ENV}で有効化)"
    print(f"http://{args.host}:{args.port}/ で待ち受けます({auth}, {asset_store_summary})")
    # Access exemption decisions must see the actual socket peer. Do not let
    # uvicorn rewrite request.client from user-controlled forwarding headers.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="soramimic-video", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="XF MIDIを解析し元歌詞とアライメントする")
    p.add_argument("--midi", required=True, help="XF MIDIファイル")
    p.add_argument("--lyrics", help="元歌詞テキスト(1行1フレーズ)")
    p.add_argument("--project", required=True, help="プロジェクトディレクトリ")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser(
        "validate-samples", help="配置済みサンプルのXF歌詞と元歌詞を一括検査する"
    )
    p.add_argument("--samples-dir", required=True, help="samples.jsonを含むディレクトリ")
    p.add_argument(
        "--local-only",
        action="store_true",
        help="samples.local.jsonの追加曲だけを検査する",
    )
    p.set_defaults(func=cmd_validate_samples)

    p = sub.add_parser(
        "analyze-audio", help="歌唱音源(wav/mp3)を解析しモーラタイミングを抽出する"
    )
    p.add_argument("--audio", required=True, help="歌唱入り音源ファイル(wav/mp3)")
    p.add_argument(
        "--lyrics",
        help="元歌詞テキスト(1行1フレーズ)。省略時はWhisperの認識結果を元歌詞にする",
    )
    p.add_argument("--project", required=True, help="プロジェクトディレクトリ")
    p.add_argument(
        "--melody-midi",
        help="メロディ入りMIDI(非XFでよい)。あればピッチ・タイミングを楽譜に寄せる",
    )
    p.add_argument(
        "--melody-channel",
        type=int,
        help="メロディのMIDIチャンネル(0始まり)。省略時は自動選択",
    )
    p.add_argument("--bpm", type=float, default=120.0, help="tick換算用の固定BPM")
    p.add_argument(
        "--whisper-model",
        default="large-v3",
        help="歌詞認識用Whisperモデル(faster-whisper)。--lyrics指定時は未使用",
    )
    p.add_argument(
        "--no-separation",
        action="store_true",
        help="音源分離をスキップ(入力が既にボーカルのみの場合)",
    )
    p.add_argument("--device", help="torchデバイス(省略時はcuda→cpuの順で自動)")
    p.set_defaults(func=cmd_analyze_audio)

    p = sub.add_parser(
        "eval-audio", help="analyze-audioの出力をXF正解プロジェクトと突き合わせて評価する"
    )
    p.add_argument("--project", required=True, help="評価対象(analyze-audioの出力)")
    p.add_argument("--truth", required=True, help="正解(XF MIDI由来のプロジェクト)")
    p.set_defaults(func=cmd_eval_audio)

    p = sub.add_parser(
        "analyze-midi",
        help="生成メロディMIDI(音源なし)から器を作る(著作権フリー替え歌用)",
    )
    p.add_argument("--midi", required=True, help="単旋律メロディMIDI(ChatMusician等の生成物)")
    p.add_argument("--lyrics", help="ベース歌詞(空耳変換の元。省略時はラで充填)")
    p.add_argument("--project", required=True, help="プロジェクトディレクトリ")
    p.add_argument("--melody-channel", type=int, help="メロディのMIDIチャンネル。省略時は自動")
    p.add_argument("--no-backing", action="store_true", help="伴奏レンダリングをしない")
    p.add_argument("--soundfont", help="伴奏レンダリング用サウンドフォント(.sf2)")
    p.set_defaults(func=cmd_analyze_midi)

    p = sub.add_parser("convert", help="soramimicで替え歌単語歌詞に変換する")
    p.add_argument("--project", required=True)
    p.add_argument("--wordlist", required=True, help="単語リスト名(例: stations)またはCSVパス")
    p.add_argument("--where", help="単語リストの絞り込み(例: 'status=current')")
    p.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="soramimicパラメータ(例: --param LENGTH=2)",
    )
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("export-edit", help="人手編集用ファイルを書き出す")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_export_edit)

    p = sub.add_parser("import-edit", help="編集済みファイルを取り込む")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_import_edit)

    p = sub.add_parser(
        "export-editor", help="soramimic編集ツールで開けるJSONを書き出す"
    )
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_export_editor)

    p = sub.add_parser(
        "import-editor", help="soramimic編集ツールが書き出したJSONを取り込む"
    )
    p.add_argument("--project", required=True)
    p.add_argument("--file", help="editorが書き出したJSON(省略時は editor.json)")
    p.set_defaults(func=cmd_import_editor)

    p = sub.add_parser(
        "edit-timing", help="モーラのタイミングをピアノロールGUIで手直しする"
    )
    p.add_argument("--project", required=True)
    p.add_argument(
        "--port", type=int, default=8765, help="GUIを配信するポート(既定: 8765)"
    )
    p.add_argument(
        "--host", default="127.0.0.1",
        help="待ち受けアドレス(既定: 127.0.0.1。LANの別端末から開くなら 0.0.0.0)",
    )
    p.add_argument(
        "--audio", help="重ねて聴く音源(既定: project.jsonのvocals_path/audio_path)"
    )
    p.add_argument(
        "--reference-midi", help="背景に薄く表示する参照メロディMIDI(既定: 編集前の音符)"
    )
    # 「🎤この行」「🔄合成」で使う合成設定(synthesize/mixと同じ意味)
    p.add_argument("--synthesizer", default="voicevox", choices=["voicevox", "neutrino"])
    p.add_argument("--model", default="MERROW", help="NEUTRINOの歌声モデル名")
    p.add_argument("--soundfont", help="伴奏レンダリング用のsf2(MIDI入力のプロジェクト)")
    p.add_argument("--voicevox-url", help="VOICEVOXエンジンのURL")
    p.add_argument("--voicevox-style", type=int, default=3003, help="VOICEVOXのスタイルID")
    p.add_argument("--transpose", type=int, default=0, help="移調(半音)")
    p.set_defaults(func=cmd_edit_timing)

    p = sub.add_parser("synthesize", help="替え歌を歌唱合成する(NEUTRINO/VOICEVOX)")
    p.add_argument("--project", required=True)
    p.add_argument("--model", default="MERROW", help="NEUTRINOの歌声モデル名")
    p.add_argument(
        "--synthesizer",
        choices=["neutrino", "voicevox"],
        default="neutrino",
        help="合成エンジン(既定: neutrino)",
    )
    p.add_argument(
        "--voicevox-url",
        default="http://127.0.0.1:50021",
        help="VOICEVOXエンジンのURL",
    )
    p.add_argument(
        "--voicevox-style",
        type=int,
        default=3003,
        help="VOICEVOXのスタイルID(例: ずんだもんノーマル=3003、波音リツ歌唱=6000)",
    )
    p.add_argument(
        "--no-auto-octave",
        action="store_true",
        help="エンジンの音域に合わせた自動オクターブ調整を無効にする"
        "(VOICEVOX/NEUTRINO共通)",
    )
    p.add_argument(
        # 旧名。--no-auto-octave に統合したが後方互換で受け続ける(deprecated)
        "--no-voicevox-auto-octave",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--transpose", type=int, default=0, help="半音単位の移調(-12で1オクターブ下)"
    )
    p.add_argument("--dry-run", action="store_true", help="コマンドを表示するだけ")
    p.set_defaults(func=cmd_synthesize)

    p = sub.add_parser("mix", help="伴奏と歌唱をミックスする")
    p.add_argument("--project", required=True)
    p.add_argument("--soundfont", help="伴奏レンダリング用サウンドフォント(.sf2)")
    p.set_defaults(func=cmd_mix)

    p = sub.add_parser("video", help="替え歌動画を生成する")
    p.add_argument("--project", required=True)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30, help="動画のフレームレート(既定: 30)")
    p.add_argument(
        "--image-lead-sec", type=float, default=0.1,
        help="カードを音声より先に表示する秒数(既定: 0.1、無効化: 0)",
    )
    p.add_argument(
        "--noncommercial-fanwork",
        action="store_true",
        help="非営利ファン活動に限定された単語画像を利用する",
    )
    p.add_argument("--font", default="Hiragino Sans", help="字幕フォント名")
    p.add_argument("--audio", help="音声ファイル(省略時は mix/song.wav か neutrino/vocal.wav)")
    p.add_argument(
        "--image-cache",
        help="単語画像の共有キャッシュ(環境変数 SORAMIMIC_VIDEO_IMAGE_CACHE でも指定可)",
    )
    p.add_argument(
        "--layout",
        help="フレームレイアウト。組み込み名(default/caption)またはJSONファイルパス"
        "(書き方は examples/layouts/ と layout.py 冒頭を参照)",
    )
    p.add_argument(
        "--synth-credit",
        default="",
        help="歌声合成のクレジット表記(例 'VOICEVOX:四国めたん')。"
        "指定するとフレーム左下の署名に「lyrics & video by Soramimic / ...」と併記する",
    )
    p.add_argument(
        "--song-title",
        default="",
        help="末尾クレジットに出す元曲名(省略時はMIDIファイル名)",
    )
    p.add_argument(
        "--song-title-kana",
        default="",
        help="サムネの曲名変換に使う読み(カタカナ)",
    )
    p.add_argument(
        "--original-credit",
        default="",
        help="元曲の著作者クレジット(例: '作詞: ○○ / 作曲: △△')",
    )
    p.add_argument(
        "--credit-notice",
        default="",
        help="権利者やライセンスから指定された表記(改変せず末尾に表示)",
    )
    p.set_defaults(func=cmd_video)

    p = sub.add_parser(
        "prewarm-images",
        help="単語リストCSVの画像を画像キャッシュへ事前ダウンロードする",
    )
    p.add_argument("csv", nargs="+", help="単語リストCSV(複数可)。image列のhttp(s) URLが対象")
    p.add_argument(
        "--cache-dir",
        help="画像キャッシュの場所(既定は SORAMIMIC_VIDEO_IMAGE_CACHE か "
        "work/api-jobs/image-cache)",
    )
    p.add_argument(
        "--delay", type=float, default=1.0, help="リクエスト間の待機秒(既定1.0)"
    )
    p.add_argument(
        "--revalidate",
        action="store_true",
        help="キャッシュ済み画像もETag/Last-Modifiedで更新確認する",
    )
    p.add_argument(
        "--noncommercial-fanwork",
        action="store_true",
        help="非営利ファン活動に限定された画像も事前取得する",
    )
    p.set_defaults(func=cmd_prewarm_images)

    p = sub.add_parser(
        "sync-assets",
        help="全組み込み単語リストの画像とクレジットを永続asset storeへ同期する",
    )
    p.add_argument(
        "--wordlists-dir", default="external/soramimic-wordlists",
        help="組み込み単語リストのディレクトリ(既定: external/soramimic-wordlists)",
    )
    p.add_argument(
        "--asset-store",
        help="共有asset store (環境変数 SORAMIMIC_VIDEO_ASSET_STORE でも指定可)",
    )
    p.add_argument(
        "--revalidate", action="store_true",
        help="--mode full の互換alias",
    )
    p.add_argument(
        "--mode", choices=("manifest", "full"), default="manifest",
        help="manifest=Release差分同期、full=全URL再検証(既定: manifest)",
    )
    p.add_argument(
        "--source-manifest-url",
        default=(
            "https://github.com/soramimic/soramimic-wordlists/releases/download/"
            "release-image-source-manifest-v1/source-manifest.json"
        ),
        help="wordlists Release画像source manifest URL",
    )
    p.add_argument(
        "--priority-wordlist", action="append", default=[], metavar="NAME",
        help="単一transaction内で先に取得するリスト(複数指定可)",
    )
    p.add_argument(
        "--download-workers", type=int, choices=range(1, 3), default=2,
        help="画像取得の並列数(1または2、既定2。長時間のCommons一括取得は1を推奨)",
    )
    p.add_argument("--dry-run", action="store_true", help="取得せず差分件数だけ表示する")
    p.add_argument(
        "--noncommercial-fanwork",
        action="store_true",
        help="非営利ファン活動に限定された画像も同期する",
    )
    p.set_defaults(func=cmd_sync_assets)

    p = sub.add_parser("asset-status", help="永続asset storeのmanifest集計を表示する")
    p.add_argument(
        "--asset-store",
        help="共有asset store (環境変数 SORAMIMIC_VIDEO_ASSET_STORE でも指定可)",
    )
    p.set_defaults(func=cmd_asset_status)

    p = sub.add_parser("serve", help="動画生成APIサーバー(+Web UI)を起動する")
    p.add_argument("--host", default="127.0.0.1", help="LANに公開するなら 0.0.0.0")
    p.add_argument("--port", type=int, default=8300)
    p.add_argument("--jobs-dir", default="work/api-jobs", help="ジョブの作業ディレクトリ")
    p.add_argument("--soundfont", help="伴奏用サウンドフォント(.sf2)")
    p.add_argument("--font", help="字幕フォント名(既定はOSに応じて選択)")
    p.add_argument("--threads", type=int, default=4, help="NEUTRINOのスレッド数")
    p.add_argument("--video-fps", type=int, default=30, help="生成動画のfps(既定: 30)")
    p.add_argument(
        "--video-image-lead-sec", type=float, default=0.1,
        help="生成カードを音声より先に表示する秒数(既定: 0.1、無効化: 0)",
    )
    p.add_argument(
        "--serial-video", action="store_true",
        help="歌声・ミックス完了後に従来の1パス動画生成を行う(並列化の無効化)",
    )
    p.add_argument(
        "--voicevox-url",
        default="http://127.0.0.1:50021",
        help="VOICEVOXエンジンのURL(合成エンジンにVOICEVOXを選んだとき使う)",
    )
    p.add_argument("--layout", help="フレームレイアウト(組み込み名かJSONパス)")
    p.add_argument(
        "--asset-store",
        help="組み込み単語画像を読むasset store。指定時はmanifestが無ければ起動しない",
    )
    p.add_argument(
        "--editor-dist",
        help="同梱editorの静的ビルド出力(既定は external/soramimic/frontend/dist)。"
        "scripts/build-editor.sh で生成する",
    )
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
