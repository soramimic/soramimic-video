import re
from dataclasses import asdict

import pytest

from soramimic_video import subtitle_pages
from soramimic_video.align import SubtitleSegment
from soramimic_video.layout import parse_layout
from soramimic_video.project import Line, Note, Parody, ParodyLine, ParodyWord, Project, SongInfo
from soramimic_video.subtitle_pages import paginate_subtitle
from soramimic_video.video import _ass_text_width, build_ass


def _project(text="アオイ ソラオ ミアゲテ ユックリ アルコウ"):
    kana = "".join(text.split())
    notes = [Note(i, 60, i * 480, (i + 1) * 480, 1 + i * .4, 1.4 + i * .4,
                  0, ch, ch, ch) for i, ch in enumerate(kana)]
    return Project(SongInfo("", 480), notes, [Line(0, text, kana, list(range(len(notes))), text)])


def _pages(project, width, source="original", words=None):
    text = ("  ".join(w.surface for w in words) if words
            else project.lines[0].original_text)
    segment = SubtitleSegment(text, .85, project.notes[-1].end_sec + .15, [0])
    return paginate_subtitle(project, segment, source, project.lines, words or [], len, width)


def test_original_pages_preserve_all_text_and_follow_notes(monkeypatch):
    text = "アオイ ソラオ\nミアゲテ ユックリ アルコウ"
    monkeypatch.setattr(subtitle_pages, "_default_reader",
                        lambda text: [(t, "" if t.isspace() else t)
                                      for t in re.findall(r"\s+|\S+", text)])
    project = _project(text)
    # A long pause must not shift the split to an equal fraction of the line duration.
    for note in project.notes[6:]:
        note.start_sec += 4
        note.end_sec += 4
    before = asdict(project)
    pages = _pages(project, 10)
    assert len(pages) >= 3
    assert "".join("".join(page.text.split()) for page in pages) == "".join(text.split())
    assert all(len(page.text) <= 10 for page in pages)
    assert pages[0].text == "アオイ ソラオ"
    assert pages[1].start == pytest.approx(project.notes[6].start_sec - .15)
    assert all(a.end == b.start for a, b in zip(pages, pages[1:], strict=False))
    assert asdict(project) == before


def test_short_caption_keeps_original_group_and_needs_no_reading(monkeypatch):
    def unexpected(_text):
        raise AssertionError("short captions must not be tokenized")

    monkeypatch.setattr(subtitle_pages, "_default_reader", unexpected)
    project = _project()
    pages = _pages(project, 1000)
    assert len(pages) == 1
    assert pages[0].text == project.lines[0].original_text


def test_fallback_keeps_english_words_and_japanese_punctuation(monkeypatch):
    monkeypatch.setattr(subtitle_pages, "_default_reader", lambda text: [])
    project = _project("青空へ、 walking slowly 帰ろう。")
    pages = _pages(project, 10)
    assert len(pages) > 1
    assert all(not page.text.startswith(("、", "。")) for page in pages)
    assert any("walking" in page.text for page in pages)
    assert any("slowly" in page.text for page in pages)
    assert "".join("".join(page.text.split()) for page in pages) == "".join(
        project.lines[0].original_text.split())


def test_parody_pages_keep_words_and_shared_notes_together():
    project = _project()
    words = [ParodyWord(f"名前{i}", "ナマエ", "", "", "", [i * 3, i * 3 + 1, i * 3 + 2])
             for i in range(6)]
    words[1].note_ids = words[0].note_ids[:]
    project.parody = Parody("test", lines=[ParodyLine(0, words)])
    before = asdict(project)
    pages = _pages(project, 10, "parody", words)
    assert len(pages) > 1
    assert [word for page in pages for word in page.words] == words
    assert pages[0].words[:2] == words[:2]
    assert all(len(page.text) <= 10 for page in pages)
    assert asdict(project) == before


def test_unsplittable_time_range_retains_full_caption(monkeypatch):
    monkeypatch.setattr(subtitle_pages, "_default_reader", lambda text: [])
    project = _project()
    for note in project.notes:
        note.start_sec = 1
        note.end_sec = 2
    pages = _pages(project, 4)
    assert len(pages) == 1
    assert pages[0].text == project.lines[0].original_text


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_ass_pages_fit_subtitle_box_and_keep_timing(monkeypatch, align):
    monkeypatch.setattr(subtitle_pages, "_default_reader",
                        lambda text: [(t, t) for t in text.split()])
    project = _project("サクラ " * 20)
    layout = parse_layout({"elements": [
        {"type": "subtitle", "source": "original", "box": [.2, .8, .6, .1],
         "size": .1, "align": align},
    ]})
    ass = build_ass(project, 640, 360, "Font", layout)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) > 1
    assert all("\\fs" not in event for event in events)
    texts = [event.rsplit("}", 1)[1] for event in events]
    assert "".join(texts).replace(" ", "") == "サクラ" * 20
    from soramimic_video.layout import resolve_font_path

    assert all(_ass_text_width(resolve_font_path(None), 36, text) <= .6 * 640 - 8
               for text in texts)
    assert all(a.split(",")[2] == b.split(",")[1]
               for a, b in zip(events, events[1:], strict=False))


def test_ass_single_oversized_word_fits_without_cutting_it():
    project = _project("Supercalifragilisticexpialidocious")
    layout = parse_layout({"elements": [
        {"type": "subtitle", "source": "original", "box": [.4, .8, .2, .1], "size": .1},
    ]})
    ass = build_ass(project, 640, 360, "Font", layout)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 1
    assert "\\fs" in events[0]
    assert events[0].endswith(project.lines[0].original_text)


@pytest.mark.parametrize("ruby", [False, True])
def test_ass_parody_pages_keep_every_word_with_its_ruby(ruby):
    project = _project()
    names = ["青空", "海風", "山道", "星空", "森", "川", "月", "花"]
    words = [ParodyWord(name, "アオゾラ", "", "", "", [i * 2, i * 2 + 1])
             for i, name in enumerate(names)]
    project.parody = Parody("test", lines=[ParodyLine(0, words)])
    layout = parse_layout({"elements": [
        {"type": "subtitle", "source": "parody", "box": [.35, .8, .3, .1],
         "size": .1, "ruby": ruby},
    ]})
    ass = build_ass(project, 640, 360, "Font", layout)
    bodies = [s.split(",", 9) for s in ass.splitlines()
              if s.startswith("Dialogue:") and ",Parody,," in s]
    annotations = [s.split(",", 9) for s in ass.splitlines()
                   if s.startswith("Dialogue:") and ",Parody,Ruby," in s]
    assert len({tuple(b[1:3]) for b in bodies}) > 1
    assert all("\\fs" not in b[9] for b in bodies)
    assert "".join(b[9].rsplit("}", 1)[1].replace(" ", "") for b in bodies) == "".join(names)
    if ruby:
        assert [a[1:3] for a in annotations] == [b[1:3] for b in bodies]
    else:
        assert annotations == []


@pytest.mark.parametrize("source", ["original", "parody"])
@pytest.mark.parametrize("width", [5, 1000])
def test_interludes_split_even_short_captions_and_preserve_later_words(monkeypatch, source, width):
    monkeypatch.setattr(subtitle_pages, "_default_reader",
                        lambda text: [(t, t) for t in text.split()])
    project = _project("アオイ ソラ ヒカル ホシ シロイ クモ")
    for note in project.notes:
        offset = 20 if note.id >= 10 else 10 if note.id >= 5 else 0
        note.start_sec += offset
        note.end_sec += offset
    words = [ParodyWord(t, t, "", "", "", list(range(a, b)))
             for t, a, b in [("アオイ", 0, 3), ("ソラ", 3, 5), ("ヒカル", 5, 8),
                              ("ホシ", 8, 10), ("シロイ", 10, 13), ("クモ", 13, 15)]]
    text = "  ".join(w.surface for w in words)
    segment = SubtitleSegment(text, .85, 30, [0])
    before = asdict(project)
    # Deliberately unordered/overlapping ranges must still describe two breaks.
    clear = [(18, 24.9), (5.9, 12.9), (17.9, 20)]
    pages = paginate_subtitle(project, segment, source, project.lines, words, len, width,
                              clear_ranges=clear)
    assert "".join(p.text.replace(" ", "") for p in pages) == text.replace(" ", "")
    assert all(not (p.start < end and start < p.end)
               for p in pages for start, end in clear)
    assert any(p.start >= 12.9 and "ヒカル" in p.text for p in pages)
    assert any(p.start >= 24.9 and "シロイ" in p.text for p in pages)
    assert all("ヒカル" not in p.text and "シロイ" not in p.text
               for p in pages if p.start < 5.9)
    if source == "parody":
        assert [w for page in pages for w in page.words] == words
    assert asdict(project) == before


def test_clear_range_covering_entire_caption_emits_no_page():
    project = _project()
    segment = SubtitleSegment("アオイ", 1, 2, [0])
    assert paginate_subtitle(project, segment, "original", project.lines, [], len, 100,
                             clear_ranges=[(0, 3)]) == []
