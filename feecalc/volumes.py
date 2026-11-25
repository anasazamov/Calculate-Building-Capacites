"""Volumes (code only): Hb, Ha, Ht, Hu and a proposal for Hyu.

- Hb: total building volume from the project, compared with the conclusion.
- Ha / Ht / Hu: a volume stated in the document is used as is; when only an area is given,
  volume = area x height of that level. Such a volume is marked "computed", its formula is
  shown, and staff may change the height or the result (with a reason).
- Hyu: staff enters the permitted number of floors; code proposes the volume of the floors above.

Everything here is a proposal: nothing reaches the fee until staff confirms each volume.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import classify, verify
from .fee import fmt_num

VOLUME_CODES: tuple[str, ...] = ("Hb", "Hyu", "Ha", "Ht", "Hu")
VOLUME_LABELS: dict[str, str] = {
    "Hb": "Binoning umumiy hajmi", "Hyu": "Ruxsat etilgan qavatlar sonidan yuqori hajm",
    "Ha": "Avtoturargoh hajmi", "Ht": "Texnik qavat va xonalar hajmi",
    "Hu": "Umumiy foydalanishdagi qism hajmi",
}

# Level descriptions are matched on their transliterated form (Cyrillic -> Latin, apostrophes dropped),
# so "подвал", "yerto‘la" and "yerto'la" all reduce to the same spelling.
_BASEMENT = re.compile(r"podval|podzemn|tsokol|texpodpol|yertola|ertola|yer osti|sokol")
_TECHNICAL = re.compile(r"\btex|cherdak|krovl|chordoq")
_MANSARD = re.compile(r"mansard")
_FOUNDATION = re.compile(r"poydevor|fundament|tsokol qism")
_FLOOR_WORD = r"(?:etaj\w*|qavat\w*)"
_RANGE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)\s*[-–]?\s*(?:go\s+)?" + _FLOOR_WORD)
_PAIR = re.compile(r"(\d+)\s*-?\s*(?:chi|nchi|inchi)?\s+va\s+(\d+)\s*-?\s*" + _FLOOR_WORD)
_LIST = re.compile(r"(\d+(?:\s*,\s*\d+)+)\s*[-–]?\s*" + _FLOOR_WORD)
_SINGLE = re.compile(r"(\d+)\s*[-–]?\s*(?:y|go|oy|iy|chi|nchi|inchi)?\s*[-–]?\s*" + _FLOOR_WORD
                     + r"|" + _FLOOR_WORD + r"\s*(\d+)")
_TYPICAL = re.compile(r"tipov|etaj|qavat|xona|pomeshen|komnat")


@dataclass(frozen=True)
class Level:
    kind: str                 # "basement" | "floor" | "range" | "technical" | "foundation" | "generic" | "unknown"
    first: int | None = None
    last: int | None = None

    def floors(self) -> list[int]:
        if self.kind == "floor" and self.first is not None:
            return [self.first]
        if self.kind == "range" and self.first is not None and self.last is not None:
            return list(range(self.first, self.last + 1))
        return []


def _floor_level(t: str) -> Level | None:
    m = _LIST.search(t)
    if m:
        numbers = [int(x) for x in re.findall(r"\d+", m.group(1))]
        return Level("range", min(numbers), max(numbers))
    for pattern in (_RANGE, _PAIR):
        m = pattern.search(t)
        if m and int(m.group(1)) < int(m.group(2)):
            return Level("range", int(m.group(1)), int(m.group(2)))
    m = _SINGLE.search(t)
    if m:
        return Level("floor", int(m.group(1) or m.group(2)))
    return None


def parse_levels(text: str | None, mark: str | None = None) -> list[Level]:
    """Every level a description refers to. "Yerto'la qavati va qavatlarning balandligi" names two:
    the basement and the typical floors."""
    t = verify.transliterate(text or "")
    if _FOUNDATION.search(t):
        return [Level("foundation")]
    if _MANSARD.search(t):
        return [Level("technical")]  # attic storey: never taken as a typical floor height
    levels: list[Level] = []
    if _BASEMENT.search(t):
        levels.append(Level("basement"))
        t = re.sub(r"(?:" + _BASEMENT.pattern + r")\w*(?:\s+qavat\w*|\s+etaj\w*)?", " ", t)
    floor = _floor_level(t)
    if floor:
        levels.append(floor)
    elif levels:
        if _TYPICAL.search(t):
            levels.append(Level("generic"))
    elif _TECHNICAL.search(t):
        levels.append(Level("technical"))
    else:
        number = verify.parse_number(mark) if mark else None
        if number is not None and number < 0:
            levels.append(Level("basement"))
        elif _TYPICAL.search(t):
            levels.append(Level("generic"))
    return levels or [Level("unknown")]


def parse_level(text: str | None, mark: str | None = None) -> Level:
    """The (first) level of a description such as "Подвал", "1-этаж", "2–16 этажи", "+52.420"."""
    return parse_levels(text, mark)[0]


def _accepted_number(v: dict | None) -> Decimal | None:
    if v and v.get("status") == verify.ACCEPTED and v.get("number") is not None:
        return Decimal(v["number"])
    return None


@dataclass(frozen=True)
class Height:
    level: Level
    value: Decimal
    origin: str     # "loyiha" | "xulosa"
    label: str      # level text as written


def collect_heights(project_heights: list[dict], conclusion_heights: list[dict]) -> list[Height]:
    out: list[Height] = []
    for origin, items in (("loyiha", project_heights), ("xulosa", conclusion_heights)):
        for h in items:
            value = _accepted_number(h)
            if value is not None and value > 0:
                out.extend(Height(level, value, origin, h.get("level") or "") for level in parse_levels(h.get("level")))
    return out


def resolve_height(level: Level, heights: list[Height]) -> tuple[Decimal | None, str]:
    """Height for a level -> (height, explanation). Project heights win over conclusion heights."""
    for origin in ("loyiha", "xulosa"):
        own = [h for h in heights if h.origin == origin]
        if level.kind == "basement":
            hit = [h for h in own if h.level.kind == "basement"]
        elif level.kind == "technical":
            hit = [h for h in own if h.level.kind == "technical"]
        elif level.kind in ("floor", "range"):
            wanted = set(level.floors())
            hit = [h for h in own if h.level.kind in ("floor", "range") and wanted <= set(h.level.floors())]
            if not hit:
                hit = [h for h in own if h.level.kind == "generic"]
        else:
            hit = []
        distinct = {h.value for h in hit}
        if len(distinct) == 1:
            return hit[0].value, f"{origin}: «{hit[0].label}» balandligi {fmt_num(hit[0].value)} m"
        if len(distinct) > 1:
            return None, f"{origin}da shu sath uchun turli balandliklar: " + ", ".join(sorted(map(fmt_num, distinct)))
    if level.kind == "unknown":
        above = {h.value for h in heights if h.level.kind in ("floor", "range", "generic")}
        if len(above) == 1:
            v = next(iter(above))
            return v, f"sath aniqlanmagan; hujjatdagi yagona qavat balandligi {fmt_num(v)} m olindi (tasdiqlang)"
        return None, "sath aniqlanmagan — balandlikni xodim kiritadi"
    return None, "shu sath uchun balandlik hujjatda topilmadi — xodim kiritadi"


def component_id(origin: str, file: str | None, page: int | None, name: str, value: str | None) -> str:
    raw = f"{origin}|{file}|{page}|{name}|{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def build_components(project: dict, heights: list[Height], suggestions: dict[str, str],
                     overrides: dict[str, dict], names: dict[str, str] | None = None) -> list[dict]:
    """Rooms and area-breakdown rows as volume components, with category, height and volume.

    `suggestions`: model-suggested categories for names missing from the dictionary.
    `overrides`: staff changes per component id: category / height / volume / included (+ reason).
    """
    rows: list[tuple[str, dict, dict | None, str, str | None, str | None]] = []
    for b in project.get("area_breakdown", []):
        rows.append(("breakdown", b, None, b["name"], b["name"], b.get("level_mark")))
    for r in project.get("rooms", []):
        rows.append(("room", r["area"], r.get("volume"), r["name"], r.get("level"), None))

    components: list[dict] = []
    for origin, area_v, volume_v, name, level_text, mark in rows:
        cat = classify.room_category(name)
        category, method, needs, note = cat.category, cat.method, cat.needs_confirmation, cat.note
        level = parse_level(level_text, mark)
        if category is None and origin == "breakdown" and level.kind in ("basement", "floor", "range"):
            # a plain level row ("1-этаж", "Подвал ..."): not a room group
            category, method, needs, note = "other", "level_row", False, "Sath qatori (xona guruhi emas)"
        if category is None and classify.name_key(name) in (names or {}):
            # a name staff have decided once for all jobs (the reference list of room names)
            category, method, needs, note = (names or {})[classify.name_key(name)], "reference", False, "Ma'lumotnomadagi nomlar ro'yxati bo'yicha"
        if category is None:
            # Rule: a room or area that the documents do not clearly name as parking, technical or common-use
            # is ordinary usable area and is not deducted. A model's guess is shown, never applied.
            guess = suggestions.get(name)
            category, method, needs = "other", "not_explicit", False
            note = ("Texnik yoki umumiy foydalanish deb aniq ko'rsatilmagan — foydalanish maydoni deb olindi"
                    + (f" (model taxmini: {guess})" if guess and guess != "other" else ""))
        cid = component_id(origin, area_v.get("file"), area_v.get("page"), name, area_v.get("value"))
        ov = overrides.get(cid, {})
        if ov.get("category"):
            category, method, needs = ov["category"], "staff", False
        area = _accepted_number(area_v)
        stated = _accepted_number(volume_v)
        height, height_note = resolve_height(level, heights)
        if ov.get("height") is not None:
            height, height_note = Decimal(ov["height"]), "xodim kiritgan balandlik"
        volume: Decimal | None
        if ov.get("volume") is not None:
            volume, computed = Decimal(ov["volume"]), False
            formula = f"xodim kiritgan hajm: {fmt_num(volume)} m³"
        elif stated is not None and ov.get("height") is None:
            volume, computed, formula = stated, False, f"hujjatda yozilgan hajm: {fmt_num(stated)} m³"
        elif area is not None and height is not None:
            volume, computed = area * height, True
            formula = f"{fmt_num(area)} m² × {fmt_num(height)} m = {fmt_num(area * height)} m³ (hisoblangan)"
        else:
            volume, computed = None, False
            formula = ("maydon qabul qilinmagan" if area is None else "balandlik yo'q") + " — hajm aniqlanmadi"
        components.append({
            "id": cid, "origin": origin, "name": name, "level_text": level_text, "level_kind": level.kind,
            "level_first": level.first, "level_last": level.last,
            "area": None if area is None else str(area), "area_status": area_v.get("status"),
            "file": area_v.get("file"), "page": area_v.get("page"), "source_text": area_v.get("source_text"),
            "category": category, "category_method": method, "needs_confirmation": needs, "category_note": note,
            "height": None if height is None else str(height), "height_note": height_note,
            "volume": None if volume is None else str(volume), "computed": computed, "formula": formula,
            "override": ov or None, "included": True, "exclusion_note": "",
        })
    _apply_default_inclusion(components, overrides)
    return components


def _apply_default_inclusion(components: list[dict], overrides: dict[str, dict]) -> None:
    """Avoid double counting: when the indicators table already has rows of a category, individual
    room-table rows of that category are left out by default (staff may include them)."""
    in_breakdown = {c["category"] for c in components if c["origin"] == "breakdown"}
    for c in components:
        if c["category"] == "other":
            c["included"] = False
            continue
        if c["origin"] == "room" and c["category"] in in_breakdown:
            c["included"] = False
            c["exclusion_note"] = ("ko'rsatkichlar jadvalida shu toifadagi qator bor — takror sanalmasligi "
                                   "uchun sukut bo'yicha kiritilmagan")
        ov = overrides.get(c["id"], {})
        if ov.get("included") is not None:
            c["included"] = bool(ov["included"])
            c["exclusion_note"] = "" if c["included"] else "xodim chiqarib tashlagan"


def category_volume(code: str, components: list[dict]) -> dict:
    """Proposal for Ha / Ht / Hu from the included components of that category."""
    own = [c for c in components if c["category"] == code and c["included"]]
    missing = [c for c in own if c["volume"] is None]
    unconfirmed = [c for c in own if c["needs_confirmation"]]
    notes: list[str] = []
    if not own:
        return {"code": code, "proposal": "0", "complete": True, "formula": "0",
                "notes": ["hujjatdan bu toifaga kiradigan xona topilmadi — 0 taklif qilinadi (tasdiqlang)"],
                "component_ids": []}
    if unconfirmed:
        notes.append(f"{len(unconfirmed)} ta komponent toifasi xodim tasdiqlashini kutmoqda")
    if missing:
        notes.append(f"{len(missing)} ta komponent hajmi aniqlanmagan (balandlik yoki maydon yo'q) — yig'indi to'liq emas")
        return {"code": code, "proposal": None, "complete": False, "formula": "to'liq emas", "notes": notes,
                "component_ids": [c["id"] for c in own]}
    total = sum((Decimal(c["volume"]) for c in own), Decimal(0))
    formula = " + ".join(fmt_num(Decimal(c["volume"])) for c in own) + f" = {fmt_num(total)} m³"
    if any(c["computed"] for c in own):
        notes.append("hisoblangan (maydon × balandlik)")
    return {"code": code, "proposal": str(total), "complete": True, "formula": formula, "notes": notes,
            "component_ids": [c["id"] for c in own]}


# ---------- Hb: one row per building ----------

_NAME_NOISE = {"binosi", "bino", "binolari", "binolar", "zdanie", "va", "i"}


def _name_tokens(building: dict) -> set[str]:
    text = f"{(building.get('type') or {}).get('value') or ''} {building['name'].get('value') or ''}"
    return {x for x in re.split(r"[^a-z0-9]+", verify.transliterate(text)) if x and x not in _NAME_NOISE}


def _pair_score(p: dict, c: dict) -> tuple[float, str]:
    a, b = _name_tokens(p), _name_tokens(c)
    name = len(a & b) / min(len(a), len(b)) if a and b else 0.0
    score, why = name, [f"nom o'xshashligi {name:.2f}"] if name else []
    count = _accepted_number(p.get("count")) or _accepted_number(c.get("count"))
    for field, weight, label in (("volume", 1.0, "hajm"), ("footprint_area", 0.5, "qurilish maydoni")):
        check = verify.check_equal_scaled("x", "x", _accepted_number(p.get(field)), _accepted_number(c.get(field)), count)
        if check.status in (verify.OK, verify.ROUNDING):
            score += weight
            why.append(f"{label} mos")
    return score, ", ".join(why)


def match_buildings(project: list[dict], conclusion: list[dict],
                    manual: dict[str, int] | None = None) -> list[dict]:
    """Pair project buildings with conclusion buildings -> [{"p": index|None, "c": index|None, "basis": str}].

    Every conclusion building appears exactly once (in document order), unmatched project buildings
    follow. `manual` maps a project index (as a string) to a conclusion index, or to -1 for "no pair".
    """
    manual = manual or {}
    pair_of: dict[int, tuple[int, str]] = {}      # conclusion index -> (project index, basis)
    used_p: set[int] = set()
    for key, ci in manual.items():
        pi = int(key)
        if 0 <= pi < len(project):
            used_p.add(pi)
            if 0 <= ci < len(conclusion):
                pair_of[ci] = (pi, "xodim belgilagan")
    if len(project) == 1 and len(conclusion) == 1 and not manual:
        pair_of[0] = (0, "har ikki hujjatda bitta bino")
        used_p.add(0)
    candidates = sorted(((*_pair_score(p, c), pi, ci) for pi, p in enumerate(project)
                         for ci, c in enumerate(conclusion)), key=lambda x: -x[0])
    for score, why, pi, ci in candidates:
        if score >= 0.34 and pi not in used_p and ci not in pair_of:
            pair_of[ci] = (pi, f"avtomatik: {why} (xodim tekshirsin)")
            used_p.add(pi)
    pairs = [{"p": pair_of[ci][0] if ci in pair_of else None, "c": ci,
              "basis": pair_of[ci][1] if ci in pair_of else "loyihada mos bino topilmadi yoki loyiha berilmagan"}
             for ci in range(len(conclusion))]
    pairs.extend({"p": pi, "c": None, "basis": "xulosada mos bino topilmadi"}
                 for pi in range(len(project)) if pi not in {x["p"] for x in pairs})
    return pairs


def hb_rows(project: list[dict], conclusion: list[dict], pairs: list[dict], overrides: dict[str, dict]) -> list[dict]:
    """One Hb row per building (pair): the volume this building contributes, with its basis.

    - project volume consistent with the conclusion (equal, or equal to count x per-unit value,
      or a rounding difference): the project value is proposed;
    - no project: count x conclusion volume ("computed" when count > 1);
    - project and conclusion disagree, or the conclusion only refers to another building:
      no proposal for the row, staff decides.
    """
    rows: list[dict] = []
    for pair in pairs:
        p = project[pair["p"]] if pair["p"] is not None else None
        c = conclusion[pair["c"]] if pair["c"] is not None else None
        name_v = (c or p or {}).get("name", {})
        name = str(name_v.get("value") or "(nomsiz)")
        if c and (c.get("type") or {}).get("value"):
            name = f"{c['type']['value']} — {name}"
        pv, cv = (p or {}).get("volume"), (c or {}).get("volume")
        p_num, c_num = _accepted_number(pv), _accepted_number(cv)
        count_c, count_p = _accepted_number((c or {}).get("count")), _accepted_number((p or {}).get("count"))
        count = count_c or count_p or Decimal(1)
        count_note = ("xulosadan" if count_c else "loyihadan") if (count_c or count_p) else "yozilmagan (1 deb olindi)"
        reference = (c or {}).get("reference") or {}
        check = verify.check_equal_scaled(f"hb_row_{len(rows)}", f"Hb: {name}", p_num, c_num, count)
        proposal: Decimal | None = None
        computed = False
        if p_num is not None and c_num is not None:
            if check.status in (verify.OK, verify.ROUNDING):
                if p_num == c_num and count > 1:
                    proposal, computed = p_num * count, True
                    formula = f"{fmt_num(count)} dona × {fmt_num(p_num)} = {fmt_num(proposal)} m³ (hisoblangan: soni × birlik)"
                else:
                    proposal = p_num
                    formula = f"loyihada yozilgan hajm {fmt_num(p_num)} m³; xulosa bilan mos"
            else:
                formula = "loyiha va xulosa hajmi mos emas — qiymatni xodim tanlaydi"
        elif p_num is not None:
            proposal = p_num
            formula = f"loyihada yozilgan hajm {fmt_num(p_num)} m³ (xulosada bu bino hajmi yo'q)"
        elif c_num is not None:
            proposal, computed = c_num * count, count > 1
            if pv is None:
                why = "loyiha berilmagan"
            elif pv["status"] == verify.NOT_FOUND:
                why = "loyihada bu bino hajmi topilmadi"
            else:
                why = f"loyihadagi hajm qabul qilinmagan ({verify.STATUS_LABELS[pv['status']]})"
            formula = (f"{fmt_num(count)} dona × {fmt_num(c_num)} = {fmt_num(proposal)} m³ (hisoblangan: soni × birlik)"
                       if count > 1 else f"xulosada yozilgan hajm {fmt_num(c_num)} m³") + "; " + why
        elif reference.get("status") == verify.ACCEPTED:
            formula = f"hajm yozilmagan, faqat havola: «{reference['value']}» — qiymatni xodim kiritadi"
        else:
            formula = "hajm hujjatlardan qabul qilinmadi — qiymatni xodim kiritadi"
        cid = component_id("hb", (cv or pv or {}).get("file"), (cv or pv or {}).get("page"), name,
                           str((cv or pv or {}).get("value")))
        ov = overrides.get(cid, {})
        if ov.get("volume") is not None:
            proposal, computed = Decimal(ov["volume"]), False
            formula = f"xodim kiritgan hajm: {fmt_num(proposal)} m³"
        included = bool(ov["included"]) if ov.get("included") is not None else True
        rows.append({
            "id": cid, "name": name, "pair": pair, "count": str(count), "count_note": count_note,
            "project_name": str((p or {}).get("name", {}).get("value") or ""),
            "project": pv, "conclusion": cv, "reference": reference or None, "check": check.dict(),
            "volume": None if proposal is None else str(proposal), "computed": computed, "formula": formula,
            "included": included, "override": ov or None,
            "footprint": (c or p or {}).get("footprint_area"), "floors": (c or p or {}).get("floors"),
        })
    return rows


def hb_volume(rows: list[dict]) -> dict:
    """Hb proposal: the sum of the included building rows; incomplete while any of them has no volume."""
    own = [r for r in rows if r["included"]]
    notes: list[str] = []
    if not rows:
        return {"code": "Hb", "proposal": None, "complete": False, "formula": "aniqlanmadi",
                "notes": ["hujjatlardan hajmi bor bino o'qilmadi — Hb ni xodim kiritadi"], "component_ids": []}
    missing = [r for r in own if r["volume"] is None]
    excluded = [r for r in rows if not r["included"]]
    if excluded:
        notes.append(f"{len(excluded)} ta bino xodim tomonidan hisobdan chiqarilgan")
    if any(r["computed"] for r in own):
        notes.append("ayrim qatorlar hisoblangan (soni × bir dona hajmi)")
    if any(r["project"] is None or r["project"]["status"] != verify.ACCEPTED for r in own if r["volume"] is not None):
        notes.append("ayrim binolar uchun loyiha hajmi yo'q — xulosadagi qiymat olindi")
    if missing:
        notes.append(f"{len(missing)} ta bino hajmi aniqlanmagan: " + "; ".join(r["name"] for r in missing)
                     + " — yig'indi to'liq emas")
        return {"code": "Hb", "proposal": None, "complete": False, "formula": "to'liq emas", "notes": notes,
                "component_ids": [r["id"] for r in own]}
    total = sum((Decimal(r["volume"]) for r in own), Decimal(0))
    formula = " + ".join(fmt_num(Decimal(r["volume"])) for r in own) + f" = {fmt_num(total)} m³"
    return {"code": "Hb", "proposal": str(total), "complete": True, "formula": formula, "notes": notes,
            "component_ids": [r["id"] for r in own]}


def industrial_indicator(rows: list[dict]) -> tuple[list[dict], Decimal]:
    """Industrial buildings are charged for at most 3 m of height per storey. For every Hb row:
    footprint x 3 m x storeys, and the part of the volume above it. Shown only, never subtracted."""
    out: list[dict] = []
    total = Decimal(0)
    for r in rows:
        footprint, floors = _accepted_number(r["footprint"]), _accepted_number(r["floors"]) or Decimal(1)
        if not r["included"] or r["volume"] is None or footprint is None:
            continue
        count = Decimal(r["count"])
        within = footprint * 3 * floors * (count if r["computed"] else Decimal(1))
        excess = max(Decimal(0), Decimal(r["volume"]) - within)
        total += excess
        out.append({"name": r["name"], "volume": r["volume"], "footprint": str(footprint), "floors": str(floors),
                    "within_3m": str(within), "excess": str(excess)})
    return out, total


def _round2(v: Decimal) -> Decimal:
    """Proposals that involve a division are rounded to 0.01 m3."""
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def needs_model_suggestion(name: str, origin: str, level_text: str | None, mark: str | None) -> bool:
    """Should the model be asked for a category (name not in the dictionary and not a plain level row)."""
    if classify.room_category(name).category is not None:
        return False
    return not (origin == "breakdown" and parse_level(level_text, mark).kind in ("basement", "floor", "range"))


def floor_table(components: list[dict]) -> dict[int, tuple[Decimal, Decimal | None]]:
    """Per-floor (area, height) from area-breakdown rows; a range row is split equally."""
    table: dict[int, tuple[Decimal, Decimal | None]] = {}
    for c in components:
        if c["origin"] != "breakdown" or c["area"] is None or c["level_kind"] not in ("floor", "range"):
            continue
        floors = list(range(c["level_first"], (c["level_last"] or c["level_first"]) + 1))
        share = Decimal(c["area"]) / len(floors)
        for f in floors:
            table[f] = (share, None if c["height"] is None else Decimal(c["height"]))
    return table


def hyu_volume(permitted_floors: int | None, total_floors: Decimal | None, components: list[dict],
               volume_above_ground: Decimal | None) -> dict:
    """Proposal for the volume above the permitted number of floors."""
    base = {"code": "Hyu", "component_ids": []}
    if permitted_floors is None:
        return {**base, "proposal": None, "complete": False, "formula": "",
                "notes": ["ruxsat etilgan qavatlar sonini xodim kiritadi"]}
    if total_floors is None:
        return {**base, "proposal": None, "complete": False, "formula": "",
                "notes": ["binoning qavatlar soni hujjatdan qabul qilinmagan — Hyu ni xodim kiritadi"]}
    floors = int(total_floors)
    if permitted_floors >= floors:
        return {**base, "proposal": "0", "complete": True,
                "formula": f"ruxsat etilgan {permitted_floors} ≥ bino qavatlari {floors} → 0", "notes": []}
    extra = list(range(permitted_floors + 1, floors + 1))
    table = floor_table(components)
    if all(f in table and table[f][1] is not None for f in extra):
        parts = [table[f][0] * table[f][1] for f in extra]  # type: ignore[operator]
        total = _round2(sum(parts, Decimal(0)))
        return {**base, "proposal": str(total), "complete": True,
                "formula": (f"{permitted_floors + 1}–{floors}-qavatlar: Σ(qavat maydoni × balandlik) = "
                            f"{fmt_num(total)} m³"),
                "notes": ["taklif: qavat maydonlari ko'rsatkichlar jadvalidan (oraliq qatori qavatlarga teng "
                          "bo'lingan); ko'rsatkichlardagi qavat maydoni umumiy foydalanish maydonlarini o'z "
                          "ichiga olmasligi mumkin — tekshiring"]}
    if volume_above_ground is not None:
        total = _round2(volume_above_ground / floors * len(extra))
        return {**base, "proposal": str(total), "complete": True,
                "formula": (f"yer usti hajmi {fmt_num(volume_above_ground)} ÷ {floors} qavat × {len(extra)} qavat = "
                            f"{fmt_num(total)} m³"),
                "notes": ["taklif: qavatlar bo'yicha maydon/balandlik to'liq emas, yer usti hajmi qavatlarga "
                          "teng bo'lingan — tekshiring"]}
    return {**base, "proposal": None, "complete": False, "formula": "",
            "notes": ["qavat maydonlari, balandliklari va yer usti hajmi yetarli emas — Hyu ni xodim kiritadi"]}
