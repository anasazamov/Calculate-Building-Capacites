"""Local normative knowledge base: resolutions and building codes split into citable clauses.

    registry (norm_sources.json) -> fetch (raw file, kept on disk) -> parse (clauses) -> KnowledgeBase

A clause is a numbered point, a paragraph of a long point, a note paragraph or a table row. It keeps
its text verbatim, a reference label a person would cite ("4-ilova, 5-band", "3.12-band",
"1-jadval, 3-qator") and its heading path. `KnowledgeBase` stores clauses in one SQLite file and
searches them with FTS5 (bm25) over a folded copy of the text, so that Latin, Cyrillic and
differently typed apostrophes all meet in one index.

Nothing here talks to a language model. The only network access is `fetch`, which requests the
public URL written in the registry and nothing else. Reference labels are Uzbek (Latin) because
staff cite them; identifiers and comments are English.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx

from . import ingest
from .verify import transliterate

REGISTRY = Path(__file__).with_name("norm_sources.json")
DATA_DIR = Path(os.environ.get("FEECALC_DATA", "data")) / "norms"
DEFAULT_DB = DATA_DIR / "knowledge.db"
RAW_DIR = DATA_DIR / "raw"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"   # plain and generic: nothing about this project
MIN_INTERVAL = 1.0      # seconds between two requests
SPLIT_CHARS = 1500      # a numbered point longer than this is stored paragraph by paragraph
CONTEXT_CHARS = 240     # longest lead-in sentence or table header copied into a heading path


# ---------- data ----------

@dataclass(frozen=True)
class Source:
    """A registry entry: where a document is published and how its page is laid out."""
    id: str
    title: str
    kind: str                       # qaror | shnq | qmq | sanqvan | kengash
    url: str
    language: str                   # uz-Latn | uz-Cyrl | ru
    designation: str = ""           # short citable name: "SHNQ 2.08.01-24", "VM 151-son (08.04.2026)"
    format: str = "lex"             # lex: document page of lex.uz; pdf: PDF with a text layer
    body_annex: str | None = None   # annex of the approving act that is the document itself ("" = unnumbered)
    wrapper: str = "buyruq"         # how the approving act is called in references when body_annex is set
    note: str = ""


@dataclass(frozen=True)
class Clause:
    ref: str                        # "4-ilova, 5-band"
    path: tuple[str, ...]           # headings above the clause, outermost first
    text: str                       # verbatim
    note: str = ""                  # amendment remark printed under the clause by the publisher


@dataclass(frozen=True)
class Document:
    """What the knowledge base records about one ingested document."""
    doc_id: str
    title: str
    kind: str
    url: str
    language: str
    designation: str = ""
    fetched_at: str = ""
    sha256: str = ""
    clauses: int = 0
    characters: int = 0


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    ref: str
    text: str
    url: str
    score: float                    # higher is better (bm25 with the sign turned)
    designation: str = ""
    kind: str = ""
    path: tuple[str, ...] = ()
    note: str = ""


def load_sources(registry: Path = REGISTRY) -> list[Source]:
    sources = [Source(**entry) for entry in json.loads(Path(registry).read_text(encoding="utf-8"))]
    ids = [s.id for s in sources]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate source ids in {registry}: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
    return sources


# ---------- fetch ----------

_clock = {"last": 0.0}


def raw_path(source: Source, raw_dir: Path) -> Path:
    return Path(raw_dir) / f"{source.id}.{'pdf' if source.format == 'pdf' else 'html'}"


def fetch_record(source: Source, raw_dir: Path) -> dict:
    """What was written next to the raw file: url, fetch time, sha256, size."""
    return json.loads((Path(raw_dir) / f"{source.id}.json").read_text(encoding="utf-8"))


def fetch(source: Source, raw_dir: Path, refresh: bool = False) -> Path:
    """Download the source once and keep the raw file; later calls return the cached file.

    A failed request raises (httpx.HTTPStatusError and friends) and nothing is written.
    """
    path = raw_path(source, raw_dir)
    record = path.with_suffix(".json")
    if not refresh and path.exists() and record.exists() and fetch_record(source, raw_dir)["url"] == source.url:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    wait = MIN_INTERVAL - (time.monotonic() - _clock["last"])
    if wait > 0:
        time.sleep(wait)
    response = httpx.get(source.url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120.0)
    _clock["last"] = time.monotonic()
    response.raise_for_status()
    path.write_bytes(response.content)
    record.write_text(json.dumps({
        "id": source.id, "url": source.url, "final_url": str(response.url),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(response.content).hexdigest(), "bytes": len(response.content),
        "content_type": response.headers.get("content-type", ""),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ---------- text forms ----------

_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_PLAIN_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})   # PDF text layers keep them
_MARKED_DIGITS = str.maketrans({s: "^" + d for s, d in zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")})
# Uzbek Cyrillic "е" is "ye" at the start of a word and after a vowel; "ц" is "s" unless a vowel precedes
_YE = re.compile(r"(?:(?<![^\W\d_])|(?<=[аеёиоуэюяўъь]))е")
_TS = re.compile(r"(?<![аеёиоуэюяў])ц")


def fold(text: str) -> str:
    """Search form of a text: lower-case Latin, no apostrophes, plain digits.

    Cyrillic (Uzbek and Russian) is transliterated, so "қурилиш ҳажми", "qurilish hajmi" and
    "Qurilish Hajmi" fold to the same string; so do "koʻcha", "ko‘cha", "ko'cha", "ko`cha" and "кўча".
    """
    lowered = text.translate(_LIGATURES).lower().translate(_PLAIN_DIGITS)
    return transliterate(_TS.sub("s", _YE.sub("ye", lowered)))


def ref_key(ref: str) -> str:
    """Comparison form of a reference label: "4-Илова,  5-банд" -> "4-ilova 5-band"."""
    return re.sub(r"[\s,;]+", " ", fold(ref.translate(_MARKED_DIGITS))).strip()


def _clean(text: str) -> str:
    """Whitespace only: runs collapse, lines are trimmed, empty lines go."""
    lines = (re.sub(r"[ \t\r\f\v\u00a0\u200b\ufeff]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _shorten(text: str) -> str:
    flat = text.replace("\n", " ")
    return flat if len(flat) <= CONTEXT_CHARS else flat[:CONTEXT_CHARS].rstrip() + "…"


def _join(*parts: str) -> str:
    return ", ".join(p for p in parts if p)


# ---------- blocks: what both readers (lex.uz page, PDF) hand to the assembler ----------

@dataclass
class _Block:
    kind: str                       # annex | title | heading | para | footnote | table | amendment | signature | edition | stub
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    bold: list[bool] = field(default_factory=list)     # per row: every written cell is bold (a group row)


@dataclass
class _Cell:
    colspan: int
    rowspan: int
    parts: list[str] = field(default_factory=list)
    plain: bool = False             # has text that is not bold


def _span(value: str | None) -> int:
    m = re.match(r"\d+", value or "")
    return max(1, int(m.group(0))) if m else 1


def _grid(rows: list[list[_Cell]]) -> tuple[list[list[str]], list[bool]]:
    """Table rows with row spans resolved: a cell spanning several rows is repeated in each of them,
    so that every row can be read on its own."""
    out: list[list[str]] = []
    bold: list[bool] = []
    carry: dict[int, tuple[str, int]] = {}           # column -> (text, rows still covered)
    for row in rows:
        line: dict[int, str] = {}
        ahead: dict[int, tuple[str, int]] = {c: (t, left - 1) for c, (t, left) in carry.items() if left > 1}
        col, strong, written = 0, True, False
        for cell in row:
            while col in carry:
                col += 1
            text = _clean("".join(cell.parts)).replace("\n", " ")
            line[col] = text
            if text:
                written = True
                strong = strong and not cell.plain
            if cell.rowspan > 1:
                for c in range(col, col + cell.colspan):
                    ahead[c] = (text if c == col else "", cell.rowspan - 1)
            col += cell.colspan
        for c, (text, _left) in carry.items():
            line.setdefault(c, text)
        carry = ahead
        if any(line.values()):
            out.append([line[c] for c in sorted(line)])
            bold.append(written and strong)
    return out, bold


# ---------- reader: lex.uz document page ----------

_LEX_IGNORED = {"INDEXES_ON_REF", "COMMENT", "ACT_ESSENTIAL_ELEMENTS", "ACT_ESSENTIAL_ELEMENTS_NUM",
                "ACCEPTING_BODY", "ACT_TITLE", "DEPARTMENTAL"}


class _LexPage(HTMLParser):
    """A lex.uz document page as blocks: one per child of <div id="divCont">.

    Every element of a document is a <div class="KIND lx_elem"> holding a menu (<div class="lx_elem2">,
    ignored) and the content. KIND tells what the element is: ACT_TEXT a paragraph, TEXT_HEADER_* a
    heading, APPL_BANNER_* the "N-ILOVA" banner of an annex, FOOTNOTE a note, and so on.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Block] = []
        self._depth = 0                       # <div> nesting
        self._root: int | None = None         # depth of divCont while inside it
        self._done = False
        self._skip: int | None = None         # depth of a subtree being ignored
        self._kind = ""
        self._text: list[str] = []
        self._rows: list[list[_Cell]] | None = None
        self._cell: _Cell | None = None
        self._tables = 0
        self._bold = 0
        self._sup = 0
        self._in_annex = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "div":
            self._depth += 1
            if self._root is None:
                if a.get("id") == "divCont" and not self._done:
                    self._root = self._depth
            elif self._depth == self._root + 1:
                self._kind = (a.get("class") or "").split(" ")[0]
                self._text, self._rows, self._cell = [], None, None
                self._tables = self._bold = self._sup = 0
                if self._kind in _LEX_IGNORED:
                    self._skip = self._depth
            elif self._skip is None and "lx_elem2" in (a.get("class") or ""):
                self._skip = self._depth
            return
        if self._root is None or self._skip is not None:
            return
        if tag == "table":
            self._tables += 1
            if self._tables == 1 and self._rows is None:
                self._rows = []
        elif tag == "tr" and self._tables == 1 and self._rows is not None:
            self._rows.append([])
        elif tag in ("td", "th") and self._tables == 1 and self._rows:
            self._cell = _Cell(_span(a.get("colspan")), _span(a.get("rowspan")))
            self._rows[-1].append(self._cell)
        elif tag in ("b", "strong"):
            self._bold += 1
        elif tag == "sup":
            self._sup += 1
        elif tag in ("br", "p"):
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._root is not None:
                if self._depth == self._root + 1:
                    if self._skip == self._depth:
                        self._skip = None
                    else:
                        self._finish()
                elif self._skip == self._depth:
                    self._skip = None
                elif self._depth == self._root:
                    self._root, self._done = None, True
            self._depth -= 1
            return
        if self._root is None or self._skip is not None:
            return
        if tag == "table":
            self._tables = max(0, self._tables - 1)
        elif tag in ("td", "th") and self._tables == 1:
            self._cell = None
        elif tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag == "sup":
            self._sup = max(0, self._sup - 1)
        elif tag == "p":
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._root is None or self._skip is not None or self._depth <= self._root:
            return
        self._emit(data.translate(_SUPERSCRIPT) if self._sup else data)

    def _emit(self, text: str) -> None:
        if self._cell is not None:
            self._cell.parts.append(text)
            if text.strip() and not self._bold:
                self._cell.plain = True
        elif self._tables == 0:
            self._text.append(text)

    def _finish(self) -> None:
        text = _clean("".join(self._text))
        kind = self._kind
        if self._rows is not None:
            rows, bold = _grid(self._rows)
            if text:
                self.blocks.append(_Block("para", text))
            if rows:
                self.blocks.append(_Block("table", rows=rows, bold=bold))
        elif not text:
            return
        elif kind.startswith("APPL_BANNER"):
            self._in_annex = True
            self.blocks.append(_Block("annex", text))
        elif kind == "ACT_TITLE_APPL" or (kind == "ACT_FORM" and self._in_annex):
            self.blocks.append(_Block("title", text))
        elif kind == "ACT_FORM":
            return                              # the word "qarori" / "buyrug'i" in the head of the act
        elif kind.startswith("TEXT_HEADER"):
            self.blocks.append(_Block("heading", text))
        elif kind == "FOOTNOTE":
            self.blocks.append(_Block("footnote", text))
        elif kind == "CHANGES_ORIGINS":
            self.blocks.append(_Block("amendment", text))
        elif kind == "SIGNATURE":
            self.blocks.append(_Block("signature", text))
        elif kind == "PUBLICATION_ORIGIN":
            self.blocks.append(_Block("edition", text))
        elif kind == "COMMENT_FOR_WARNING":
            self.blocks.append(_Block("stub", text))
        else:
            self.blocks.append(_Block("para", text))


def _lex_blocks(html: str) -> list[_Block]:
    page = _LexPage()
    page.feed(html)
    page.close()
    return page.blocks


# ---------- reader: PDF text layer ----------

_ANNEX_LABEL = re.compile(r"^\s*(?:(\d+\s*[a-zа-я]?)\s*-\s*)?(?:ilova|илова)\s*$|^\s*приложение\s*(?:№\s*)?(\d+\s*[а-я]?)?\s*$",
                          re.IGNORECASE)
_POINT = re.compile(r"^(\d{1,4})([⁰¹²³⁴⁵⁶⁷⁸⁹]*)\.\s+\S")
_DOTTED = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){1,3}\*?)\.?\s+\S")


def _pdf_blocks(path: Path) -> list[_Block]:
    """Blocks of a PDF with a text layer: paragraphs rebuilt from line geometry (a first-line indent
    or a vertical gap starts a paragraph), vector tables row by row. A table that runs over several
    pages comes out as one table."""
    blocks: list[_Block] = []
    flush_right: list[bool] = []          # per block: a paragraph set in the right half (annex banner lines)
    for page in ingest.ingest(path):
        tables = ingest.find_tables(path, page.number)
        loose = [w for w in page.words
                 if not any(t.x0 <= (w.x0 + w.x1) / 2 <= t.x1 and t.y0 <= (w.y0 + w.y1) / 2 <= t.y1 for t in tables)]
        lines = [ln for ln in ingest.group_lines(loose)
                 if not (re.fullmatch(r"\d{1,4}", ln.text.strip())             # a page number in the margin
                         and not 0.07 * page.height < ln.y0 < 0.93 * page.height)]
        margin = min((ln.x0 for ln in lines), default=0.0)
        height = statistics.median(ln.y1 - ln.y0 for ln in lines) if lines else 1.0
        previous: ingest.Line | None = None
        for _y, is_table, item in sorted([(ln.y0, 0, ln) for ln in lines] + [(t.y0, 1, t) for t in tables],
                                         key=lambda x: (x[0], x[1])):
            if is_table:
                rows = [list(r) for r in item.rows]
                if blocks and blocks[-1].kind == "table":
                    blocks[-1].rows.extend(rows)
                    blocks[-1].bold.extend([False] * len(rows))
                else:
                    blocks.append(_Block("table", rows=rows, bold=[False] * len(rows)))
                    flush_right.append(False)
                previous = None
                continue
            text = _clean(item.text.replace(ingest.COLUMN_SEP, " "))
            starts = (not blocks or blocks[-1].kind != "para"
                      or bool(_ANNEX_LABEL.match(blocks[-1].text))                       # a label stands alone
                      or (previous is None and blocks[-1].text[-1:] in ".:;!?")          # new page, sentence over
                      or item.x0 - margin > 0.8 * height
                      or (previous is not None and item.y0 - previous.y1 > 0.8 * height)
                      or bool(_POINT.match(text) or _DOTTED.match(text) or _ANNEX_LABEL.match(text)))
            if starts:
                blocks.append(_Block("para", text))
                flush_right.append(item.x0 > 0.4 * page.width)
            else:
                blocks[-1].text += " " + text
            previous = item
    out: list[_Block] = []
    right: list[bool] = []
    title_next = False
    for block, is_right in zip(blocks, flush_right):
        if block.kind == "para" and _ANNEX_LABEL.match(block.text):
            target: list[str] = []
            while out and out[-1].kind == "para" and right[-1]:      # "... qaroriga" lines above the label
                target.insert(0, out.pop().text)
                right.pop()
            block = _Block("annex", "\n".join(target + [block.text]))
            title_next = True
        elif block.kind == "para" and title_next:
            block, title_next = _Block("title", block.text), False
        else:
            title_next = False
        out.append(block)
        right.append(is_right)
    return out


# ---------- assembler: blocks -> clauses ----------

_SECTION_SIGN = re.compile(r"^(?:(\d+[⁰¹²³⁴⁵⁶⁷⁸⁹]*)\s*-?\s*§|§\s*(\d+))")
_CHAPTER = re.compile(r"^(\d+[⁰¹²³⁴⁵⁶⁷⁸⁹]*)\s*-\s*(bob|bo.?lim|qism|боб|бўлим|қисм)\b", re.IGNORECASE)
_CHAPTER_RU = re.compile(r"^(глава|раздел)\s+(\d+|[IVXLC]+)\b", re.IGNORECASE)
_ROMAN = re.compile(r"^([IVXLC]+)\.?\s")
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s")
_TABLE_LABEL = re.compile(r"^(?:(\d+\s*[a-zа-я]?)\s*-\s*(?:jadval|жадвал)|(?:jadval|жадвал|таблица)\s*№?\s*(\d+\s*[a-zа-я]?))"
                          r"(?:ning\s+davomi)?\.?$", re.IGNORECASE)
_CHAPTER_WORDS = {"боб": "bob", "бўлим": "bo'lim", "қисм": "qism", "qism": "qism", "bob": "bob"}


def _heading_label(text: str) -> str:
    """Short label of a heading for references to unnumbered text under it: "2-bob", "I bo'lim"."""
    section = "bo'lim"
    if m := _CHAPTER.match(text):
        return m.group(1) + "-" + _CHAPTER_WORDS.get(m.group(2).lower(), section)
    if m := _SECTION_SIGN.match(text):
        return (m.group(1) or m.group(2)) + "-§"
    if m := _CHAPTER_RU.match(text):
        return m.group(2) + "-" + ("bob" if m.group(1).lower() == "глава" else section)
    if m := _ROMAN.match(text):
        return m.group(1) + " " + section
    if m := _NUMBERED_HEADING.match(text):
        return m.group(1) + "-" + section
    return _shorten(text)[:60]


@dataclass
class _Unit:
    """Paragraphs that form one point (or one stretch of unnumbered text) until the next one starts."""
    ref: str
    path: tuple[str, ...]
    paragraphs: list[str]
    numbered: bool
    start: int                      # where its clauses go in the output, so that reading order is kept
    notes: list[str] = field(default_factory=list)


class _Assembler:
    def __init__(self, body_annex: str | None, wrapper: str) -> None:
        self.body_annex, self.wrapper = body_annex, wrapper
        self.clauses: list[Clause] = []
        self.edition = ""
        self.main_target: str | None = None      # what the annexes of the act itself are attached to
        self.top: str | None = None              # number of the current annex of the act ("" = unnumbered)
        self.nested: str | None = None           # number of the current annex of that annex
        self.top_title = self.nested_title = ""
        self.title_open = False
        self.headings: list[str] = []
        self.heading_label = ""
        self.unit: _Unit | None = None
        self.anchor = ""                          # reference of the point that notes and tables hang on
        self.last: tuple[int, ...] | None = None  # number of the last point in this annex
        self.dotted: bool | None = None           # numbering style of this annex: 3.12 (True) or 12 (False)
        self.skipping = False                     # between the signature and the next annex
        self.last_paragraph = ""
        self.note_lead = ""
        self.counts: dict[str, int] = {}
        self.used: dict[str, int] = {}

    def run(self, blocks: list[_Block]) -> list[Clause]:
        for block in blocks:
            getattr(self, "_" + block.kind)(block)
        self._flush()
        return self.clauses

    # --- where we are ---

    def _body(self) -> bool:
        return self.body_annex is not None and self.top == self.body_annex

    def _prefix(self) -> str:
        def label(number: str) -> str:
            return f"{number}-ilova" if number else "ilova"
        outer = self.wrapper if self.body_annex is not None else ""
        if self.top is None:
            return outer
        if self._body():
            return label(self.nested) if self.nested is not None else ""
        if self.nested is None:
            return _join(outer, label(self.top))
        return _join(outer, f"{label(self.top)}ga {label(self.nested)}")

    def _path(self) -> tuple[str, ...]:
        def named(number: str, title: str) -> str:
            label = f"{number}-ilova" if number else "ilova"
            return f"{label}. {title}" if title else label
        path: list[str] = []
        if self.top is not None and not self._body():
            path.append(named(self.top, self.top_title))
        if self.nested is not None:
            path.append(named(self.nested, self.nested_title))
        return tuple(path + self.headings)

    def _unique(self, ref: str) -> str:
        n = self.used[ref] = self.used.get(ref, 0) + 1
        return ref if n == 1 else f"{ref}({n})"

    @staticmethod
    def _lead(lead: str, paragraph: str) -> str:
        """The lead-in sentence that the next paragraph hangs on: "Quyidagi holatlarda ...:" stays the
        lead of the list items after it until one of them ends the sentence."""
        tail = paragraph.rstrip()[-1:]
        if tail == ":":
            return _shorten(paragraph)
        return "" if tail in ".!?" else lead

    # --- structure ---

    def _annex(self, block: _Block) -> None:
        self._flush()
        lines = [line for line in block.text.split("\n") if line.strip()]
        m = _ANNEX_LABEL.match(lines[-1])
        number = re.sub(r"\s+", "", (m.group(1) or m.group(2) or "").lower()) if m else ""
        target = " ".join(lines[:-1] if m else lines)
        if self.main_target is None:
            self.main_target = target
        if target == self.main_target or self.top is None:
            self.top, self.nested, self.top_title, self.nested_title = number, None, "", ""
        else:
            self.nested, self.nested_title = number, ""
        self.skipping, self.title_open = False, True
        self.headings, self.heading_label, self.anchor = [], "", ""
        self.last, self.dotted, self.last_paragraph, self.note_lead = None, None, "", ""

    def _title(self, block: _Block) -> None:
        if self.skipping:
            return
        if not self.title_open:
            self._heading(block)
        elif self.nested is not None:
            self.nested_title = (self.nested_title + " " + block.text.replace("\n", " ")).strip()
        else:
            self.top_title = (self.top_title + " " + block.text.replace("\n", " ")).strip()

    def _heading(self, block: _Block) -> None:
        if self.skipping:
            return
        self._flush()
        self.title_open = False
        text = block.text.replace("\n", " ")
        if _SECTION_SIGN.match(text) and self.headings:
            self.headings = [self.headings[0], text]
            self.heading_label = _join(_heading_label(self.headings[0]), _heading_label(text))
        else:
            self.headings = [text]
            self.heading_label = _heading_label(text)
        self.anchor = ""

    def _signature(self, _block: _Block) -> None:
        self._flush()
        self.skipping = True

    def _edition(self, block: _Block) -> None:
        self.edition = block.text

    def _stub(self, block: _Block) -> None:
        raise ValueError(f"the page carries no text of the act in this language: {block.text!r}")

    # --- text ---

    def _point(self, paragraph: str) -> str | None:
        """Label of the numbered point this paragraph opens ("12", "45¹", "3.12*"), or None.

        A number opens a point only when it continues the numbering of the annex; "2.5 m ..." or a
        list item "1." inside a point stays a paragraph.
        """
        first = paragraph.split("\n", 1)[0]
        if self.dotted is not False and (m := _DOTTED.match(first)):
            order = tuple(int(x) for x in re.findall(r"\d+", m.group(1)))
            if (order[0] >= 1 and order[-1] == 1) if self.last is None else self.last < order < (self.last[0] + 4,):
                self.dotted, self.last = True, order
                return m.group(1)
        if self.dotted is not True and (m := _POINT.match(first)):
            order = (int(m.group(1)), int(m.group(2).translate(_PLAIN_DIGITS) or 0))
            if self.last is None or (order > self.last and order[0] - self.last[0] <= 40):
                self.dotted, self.last = False, order
                return m.group(1) + m.group(2)
        return None

    def _para(self, block: _Block) -> None:
        if self.skipping:
            return
        self.title_open, self.note_lead = False, ""
        label = self._point(block.text)
        if label is not None:
            self._flush()
            ref = self._unique(_join(self._prefix(), f"{label}-band"))
            self.unit = _Unit(ref, self._path(), [block.text], True, len(self.clauses))
            self.anchor = ref
        elif self.unit is not None:
            self.unit.paragraphs.append(block.text)
        else:
            prefix = self._prefix()
            if self.heading_label:
                base = _join(prefix, self.heading_label)
            elif self.top is None or not prefix:
                base = _join(prefix, "muqaddima")
            else:
                base = prefix
            self.unit = _Unit(self._unique(base), self._path(), [block.text], False, len(self.clauses))
        self.last_paragraph = block.text

    def _amendment(self, block: _Block) -> None:
        if self.unit is not None and not self.skipping:
            self.unit.notes.append(block.text)

    def _flush(self) -> None:
        unit, self.unit = self.unit, None
        if unit is None:
            return
        whole = "\n".join(unit.paragraphs)
        note = " ".join(unit.notes)
        if unit.numbered:
            split = len(unit.paragraphs) >= 3 and len(whole) > SPLIT_CHARS
        else:
            split = not (len(unit.paragraphs) == 1 and unit.ref.endswith("muqaddima"))
        if not split:
            out = [Clause(unit.ref, unit.path, whole, note)]
        else:
            out, lead = [], ""
            for k, paragraph in enumerate(unit.paragraphs, 1):
                path = unit.path + ((lead,) if lead else ())
                out.append(Clause(f"{unit.ref}, {k}-xatboshi", path, paragraph, note if k == 1 else ""))
                lead = self._lead(lead, paragraph)
        self.clauses[unit.start:unit.start] = out

    def _footnote(self, block: _Block) -> None:
        if self.skipping:
            return
        self.title_open = False
        base = _join(self.anchor or self._prefix(), "izoh")
        k = self.counts[base] = self.counts.get(base, 0) + 1
        path = self._path() + ((self.note_lead,) if self.note_lead else ())
        self.clauses.append(Clause(f"{base}, {k}-xatboshi", path, block.text))
        self.note_lead = self._lead(self.note_lead, block.text)

    def _table(self, block: _Block) -> None:
        if self.skipping or not block.rows:
            return
        self.title_open, self.note_lead = False, ""
        rows, bold = block.rows, block.bold
        written = [c for c in rows[0] if c]
        label, m = "", None
        if len(written) == 1 and (m := _TABLE_LABEL.match(written[0])):
            label, rows, bold = written[0], rows[1:], bold[1:]
        elif m := _TABLE_LABEL.match(self.last_paragraph.strip()):
            label = self.last_paragraph.strip()
        if not rows:
            return
        number = re.sub(r"\s+", "", (m.group(1) or m.group(2)).lower()) if m else ""
        base = _join(self._prefix(), f"{number}-jadval") if number else _join(self.anchor or self._prefix(), "jadval")
        base = self._unique(base)
        name = label or "jadval"
        context: tuple[str, ...] = (f"{name}: {_shorten(' | '.join(c for c in rows[0] if c))}",)
        for i, (cells, strong) in enumerate(zip(rows, bold), 1):
            text = " | ".join(c for c in cells if c)
            self.clauses.append(Clause(f"{base}, {i}-qator", self._path() + (context if i > 1 else ()), text))
            if i > 1 and strong:                 # a group row: "III. | Hududiy zonalar koeffitsiyentlari (Kz)"
                group = f"{name}: {_shorten(text)}"
                context = (group,) if bold[0] else (context[0], group)
        self.last_paragraph = ""


def _assemble(blocks: list[_Block], body_annex: str | None = None, wrapper: str = "buyruq") -> list[Clause]:
    return _Assembler(body_annex, wrapper).run(blocks)


def parse_lex(html: str, body_annex: str | None = None, wrapper: str = "buyruq") -> list[Clause]:
    """Clauses of a lex.uz document page (the HTML of https://lex.uz/docs/<id>)."""
    return _assemble(_lex_blocks(html), body_annex, wrapper)


def parse_pdf(path: Path, body_annex: str | None = None, wrapper: str = "buyruq") -> list[Clause]:
    """Clauses of a PDF that has a text layer. A scanned PDF yields nothing: there is no OCR here."""
    return _assemble(_pdf_blocks(Path(path)), body_annex, wrapper)


def parse(source: Source, path: Path) -> list[Clause]:
    """Split the raw file of a source into clauses."""
    if source.format == "pdf":
        return parse_pdf(path, source.body_annex, source.wrapper)
    if source.format == "lex":
        return parse_lex(Path(path).read_text(encoding="utf-8"), source.body_annex, source.wrapper)
    raise ValueError(f"{source.id}: unknown format {source.format!r} (lex or pdf)")


# ---------- query ----------

_WORD = re.compile(r"[^\W_]+")
_STOP = {"va", "yoki", "hamda", "bilan", "uchun", "ham", "bu", "shu", "ushbu", "mazkur", "boyicha", "kerak",
         "i", "v", "vo", "na", "dlya", "ili", "po", "ot", "do", "s", "k", "o", "ob", "iz", "pri", "ne", "chto", "kak"}
# Endings as they look after folding, longest first: Uzbek case/plural/possessive, then Russian ones.
_SUFFIXES = ("larining", "larning", "laridan", "larini", "lariga", "larida", "lardan", "larni", "larga", "larda",
             "gacha", "lari", "ning", "ligi", "dagi", "idan", "yami", "lar", "lik", "iga", "ini", "ida", "dan",
             "ogo", "ego", "omu", "emu", "imi", "ami", "iye", "oye", "aya", "uyu", "yax",
             "ga", "ni", "da", "si", "li", "iy", "ix", "ax", "om", "em", "ov", "ev", "ey", "oy", "am", "ya", "yu",
             "i", "a", "u", "e", "o", "y")


def _stem(token: str) -> str:
    """Crude stem for prefix matching: endings are cut while at least three letters stay (four when
    the ending is a single letter). "qavatlarining" -> "qavat", "hajmini" -> "hajm"."""
    stem, cut = token, True
    while cut:
        cut = False
        for suffix in _SUFFIXES:
            if stem.endswith(suffix) and len(stem) - len(suffix) >= (4 if len(suffix) == 1 else 3):
                stem, cut = stem[:-len(suffix)], True
                break
    return stem


def match_expression(query: str) -> str:
    """FTS5 expression for a free-text query, or "" when the query has no usable word.

    Every word may match exactly, as a prefix, or by the prefix of its stem; the alternatives are
    separate phrases, so bm25 ranks an exact match above a prefix match above a stem match. Words
    are OR-ed (a clause need not hold all of them); the whole query as one phrase adds weight.
    """
    words: list[str] = []
    for w in _WORD.findall(fold(query)):
        if w not in words and w not in _STOP and (len(w) > 1 or w.isdigit()):
            words.append(w)
    terms: list[str] = []
    for w in words:
        terms.append(f'"{w}"')
        if len(w) >= 3 and not w.isdigit():
            terms.append(f'"{w}"*')
            if (stem := _stem(w)) != w:
                terms.append(f'"{stem}"*')
    if len(words) > 1:
        terms.append('"' + " ".join(words) + '"')
    return " OR ".join(terms)


# ---------- storage ----------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, language TEXT NOT NULL,
    designation TEXT NOT NULL, fetched_at TEXT NOT NULL, sha256 TEXT NOT NULL,
    clauses INTEGER NOT NULL, characters INTEGER NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS clauses (
    id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL, ref TEXT NOT NULL, ref_key TEXT NOT NULL,
    path TEXT NOT NULL, text TEXT NOT NULL, note TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS clauses_by_ref ON clauses (doc_id, ref_key);
CREATE VIRTUAL TABLE IF NOT EXISTS clauses_fts USING fts5(body, context, tokenize = 'unicode61 remove_diacritics 2');
"""


class KnowledgeBase:
    """Clauses of the ingested documents in one SQLite file, searched with FTS5.

    Every call opens its own connection, so one instance can be shared between threads.
    """

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:      # commit on success, then close
            yield db

    def add_document(self, meta: Document, clauses: list[Clause]) -> None:
        """Store a document; an older copy with the same doc_id is replaced as a whole."""
        with self._connect() as db:
            old = db.execute("SELECT position FROM documents WHERE doc_id = ?", (meta.doc_id,)).fetchone()
            position = old[0] if old else db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM documents").fetchone()[0]
            db.execute("DELETE FROM clauses_fts WHERE rowid IN (SELECT id FROM clauses WHERE doc_id = ?)", (meta.doc_id,))
            db.execute("DELETE FROM clauses WHERE doc_id = ?", (meta.doc_id,))
            db.execute("DELETE FROM documents WHERE doc_id = ?", (meta.doc_id,))
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (meta.doc_id, meta.title, meta.kind, meta.url, meta.language, meta.designation,
                        meta.fetched_at, meta.sha256, len(clauses), sum(len(c.text) for c in clauses), position))
            for seq, clause in enumerate(clauses):
                row = db.execute("INSERT INTO clauses (doc_id, seq, ref, ref_key, path, text, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (meta.doc_id, seq, clause.ref, ref_key(clause.ref),
                                  json.dumps(list(clause.path), ensure_ascii=False), clause.text, clause.note))
                db.execute("INSERT INTO clauses_fts (rowid, body, context) VALUES (?, ?, ?)",
                           (row.lastrowid, fold(clause.text), fold(" ".join((*clause.path, clause.ref)))))

    def documents(self) -> list[Document]:
        with self._connect() as db:
            rows = db.execute("SELECT doc_id, title, kind, url, language, designation, fetched_at, sha256, clauses, "
                              "characters FROM documents ORDER BY position").fetchall()
        return [Document(*row) for row in rows]

    def get(self, doc_id: str, ref: str) -> Clause | None:
        """The clause with this reference. A reference to something larger than one stored clause
        ("2-band" of a point kept paragraph by paragraph, or with a table and notes under it;
        "4-ilova") gives all of its clauses joined in reading order. Script, case, apostrophes and
        commas of `ref` do not matter. None when the document has nothing under this reference."""
        key = ref_key(ref)
        with self._connect() as db:
            rows = db.execute("SELECT ref, path, text, note FROM clauses WHERE doc_id = ? AND (ref_key = ? OR "
                              "substr(ref_key, 1, ?) = ?) ORDER BY seq", (doc_id, key, len(key) + 1, key + " ")).fetchall()
        if not rows:
            return None
        if len(rows) == 1 and ref_key(rows[0][0]) == key:
            return Clause(rows[0][0], tuple(json.loads(rows[0][1])), rows[0][2], rows[0][3])
        paths = [json.loads(r[1]) for r in rows]
        common = paths[0]
        for other in paths[1:]:
            same = 0
            while same < min(len(common), len(other)) and common[same] == other[same]:
                same += 1
            common = common[:same]
        exact = [r[0] for r in rows if ref_key(r[0]) == key]
        return Clause(exact[0] if exact else ref, tuple(common), "\n".join(r[2] for r in rows),
                      " ".join(r[3] for r in rows if r[3]))

    def search(self, query: str, k: int = 8, kinds: set[str] | None = None) -> list[Hit]:
        """The k clauses that match the query best, best first. `kinds` limits the search to
        documents of these kinds ("qaror", "shnq", ...)."""
        expression = match_expression(query)
        if not expression or k <= 0 or (kinds is not None and not kinds):
            return []
        where, args = "", [expression]
        if kinds is not None:
            where = f" AND d.kind IN ({', '.join('?' * len(kinds))})"
            args += sorted(kinds)
        with self._connect() as db:
            rows = db.execute(
                "SELECT d.doc_id, d.title, c.ref, c.text, d.url, -bm25(clauses_fts, 1.0, 0.5) AS score, d.designation, "
                "d.kind, c.path, c.note FROM clauses_fts JOIN clauses c ON c.id = clauses_fts.rowid "
                f"JOIN documents d ON d.doc_id = c.doc_id WHERE clauses_fts MATCH ?{where} "
                "ORDER BY score DESC, c.id LIMIT ?", (*args, k)).fetchall()
        return [Hit(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], tuple(json.loads(r[8])), r[9]) for r in rows]
