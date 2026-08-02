"""soramimic editor との連携(JSONファイルの書き出し/取り込み)。

soramimic の編集ツールは `soramimic-editor/1` 形式のJSON
(phrases/tokensList/results/param/wordlist/unitsList)を
読み込み/書き出しできる(soramimic#51)。

- export_editor: convert時に保存した変換の生応答から editor 用JSONを作る
- import_editor: editorで編集・書き出したJSONから project.parody を作り直す
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .convert import REPO_ROOT, apply_converted_lines, resolve_wordlist
from .facets import survives_editor_facets
from .layout import Layout
from .project import ParodyWord, Project
from .ruby import strip_ruby

logger = logging.getLogger(__name__)

RAW_FILENAME = "soramimic_raw.json"
EDITOR_FILENAME = "editor.json"
EXPORT_FORMAT = "soramimic-editor/1"

SETTING_JSON = REPO_ROOT / "external" / "soramimic" / "conf" / "setting.json"
# 文中で使う単語リスト名(「<曲名> を <ここ> で歌ってみた」)。setting.json の text は
# プルダウンの選択肢用に短く付けられており(「日常系」など)、単体では意味が
# 取れないものがあるため、文章向けの言い方をこちらで持つ。
WORDLIST_PHRASES_PATH = Path(__file__).resolve().parent / "wordlist_phrases.json"

# ---- 自作リスト(アップロードされたCSV)の editor セッション ----
# 名前付きリスト(external/soramimic-wordlists)と違い、自作リストはサーバー上に
# 名前で置かれていないので、editor の buildDatabase が引ける場所が無い。
# /api/editor-session は正規化済みCSVを
#   <jobs_dir>/editor-sessions/<sid>/wordlist.csv
# に置き、editor には同一オリジンの /editor/session-wordlists/<sid>.csv として
# 引かせる(editor側は無改修。filepath が違うだけの tidy リストに見える)。
# sid は正規化CSVの内容ハッシュ(wordlist_csv.WordlistCsv.fingerprint)なので、
# 同じ入力なら同じセッションを指す(開き直してもディレクトリが増えない)。
EDITOR_SESSIONS_DIRNAME = "editor-sessions"
SESSION_WORDLIST_FILENAME = "wordlist.csv"
# editor JSON の filepath に書く相対パス(editor.html から見た位置)
SESSION_WORDLIST_URLDIR = "session-wordlists"
# editor JSON の wordlist.value。名前付きリスト(BASEBALL 等)と衝突しない形にする
CUSTOM_WORDLIST_PREFIX = "custom:"
CUSTOM_WORDLIST_TEXT = "自作リスト"
# sid は fingerprint(sha256の先頭16桁)だけを通す(パスに使うので厳格に見る)
_SID_RE = re.compile(r"[0-9a-f]{16}")


# ---- 自作リスト(エディタが同梱してくる csvText)----
# エディタ側の⚙モーダルで自作リストを使うと、書き出しJSONの wordlist は
#   {"value": "ORIGINAL", "text": "自作リスト", "csvText": "<正規化済みtidy CSV>"}
# になる(filepath は持たない)。csvText は results の id を振ったときのCSVその
# ものなので、これをそのまま行の引き当てに使えば id が必ず一致する。
# サーバー上のセッション(custom:<sid>)を必要としない自己完結の経路。
ORIGINAL_WORDLIST_VALUE = "ORIGINAL"
# 取り込み時にジョブディレクトリへ書き出すファイル名(以後ここから行を引く)
ORIGINAL_WORDLIST_FILENAME = "original-wordlist.csv"


# ---- 元歌詞(字幕用)の受け渡し ----
# 元歌詞は video のフォーム(#lyrics)が正本で、字幕の行対応づけは video 側の
# align_lines が行う。エディタでも元歌詞を入力・確認できるようにするため、
# **生テキストだけ**を双方向にやり取りする。
#
#   ホスト→エディタ(シード)  lyrics … 元歌詞の生テキスト(任意)
#   エディタ→ホスト(書き出し) lyrics … エディタで編集されうる生テキスト
#
# 書き出しJSONにはエディタが対応づけた行ごとの元歌詞(originalLines)も入るが、
# **video は採用しない**。ブラウザ側の対応づけ(soramimic の xfAlign 由来)は
# 精度が足りず、行数が一致する曲でも境界が1〜2文字ずれたり対応づかない行が
# 出たりして字幕が劣化するため。対応づけは align_lines を正とし、
# editor.json の originalLines はエディタの表示用と割り切る。
LYRICS_FIELD = "lyrics"


def seed_with_lyrics(payload: dict[str, Any], lyrics: str) -> dict[str, Any]:
    """editor へのシードに元歌詞の生テキストを載せる(空なら載せない・破壊的)。

    ルビ記法(``｜表層《よみ》``)が入っていても素通しする——記法はエディタ側の
    読み生成にも効かせたいので、ここで剥がさない(剥がすと読みの指定が落ちる)。
    """
    if lyrics.strip():
        payload[LYRICS_FIELD] = lyrics
    return payload


def editor_lyrics(payload: dict) -> str | None:
    """editor 書き出しJSONの ``lyrics``(元歌詞の生テキスト)。無ければ None。

    ルビ記法(``｜表層《よみ》``)は剥がさない——対応づけ(align_lines)が記法を
    解釈して読みに効かせるので、そのまま渡す。
    """
    lyrics = payload.get(LYRICS_FIELD)
    if not isinstance(lyrics, str) or not lyrics.strip():
        return None
    return lyrics


def editor_sessions_dir(jobs_dir: Path) -> Path:
    """自作リストのeditorセッション置き場(ジョブディレクトリの隣)。"""
    return jobs_dir.resolve() / EDITOR_SESSIONS_DIRNAME


def valid_session_id(sid: str | None) -> bool:
    """sid がセッションIDの形(16桁の16進)かどうか。パス組み立て前に必ず通す。"""
    return bool(sid) and bool(_SID_RE.fullmatch(str(sid)))


def session_wordlist_path(sessions_dir: Path | None, sid: str | None) -> Path | None:
    """セッションIDに対応する正規化済みCSVのパス(無ければ None)。"""
    if sessions_dir is None or not valid_session_id(sid):
        return None
    path = Path(sessions_dir) / str(sid) / SESSION_WORDLIST_FILENAME
    return path if path.is_file() else None


def custom_wordlist_entry(sid: str) -> dict[str, Any]:
    """自作リスト用の単語リスト設定(conf/setting.json のエントリと同じ形)。

    editor の buildDatabase は value==="ORIGINAL" だけ特別扱いし、それ以外は
    filepath を fetch して dbtype で解釈する。tidy CSV をそのまま置くので
    dbtype は "tidy"。where(ファセット絞り込み)は自作リストでは持たない。
    """
    return {
        "value": f"{CUSTOM_WORDLIST_PREFIX}{sid}",
        "text": CUSTOM_WORDLIST_TEXT,
        "filepath": f"{SESSION_WORDLIST_URLDIR}/{sid}.csv",
        "dbtype": "tidy",
    }


def custom_wordlist_sid(payload: dict | None) -> str | None:
    """editor JSON の wordlist.value が custom:<sid> ならその sid を返す。

    editor は読み込んだ wordlist エントリをそのまま書き出す(editor.js の
    exportData)ので、編集を往復しても value は保たれる。
    """
    entry = (payload or {}).get("wordlist") or {}
    value = str(entry.get("value", "")) if isinstance(entry, dict) else ""
    if not value.startswith(CUSTOM_WORDLIST_PREFIX):
        return None
    sid = value[len(CUSTOM_WORDLIST_PREFIX) :]
    return sid if valid_session_id(sid) else None


def is_original_wordlist(payload: dict | None) -> bool:
    """editor JSON の wordlist が「エディタ内の自作リスト」(ORIGINAL)かどうか。"""
    entry = (payload or {}).get("wordlist") or {}
    if not isinstance(entry, dict):
        return False
    return str(entry.get("value", "")) == ORIGINAL_WORDLIST_VALUE


def original_wordlist_text(payload: dict | None) -> str | None:
    """ORIGINAL の単語リスト本体(csvText)。無い・文字列でないなら None。"""
    entry = (payload or {}).get("wordlist") or {}
    if not is_original_wordlist(payload):
        return None
    text = entry.get("csvText")
    return text if isinstance(text, str) else None


def normalized_original_wordlist(payload: dict | None) -> str:
    """ORIGINAL の csvText を検証して、そのまま .csv として置けるテキストを返す。

    id 列は書き換えない(results の id と対応しているため)。画像列は落とす。
    壊れている・そもそも入っていないときは理由付きの ValueError を投げる。
    """
    from .wordlist_csv import WordlistCsvError, parse_editor_text

    text = original_wordlist_text(payload)
    if text is None:
        raise ValueError(
            "自作リストの単語データ(csvText)がありません。"
            "替え歌エディタを開き直して、自作リストを選び直してください。"
        )
    try:
        return parse_editor_text(text).text
    except WordlistCsvError as exc:
        raise ValueError(f"自作リストの単語データ(csvText)が読めません: {exc}") from exc


def write_original_wordlist(payload: dict, project_dir: Path) -> Path:
    """ORIGINAL の csvText を検証して、プロジェクトディレクトリに書き出す。

    以後の工程(単語行の引き当て・曲名の変換)は普通のCSVパスとして扱える。
    """
    path = Path(project_dir) / ORIGINAL_WORDLIST_FILENAME
    path.write_text(normalized_original_wordlist(payload), encoding="utf-8")
    return path


def save_raw(raw: dict, project_dir: Path) -> Path:
    path = project_dir / RAW_FILENAME
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _setting_wordlists() -> list[dict[str, Any]]:
    """conf/setting.json の単語リスト定義を平坦に並べる。

    setting.json のトップレベルには、単体のリストのほかに
    ``{"label": "生物", "items": [...]}`` のグループ(editorの選択肢の見出し)が
    混ざる。グループ内のリスト(動物・ファンタジーなど)も同じように扱えるよう、
    items を展開して1つのリストにする。
    """
    try:
        conf = json.loads(SETTING_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("conf/setting.json が読めません(汎用の単語リスト設定を使います)")
        return []
    return _flatten_wordlists(conf.get("wordlist", []))


def _flatten_wordlists(items: list) -> list[dict[str, Any]]:
    """{label, items:[...]} のグループを再帰的に均してリスト定義だけにする。"""
    out: list[dict[str, Any]] = []
    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("items"), list):  # グループ(label + items)
            out.extend(_flatten_wordlists(entry["items"]))
        else:
            out.append(entry)
    return out


def conf_wordlist_entry(name: str) -> dict[str, Any] | None:
    """conf/setting.json の単語リスト設定を名前(stem)で引く。

    リスト名は filepath(wordlists/<stem>.csv)か value(SEKITSUI などの
    大文字表記)で引き当てる。設定に無い(自作リスト等)なら None。
    """
    for entry in _setting_wordlists() if name else []:
        filepath = str(entry.get("filepath", ""))
        value = str(entry.get("value", ""))
        if filepath.endswith(f"/{name}.csv") or value.lower() == name.lower():
            return dict(entry)
    return None


def _wordlist_entry(name: str, where: str | None) -> dict[str, Any]:
    """editorのconf(setting.json)と同じ形の単語リスト設定を返す。

    設定に無いリストは汎用の設定を組み立てて返す。
    """
    conf_entry = conf_wordlist_entry(name)
    if conf_entry is not None:
        if where is not None:
            conf_entry["where"] = where
        return conf_entry
    entry = {
        "value": name.upper(),
        "text": name,
        "filepath": f"wordlists/{name}.csv",
        "dbtype": "tidy",
    }
    if where is not None:
        entry["where"] = where
    return entry


def named_wordlist_entry(name: str, where: str | None = None) -> dict[str, Any]:
    """名前付き単語リストの editor 設定エントリ(conf/setting.json と同じ形)。

    export_editor が変換結果から組むのと同じもの。変換せず解析だけする経路
    (/api/editor-session の解析のみモード)からも同じ形を返せるようにする。
    """
    return _wordlist_entry(name, where)


def wordlist_display_name(name: str) -> str:
    """単語リストの表示名(conf/setting.json の text。例: stations → 駅名)。

    設定に無いリスト・設定を読めないときはリスト名(stem)をそのまま返す。
    """
    return str(_wordlist_entry(name, None).get("text") or name)


def wordlist_phrase_name(name: str) -> str:
    """文中で使う単語リスト名(「ふるさと を <ここ> で歌ってみた」)。

    wordlist_phrases.json にあればそれを、無ければ表示名(setting.jsonのtext)を返す。
    プルダウン用の短い名前(「日常系」)は単体だと意味が取れないので、
    そういうリストだけ文章向けの言い方(「架空の日常系アニメキャラ名」)を持たせる。
    """
    try:
        phrases = json.loads(WORDLIST_PHRASES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        phrases = {}
    phrase = phrases.get(name) if isinstance(phrases, dict) else None
    return str(phrase) if isinstance(phrase, str) and phrase else wordlist_display_name(name)


def export_editor(
    project: Project,
    project_dir: Path,
    wordlist_entry: dict[str, Any] | None = None,
    weights_list: list[list[float]] | None = None,
) -> Path:
    """editorの「読み込み」で開けるJSONを書き出す。

    wordlist_entry を渡すと、単語リスト設定をそのまま使う(自作リストのように
    conf/setting.json にもリスト名にも紐づかないリスト向け。
    :func:`custom_wordlist_entry` を渡す)。

    weights_list はノート長重み(NOTE_LENGTH_WEIGHT のα由来、行ごとの
    ユニット位置別重み)。α はエンジンパラメータではないので param には
    載らず、計算済みの重みだけを editor へ渡す。editor はこれを再変換の
    weightsPerLine としてそのまま使う(soramimic-editor/1 の追加フィールド)。
    """
    if project.parody is None:
        raise ValueError("替え歌案がありません。先に convert を実行してください")
    raw_path = project_dir / RAW_FILENAME
    if not raw_path.exists():
        raise ValueError(
            f"{raw_path} がありません。convert を実行し直してください"
            "(旧バージョンのconvert結果には編集ツール連携用のデータが含まれません)"
        )
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    # 現在のparodyのlocked状態をresultsに反映する(単語数が一致する行のみ)
    results = [line["words"] for line in raw["lines"]]
    for pline, words in zip(project.parody.lines, results, strict=True):
        if len(pline.words) == len(words):
            for w, raw_w in zip(pline.words, words, strict=True):
                raw_w["locked"] = w.locked

    payload = {
        "format": EXPORT_FORMAT,
        "phrases": raw.get("phrases", [ln.xf_kana for ln in project.lines]),
        "tokensList": raw.get("tokensList", []),
        "results": results,
        "param": project.parody.params,
        "wordlist": wordlist_entry
        or _wordlist_entry(
            resolve_wordlist(project.parody.wordlist).stem, project.parody.where
        ),
        "unitsList": [line["units"] for line in raw["lines"]],
    }
    # ファセットで表せる絞り込みはトップレベルにも載せる。editor は⚙モーダルの
    # チェック状態をここから復元する(restoreFacets)ので、載せないと再変換で
    # conf の既定に戻ってしまう——変換に使ったのと違う条件で作り直される。
    # 表せない形(自作リスト・手書きのwhere)はエントリの where だけ(従来どおり)
    if survives_editor_facets(payload["wordlist"], project.parody.where):
        payload["where"] = project.parody.where
    if weights_list is not None:
        payload["weightsList"] = weights_list
    path = project_dir / EDITOR_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _resolve_preview_wordlist(
    payload: dict, fallback: str | None, sessions_dir: Path | None = None
) -> str | None:
    """プレビュー用の単語リストを決める。

    editor JSONのwordlist情報(filepath: パスそのもの→リスト名stem)を優先し、
    だめならフォームのwordlist名にフォールバックする(import_editorと同じ考え方)。
    自作リスト(value が custom:<sid>)は sessions_dir 配下の正規化済みCSVを使う。
    エディタ内の自作リスト(value が ORIGINAL)は名前でもパスでも引けないので
    None を返す(行は csvText から直接引く。:func:`build_editor_preview` 参照)。
    どれも解決できなければ None(=単語リスト行なしでプレビュー)。
    """
    if is_original_wordlist(payload):
        # フォームのリスト名へフォールバックさせない(別のリストの行が当たるため)
        return None
    sid = custom_wordlist_sid(payload)
    if sid:
        path = session_wordlist_path(sessions_dir, sid)
        return str(path) if path is not None else None
    entry = payload.get("wordlist") or {}
    filepath = entry.get("filepath", "")
    candidates: list[str] = []
    if filepath.endswith(".csv"):
        candidates += [filepath, Path(filepath).stem]
    if fallback and fallback.strip():
        candidates.append(fallback.strip())
    for cand in candidates:
        try:
            resolve_wordlist(cand)
            return cand
        except FileNotFoundError:
            continue
    return None


def build_editor_preview(
    payload: dict,
    wordlist: str | None,
    layout: Layout,
    lyrics: str = "",
    granularity: dict[str, str] | None = None,
    sessions_dir: Path | None = None,
) -> dict[str, Any]:
    """editor書き出しJSONから、実動画と同じキュー順・フィルタのプレビューデータを作る。

    キューは build_image_cues と同じく「このレイアウトで表示できるものがある
    替え歌単語」を行順・単語順(=歌唱順)に並べたもの。各キューには単語の
    data(単語リスト行+替え歌フィールド)・use_fallback・その行の字幕テキスト
    (替え歌/元歌詞)を持たせる。MIDI(音符)なしでも作れるよう、時間ではなく
    行・単語の並びでキュー順を決める(実動画でも歌唱順=この並び)。

    字幕テキストの粒度(行/フレーズ)は video.build_ass と同じ align 側の共通
    ロジックで解決し、動画と一致した元歌詞/替え歌を返す。
    """
    from .align import (
        build_subtitle_segments,
        effective_granularities,
        segment_text_by_line,
    )
    from .convert import _find_row, _load_wordlist_rows, wordlist_rows_from_text
    from .video import WORD_SEP, effective_fallback, word_frame_data, word_is_shown

    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("editorの書き出しファイルではありません(resultsが必要)")
    phrases = payload.get("phrases") or []
    # 元歌詞が与えられていれば実動画(align_lines)と同様に行対応づけし、
    # 字幕の元歌詞をカナ(phrases)ではなく元歌詞の行で出す
    # 元歌詞のルビ記法(｜表層《よみ》)は align_lines と同じく、突き合わせには
    # 記法つきの行(読みに注釈が効く)を、表示には素テキストを使う
    # エディタ側が持つ行ごとの対応づけ(originalLines)は使わない——ブラウザ側の
    # 対応づけは精度が足りず字幕が劣化するため、対応づけは align_lines(こちら)を
    # 正とする(editor.json の originalLines はエディタの表示用)。実動画
    # (import_editor)も同じ方針なので、プレビューと本番の字幕が揃う。
    aligned: list[str | None] = [None] * len(phrases)
    # フォームに元歌詞が無ければ、JSONが持つ生テキストで対応づける
    # (editor.json だけを持ち込んだケース。import_editor と同じ考え方)
    source = lyrics if lyrics.strip() else (editor_lyrics(payload) or "")
    lyric_lines = [ln.strip() for ln in source.splitlines() if ln.strip()]
    if lyric_lines and phrases:
        from .align import align_texts

        assignments = align_texts([str(p) for p in phrases], lyric_lines)
        aligned = [strip_ruby(lyric_lines[a]) if a is not None else None for a in assignments]

    # 粒度に応じて、各行に出す元歌詞/替え歌テキストを video と同じ手順で解決する。
    # プレビューは時間軸を持たないので区間はダミー(表示テキストのみ使う)。
    n_lines = len(results)
    grans = effective_granularities(layout.subtitles, granularity)
    originals: list[str | None] = [aligned[i] if i < len(aligned) else None for i in range(n_lines)]
    xf_texts = [str(phrases[i]) if i < len(phrases) else "" for i in range(n_lines)]
    original_full = [
        (originals[i] or (str(phrases[i]) if i < len(phrases) else "")) for i in range(n_lines)
    ]
    parody_full = [
        WORD_SEP.join(w.get("surface", "") for w in lw) if isinstance(lw, list) else ""
        for lw in results
    ]
    dummy_spans = [(0.0, 0.0)] * n_lines
    original_by_line = segment_text_by_line(
        build_subtitle_segments(
            "original", grans["original"], originals, original_full, xf_texts,
            dummy_spans, sep=WORD_SEP,
        ),
        n_lines,
    )
    parody_by_line = segment_text_by_line(
        build_subtitle_segments(
            "parody", grans["parody"], originals, parody_full, xf_texts,
            dummy_spans, sep=WORD_SEP,
        ),
        n_lines,
    )
    resolved = _resolve_preview_wordlist(payload, wordlist, sessions_dir)
    rows_by_id: dict[str, list[dict[str, str]]] = {}
    if is_original_wordlist(payload):
        # エディタ内の自作リスト。行はJSONに同梱されたCSVから直接引く
        # (ファイルに置かないので resolved は空のまま=画像URLも作らない)
        rows_by_id = wordlist_rows_from_text(normalized_original_wordlist(payload))
    elif resolved is not None:
        rows_by_id = _load_wordlist_rows(resolve_wordlist(resolved))

    cues: list[dict[str, Any]] = []
    for i, line_words in enumerate(results):
        if not isinstance(line_words, list):
            continue
        # 字幕テキストは粒度解決済み(video.build_ass と同じ align 側ロジック)。
        # 元歌詞=行/フレーズ、替え歌=フレーズ/行 のいずれか。
        parody_text = parody_by_line[i]
        original_text = original_by_line[i]
        for w in line_words:
            row = _find_row(rows_by_id, w) or {}
            # 単語リストに行がない単語(未知語・filler)はfallback側で描く
            use_fallback = not row
            word = ParodyWord(
                surface=w.get("surface", ""),
                kana=w.get("kana", ""),
                original=w.get("original", ""),
                original_surface=w.get("original_surface", ""),
                originalkana=w.get("originalkana", ""),
                note_ids=[],
                filler=bool(w.get("filler", False)),
            )
            data = word_frame_data(word, row)
            # 画像列が空の既知語も実動画(build_image_cues)と同じく文字フレームに落とす
            use_fallback = effective_fallback(
                layout, data, use_fallback, has_image=bool(data.get("image"))
            )
            if not word_is_shown(layout, data, use_fallback):
                continue  # このレイアウトでは表示できるものがない単語
            cues.append(
                {
                    "data": data,
                    "use_fallback": use_fallback,
                    "image": data.get("image") or "",
                    "parody_text": parody_text,
                    "original_text": original_text,
                }
            )
    return {"wordlist": resolved or "", "cues": cues}


def import_editor(
    project: Project,
    project_dir: Path,
    file: Path | None = None,
    sessions_dir: Path | None = None,
) -> None:
    """editorが書き出したJSONを取り込み、project.parodyを作り直す。

    convert を経ていないプロジェクトにも取り込める(JSON側のwordlist情報を使う)。
    ブラウザ(soramimic.com)で変換・編集した結果だけを持ち込むケース用で、
    このときローカル/Colab側では変換処理(soramimic ライブラリ)が不要になる。

    sessions_dir は自作リスト(wordlist.value が custom:<sid>)の置き場。
    単語画像は単語リスト行の image 列から引くので、行を引ける正規化済みCSVが
    ここに残っている必要がある(/api/editor-session が置く)。

    エディタの中で選んだ自作リスト(wordlist.value が ORIGINAL)は単語データを
    JSON自体が持っている(csvText)ので、sessions_dir は要らない。

    字幕の元歌詞がまだ無いプロジェクト(align_lines を通っていない)には、
    JSONの ``lyrics``(元歌詞の生テキスト)から対応づけて埋める。
    """
    path = file or (project_dir / EDITOR_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results")
    units_list = payload.get("unitsList")
    if not isinstance(results, list) or not isinstance(units_list, list):
        raise ValueError("editorの書き出しファイルではありません(results/unitsListが必要)")
    if len(results) != len(project.lines):
        raise ValueError(
            f"行数が合いません: editor={len(results)}行, project={len(project.lines)}行"
        )

    lines = [
        {"units": units, "words": words}
        for units, words in zip(units_list, results, strict=True)
    ]
    # 単語リストはJSON側の情報(filepath)が解決できるならそちらを優先し、
    # だめなら既存のparody(convert済みの場合)にフォールバックする
    wordlist = project.parody.wordlist if project.parody else None
    where = project.parody.where if project.parody else None
    entry = payload.get("wordlist") or {}
    filepath = entry.get("filepath", "")
    sid = custom_wordlist_sid(payload)
    if is_original_wordlist(payload):
        # エディタの中で使った自作リスト。単語データ(csvText)がJSONに入って
        # いるので、それをジョブディレクトリに置いてそのまま行の引き当てに使う
        # (results の id と同じ採番のCSVなので、必ず同じ行が当たる)
        wordlist = str(write_original_wordlist(payload, project_dir))
        where = None
    elif sid:
        # 自作リスト。名前では引けないので、変換時に置いたセッションのCSVを使う
        session_csv = session_wordlist_path(sessions_dir, sid)
        if session_csv is None:
            raise ValueError(
                "自作リストの単語データが見つかりません"
                f"(セッション {sid})。替え歌エディタを開き直して"
                "作り直してください"
            )
        wordlist = str(session_csv)
        where = None
    elif filepath.endswith(".csv"):
        # まずパスそのもの、だめならリスト名(stem)で解決を試みる
        for candidate in (filepath, Path(filepath).stem):
            try:
                resolve_wordlist(candidate)
                wordlist = candidate
                # editorが編集中に使っていた絞り込みはトップレベルの where。
                # ファセットのチェックを操作すると editor は wordlist を conf の
                # エントリ(where なし)で置き換えるので、こちらを先に見ないと
                # ユーザーが絞り込みを変えた事実が落ちる(以後の再変換・曲名変換が
                # 別の条件になる)。トップレベルが無い旧JSONはエントリの where。
                top = payload.get("where")
                where = top if isinstance(top, str) else entry.get("where")
                break
            except FileNotFoundError:
                continue
        else:
            logger.warning(
                "editor JSONの単語リスト %s が見つからないため %s を使います", filepath, wordlist
            )
    if wordlist is None:
        raise ValueError(
            "単語リストを特定できません。convert を先に実行するか、"
            "wordlist情報(filepath)を含むeditor JSONを使ってください"
        )
    # 字幕の元歌詞。JSONが持つ行ごとの対応づけ(originalLines)は**採用しない**——
    # ブラウザ側の対応づけは精度が足りず字幕が劣化するため、対応づけは
    # align_lines を正とする(editor.json の originalLines はエディタの表示用)。
    # ただし元歌詞そのもの(生テキスト)は使う: フォーム/プロジェクト側に元歌詞が
    # 無い(= original_text が全行 None。align_lines を通していない)のに JSON が
    # lyrics を持つときは、その生テキストで対応づけて埋める。「editor.json だけを
    # 持ち込んだ」ケースでも字幕が出る。ルビ記法は align_lines が解釈するので
    # 剥がさずそのまま渡す。
    if all(line.original_text is None for line in project.lines):
        lyrics = editor_lyrics(payload)
        if lyrics is not None:
            from .align import align_lines

            align_lines(project, lyrics.splitlines())

    default_params = project.parody.params if project.parody else {}
    apply_converted_lines(
        project,
        lines,
        wordlist=wordlist,
        where=where,
        params=payload.get("param") or default_params,
    )
    # 再書き出しできるよう生応答も更新する
    save_raw(
        {
            "lines": lines,
            "tokensList": payload.get("tokensList", []),
            "phrases": payload.get("phrases", []),
        },
        project_dir,
    )
