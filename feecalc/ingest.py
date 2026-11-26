"""Ingest: text layer (with coordinates) and a PNG image for every PDF page.

Text inside raster images (scans, pasted pictures of tables, raster drawings) is read by the vision model
(llm.vlm(), Qwen3-VL): it transcribes every line or table cell together with its box on the image, and the
box is mapped to page points. Those words join the page's words marked `from_image`. From there on they are
handled like the text layer: the reading model reads them, the verbatim source check runs against them, the
highlight finds them and floor plans are read from their coordinates. The vision model also says what the
image is (a table, text, a drawing, a photo) and whether its text is legible, so no list of what to read is
needed. A page that still has no text is flagged `textless`.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import llm

TEXTLESS_THRESHOLD = 50       # fewer characters than this -> page is "textless"
IMAGE_LONGEST_SIDE = 2600     # longest side of the PNG, pixels
IMAGE_MAX_ZOOM = 2.0
STAMP_WIDTH_PT = 560.0        # GOST title block is 185 mm ~ 524 pt, plus margin
STAMP_HEIGHT_PT = 200.0       # 55 mm ~ 156 pt, plus margin
COLUMN_SEP = " | "
IMAGE_MIN_SHARE = 0.01       # images smaller than this share of the page are not read
VLM_MAX_SIDE = 2048           # longest side, pixels, of an image sent to the vision model
VLM_MIN_DPI = 150             # a small image is enlarged to at least this resolution
VLM_MAX_TOKENS = 6000         # the longest table answer measured was ~1.9k tokens; a denser image is read in tiles
VLM_WORKERS = 8               # images of one document read at the same time
VLM_VERSION = 1               # part of the cache key: raise it when the question or the settings change
VLM_CACHE = Path(os.environ.get("FEECALC_DATA", "data")) / "vlm"
VLM_RAW = Path(os.environ.get("FEECALC_DATA", "data")) / "vlm_raw"
TEXT_KINDS = ("table", "text", "stamp", "drawing")  # images whose writing is read into the page

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
    from_image: bool = False  # read from a raster image by the vision model, not from the text layer


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
    image_words: int = 0      # how many of the words the vision model read from images
    image_unclear: int = 0    # images that hold writing the vision model calls illegible


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


# ---------- raster images, read by the vision model ----------
VLM_SYSTEM = """You read the writing inside ONE image cut from a construction project album or an expertise
conclusion (Uzbek in Latin or Cyrillic script, Russian). Return JSON only.

- kind: what the image is. "table" (a table or a list of figures), "text" (a scanned page or paragraph),
  "stamp" (a drawing's title block), "drawing" (a plan, section or facade with labels and dimensions),
  "photo" (a photograph or satellite picture), "other".
- legible: false when the image holds writing you cannot read with certainty (too small, blurred, cut off).
- items: every piece of writing, in reading order, each with its box. For a table give every cell as its own
  item (an empty cell is left out); for running text give each visual line as one item; on a drawing give
  every label, room name, area and dimension as its own item. For a photo give nothing.
- text: exactly as written: the same characters, script, digits, decimal comma or point and spaces. Never
  translate, correct, complete or guess. Writing you cannot read with certainty is left out, not invented.
- bbox_2d: [x1, y1, x2, y2] on a 0-1000 grid of this image (0,0 is the top left corner, 1000,1000 the bottom
  right), tight around the writing."""

VLM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["table", "text", "stamp", "drawing", "photo", "other"]},
        "legible": {"type": "boolean"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "bbox_2d": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                                       "minItems": 4, "maxItems": 4}},
            "required": ["text", "bbox_2d"], "additionalProperties": False}},
    },
    "required": ["kind", "legible", "items"], "additionalProperties": False,
}


def _image_regions(p: pymupdf.Page) -> list[tuple[pymupdf.Rect, int]]:
    """Every sizeable image on the page (each placement once) and the resolution to render it at: its own
    resolution, at least VLM_MIN_DPI, with the longest side at most VLM_MAX_SIDE pixels."""
    area = abs(p.rect) or 1.0
    out: list[tuple[pymupdf.Rect, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for info in p.get_image_info():
        box = pymupdf.Rect(info["bbox"]) & p.rect
        key = (round(box.x0), round(box.y0), round(box.x1), round(box.y1))
        if box.is_empty or abs(box) / area < IMAGE_MIN_SHARE or key in seen:
            continue
        seen.add(key)
        native = info["width"] / max(box.width / 72, 1e-6) if info.get("width") else VLM_MIN_DPI
        cap = VLM_MAX_SIDE / max(box.width / 72, box.height / 72, 1e-6)
        out.append((box, max(36, int(min(cap, max(VLM_MIN_DPI, native))))))
    return out


VLM_RUN_LIMIT = 8      # this many items in a row with the same text: the answer has degenerated into a loop
VLM_TILE_OVERLAP = 0.06


def _ask_vlm(png: bytes) -> dict:
    """One reading of one image. A truncated answer, or one that degenerated into a loop (the same text over
    and over with the box sliding along, a known failure of vision models on dense drawings), raises
    llm.MalformedResponse."""
    m = llm.vlm()
    if not llm.port_open(m):
        raise llm.ModelError(f"Rasmlarni o'qiydigan model (VLM) serveri ishlamayapti: {m.url}")
    data = llm.ask(m, VLM_SYSTEM, "Read this image.", VLM_SCHEMA, VLM_MAX_TOKENS, VLM_RAW,
                   sampling={"temperature": 0.0, "top_p": 1.0, "top_k": 1, "presence_penalty": 0},
                   images=[png]).data
    run = 1
    texts = [i["text"].strip() for i in data["items"]]
    for a, b in zip(texts, texts[1:]):
        run = run + 1 if a == b else 1
        if run >= VLM_RUN_LIMIT:
            raise llm.RepetitionLoop(f"vlm: {run} ta ketma-ket bir xil yozuv {a!r}")
    return data


def _tiles(png: bytes) -> list[tuple[bytes, tuple[float, float, float, float]]]:
    """The image cut into 2 x 2 overlapping tiles, each with its place on the image's 0-1000 grid."""
    out = []
    with MUPDF_LOCK, pymupdf.open(stream=png, filetype="png") as doc:
        page = doc[0]
        w, h = page.rect.width, page.rect.height
        zoom = pymupdf.Pixmap(png).width / w  # keep the image's own pixels
        for ty in range(2):
            for tx in range(2):
                fx0, fy0 = max(0.0, tx / 2 - VLM_TILE_OVERLAP), max(0.0, ty / 2 - VLM_TILE_OVERLAP)
                fx1, fy1 = min(1.0, (tx + 1) / 2 + VLM_TILE_OVERLAP), min(1.0, (ty + 1) / 2 + VLM_TILE_OVERLAP)
                clip = pymupdf.Rect(fx0 * w, fy0 * h, fx1 * w, fy1 * h)
                pix = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                out.append((pix.tobytes("png"), (fx0, fy0, fx1, fy1)))
    return out


def _read_tiled(png: bytes) -> dict:
    """Read the image in four tiles and put the items back on the whole image's grid. A tile that still cannot
    be read makes the image count as not legible (a warning for staff), not a failed job."""
    items: list[dict] = []
    kinds: list[str] = []
    legible = True
    for tile, (fx0, fy0, fx1, fy1) in _tiles(png):
        try:
            a = _ask_vlm(tile)
        except llm.MalformedResponse:
            legible = False
            continue
        kinds.append(a["kind"])
        legible = legible and a["legible"]
        s = llm.VLM_COORD_SCALE
        for it in a["items"]:
            x0, y0, x1, y1 = it["bbox_2d"]
            box = [round((fx0 + x0 / s * (fx1 - fx0)) * s), round((fy0 + y0 / s * (fy1 - fy0)) * s),
                   round((fx0 + x1 / s * (fx1 - fx0)) * s), round((fy0 + y1 / s * (fy1 - fy0)) * s)]
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            # the same writing read again in the overlap of two tiles
            if any(k["text"] == it["text"] and k["bbox_2d"][0] <= cx <= k["bbox_2d"][2]
                   and k["bbox_2d"][1] <= cy <= k["bbox_2d"][3] for k in items):
                continue
            items.append({"text": it["text"], "bbox_2d": box})
    kind = max(set(kinds), key=kinds.count) if kinds else "other"
    return {"kind": kind, "legible": legible, "items": items}


def read_image(png: bytes) -> dict:
    """What the vision model reads in one image: {"kind", "legible", "items": [{"text", "bbox_2d"}]}.
    A server that is down raises llm.ModelError (the job stops: nothing is skipped silently). An answer that is
    truncated or degenerated is read again in tiles, which also gives finer boxes on a dense drawing."""
    try:
        return _ask_vlm(png)
    except llm.MalformedResponse:
        return _read_tiled(png)


def _split_item(text: str, x0: float, y0: float, x1: float, y1: float) -> list[Word]:
    """One read item (a cell or a line) as words: the box is shared out by the length of each word, so a
    highlight lands on the word and the column gaps between cells stay where they are."""
    tokens = text.split()
    total = sum(len(t) for t in tokens) + max(len(tokens) - 1, 0)
    out, pos, width = [], 0, x1 - x0
    for t in tokens:
        a = x0 + width * pos / total
        pos += len(t)
        out.append(Word(a, y0, x0 + width * pos / total, y1, t, True))
        pos += 1
    return out


def _cache_path(pdf_key: str, index: int) -> Path:
    model = re.sub(r"[^\w.-]", "_", llm.vlm().name)
    return VLM_CACHE / f"{pdf_key}_{index + 1}_{model}_v{VLM_VERSION}.json"


def _page_regions_read(answers: list[dict], regions: list[tuple[pymupdf.Rect, int]]) -> dict:
    """The vision model's answers for one page, in page points (the form kept in the cache)."""
    words, unclear, kinds = [], 0, []
    for (box, _dpi), a in zip(regions, answers):
        kinds.append(a["kind"])
        if a["kind"] in TEXT_KINDS and not a["legible"]:
            unclear += 1
        if a["kind"] not in TEXT_KINDS:
            continue
        sx, sy = box.width / llm.VLM_COORD_SCALE, box.height / llm.VLM_COORD_SCALE
        for item in a["items"]:
            bx0, by0, bx1, by1 = item["bbox_2d"]
            if not item["text"].strip() or bx1 <= bx0 or by1 <= by0:
                continue
            words += [[w.x0, w.y0, w.x1, w.y1, w.text] for w in _split_item(
                item["text"], box.x0 + bx0 * sx, box.y0 + by0 * sy, box.x0 + bx1 * sx, box.y0 + by1 * sy)]
    return {"words": words, "unclear": unclear, "kinds": kinds}


Pending = list[tuple[int, list[tuple[pymupdf.Rect, int]], list[bytes]]]


def _collect_images(doc: pymupdf.Document, pdf_key: str) -> Pending:
    """The not yet cached sizeable images of the document, rendered (needs the MuPDF lock, held by the caller)."""
    pending: Pending = []
    for i in range(doc.page_count):
        if _cache_path(pdf_key, i).exists():
            continue
        p = _open_page(doc, i)
        regions = _image_regions(p)
        if regions:
            pending.append((i, regions, [p.get_pixmap(clip=box, dpi=dpi, alpha=False).tobytes("png")
                                         for box, dpi in regions]))
    return pending


def _read_images(pending: Pending, pdf_key: str) -> None:
    """Send the rendered images to the vision model, several at the same time, and cache the answers.
    Touches no MuPDF object, so it runs without the lock: the rest of the service keeps working meanwhile."""
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=VLM_WORKERS) as pool:
        # the same picture placed on several pages (a title block, a repeated table) is read once
        unique: dict[str, object] = {}
        for _i, _r, pngs in pending:
            for png in pngs:
                h = hashlib.sha256(png).hexdigest()
                if h not in unique:
                    unique[h] = pool.submit(read_image, png)
        for i, regions, pngs in pending:
            read = [unique[hashlib.sha256(png).hexdigest()].result() for png in pngs]  # an error is raised here
            _write_cache(pdf_key, i, _page_regions_read(read, regions))


def _prefetch_images(doc: pymupdf.Document, pdf_key: str) -> None:
    _read_images(_collect_images(doc, pdf_key), pdf_key)


def _write_cache(pdf_key: str, index: int, data: dict) -> None:
    path = _cache_path(pdf_key, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def image_words(doc: pymupdf.Document, index: int, pdf_key: str, text_words: list[Word]) -> tuple[list[Word], int]:
    """Words the vision model read from the page's images, in page points, and the number of images whose
    writing it calls illegible. Cached on disk by the file's content and the model; a word that the text layer
    already has at that place (a scan with a text layer on top) is left out."""
    path = _cache_path(pdf_key, index)
    if not path.exists():
        _prefetch_images(doc, pdf_key)
    if not path.exists():
        return [], 0  # no image on the page is large enough to hold readable writing
    stored = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for x0, y0, x1, y1, text in stored["words"]:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if not any(t.x0 <= cx <= t.x1 and t.y0 <= cy <= t.y1 for t in text_words):
            out.append(Word(x0, y0, x1, y1, text, True))
    return out, stored["unclear"]


def _document_key(doc: pymupdf.Document) -> str:
    """Content key of an open document (the file's bytes), for the image-reading cache."""
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
    extra, unclear = image_words(doc, index, pdf_key or _document_key(doc), words) if ratio > 0 else ([], 0)
    if extra:
        words = words + extra
        text = text + "\n" + "\n".join(line.text for line in group_lines(extra))
    count = len(text.strip())
    return Page(file=file_name, number=index + 1, width=p.rect.width, height=p.rect.height, text=text,
                words=words, char_count=count, textless=count < TEXTLESS_THRESHOLD,
                image_ratio=round(ratio, 3), image_words=len(extra), image_unclear=unclear)


def ingest(pdf_path: Path, image_dir: Path | None = None) -> list[Page]:
    """Read all pages of a PDF; when `image_dir` is given also render PNGs."""
    pdf_path = Path(pdf_path)
    pages: list[Page] = []
    # All images of the document are read at once, not page by page, and without holding the MuPDF lock.
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
        key = _document_key(doc)
        pending = _collect_images(doc, key)
    _read_images(pending, key)
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
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
