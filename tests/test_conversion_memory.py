"""The installed conversion engine must handle long branching pronunciations."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux process memory limits")
def test_long_reading_keeps_all_syllables_with_bounded_memory(tmp_path: Path) -> None:
    wordlist = tmp_path / "words.csv"
    wordlist.write_text(
        "id,original,surface,pronunciation\n1,候補,長い候補," + "カ" * 18,
        encoding="utf-8",
    )
    script = """
import json
import resource
import sys
from pathlib import Path

resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
from soramimic_video.soramimic_engine import run_convert

result = run_convert(
    ["カン" * 18], Path(sys.argv[1]), None,
    {"DUPLICATE": False, "VARIATION_COST": 16}, cache_db=False,
    word_boundaries_per_line=lambda lines: [[0, len(units)] for units in lines],
)
print(json.dumps(result, ensure_ascii=False))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(wordlist)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["phrases"] == ["カン" * 18]
    assert len(result["lines"]) == 1
    line = result["lines"][0]
    assert "".join(unit["pronunciation"] for unit in line["units"]) == "カン" * 18
    assert len(line["words"]) == 1
    word = line["words"][0]
    assert word["id"] == "1"
    assert word["period"] == [0, len(line["units"])]
    assert word["originalkana"] == "カン" * 18
    assert word["sim"] == 18 * 16
    assert not word.get("filler")
