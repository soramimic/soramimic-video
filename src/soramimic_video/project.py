"""project.json のデータモデルと入出力。

パイプラインの全ステージは、プロジェクトディレクトリ直下の project.json を
唯一の受け渡しファイルとして読み書きする(DESIGN.md参照)。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROJECT_FILENAME = "project.json"
SCHEMA_VERSION = 1


@dataclass
class SongInfo:
    midi_path: str  # 音源プロジェクト(analyze-audio)では空文字
    ticks_per_beat: int
    melody_channel: int | None = None  # $Lyrcヘッダの値(1始まり)。無ければ自動判定
    time_offset: int = 0
    language: str = "JP"
    tempo_map: list[list[int]] = field(default_factory=list)  # [tick, us/beat]
    # [tick, 分子, 分母]
    time_signatures: list[list[int]] = field(default_factory=lambda: [[0, 4, 4]])
    # analyze-audio(歌唱音源入力)のとき設定される
    audio_path: str | None = None  # 入力音源
    vocals_path: str | None = None  # demucs分離後のボーカル
    accompaniment_path: str | None = None  # demucs分離後の伴奏(mixで使用)


@dataclass
class Note:
    """歌唱モーラ = 歌詞イベントが対応づいたメロディ音符。"""

    id: int
    midi_note: int
    start_tick: int
    end_tick: int
    start_sec: float
    end_sec: float
    line: int
    surface: str  # XF歌詞の表記部分(継続モーラでは空文字)
    kana: str  # 読み(カタカナ正規化)
    raw: str  # XFKMイベントの生テキスト


@dataclass
class Line:
    """XFの行(`/` 区切り)。"""

    id: int
    xf_surface: str
    xf_kana: str
    note_ids: list[int]
    original_text: str | None = None  # アライメントで対応づいた元歌詞の行


@dataclass
class ParodyWord:
    surface: str
    kana: str
    original: str  # 単語リストの original 列(画像等の紐付けキー)
    original_surface: str  # 置き換え対象になった元歌詞側の表記
    originalkana: str
    note_ids: list[int]
    note_kana: list[str] = field(default_factory=list)  # 音符ごとの歌唱カナ(note_idsと同長)
    wordlist_row: dict[str, Any] | None = None  # image列などを含むCSV行
    locked: bool = False


@dataclass
class ParodyLine:
    line_id: int
    words: list[ParodyWord] = field(default_factory=list)


@dataclass
class Parody:
    wordlist: str
    where: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    lines: list[ParodyLine] = field(default_factory=list)


@dataclass
class Project:
    song: SongInfo
    notes: list[Note] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    parody: Parody | None = None
    version: int = SCHEMA_VERSION

    # ---- 参照ヘルパ ----

    def note_by_id(self, note_id: int) -> Note:
        return self.notes[note_id]

    def word_time_range(self, word: ParodyWord) -> tuple[float, float]:
        """単語が歌われる区間 [start_sec, end_sec]。"""
        notes = [self.notes[i] for i in word.note_ids]
        return notes[0].start_sec, notes[-1].end_sec

    def line_time_range(self, line: Line) -> tuple[float, float]:
        notes = [self.notes[i] for i in line.note_ids]
        return notes[0].start_sec, notes[-1].end_sec

    # ---- 入出力 ----

    def save(self, project_dir: Path) -> Path:
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / PROJECT_FILENAME
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, project_dir: Path) -> Project:
        data = json.loads((project_dir / PROJECT_FILENAME).read_text(encoding="utf-8"))
        if data.get("version") != SCHEMA_VERSION:
            raise ValueError(f"未対応のproject.jsonバージョン: {data.get('version')}")
        song = SongInfo(**data["song"])
        notes = [Note(**n) for n in data["notes"]]
        lines = [Line(**ln) for ln in data["lines"]]
        parody = None
        if data.get("parody"):
            p = data["parody"]
            parody = Parody(
                wordlist=p["wordlist"],
                where=p.get("where"),
                params=p.get("params", {}),
                lines=[
                    ParodyLine(
                        line_id=pl["line_id"],
                        words=[ParodyWord(**w) for w in pl["words"]],
                    )
                    for pl in p["lines"]
                ],
            )
        return cls(song=song, notes=notes, lines=lines, parody=parody)
