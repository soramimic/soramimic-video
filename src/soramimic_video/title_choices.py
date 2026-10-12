"""Selectable title word sequences, ranked with the balance preset."""

from __future__ import annotations

import csv
import heapq
import io
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_WORDS = 4
CHOICE_COUNT = 12
SELECTION_MAX_BYTES = 4096


def parse_title_words(value: str) -> list[dict[str, str]] | None:
    """An empty value preserves automatic titles; explicit choices contain row keys only."""
    if not value.strip():
        return None
    try:
        if len(value.encode("utf-8")) > SELECTION_MAX_BYTES:
            raise ValueError
        words = json.loads(value)
        if not isinstance(words, list) or not 1 <= len(words) <= MAX_WORDS:
            raise ValueError
        for word in words:
            if not isinstance(word, dict) or set(word) != {"id", "surface"}:
                raise ValueError
            if any(not isinstance(v, str) or not v.strip() or len(v) > 200
                   for v in word.values()):
                raise ValueError
        return words
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("タイトルの単語を候補から選び直してください") from exc


def selected_rows(
    words: list[dict[str, str]], csv_text: str, where: str | None = None,
) -> list[dict[str, str]]:
    """Resolve selected identities against eligible CSV rows, never client image URLs."""
    from soramimic.word_list import Parser

    # Validate callers other than the HTTP form as well.
    parse_title_words(json.dumps(words, ensure_ascii=False))
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)
    if where:
        try:
            filtered = Parser().filter(where, headers, [
                [r.get(key) or "" for key in headers] for r in rows
            ])
        except (ValueError, IndexError, TypeError, RecursionError) as exc:
            raise ValueError("単語リストのフィルターを確認してください") from exc
        rows = (
            [dict(zip(headers, values, strict=True)) for values in filtered]
            if isinstance(filtered, list) else []
        )
    by_key = {(r.get("id"), r.get("surface")): r for r in rows}
    try:
        return [by_key[(w["id"], w["surface"])] for w in words]
    except KeyError as exc:
        raise ValueError(
            "タイトルの単語が選択中のリストにありません。候補を選び直してください"
        ) from exc


def generate_choices(title: str, csv_path: Path, where: str | None) -> list[dict[str, Any]]:
    """Keep a small beam of complete word sequences, including alternate segmentations."""
    from .convert import resolve_convert_settings
    from .soramimic_engine import _get_app, _get_db, _max_variation_units, run_tokenize
    from .thumbnail import _title_conversion_within_budget

    where, params, _ = resolve_convert_settings(csv_path, where)
    if not title.strip() or not _title_conversion_within_budget(title, params):
        return []
    lines = run_tokenize([title], params)
    units = lines[0] if lines else []
    if not units:
        return []
    app = _get_app(params["VOWEL_RATIO"])
    db = _get_db(app, "0.80", csv_path, where or "", _max_variation_units(lines), cache=False)
    distances = app.kana_similarity.get_kana_similarity(params)
    target = [unit["pronunciation"] for unit in units]
    # (cost, selected words). The worker bounds both runtime and memory for large lists.
    beams: list[list[tuple[float, list[dict[str, Any]]]]] = [[(0.0, [])]]
    for end in range(1, len(units) + 1):
        paths: dict[tuple[tuple[str, str], ...], tuple[float, list[dict[str, Any]]]] = {}
        for start in range(end):
            prefixes = [(cost, words) for cost, words in beams[start] if len(words) < MAX_WORDS]
            if not prefixes:
                continue
            nearest = heapq.nsmallest(
                CHOICE_COUNT,
                (w for w in app.soramimi_maker.get_similar_word(
                    db, target[start:end], distances, variation_cost=params["VARIATION_COST"],
                ) if math.isfinite(w["sim"])),
                key=lambda w: w["sim"],
            )
            boundary_cost = (
                params["MID_PHRASE_BREAK_PENALTY"]
                if end < len(units) and units[end - 1]["phrase"] == units[end]["phrase"]
                else 0
            )
            for cost, words in prefixes:
                used = {w["id"] for w in words}
                for word in nearest:
                    if word["id"] in used:
                        continue
                    sequence = [*words, word]
                    key = tuple((str(w["id"]), w["surface"]) for w in sequence)
                    total = cost + word["sim"] + params["WORD_NUMBER_PENALTY"] + boundary_cost
                    if key not in paths or total < paths[key][0]:
                        paths[key] = (total, sequence)
        beams.append(heapq.nsmallest(CHOICE_COUNT, paths.values(), key=lambda path: path[0]))
    return [
        {
            "words": [{"id": str(w["id"]), "surface": w["surface"]} for w in words],
            "label": " ＋ ".join(w["surface"] for w in words),
            "reading": "・".join(w["kana"] for w in words),
            "originals": [w.get("original") or w["surface"] for w in words],
        }
        for _cost, words in beams[-1]
    ]


def find_title_choices(
    title: str, csv_path: Path | None, where: str | None = None, *, csv_text: str = "",
) -> list[dict[str, Any]]:
    """Run optional candidate work in a disposable process; never cache user titles."""
    from .custom_thumbnail_preview import WORKER_TIMEOUT_SECONDS
    from .thumbnail_preview import render_slot

    with (
        render_slot(timeout=2),
        tempfile.TemporaryDirectory(prefix="soramimic-title-") as temporary,
    ):
        directory = Path(temporary)
        if csv_path is None:
            csv_path = directory / "wordlist.csv"
            csv_path.write_text(csv_text, encoding="utf-8")
        (directory / "request.json").write_text(json.dumps({
            "title": title, "csv_path": str(csv_path.resolve()), "where": where,
        }, ensure_ascii=False), encoding="utf-8")
        try:
            subprocess.run(
                [sys.executable, "-m", "soramimic_video.title_choices", temporary],
                check=True, timeout=WORKER_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Title search exceeded its time limit") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError("Title candidates could not be generated") from exc
        return json.loads((directory / "choices.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    from .custom_thumbnail_preview import _limit_worker

    _limit_worker()
    directory = Path(sys.argv[1])
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    choices = generate_choices(request["title"], Path(request["csv_path"]), request["where"])
    (directory / "choices.json").write_text(
        json.dumps(choices, ensure_ascii=False), encoding="utf-8",
    )
