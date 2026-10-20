from __future__ import annotations

import io
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from mido import MidiFile
from xfmido import XFMidiFile

from soramimic_video.cli import main
from soramimic_video.exports import FORMATS, export_file, result_path, save_results
from soramimic_video.project import Note, Parody, ParodyLine, ParodyWord, Project, SongInfo
from soramimic_video.xfparse import analyze_midi


def project():
    return Project(
        song=SongInfo("", 480, tempo_map=[[0, 500000], [240, 600000]], key_shift=2),
        notes=[Note(0, 60, 0, 120, 0, .125, 0, "カ", "カ", "カ"),
               Note(1, 62, 140, 240, .146, .25, 1, "キ", "キ", "キ"),
               Note(2, 64, 240, 2400, .25, 2.95, 1, "ク", "ク", "ク")],
        parody=Parody("synthetic", lines=[ParodyLine(0, [ParodyWord(
            surface="ア", kana="ア", original="", original_surface="", originalkana="",
            note_ids=[0], note_kana=["ア"],
        )])]),
    )


def events(midi):
    tick = 0
    result = []
    for event in midi.tracks[0]:
        tick += event.time
        result.append((tick, event))
    return result


def test_midi_contains_current_lyrics_original_pitch_and_exact_times(tmp_path):
    output = export_file(project(), tmp_path, "midi", tmp_path / "lyrics.mid")
    midi = MidiFile(output, charset="utf8")
    result = events(midi)
    assert [(t, e.note) for t, e in result if e.type == "note_on"] == [
        (0, 60), (140, 62), (240, 64),
    ]
    assert [(t, e.text) for t, e in result if e.type == "lyrics"] == [
        (0, "ア"), (140, "\nキ"), (240, "ク"),
    ]
    assert [(t, e.tempo) for t, e in result if e.type == "set_tempo"] == [
        (0, 500000), (240, 600000),
    ]
    assert [(t, e.note) for t, e in result if e.type == "note_off"] == [
        (120, 60), (240, 62), (2400, 64),
    ]


def test_xf_roundtrip_without_private_provenance(tmp_path):
    path = export_file(project(), tmp_path, "xf", tmp_path / "lyrics.xf.mid")
    restored = analyze_midi(path)
    assert [n.kana for n in restored.notes] == ["ア", "キ", "ク"]
    assert [n.start_tick for n in restored.notes] == [0, 140, 240]
    assert not path.with_suffix('.mid.provenance.json').exists()


def test_xf_declares_its_format_version_sections_and_lyric_encoding(tmp_path):
    path = export_file(project(), tmp_path, "xf", tmp_path / "lyrics.xf.mid")
    midi = XFMidiFile(str(path), charset="cp932")
    assert midi.type == 0
    assert len(midi.tracks) == 1
    result = events(midi)
    assert [(t, bytes(e.data)) for t, e in result if e.type == "sequencer_specific"] == [
        (0, bytes.fromhex("43 7b 00 58 46 30 32 00 19")),
    ]
    assert result[0][1].type == "sequencer_specific"
    assert [(t, e.text) for t, e in result if e.type == "cue_marker"] == [
        (0, "$Lyrc:1:0:JP"),
    ]
    assert midi.xfih[0].type == "text"
    assert midi.xfih[0].text == "XFhd:"
    assert midi.xfih[0].time == 0
    assert midi.xfkm[0].type == "cue_marker"
    assert midi.xfkm[0].text == "$Lyrc:1:0:JP"
    assert midi.xfkm[0].time == 0
    # Both lyric representations describe the same syllables at the same ticks.
    tick = 0
    karaoke = []
    for event in midi.xfkm:
        tick += event.time
        if event.type == "lyrics" and event.text != "/":
            karaoke.append((tick, event.text))
    assert karaoke == [(t, e.text.lstrip("\n")) for t, e in result if e.type == "lyrics"]


def test_xf_does_not_advertise_standard_lyrics_when_none_are_present(tmp_path):
    value = project()
    value.parody = None
    for note in value.notes:
        note.kana = ""
    path = export_file(value, tmp_path, "xf", tmp_path / "empty-lyrics.mid")
    midi = XFMidiFile(str(path), charset="cp932")
    assert not any(e.type == "lyrics" for _, e in events(midi))
    version = next(e for _, e in events(midi) if e.type == "sequencer_specific")
    assert bytes(version.data) == bytes.fromhex("43 7b 00 58 46 30 32 00 11")
    assert len([e for _, e in events(midi) if e.type == "note_on"]) == 3


def test_xf_also_restores_lyrics_without_an_xf_aware_reader(tmp_path):
    path = export_file(project(), tmp_path, "xf", tmp_path / "lyrics.xf.mid")
    ordinary = MidiFile(path, charset="cp932")
    assert [(t, e.text) for t, e in events(ordinary) if e.type == "lyrics"] == [
        (0, "ア"), (140, "\nキ"), (240, "ク"),
    ]
    # Saving with an ordinary SMF reader removes the XF extension chunks.
    stripped = tmp_path / "standard.mid"
    ordinary.save(stripped)
    assert b"XFKM" not in stripped.read_bytes()
    restored = analyze_midi(stripped)
    assert [n.kana for n in restored.notes] == ["ア", "キ", "ク"]
    assert [(n.midi_note, n.start_tick, n.end_tick) for n in restored.notes] == [
        (60, 0, 120), (62, 140, 240), (64, 240, 2400),
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("full_song", [False, True])
def test_xf_is_importable_by_bundled_soramimic(tmp_path, full_song):
    root = Path(__file__).resolve().parents[1]
    value = analyze_midi(root / "src/soramimic_video/static/sample/furusato_full.mid") \
        if full_song else project()
    path = export_file(value, tmp_path, "xf", tmp_path / "lyrics.xf.mid")
    parser = root / "external/soramimic/frontend/src/xfMidi.js"
    source = """
        import {readFileSync} from 'node:fs';
        const {parseXfMidi} = await import(process.argv[1]);
        const bytes = readFileSync(process.argv[2]);
        console.log(JSON.stringify(parseXfMidi(
            bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength))));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source, parser.as_uri(), str(path)],
        check=True, capture_output=True, text=True,
    )
    imported = json.loads(result.stdout)
    restored = analyze_midi(path)
    assert imported["noteCount"] == len(restored.notes)
    assert [line["kana"] for line in imported["lines"]] == [
        line.xf_kana for line in restored.lines
    ]
    assert imported["warnings"] == []


def test_all_results_use_synthesized_vocal_and_fixed_allowlist(tmp_path):
    from test_api import fake_wav

    original = fake_wav()
    (tmp_path / "neutrino").mkdir()
    (tmp_path / "neutrino/vocal.wav").write_bytes(original)
    (tmp_path / "mix").mkdir()
    (tmp_path / "mix/song.wav").write_bytes(b"accompaniment must not be exported")
    save_results(project(), tmp_path)
    assert all(result_path(tmp_path, kind) for kind in FORMATS)
    assert result_path(tmp_path, 'vocal').read_bytes() == original
    assert result_path(tmp_path, '../project.json') is None
    assert result_path(tmp_path, 'provenance') is None
    path = result_path(tmp_path, 'midi')
    path.unlink()
    path.symlink_to(tmp_path / 'neutrino/vocal.wav')
    assert result_path(tmp_path, 'midi') is None


def test_vocal_requires_synthesis(tmp_path):
    with pytest.raises(FileNotFoundError, match="先に歌唱合成"):
        export_file(project(), tmp_path, "vocal", tmp_path / "out.wav")


@pytest.mark.parametrize('kind', FORMATS)
def test_cli_exports_each_format(tmp_path, kind):
    value = project()
    value.save(tmp_path)
    (tmp_path / 'neutrino').mkdir()
    (tmp_path / 'neutrino/vocal.wav').write_bytes(b'vocal')
    output = tmp_path / 'output' / FORMATS[kind][0]
    assert main(['export', '--project', str(tmp_path), '--format', kind,
                 '--output', str(output)]) == 0
    assert output.is_file()


def test_unknown_lyric_keeps_note_in_score_exports(tmp_path):
    value = project()
    value.notes[1].kana = ''
    for kind in ('midi', 'xf'):
        assert export_file(value, tmp_path, kind, tmp_path / FORMATS[kind][0]).is_file()
    midi = MidiFile(file=io.BytesIO((tmp_path / 'lyrics.mid').read_bytes()), charset='utf8')
    assert len([e for _, e in events(midi) if e.type == 'note_on']) == 3


def test_public_downloads_survive_cleanup_restart_and_expire_with_job(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from soramimic_video import api
    from test_api import FAKE_MIDI, fake_wav, wait_done

    monkeypatch.setenv(api.PUBLIC_ENV, '1')
    monkeypatch.setenv(api.JOB_TTL_HOURS_ENV, '1')
    monkeypatch.setattr(api, 'song_seconds', lambda _: 0)

    def pipeline(job, config):
        (job.dir / 'neutrino').mkdir()
        (job.dir / 'neutrino/vocal.wav').write_bytes(fake_wav())
        project().save(job.dir)
        save_results(project(), job.dir)
        out = job.dir / 'out.mp4'
        out.write_bytes(b'video')
        return out

    monkeypatch.setattr(api, 'run_pipeline', pipeline)
    app = api.create_app(jobs_dir=tmp_path / 'jobs')
    client = TestClient(app)
    other = TestClient(app)
    response = client.post('/api/jobs', files={'midi': ('song.mid', FAKE_MIDI)},
                           data={'wordlist': 'stations'})
    job_id = response.json()['id']
    body = wait_done(client, job_id)
    assert body['status'] == 'done', body
    assert set(body['export_urls']) == set(FORMATS)
    job = app.state.manager.jobs[job_id]
    assert not (job.dir / 'input.mid').exists()
    assert not (job.dir / 'project.json').exists()
    assert not (job.dir / 'neutrino').exists()
    for kind, url in body['export_urls'].items():
        result = client.get(url)
        assert result.status_code == 200
        assert result.content == result_path(job.dir, kind).read_bytes()
        assert result.headers['content-disposition'].startswith('attachment;')
        assert result.headers['cache-control'] == 'private, no-store'
        assert other.get(url).status_code == 404
    ranged = client.get(body['export_urls']['vocal'], headers={'Range': 'bytes=0-15'})
    assert ranged.status_code == 206
    assert ranged.content == fake_wav()[:16]
    assert client.get(f'/api/jobs/{job_id}/exports/provenance').status_code == 404
    xml = ET.fromstring(client.get(body['export_urls']['musicxml']).content)
    assert [e.text for e in xml.findall('.//lyric/text')] == ['ア', 'キ', 'ク']
    restarted = TestClient(api.create_app(jobs_dir=tmp_path / 'jobs'))
    restarted.cookies.update(client.cookies)
    assert restarted.get(f'/api/jobs/{job_id}').json()['export_urls'] == body['export_urls']
    assert restarted.get(body['export_urls']['vocal']).content == fake_wav()
    manager = restarted.app.state.manager
    manager.jobs[job_id].finished_at = time.time() - 7200
    assert manager.cleanup_expired() == [job_id]
    assert not job.dir.exists()
    assert restarted.get(body['export_urls']['vocal']).status_code == 404


def test_exports_require_api_key_and_completed_job(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from soramimic_video import api

    monkeypatch.setenv(api.API_KEY_ENV, 'test-key')
    app = api.create_app(jobs_dir=tmp_path / 'jobs')
    job_dir = tmp_path / 'jobs' / 'test'
    job_dir.mkdir()
    job = api.Job(id='test', dir=job_dir, params={})
    app.state.manager.jobs[job.id] = job
    browser = TestClient(app)
    url = '/api/jobs/test/exports/midi'
    assert browser.get(url).status_code == 401
    assert browser.get(url, headers={'X-API-Key': 'test-key'}).status_code == 409
    job.status = 'done'
    assert browser.get(url, headers={'X-API-Key': 'test-key'}).status_code == 404



def test_musicxml_uses_common_score_and_does_not_reuse_stale_synthesis(tmp_path):
    from soramimic_video.musicxml import build_musicxml
    from soramimic_video.synthesize import build_lyric_map

    stale = tmp_path / 'neutrino/score.musicxml'
    stale.parent.mkdir()
    stale.write_text('stale score')
    value = project()
    output = export_file(value, tmp_path, 'musicxml', tmp_path / 'score.musicxml')
    assert output.read_text() == build_musicxml(value, build_lyric_map(value))


def test_musicxml_can_preserve_actual_neutrino_input(tmp_path, monkeypatch):
    from soramimic_video import synthesize as synth

    value = project()
    captured = []

    def run(xml_path, *args, **kwargs):
        captured.append(xml_path.read_bytes())
        return tmp_path / 'neutrino/vocal.wav'

    monkeypatch.setattr(synth, 'run_neutrino', run)
    synth.synthesize(value, tmp_path, transpose=-12, auto_octave=False)
    output = export_file(value, tmp_path, 'musicxml', tmp_path / 'score.musicxml',
                         musicxml_source=tmp_path / 'neutrino/score.musicxml')
    assert output.read_bytes() == captured[0]
    root = ET.fromstring(output.read_bytes())
    assert root.findtext('.//pitch/octave') == '3'


def test_unknown_export_format_is_rejected(tmp_path):
    output = tmp_path / 'out'
    with pytest.raises(ValueError, match='Unknown export format'):
        export_file(project(), tmp_path, 'unknown', output)
    assert not output.exists()
