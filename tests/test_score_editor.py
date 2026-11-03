from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from soramimic_video.api import create_app
from soramimic_video.xfparse import analyze_midi

ROOT = Path(__file__).resolve().parents[1]


def test_client_score_exchange() -> None:
    subprocess.run(
        ["node", "--test", "tests/score-editor-io.mjs", "tests/score-editor-ui.mjs"],
        cwd=ROOT, check=True,
    )


def test_client_editor_static_routes(tmp_path: Path) -> None:
    with TestClient(create_app(jobs_dir=tmp_path / "jobs")) as client:
        response = client.get("/score-editor")
        assert response.status_code == 200
        assert 'id="local-score-file"' in response.text
        for script in ("score-editor.mjs", "score-editor-io.mjs"):
            response = client.get("/" + script)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/javascript")


def test_browser_midi_is_accepted_by_video(tmp_path: Path) -> None:
    script = """
      import { openDocument, lyricMidi } from './src/soramimic_video/static/score-editor-io.mjs';
      const input = JSON.parse(await new Response(process.stdin).text());
      const document = openDocument(input);
      process.stdout.write(lyricMidi(document, document.payload.moras));
    """
    moras = [
        {"id": 0, "line": 0, "text": "ソ", "start": 1., "end": 1.5, "pitch": 60},
        {"id": 1, "line": 0, "text": "ラ", "start": 1.5, "end": 2., "pitch": 64},
        {"id": 2, "line": 1, "text": "ミ", "start": 3., "end": 3.5, "pitch": 67},
    ]
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps({"moras": moras}).encode(), capture_output=True, cwd=ROOT, check=True,
    )
    midi = tmp_path / "edited.mid"
    midi.write_bytes(result.stdout)
    project = analyze_midi(midi)
    assert [note.kana for note in project.notes] == ["ソ", "ラ", "ミ"]
    assert [note.midi_note for note in project.notes] == [60, 64, 67]
    assert [note.start_sec for note in project.notes] == [1., 1.5, 3.]
    assert [line.xf_kana for line in project.lines] == ["ソラ", "ミ"]
