"""Ingest: text layer (with coordinates) and a PNG image for every PDF page.

No OCR: a page without a text layer is flagged `textless`.
"""
from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

TEXTLESS_THRESHOLD = 50       # fewer characters than this -> page is "textless"
IMAGE_LONGEST_SIDE = 2600     # longest side of the PNG, pixels
IMAGE_MAX_ZOOM = 2.0
STAMP_WIDTH_PT = 560.0        # GOST title block is 185 mm ~ 524 pt, plus margin
STAMP_HEIGHT_PT = 200.0       # 55 mm ~ 156 pt, plus margin
COLUMN_SEP = " | "


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class Table:
    x0: float
    y0: float
    x1: float
    y1: float
    rows: list[list[str]]

    def text_lines(self) -> list[str]:
        return [COLUMN_SEP.join(c for c in r if c) for r in self.rows if any(r)]


@dataclass
class Page:
    file: str
    number: int               # 1-based
    width: float
    height: float
    text: str
    words: list[Word] = field(default_factory=list)
    char_count: int = 0
    textless: bool = False
    image_ratio: float = 0.0  # share of the page area covered by raster images
    image_path: str | None = None
    image_zoom: float = 1.0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_page(doc: pymupdf.Document, index: int) -> pymupdf.Page:
    page = doc[index]
    if page.rotation != 0:
        page.remove_rotation()  # make coordinates match what is displayed
    return page


def _image_ratio(page: pymupdf.Page) -> float:
    area = abs(page.rect) or 1.0
    covered = 0.0
    for info in page.get_image_info():
        covered += abs(pymupdf.Rect(info["bbox"]) & page.rect)
    return min(1.0, covered / area)


def read_page(doc: pymupdf.Document, index: int, file_name: str) -> Page:
    p = _open_page(doc, index)
    words = [Word(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
    text = p.get_text("text")
    count = len(text.strip())
    return Page(file=file_name, number=index + 1, width=p.rect.width, height=p.rect.height, text=text,
                words=words, char_count=count, textless=count < TEXTLESS_THRESHOLD,
                image_ratio=round(_image_ratio(p), 3))


def ingest(pdf_path: Path, image_dir: Path | None = None) -> list[Page]:
    """Read all pages of a PDF; when `image_dir` is given also render PNGs."""
    pdf_path = Path(pdf_path)
    pages: list[Page] = []
    with pymupdf.open(pdf_path) as doc:
        for i in range(doc.page_count):
            page = read_page(doc, i, pdf_path.name)
            if image_dir is not None:
                image_dir.mkdir(parents=True, exist_ok=True)
                p = _open_page(doc, i)
                zoom = min(IMAGE_MAX_ZOOM, IMAGE_LONGEST_SIDE / max(p.rect.width, p.rect.height))
                pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                out = image_dir / f"{pdf_path.stem}_{i + 1:04d}.png"
                pix.save(out)
                page.image_path = str(out)
                page.image_zoom = zoom
            pages.append(page)
    return pages


# ---------- lines and regions ----------

def _line_groups(words: list[Word]) -> list[list[Word]]:
    """Cluster words into visual lines by vertical centre; each line sorted left to right."""
    ordered = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    groups: list[list[Word]] = []
    for w in ordered:
        centre = (w.y0 + w.y1) / 2
        height = max(w.y1 - w.y0, 1.0)
        if groups:
            last = groups[-1]
            last_centre = statistics.fmean((q.y0 + q.y1) / 2 for q in last)
            last_height = statistics.fmean(max(q.y1 - q.y0, 1.0) for q in last)
            if abs(centre - last_centre) <= 0.45 * min(height, last_height):
                last.append(w)
                continue
        groups.append([w])
    for g in groups:
        g.sort(key=lambda w: w.x0)
    return groups


def group_lines(words: list[Word], column_gap: float = 2.5) -> list[Line]:
    """Rebuild visual lines so that a table row becomes one text line.

    When the gap between neighbouring words exceeds `column_gap` x glyph height,
    a column separator is inserted between them.
    """
    lines: list[Line] = []
    for g in _line_groups(words):
        parts = [g[0].text]
        for prev, w in zip(g, g[1:]):
            height = max(w.y1 - w.y0, prev.y1 - prev.y0, 1.0)
            parts.append(COLUMN_SEP if w.x0 - prev.x1 > column_gap * height else " ")
            parts.append(w.text)
        lines.append(Line(min(w.x0 for w in g), min(w.y0 for w in g),
                          max(w.x1 for w in g), max(w.y1 for w in g), "".join(parts)))
    return lines


def words_in_rect(page: Page, x0: float, y0: float, x1: float, y1: float) -> list[Word]:
    return [w for w in page.words if x0 <= (w.x0 + w.x1) / 2 <= x1 and y0 <= (w.y0 + w.y1) / 2 <= y1]


def stamp_text(page: Page) -> str:
    """Text of the title block (bottom-right corner of the sheet)."""
    words = words_in_rect(page, page.width - STAMP_WIDTH_PT, page.height - STAMP_HEIGHT_PT,
                          page.width, page.height)
    return "\n".join(line.text for line in group_lines(words))


def find_tables(pdf_path: Path, number: int) -> list[Table]:
    """Tables drawn with vector lines (PyMuPDF find_tables).

    Only the cell geometry is taken from PyMuPDF: its own cell-text extraction can drop
    spaces and reorder punctuation ("600.3" -> "6003 ."), which would defeat verbatim source
    checks. Cell text is rebuilt here from the page words that fall inside each cell.
    """
    out: list[Table] = []
    with pymupdf.open(pdf_path) as doc:
        p = _open_page(doc, number - 1)
        words = [Word(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
        for t in p.find_tables().tables:
            rows: list[list[str]] = []
            for row in t.rows:
                cells = [_cell_text(words, c) for c in row.cells]
                if any(cells):
                    rows.append(cells)
            if rows:
                b = t.bbox
                out.append(Table(b[0], b[1], b[2], b[3], rows))
    return out


def _cell_text(words: list[Word], bbox: tuple[float, float, float, float] | None) -> str:
    if bbox is None:
        return ""
    x0, y0, x1, y1 = bbox
    inside = [w for w in words if x0 <= (w.x0 + w.x1) / 2 <= x1 and y0 <= (w.y0 + w.y1) / 2 <= y1]
    return " ".join(" ".join(w.text for w in g) for g in _line_groups(inside))


def structured_text(pdf_path: Path, page: Page) -> str:
    """Page text for the model: every vector table as its own block of rows (cells joined with the
    column separator), followed by the remaining lines outside tables.

    On drawing sheets several tables sit side by side; rebuilding lines across the whole sheet
    would merge rows of unrelated tables, so tables are cut out by their geometry first.
    """
    tables = find_tables(pdf_path, page.number)
    parts: list[str] = []
    for i, t in enumerate(tables, start=1):
        parts.append(f"[TABLE {i}]")
        parts.extend(t.text_lines())
        parts.append("")
    outside = [w for w in page.words
               if not any(t.x0 <= (w.x0 + w.x1) / 2 <= t.x1 and t.y0 <= (w.y0 + w.y1) / 2 <= t.y1 for t in tables)]
    if tables and outside:
        parts.append("[TEXT OUTSIDE TABLES]")
    parts.extend(line.text for line in group_lines(outside))
    return "\n".join(parts)


# ---------- highlighting a source snippet on the page ----------

Rect = tuple[float, float, float, float]


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s|]+", text) if t]


def snippet_rects(pdf_path: Path, number: int, snippet: str) -> list[Rect]:
    """Rectangles of the snippet on the page, in PDF points.

    PyMuPDF search_for first; if it finds nothing (table columns stored in a different
    order in the text layer) the snippet words are matched against line-ordered words.
    """
    with pymupdf.open(pdf_path) as doc:
        p = _open_page(doc, number - 1)
        # Default search_for joins a line-final "-" with the next line, so a snippet ending in a
        # hyphen would not be found; de-hyphenation is switched off.
        flags = pymupdf.TEXTFLAGS_SEARCH & ~pymupdf.TEXT_DEHYPHENATE
        found = p.search_for(snippet.replace(COLUMN_SEP, " "), flags=flags)
        if found:
            return [(r.x0, r.y0, r.x1, r.y1) for r in found]
        words = [Word(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
    return match_word_sequence(words, snippet)


def match_word_sequence(words: list[Word], snippet: str) -> list[Rect]:
    wanted = _tokens(snippet)
    if not wanted:
        return []
    ordered: list[Word] = [w for g in _line_groups(words) for w in g]
    texts = [w.text for w in ordered]
    n = len(wanted)
    for i in range(len(texts) - n + 1):
        if texts[i:i + n] == wanted:
            return [(w.x0, w.y0, w.x1, w.y1) for w in ordered[i:i + n]]
    # word boundaries may differ: compare concatenated text
    joined = "".join(wanted)
    for i in range(len(texts)):
        acc = ""
        for j in range(i, min(i + 4 * n + 4, len(texts))):
            acc += texts[j]
            if acc == joined:
                return [(w.x0, w.y0, w.x1, w.y1) for w in ordered[i:j + 1]]
            if not joined.startswith(acc):
                break
    return []
