"""動画生成ステージ: 単語画像+字幕(替え歌/元歌詞)+歌唱音源 → out.mp4。

構成(ffmpeg 3パス):
 1. 単語リスト由来の画像をダウンロードし、レイアウト定義(layout.py)に従って
    列情報のテキストと合成した同一サイズのフレームPNGを作る
 2. concatデマルチプレクサで「歌唱タイミングにフレームが出るスライドショー」を作る
    (単語数が多くてもffmpegの入力数が増えない)
 3. ASS字幕(下部: 替え歌歌詞/元歌詞)と音声を焼き込んで完成

画像はWikimedia Commons等のURL(wordlist_rowのimage列)。クレジット表記が
必要な画像(CommonsでAttributionRequiredのもの)は出典文言をフレームに自動で
焼き込む(image_credit.py / layout.py参照。単語リストにimage_credit列があれば
その文言を優先)。あわせて image_page からクレジット一覧(credits.md)も生成
するので、公開時はライセンス表記に従うこと。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import ImageColor, ImageFont

from . import runproc
from .image_credit import USER_AGENT, fetch_image_credit, http_get_with_retry
from .kana import normalize_long_vowels
from .layout import (
    DEFAULT_SUBTITLES,
    Layout,
    SubtitleElement,
    _font,
    load_layout,
    render_frame,
    render_idle_frame,
    resolve_font_path,
)
from .mix import MIX_DIR
from .project import ParodyWord, Project
from .synthesize import NEUTRINO_DIR

logger = logging.getLogger(__name__)

VIDEO_DIR = "video"
HOLD_MAX_SEC = 3.0  # 次の単語が来ないとき画像を表示し続ける最大時間
SUB_PAD_SEC = 0.15  # 字幕を歌唱区間より少し早出し/遅消しする
# build_image_cues 前の画像/クレジットのプリフェッチ並列数。
# Commonsのサムネイル生成(Special:FilePath?width=)は並列4だと429が返ることを実測済み。
# 2なら429なしで、リトライ待ちが入る4より速かった(20枚: 24.6秒 vs 31.5秒)
IMAGE_FETCH_WORKERS = 2


def _run(cmd: list[str], what: str) -> None:
    proc = runproc.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{what}が失敗しました:\n{proc.stderr[-2000:]}")


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg が見つかりません")
    return path


def _ffprobe() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        raise RuntimeError("ffprobe が見つかりません")
    return path


def _audio_duration_sec(path: Path) -> float | None:
    """音声ファイルの実長(秒)をffprobeで取得する。取得できなければNone。

    ffprobeバイナリが無い/失敗する/出力をパースできない場合はいずれも
    警告ログを出してNoneを返す(動画生成自体は止めない)。
    """
    try:
        ffprobe_path = _ffprobe()
    except RuntimeError as exc:
        logger.warning("音声長の取得をスキップします: %s", exc)
        return None
    proc = runproc.run(
        [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        logger.warning("音声長の取得に失敗しました(%s): %s", path, proc.stderr[-500:])
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        logger.warning("音声長の取得結果を解析できませんでした(%s): %r", path, proc.stdout)
        return None


def _resolve_total_sec(sung_end_sec: float, audio_duration_sec: float | None) -> float:
    """動画の総尺(秒)を決める。

    後奏(エンディング)があると伴奏のMIDI長 = 音声の実長が、最後の歌唱ノート
    終端(+3秒の余韻)より長くなることがある。その場合は音声の実長に合わせて
    スライドショーを延ばし、後奏が映像側で切り詰められないようにする。
    音声長が取得できなかった場合は従来通り歌唱ノート側にフォールバックする。
    """
    if audio_duration_sec is None:
        return sung_end_sec
    return max(sung_end_sec, audio_duration_sec)


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
        resp = http_get_with_retry(fetch_url, headers={"User-Agent": USER_AGENT}, timeout=30)
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
        # キュー画像が1枚も無いと out_dir(frames)は誰も作っていない。
        # ffmpegは親ディレクトリを作らず「Could not open file」で失敗する
        out_dir.mkdir(parents=True, exist_ok=True)
        _run(
            [_ffmpeg(), "-y", "-f", "lavfi", "-i", f"color=black:s={width}x{height}",
             "-frames:v", "1", "-update", "1", str(out)],
            "黒フレーム生成",
        )
    return out


def _prefetch_image_assets(frames: list[WordFrame], cache: Path) -> None:
    """逐次ループの前に、使う画像とクレジットをスレッドプールで温めておく。

    download_image はファイルキャッシュ(cache/<hash>.*)へ、fetch_image_credit は
    ファイルキャッシュ(cache/credits/<hash>.json)へ書き込むので、ここで先に走らせて
    おけば後続の逐次ループの同名呼び出しはネットワークに出ずキャッシュを読む。
    URLごとに1回だけ「画像→(必要なら)クレジット」の順で実行し、重複は排除する。

    キャンセル要求時は未実行のfutureをcancelして Cancelled を伝播する
    (実行中のダウンロードは高々 IMAGE_FETCH_WORKERS 本で、タイムアウトで終わる)。
    """
    # URLごとに: クレジット取得が要るか / どの image_page で引くか(初出を採用)
    need_credit: dict[str, str] = {}
    urls: list[str] = []
    for frame in frames:
        data = frame.data
        url = data.get("image") or ""
        if not url:
            continue
        if url not in need_credit and url not in urls:
            urls.append(url)
        if not str(data.get("image_credit") or "").strip() and url not in need_credit:
            need_credit[url] = data.get("image_page", "")
    if not urls:
        return

    def _fetch(url: str) -> None:
        raw = download_image(url, cache)
        # クレジットは画像が取れたものだけ(逐次ループの条件と揃える)
        if raw is not None and url in need_credit:
            fetch_image_credit(url, need_credit[url], cache)

    with ThreadPoolExecutor(max_workers=IMAGE_FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch, url): url for url in urls}
        try:
            for fut in as_completed(futures):
                runproc.raise_if_cancelled()  # プリフェッチ中でも中断できるように
                fut.result()  # Cancelled等の例外は握りつぶさず伝播(通常は例外なし)
        except BaseException:
            for f in futures:
                f.cancel()  # 未実行分は捨てる(実行中の分は__exit__が待つ)
            raise


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


def idle_frame_data(project: Project) -> dict:
    """idle(歌唱なし区間)フレームのテンプレートに渡すプロジェクトレベルの情報。

    単語データはないので、曲(入力MIDIのファイル名)と単語リスト名だけを渡す。
    """
    title = Path(project.song.midi_path).stem if project.song.midi_path else ""
    wordlist = project.parody.wordlist if project.parody else ""
    return {"title": title, "wordlist": wordlist}


def word_is_shown(layout: Layout, data: dict, use_fallback: bool) -> bool:
    """このレイアウトでこの単語に表示できるもの(画像 or テキスト)があるか。

    build_image_cues が「表示するものがない単語」をキューから外す判定と同じ。
    """
    return bool(data.get("image")) or any(layout.render_texts(data, use_fallback))


def effective_fallback(
    layout: Layout, data: dict, use_fallback: bool, has_image: bool
) -> bool:
    """画像が無く通常側に出せるテキストも無い単語を、未知語と同じfallbackへ落とす判定。

    既知語でも画像列が空だったり画像が取得できなかったときに、何も出さない
    のではなく文字フレーム(fallback)で描くために使う。editorのキュープレビュー
    (editor_io)と build_image_cues で共用する。
    """
    if use_fallback or has_image:
        return use_fallback
    return not any(layout.render_texts(data, False))


@dataclass
class WordFrame:
    """1単語ぶんのフレーム候補(表示区間+テンプレートに渡すデータ)。"""

    line_id: int
    start: float
    end: float
    data: dict
    use_fallback: bool


def collect_word_frames(project: Project, layout: Layout) -> list[WordFrame]:
    """このレイアウトで表示できる替え歌単語を、歌唱順に並べたフレーム候補列。

    画像のダウンロード結果に依存しない純粋な計算で、build_image_cues(実際の
    フレーム生成)と build_ass(字幕の消灯タイミング)が同じ並び・同じ時間軸を
    共有するために使う。
    """
    if project.parody is None:
        return []
    frames: list[WordFrame] = []
    for pline in project.parody.lines:
        for w in pline.words:
            row = w.wordlist_row or {}
            # 単語リストに行がない単語(手入力の未知語など)はfallback側で描く
            use_fallback = not row
            # レイアウトのテンプレートには行の全列+替え歌単語のフィールドを渡す
            data = word_frame_data(w, row)
            # 画像列が空の既知語も文字フレーム(fallback)で出す
            use_fallback = effective_fallback(
                layout, data, use_fallback, has_image=bool(data.get("image"))
            )
            if not word_is_shown(layout, data, use_fallback):
                continue  # このレイアウトでは表示できるものがない単語
            start, end = project.word_time_range(w)
            frames.append(WordFrame(pline.line_id, start, end, data, use_fallback))
    frames.sort(key=lambda f: f.start)
    return frames


def frame_show_end(frames: list[WordFrame], i: int, hold_next: bool) -> float:
    """frames[i] の表示を次の単語まで持続させたときの終了時刻。

    既定は最大 HOLD_MAX_SEC 秒。hold_next(レイアウトの "hold": "next")なら
    次の単語まで隙間を埋め続ける(間奏まるごと持続する)。最終単語より後(後奏)は
    hold_next でも持続させず idle/黒に任せる。
    字幕(build_ass)も行末の単語のこの値に合わせて消える。
    """
    end = frames[i].end
    if i + 1 >= len(frames):
        return end if hold_next else end + HOLD_MAX_SEC
    next_start = frames[i + 1].start
    if hold_next:
        return max(end, next_start)
    return min(max(end, next_start), end + HOLD_MAX_SEC)


def line_show_ends(project: Project, layout: Layout) -> dict[int, float]:
    """行ID -> その行の最後の単語フレームの表示終了時刻(単語フレームが無い行は含まない)。"""
    frames = collect_word_frames(project, layout)
    return {
        f.line_id: frame_show_end(frames, i, layout.hold_next) for i, f in enumerate(frames)
    }


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
    frames = collect_word_frames(project, layout)

    cues: list[ImageCue] = []
    credits: dict[str, dict] = {}
    # ダウンロード画像はプロジェクトをまたいで使い回せる(URLベースのキー)。
    # 共有キャッシュを指定すると、同じ単語リストの2回目以降が速くなる
    cache = image_cache or Path(
        os.environ.get("SORAMIMIC_VIDEO_IMAGE_CACHE") or work / "images"
    )
    norm = work / "frames"
    # 逐次ループが読む画像/クレジットを先に並列で温める(キャッシュが冷えていると
    # 1単語あたり画像DL+クレジット取得で数秒かかり、単語数ぶん直列に積み上がるため)
    _prefetch_image_assets(frames, cache)
    for i, wf in enumerate(frames):
        start, data, use_fallback = wf.start, wf.data, wf.use_fallback
        runproc.raise_if_cancelled()  # 画像ダウンロード中でも中断できるように
        url = data.get("image") or ""
        raw = download_image(url, cache) if url else None
        if raw is None:
            # 画像が取得できなかった既知語も未知語と同じ文字フレームに落とす
            use_fallback = effective_fallback(layout, data, use_fallback, has_image=False)
            if not any(layout.render_texts(data, use_fallback)):
                continue  # 画像が取れずテキストもないフレームは出さない
        # 画像クレジット文言: 単語リストのimage_credit列があればそれを、なければ
        # Commonsから取得(表記不要な画像では空になり、フレームには描かれない)
        if raw is not None and url and not str(data.get("image_credit") or "").strip():
            info = fetch_image_credit(url, data.get("image_page", ""), cache)
            if info is not None:
                data["image_credit"] = info["credit_text"]
        frame = render_frame(layout, raw, data, width, height, norm, use_fallback)
        if frame is None:
            continue
        # 次の単語まで表示を持続(字幕の消灯も build_ass が同じ値に合わせる)
        show_end = frame_show_end(frames, i, layout.hold_next)
        if cues and cues[-1].end > start:
            cues[-1].end = start
        cues.append(ImageCue(start=start, end=show_end, frame=frame))
        if url and raw is not None and url not in credits:
            credits[url] = {
                "word": data["surface"],
                "original": data["original"],
                "image": url,
                "image_page": data.get("image_page", ""),
                "credit": str(data.get("image_credit") or ""),
            }
    return cues, list(credits.values())


def write_slideshow(
    cues: list[ImageCue],
    work: Path,
    width: int,
    height: int,
    total_sec: float,
    idle_frame: Path | None = None,
) -> Path:
    """concatデマルチプレクサでスライドショー動画を作る。

    歌唱がない隙間(前奏・間奏・後奏)は idle_frame があればそれで、なければ黒で埋める。
    """
    fill = idle_frame or _black_frame(work / "frames", width, height)
    entries: list[tuple[Path, float]] = []
    cursor = 0.0
    for cue in cues:
        if cue.start > cursor:
            entries.append((fill, cue.start - cursor))
        entries.append((cue.frame, cue.end - cue.start))
        cursor = cue.end
    if total_sec > cursor:
        entries.append((fill, total_sec - cursor))

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


WORD_SEP = "  "  # 替え歌字幕で単語を区切る空白(build_ass本文とルビ位置計算で共有)

# ひらがな→カタカナ(表記が既にカナかを判定するための正規化用)
_HIRA_TO_KATA = {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}


def _to_katakana(text: str) -> str:
    return "".join(_HIRA_TO_KATA.get(ch, ch) for ch in text)


_KATA_TO_HIRA = {v: k for k, v in _HIRA_TO_KATA.items()}


def _to_hiragana(text: str) -> str:
    """ルビ表示用にカタカナをひらがなへ(長音「ー」などはそのまま)。"""
    return "".join(_KATA_TO_HIRA.get(ch, ch) for ch in text)


def _is_all_kana(text: str) -> bool:
    return all(ch in _KANA_CHARS for ch in text)


# 読みを持たない記号(区切り・つなぎ)。表記には出るが読みには現れないので、
# ルビの要否判定でも読みの割り付けでも「無いもの」として扱う。
# (例: 「バリッシュ・コノル」の読みは「バリッシュコノル」で中黒は現れない)
# 中黒(全角・半角)、イコール類(人名の区切り)、空白。
_SILENT_SYMBOLS = frozenset("・･＝=゠ 　")


def _strip_silent(text: str) -> str:
    """読みを持たない記号を取り除く。"""
    return "".join(ch for ch in text if ch not in _SILENT_SYMBOLS)


def _needs_ruby(surface: str, kana: str) -> bool:
    """この単語にルビを振るべきか。

    表記が全部カナなら読みは表記から自明なのでルビ不要(カタカナ表記に
    ひらがなルビを重ねない)。カナ以外(漢字等)を含む場合は、ひらがな/
    カタカナ・長音表記のゆれを吸収したうえで読みと違うときだけ振る。
    読みを持たない記号(中黒等)は判定前に除去する(「バリッシュ・コノル」は
    実質全カナなのでルビ不要)。
    """
    if not surface or not kana:
        return False
    bare = _strip_silent(surface)
    if _is_all_kana(bare):
        return False
    a = normalize_long_vowels(_to_katakana(bare))
    b = normalize_long_vowels(_to_katakana(kana))
    return a != b


# そのまま読める文字(ひらがな・カタカナ・長音記号など)。部分ルビのラン分割用
_KANA_CHARS = frozenset(
    [chr(c) for c in range(0x3041, 0x3097)]  # ひらがな
    + [chr(c) for c in range(0x30A1, 0x30FB)]  # カタカナ(ァ〜ヺ)
    + list("ーゝゞヽヾ")
)


def _kana_runs(surface: str) -> list[tuple[int, int, bool]]:
    """表記をカナ/非カナの連続ランに分ける。(開始idx, 終了idx, カナか) の列。

    読みを持たない記号(中黒等)はカナ側に含める(ルビを振る対象ではないため)。
    """
    runs: list[tuple[int, int, bool]] = []
    for i, ch in enumerate(surface):
        is_kana = ch in _KANA_CHARS or ch in _SILENT_SYMBOLS
        if runs and runs[-1][2] == is_kana:
            runs[-1] = (runs[-1][0], i + 1, is_kana)
        else:
            runs.append((i, i + 1, is_kana))
    return runs


def _ruby_segments(surface: str, kana: str) -> list[tuple[int, int, str]] | None:
    """読みを表記の非カナ部分(漢字等)へ割り付ける。

    表記をカナ/非カナのランに分け、カナランはリテラル・非カナランは「1文字以上の
    任意」として組んだ正規表現を読み全体にfullmatchさせる。返すのは非カナランごとの
    (表記の開始idx, 終了idx, そのランの読み)。素のカタカナ化で一致しなければ長音の
    表記ゆれを吸収して再試行し、それでも対応づけできなければNone(呼び出し側は
    単語全体ルビにフォールバックする)。

    読みを持たない記号(中黒等)はカナ側のランに含めたうえで、リテラルからは
    除去する(読みには現れないため)。

    例: 「燦花シノノ」×「サンカシノノ」→ [(0, 2, "サンカ")]
        「アテル＝参」×「アテルサン」→ [(4, 5, "サン")]
    """
    if not surface or not kana:
        return None
    runs = _kana_runs(surface)
    surface_kata = _to_katakana(surface)
    kana_kata = _to_katakana(kana)

    def _match(normalize: bool) -> re.Match[str] | None:
        pattern = ""
        for s, e, is_kana in runs:
            if is_kana:
                # 読みを持たない記号(中黒等)は読みに現れないのでリテラルから外す
                part = _strip_silent(surface_kata[s:e])
                pattern += re.escape(normalize_long_vowels(part) if normalize else part)
            else:
                pattern += "(.+?)"
        target = normalize_long_vowels(kana_kata) if normalize else kana_kata
        return re.fullmatch(pattern, target)

    m = _match(False) or _match(True)
    if m is None:
        return None
    # normalize_long_vowels は1文字を1文字に置き換えるので、マッチ位置は元の読みと揃う
    segments: list[tuple[int, int, str]] = []
    group = 0
    for s, e, is_kana in runs:
        if is_kana:
            continue
        group += 1
        gs, ge = m.span(group)
        segments.append((s, e, kana[gs:ge]))
    return segments


def _measuring_font(font_path: Path | None, ass_fontsize: int):
    """ASSの本文と同じ字面幅を測るためのPillowフォント。

    libass(VSFilter互換)はASSのFontsizeを「行セル高(アセント+ディセント)」として
    扱うため、実際の字面(em)は Fontsize×em/セル高 に縮む。Pillowのサイズ指定は
    emそのものなので、そのまま測ると横位置が字面比ぶん(Noto CJKで約1.48倍)
    外側へずれていく。セル高比で縮めたサイズで測って揃える。
    """
    font = _font(font_path, ass_fontsize)
    if not isinstance(font, ImageFont.FreeTypeFont):  # フォント未解決時は補正不能
        return font
    ascent, descent = font.getmetrics()
    cell = ascent + descent
    if cell <= 0 or cell == ass_fontsize:
        return font
    return _font(font_path, max(1, round(ass_fontsize * ass_fontsize / cell)))


def _ruby_events(
    el: SubtitleElement,
    name: str,
    layer: int,
    start: float,
    end: float,
    words: list[ParodyWord],
    px: float,
    py: float,
    an: int,
    height: int,
    font_path: Path | None,
) -> list[str]:
    """替え歌字幕の漢字等の部分の真上にルビ(ふりがな)を置くASSイベント列。

    本文行と同じフォント・同じピクセルサイズでPillowで文字幅を測り、ルビを振る
    範囲のx中心を求める。本文と同一レイヤー・同一区間で、範囲ごとに小さい
    フォントの別イベントを本文の上端すぐ上に \\pos で配置する。
    ルビは表記の非カナ部分にだけ振る(_ruby_segments)。読みを割り付けられない
    単語だけ、従来どおり単語全体に読み全体を置く。
    """
    body_px = int(el.size * height)
    if body_px <= 0 or not words:
        return []
    font = _measuring_font(font_path, body_px)
    full = WORD_SEP.join(w.surface for w in words)
    total_w = font.getlength(full)
    # 本文行の左端x。build_ass本体の px(align基準点)と揃える
    if el.align == "left":
        x0 = px
    elif el.align == "right":
        x0 = px - total_w
    else:
        x0 = px - total_w / 2
    # 本文行の上端y(\pos の基準点 py と valign(an)から逆算。行高は概ねフォントpx)
    if an in (1, 2, 3):
        top = py - body_px
    elif an in (4, 5, 6):
        top = py - body_px / 2
    else:
        top = py
    ruby_px = max(1, round(el.ruby_size * body_px))
    events: list[str] = []
    prefix = ""
    for i, w in enumerate(words):
        if i:
            prefix += WORD_SEP
        start_x = font.getlength(prefix)
        prefix += w.surface
        end_x = font.getlength(prefix)
        if not _needs_ruby(w.surface, w.kana):
            continue
        # 表記のカナ部分(そのまま読める部分)にはルビを振らず、漢字等のランごとに
        # 対応する読みだけをその真上へ置く。対応づけできない語は単語全体に読み全体
        segments = _ruby_segments(w.surface, w.kana)
        if segments is None:
            spans = [(start_x, end_x, w.kana)]
        else:
            spans = []
            for s, e, reading in segments:
                if _to_katakana(w.surface[s:e]) == reading:
                    continue  # 表記どおりの読みならルビ不要
                spans.append((
                    start_x + font.getlength(w.surface[:s]),
                    start_x + font.getlength(w.surface[:e]),
                    reading,
                ))
        for sx, ex, reading in spans:
            cx = x0 + (sx + ex) / 2  # ルビを振る範囲の中心x
            # \an2: ルビの下端中央をその範囲の中心・本文上端に合わせる(本文のすぐ上に載る)
            events.append(
                f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{name},,0,0,0,,"
                f"{{\\an2\\pos({cx:.0f},{top:.0f})\\fs{ruby_px}}}"
                f"{_ass_escape(_to_hiragana(reading))}"
            )
    return events


def build_ass(
    project: Project,
    width: int,
    height: int,
    font: str,
    layout: Layout | None = None,
    granularity: dict[str, str] | None = None,
) -> str:
    """歌詞字幕(替え歌/元歌詞)のASSを作る。行の歌唱区間で表示する。

    消灯はその行の最後の単語画像の余韻(frame_show_end)に合わせる(次の行の
    表示が始まればそこで交代)。位置・サイズ・色はレイアウトのsubtitle要素から
    決める。subtitle要素のないレイアウトでは既定(下部2段: 上=替え歌、下=元歌詞)になる。
    表示粒度(行/フレーズ)は subtitle要素の granularity、なければ granularity 引数
    (Web UIの一括指定)、それも無ければ source 既定に従う。
    """
    from .align import build_subtitle_segments, resolve_granularity

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
    # 行の消灯は単語画像の余韻(frame_show_end)に合わせる。間奏の手前で
    # 字幕だけ先に消えて画像が残る、というずれをなくすため。
    # 単語フレームが無い行(画像なしレイアウト等)は従来のパディングのみ
    show_ends = line_show_ends(project, layout or load_layout(None))
    spans = []
    for line in shown:
        start, end = project.line_time_range(line)
        stop = max(end + SUB_PAD_SEC, show_ends.get(line.id, end))
        spans.append([start - SUB_PAD_SEC, stop])
    for j in range(len(spans) - 1):
        spans[j][1] = min(spans[j][1], spans[j + 1][0])
        spans[j][1] = max(spans[j][1], spans[j][0] + 0.2)  # 行の重なりが極端でも一瞬は出す
        spans[j + 1][0] = max(spans[j + 1][0], spans[j][1])

    font_path = resolve_font_path(layout.font if layout else None)
    # 行ごとの素材(グループ化・切り出し・マージは align 側の共通ロジックで行う)
    plines = [parody_lines.get(line.id) for line in shown]
    originals = [line.original_text for line in shown]  # グループ化キー(未対応はNone)
    # 元歌詞のフレーズ切り出しは読み(かな)どうしで突き合わせるので XFカナを優先
    xf_texts = [line.xf_kana or line.xf_surface for line in shown]
    original_full = [(line.original_text or line.xf_surface) for line in shown]
    parody_full = [
        WORD_SEP.join(w.surface for w in pl.words) if pl and pl.words else ""
        for pl in plines
    ]
    # spans は上の重なり調整で可変listにしていたので、区間は (start, end) に固める
    span_pairs = [(s[0], s[1]) for s in spans]

    events = []
    for el, name in zip(subs, names, strict=True):
        gran = resolve_granularity(el.source, getattr(el, "granularity", None), granularity)
        full_texts = parody_full if el.source == "parody" else original_full
        segments = build_subtitle_segments(
            el.source, gran, originals, full_texts, xf_texts, span_pairs, sep=WORD_SEP
        )
        # \posで固定配置(boxのalign/valign側の辺が基準点)。
        # レイヤーをsourceで分けておくと、万一区間が重なっても替え歌と
        # 元歌詞が衝突回避で入れ替わらない(衝突判定は同一レイヤー内のみ)
        layer = 1 if el.source == "parody" else 0
        an = _ass_alignment(el)
        x, y, w, h = el.box
        px = {"left": x, "right": x + w}.get(el.align, x + w / 2) * width
        py = {"top": y, "middle": y + h / 2}.get(el.valign, y + h) * height
        for seg in segments:
            if not seg.text:
                continue
            events.append(
                f"Dialogue: {layer},{_ass_time(seg.start)},{_ass_time(seg.end)},{name},,0,0,0,,"
                f"{{\\an{an}\\pos({px:.0f},{py:.0f})}}{_ass_escape(seg.text)}"
            )
            # ルビ(ふりがな): 替え歌字幕のみ。本文と同一レイヤー・同一区間で、
            # 各単語の真上に小さいフォントの別イベントを追加する(本文は変えない)。
            # 行マージ(parody=line)時はグループ内の全単語を連結して並べる
            if el.source == "parody" and el.ruby:
                words = []
                for k in seg.indices:
                    pl = plines[k]
                    if pl is not None:
                        words.extend(pl.words)
                if words:
                    events.extend(
                        _ruby_events(
                            el, name, layer, seg.start, seg.end, words, px, py, an,
                            height, font_path,
                        )
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
        "クレジット欄が空の画像は表記不要(パブリックドメイン等)か情報を取得"
        "できなかったもので、後者はライセンス確認先で要確認です。",
        "",
        "| 単語 | 画像 | クレジット | ライセンス確認先 |",
        "|---|---|---|---|",
    ]
    for c in credits:
        lines.append(
            f"| {c['original']} | {c['image']} | {c.get('credit', '')} | {c['image_page']} |"
        )
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
    granularity: dict[str, str] | None = None,
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

    sung_end_sec = max(n.end_sec for n in project.notes) + 3.0
    total_sec = _resolve_total_sec(sung_end_sec, _audio_duration_sec(audio_path))

    cues, credits = build_image_cues(project, work, width, height, image_cache, layout_obj)
    if cues:
        logger.info("画像キュー: %d件", len(cues))
    else:
        # 画像取得の全滅(ネットワーク・レート制限)や、画像もテキストも無い
        # レイアウトで起きる。動画は生成されるが全編無地になるので目立たせる
        logger.warning("画像キューが0件です。動画の背景は全編無地になります")
    # 歌唱がない区間(前奏・間奏・後奏)用のidleフレーム(定義があるときだけ)
    idle_frame = render_idle_frame(
        layout_obj, idle_frame_data(project), width, height, work / "frames"
    )
    slideshow = write_slideshow(cues, work, width, height, total_sec, idle_frame)

    ass_path = work / "subtitles.ass"
    ass_path.write_text(
        build_ass(project, width, height, font, layout_obj, granularity), encoding="utf-8"
    )

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
