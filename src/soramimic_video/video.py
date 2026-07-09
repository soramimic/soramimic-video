"""動画生成ステージ: 単語画像+字幕(替え歌/元歌詞)+歌唱音源 → out.mp4。

構成(ffmpeg 3パス):
 1. 単語リスト由来の画像をダウンロードし、レイアウト定義(layout.py)に従って
    列情報のテキストと合成した同一サイズのフレームPNGを作る
 2. concatデマルチプレクサで「歌唱タイミングにフレームが出るスライドショー」を作る
    (単語数が多くてもffmpegの入力数が増えない)
 3. ASS字幕(下部: 替え歌歌詞/元歌詞)と音声を焼き込んで完成

画像はWikimedia Commons等のURL(wordlist_rowのimage列)。image_page から
クレジット一覧(credits.md)を生成するので、公開時はライセンス表記に従うこと。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import ImageColor

from . import runproc
from .layout import DEFAULT_SUBTITLES, Layout, SubtitleElement, load_layout, render_frame
from .mix import MIX_DIR
from .project import ParodyWord, Project
from .synthesize import NEUTRINO_DIR

logger = logging.getLogger(__name__)

VIDEO_DIR = "video"
USER_AGENT = "soramimic-video/0.1 (https://github.com/soramimic/soramimic-video)"
HOLD_MAX_SEC = 3.0  # 次の単語が来ないとき画像を表示し続ける最大時間
SUB_PAD_SEC = 0.15  # 字幕を歌唱区間より少し早出し/遅消しする


def _run(cmd: list[str], what: str) -> None:
    proc = runproc.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{what}が失敗しました:\n{proc.stderr[-2000:]}")


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg が見つかりません")
    return path


# ---- 画像 ----


def download_image(url: str, cache_dir: Path) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    for p in cache_dir.glob(f"{name}.*"):
        return p
    # ローカルパス / file:// はコピーで取り込む(生成・ローカル単語リストの画像用)
    local = url[7:] if url.startswith("file://") else url
    if "://" not in local:
        src = Path(local).expanduser()
        if not src.exists():
            logger.warning("画像が見つかりません: %s", url)
            return None
        ext = src.suffix.lstrip(".").lower() or "img"
        path = cache_dir / f"{name}.{ext}"
        path.write_bytes(src.read_bytes())
        return path
    fetch_url = url
    if "Special:FilePath" in url and "?" not in url:
        fetch_url = url + "?width=1200"  # フル解像度は不要なのでサムネイルをもらう
    try:
        resp = requests.get(fetch_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("画像の取得に失敗: %s (%s)", url, e)
        return None
    ext = url.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
        ext = "img"
    path = cache_dir / f"{name}.{ext}"
    path.write_bytes(resp.content)
    return path


def _black_frame(out_dir: Path, width: int, height: int) -> Path:
    out = out_dir / f"black_{width}x{height}.png"
    if not out.exists():
        _run(
            [_ffmpeg(), "-y", "-f", "lavfi", "-i", f"color=black:s={width}x{height}",
             "-frames:v", "1", str(out)],
            "黒フレーム生成",
        )
    return out


# ---- タイムライン ----


@dataclass
class ImageCue:
    start: float
    end: float
    frame: Path


def word_frame_data(word: ParodyWord, row: dict) -> dict:
    """レイアウトのテンプレートに渡す1単語ぶんのデータ。

    単語リスト行の全列に、替え歌単語のフィールド(surface/kana/original等)を重ねる。
    build_image_cues とレイアウト編集プレビュー(editor JSON)で共用する。
    """
    return {
        **row,
        "surface": word.surface,
        "kana": word.kana,
        "original": word.original or row.get("original", ""),
        "original_surface": word.original_surface,
        "originalkana": word.originalkana,
    }


def word_is_shown(layout: Layout, data: dict, use_fallback: bool) -> bool:
    """このレイアウトでこの単語に表示できるもの(画像 or テキスト)があるか。

    build_image_cues が「表示するものがない単語」をキューから外す判定と同じ。
    """
    return bool(data.get("image")) or any(layout.render_texts(data, use_fallback))


def build_image_cues(
    project: Project,
    work: Path,
    width: int,
    height: int,
    image_cache: Path | None = None,
    layout: Layout | None = None,
) -> tuple[list[ImageCue], list[dict]]:
    """替え歌単語の歌唱区間に対応するフレームキュー列と、使用画像のクレジット情報。

    フレームは単語リスト行の画像+列情報をレイアウト定義で合成したもの。
    画像がなくてもレイアウトのtext要素が埋まる単語はテキストのみで表示する。
    """
    if project.parody is None:
        return [], []
    if layout is None:
        layout = load_layout(None)
    words: list[tuple[float, float, dict, bool]] = []
    for pline in project.parody.lines:
        for w in pline.words:
            row = w.wordlist_row or {}
            # 単語リストに行がない単語(手入力の未知語など)はfallback側で描く
            use_fallback = not row
            # レイアウトのテンプレートには行の全列+替え歌単語のフィールドを渡す
            data = word_frame_data(w, row)
            if not word_is_shown(layout, data, use_fallback):
                continue  # このレイアウトでは表示できるものがない単語
            start, end = project.word_time_range(w)
            words.append((start, end, data, use_fallback))
    words.sort(key=lambda x: x[0])

    cues: list[ImageCue] = []
    credits: dict[str, dict] = {}
    # ダウンロード画像はプロジェクトをまたいで使い回せる(URLベースのキー)。
    # 共有キャッシュを指定すると、同じ単語リストの2回目以降が速くなる
    cache = image_cache or Path(
        os.environ.get("SORAMIMIC_VIDEO_IMAGE_CACHE") or work / "images"
    )
    norm = work / "frames"
    for i, (start, end, data, use_fallback) in enumerate(words):
        runproc.raise_if_cancelled()  # 画像ダウンロード中でも中断できるように
        url = data.get("image") or ""
        raw = download_image(url, cache) if url else None
        if raw is None and not any(layout.render_texts(data, use_fallback)):
            continue  # 画像が取れずテキストもないフレームは出さない
        frame = render_frame(layout, raw, data, width, height, norm, use_fallback)
        if frame is None:
            continue
        # 次の単語まで表示を持続(上限あり)
        next_start = words[i + 1][0] if i + 1 < len(words) else end + HOLD_MAX_SEC
        show_end = min(max(end, next_start), end + HOLD_MAX_SEC)
        if cues and cues[-1].end > start:
            cues[-1].end = start
        cues.append(ImageCue(start=start, end=show_end, frame=frame))
        if url and raw is not None and url not in credits:
            credits[url] = {
                "word": data["surface"],
                "original": data["original"],
                "image": url,
                "image_page": data.get("image_page", ""),
            }
    return cues, list(credits.values())


def write_slideshow(
    cues: list[ImageCue], work: Path, width: int, height: int, total_sec: float
) -> Path:
    """concatデマルチプレクサでスライドショー動画を作る。"""
    black = _black_frame(work / "frames", width, height)
    entries: list[tuple[Path, float]] = []
    cursor = 0.0
    for cue in cues:
        if cue.start > cursor:
            entries.append((black, cue.start - cursor))
        entries.append((cue.frame, cue.end - cue.start))
        cursor = cue.end
    if total_sec > cursor:
        entries.append((black, total_sec - cursor))

    lines = ["ffconcat version 1.0"]
    for path, dur in entries:
        if dur <= 0:
            continue
        lines.append(f"file '{path.resolve()}'")
        lines.append(f"duration {dur:.3f}")
    # concatの仕様: 最後のファイルは duration が無視されることがあるため再掲する
    if entries:
        lines.append(f"file '{entries[-1][0].resolve()}'")
    concat_path = work / "slideshow.txt"
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = work / "slideshow.mp4"
    _run(
        [_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
         "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "fast", str(out)],
        "スライドショー生成",
    )
    return out


# ---- 字幕 ----


def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_color(color: str) -> str:
    """CSS風の色(名前 / #rrggbb / #rrggbbaa)をASSの &HAABBGGRR に変換する。"""
    rgba = ImageColor.getrgb(color)
    r, g, b = rgba[:3]
    a = rgba[3] if len(rgba) == 4 else 255
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def _ass_alignment(el: SubtitleElement) -> int:
    """align/valign をASSのnumpad Alignment値にする。"""
    base = {"bottom": 1, "middle": 4, "top": 7}.get(el.valign, 1)
    return base + {"left": 0, "center": 1, "right": 2}.get(el.align, 1)


def build_ass(
    project: Project, width: int, height: int, font: str, layout: Layout | None = None
) -> str:
    """歌詞字幕(替え歌/元歌詞)のASSを作る。行の歌唱区間で表示する。

    位置・サイズ・色はレイアウトのsubtitle要素から決める。subtitle要素の
    ないレイアウトでは既定(下部2段: 上=替え歌、下=元歌詞)になる。
    """
    subs = layout.subtitles if layout and layout.subtitles else DEFAULT_SUBTITLES
    # スタイル名はsource由来(Parody/Original)。同一sourceが複数あれば連番を足す
    names: list[str] = []
    for el in subs:
        base = el.source.capitalize()
        name = base if base not in names else f"{base}{sum(n.startswith(base) for n in names) + 1}"
        names.append(name)
    styles = []
    for el, name in zip(subs, names, strict=True):
        styles.append(
            f"Style: {name},{font},{int(el.size * height)},{_ass_color(el.color)},"
            f"&H000000FF,&H00202020,&H96000000,{-1 if el.bold else 0},0,0,0,"
            f"100,100,0,0,1,2,1,{_ass_alignment(el)},0,0,0,1"
        )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{chr(10).join(styles)}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    parody_lines = {pl.line_id: pl for pl in project.parody.lines} if project.parody else {}
    # 先に全行の表示区間を決め、前後の行と重ならないようにする。
    # 同時に表示される字幕があるとASSレンダラの衝突回避が働いて
    # 字幕が上に積み上がり、行の切り替わりで位置が跳ねるため
    shown = [line for line in project.lines if line.note_ids]
    spans = []
    for line in shown:
        start, end = project.line_time_range(line)
        spans.append([start - SUB_PAD_SEC, end + SUB_PAD_SEC])
    for j in range(len(spans) - 1):
        spans[j][1] = min(spans[j][1], spans[j + 1][0])
        spans[j][1] = max(spans[j][1], spans[j][0] + 0.2)  # 行の重なりが極端でも一瞬は出す
        spans[j + 1][0] = max(spans[j + 1][0], spans[j][1])

    events = []
    for line, (start, end) in zip(shown, spans, strict=True):
        pline = parody_lines.get(line.id)
        parody_text = "  ".join(w.surface for w in pline.words) if pline and pline.words else ""
        original_text = line.original_text or line.xf_surface
        for el, name in zip(subs, names, strict=True):
            text = parody_text if el.source == "parody" else original_text
            if not text:
                continue
            # \posで固定配置(boxのalign/valign側の辺が基準点)。
            # レイヤーをsourceで分けておくと、万一区間が重なっても替え歌と
            # 元歌詞が衝突回避で入れ替わらない(衝突判定は同一レイヤー内のみ)
            layer = 1 if el.source == "parody" else 0
            an = _ass_alignment(el)
            x, y, w, h = el.box
            px = {"left": x, "right": x + w}.get(el.align, x + w / 2) * width
            py = {"top": y, "middle": y + h / 2}.get(el.valign, y + h) * height
            events.append(
                f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{name},,0,0,0,,"
                f"{{\\an{an}\\pos({px:.0f},{py:.0f})}}{_ass_escape(text)}"
            )
    return header + "\n".join(events) + "\n"


# ---- クレジット ----


def write_credits(credits: list[dict], work: Path) -> Path | None:
    if not credits:
        return None
    lines = [
        "# 画像クレジット",
        "",
        "この動画で使用した画像の出典。公開時は各ファイルページのライセンス"
        "(作者表示など)に従ってください。",
        "",
        "| 単語 | 画像 | ライセンス確認先 |",
        "|---|---|---|",
    ]
    for c in credits:
        lines.append(f"| {c['original']} | {c['image']} | {c['image_page']} |")
    path = work / "credits.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- 本体 ----


def make_video(
    project: Project,
    project_dir: Path,
    width: int = 1280,
    height: int = 720,
    font: str = "Hiragino Sans",
    audio: str | None = None,
    image_cache: Path | None = None,
    layout: str | None = None,
) -> Path:
    layout_obj = load_layout(layout)
    work = project_dir / VIDEO_DIR
    work.mkdir(parents=True, exist_ok=True)

    audio_path: Path | None = Path(audio) if audio else None
    if audio_path is None:
        for candidate in (project_dir / MIX_DIR / "song.wav",
                          project_dir / NEUTRINO_DIR / "vocal.wav"):
            if candidate.exists():
                audio_path = candidate
                break
    if audio_path is None or not audio_path.exists():
        raise RuntimeError(
            "音声がありません。先に mix(または synthesize)を実行するか --audio で指定してください"
        )

    total_sec = max(n.end_sec for n in project.notes) + 3.0

    cues, credits = build_image_cues(project, work, width, height, image_cache, layout_obj)
    logger.info("画像キュー: %d件", len(cues))
    slideshow = write_slideshow(cues, work, width, height, total_sec)

    ass_path = work / "subtitles.ass"
    ass_path.write_text(build_ass(project, width, height, font, layout_obj), encoding="utf-8")

    credits_path = write_credits(credits, work)
    if credits_path:
        logger.info("画像クレジットを書き出しました: %s", credits_path)

    out = work / "out.mp4"
    # subtitlesフィルタのパスはffmpegのフィルタ構文でエスケープが要る
    ass_arg = str(ass_path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace(
        "'", "\\'"
    )
    _run(
        [_ffmpeg(), "-y",
         "-i", str(slideshow),
         "-i", str(audio_path),
         "-vf", f"subtitles='{ass_arg}'",
         "-c:v", "libx264", "-preset", "fast",
         "-c:a", "aac", "-b:a", "192k",
         "-shortest",
         str(out)],
        "動画の最終合成",
    )
    return out
