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

_BASEMENT = re.compile(r"подвал|цокол|техподпол|yerto['ʼ’`]?la|ертўла|podval|sokol", re.I)
_TECHNICAL = re.compile(r"\bтех|чердак|кровл|texnik|техник|cherdak", re.I)
_FLOOR_WORD = r"(?:этаж\w*|qavat\w*|қават\w*)"
_RANGE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)\s*[-–]?\s*" + _FLOOR_WORD, re.I)
_SINGLE = re.compile(r"(\d+)\s*[-–]?\s*(?:й|го|ой|ый|chi|nchi|инчи|чи)?\s*[-–]?\s*" + _FLOOR_WORD
                     + r"|" + _FLOOR_WORD + r"\s*(\d+)", re.I)
_TYPICAL = re.compile(r"типов|этаж|qavat|қават", re.I)


@dataclass(frozen=True)
class Level:
    kind: str                 # "basement" | "floor" | "range" | "technical" | "generic" | "unknown"
    first: int | None = None
    last: int | None = None

    def floors(self) -> list[int]:
        if self.kind == "floor" and self.first is not None:
            return [self.first]
        if self.kind == "range" and self.first is not None and self.last is not None:
            return list(range(self.first, self.last + 1))
        return []


def parse_level(text: str | None, mark: str | None = None) -> Level:
    """Understand a level description such as "Подвал", "1-этаж", "2–16 этажи", "+52.420"."""
    t = (text or "").replace("ё", "е")
    if _BASEMENT.search(t):
        return Level("basement")
    m = _RANGE.search(t)
    if m and int(m.group(1)) < int(m.group(2)):
        return Level("range", int(m.group(1)), int(m.group(2)))
    m = _SINGLE.search(t)
    if m:
        return Level("floor", int(m.group(1) or m.group(2)))
    if _TECHNICAL.search(t):
        return Level("technical")
    number = verify.parse_number(mark) if mark else None
    if number is not None and number < 0:
        return Level("basement")
    if _TYPICAL.search(t):
        return Level("generic")
    return Level("unknown")


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
                out.append(Height(parse_level(h.get("level")), value, origin, h.get("level") or ""))
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
                     overrides: dict[str, dict]) -> list[dict]:
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
        if category is None:
            if name in suggestions:
                category, method, note = suggestions[name], "model", "Lug'atda yo'q; model taklifi"
            else:
                category, method, note = "other", "none", "Lug'atda yo'q; toifa aniqlanmadi"
            needs = True
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


def hb_volume(project: dict, conclusion_building: dict | None) -> dict:
    p = project["scalars"]["volume_total"]
    proposal = _accepted_number(p)
    c = conclusion_building["volume"] if conclusion_building else None
    check = verify.check_equal("hb_project_vs_conclusion", "Hb: loyiha va xulosa", proposal, _accepted_number(c))
    notes: list[str] = []
    if proposal is None:
        notes.append(f"loyihada jami qurilish hajmi {verify.STATUS_LABELS[p['status']]}"
                     + (f" ({p['reason']})" if p.get("reason") else ""))
    return {"code": "Hb", "proposal": None if proposal is None else str(proposal), "complete": proposal is not None,
            "formula": "loyihadagi jami qurilish hajmi" if proposal is not None else "aniqlanmadi",
            "notes": notes, "check": check.dict(), "component_ids": []}


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


def industrial_floors(components: list[dict]) -> list[tuple[str, Decimal, Decimal]]:
    """(name, area, height) rows with a known height, for the industrial 3 m indicator."""
    return [(c["name"], Decimal(c["area"]), Decimal(c["height"])) for c in components
            if c["origin"] == "breakdown" and c["area"] is not None and c["height"] is not None]
