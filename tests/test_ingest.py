from pathlib import Path

from feecalc import ingest


def test_pages_text_and_images(sample_dir: Path, tmp_path: Path):
    pages = ingest.ingest(sample_dir / "demo_project.pdf", tmp_path / "img")
    assert [p.number for p in pages] == [1, 2, 3, 4, 5]
    assert "Строительный объём" in pages[0].text
    assert not pages[0].textless and pages[4].textless
    assert all(Path(p.image_path).exists() for p in pages)
    assert pages[0].words and pages[0].char_count > 300


def test_table_row_becomes_one_line(sample_dir: Path):
    page = ingest.ingest(sample_dir / "demo_project.pdf")[0]
    lines = [ln.text for ln in ingest.group_lines(page.words)]
    assert "Строительный объём | м3 | 36036" in lines
    assert "Площадь застройки | м2 | 600.3" in lines


def test_stamp_text(sample_dir: Path):
    page = ingest.ingest(sample_dir / "demo_project.pdf")[2]
    stamp = ingest.stamp_text(page)
    assert "Разрез 1-1" in stamp and "+3.000" not in stamp


def test_find_tables(sample_dir: Path):
    tables = ingest.find_tables(sample_dir / "demo_project.pdf", 1)
    rows = [r for t in tables for r in t.text_lines()]
    assert any("Строительный объём" in r and "36036" in r for r in rows)


def test_snippet_rects(sample_dir: Path):
    pdf = sample_dir / "demo_project.pdf"
    # column order differs from the text layer: falls back to word-sequence matching
    rects = ingest.snippet_rects(pdf, 1, "Строительный объём | м3 | 36036")
    assert len(rects) >= 3
    assert ingest.snippet_rects(pdf, 1, "Площадь застройки")
    assert ingest.snippet_rects(pdf, 1, "bunday matn yo'q") == []


def test_sha256(sample_dir: Path):
    assert len(ingest.sha256_file(sample_dir / "demo_project.pdf")) == 64


# ---------- raster images, read by the vision model ----------
def _raster_pdf(path: Path, lines: list[str]) -> Path:
    """A page whose text exists only as a picture (as in a scan), plus one line of real text."""
    import pymupdf
    src = pymupdf.open()
    sp = src.new_page(width=420, height=160)
    for i, line in enumerate(lines):
        sp.insert_text((20, 40 + 30 * i), line, fontsize=16, fontname="helv")
    picture = sp.get_pixmap(dpi=300)
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((40, 40), "Texnik-iqtisodiy ko'rsatkichlar", fontsize=12)
    page.insert_image(pymupdf.Rect(40, 80, 460, 240), pixmap=picture)
    noise = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 400, 300), False)
    import random
    rnd = random.Random(1)
    noise.set_rect(noise.irect, (128,))
    for _ in range(4000):
        x, y = rnd.randrange(400), rnd.randrange(300)
        noise.set_pixel(x, y, (rnd.randrange(256),))
    page.insert_image(pymupdf.Rect(500, 80, 800, 300), pixmap=noise)    # a picture with no text
    doc.save(str(path))
    return path


def _fake_vlm(lines: list[str], calls: list[int]):
    """Stands in for the vision model: a wide image (the scanned table) holds `lines`, one item per line, on the
    0-1000 grid; the squarer noise picture is a photo with no writing."""
    import pymupdf

    def read(png: bytes) -> dict:
        calls.append(1)
        pix = pymupdf.Pixmap(png)
        if pix.width < pix.height * 1.8:
            return {"kind": "photo", "legible": True, "items": []}
        # in the source picture line i sits at y = 40 + 30 i (baseline) of 160 pt
        return {"kind": "table", "legible": True,
                "items": [{"text": t, "bbox_2d": [40, int((26 + 30 * i) / 160 * 1000), 600, int((44 + 30 * i) / 160 * 1000)]}
                          for i, t in enumerate(lines)]}
    return read


def test_text_inside_an_image_is_read_by_the_vision_model_and_cached(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "VLM_CACHE", tmp_path / "vlm")
    lines = ["Qurilish hajmi 36036 m3", "Umumiy maydon 9410,0 m2"]
    calls: list[int] = []
    monkeypatch.setattr(ingest, "read_image", _fake_vlm(lines, calls))
    pdf = _raster_pdf(tmp_path / "scan.pdf", lines)
    page = ingest.ingest(pdf)[0]
    read = " ".join(w.text for w in page.words if w.from_image)
    assert page.image_words > 0 and "36036" in read and "9410,0" in read and "hajmi" in read
    assert len(calls) == 2                                                  # both pictures were shown to the model
    assert not any(w.from_image and w.x0 > 495 for w in page.words)        # the photo gave no words
    assert all(40 <= w.x0 and w.x1 <= 460 and 80 <= w.y0 and w.y1 <= 240 for w in page.words if w.from_image)
    assert "36036" in page.text                                            # the reader sees it like the text layer
    assert len(list((tmp_path / "vlm").glob("*.json"))) == 1
    monkeypatch.setattr(ingest, "read_image", lambda png: (_ for _ in ()).throw(AssertionError("cache not used")))
    assert ingest.ingest(pdf)[0].image_words == page.image_words
    rects = ingest.snippet_rects(pdf, 1, "Qurilish hajmi 36036")
    assert rects and all(80 <= r[1] <= 240 for r in rects)                 # the highlight lands inside the picture


def test_illegible_writing_is_counted_and_an_unreachable_model_stops_the_job(tmp_path: Path, monkeypatch):
    import pytest
    from feecalc import llm
    monkeypatch.setattr(ingest, "VLM_CACHE", tmp_path / "vlm")
    monkeypatch.setattr(ingest, "read_image", lambda png: {"kind": "table", "legible": False, "items": []})
    pdf = _raster_pdf(tmp_path / "scan.pdf", ["Qurilish hajmi 36036 m3"])
    assert ingest.ingest(pdf)[0].image_unclear == 2
    monkeypatch.undo()                                                      # the real read_image, a fresh cache
    monkeypatch.setattr(ingest, "VLM_CACHE", tmp_path / "vlm2")
    monkeypatch.setattr(llm, "port_open", lambda m: False)
    with pytest.raises(llm.ModelError):
        ingest.ingest(pdf)


def test_a_value_found_only_in_image_text_is_flagged(tmp_path: Path, monkeypatch):
    from feecalc import extract
    monkeypatch.setattr(ingest, "VLM_CACHE", tmp_path / "vlm")
    monkeypatch.setattr(ingest, "read_image", _fake_vlm(["Qurilish hajmi 36036 m3"], []))
    pdf = _raster_pdf(tmp_path / "scan.pdf", ["Qurilish hajmi 36036 m3"])
    page = ingest.ingest(pdf)[0]
    from_image = {"file": "scan.pdf", "page": 1, "status": "accepted", "source_text": "Qurilish hajmi 36036", "flags": []}
    from_text = {"file": "scan.pdf", "page": 1, "status": "accepted", "source_text": "Texnik-iqtisodiy ko'rsatkichlar", "flags": []}
    extract.mark_image_values({"a": from_image, "b": [from_text]}, {("scan.pdf", 1): page})
    assert from_image["flags"] == [extract.IMAGE_FLAG] and from_text["flags"] == []


def test_a_bare_number_source_is_checked_on_its_one_labelled_line():
    from feecalc import verify
    texts = ["Turar joy\nTip-2\n9410.0\n36036.0", "Turar joy binosi | Tip-2 | 16 | 9410.0 | 36036.0\nJami 9980.4"]
    assert verify.line_of_number("9410.0", "9410.0", texts) == "Turar joy binosi | Tip-2 | 16 | 9410.0 | 36036.0"
    assert verify.line_of_number("9410.0", "Tip-2 9410.0", texts) is None          # already has a label
    twice = texts + ["Umumiy maydon 9410.0"]
    assert verify.line_of_number("9410.0", "9410.0", twice) is None                # two places: no guessing


def test_a_degenerate_answer_is_read_again_in_tiles_on_the_whole_image_grid(monkeypatch):
    import pymupdf
    from feecalc import llm
    png = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 200), False).tobytes("png")
    calls: list[tuple[int, int]] = []

    def fake(image: bytes) -> dict:
        pix = pymupdf.Pixmap(image)
        calls.append((pix.width, pix.height))
        if pix.width == 400:                                                 # the whole image: a loop
            items = [{"text": "402,80", "bbox_2d": [810, 12 * i, 832, 12 * i + 8]} for i in range(10)]
            return {"kind": "drawing", "legible": True, "items": items}
        return {"kind": "drawing", "legible": True, "items": [{"text": "A", "bbox_2d": [0, 0, 1000, 1000]}]}

    monkeypatch.setattr(llm, "port_open", lambda m: True)
    monkeypatch.setattr(llm, "ask", lambda *a, **k: type("R", (), {"data": fake(k["images"][0])})())
    read = ingest.read_image(png)
    assert calls[0] == (400, 200) and len(calls) == 5                       # whole image, then four tiles
    assert all(c[0] < 400 for c in calls[1:])
    boxes = [i["bbox_2d"] for i in read["items"]]
    assert [0, 0, 560, 560] in boxes and [440, 440, 1000, 1000] in boxes     # tiles put back on the 0-1000 grid
    assert read["legible"] and read["kind"] == "drawing"


def test_image_request_carries_the_picture_and_stores_only_its_hash(tmp_path: Path, monkeypatch):
    import json
    from feecalc import llm
    body = llm.request_body(llm.vlm(), "s", "u", ingest.VLM_SCHEMA, 100, images=[b"\x89PNG fake"])
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    stored = llm._stored_request(body)
    assert "base64" not in json.dumps(stored) and stored["messages"][1]["content"][0]["image_url"]["sha256"]
