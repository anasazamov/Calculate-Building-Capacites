from pathlib import Path

from feecalc import classify, extract, ingest, verify

TEXTS = {1: ["Строительный объём | м3 | 36036\nПлощадь застройки | м2 | 600.3\nHajmi, m2 | 36036.0"]}


def test_make_value_accepted():
    v = extract.make_value("volume_total", {"value": "36036", "unit": "м3",
                                            "source_text": "Строительный объём | м3 | 36036"}, "a.pdf", 1, TEXTS, "model1")
    assert v["status"] == verify.ACCEPTED and v["number"] == "36036" and v["flags"] == []
    assert (v["file"], v["page"]) == ("a.pdf", 1)


def test_make_value_invented_number_is_unverified():
    v = extract.make_value("volume_total", {"value": "36063", "unit": "м3",
                                            "source_text": "Строительный объём | м3 | 36036"}, "a.pdf", 1, TEXTS, "model1")
    assert v["status"] == verify.UNVERIFIED and v["number"] is None


def test_make_value_garbage_prefix_is_unverified():
    v = extract.make_value("volume_total", {"value": ">=36036", "unit": "м3",
                                            "source_text": "Строительный объём | м3 | 36036"}, "a.pdf", 1, TEXTS, "model1")
    assert v["status"] == verify.UNVERIFIED


def test_make_value_missing_and_bad_page():
    empty = {"value": None, "unit": None, "source_text": None}
    assert extract.make_value("volume", empty, "a.pdf", None, TEXTS, "model1")["status"] == verify.NOT_FOUND
    v = extract.make_value("volume", {"value": "36036.0", "unit": "m2", "source_text": "Hajmi, m2 | 36036.0"},
                           "a.pdf", 7, TEXTS, "model1")
    assert v["status"] == verify.UNVERIFIED and "sahifa" in v["reason"]


def test_unit_typo_is_flagged_not_rejected():
    v = extract.make_value("volume", {"value": "36036.0", "unit": "m2", "source_text": "Hajmi, m2 | 36036.0"},
                           "a.pdf", 1, TEXTS, "model1")
    assert v["status"] == verify.ACCEPTED and "xato yozilgan" in v["flags"][0]


def test_merge_equal_and_conflict():
    def val(value: str, page: int) -> dict:
        return {"field": "f", "value": value, "number": value, "status": verify.ACCEPTED, "file": "a.pdf",
                "page": page, "source_text": value, "reason": "", "flags": [], "model": "model1"}
    same = extract.merge("f", [val("36036", 1), val("36036.0", 3)], "model1")
    assert same["status"] == verify.ACCEPTED and len(same["other_sources"]) == 1
    diff = extract.merge("f", [val("36036", 1), val("36100", 3)], "model1")
    assert diff["status"] == verify.CONFLICT and diff["number"] is None
    assert extract.merge("f", [], "model1")["status"] == verify.NOT_FOUND


def test_project_chunks_select_relevant_sheets(sample_dir: Path):
    pdf = sample_dir / "demo_project.pdf"
    pages = ingest.ingest(pdf)
    kinds = {p.number: classify.sheet_type(p) for p in pages}
    prep = extract.project_chunks(pdf, pages, kinds)
    assert [(c.kind, c.pages) for c in prep.chunks] == [("tep", [1]), ("rooms", [2])]
    assert all(c.doc == "project" for c in prep.chunks)
    assert any("matnsiz" in w for w in prep.warnings)


def test_conclusion_chunk_has_page_markers(sample_dir: Path):
    pages = ingest.ingest(sample_dir / "demo_conclusion.pdf")
    prep = extract.conclusion_chunks(pages)
    assert len(prep.chunks) == 1 and prep.chunks[0].pages == [1, 2]
    assert "[[PAGE 1]]" in prep.chunks[0].text and "[[PAGE 2]]" in prep.chunks[0].text


def test_large_page_is_split_and_capped(monkeypatch):
    prep = extract.Prepared()
    page = ingest.Page(file="a.pdf", number=1, width=100, height=100, text="")
    monkeypatch.setattr(extract, "CHUNK_CHARS", 50)
    extract._add_chunks(prep, "rooms", page, [f"Комната {i} | 10.{i}" for i in range(100)])
    assert len(prep.chunks) == extract.MAX_CHUNKS_PER_PAGE
    assert any("O'QILMADI" in w for w in prep.warnings)


def test_level_marks(sample_dir: Path):
    page = ingest.ingest(sample_dir / "demo_project.pdf")[2]
    marks = [m["number"] for m in extract.level_marks(page)]
    assert marks[0] == "-3.200" and "0.000" in marks and marks[-1] == "52.420"


def test_prompts_state_the_response_shape():
    # Without the shape in the prompt, schema-constrained output degrades (see HISOBOT.md).
    for system in (extract.CONCLUSION_SYSTEM, extract.TEP_SYSTEM, extract.ROOMS_SYSTEM):
        assert "Response shape:" in system and '"source_text"' in system
