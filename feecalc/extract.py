"""Extraction: find volume-related values in a document with the help of a language model.

Rules:
- The project and the conclusion are read separately: one request holds a fragment of one document only.
- The model returns values and source snippets only; no coefficients, no arithmetic.
- Every value goes through verify.verify_source; if it fails it is "unverified".
- A whole drawing sheet is never sent to the model: the relevant table/region is cut out (ingest).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from . import classify, llm, verify
from .ingest import Line, Page, Table, find_tables, group_lines, stamp_text

CHUNK_CHARS = 9000            # text limit for one project request
CONCLUSION_CHARS = 36000      # a conclusion up to this size goes in one request
MAX_CHUNKS_PER_PAGE = 8
SMALL_PAGE_CHARS = 6000       # a page smaller than this is sent whole (as rebuilt lines)
PARALLEL = 8
MAX_TOKENS = {"conclusion": 8192, "tep": 4096, "rooms": 6144, "sheet": 64, "category": 2048}
PROMPT_VERSION = "2026-11-24.2"

_S = {"type": ["string", "null"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _value_schema(extra: dict | None = None, with_page: bool = False) -> dict:
    props: dict = dict(extra or {})
    props.update({"value": _S, "unit": _S, "source_text": _S})
    if with_page:
        props["page"] = {"type": ["integer", "null"]}
    return _obj(props)


CONCLUSION_SCALARS = ("conclusion_number", "conclusion_date", "address", "customer", "construction_type")
BUILDING_FIELDS = ("name", "type", "floors", "footprint_area", "total_area", "volume", "plan_size", "apartments")
TOTAL_FIELDS = ("footprint_area", "total_area", "volume")
PROJECT_SCALARS = ("address", "footprint_area", "volume_total", "volume_above_ground", "volume_below_ground",
                   "total_area", "apartments", "floors", "plan_size")

# field -> (numeric?, expected unit)
FIELD_KIND: dict[str, tuple[bool, str | None]] = {
    "conclusion_number": (False, None), "conclusion_date": (False, None), "address": (False, None),
    "customer": (False, None), "construction_type": (False, None), "name": (False, None), "type": (False, None),
    "plan_size": (False, None),
    "floors": (True, None), "apartments": (True, None),
    "footprint_area": (True, "m2"), "total_area": (True, "m2"), "area": (True, "m2"),
    "volume": (True, "m3"), "volume_total": (True, "m3"), "volume_above_ground": (True, "m3"),
    "volume_below_ground": (True, "m3"), "height": (True, "m"),
}
FIELD_LABELS: dict[str, str] = {
    "conclusion_number": "Xulosa raqami", "conclusion_date": "Xulosa sanasi", "address": "Manzil",
    "customer": "Buyurtmachi", "construction_type": "Qurilish turi", "name": "Bino nomi", "type": "Bino turi",
    "floors": "Qavatlar soni", "footprint_area": "Qurilish maydoni", "total_area": "Umumiy maydon",
    "volume": "Qurilish hajmi", "volume_total": "Qurilish hajmi (jami)",
    "volume_above_ground": "Qurilish hajmi (yer usti)", "volume_below_ground": "Qurilish hajmi (yer osti)",
    "plan_size": "Tarx o'lchami", "apartments": "Xonadonlar soni",
}

CONCLUSION_SCHEMA = _obj({
    **{k: _value_schema(with_page=True) for k in CONCLUSION_SCALARS},
    "buildings": {"type": "array", "items": _obj({
        **{k: _value_schema(with_page=True) for k in BUILDING_FIELDS},
        "floor_heights": {"type": "array", "items": _value_schema({"level": _S}, with_page=True)},
    })},
    "totals": _obj({k: _value_schema(with_page=True) for k in TOTAL_FIELDS}),
})
TEP_SCHEMA = _obj({
    **{k: _value_schema() for k in PROJECT_SCALARS},
    "area_breakdown": {"type": "array", "items": _value_schema({"name": {"type": "string"}, "level_mark": _S})},
    "floor_heights": {"type": "array", "items": _value_schema({"level": _S})},
})
ROOMS_SCHEMA = _obj({
    "rooms": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "level": _S, "area": _S, "volume": _S, "unit": _S, "source_text": _S})},
})
SHEET_SCHEMA = _obj({"kind": {"type": "string", "enum": list(classify.SHEET_TYPES)}})
CATEGORY_SCHEMA = _obj({"rooms": {"type": "array", "items": _obj({
    "name": {"type": "string"}, "category": {"type": "string", "enum": list(classify.CATEGORIES)}})}})

_COMMON_RULES = """You copy data out of construction documents. The document is in Uzbek (Latin or Cyrillic script) or Russian, often mixed.
Strict rules:
1. Copy only values that are explicitly written in the given text. Never calculate, guess, convert or fill in.
2. "value" is a string. For a number it is the number alone, exactly as written in the document (e.g. "36036", "9410.0", "600,3", "-3.200"); do not add units, comparison signs or comments.
3. "unit" is the unit written next to that value, exactly as written (e.g. "м3", "m2", "м"); null if no unit is written.
4. "source_text" is one contiguous fragment copied character by character from the text (up to 200 characters) that contains both the value and its label. Do not alter, shorten, translate or reorder it. The " | " column separator may be kept.
5. If a value is not in the text: "value", "unit" and "source_text" are all null. An empty list is [].
6. Reply with JSON only, in the shape below. All keys are required."""

_V = '{"value": "<string or null>", "unit": "<string or null>", "source_text": "<string or null>"}'
_VP = ('{"value": "<string or null>", "unit": "<string or null>", "source_text": "<string or null>", '
       '"page": <n from the [[PAGE n]] marker, or null>}')

CONCLUSION_SYSTEM = _COMMON_RULES + f"""

This is the text of a state expertise conclusion (ekspertiza xulosasi / экспертное заключение). Pages are separated by [[PAGE n]] markers; for each value put the number of the page where it was found into "page".
Every V below has this shape: {_VP}
Response shape:
{{"conclusion_number": V, "conclusion_date": V, "address": V, "customer": V, "construction_type": V,
 "buildings": [{{"name": V, "type": V, "floors": V, "footprint_area": V, "total_area": V, "volume": V, "plan_size": V, "apartments": V,
   "floor_heights": [{{"level": "<which floor/level, or null>", "value": ..., "unit": ..., "source_text": ..., "page": ...}}]}}],
 "totals": {{"footprint_area": V, "total_area": V, "volume": V}}}}
Notes:
- "buildings": one element per row of the buildings/structures table. The total row ("Jami" / "Итого" / "Всего") is not a building; it goes into "totals".
- "construction_type": new construction, reconstruction, renovation etc. (yangi qurilish, rekonstruksiya, renovatsiya / новое строительство, реконструкция), as written.
- "customer": the customer / developer (buyurtmachi / заказчик).
- "footprint_area" is the built-up area (qurilish maydoni / площадь застройки); "total_area" is the total floor area (umumiy maydon / общая площадь); "volume" is the building volume (qurilish hajmi / строительный объём). The table columns usually go: type, name, floors, footprint area, total area, volume.
- "floors": number of storeys (qavatlar soni / этажность); "apartments": number of apartments (xonadonlar soni / количество квартир).
- "plan_size": the building's dimensions in plan, e.g. "26.97 × 26.89", as written.
- "floor_heights": one element for each stated floor/level height (in metres)."""

TEP_SYSTEM = _COMMON_RULES + f"""

This is a fragment cut from a sheet of the architectural (AR) drawing album of a project: technical-economic indicators (технико-экономические показатели) and the title block.
Every V below has this shape: {_V}
Response shape:
{{"address": V, "footprint_area": V, "volume_total": V, "volume_above_ground": V, "volume_below_ground": V, "total_area": V, "apartments": V, "floors": V, "plan_size": V,
 "area_breakdown": [{{"name": "<row label as written>", "level_mark": "<level mark such as -3.200, or null>", "value": ..., "unit": ..., "source_text": ...}}],
 "floor_heights": [{{"level": "<which floor/level, or null>", "value": ..., "unit": ..., "source_text": ...}}]}}
Notes:
- "footprint_area": площадь застройки / qurilish maydoni. "volume_total": строительный объём (total) / qurilish hajmi. "volume_above_ground": надземная часть / выше отм. 0.000. "volume_below_ground": подземная часть / ниже отм. 0.000.
- "total_area": total floor area of the building (общая площадь здания / umumiy maydon), the grand total.
- "area_breakdown": the rows that make up the total area: areas by level, by floor or by room group (e.g. подвал, 1-этаж, 2–16 этажи, лестничная клетка etc., технические помещения). One element per row; the grand-total row is not included. Do not merge or split rows.
- "floor_heights": floor heights explicitly written in the text (высота этажа / qavat balandligi), in metres.
- "apartments": количество квартир / xonadonlar soni. "floors": этажность / qavatlar soni.
- "address": the address of the object (city, street), as written."""

ROOMS_SYSTEM = _COMMON_RULES + """

This is a fragment of a room table from a project drawing album (экспликация полов / экспликация помещений / ведомость отделки помещений).
Response shape:
{"rooms": [{"name": "<room name as written>", "level": "<floor or level mark if written, else null>", "area": "<area number or null>", "volume": "<volume number if the document states a volume, else null>", "unit": "<unit of the area or null>", "source_text": "<fragment copied character by character from that row, containing the name and the area>"}]}
Notes:
- One element per table row. If one row lists several room names, copy the name in full.
- Skip rows that have no area. Skip total rows (Итого / Всего / Jami).
- Rule 2 applies to "area" and "volume" as well: the number alone."""

SHEET_SYSTEM = """You are given the title block and headings of a sheet from a project drawing album. Decide the sheet type.
Types: general_data (общие данные / umumiy ma'lumotlar), site_plan (генплан / bosh reja), floor_plan (план этажа / qavat rejasi), section (разрез / kesim), explication (экспликация полов or помещений), finishing_schedule (ведомость отделки помещений), other (facades, details and anything else).
Reply with JSON only: {"kind": "<one of the types>"}"""

CATEGORY_SYSTEM = """You are given a list of room names from a building. Classify each name:
Ha - car parking (паркинг, автостоянка, garage); Ht - technical room or technical floor (pump room, switchboard room, ventilation chamber etc.);
Hu - common-use part of a residential building (staircase, lift lobby, common corridor, vestibule etc.); other - everything else (apartment, office, retail etc.).
Return the names unchanged and in the given order.
Reply with JSON only: {"rooms": [{"name": "<the given name>", "category": "Ha" | "Ht" | "Hu" | "other"}]}"""

_KEY_TEP = re.compile(r"площад|объ[её]м|этаж|квартир|высот|застройк|maydon|hajm|qavat|xonadon|balandlik"
                      r"|майдон|ҳажм|қават|хонадон|баландлик", re.I)
_KEY_ROOMS = re.compile(r"помещ|площад|xona|maydon|хона|майдон|наименован|nomi", re.I)
_KEY_ADDRESS = re.compile(r"адрес|manzil|манзил|\bг\.|\bул\.|shahar|шаҳар|ko['ʼ’`]cha|кўча|район|tuman|туман", re.I)


@dataclass
class Chunk:
    doc: str             # "project" | "conclusion"
    kind: str            # "conclusion" | "tep" | "rooms"
    file: str
    pages: list[int]
    text: str            # the text shown to the model


@dataclass
class Prepared:
    chunks: list[Chunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _split(lines: list[str], limit: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for ln in lines:
        if current and size + len(ln) + 1 > limit:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(ln)
        size += len(ln) + 1
    if current:
        pieces.append("\n".join(current))
    return pieces


def page_line_text(page: Page) -> str:
    return "\n".join(ln.text for ln in group_lines(page.words))


def conclusion_chunks(pages: list[Page]) -> Prepared:
    prep = Prepared()
    current: list[str] = []
    numbers: list[int] = []
    size = 0

    def close() -> None:
        if current:
            prep.chunks.append(Chunk("conclusion", "conclusion", pages[0].file, list(numbers), "\n".join(current)))

    for p in pages:
        if p.textless:
            prep.warnings.append(f"{p.file}, {p.number}-sahifa: matnsiz (matn qatlami yo'q), o'qilmadi")
            continue
        text = f"[[PAGE {p.number}]]\n{page_line_text(p)}"
        if len(text) > CONCLUSION_CHARS:
            prep.warnings.append(
                f"{p.file}, {p.number}-sahifa: matn {len(text)} belgi — chegaradan ({CONCLUSION_CHARS}) katta, "
                f"faqat boshidagi {CONCLUSION_CHARS} belgi o'qildi")
            text = text[:CONCLUSION_CHARS]
        if current and size + len(text) > CONCLUSION_CHARS:
            close()
            current, numbers, size = [], [], 0
        current.append(text)
        numbers.append(p.number)
        size += len(text)
    close()
    if len(prep.chunks) > 1:
        prep.warnings.append(
            f"Xulosa {len(prep.chunks)} ta so'rovga bo'lib o'qildi; sahifa chegarasida bo'lingan jadval "
            f"qatorlari to'liq o'qilmagan bo'lishi mumkin")
    return prep


def _inside(line: Line, tables: list[Table]) -> bool:
    mx, my = (line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2
    return any(t.x0 <= mx <= t.x1 and t.y0 <= my <= t.y1 for t in tables)


def project_chunks(pdf_path: Path, pages: list[Page], kinds: dict[int, classify.SheetType]) -> Prepared:
    prep = Prepared()
    for p in pages:
        st = kinds[p.number]
        if p.textless:
            prep.warnings.append(f"{p.file}, {p.number}-sahifa: matnsiz (matn qatlami yo'q), o'qilmadi")
            continue
        tep = st.kind == "general_data" or st.has_tep
        rooms = st.kind in ("explication", "finishing_schedule") or st.has_rooms
        if not tep and not rooms:
            continue
        if p.image_ratio >= 0.1:
            prep.warnings.append(
                f"{p.file}, {p.number}-sahifa: maydonining {round(p.image_ratio * 100)}% qismi rastr rasm — "
                f"ko'rsatkichlar rasm ichida bo'lsa o'qilmaydi (OCR yo'q)")
        if p.char_count <= SMALL_PAGE_CHARS:
            text = page_line_text(p)
            if tep:
                prep.chunks.append(Chunk("project", "tep", p.file, [p.number], text))
            if rooms:
                prep.chunks.append(Chunk("project", "rooms", p.file, [p.number], text))
            continue
        tables = find_tables(pdf_path, p.number)
        all_lines = group_lines(p.words)
        if tep:
            relevant = [t for t in tables if _KEY_TEP.search(" ".join(t.text_lines()))]
            lines: list[str] = []
            for t in relevant:
                lines.extend(t.text_lines())
                lines.append("")
            lines.extend(ln.text for ln in all_lines
                         if not _inside(ln, relevant) and re.search(r"\d", ln.text)
                         and (_KEY_TEP.search(ln.text) or _KEY_ADDRESS.search(ln.text)))
            lines.append("--- title block ---")
            lines.append(stamp_text(p))
            _add_chunks(prep, "tep", p, lines)
        if rooms:
            relevant = [t for t in tables if _KEY_ROOMS.search(" ".join(t.text_lines()))]
            if relevant:
                lines = [ln for t in relevant for ln in t.text_lines()]
            else:
                lines = [ln.text for ln in all_lines
                         if re.search(r"[^\W\d_]{3,}", ln.text) and re.search(r"\d", ln.text)]
            _add_chunks(prep, "rooms", p, lines)
    return prep


def _add_chunks(prep: Prepared, kind: str, p: Page, lines: list[str]) -> None:
    pieces = _split(lines, CHUNK_CHARS)
    if len(pieces) > MAX_CHUNKS_PER_PAGE:
        prep.warnings.append(
            f"{p.file}, {p.number}-sahifa ({kind}): matn {len(pieces)} bo'lak — faqat birinchi "
            f"{MAX_CHUNKS_PER_PAGE} tasi o'qildi, qolgani O'QILMADI")
        pieces = pieces[:MAX_CHUNKS_PER_PAGE]
    for piece in pieces:
        if piece.strip():
            prep.chunks.append(Chunk("project", kind, p.file, [p.number], piece))


# ---------- building and verifying a value record ----------

def make_value(field_name: str, raw: dict, file: str, page: int | None, texts: dict[int, list[str]],
               model: str, kind_key: str | None = None, value_key: str = "value") -> dict:
    """Turn the model's {value, unit, source_text} into a stored, verified record."""
    numeric, expected = FIELD_KIND[kind_key or field_name]
    value, source, unit = raw.get(value_key), raw.get("source_text"), raw.get("unit")
    if page is None or page not in texts:
        if value is None or not str(value).strip():
            status, reason, number = verify.NOT_FOUND, "", None
        else:
            status, reason, number = verify.UNVERIFIED, f"sahifa raqami noto'g'ri yoki berilmagan: {page!r}", None
    else:
        status, reason, number = verify.verify_source(value, source, texts[page], numeric)
    flags: list[str] = []
    if status == verify.ACCEPTED and expected in ("m2", "m3"):
        flag = verify.unit_flag(expected, unit)
        if flag:
            flags.append(flag)
    return {
        "field": field_name, "value": value, "number": None if number is None else str(number), "unit": unit,
        "unit_norm": expected if numeric else None, "file": file, "page": page, "source_text": source,
        "status": status, "reason": reason, "flags": flags, "model": model,
    }


def empty_value(field_name: str, model: str) -> dict:
    return {"field": field_name, "value": None, "number": None, "unit": None, "unit_norm": None, "file": None,
            "page": None, "source_text": None, "status": verify.NOT_FOUND, "reason": "", "flags": [], "model": model}


def merge(field_name: str, candidates: list[dict], model: str) -> dict:
    """One field found in several chunks: equal accepted values collapse into one;
    different accepted values become a "conflict" (not accepted, staff decides)."""
    accepted = [c for c in candidates if c["status"] == verify.ACCEPTED]
    if accepted:
        distinct = {Decimal(c["number"]) if c["number"] is not None else verify.normalize(str(c["value"])).lower()
                    for c in accepted}
        main = dict(accepted[0])
        main["other_sources"] = [{"file": c["file"], "page": c["page"], "value": c["value"],
                                  "source_text": c["source_text"]} for c in accepted[1:]]
        if len(distinct) > 1:
            main["status"] = verify.CONFLICT
            main["reason"] = "hujjatning turli joylarida turli qiymat: " + "; ".join(
                f"{c['value']} ({c['page']}-sahifa)" for c in accepted)
            main["number"] = None
        return main
    rejected = [c for c in candidates if c["status"] == verify.UNVERIFIED]
    return dict(rejected[0]) if rejected else empty_value(field_name, model)


# ---------- requests to the model ----------

@dataclass
class RequestRecord:
    key: str
    prompt_hash: str
    model: str
    kind: str
    file: str
    pages: list[int]
    prompt_tokens: int
    completion_tokens: int
    duration_s: float
    cached: bool


_PROMPTS = {"conclusion": (CONCLUSION_SYSTEM, CONCLUSION_SCHEMA), "tep": (TEP_SYSTEM, TEP_SCHEMA),
            "rooms": (ROOMS_SYSTEM, ROOMS_SCHEMA)}


def ask_chunks(m: llm.Model, chunks: list[Chunk], raw_dir: Path) -> list[tuple[Chunk, llm.Response]]:
    def one(c: Chunk) -> tuple[Chunk, llm.Response]:
        system, schema = _PROMPTS[c.kind]
        return c, llm.ask(m, system, "Text:\n" + c.text, schema, MAX_TOKENS[c.kind], raw_dir)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return list(pool.map(one, chunks))  # an error is raised here


def _record(c: Chunk, r: llm.Response) -> dict:
    return asdict(RequestRecord(r.key, r.prompt_hash, r.model, c.kind, c.file, c.pages, r.prompt_tokens,
                                r.completion_tokens, r.duration_s, r.cached))


def page_texts(pages: list[Page], chunks: list[Chunk]) -> dict[int, list[str]]:
    """Texts used for source verification, per page: the text layer, its rebuilt-lines form and
    the chunks shown to the model (all derived from that page's text layer)."""
    texts: dict[int, list[str]] = {p.number: [p.text, page_line_text(p)] for p in pages}
    for c in chunks:
        if len(c.pages) == 1:
            texts[c.pages[0]].append(c.text)
    return texts


def read_conclusion(m: llm.Model, pages: list[Page], raw_dir: Path) -> dict:
    prep = conclusion_chunks(pages)
    texts = page_texts(pages, [])
    file = pages[0].file if pages else ""
    scalars: dict[str, list[dict]] = {k: [] for k in CONCLUSION_SCALARS}
    totals: dict[str, list[dict]] = {k: [] for k in TOTAL_FIELDS}
    buildings: list[dict] = []
    requests: list[dict] = []
    for c, r in ask_chunks(m, prep.chunks, raw_dir):
        requests.append(_record(c, r))
        d = r.data
        for k in CONCLUSION_SCALARS:
            scalars[k].append(make_value(k, d[k], file, d[k].get("page"), texts, m.key))
        for k in TOTAL_FIELDS:
            totals[k].append(make_value(k, d["totals"][k], file, d["totals"][k].get("page"), texts, m.key))
        for b in d["buildings"]:
            building = {k: make_value(k, b[k], file, b[k].get("page"), texts, m.key) for k in BUILDING_FIELDS}
            building["floor_heights"] = [
                {**make_value("height", x, file, x.get("page"), texts, m.key), "level": x.get("level")}
                for x in b["floor_heights"]]
            buildings.append(building)
    return {
        "doc": "conclusion", "model": m.key,
        "scalars": {k: merge(k, v, m.key) for k, v in scalars.items()},
        "totals": {k: merge(k, v, m.key) for k, v in totals.items()},
        "buildings": buildings, "requests": requests, "warnings": prep.warnings,
    }


def read_project(m: llm.Model, files: list[tuple[Path, list[Page], dict[int, classify.SheetType]]],
                 raw_dir: Path) -> dict:
    """The project album (one or several PDFs). Pages of each file are chunked separately."""
    scalars: dict[str, list[dict]] = {k: [] for k in PROJECT_SCALARS}
    breakdown: list[dict] = []
    heights: list[dict] = []
    rooms: list[dict] = []
    requests: list[dict] = []
    warnings: list[str] = []
    for pdf_path, pages, kinds in files:
        prep = project_chunks(pdf_path, pages, kinds)
        warnings.extend(prep.warnings)
        texts = page_texts(pages, prep.chunks)
        file = pdf_path.name
        for c, r in ask_chunks(m, prep.chunks, raw_dir):
            requests.append(_record(c, r))
            d, page = r.data, c.pages[0]
            if c.kind == "tep":
                for k in PROJECT_SCALARS:
                    scalars[k].append(make_value(k, d[k], file, page, texts, m.key))
                for x in d["area_breakdown"]:
                    breakdown.append({**make_value("area", x, file, page, texts, m.key),
                                      "name": x["name"], "level_mark": x.get("level_mark")})
                for x in d["floor_heights"]:
                    heights.append({**make_value("height", x, file, page, texts, m.key), "level": x.get("level")})
            else:
                for x in d["rooms"]:
                    area = make_value("area", x, file, page, texts, m.key, value_key="area")
                    volume = None
                    if x.get("volume"):
                        volume = make_value("volume", x, file, page, texts, m.key, value_key="volume")
                        volume["unit"], volume["flags"] = None, []
                    rooms.append({"name": x["name"], "level": x.get("level"), "area": area, "volume": volume})
    return {
        "doc": "project", "model": m.key,
        "scalars": {k: merge(k, v, m.key) for k, v in scalars.items()},
        "area_breakdown": _dedupe(breakdown), "floor_heights": _dedupe(heights),
        "rooms": rooms, "requests": requests, "warnings": warnings,
    }


def _dedupe(items: list[dict]) -> list[dict]:
    """The same source (file, page, snippet, value) arriving from two chunks is kept once."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for v in items:
        key = (v["file"], v["page"], verify.normalize(v["source_text"] or ""), v["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def sheet_type_by_model(m: llm.Model, page: Page, raw_dir: Path) -> str:
    text = f"Title block:\n{stamp_text(page)[:1500]}\n\nHeadings:\n{classify.heading_text(page)[:1500]}"
    return str(llm.ask(m, SHEET_SYSTEM, text, SHEET_SCHEMA, MAX_TOKENS["sheet"], raw_dir).data["kind"])


def suggest_categories(m: llm.Model, names: list[str], raw_dir: Path) -> dict[str, str]:
    """Model suggestion for room names missing from the dictionary (staff must confirm)."""
    out: dict[str, str] = {}
    for i in range(0, len(names), 40):
        batch = names[i:i + 40]
        text = "Room names:\n" + "\n".join(f"{n + 1}. {name}" for n, name in enumerate(batch))
        d = llm.ask(m, CATEGORY_SYSTEM, text, CATEGORY_SCHEMA, MAX_TOKENS["category"], raw_dir).data
        returned = [x["name"] for x in d["rooms"]]
        if returned != batch:
            raise llm.MalformedResponse(f"{m.key}: toifalash javobida nomlar ro'yxati so'rovdagidan farq qiladi: "
                                        f"{returned!r} != {batch!r}")
        out.update({x["name"]: x["category"] for x in d["rooms"]})
    return out


# ---------- level marks from a section sheet (code, no model) ----------

_LEVEL = re.compile(r"^[+\-−±]?\d{1,3}[.,]\d{3}$")


def level_marks(page: Page) -> list[dict]:
    """Level marks on a section sheet (e.g. +3.300, -3.200, ±0.000): unique, ascending."""
    found: dict[str, dict] = {}
    for w in page.words:
        if not _LEVEL.match(w.text):
            continue
        v = verify.parse_number(w.text)
        if v is None:
            continue
        found.setdefault(str(v), {"number": str(v), "source_text": w.text, "file": page.file, "page": page.number})
    return sorted(found.values(), key=lambda x: Decimal(x["number"]))
