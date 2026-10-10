import pytest

from soramimic_video import parenthetical_ruby
from soramimic_video.parenthetical_ruby import normalize_parenthetical_ruby as normalize
from soramimic_video.ruby import parse, segments, strip_ruby


@pytest.mark.parametrize(("source", "expected"), [
    ("未来（みらい）へ", "｜未来《ミライ》へ"),
    ("僕の紅葉(もみじ)が散る", "僕の｜紅葉《モミジ》が散る"),
    ("明日[あした]", "｜明日《アシタ》"),
    ("明日（あす）", "｜明日《アス》"),
    ("東京［ﾄｰｷｮｰ］", "｜東京《トーキョー》"),
    ("東京（とうきょう）", "｜東京《トウキョウ》"),
    ("想い（おもい）", "｜想い《オモイ》"),
    ("思い出(おもいで)", "｜思い出《オモイデ》"),
    ("生(い)きる", "｜生《イ》きる"),
    ("生(い)きるのは", "｜生《イ》きるのは"),
    ("振(ふ)り返(かえ)る", "｜振《フ》り｜返《カエ》る"),
    ("未来（みらい）(ラララ)", "｜未来《ミライ》(ラララ)"),
    ("｜夢《きぼう》と未来（みらい）", "｜夢《きぼう》と｜未来《ミライ》"),
    ("未来（みらい）\n夢（ゆめ）", "｜未来《ミライ》\n｜夢《ユメ》"),
])
def test_only_dictionary_backed_readings_become_ruby(source, expected):
    assert normalize(source) == expected
    assert normalize(expected) == expected


@pytest.mark.parametrize("source", [
    "未来（ラララ）", "夢（Oh yeah）", "(Oh yeah)", "（ラララ）",
    "夢（君と）", "未来（あす）", "あす（あす）", "ABC（エービーシー）",
    "夢 （ゆめ）", "夢\n（ゆめ）", "夢（ ゆめ ）", "夢（ゆめ！）", "夢（ゆめ ゆめ）",
    "夢（ゆめゆめ）", "夢()", "夢(ゆめ）", "夢（ゆめ", "夢（ゆめ\n）",
    "（夢(ゆめ)）", "夢（（ゆめ））", "【夢（ゆめ）】", r"夢\(ゆめ)",
    "｜未来（みらい）《あす》", "｜夢《きぼう（ゆめ）》", "未来星（ほし）",
    "𠮷夢（ゆめ）",
    "君の夢（きみのゆめ）", "君に会いたい（きみにあいたい）",
])
def test_chorus_uncertain_nested_and_explicit_text_stays_literal(source):
    assert normalize(source) == source


def test_dictionary_failure_does_not_erase_text(monkeypatch):
    def fail(_surface):
        raise RuntimeError("dictionary unavailable")

    monkeypatch.setattr(parenthetical_ruby, "_dictionary_readings", fail)
    assert normalize("未来（みらい）") == "未来（みらい）"


def test_annotation_offsets_readings_and_chorus_survive_together():
    source = "春の紅葉（もみじ）と夢（ゆめ）（ラララ）"
    parsed = parse(normalize(source))
    assert parsed.plain == "春の紅葉と夢（ラララ）"
    assert [(span.start, span.end, span.reading) for span in parsed.spans] == [
        (2, 4, "モミジ"), (5, 6, "ユメ"),
    ]
    assert segments(normalize(source)) == [
        ("春の", None), ("紅葉", "モミジ"), ("と", None), ("夢", "ユメ"), ("（ラララ）", None),
    ]


def test_reading_and_tokenization_do_not_duplicate_the_annotation():
    from soramimic_video.reading import reading_candidates, reading_tokens, text_to_kana
    from soramimic_video.soramimic_engine import run_tokenize

    assert reading_candidates(normalize("紅葉（もみじ）")) == ["モミジ"]
    assert text_to_kana(normalize("未来（みらい）（ラララ）")) == "ミライラララ"
    assert reading_tokens(normalize("紅葉（もみじ）")) == [("紅葉", "モミジ")]
    units = run_tokenize(["紅葉（もみじ）"])[0]
    assert "".join(unit["pronunciation"] for unit in units) == "モミジ"


def test_display_and_automatic_readings_never_reclassify_a_retained_echo():
    from soramimic_video.reading import text_to_kana

    normalized = normalize("夢（ゆめ）（ゆめ）")
    assert normalized == "｜夢《ユメ》（ゆめ）"
    plain = strip_ruby(normalized)
    assert plain == "夢（ゆめ）"
    assert strip_ruby(plain) == plain
    assert text_to_kana(plain) == "ユメユメ"
