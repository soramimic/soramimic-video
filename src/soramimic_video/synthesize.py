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


def build_lyric_map(project: Project) -> dict[int, str]:
    """note_id -> 歌唱カナ。替え歌単語があればその読み、なければ元の読み。"""
    lyric_map = {n.id: n.kana for n in project.notes}
    if project.parody is None:
        logger.warning("替え歌案がないため元の歌詞で合成します")
        return lyric_map
    for pline in project.parody.lines:
        for w in pline.words:
            kana_list = w.note_kana
            if len(kana_list) != len(w.note_ids):
                moras = split_moras(w.kana)
                kana_list = moras[: len(w.note_ids)]
                kana_list += ["ー"] * (len(w.note_ids) - len(kana_list))
            for nid, kana in zip(w.note_ids, kana_list, strict=True):
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
) -> Path | None:
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
