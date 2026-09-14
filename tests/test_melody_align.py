from soramimic_video.melody_align import MelodyNote, monophony_ratio, skyline


def _note(start: float, end: float, pitch: int) -> MelodyNote:
    return MelodyNote(start_sec=start, end_sec=end, midi_note=pitch)


def test_monophony_ratio():
    mono = [_note(0, 1, 60), _note(1, 2, 62), _note(2, 3, 64)]
    poly = [_note(0, 2, 60), _note(1, 3, 64), _note(2, 4, 67)]
    assert monophony_ratio(mono) == 1.0
    assert monophony_ratio(poly) == 0.0


def test_skyline_keeps_top_voice():
    notes = [
        _note(0.0, 1.0, 60),
        _note(0.0, 1.0, 64),
        _note(0.0, 1.0, 67),
        _note(0.0, 1.0, 72),
    ]
    assert [note.midi_note for note in skyline(notes)] == [72]


def test_skyline_truncates_when_higher_note_enters():
    result = skyline([_note(0.0, 2.0, 60), _note(1.0, 2.0, 67)])
    assert [
        (note.start_sec, note.end_sec, note.midi_note) for note in result
    ] == [(0.0, 1.0, 60), (1.0, 2.0, 67)]


def test_skyline_keeps_sequential_notes():
    notes = [_note(0.0, 1.0, 60), _note(1.0, 2.0, 62), _note(2.0, 3.0, 64)]
    assert len(skyline(notes)) == 3
