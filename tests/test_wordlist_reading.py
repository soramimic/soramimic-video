"""Wordlist readings use yomi before conversion and reject invalid outputs."""

from __future__ import annotations

import pytest

from soramimic_video import wordlist_csv
from soramimic_video.soramimic_engine import _get_app
from soramimic_video.wordlist_reading import wordlist_pronunciation, wordlist_reading


@pytest.mark.parametrize("input_text", ["メカX", "メカＸ", "メカ2", "メカ２", "ノ"])
def test_omitted_wordlist_readings_use_yomi(input_text):
    app = _get_app()
    source = (
        "id,original,surface,pronunciation\n"
        f"1,{input_text},{input_text},"
    )
    db = app.word_list.parse_tidy(source, max_units=8)
    words = [w for bucket in db.values() for w in bucket]
    assert {w["id"] for w in words} == {"1"}
    assert {w["kana"] for w in words} == {wordlist_reading(input_text)}
    assert not any(char in "XＸ2２" for w in words for char in w["kana"])


def test_explicit_reading_takes_precedence_over_surface():
    app = _get_app()
    db = app.word_list.parse_tidy(
        "id,original,surface,pronunciation\n1,メカ2,メカ2,メカツー", max_units=8
    )
    assert {w["kana"] for bucket in db.values() for w in bucket} == {"メカツー"}


def test_csv_checks_all_entries_including_inferred_readings(monkeypatch):
    seen = []

    def read(text):
        seen.append(text)
        return "ネコ"

    monkeypatch.setattr(wordlist_csv, "wordlist_reading", read)
    monkeypatch.setattr(wordlist_csv, "wordlist_pronunciation", read)
    source = "surface,pronunciation\nメカＸ,\n表記,メカ２\n"
    result = wordlist_csv.parse(source.encode())
    assert seen == ["メカＸ", "メカ２"]
    assert result.auto_reading_rows == 1
    assert "メカＸ" in result.text and "メカ２" in result.text


def test_csv_reports_multiple_bad_readings_and_their_sources():
    with pytest.raises(wordlist_csv.WordlistCsvError) as exc:
        wordlist_csv.parse("surface,pronunciation\n❤,\n対象,???\n".encode())
    message = str(exc.value)
    assert "2行目「❤」" in message
    assert "自動推定" in message
    assert "3行目「対象」" in message
    assert "指定した読み" in message
    assert "読み欄" in message


def test_plain_na_reading_uses_surface_for_validation():
    with pytest.raises(wordlist_csv.WordlistCsvError) as exc:
        wordlist_csv.parse("❤,NA".encode())
    assert "1行目「❤」" in str(exc.value)
    assert "自動推定" in str(exc.value)


def test_editor_csv_does_not_skip_reading_validation():
    with pytest.raises(wordlist_csv.WordlistCsvError, match="2行目「対象」"):
        wordlist_csv.parse_editor_text("id,surface,pronunciation\n17,対象,???")


@pytest.mark.parametrize("editor", [False, True])
def test_error_line_matches_uploaded_file_after_blanks_and_quoted_newlines(editor):
    source = 'id,surface,pronunciation,note\n\n1,ネコ,ネコ,"two\nlines"\n\n2,対象,???,\n'
    with pytest.raises(wordlist_csv.WordlistCsvError, match="6行目「対象」"):
        if editor:
            wordlist_csv.parse_editor_text(source)
        else:
            wordlist_csv.parse(source.encode())


@pytest.mark.parametrize("style", ["tidy", "plain", "editor"])
@pytest.mark.parametrize("reading, expected", [
    ("はない", "ハナイ"), ("ハナイ", "ハナイ"), ("はへを", "ハヘヲ"),
    ("ｳｯｾｪﾜ", "ウッセエワ"), ("ドーー", "ドーオ"),
    ("ティーファイル", "ティーファイル"), ("キャァ", "キャア"),
    ("トウキョウ", "トウキョウ"), ("フュージョン", "フュージョン"),
])
def test_supplied_reading_survives_upload_and_conversion(style, reading, expected):
    if style == "plain":
        source = f"花井,{reading}"
        parsed = wordlist_csv.parse(source.encode())
    else:
        source = f"id,original,surface,pronunciation\n17,花井,花井,{reading}"
        parsed = (wordlist_csv.parse_editor_text(source) if style == "editor"
                  else wordlist_csv.parse(source.encode()))
    db = _get_app().word_list.parse_tidy(parsed.text, max_units=12)
    assert {w["kana"] for bucket in db.values() for w in bucket} == {expected}
    assert {w["surface"] for bucket in db.values() for w in bucket} == {"花井"}
    assert wordlist_pronunciation(reading) == expected


def test_provenance_keeps_sentence_inference_separate_from_supplied_reading():
    source = (
        "id,original,surface,pronunciation\n"
        "1,はない,はない,\n2,はない,はない,はない\n3,はない,はない,NA"
    )
    db = _get_app().word_list.parse_tidy(source, max_units=8)
    assert {(w["id"], w["kana"]) for bucket in db.values() for w in bucket} == {
        ("1", "ワナイ"), ("2", "ハナイ"), ("3", "ワナイ"),
    }


@pytest.mark.parametrize("reading", [
    "はないX", "メガリザードンX", "ポリゴン2", "ケルヴィン卿", "カ ナ", "ハナイ・ティー",
])
def test_non_kana_supplied_reading_requires_correction(reading):
    with pytest.raises(ValueError, match="指定した読み"):
        wordlist_pronunciation(reading)
    with pytest.raises(wordlist_csv.WordlistCsvError, match="1行目「花井」.*指定した読み"):
        wordlist_csv.parse(f"花井,{reading}".encode())
    with pytest.raises(ValueError, match="2行目「花井」.*指定した読み"):
        _get_app().word_list.parse_tidy(
            f"id,original,surface,pronunciation\n17,花井,花井,{reading}", max_units=12,
        )
