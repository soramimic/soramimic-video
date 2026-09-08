import json

from soramimic_video.credits import distinct_credits, write_credit_files


def test_export_preserves_sources_terms_usage_and_people(tmp_path):
    common = {"image": "https://example.com/a.jpg", "image_page": "https://youtu.be/abc?t=123",
              "image_terms_page": "https://note.com/agency/terms",
              "image_usage": "noncommercial_fanwork",
              "image_credit": "非公式ファン作品 © A"}
    rows = [{**common, "original": "A", "surface": "エー"},
            {**common, "original": "A", "surface": "A"},
            {**common, "original": "B"},
            {"original": "象徴カード", "image": "https://example.com/card.svg"},
            {"original": "通常写真", "image": "https://example.com/photo.jpg", "credit": "CC BY"}]
    path = write_credit_files(rows, tmp_path)
    data = json.loads((tmp_path / "credits.json").read_text())
    assert len(data["items"]) == 4
    assert [row["original"] for row in data["items"]] == ["A", "B", "象徴カード", "通常写真"]
    for field in ("image_page", "image_terms_page", "image_credit", "image_usage"):
        assert data["items"][0][field] == common[field]
        assert common[field] in data["text"]
        assert common[field] in path.read_text()
    assert data["text"].count("非公式ファン作品 © A") == 2
    assert data["items"][2]["image_usage"] == ""
    assert data["items"][3]["image_credit"] == "CC BY"


def test_markdown_escapes_special_names_and_empty_export_is_explicit(tmp_path):
    path = write_credit_files([{"original": "A|B\n<script>[C]", "credit": "x|y"}], tmp_path)
    text = path.read_text()
    assert "A&#124;B<br>&lt;script&gt;&#91;C&#93;" in text
    assert "x&#124;y" in text
    assert len([line for line in text.splitlines() if line.startswith("|")]) == 3
    path = write_credit_files([], tmp_path)
    assert "使用素材：0件" in path.read_text()
    assert json.loads((tmp_path / "credits.json").read_text()) == {"items": [], "text": ""}


def test_distinct_images_keep_different_terms_and_credits():
    row = {"original": "同じ人物", "image": "https://example.com/image"}
    assert len(distinct_credits([row, row, {**row, "image_terms_page": "https://example.com/en"},
                                 {**row, "image_credit": "追加表記"}])) == 3
