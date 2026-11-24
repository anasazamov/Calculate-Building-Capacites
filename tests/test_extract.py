from pathlib import Path

from feecalc import classify, extract, ingest, verify

TEXTS = {1: ["Строительный объём | м3 | 36036\nПлощадь застройки | м2 | 600.3\nHajmi, m2 | 36036.0"]}


def test_make_value_accepted():
    v = extract.make_value("volume", {"value": "36036", "unit": "м3",
                                            "source_text": "Строительный объём | м3 | 36036"}, "a.pdf", 1, TEXTS, "model1")
    assert v["status"] == verify.ACCEPTED and v["number"] == "36036" and v["flags"] == []
    assert (v["file"], v["page"]) == ("a.pdf", 1)


def test_make_value_invented_number_is_unverified():
    v = extract.make_value("volume", {"value": "36063", "unit": "м3",
                                            "source_text": "Строительный объём | м3 | 36036"}, "a.pdf", 1, TEXTS, "model1")
    assert v["status"] == verify.UNVERIFIED and v["number"] is None


def test_make_value_garbage_prefix_is_unverified():
    v = extract.make_value("volume", {"value": ">=36036", "unit": "м3",
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


def test_large_page_is_split_and_capped(sample_dir: Path, monkeypatch):
    pdf = sample_dir / "demo_project.pdf"
    pages = ingest.ingest(pdf)
    kinds = {p.number: classify.sheet_type(p) for p in pages}
    monkeypatch.setattr(extract, "CHUNK_CHARS", 40)
    prep = extract.project_chunks(pdf, pages[:1], kinds)
    assert len(prep.chunks) == extract.MAX_CHUNKS_PER_PAGE
    assert any("O'QILMADI" in w for w in prep.warnings)


def test_project_page_text_keeps_tables_apart(sample_dir: Path):
    pdf = sample_dir / "demo_project.pdf"
    text = ingest.structured_text(pdf, ingest.ingest(pdf)[0])
    assert "[TABLE 1]" in text and "Строительный объём | м3 | 36036" in text
    assert "[TEXT OUTSIDE TABLES]" in text and "Высота этажа - 3.0 м" in text


def test_merge_address_variants():
    def val(value: str, page: int) -> dict:
        return {"field": "address", "value": value, "number": None, "status": verify.ACCEPTED, "file": "a.pdf",
                "page": page, "source_text": value, "reason": "", "flags": [], "model": "model1"}
    same = extract.merge("address", [val("г. Самарканд, ул. Майсазор", 1), val("Samarqand sh., Maysazor ko'chasi", 2)], "m")
    assert same["status"] == verify.ACCEPTED and len(same["variants"]) == 1
    diff = extract.merge("address", [val("г. Самарканд, ул. Майсазор", 1), val("г. Наманган", 3)], "m")
    assert diff["status"] == verify.CONFLICT and [v["page"] for v in diff["variants"]] == [1, 3]


def test_level_mark_must_be_in_the_row():
    row = {"level_mark": "+3.710...+49.210", "source_text": "План 2-16 го этажа на отм. +3.710м...+49.210м | м² | 7439.3"}
    assert extract._checked_mark(row) == "+3.710...+49.210"
    assert extract._checked_mark({"level_mark": "-3.300", "source_text": "План подвала на отм. -3.200м | м² | 465.9"}) is None
    assert extract._checked_mark({"level_mark": None, "source_text": "x"}) is None


def test_schema_strings_are_bounded():
    # an unbounded string lets a repetition loop run to max_tokens (seen on a real drawing sheet)
    value = extract.TEP_SCHEMA["properties"]["project_code"]["properties"]
    assert all(value[k]["maxLength"] <= 250 for k in ("value", "unit", "source_text"))
    assert extract.ROOMS_SCHEMA["properties"]["rooms"]["items"]["properties"]["name"]["maxLength"] <= 250


def test_level_marks(sample_dir: Path):
    page = ingest.ingest(sample_dir / "demo_project.pdf")[2]
    marks = [m["number"] for m in extract.level_marks(page)]
    assert marks[0] == "-3.200" and "0.000" in marks and marks[-1] == "52.420"


def test_prompts_state_the_response_shape():
    # Without the shape in the prompt, schema-constrained output degrades (see HISOBOT.md).
    for system in (extract.CONCLUSION_SYSTEM, extract.TEP_SYSTEM, extract.ROOMS_SYSTEM):
        assert "Response shape:" in system and '"source_text"' in system


def test_floor_contradiction_is_flagged():
    def page(n: int, text: str) -> ingest.Page:
        words = [ingest.Word(10 + 40 * i, 10, 45 + 40 * i, 20, w) for i, w in enumerate(text.split())]
        return ingest.Page(file="c.pdf", number=n, width=600, height=800, text=text, words=words)
    pages = [page(1, "Ko'ksaroy maydoni-1 bino 2-qavat, 1-qavatda tibbiy markaz"),
             page(2, "Bino yerto‘la, 4 qavat hamda mansarda qavatidan iborat"),
             page(5, "Yerto‘lali 5-qavat bino o'lchamlari o'qlarda 12,5x49,6 metr")]
    statements = extract.floor_statements(pages)
    assert [(s["number"], s["page"]) for s in statements] == [(4, 2), (5, 5)]     # ordinals are not statements

    def building(floors: str) -> dict:
        return {"floors": {"value": floors, "number": floors, "status": verify.ACCEPTED, "flags": []}}
    b = [building("5")]
    extract.flag_floor_contradictions(b, statements)
    assert b[0]["floors"]["flags"] and b[0]["floors"]["flags"][0].startswith(extract.CONTRADICTION)
    assert "4 (2-sahifa" in b[0]["floors"]["flags"][0]
    # every stated number belongs to some building: nothing to flag
    two = [building("4"), building("5")]
    extract.flag_floor_contradictions(two, statements)
    assert two[0]["floors"]["flags"] == [] and two[1]["floors"]["flags"] == []
