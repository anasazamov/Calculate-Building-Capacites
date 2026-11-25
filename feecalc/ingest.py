"""Ingest: text layer (with coordinates) and a PNG image for every PDF page.

Text inside raster images (scans, pasted pictures of tables) is read with OCR (Tesseract, Russian and
Uzbek in both scripts) and added to the page's words with its coordinates, marked `ocr`. From there on it
is handled like the text layer: the models read it, the verbatim source check runs against it and the
highlight finds it. Only words Tesseract is confident about are kept; a page that still has no text is
flagged `textless`.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import tempfile
import re
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

TEXTLESS_THRESHOLD = 50       # fewer characters than this -> page is "textless"
IMAGE_LONGEST_SIDE = 2600     # longest side of the PNG, pixels
IMAGE_MAX_ZOOM = 2.0
STAMP_WIDTH_PT = 560.0        # GOST title block is 185 mm ~ 524 pt, plus margin
STAMP_HEIGHT_PT = 200.0       # 55 mm ~ 156 pt, plus margin
COLUMN_SEP = " | "
OCR_LANGUAGES = "rus+uzb_cyrl+uzb+eng"
OCR_DPI = (300, 600)           # read at the image's own resolution, within these bounds
OCR_MIN_CONFIDENCE = 60       # Tesseract word confidence (0-100); less confident words are dropped
OCR_TEXT_IMAGE = 75           # median word confidence above which an image holds text (a scan, a table)
OCR_PROBE_PIXELS = 2_500_000  # a quick first reading (at most this many pixels) decides whether to read in full
OCR_PROBE_MIN = 55            # below this median at the probe, the image is a picture (photo, drawing)
OCR_MIN_SHARE = 0.01          # images smaller than this share of the page are not read
OCR_MAX_PIXELS = 40_000_000   # a huge image is read at a lower resolution
OCR_VERSION = 5               # part of the cache key: raise it when the OCR settings change
OCR_CACHE = Path(os.environ.get("FEECALC_DATA", "data")) / "ocr"

# PyMuPDF 2.1 routes find_tables() through its bundled layout model (ONNX, CPU) whenever the
# pymupdf-layout package is installed, which it is as a hard dependency. On drawing sheets that
# costs 8-25 s per page and adds a second neural model to the chain. The classic vector-line
# detector finds the same tables in under a second and is deterministic, so the layout model is
# switched off for this process.
assert hasattr(pymupdf, "_layout_enabled"), "PyMuPDF changed its layout switch; review feecalc/ingest.py"
pymupdf._layout_enabled = False

# MuPDF is not thread-safe, not even across documents. The web worker and the test scripts read
# several documents from worker threads, so every use of a PyMuPDF document takes this lock.
MUPDF_LOCK = threading.RLock()


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    ocr: bool = False         # read from a raster image by OCR, not from the text layer


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
    ocr_words: int = 0        # how many of the words came from OCR
    ocr_unclear: int = 0      # images that look like text but OCR could not read them confidently


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


# ---------- OCR of raster images ----------
def _tesseract(png: bytes) -> list[tuple[float, float, float, float, str, float]]:
    """Words of an image: (x0, y0, x1, y1 in pixels, text, confidence)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "region.png"
        path.write_bytes(png)
        out = subprocess.run(["tesseract", str(path), "stdout", "-l", OCR_LANGUAGES, "--psm", "11", "tsv"],
                             capture_output=True, text=True, check=True).stdout
    words = []
    for line in out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 12 and cols[11].strip() and float(cols[10]) >= 0:
            left, top, width, height = (float(cols[i]) for i in (6, 7, 8, 9))
            words.append((left, top, left + width, top + height, cols[11].strip(), float(cols[10])))
    return words


def _page_ocr(p: pymupdf.Page, text_words: list[Word]) -> tuple[list[Word], int]:
    """OCR words of every sizeable image on the page, in page points; words already present in the text
    layer (a scan with a text layer on top) and unconfident or punctuation-only words are left out."""
    out: list[Word] = []
    unclear = 0
    area = abs(p.rect) or 1.0
    seen: set[tuple[float, float, float, float]] = set()
    for info in p.get_image_info():
        box = pymupdf.Rect(info["bbox"]) & p.rect
        key = (round(box.x0), round(box.y0), round(box.x1), round(box.y1))
        if box.is_empty or abs(box) / area < OCR_MIN_SHARE or key in seen:
            continue
        seen.add(key)
        native = info["width"] / max(box.width / 72, 1e-6) if info.get("width") else OCR_DPI[0]
        dpi = int(min(OCR_DPI[1], max(OCR_DPI[0], native)))
        while dpi > 72 and (box.width / 72 * dpi) * (box.height / 72 * dpi) > OCR_MAX_PIXELS:
            dpi -= 50
        # A photo or a drawing also yields "words", with low confidence; a scanned table or text yields
        # confident ones. The image's own median confidence decides, so no list of what to read is needed.
        probe_dpi = int(min(dpi, (OCR_PROBE_PIXELS / max(box.width / 72 * box.height / 72, 1e-6)) ** 0.5))
        probe = _tesseract(p.get_pixmap(clip=box, dpi=probe_dpi, alpha=False, colorspace=pymupdf.csGRAY).tobytes("png"))
        if not probe or statistics.median(w[5] for w in probe) < OCR_PROBE_MIN:
            continue
        found = probe if probe_dpi == dpi else _tesseract(
            p.get_pixmap(clip=box, dpi=dpi, alpha=False, colorspace=pymupdf.csGRAY).tobytes("png"))
        scale = 72 / dpi if probe_dpi != dpi else 72 / probe_dpi
        if not found or statistics.median(w[5] for w in found) < OCR_TEXT_IMAGE:
            unclear += 1  # it passed the probe as text, but not confidently enough to use
            continue
        for x0, y0, x1, y1, text, conf in found:
            # a table rule read as a bracket or bar next to a number: "2457,84]" -> "2457,84"
            text = re.sub(r"^[\[\(|]+(?=\d)|(?<=\d)[\]\)|]+$", "", text)
            if conf < OCR_MIN_CONFIDENCE or not re.search(r"[0-9A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", text) \
                    or (len(text) < 2 and not text.isdigit()):
                continue
            w = Word(box.x0 + x0 * scale, box.y0 + y0 * scale, box.x0 + x1 * scale, box.y0 + y1 * scale, text, True)
            cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
            if any(t.x0 <= cx <= t.x1 and t.y0 <= cy <= t.y1 for t in text_words):
                continue
            out.append(w)
    return out, unclear


def ocr_words(doc: pymupdf.Document, index: int, pdf_key: str, text_words: list[Word]) -> tuple[list[Word], int]:
    """OCR words of one page and the number of images that hold unreadable text. Cached on disk by the file's
    content: OCR takes seconds and every reader of the document (models, highlighting, sheet types) needs it."""
    cache = OCR_CACHE / f"{pdf_key}_{index + 1}_v{OCR_VERSION}.json"
    if cache.exists():
        stored = json.loads(cache.read_text(encoding="utf-8"))
        return [Word(*w, ocr=True) for w in stored["words"]], stored["unclear"]
    words, unclear = _page_ocr(_open_page(doc, index), text_words)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"words": [[w.x0, w.y0, w.x1, w.y1, w.text] for w in words], "unclear": unclear},
                                ensure_ascii=False), encoding="utf-8")
    return words, unclear


def _document_key(doc: pymupdf.Document) -> str:
    """Content key of an open document (the file's bytes), for the OCR cache."""
    path = Path(doc.name)
    stat = path.stat()
    return _file_key(str(path), stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=256)
def _file_key(path: str, _mtime: int, _size: int) -> str:
    return sha256_file(Path(path))[:24]


def read_page(doc: pymupdf.Document, index: int, file_name: str, pdf_key: str = "") -> Page:
    p = _open_page(doc, index)
    words = [Word(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
    text = p.get_text("text")
    ratio = _image_ratio(p)
    extra, unclear = ocr_words(doc, index, pdf_key or _document_key(doc), words) if ratio > 0 else ([], 0)
    if extra:
        words = words + extra
        text = text + "\n" + "\n".join(line.text for line in group_lines(extra))
    count = len(text.strip())
    return Page(file=file_name, number=index + 1, width=p.rect.width, height=p.rect.height, text=text,
                words=words, char_count=count, textless=count < TEXTLESS_THRESHOLD,
                image_ratio=round(ratio, 3), ocr_words=len(extra), ocr_unclear=unclear)


def ingest(pdf_path: Path, image_dir: Path | None = None) -> list[Page]:
    """Read all pages of a PDF; when `image_dir` is given also render PNGs."""
    pdf_path = Path(pdf_path)
    pages: list[Page] = []
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
        key = _document_key(doc)
        for i in range(doc.page_count):
            page = read_page(doc, i, pdf_path.name, key)
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
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
        p = _open_page(doc, number - 1)
        words = read_page(doc, number - 1, Path(pdf_path).name, _document_key(doc)).words
        p = _open_page(doc, number - 1)
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
    """Cached wrapper: table detection on a large drawing sheet takes seconds, and every model
    reading the document needs the same text."""
    stat = Path(pdf_path).stat()
    return _structured_text(str(pdf_path), stat.st_mtime_ns, stat.st_size, page.number)


@functools.lru_cache(maxsize=512)
def _structured_text(pdf_path: str, _mtime: int, _size: int, number: int) -> str:
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
        page = read_page(doc, number - 1, Path(pdf_path).name, _document_key(doc))
    return _build_structured_text(Path(pdf_path), page)


def _build_structured_text(pdf_path: Path, page: Page) -> str:
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
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
        p = _open_page(doc, number - 1)
        # Default search_for joins a line-final "-" with the next line, so a snippet ending in a
        # hyphen would not be found; de-hyphenation is switched off.
        flags = pymupdf.TEXTFLAGS_SEARCH & ~pymupdf.TEXT_DEHYPHENATE
        found = p.search_for(snippet.replace(COLUMN_SEP, " "), flags=flags)
        if found:
            return [(r.x0, r.y0, r.x1, r.y1) for r in found]
        words = read_page(doc, number - 1, Path(pdf_path).name, _document_key(doc)).words
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
