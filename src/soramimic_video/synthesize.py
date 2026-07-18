"""歌唱合成ステージ: 替え歌歌詞 → MusicXML → NEUTRINO → vocal.wav。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .kana import split_moras
from .musicxml import build_musicxml
from .neutrino import run_neutrino
from .project import Project

logger = logging.getLogger(__name__)

NEUTRINO_DIR = "neutrino"


def vocal_path(project_dir: Path) -> Path:
    """合成した歌唱wavの正規パス。バックエンド(NEUTRINO/VOICEVOX)共通。

    ミックスはこの1箇所の定義を参照する(mix.pyが同じ関数を使う)。
    ディスク上の場所は歴史的経緯で neutrino/ 配下だが、両バックエンド共通。
    """
    return project_dir / NEUTRINO_DIR / "vocal.wav"


def build_lyric_map(project: Project) -> dict[int, str]:
    """note_id -> 歌唱カナ。替え歌単語があればその読み、なければ元の読み。"""
    lyric_map = {n.id: n.kana for n in project.notes}
    if project.parody is None:
        logger.warning("替え歌案がないため元の歌詞で合成します")
        return lyric_map
    # 替え歌単語が同じ音符を二重に取ると後勝ち上書きで先行単語の末尾モーラが
    # 潰れる(convert側で解消済みのはずだが、将来の同種バグ検出のため記録する)
    assigned_by: dict[int, str] = {}
    for pline in project.parody.lines:
        for w in pline.words:
            kana_list = w.note_kana
            if len(kana_list) != len(w.note_ids):
                moras = split_moras(w.kana)
                kana_list = moras[: len(w.note_ids)]
                kana_list += ["ー"] * (len(w.note_ids) - len(kana_list))
            for nid, kana in zip(w.note_ids, kana_list, strict=True):
                if nid in assigned_by and assigned_by[nid] != w.surface:
                    logger.warning(
                        "音符%d が複数の替え歌単語に割り当てられています"
                        "(%r を %r が上書き)。先行単語の歌唱が欠落します",
                        nid, assigned_by[nid], w.surface,
                    )
                assigned_by[nid] = w.surface
                lyric_map[nid] = kana
    return lyric_map


def synthesize(
    project: Project,
    project_dir: Path,
    model: str = "MERROW",
    threads: int = 4,
    transpose: int = 0,
    dry_run: bool = False,
    progress_cb: Callable[[float], None] | None = None,
    synthesizer: str = "neutrino",
    voicevox_url: str = "http://127.0.0.1:50021",
    voicevox_style: int = 3003,
    voicevox_auto_octave: bool = True,
) -> Path | None:
    """歌唱合成を実行して vocal.wav のパスを返す。

    synthesizer で使うバックエンドを選ぶ("neutrino" 既定 / "voicevox")。
    """
    if synthesizer == "voicevox":
        from .voicevox import run_voicevox

        if dry_run:
            return None
        return run_voicevox(
            project,
            project_dir,
            engine_url=voicevox_url,
            style_id=voicevox_style,
            transpose=transpose,
            auto_octave=voicevox_auto_octave,
            progress_cb=progress_cb,
        )
    if synthesizer != "neutrino":
        raise ValueError(f"未対応の合成エンジンです: {synthesizer}")

    work_dir = project_dir / NEUTRINO_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    xml_path = work_dir / "score.musicxml"
    xml_path.write_text(
        build_musicxml(project, build_lyric_map(project), transpose=transpose),
        encoding="utf-8",
    )
    logger.info("MusicXMLを書き出しました: %s", xml_path)
    return run_neutrino(
        xml_path,
        work_dir,
        model=model,
        threads=threads,
        dry_run=dry_run,
        progress_cb=progress_cb,
    )
