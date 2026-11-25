"""Classification: (1) sheet type from the title block and headings; (2) room name -> Ha / Ht / Hu / other.

Rules and the dictionary in code come first; when they are ambiguous the model makes a
suggestion (via extract.py) and such a suggestion is flagged "needs staff confirmation".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ingest import Page, group_lines, stamp_text

SHEET_TYPES: tuple[str, ...] = (
    "general_data", "site_plan", "floor_plan", "section", "explication", "finishing_schedule", "other",
)
SHEET_LABELS: dict[str, str] = {
    "general_data": "Umumiy ma'lumotlar", "site_plan": "Bosh reja", "floor_plan": "Qavat rejasi",
    "section": "Kesim", "explication": "Eksplikatsiya", "finishing_schedule": "Pardozlash jadvali", "other": "Boshqa",
}

# Order matters: more specific rules first.
_SHEET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("general_data", re.compile(r"общи[еx]\s+данны|umumiy\s+ma['ʼ’`]?lumot|умумий\s+маълумот", re.I)),
    ("finishing_schedule", re.compile(r"ведомость\s+отделки|отделк\w+\s+помещен|pardozlash|пардозлаш", re.I)),
    ("explication", re.compile(r"экспликаци\w+\s+(пол|помещ)|eksplikatsiya|экспликация", re.I)),
    ("site_plan", re.compile(r"генеральн\w+\s+план|генплан|ситуационн\w+\s+(план|схем)|bosh\s+reja|бош\s+режа", re.I)),
    ("section", re.compile(r"\bразрез|\bkesim|\bкесим", re.I)),
    ("floor_plan", re.compile(
        r"план\s+(на\s+отм|\d|подвал|техпод|цокол|типов|перв|втор|этаж|черда|техническ|кровл)"
        r"|план\b[^\n]{0,40}этаж|qavat\w*\s+reja|қават\w*\s+режа|\breja\w*\s+\d|yerto['ʼ’`]?la\s+reja", re.I)),
)

# Markers that make a page worth reading wherever they occur on it.
MARK_TEP = re.compile(
    r"технико[\s-]*экономическ|\bТЭП\b|строительн\w+\s+объ[её]м|площадь\s+застройки"
    r"|texnik[\s-]*iqtisodiy|qurilish\s+hajmi|qurilish\s+maydoni|техник[\s-]*иқтисодий|қурилиш\s+ҳажми", re.I)
MARK_ROOMS = re.compile(
    r"экспликаци\w+\s+(пол|помещ)|ведомость\s+отделки|наименование\s+помещ|xonalar\s+eksplikatsiya"
    r"|xona\w*\s+nomi|хона\w*\s+номи", re.I)


@dataclass(frozen=True)
class SheetType:
    kind: str
    method: str        # "rule:stamp" | "rule:heading" | "model" | "ambiguous" | "textless"
    ambiguous: bool
    basis: str         # the text the decision was based on (short)
    has_tep: bool      # technical-economic indicators present
    has_rooms: bool    # room table present


def _matching_kinds(text: str) -> list[str]:
    return [kind for kind, rule in _SHEET_RULES if rule.search(text)]


def heading_text(page: Page, count: int = 12) -> str:
    """The lines with the largest glyphs on the page (sheet and table headings)."""
    lines = [ln for ln in group_lines(page.words) if re.search(r"[^\W\d_]{4,}", ln.text)]
    lines.sort(key=lambda ln: -(ln.y1 - ln.y0))
    return "\n".join(ln.text for ln in lines[:count])


def sheet_type(page: Page) -> SheetType:
    """Sheet type by rules. `ambiguous=True` unless there is exactly one clear match."""
    if page.textless:
        return SheetType("other", "textless", False, "", False, False)
    tep = bool(MARK_TEP.search(page.text))
    rooms = bool(MARK_ROOMS.search(page.text))
    stamp = stamp_text(page)
    kinds = _matching_kinds(stamp)
    if len(kinds) == 1:
        return SheetType(kinds[0], "rule:stamp", False, _short(stamp), tep, rooms)
    if len(kinds) > 1:
        # several names in the stamp (e.g. "План 1 этажа. Экспликация полов"): take the first, but ambiguous
        return SheetType(kinds[0], "rule:stamp", True, _short(stamp), tep, rooms)
    heading = heading_text(page)
    kinds = _matching_kinds(heading)
    if len(kinds) == 1:
        return SheetType(kinds[0], "rule:heading", False, _short(heading), tep, rooms)
    if len(kinds) > 1:
        return SheetType(kinds[0], "rule:heading", True, _short(heading), tep, rooms)
    return SheetType("other", "ambiguous", True, _short(stamp or heading), tep, rooms)


def _short(text: str, length: int = 200) -> str:
    return re.sub(r"\s+", " ", text).strip()[:length]


# ---------- room categories ----------

CATEGORIES: tuple[str, ...] = ("Ha", "Ht", "Hu", "other")
CATEGORY_LABELS: dict[str, str] = {
    "Ha": "Avtoturargoh", "Ht": "Texnik", "Hu": "Umumiy foydalanish", "other": "Boshqa",
}

# Matched by stem (lower case, ё→е). Russian, Uzbek Latin and Uzbek Cyrillic.
_DICTIONARY: dict[str, tuple[str, ...]] = {
    "Ha": (
        "паркинг", "автостоянк", "автопарковк", "парковк", "гараж", "машино-мест", "машиномест",
        "avtoturargoh", "avtomobil saqlash", "parking", "garaj", "автотураргоҳ", "автотураргох",
    ),
    "Ht": (
        "насосн", "электрощитов", "венткамер", "вентиляционн", "тех. помещ", "тех.помещ", "техпомещ",
        "техническое помещ", "технические помещ", "техническ", "техподполь", "технический этаж",
        "тепловой пункт", "теплов", "итп", "водомерн", "машинное отделение", "машинное помещ",
        "серверн", "котельн", "бойлерн", "узел учета", "узел ввода", "узел в вод", "в вод", "ввод", "щитов", "кроссов", "дгу",
        "трансформатор", "мусорокамер", "мусоросборн",
        "texnik xona", "texnik qavat", "texnik", "nasos", "elektr shchit", "elektr shit", "elektroshit",
        "shchit xona", "shit xona", "ventilyatsiya", "ventkamera", "qozonxona", "issiqlik punkti",
        "suv o'lchash", "mashina bo'limi", "техник хона", "техник қават", "насос", "қозонхона",
    ),
    "Hu": (
        "лестничная клетка", "лестничн", "лестниц", "лифтовой холл", "лифтов", "лифт",
        "приквартирн", "поэтажн", "межквартирн", "общий коридор", "коридор общ", "коридор", "вестибюл",
        "тамбур", "холл", "колясочн", "консьерж", "инвентарн", "уборочн", "комната уборочного",
        "мусоропровод", "общего пользования", "площадк",
        "zinapoya", "zina katagi", "zina", "lift xolli", "lift", "koridor", "yo'lak", "yolak", "vestibyul",
        "tambur", "xoll", "umumiy foydalanish", "kirish yo'lagi", "зинапоя", "зина", "лифт холли",
        "йўлак", "умумий фойдаланиш",
    ),
    "other": (
        "квартир", "жилая", "жилые", "комната", "гостиная", "спальн", "кухня", "кухн", "санузел", "ванная",
        "прихожая", "лоджия", "балкон", "терраса", "офис", "магазин", "торгов", "кабинет", "зал",
        "кладов", "гардероб", "класс", "палата", "склад", "цех", "производствен",
        "xonadon", "yashash", "turar joy", "oshxona", "yotoqxona", "mehmonxona", "hammom", "sanuzel",
        "hojatxona", "dahliz", "lodjiya", "balkon", "ofis", "do'kon", "dokon", "savdo", "kabinet",
        "omborxona", "sex", "ishlab chiqarish", "хонадон", "яшаш", "ошхона", "ётоқхона", "дўкон",
    ),
}


def name_key(name: str) -> str:
    """How a room name is looked up in the staff-kept list of names: case, spacing and apostrophes ignored."""
    return _normalize(name)


def _normalize(name: str) -> str:
    n = name.lower().replace("ё", "е")
    n = re.sub(r"[ʼ’`‘]", "'", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass(frozen=True)
class RoomCategory:
    category: str | None         # None: not in the dictionary (the model has to suggest)
    method: str                  # "dictionary" | "dictionary:mixed" | "none"
    needs_confirmation: bool
    matched: tuple[str, ...]     # the stems that matched
    note: str = ""


def room_category(name: str) -> RoomCategory:
    """Category by dictionary. A name with words of several categories (a group name) is "mixed"."""
    n = _normalize(name)
    found: dict[str, list[tuple[int, str]]] = {}
    taken: list[tuple[int, int]] = []
    # longer stems first; the same span is not counted twice
    candidates = sorted(((stem, cat) for cat, stems in _DICTIONARY.items() for stem in stems),
                        key=lambda x: -len(x[0]))
    for stem, cat in candidates:
        for m in re.finditer(re.escape(stem), n):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            found.setdefault(cat, []).append((m.start(), stem))
    if not found:
        return RoomCategory(None, "none", True, ())
    special = {c: v for c, v in found.items() if c != "other"}
    matched = tuple(stem for _, stem in sorted(x for v in found.values() for x in v))
    if not special:
        return RoomCategory("other", "dictionary", False, matched)
    if len(special) == 1 and "other" not in found:
        return RoomCategory(next(iter(special)), "dictionary", False, matched)
    # Mixed: suggest the special category with the most matching words; staff confirms.
    suggestion = max(special, key=lambda c: (len(special[c]), -min(p for p, _ in special[c])))
    parts = ", ".join(f"{CATEGORY_LABELS[c]}: {', '.join(stem for _, stem in v)}" for c, v in sorted(found.items()))
    return RoomCategory(suggestion, "dictionary:mixed", True, matched,
                        f"Nomda bir nechta toifa so'zlari bor ({parts}); taklif — {suggestion}")
