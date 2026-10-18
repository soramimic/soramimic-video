from __future__ import annotations

import io
import xml.etree.ElementTree as ET

import pytest
from mido import MidiFile

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


def test_musicxml_retains_first_note_short_rest_ties_and_tempo_offset(tmp_path):
    output = export_file(project(), tmp_path, "musicxml", tmp_path / "score.musicxml")
    root = ET.fromstring(output.read_text())
    notes = root.findall(".//note")
    assert notes[0].findtext("pitch/step") == "C"
    assert notes[0].findtext("duration") == "120"  # no synthesis head-rest
    assert notes[0].findtext("lyric/text") == "ア"
    assert notes[1].find("rest") is not None
    assert notes[1].findtext("duration") == "20"  # no synthesis gap absorption
    assert root.findtext('.//direction/offset') == '240'
    assert root.find('.//direction/offset').get('sound') == 'yes'
    assert [e.text for e in root.findall('.//lyric/text')] == ["ア", "キ", "ク"]
    assert root.find('.//tie[@type="start"]') is not None
    assert root.find('.//tie[@type="stop"]') is not None


def test_xf_roundtrip_without_private_provenance(tmp_path):
    path = export_file(project(), tmp_path, "xf", tmp_path / "lyrics.xf.mid")
    restored = analyze_midi(path)
    assert [n.kana for n in restored.notes] == ["ア", "キ", "ク"]
    assert [n.start_tick for n in restored.notes] == [0, 140, 240]
    assert not path.with_suffix('.mid.provenance.json').exists()


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
    for kind in ('musicxml', 'midi', 'xf'):
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


def test_musicxml_preserves_overlaps_and_meter_changes(tmp_path):
    value = project()
    value.notes[0].end_tick = 200
    value.song.time_signatures = [[0, 4, 4], [480, 3, 4]]
    output = export_file(value, tmp_path, 'musicxml', tmp_path / 'score.musicxml')
    root = ET.fromstring(output.read_text())
    measures = root.findall('.//measure')
    assert measures[1].findtext('attributes/time/beats') == '3'
    assert measures[0].findtext('backup/duration') == '60'
    # The first partial measure ends exactly at the new meter's tick.
    assert sum(int(e.text) for e in measures[0].findall('note/duration')) - 60 == 480
