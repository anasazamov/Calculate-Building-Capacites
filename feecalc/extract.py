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
from .ingest import Page, group_lines, stamp_text, structured_text

CHUNK_CHARS = 9000            # text limit for one project request
CONCLUSION_CHARS = 36000      # a conclusion up to this size goes in one request
MAX_CHUNKS_PER_PAGE = 8
PARALLEL = 8
MAX_TOKENS = {"conclusion": 12000, "tep": 6144, "rooms": 6144, "sheet": 64, "category": 2048}
PROMPT_VERSION = "2026-11-24.6"

# Length bounds make a runaway string (a repetition loop inside a value) structurally impossible.
_S = {"type": ["string", "null"], "maxLength": 250}
_NAME = {"type": "string", "maxLength": 250}
SITE_KINDS = ("total", "built_up", "paving", "greenery", "other")
SITE_KIND_LABELS = {"total": "hudud maydoni (jami)", "built_up": "qurilish osti maydoni", "paving": "qoplama",
                    "greenery": "ko'kalamzor", "other": "boshqa"}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _value_schema(extra: dict | None = None, with_page: bool = False) -> dict:
    props: dict = dict(extra or {})
    props.update({"value": _S, "unit": _S, "source_text": _S})
    if with_page:
        props["page"] = {"type": ["integer", "null"]}
    return _obj(props)


def _site_schema(with_page: bool) -> dict:
    return {"type": "array", "items": _value_schema(
        {"name": _NAME, "kind": {"type": "string", "enum": list(SITE_KINDS)}}, with_page)}


CONCLUSION_SCALARS = ("conclusion_number", "conclusion_date", "application_number", "address", "customer",
                      "construction_type")
BUILDING_FIELDS = ("name", "type", "count", "floors", "footprint_area", "total_area", "usable_area", "volume",
                   "plan_size", "apartments", "reference")
TOTAL_FIELDS = ("footprint_area", "total_area", "volume")
PROJECT_SCALARS = ("project_code",)
PROJECT_BUILDING_FIELDS = ("name", "count", "floors", "footprint_area", "total_area", "volume",
                           "volume_above_ground", "volume_below_ground", "plan_size", "apartments")
# building fields that carry a number worth keeping a building for
KEY_NUMERIC_FIELDS = ("footprint_area", "total_area", "volume", "volume_above_ground", "volume_below_ground")

# field -> (numeric?, expected unit)
FIELD_KIND: dict[str, tuple[bool, str | None]] = {
    "conclusion_number": (False, None), "conclusion_date": (False, None), "application_number": (False, None),
    "address": (False, None), "customer": (False, None), "construction_type": (False, None),
    "project_code": (False, None), "name": (False, None), "type": (False, None), "plan_size": (False, None),
    "reference": (False, None),
    "floors": (True, None), "apartments": (True, None), "count": (True, None),
    "footprint_area": (True, "m2"), "total_area": (True, "m2"), "usable_area": (True, "m2"), "area": (True, "m2"),
    "site_area": (True, "m2"),
    "volume": (True, "m3"), "volume_above_ground": (True, "m3"), "volume_below_ground": (True, "m3"),
    "height": (True, "m"),
}
FIELD_LABELS: dict[str, str] = {
    "conclusion_number": "Xulosa raqami", "conclusion_date": "Xulosa sanasi", "application_number": "Murojaat raqami",
    "address": "Manzil", "customer": "Buyurtmachi", "construction_type": "Qurilish turi",
    "project_code": "Loyiha shifri", "name": "Nomi", "type": "Turi", "count": "Soni", "floors": "Qavatlar soni",
    "footprint_area": "Qurilish maydoni, m²", "total_area": "Umumiy maydon, m²", "usable_area": "Foydali maydon, m²",
    "volume": "Qurilish hajmi, m³", "volume_above_ground": "Hajm (yer usti), m³",
    "volume_below_ground": "Hajm (yer osti), m³", "plan_size": "Tarx o'lchami", "apartments": "Xonadonlar soni",
    "reference": "Havola",
}

CONCLUSION_SCHEMA = _obj({
    **{k: _value_schema(with_page=True) for k in CONCLUSION_SCALARS},
    "site_areas": _site_schema(True),
    "buildings": {"type": "array", "items": _obj({
        **{k: _value_schema(with_page=True) for k in BUILDING_FIELDS},
        "floor_heights": {"type": "array", "items": _value_schema({"level": _S}, with_page=True)},
    })},
    "totals": _obj({k: _value_schema(with_page=True) for k in TOTAL_FIELDS}),
})
TEP_SCHEMA = _obj({
    **{k: _value_schema() for k in PROJECT_SCALARS},
    "addresses": {"type": "array", "items": _value_schema()},
    "site_areas": _site_schema(False),
    "buildings": {"type": "array", "items": _obj({k: _value_schema() for k in PROJECT_BUILDING_FIELDS})},
    "area_breakdown": {"type": "array", "items": _value_schema({"name": _NAME, "level_mark": _S})},
    "floor_heights": {"type": "array", "items": _value_schema({"level": _S})},
})
ROOMS_SCHEMA = _obj({
    "rooms": {"type": "array", "items": _obj({
        "name": _NAME, "level": _S, "area": _S, "volume": _S, "unit": _S, "source_text": _S})},
})
SHEET_SCHEMA = _obj({"kind": {"type": "string", "enum": list(classify.SHEET_TYPES)}})
CATEGORY_SCHEMA = _obj({"rooms": {"type": "array", "items": _obj({
    "name": _NAME, "category": {"type": "string", "enum": list(classify.CATEGORIES)}})}})

_COMMON_RULES = """You copy data out of construction documents. The document is in Uzbek (Latin or Cyrillic script) or Russian, often mixed.
Strict rules:
1. Copy only values that are explicitly written in the given text. Never calculate, sum, multiply, guess, convert or fill in.
2. "value" is a string. For a number it is the number alone, exactly as written in the document (e.g. "36036", "9410.0", "600,3", "25 457", "-3.200"); do not add units, comparison signs or comments.
3. "unit" is the unit written next to that value, exactly as written (e.g. "м3", "m2", "m²", "м"); null if no unit is written.
4. "source_text" is one contiguous fragment copied character by character from the text (up to 150 characters) that contains the value itself, together with its label when the label is next to it. It may run over a line break when the sentence continues on the next line. For a number the fragment must include its label or, in a table, the whole row with the row's name: a fragment made of numbers only is rejected. Do not alter it, skip words in the middle, translate or reorder it. The " | " column separator may be kept.
5. If a value is not in the text: "value", "unit" and "source_text" are all null. An empty list is []. Most documents lack some fields: null is the expected answer for them.
6. Reply with JSON only, in the shape below. All keys are required."""

_V = '{"value": "<string or null>", "unit": "<string or null>", "source_text": "<string or null>"}'
_VP = ('{"value": "<string or null>", "unit": "<string or null>", "source_text": "<string or null>", '
       '"page": <n from the [[PAGE n]] marker, or null>}')
_SITE_NOTE = """- "site_areas": the land-plot balance (hudud ko'rsatkichlari / показатели участка): one element per stated area of the site. "kind" is: "total" for the area of the whole plot (hudud maydoni, yer maydoni, tanlangan yer maydoni / площадь участка); "built_up" for the area under buildings (qurilish maydoni, qurilish osti maydoni, binolar osti maydoni / площадь застройки) when stated for the site as a whole; "paving" for paving/roads (qoplama maydoni / покрытия); "greenery" for landscaping (ko'kalamzorlashtirish, obodonlashtirish / озеленение); "other" for anything else. "name" is the label as written."""

CONCLUSION_SYSTEM = _COMMON_RULES + f"""

This is the text of a state expertise conclusion (ekspertiza xulosasi / экспертное заключение). Pages are separated by [[PAGE n]] markers; for each value put the number of the page where it was found into "page".
Every V below has this shape: {_VP}
Response shape:
{{"conclusion_number": V, "conclusion_date": V, "application_number": V, "address": V, "customer": V, "construction_type": V,
 "site_areas": [{{"name": "<label as written>", "kind": "total" | "built_up" | "paving" | "greenery" | "other", "value": ..., "unit": ..., "source_text": ..., "page": ...}}],
 "buildings": [{{"name": V, "type": V, "count": V, "floors": V, "footprint_area": V, "total_area": V, "usable_area": V, "volume": V, "plan_size": V, "apartments": V, "reference": V,
   "floor_heights": [{{"level": "<which floor/level as written, or null>", "value": ..., "unit": ..., "source_text": ..., "page": ...}}]}}],
 "totals": {{"footprint_area": V, "total_area": V, "volume": V}}}}
Notes:
- "conclusion_number": the number of the conclusion itself; "application_number": the application number (murojaat raqami); "conclusion_date": its date as written.
- "customer": the customer / developer (buyurtmachi / заказчик). "construction_type": new construction, reconstruction, renovation etc. (yangi qurilish, rekonstruksiya, renovatsiya), as written.
- "address": the address of the object as written (region, district/city, street).
{_SITE_NOTE}
- "buildings": one element for every building, block or structure whose indicators are described (in a table row or in a text section such as "Blok №1 - ..."). Describe each building once, even if its section continues on the next page or it is mentioned again later. Keep them in document order.
- "name": the building's name as written (e.g. "Blok №3 – Ayvon", "2-tip"); "type": its kind if written separately (e.g. a "turi" column), else null.
- "count": how many identical buildings of this kind, only if written (e.g. "21 dona"); else null.
- "floors": number of storeys (qavatlar soni / этажность) as a number; "apartments": number of apartments (xonadonlar soni).
- "footprint_area": the building's built-up area (qurilish maydoni / qurilish osti maydoni / площадь застройки); "total_area": total floor area (umumiy maydon / общая площадь); "usable_area": usable area (foydali maydon / полезная площадь); "volume": building volume (qurilish hajmi / строительный объём). In a table the columns usually go: type, name, floors, footprint area, total area, volume.
- A volume is still a volume when its unit is misprinted (e.g. "Qurilish hajmi – 10640,00 m2"): copy the value and the unit as written.
- "plan_size": the building's dimensions in plan, as written (e.g. "26.97 × 26.89", "1,6x15,5").
- "reference": if the document gives no numbers for a building and instead says it is the same as another one (e.g. "Blok №3 bilan bir xil"), copy that statement here and leave the number fields null. Never copy the other building's numbers.
- "floor_heights": one element for each height stated for the building, in metres: storey heights, basement, attic/mansard, hall or shed height, foundation/plinth height. "level" says what the height refers to, in the document's words. A range such as "7,0 – 10,9" gives two elements (the lower and the upper value).
- "totals": the total row of the buildings table ("Jami" / "Итого" / "Всего") if there is one. It is not a building."""

TEP_SYSTEM = _COMMON_RULES + f"""

This is the text of one sheet of a project drawing album (general data, site plan or similar): tables are given as [TABLE n] blocks with one row per line, followed by the text outside tables (including the title block).
Every V below has this shape: {_V}
Response shape:
{{"project_code": V, "addresses": [V],
 "site_areas": [{{"name": "<label as written>", "kind": "total" | "built_up" | "paving" | "greenery" | "other", "value": ..., "unit": ..., "source_text": ...}}],
 "buildings": [{{"name": V, "count": V, "floors": V, "footprint_area": V, "total_area": V, "volume": V, "volume_above_ground": V, "volume_below_ground": V, "plan_size": V, "apartments": V}}],
 "area_breakdown": [{{"name": "<row label as written>", "level_mark": "<level mark such as -3.200, or null>", "value": ..., "unit": ..., "source_text": ...}}],
 "floor_heights": [{{"level": "<which floor/level, or null>", "value": ..., "unit": ..., "source_text": ...}}]}}
Notes:
- Many sheets (fire-alarm schemes, plans, details) contain none of this. Then every V is null and every list is [].
- "addresses": every statement of where the object is located that this sheet contains, each as its own element, as written: the address in the object title or title block, and lines such as "Район строительства: ...", "Адрес строительства: ...", "Manzil: ...". Do not merge separate statements. [] if the sheet states no location.
- "project_code": the project code (shifr / шифр), as written.
{_SITE_NOTE}
- "buildings": the buildings whose indicators this sheet states. A sheet with technical-economic indicators (технико-экономические показатели) of one building gives one element; a list of buildings and structures (binolar va inshootlar eksplikatsiyasi / экспликация зданий и сооружений) gives one element per row. Do not list buildings that have no number on this sheet.
- "name": the building's name as written; "count": the number of such buildings if written (Soni / кол-во); "floors": number of storeys (Qavati / этажность).
- "footprint_area": площадь застройки / qurilish osti maydoni; "total_area": общая площадь здания / umumiy maydoni; "volume": строительный объём (total) / qurilish hajmi; "volume_above_ground": надземная часть / выше отм. 0.000; "volume_below_ground": подземная часть / ниже отм. 0.000.
- "plan_size": the building's dimensions in plan (размеры в осях), as written. "apartments": количество квартир / xonadonlar soni.
- "area_breakdown": for a single building, the rows that make up its total area: areas by level, by floor or by room group (e.g. подвал, 1-этаж, 2–16 этажи, лестничная клетка etc., технические помещения). One element per row; the grand-total row goes to "total_area", not here. Do not merge or split rows.
- "floor_heights": floor heights explicitly written in the text (высота этажа / qavat balandligi), in metres. A range gives two elements."""

ROOMS_SYSTEM = _COMMON_RULES + """

This is the text of one sheet of a project drawing album with a room table (экспликация полов / экспликация помещений / ведомость отделки помещений / xonalar eksplikatsiyasi): tables are given as [TABLE n] blocks with one row per line.
Response shape:
{"rooms": [{"name": "<room name as written>", "level": "<floor or level mark if written, else null>", "area": "<area number or null>", "volume": "<volume number if the document states a volume, else null>", "unit": "<unit of the area or null>", "source_text": "<fragment copied character by character from that row, containing the name and the area>"}]}
Notes:
- One element per table row that names a room (or a group of rooms) and gives its area. If one row lists several room names, copy the name in full.
- Skip rows that have no area. Skip total rows (Итого / Всего / Jami). If the sheet has no room areas, return {"rooms": []}.
- "level": the floor or level the table (or the row) belongs to, if the sheet says so (e.g. in the table title: "План подвала на отм. -3.200").
- Rule 2 applies to "area" and "volume" as well: the number alone."""

SHEET_SYSTEM = """You are given the title block and headings of a sheet from a project drawing album. Decide the sheet type.
Types: general_data (общие данные / umumiy ma'lumotlar), site_plan (генплан / bosh reja), floor_plan (план этажа / qavat rejasi), section (разрез / kesim), explication (экспликация полов or помещений), finishing_schedule (ведомость отделки помещений), other (facades, details and anything else).
Reply with JSON only: {"kind": "<one of the types>"}"""

CATEGORY_SYSTEM = """You are given a list of room names from a building. Classify each name:
Ha - car parking (паркинг, автостоянка, garage); Ht - technical room or technical floor (pump room, switchboard room, ventilation chamber etc.);
Hu - common-use part of a residential building (staircase, lift lobby, common corridor, vestibule etc.); other - everything else (apartment, office, retail etc.).
Return the names unchanged and in the given order.
Reply with JSON only: {"rooms": [{"name": "<the given name>", "category": "Ha" | "Ht" | "Hu" | "other"}]}"""


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
            f"Xulosa {len(prep.chunks)} ta so'rovga bo'lib o'qildi; sahifa chegarasida bo'lingan bino "
            f"ko'rsatkichlari to'liq o'qilmagan bo'lishi mumkin")
    return prep


def project_chunks(pdf_path: Path, pages: list[Page], kinds: dict[int, classify.SheetType]) -> Prepared:
    """Chunks for the sheets worth reading: indicator sheets ("tep") and room-table sheets ("rooms")."""
    prep = Prepared()
    for p in pages:
        st = kinds[p.number]
        if p.textless:
            prep.warnings.append(f"{p.file}, {p.number}-sahifa: matnsiz (matn qatlami yo'q), o'qilmadi")
            continue
        tep = st.kind in ("general_data", "site_plan") or st.has_tep
        rooms = st.kind in ("explication", "finishing_schedule") or st.has_rooms
        if not tep and not rooms:
            continue
        if p.image_ratio >= 0.1:
            prep.warnings.append(
                f"{p.file}, {p.number}-sahifa: maydonining {round(p.image_ratio * 100)}% qismi rastr rasm — "
                f"ko'rsatkichlar rasm ichida bo'lsa o'qilmaydi (OCR yo'q)")
        lines = structured_text(pdf_path, p).splitlines()
        for kind, wanted in (("tep", tep), ("rooms", rooms)):
            if not wanted:
                continue
            pieces = _split(lines, CHUNK_CHARS)
            if len(pieces) > MAX_CHUNKS_PER_PAGE:
                prep.warnings.append(
                    f"{p.file}, {p.number}-sahifa ({kind}): matn {len(pieces)} bo'lak — faqat birinchi "
                    f"{MAX_CHUNKS_PER_PAGE} tasi o'qildi, qolgani O'QILMADI")
                pieces = pieces[:MAX_CHUNKS_PER_PAGE]
            if len(pieces) > 1:
                prep.warnings.append(f"{p.file}, {p.number}-sahifa ({kind}): matn {len(pieces)} so'rovga bo'lindi")
            prep.chunks.extend(Chunk("project", kind, p.file, [p.number], piece) for piece in pieces if piece.strip())
    return prep


# ---------- building and verifying a value record ----------

def make_value(field_name: str, raw: dict, file: str, page: int | None, texts: dict[int, list[str]],
               model: str, kind_key: str | None = None, value_key: str = "value") -> dict:
    """Turn the model's {value, unit, source_text} into a stored, verified record."""
    numeric, expected = FIELD_KIND[kind_key or field_name]
    value, source, unit = raw.get(value_key), raw.get("source_text"), raw.get("unit")
    if value is None or not str(value).strip():
        status, reason, number = verify.NOT_FOUND, "", None
    elif page is None or page not in texts:
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
        "unit_norm": expected if numeric else None, "file": file if status != verify.NOT_FOUND else None,
        "page": page if status != verify.NOT_FOUND else None, "source_text": source,
        "status": status, "reason": reason, "flags": flags, "model": model,
    }


def empty_value(field_name: str, model: str) -> dict:
    return {"field": field_name, "value": None, "number": None, "unit": None, "unit_norm": None, "file": None,
            "page": None, "source_text": None, "status": verify.NOT_FOUND, "reason": "", "flags": [], "model": model}


def _same(field_name: str, a: dict, b: dict) -> bool:
    if a["number"] is not None and b["number"] is not None:
        return Decimal(a["number"]) == Decimal(b["number"])
    if field_name == "address":
        return verify.same_address(str(a["value"]), str(b["value"]))[0]
    return verify.transliterate(verify.normalize(str(a["value"]))) == verify.transliterate(verify.normalize(str(b["value"])))


def merge(field_name: str, candidates: list[dict], model: str) -> dict:
    """One field found in several places: equal accepted values collapse into one; different
    accepted values become a "conflict" (not accepted; all variants are kept for staff)."""
    accepted = [c for c in candidates if c["status"] == verify.ACCEPTED]
    if accepted:
        groups: list[list[dict]] = []
        for c in accepted:
            home = [g for g in groups if _same(field_name, g[0], c)]
            if home:
                home[0].append(c)
            else:
                groups.append([c])
        main = dict(accepted[0])
        main["other_sources"] = [{"file": c["file"], "page": c["page"], "value": c["value"],
                                  "source_text": c["source_text"]} for c in accepted[1:]]
        main["variants"] = [{"file": g[0]["file"], "page": g[0]["page"], "value": g[0]["value"],
                             "source_text": g[0]["source_text"]} for g in groups]
        if len(groups) > 1:
            main["status"] = verify.CONFLICT
            main["reason"] = "hujjatning turli joylarida turli qiymat: " + "; ".join(
                f"«{g[0]['value']}» ({g[0]['page']}-sahifa)" for g in groups)
            main["number"] = None
        return main
    rejected = [c for c in candidates if c["status"] == verify.UNVERIFIED]
    return dict(rejected[0]) if rejected else empty_value(field_name, model)


def name_key(value: dict) -> str:
    """Key for matching the same building across chunks, models and documents."""
    return verify.transliterate(verify.normalize(str(value.get("value") or ""))).strip()


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



def _same_building(a: dict, b: dict) -> bool:
    """Do two records from different sheets describe the same building: equal names, one name
    continuing the other, or the same volume together with the same built-up area."""
    ka, kb = name_key(a["name"]), name_key(b["name"])
    if ka and kb and (ka == kb or (min(len(ka), len(kb)) >= 10 and (ka.startswith(kb) or kb.startswith(ka)))):
        return True
    numbers = [(a[f]["number"], b[f]["number"]) for f in ("volume", "footprint_area")]
    return all(x is not None and y is not None and Decimal(x) == Decimal(y) for x, y in numbers)


def _merge_buildings(buildings: list[dict], fields: tuple[str, ...], model: str) -> list[dict]:
    """Records of the same building coming from several sheets are merged field by field;
    values that disagree between sheets become a conflict."""
    groups: list[list[dict]] = []
    for b in buildings:
        home = [g for g in groups if any(_same_building(x, b) for x in g)]
        if home:
            home[0].append(b)
        else:
            groups.append([b])
    merged: list[dict] = []
    for group in groups:
        out = {f: merge(f, [b[f] for b in group], model) for f in fields if f != "name"}
        names = [b["name"] for b in group if b["name"]["status"] == verify.ACCEPTED]
        out["name"] = dict(max(names, key=lambda v: len(str(v["value"])))) if names else merge("name", [b["name"] for b in group], model)
        out["floor_heights"] = _dedupe([h for b in group for h in b.get("floor_heights", [])])
        merged.append(out)
    return merged


def read_conclusion(m: llm.Model, pages: list[Page], raw_dir: Path) -> dict:
    prep = conclusion_chunks(pages)
    texts = page_texts(pages, [])
    file = pages[0].file if pages else ""
    scalars: dict[str, list[dict]] = {k: [] for k in CONCLUSION_SCALARS}
    totals: dict[str, list[dict]] = {k: [] for k in TOTAL_FIELDS}
    buildings: list[dict] = []
    site: list[dict] = []
    requests: list[dict] = []
    for c, r in ask_chunks(m, prep.chunks, raw_dir):
        requests.append(_record(c, r))
        d = r.data
        for k in CONCLUSION_SCALARS:
            scalars[k].append(make_value(k, d[k], file, d[k].get("page"), texts, m.key))
        for k in TOTAL_FIELDS:
            totals[k].append(make_value(k, d["totals"][k], file, d["totals"][k].get("page"), texts, m.key))
        site.extend({**make_value("site_area", x, file, x.get("page"), texts, m.key),
                     "name": x["name"], "kind": x["kind"]} for x in d["site_areas"])
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
        "site_areas": _dedupe(site),
        "buildings": _merge_buildings(buildings, BUILDING_FIELDS, m.key) if len(prep.chunks) > 1 else buildings,
        "requests": requests, "warnings": prep.warnings,
    }


def read_project(m: llm.Model, files: list[tuple[Path, list[Page], dict[int, classify.SheetType]]],
                 raw_dir: Path) -> dict:
    """The project album (one or several PDFs). Every relevant sheet is one request."""
    scalars: dict[str, list[dict]] = {k: [] for k in PROJECT_SCALARS}
    addresses: list[dict] = []
    buildings: list[dict] = []
    site: list[dict] = []
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
                addresses.extend(make_value("address", x, file, page, texts, m.key) for x in d["addresses"])
                site.extend({**make_value("site_area", x, file, page, texts, m.key),
                             "name": x["name"], "kind": x["kind"]} for x in d["site_areas"])
                for b in d["buildings"]:
                    buildings.append({k: make_value(k, b[k], file, page, texts, m.key)
                                      for k in PROJECT_BUILDING_FIELDS})
                for x in d["area_breakdown"]:
                    breakdown.append({**make_value("area", x, file, page, texts, m.key),
                                      "name": x["name"], "level_mark": _checked_mark(x)})
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
    merged = _merge_buildings(buildings, PROJECT_BUILDING_FIELDS, m.key)
    kept = [b for b in merged if any(b[f]["status"] != verify.NOT_FOUND for f in KEY_NUMERIC_FIELDS)]
    if len(kept) < len(merged):
        dropped = [str(b["name"]["value"]) for b in merged if b not in kept]
        warnings.append(f"Loyihada {len(dropped)} ta bino nomi maydon/hajm ko'rsatkichisiz uchradi va binolar "
                        f"ro'yxatiga kiritilmadi: " + "; ".join(dropped[:6]))
    return {
        "doc": "project", "model": m.key,
        "scalars": {**{k: merge(k, v, m.key) for k, v in scalars.items()},
                    "address": merge("address", addresses, m.key)},
        "site_areas": _dedupe(site), "buildings": kept,
        "area_breakdown": _dedupe(breakdown), "floor_heights": _dedupe(heights),
        "rooms": rooms, "requests": requests, "warnings": warnings,
    }


def _checked_mark(row: dict) -> str | None:
    """A level mark is kept only if every number in it is present in the row's own source text."""
    mark, source = row.get("level_mark"), row.get("source_text")
    if not mark or not source:
        return None
    wanted = verify.numbers_in_text(mark)
    present = verify.numbers_in_text(verify.normalize(source))
    return mark if wanted and all(n in present for n in wanted) else None


def _dedupe(items: list[dict]) -> list[dict]:
    """The same source (file, page, snippet, value) arriving twice is kept once."""
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
