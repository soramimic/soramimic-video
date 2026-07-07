"""soramimic-video CLI。

各サブコマンドがパイプラインの1ステージ(DESIGN.md参照)。
プロジェクトディレクトリの project.json を介して受け渡しする。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .project import Project


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


def cmd_eval_audio(args: argparse.Namespace) -> int:
    from .evaluate import evaluate

    truth = Project.load(Path(args.truth))
    est = Project.load(Path(args.project))
    print(evaluate(truth, est).summary())
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
        dry_run=args.dry_run,
    )
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
    )
    print(f"動画完成: {out}")
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

    p = sub.add_parser(
        "eval-audio", help="analyze-audioの出力をXF正解プロジェクトと突き合わせて評価する"
    )
    p.add_argument("--project", required=True, help="評価対象(analyze-audioの出力)")
    p.add_argument("--truth", required=True, help="正解(XF MIDI由来のプロジェクト)")
    p.set_defaults(func=cmd_eval_audio)

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

    p = sub.add_parser("synthesize", help="NEUTRINOで替え歌を歌唱合成する")
    p.add_argument("--project", required=True)
    p.add_argument("--model", default="MERROW", help="NEUTRINOの歌声モデル名")
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
    p.add_argument("--font", default="Hiragino Sans", help="字幕フォント名")
    p.add_argument("--audio", help="音声ファイル(省略時は mix/song.wav か neutrino/vocal.wav)")
    p.set_defaults(func=cmd_video)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        # numba等の外部ライブラリのDEBUGは大量に出るので自パッケージだけ下げる
        logging.getLogger("soramimic_video").setLevel(logging.DEBUG)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
