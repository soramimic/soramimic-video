import json
from pathlib import Path

import pytest

from soramimic_video.editing import export_edit, import_edit
from soramimic_video.project import (
    Line,
    Note,
    Parody,
    ParodyLine,
    ParodyWord,
    Project,
    SongInfo,
)


def _project_with_parody() -> Project:
    notes = [
        Note(id=i, midi_note=60, start_tick=0, end_tick=1, start_sec=0.0, end_sec=0.1,
             line=0, surface=k, kana=k, raw=k)
        for i, k in enumerate(["シ", "ズ", "ム"])
    ]
    lines = [Line(id=0, xf_surface="シズム", xf_kana="シズム", note_ids=[0, 1, 2])]
    parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="静", kana="シズ", original="静",
                        original_surface="シズ", originalkana="シズ", note_ids=[0, 1],
                    )
                ],
            )
        ],
    )
    return Project(
        song=SongInfo(midi_path="x.mid", ticks_per_beat=480),
        notes=notes, lines=lines, parody=parody,
    )


def test_export_import_roundtrip(tmp_path: Path):
    project = _project_with_parody()
    path = export_edit(project, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["lines"][0]["words"][0]["surface"] = "鈴鹿"
    data["lines"][0]["words"][0]["kana"] = "スズ"
    data["lines"][0]["words"][0]["locked"] = True
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    import_edit(project, tmp_path)
    word = project.parody.lines[0].words[0]
    assert word.surface == "鈴鹿"
    assert word.kana == "スズ"
    assert word.locked is True


def test_import_rejects_too_many_moras(tmp_path: Path):
    project = _project_with_parody()
    path = export_edit(project, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["lines"][0]["words"][0]["kana"] = "スズシイヨ"  # 5モーラ > 2音符
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="超えています"):
        import_edit(project, tmp_path)
