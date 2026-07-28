"""サムネ画像(thumbnail.png)の生成。

曲名をそのジョブと同じ単語リスト・パラメータで空耳変換し、言い換え単語を
大きく載せた動画と同じアスペクト(既定1280x720)のPNGを作る。

    ┌─────────────────────────────┐
    │ 【ダイオージャ】     [単語の画像]  │
    │                             │
    │   lemon を 架空のアニメキャラ で歌ってみた │
    │                lyrics by Soramimic │
    └─────────────────────────────┘

描画は layout.py のレイアウト機構(フォント解決・テキスト収め込み・画像配置)を
そのまま使うので、見た目のトーンは動画のカードレイアウトと揃う。
変換に失敗した・曲名が無い・言い換え単語が取れないときは、言い換えなしの
「<曲名> を <リスト名> で歌ってみた」だけのサムネにフォールバックする
(サムネ生成の失敗で動画生成そのものは止めない)。

生成物は make_video から呼ばれてプロジェクト(ジョブ)ディレクトリ直下に
thumbnail.png として置かれ、動画の前奏区間にも表示される(video.py 参照)。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from . import runproc
from .convert import _find_row, _load_wordlist_rows, resolve_wordlist
from .editor_io import wordlist_display_name
from .layout import Layout, fitted_image_box, parse_layout, render_image
from .project import Project
from .soramimic_engine import run_convert

logger = logging.getLogger(__name__)

THUMBNAIL_FILENAME = "thumbnail.png"
SIGNATURE = "lyrics by Soramimic"
# 単語画像を貼る枠(フレーム比率)。画像はこの中にアスペクト維持で収まる
IMAGE_BOX = (0.53, 0.08, 0.41, 0.55)

# 隅の署名と「<曲名> を <リスト名> で歌ってみた」は3パターン共通
_CAPTION_ELEMENT = {
    "type": "text", "text": "{caption}", "box": [0.05, 0.70, 0.90, 0.15],
    "size": 0.075, "color": "white", "wrap": True,
}
_SIGNATURE_ELEMENT = {
    "type": "text", "text": SIGNATURE, "box": [0.04, 0.89, 0.92, 0.05],
    "size": 0.032, "color": "#b8b8b8", "align": "right", "valign": "bottom",
}


def thumbnail_layout_spec(
    has_word: bool,
    has_image: bool,
    credit_box: tuple[float, float, float, float] | None = None,
) -> dict:
    """サムネのレイアウト定義(layout.py と同じ書式のdict)。

    - 言い換え単語+画像: 左に【単語】、右に画像(クレジットは画像の右下)
    - 言い換え単語のみ: 中央に大きく【単語】
    - どちらも無い: 「<曲名> を <リスト名> で歌ってみた」だけ(フォールバック)

    見出し(【単語】)は折り返さず、長い単語ではフォントを縮めて1行に収める
    (「】」だけが次の行に落ちるのを避けるため)。credit_box を渡すと
    クレジットの帯をその枠(=実際に貼られた画像の領域)の右下に置く。
    """
    if not has_word:
        return {
            "background": "black",
            "elements": [
                {"type": "text", "text": "{caption}", "box": [0.06, 0.28, 0.88, 0.34],
                 "size": 0.11, "color": "white", "wrap": True},
                _SIGNATURE_ELEMENT,
            ],
        }
    if has_image:
        return {
            "background": "black",
            "elements": [
                {"type": "image", "box": list(IMAGE_BOX)},
                {"type": "text", "text": "{headline}", "box": [0.04, 0.10, 0.45, 0.51],
                 "size": 0.15, "color": "white", "stroke_width": 0.004},
                # 画像クレジット(表記不要な画像では空文字になり描かれない)
                {"type": "text", "text": "{image_credit}", "size": 0.024,
                 "box": list(credit_box or IMAGE_BOX),
                 "color": "#dddddd", "align": "right", "valign": "bottom",
                 "background": "#00000080"},
                _CAPTION_ELEMENT,
                _SIGNATURE_ELEMENT,
            ],
        }
    return {
        "background": "black",
        "elements": [
            {"type": "text", "text": "{headline}", "box": [0.06, 0.12, 0.88, 0.45],
             "size": 0.2, "color": "white", "stroke_width": 0.004},
            _CAPTION_ELEMENT,
            _SIGNATURE_ELEMENT,
        ],
    }


def thumbnail_layout(
    has_word: bool,
    has_image: bool,
    credit_box: tuple[float, float, float, float] | None = None,
) -> Layout:
    return parse_layout(
        thumbnail_layout_spec(has_word, has_image, credit_box), "<thumbnail>"
    )


def thumbnail_data(
    title: str, wordlist_text: str, word: str = "", image_credit: str = ""
) -> dict:
    """サムネのテンプレートに渡す値(headline / caption / image_credit)。

    曲名・単語リスト名のどちらかが取れないときは、その部分を省いた文にする
    (「 を  で歌ってみた」のような空欄が残らないように)。
    """
    if title and wordlist_text:
        caption = f"{title} を {wordlist_text} で歌ってみた"
    elif title:
        caption = f"{title} を歌ってみた"
    elif wordlist_text:
        caption = f"{wordlist_text} で歌ってみた"
    else:
        caption = ""
    return {
        "headline": f"【{word}】" if word else "",
        "caption": caption,
        "image_credit": image_credit,
    }


def song_title(project: Project, fallback: str | None = None) -> str:
    """サムネ・キャプションに出す曲名。

    APIジョブでは project の midi_path が固定名(input.mid)になるので、
    アップロード時のファイル名(job.params の midi_filename)を fallback として
    渡してもらい、そちらを優先する。どちらも無ければ空文字。
    """
    for candidate in (fallback, project.song.midi_path):
        if candidate and candidate.strip():
            name = candidate.strip().rsplit("/", 1)[-1]
            # 拡張子だけを落とす(「Mr.Children」のような曲名を切らないよう短い
            # 英数字の末尾に限る)
            stem = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name).strip()
            if stem:
                return stem
    return ""


def title_paraphrase(
    title: str,
    wordlist: str,
    where: str | None,
    params: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str] | None] | None:
    """曲名を1フレーズだけ空耳変換し、(先頭の単語, 単語リスト行)を返す。

    変換はジョブ本体(convert.py)と同じ経路・同じ単語リスト・同じパラメータで
    行うので、動画に出てくる単語と同じ雰囲気の言い換えになる。
    変換できる単語が無ければ None。
    """
    csv_path = resolve_wordlist(wordlist)
    result = run_convert([title], csv_path, where, dict(params or {}))
    lines = result.get("lines") or []
    words = lines[0].get("words") if lines else []
    if not words:
        return None
    word = words[0]
    return word, _find_row(_load_wordlist_rows(csv_path), word)


def render_thumbnail(
    out_path: Path,
    title: str,
    wordlist_text: str,
    word: str = "",
    image_path: Path | None = None,
    image_credit: str = "",
    width: int = 1280,
    height: int = 720,
) -> Path:
    """サムネPNGを描いて out_path に保存する。"""
    credit_box = None
    if image_path is not None and image_credit:
        # クレジットの帯は枠ではなく実際に貼られた画像の右下に載せる(動画のフレームと同じ)
        credit_box = fitted_image_box(image_path, IMAGE_BOX, width, height)
    layout = thumbnail_layout(
        has_word=bool(word), has_image=image_path is not None, credit_box=credit_box
    )
    data = thumbnail_data(title, wordlist_text, word, image_credit)
    canvas = render_image(layout, image_path, data, width, height)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path


def _word_image(
    row: dict[str, str] | None, cache: Path, download: bool = True
) -> tuple[Path | None, str]:
    """単語リスト行の image 列から画像とクレジット文言を取る(取れなければ空)。

    download=False ならネットワークを使わず、キャッシュ済みの画像・クレジットだけを
    使う(待てないプレビュー生成向け。無ければ画像なしのサムネになる)。
    """
    from .image_credit import fetch_image_credit
    from .video import cached_image, download_image

    url = (row or {}).get("image") or ""
    if not url:
        return None, ""
    path = download_image(url, cache) if download else cached_image(url, cache)
    if path is None:
        return None, ""
    credit = str((row or {}).get("image_credit") or "").strip()
    if not credit:
        info = fetch_image_credit(
            url, (row or {}).get("image_page", ""), cache, cached_only=not download
        )
        credit = info["credit_text"] if info else ""
    return path, credit


def wordlist_text_of(wordlist: str) -> str:
    """サムネのキャプションに出す単語リストの表示名(解決できなければ空)。"""
    stem = wordlist
    try:
        stem = resolve_wordlist(wordlist).stem if wordlist else ""
    except FileNotFoundError:
        logger.warning("単語リストが見つかりません(表示名はそのまま使います): %s", wordlist)
    return wordlist_display_name(stem) if stem else ""


def build_thumbnail(
    out_path: Path,
    song: str,
    wordlist: str,
    where: str | None = None,
    params: dict[str, Any] | None = None,
    image_cache: Path | None = None,
    width: int = 1280,
    height: int = 720,
    download_images: bool = True,
) -> Path | None:
    """曲名を1フレーズ変換してサムネPNGを out_path に作る(サムネ生成の本体)。

    ジョブのサムネ(generate_thumbnail)と、生成前のプレビュー
    (thumbnail_preview.py)が共有する。変換・画像取得が失敗しても
    言い換えなし・画像なしのサムネにフォールバックし、描画自体に失敗した
    ときだけ None を返す(いずれも警告ログのみ)。中断要求(Cancelled)は伝播する。
    """
    wordlist_text = wordlist_text_of(wordlist)

    word = ""
    row: dict[str, str] | None = None
    if song and wordlist:
        try:
            found = title_paraphrase(song, wordlist, where, params)
            if found is not None:
                raw_word, row = found
                word = str(raw_word.get("surface") or "")
        except runproc.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - サムネの失敗でジョブを落とさない
            logger.warning("曲名の空耳変換に失敗しました(言い換えなしのサムネにします): %s", e)

    image_path: Path | None = None
    image_credit = ""
    if word and image_cache is not None:
        try:
            image_path, image_credit = _word_image(row, image_cache, download_images)
        except runproc.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 画像なしのサムネにフォールバック
            logger.warning("サムネ用の画像を取得できませんでした: %s", e)

    try:
        path = render_thumbnail(
            out_path,
            song,
            wordlist_text,
            word=word,
            image_path=image_path,
            image_credit=image_credit,
            width=width,
            height=height,
        )
    except Exception as e:  # noqa: BLE001 - 描画失敗もジョブは落とさない
        logger.warning("サムネ画像を生成できませんでした: %s", e)
        return None
    logger.info("サムネ画像を生成しました: %s", path)
    return path


def generate_thumbnail(
    project: Project,
    project_dir: Path,
    width: int = 1280,
    height: int = 720,
    image_cache: Path | None = None,
    title: str | None = None,
) -> Path | None:
    """曲名の空耳変換つきサムネPNGを project_dir/thumbnail.png に作る。

    変換条件(単語リスト・where・パラメータ)はジョブ本体の変換と同じものを
    project.parody から取る。失敗時の扱いは build_thumbnail と同じ。
    """
    from .video import VIDEO_DIR, image_cache_dir

    return build_thumbnail(
        project_dir / THUMBNAIL_FILENAME,
        song_title(project, title),
        project.parody.wordlist if project.parody else "",
        where=project.parody.where if project.parody else None,
        params=project.parody.params if project.parody else None,
        image_cache=image_cache_dir(project_dir / VIDEO_DIR, image_cache),
        width=width,
        height=height,
    )
