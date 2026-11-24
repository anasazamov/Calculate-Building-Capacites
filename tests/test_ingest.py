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
