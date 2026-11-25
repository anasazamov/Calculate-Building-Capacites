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


# ---------- OCR of raster images ----------
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


def test_text_inside_an_image_is_read_by_ocr_and_cached(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "OCR_CACHE", tmp_path / "ocr")
    pdf = _raster_pdf(tmp_path / "scan.pdf", ["Qurilish hajmi 36036 m3", "Umumiy maydon 9410,0 m2"])
    page = ingest.ingest(pdf)[0]
    ocr = " ".join(w.text for w in page.words if w.ocr)
    assert page.ocr_words > 0 and "36036" in ocr and "9410,0" in ocr and "hajmi" in ocr
    assert not any(w.ocr and w.x0 > 495 for w in page.words)              # the noise picture gave no words
    assert "36036" in page.text                                            # the models see it like the text layer
    assert len(list((tmp_path / "ocr").glob("*.json"))) == 1
    monkeypatch.setattr(ingest, "_tesseract", lambda png: (_ for _ in ()).throw(AssertionError("cache not used")))
    assert ingest.ingest(pdf)[0].ocr_words == page.ocr_words
    rects = ingest.snippet_rects(pdf, 1, "Qurilish hajmi 36036")
    assert rects and all(80 <= r[1] <= 240 for r in rects)                 # the highlight lands inside the picture


def test_a_value_found_only_in_ocr_text_is_flagged(tmp_path: Path, monkeypatch):
    from feecalc import extract
    monkeypatch.setattr(ingest, "OCR_CACHE", tmp_path / "ocr")
    pdf = _raster_pdf(tmp_path / "scan.pdf", ["Qurilish hajmi 36036 m3"])
    page = ingest.ingest(pdf)[0]
    from_image = {"file": "scan.pdf", "page": 1, "status": "accepted", "source_text": "Qurilish hajmi 36036", "flags": []}
    from_text = {"file": "scan.pdf", "page": 1, "status": "accepted", "source_text": "Texnik-iqtisodiy ko'rsatkichlar", "flags": []}
    extract.mark_ocr({"a": from_image, "b": [from_text]}, {("scan.pdf", 1): page})
    assert from_image["flags"] == [extract.OCR_FLAG] and from_text["flags"] == []
