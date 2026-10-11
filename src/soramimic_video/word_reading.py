"""Restore a selected word's reading after allocating its search pronunciation."""

from __future__ import annotations

from .kana import split_fine_moras


def restore_word_reading(kana: str, note_kana: list[str]) -> list[str]:
    """Keep the word's full reading on its already selected, ordered notes.

    Search pronunciations can replace a mora with a long vowel or delete it.
    Align the full reading to the allocated pronunciation to retain its attacks
    and use spare continuation notes where available. With no spare note, put
    the missing mora on the preceding note (the first note for a missing prefix).
    Only additional vowel holds may remain between the restored word moras.
    """
    moras = split_fine_moras(kana)
    if not moras:
        return list(note_kana)
    if not note_kana:
        raise ValueError("単語の読みを復元する音符がありません")

    allocated = [
        (mora, index)
        for index, reading in enumerate(note_kana)
        for mora in (split_fine_moras(reading) or [""])
    ]
    # Exact attacks take precedence over replacing a hold, which takes
    # precedence over stacking a missing mora. Earliest equal-cost positions
    # keep a word's first attack at the start of its available notes.
    rows, cols = len(moras), len(allocated)
    costs = [[(0, 0)] * (cols + 1) for _ in range(rows + 1)]
    steps = [[""] * (cols + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        costs[i][0] = (2 * i, 0)
        steps[i][0] = "restore"
    for j in range(1, cols + 1):
        old = allocated[j - 1][0]
        cost, position = costs[0][j - 1]
        costs[0][j] = (cost + (0 if old in {"", "ー"} else 3), position)
        steps[0][j] = "hold"
    for i, mora in enumerate(moras, 1):
        for j, (old, _note) in enumerate(allocated, 1):
            replacement = 0 if mora == old else 1 if old in {"", "ー"} else 4
            cost, position = costs[i - 1][j - 1]
            choices = [((cost + replacement, position + j), "match")]
            cost, position = costs[i - 1][j]
            choices.append(((cost + 2, position), "restore"))
            cost, position = costs[i][j - 1]
            choices.append(((cost + (0 if old in {"", "ー"} else 3), position), "hold"))
            costs[i][j], steps[i][j] = min(choices, key=lambda choice: choice[0])

    edits: list[tuple[str, int, int]] = []
    i, j = rows, cols
    while i or j:
        step = steps[i][j]
        edits.append((step, i, j))
        if step == "match":
            i, j = i - 1, j - 1
        elif step == "restore":
            i -= 1
        else:
            j -= 1

    restored = [""] * len(note_kana)
    for step, i, j in reversed(edits):
        if step == "match":
            restored[allocated[j - 1][1]] += moras[i - 1]
        elif step == "restore":
            note = allocated[max(0, j - 1)][1]
            restored[note] += moras[i - 1]
        elif allocated[j - 1][0] == "ー":
            restored[allocated[j - 1][1]] += "ー"
    return restored
