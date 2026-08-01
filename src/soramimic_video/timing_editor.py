"""モーラ単位のタイミング編集GUI(ピアノロール)。

project.json の notes(歌唱モーラ)を、音高と長さが見えるピアノロール上で直接
編集するためのローカルGUIと、その入出力。

* :func:`build_payload` … project.json → GUIが読むJSON
* :func:`apply_payload` … GUIが返したJSON → project.notes / project.lines
* :func:`serve` … 2つを繋ぐ簡易HTTPサーバー(標準ライブラリのみ)

GUI本体は ``static/timing_editor.html``(単一HTML。外部依存もビルド手順も無い)。
音源の波形は ffmpeg があるときだけ表示する(無くても編集はできる)。
"""

from __future__ import annotations

import array
import http.server
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .project import Line, Note, Project, SongInfo

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
EDITOR_HTML = STATIC_DIR / "timing_editor.html"

DEFAULT_PORT = 8765
ENVELOPE_RATE = 100  # 波形表示の解像度(点/秒)
_DEFAULT_TEMPO = 500_000  # us/beat(テンポ指定が無いMIDIの既定=120BPM)


# ---- テンポマップ ----


def _tempo_map(song: SongInfo) -> list[tuple[int, int]]:
    events = [(int(t), int(us)) for t, us in (song.tempo_map or [])]
    return sorted(events)


def tick_to_sec(song: SongInfo, tick: int) -> float:
    """tick → 秒(テンポ変化を追う)。"""
    tpb = song.ticks_per_beat or 480
    sec = 0.0
    prev, cur = 0, _DEFAULT_TEMPO
    for t, us in _tempo_map(song):
        if t >= tick:
            break
        sec += (t - prev) / tpb * cur / 1e6
        prev, cur = t, us
    return sec + (tick - prev) / tpb * cur / 1e6


def sec_to_tick(song: SongInfo, sec: float) -> int:
    """秒 → tick(:func:`tick_to_sec` の逆)。"""
    tpb = song.ticks_per_beat or 480
    acc = 0.0
    prev, cur = 0, _DEFAULT_TEMPO
    for t, us in _tempo_map(song):
        span = (t - prev) / tpb * cur / 1e6
        if acc + span >= sec:
            break
        acc += span
        prev, cur = t, us
    return int(round(prev + (sec - acc) * 1e6 / cur * tpb))


def grid_lines(song: SongInfo, until_sec: float) -> dict[str, list]:
    """小節線と拍線の時刻(秒)。GUIの補助線に使う。"""
    tpb = song.ticks_per_beat or 480
    sigs = [tuple(int(v) for v in s) for s in (song.time_signatures or [])]
    if not sigs:
        sigs = [(0, 4, 4)]
    sigs.sort()
    measures: list[list[float]] = []
    beats: list[float] = []
    tick, idx, number = 0, 0, 1
    while number <= 10_000:
        while idx + 1 < len(sigs) and sigs[idx + 1][0] <= tick:
            idx += 1
        _, num, den = sigs[idx]
        num = max(1, num)
        beat_ticks = max(1, tpb * 4 // max(1, den))
        sec = tick_to_sec(song, tick)
        if sec > until_sec:
            break
        measures.append([round(sec, 4), number])
        for b in range(1, num):
            beats.append(round(tick_to_sec(song, tick + b * beat_ticks), 4))
        tick += num * beat_ticks
        number += 1
    return {"measures": measures, "beats": beats}


# ---- 音源の波形 ----


def audio_envelope(path: Path, rate: int = ENVELOPE_RATE) -> dict[str, Any] | None:
    """音源のピーク包絡(0..1)。ffmpegが無ければNone(波形なしで動かす)。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning("ffmpeg が無いので波形表示を省略します")
        return None
    sr = 8000
    try:
        raw = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path),
             "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
            capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("波形を読めませんでした(%s): %s", path, exc)
        return None
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    hop = max(1, sr // rate)
    values: list[int] = []
    peak = 1
    for i in range(len(samples) // hop):
        block = samples[i * hop : (i + 1) * hop]
        v = max(max(block), -min(block))
        values.append(v)
        peak = max(peak, v)
    return {"rate": rate, "v": [round(v / peak, 3) for v in values]}


# ---- project.json ↔ GUI ----


def build_payload(
    project: Project,
    *,
    reference: list[list[float]] | None = None,
    envelope: dict[str, Any] | None = None,
    has_audio: bool = False,
) -> dict[str, Any]:
    """GUIが読むJSONを組み立てる。

    ``reference`` は背景にうっすら出す参照音符 ``[[開始秒, 終了秒, 音高], ...]``
    (元MIDIのメロディなど)。省略時は編集前の音符をそのまま参照にする。
    """
    moras: list[dict[str, Any]] = []
    seen: dict[int, int] = {}
    for note in sorted(project.notes, key=lambda n: n.start_sec):
        index = seen.get(note.line, 0)
        seen[note.line] = index + 1
        moras.append({
            "id": note.id,
            "line": note.line,
            "i": index,
            "text": note.kana,
            "start": round(note.start_sec, 4),
            "end": round(note.end_sec, 4),
            "pitch": note.midi_note,
        })
    if reference is None:
        reference = [[m["start"], m["end"], m["pitch"]] for m in moras]
    end_sec = max((m["end"] for m in moras), default=0.0)
    return {
        "moras": moras,
        "reference": reference,
        "grid": grid_lines(project.song, end_sec + 5.0),
        "rms": envelope,
        "has_audio": has_audio,
        "line_texts": {
            str(line.id): line.original_text or line.xf_surface for line in project.lines
        },
    }


def apply_payload(project: Project, payload: dict[str, Any]) -> dict[str, Any]:
    """GUIが返したJSONを project.notes / project.lines に反映する。

    モーラは開始秒の順に並べ直して ``Note.id`` を振り直す。``id`` が元の音符を
    指しているものは ``surface`` / ``raw``(XFの表記)を引き継ぎ、GUIで増やした
    ぶんは継続モーラと同じく空にする。行は詰めて0番から振り直す。

    替え歌(``project.parody``)は音符IDと行IDを参照しているため、モーラの数や
    行の割り当てが変わったときは破棄する(変換をやり直せば作り直せる)。
    """
    raw_moras = payload.get("moras") or []
    if not raw_moras:
        raise ValueError("moras が空です")
    raw_moras = sorted(raw_moras, key=lambda m: float(m["start"]))

    old_by_id = {n.id: n for n in project.notes}
    used: set[int] = set()
    notes: list[Note] = []
    for new_id, item in enumerate(raw_moras):
        src_id = item.get("id")
        src = None
        if isinstance(src_id, int) and src_id in old_by_id and src_id not in used:
            src = old_by_id[src_id]
            used.add(src_id)
        start = float(item["start"])
        end = max(float(item["end"]), start + 0.01)
        kana = str(item.get("text") or "")
        notes.append(Note(
            id=new_id,
            midi_note=int(item.get("pitch", src.midi_note if src else 60)),
            start_tick=sec_to_tick(project.song, start),
            end_tick=sec_to_tick(project.song, end),
            start_sec=start,
            end_sec=end,
            line=int(item.get("line", src.line if src else 0)),
            surface=src.surface if src else "",
            kana=kana,
            raw=src.raw if src else kana,
        ))

    # 行: 空になった行は捨てて0番から振り直す(Line.id == project.lines の添字)
    old_lines = {ln.id: ln for ln in project.lines}
    order = sorted({n.line for n in notes})
    renumber = {old: new for new, old in enumerate(order)}
    for note in notes:
        note.line = renumber[note.line]
    lines: list[Line] = []
    for old_id in order:
        base = old_lines.get(old_id)
        note_ids = [n.id for n in notes if n.line == renumber[old_id]]
        lines.append(Line(
            id=renumber[old_id],
            xf_surface=base.xf_surface if base else "",
            xf_kana="".join(notes[i].kana for i in note_ids),
            note_ids=note_ids,
            original_text=base.original_text if base else None,
        ))

    structural = (
        len(notes) != len(project.notes)
        or len(lines) != len(project.lines)
        or any(a.line != b.line or a.kana != b.kana
               for a, b in zip(notes, sorted(project.notes, key=lambda n: n.start_sec)))
    )
    dropped = False
    if structural and project.parody is not None:
        project.parody = None
        dropped = True
        logger.warning("モーラの構成が変わったため替え歌(parody)を破棄しました")

    project.notes = notes
    project.lines = lines
    return {"notes": len(notes), "lines": len(lines), "parody_dropped": dropped}


def _resolve_audio(project: Project, project_dir: Path, audio: Path | None) -> Path | None:
    if audio is not None:
        return audio if audio.exists() else None
    for candidate in (project.song.vocals_path, project.song.audio_path):
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = project_dir / path
        if path.exists():
            return path
    return None


def reference_from_midi(midi_path: Path, channel: int | None = None) -> list[list[float]]:
    """参照表示用に、メロディMIDIから ``[開始秒, 終了秒, 音高]`` を作る。"""
    from .melody_align import load_midi_notes

    by_channel = load_midi_notes(midi_path)
    if channel is None:
        channel = max(by_channel, key=lambda c: len(by_channel[c]))
    return [
        [round(n.start_sec, 4), round(n.end_sec, 4), n.midi_note]
        for n in sorted(by_channel[channel], key=lambda n: n.start_sec)
    ]


# ---- 試聴(選択行のソロ合成 / 全体の作り直し) ----


def synthesize_line(
    project: Project,
    moras: list[dict[str, Any]],
    *,
    engine_url: str | None = None,
    style_id: int = 3003,
    transpose: int = 0,
    lead_sec: float = 0.3,
) -> tuple[bytes, float]:
    """選択行のモーラだけをVOICEVOXで歌わせる(未保存の編集をそのまま試聴する用)。

    戻り値は ``(wavのバイト列, wavの先頭が対応する曲中の秒)``。曲頭からの長い
    休符を作らないよう、行の少し手前を0秒として合成する。移調は曲全体の音域で
    決めるので(``octave_keys``)、本番の合成と同じキーで鳴る。
    """
    from . import voicevox

    if not moras:
        raise ValueError("moras が空です")
    ordered = sorted(moras, key=lambda m: float(m["start"]))
    # 先頭は必ず休符から始める(VOICEVOXは最初の音符に子音があると400を返す)
    offset = float(ordered[0]["start"]) - lead_sec
    song = dataclass_replace(project.song, key_shift=0)
    notes: list[Note] = []
    for i, item in enumerate(ordered):
        start = float(item["start"]) - offset
        end = max(float(item["end"]) - offset, start + 0.02)
        notes.append(Note(
            id=i,
            midi_note=int(item.get("pitch", 60)),
            start_tick=sec_to_tick(song, start),
            end_tick=sec_to_tick(song, end),
            start_sec=start,
            end_sec=end,
            line=0,
            surface="",
            kana=str(item.get("text") or ""),
            raw="",
        ))
    preview = Project(
        song=song,
        notes=notes,
        lines=[Line(id=0, xf_surface="", xf_kana="".join(n.kana for n in notes),
                    note_ids=[n.id for n in notes])],
    )
    with TemporaryDirectory() as tmp:
        wav = voicevox.run_voicevox(
            preview,
            Path(tmp),
            engine_url=engine_url or voicevox.DEFAULT_ENGINE_URL,
            style_id=style_id,
            transpose=transpose,
            octave_keys=[n.midi_note for n in project.notes] or None,
        )
        return wav.read_bytes(), offset


def rebuild(project_dir: Path, options: dict[str, Any]) -> Path:
    """編集後のproject.jsonで歌唱合成→伴奏ミックスまでやり直し、ミックスwavを返す。"""
    from .mix import mix
    from .synthesize import synthesize

    project = Project.load(project_dir)
    extra: dict[str, Any] = {}
    if options.get("engine_url"):  # 未指定なら synthesize 側の既定に任せる
        extra["voicevox_url"] = options["engine_url"]
    synthesize(
        project,
        project_dir,
        model=options.get("model", "MERROW"),
        transpose=options.get("transpose", 0),
        synthesizer=options.get("synthesizer", "voicevox"),
        voicevox_style=options.get("style_id", 3003),
        auto_octave=options.get("auto_octave", True),
        **extra,
    )
    project.save(project_dir)  # 自動移調が決めたkey_shiftを伴奏へ渡す
    return mix(project, project_dir, soundfont=options.get("soundfont"))


def _start_rebuild(state: dict[str, Any]) -> None:
    job = state["job"]

    def worker() -> None:
        try:
            out = rebuild(state["project_dir"], state["options"])
            job.update(state="done", message=f"完了: {out.name}", mixed=out)
            logger.info("作り直し完了: %s", out)
        except Exception as exc:  # GUIに理由を出す
            logger.exception("作り直しに失敗しました")
            job.update(state="error", message=str(exc))

    job.update(state="running", message="合成中…", mixed=job.get("mixed"))
    threading.Thread(target=worker, daemon=True).start()


# ---- サーバー ----


def _make_handler(state: dict[str, Any]) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.debug("%s - %s", self.address_string(), fmt % args)

        # -- 返信ヘルパ --

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str) -> None:
            try:
                size = path.stat().st_size
            except OSError:
                self._json(404, {"error": "not found"})
                return
            rng = self.headers.get("Range")
            with path.open("rb") as fh:
                if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
                    start = int(m.group(1) or 0)
                    stop = min(int(m.group(2) or size - 1), size - 1)
                    fh.seek(start)
                    body = fh.read(max(0, stop - start + 1))
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{stop}/{size}")
                else:
                    body = fh.read()
                    self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- ルート --

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/":
                self._file(EDITOR_HTML, "text/html; charset=utf-8")
            elif path == "/data":
                project = Project.load(state["project_dir"])
                self._json(200, build_payload(
                    project,
                    reference=state["reference"],
                    envelope=state["envelope"],
                    has_audio=state["audio"] is not None,
                ))
            elif path == "/rebuild_status":
                job = state["job"]
                self._json(200, {"state": job["state"], "message": job["message"],
                                 "has_mix": job.get("mixed") is not None})
            elif path == "/mixed" and state["job"].get("mixed") is not None:
                self._file(state["job"]["mixed"], "audio/wav")
            elif path == "/audio" and state["audio"] is not None:
                audio: Path = state["audio"]
                ctype = "audio/mpeg" if audio.suffix.lower() == ".mp3" else "audio/wav"
                self._file(audio, ctype)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            if path == "/synth":
                try:
                    project = Project.load(state["project_dir"])
                    opts = state["options"]
                    wav, offset = synthesize_line(
                        project, json.loads(body).get("moras") or [],
                        engine_url=opts.get("engine_url"),
                        style_id=opts.get("style_id", 3003),
                        transpose=opts.get("transpose", 0),
                    )
                except Exception as exc:
                    logger.warning("行の試聴合成に失敗しました: %s", exc)
                    self._json(500, {"error": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.send_header("X-Start", str(offset))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(wav)
                return
            if path == "/rebuild":
                if state["job"]["state"] == "running":
                    self._json(409, {"error": "すでに実行中です"})
                else:
                    _start_rebuild(state)
                    self._json(200, {"state": "running"})
                return
            if path != "/save":
                self._json(404, {"error": "not found"})
                return
            try:
                payload = json.loads(body)
                project_dir: Path = state["project_dir"]
                project = Project.load(project_dir)
                info = apply_payload(project, payload)
                stamp = time.strftime("%m%d-%H%M%S")
                current = project_dir / "project.json"
                if current.exists():
                    shutil.copy(current, current.with_suffix(f".json.bak-{stamp}"))
                project.save(project_dir)
                info["backup"] = stamp
                logger.info("保存しました: %d音符 / %d行", info["notes"], info["lines"])
                self._json(200, info)
            except Exception as exc:  # GUIに理由を返す
                logger.exception("保存に失敗しました")
                self._json(400, {"error": str(exc)})

    return Handler


def serve(
    project_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    audio: Path | None = None,
    reference_midi: Path | None = None,
    options: dict[str, Any] | None = None,
) -> None:
    """編集GUIをローカルで配信する(Ctrl-Cで終了)。

    ``options`` は試聴・作り直しに渡す合成設定(synthesizer/model/soundfont/
    engine_url/style_id/transpose)。
    """
    project = Project.load(project_dir)
    audio_path = _resolve_audio(project, project_dir, audio)
    state: dict[str, Any] = {
        "project_dir": project_dir,
        "audio": audio_path,
        "envelope": audio_envelope(audio_path) if audio_path else None,
        "reference": reference_from_midi(reference_midi) if reference_midi else None,
        "options": options or {},
        "job": {"state": "idle", "message": "", "mixed": None},
    }
    logger.info(
        "モーラ%d個 / 行%d個 / 音源%s",
        len(project.notes), len(project.lines), audio_path or "なし",
    )
    server = http.server.ThreadingHTTPServer((host, port), _make_handler(state))
    shown = host if host != "0.0.0.0" else "<このマシンのIP>"  # noqa: S104
    logger.info("編集GUI: http://%s:%d/  (Ctrl-Cで終了)", shown, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("終了します")
    finally:
        server.server_close()
