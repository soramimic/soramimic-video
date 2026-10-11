from dataclasses import asdict
from types import SimpleNamespace

import pytest

from soramimic_video import subtitle_pages
from soramimic_video.align import SubtitleSegment
from soramimic_video.project import Line, Note, ParodyWord, Project, SongInfo
from soramimic_video.reading import reading_tokens
from soramimic_video.subtitle_pages import (
    SubtitleTrack,
    paginate_subtitle,
    paginate_subtitle_tracks,
)


def _project(text):
    tokens = reading_tokens(text)
    kana = "".join(reading for _, reading in tokens)
    notes = [Note(i, 60, i * 480, (i + 1) * 480, 1 + i * .4, 1 + (i + 1) * .4,
                  0, ch, ch, ch) for i, ch in enumerate(kana)]
    project = Project(SongInfo("", 480), notes,
                      [Line(0, text, kana, list(range(len(notes))), text)])
    words = []
    cursor = 0
    for _surface, reading in tokens:
        if reading:
            end = cursor + len(reading)
            words.append(ParodyWord(f"名前{len(words)}", reading, "", "", "",
                                    list(range(cursor, end))))
            cursor = end
    return project, words


def _pages(project, words, width, joint):
    segment = SubtitleSegment(project.lines[0].original_text, .85,
                              project.notes[-1].end_sec + .15, [0])
    before = asdict(project)
    if joint:
        parody_text = "  ".join(word.surface for word in words)
        tracks = [
            SubtitleTrack(segment, "original", [], len, width),
            SubtitleTrack(SubtitleSegment(parody_text, segment.start, segment.end, [0]),
                          "parody", words, len, 1000),
        ]
        pages, parody = paginate_subtitle_tracks(project, tracks, project.lines)
        assert [(p.start, p.end) for p in pages] == [(p.start, p.end) for p in parody]
        assert [word for page in parody for word in page.words] == words
    else:
        pages = paginate_subtitle(project, segment, "original", project.lines, [], len, width)
    assert asdict(project) == before
    assert "".join("".join(page.text.split()) for page in pages) == "".join(segment.text.split())
    assert all(len(page.text) <= width for page in pages)
    assert all(page.end - page.start >= .2 - 1e-6 for page in pages)
    assert all(a.end == b.start for a, b in zip(pages, pages[1:], strict=False))
    return [page.text for page in pages]


@pytest.mark.parametrize("joint", [False, True], ids=["single", "joint"])
@pytest.mark.parametrize("space", ["", " ", "\u3000", "\n"],
                         ids=["none", "space", "fullwidth", "newline"])
def test_fitting_pages_preserve_bunsetsu_instead_of_balancing_inside_them(joint, space):
    text = f"赤い花を{space}眺めていた{space}風が吹いていた"
    project, words = _project(text)
    assert _pages(project, words, 9 + len(space), joint) == [
        f"赤い花を{space}眺めていた", "風が吹いていた",
    ]


@pytest.mark.parametrize("joint", [False, True], ids=["single", "joint"])
def test_bunsetsu_priority_can_require_more_pages(joint):
    project, words = _project("赤い花を眺めていた風が吹いていた")
    assert _pages(project, words, 8, joint) == ["赤い花を", "眺めていた風が", "吹いていた"]


@pytest.mark.parametrize("joint", [False, True], ids=["single", "joint"])
def test_nearby_word_boundary_does_not_hide_a_fitting_bunsetsu_boundary(joint):
    project, words = _project("赤い花を眺めていた風が吹いていた")
    short_start = len("".join(reading for _, reading in reading_tokens("赤い花を眺めて")))
    short_end = short_start + 2  # い / た lasts only .1 seconds in total.
    cursor = 1.0
    for note in project.notes:
        note.start_sec = cursor
        cursor += .05 if short_start <= note.id < short_end else .4
        note.end_sec = cursor
    assert _pages(project, words, 9, joint) == ["赤い花を眺めていた", "風が吹いていた"]


@pytest.mark.parametrize("joint", [False, True], ids=["single", "joint"])
def test_oversized_bunsetsu_relaxes_only_the_needed_word_boundary(joint):
    project, words = _project("空を歩いていたから見えてきた")
    # 歩いていたから alone exceeds the box; 見えてきた still fits whole.
    assert _pages(project, words, 5, joint) == ["空を歩いて", "いたから", "見えてきた"]


@pytest.mark.parametrize("joint", [False, True], ids=["single", "joint"])
def test_nonparticle_use_of_yori_can_start_a_page(joint):
    project, words = _project("花はより美しく咲いた")
    assert _pages(project, words, 3, joint) == ["花は", "より", "美しく", "咲いた"]


def _joint_tracks_with_one_boundary(text, prefix):
    project, _ = _project(text)
    boundary = len("".join(reading for _, reading in reading_tokens(prefix)))
    words = [
        ParodyWord("アンドロメダ", "", "", "", "", list(range(boundary))),
        ParodyWord("カシオペア", "", "", "", "", list(range(boundary, len(project.notes)))),
    ]
    end = project.notes[-1].end_sec + .15
    tracks = [
        SubtitleTrack(SubtitleSegment(text, .85, end, [0]), "original", [], len, 1000),
        SubtitleTrack(SubtitleSegment("  ".join(w.surface for w in words), .85, end, [0]),
                      "parody", words, len, 6),
    ]
    before = asdict(project)
    original, parody = paginate_subtitle_tracks(project, tracks, project.lines)
    assert asdict(project) == before
    assert [(p.start, p.end) for p in original] == [(p.start, p.end) for p in parody]
    assert [word for page in parody for word in page.words] == words
    return original, parody


def test_parody_overflow_can_require_a_shared_boundary_inside_original_bunsetsu():
    text = "赤い花を眺めていた風が吹いていた"
    prefix = "赤い花を眺めて"
    original, parody = _joint_tracks_with_one_boundary(text, prefix)
    assert [p.text for p in original] == [prefix, "いた風が吹いていた"]
    assert [p.text for p in parody] == ["アンドロメダ", "カシオペア"]


@pytest.mark.parametrize("prefix", ["眺め", "眺めてい"])
def test_fallback_does_not_orphan_particles_or_auxiliaries(prefix):
    original, parody = _joint_tracks_with_one_boundary("眺めていた", prefix)
    assert [p.text for p in original] == ["眺めていた"]
    assert [p.text for p in parody] == ["アンドロメダ  カシオペア"]


def test_phrase_offsets_refer_to_original_text_with_whitespace():
    text = "\u3000赤い花を\u3000眺めていた\r\n"
    phrases, words = subtitle_pages._phrase_boundaries(text)
    assert {text[start:].rstrip() for start in phrases} == {
        "赤い花を\u3000眺めていた", "花を\u3000眺めていた", "眺めていた",
    }
    assert {text[start:].rstrip() for start in words} == {
        "赤い花を\u3000眺めていた", "花を\u3000眺めていた", "眺めていた", "いた",
    }


@pytest.mark.parametrize("failure", ["unavailable", "unmapped"])
def test_unusable_phrase_analysis_keeps_existing_safe_pagination(monkeypatch, failure):
    import jphrase

    def split(*_args, **_kwargs):
        if failure == "unavailable":
            raise RuntimeError("analyzer unavailable")
        return [[{"surface_form": "別の文"}]]

    monkeypatch.setattr(jphrase, "PhraseSplitter",
                        lambda **_: SimpleNamespace(split_text=split))
    text = "赤い花を眺める"
    project, words = _project(text)
    assert subtitle_pages._phrase_boundaries(text) is None
    assert _pages(project, words, 4, True) == ["赤い花を", "眺める"]
