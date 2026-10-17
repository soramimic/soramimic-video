"""Original lyric boundaries survive conversion without changing the reading."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from soramimic_video.api import editor_setup_seed
from soramimic_video.conversion_tokens import project_reading
from soramimic_video.convert import (
    convert_project,
    engine_phrases,
    project_conversion_tokens,
)
from soramimic_video.kana import split_fine_moras
from soramimic_video.project import Line, Note, Project, SongInfo
from soramimic_video.soramimic_engine import run_tokenize


def lyric_project(text, kana):
    moras = split_fine_moras(kana)
    notes = [Note(i, 60, i * 480, (i + 1) * 480, i * .5, (i + 1) * .5,
                  0, mora, mora, mora) for i, mora in enumerate(moras)]
    return Project(SongInfo("x.mid", 480), notes=notes,
                   lines=[Line(0, text, kana, list(range(len(notes))), original_text=text)])


@pytest.mark.parametrize(("text", "kana", "expected", "phrases"), [
    ("零にたして　今をかけて", "ゼロニタシテイマヲカケテ",
     ["ゼ", "ロ", "ニ", "タ", "シ", "テ", "イ", "マ", "ヲ", "カ", "ケ", "テ"],
     [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3]),
    ("手を打つ", "テオウツ", ["テ", "オ", "ウ", "ツ"], [0, 0, 1, 1]),
    ("時計を売る", "トケイオウル", ["ト", "ケイ", "オ", "ウ", "ル"], [0, 0, 0, 1, 1]),
])
def test_server_and_editor_keep_word_and_phrase_boundaries(tmp_path, text, kana, expected, phrases):
    project = lyric_project(text, kana)
    original = copy.deepcopy(project)
    csv = tmp_path / "words.csv"
    csv.write_text("id,original,surface,pronunciation\n0,時計,時計,トケイ", encoding="utf-8")
    raw = convert_project(project, str(csv), params={"NOTE_LENGTH_WEIGHT": 1})
    seed = editor_setup_seed(
        original, wordlist=str(csv), where=None, params={"NOTE_LENGTH_WEIGHT": "1"},
    )
    browser_units = run_tokenize(seed["phrases"], tokens_list=seed["tokensList"])
    assert [[(t["surface_form"], t["pronunciation"], t["phrase"]) for t in row]
            for row in seed["tokensList"]] == [
        [(t["surface_form"], t["pronunciation"], t["phrase"]) for t in row]
        for row in raw["tokensList"]
    ]
    assert browser_units == [line["units"] for line in raw["lines"]]
    assert [u["pronunciation"] for u in browser_units[0]] == expected
    assert [u["phrase"] for u in browser_units[0]] == phrases
    assert "".join(u["surface_form"] for u in browser_units[0]) == text
    assert "".join(expected) == kana
    assert len(seed["noteLengthRawList"][0]) == len(expected)
    assert project.notes == original.notes and project.lines == original.lines
    assert sorted(n for w in project.parody.lines[0].words for n in w.note_ids) == list(
        range(len(project.notes))
    )


def test_projection_preserves_word_boundaries_inside_one_phrase():
    tokens = [dict(surface_form="て", pronunciation="テ", phrase=0),
              dict(surface_form="いる", pronunciation="イル", phrase=0)]
    expected = [dict(t, reading=t["pronunciation"]) for t in tokens]
    assert project_reading(tokens, "テイル") == expected
    assert all("reading" not in t for t in tokens)


@pytest.mark.parametrize(("parts", "kana"), [
    (["ア", "イ"], "アキイ"),  # insertion at the proposed token boundary
    (["アカ", "キウ"], "アサシウ"),  # boundary lies inside an unmatched reading
    (["キ", "ャ"], "キャ"),  # boundary inside one mora
    (["カ"], "アカ"),  # unowned leading reading
])
def test_projection_does_not_guess_ambiguous_boundaries(parts, kana):
    tokens = [dict(surface_form=p, pronunciation=p, phrase=i) for i, p in enumerate(parts)]
    assert project_reading(tokens, kana) is None


def test_unmatched_surface_keeps_confirmed_reading_and_reports_fallback(caplog):
    project = lyric_project("別の文章", "テイマ")
    tokens = project_conversion_tokens(project)
    units = run_tokenize(engine_phrases(project), tokens_list=tokens)[0]
    assert "".join(u["pronunciation"] for u in units) == "テイマ"
    assert "単語境界" in caplog.text


def test_xf_fragments_use_corresponding_source_words():
    project = lyric_project("手を打つ", "テオウツ")
    project.lines = [
        Line(0, "テオ", "テオ", [0, 1], original_text="手を打つ", original_line_index=0),
        Line(1, "ウツ", "ウツ", [2, 3], original_text="手を打つ", original_line_index=0),
    ]
    tokens = project_conversion_tokens(project)
    assert ["".join(t["surface_form"] for t in line) for line in tokens] == ["手を", "打つ"]
    assert ["".join(t["pronunciation"] for t in line) for line in tokens] == ["テオ", "ウツ"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for browser parity")
def test_pinned_browser_engine_uses_same_surface_reading_and_phrases():
    cases = []
    for text, kana in [("零にたして　今をかけて", "ゼロニタシテイマヲカケテ"),
                       ("手を打つ", "テオウツ"), ("時計を売る", "トケイオウル")]:
        project = lyric_project(text, kana)
        tokens = project_conversion_tokens(project)
        units = run_tokenize(engine_phrases(project), tokens_list=tokens)
        cases.append({"tokens": tokens[0], "expected": units[0]})
    script = """
        import fs from 'node:fs';
        import assert from 'node:assert/strict';
        import {createSoramimic} from './frontend/src/lib/index.js';
        const json = file => JSON.parse(fs.readFileSync('data/' + file, 'utf8'));
        console.log = () => {};
        const app = createSoramimic({
            kanjiDict: json('kanjiyomi.json'), englishDict: json('english-kana.json'),
            romanTree: json('tree_roma2kana.json'), kana2phonon: json('kana2phonon.json'),
            vowelSimilarity: json('simVowelsMonoTie.json'),
            consonantSimilarity: json('simConsonantsMonoTie.json'),
            tokenizeSentenses: () => { throw Error('Confirmed tokens must be reused'); },
            getYomi: t => t,
        });
        for (const c of JSON.parse(fs.readFileSync(0, 'utf8'))) {
            const units = app.textAnalyzer.getYomiAndPhraseBreak(c.tokens).map(u => ({
                surface_form: u.surface_form, pronunciation: u.pronunciation, phrase: u.phrase,
            }));
            assert.deepEqual(units, c.expected);
        }
    """
    subprocess.run(["node", "--input-type=module", "-e", script],
                   cwd=Path(__file__).resolve().parents[1] / "external/soramimic",
                   input=json.dumps(cases), text=True, capture_output=True, check=True)
