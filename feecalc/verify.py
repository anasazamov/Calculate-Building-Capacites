"""Checks (code only): verbatim source text, sums, project vs conclusion equality,
volume bounds, agreement of the two models.

Nothing is silently repaired: a value that fails verification becomes "unverified".
Detail strings are Uzbek because they are shown to staff.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .fee import fmt_num

ACCEPTED, UNVERIFIED, NOT_FOUND, CONFLICT = "accepted", "unverified", "not_found", "conflict"
STATUS_LABELS = {ACCEPTED: "qabul qilindi", UNVERIFIED: "tekshirilmadi", NOT_FOUND: "topilmadi",
                 CONFLICT: "ziddiyat"}

_SPACES = re.compile(r"[\s    |]+")
_TYPOGRAPHY = str.maketrans({"−": "-", "–": "-", "—": "-", "‒": "-", "‐": "-", "‑": "-",
                             "«": '"', "»": '"', "“": '"', "”": '"', "„": '"',
                             "‘": "'", "’": "'", "ʼ": "'", "ʻ": "'", "`": "'"})


def normalize(text: str) -> str:
    """For verbatim comparison: whitespace and column separators collapse to one space,
    dash variants map to one character and every kind of quotation mark or apostrophe to another
    (models often change the quote style when copying). Letters and digits are untouched."""
    return _SPACES.sub(" ", text.translate(_TYPOGRAPHY).replace('"', "'")).strip()


def normalize_joined(text: str) -> str:
    """`normalize`, plus whitespace around hyphens removed: a word broken at a line end
    ("125-\\nuy") then equals the same word written in one piece ("125-uy")."""
    return re.sub(r"\s*-\s*", "-", normalize(text))


_NUMBER = re.compile(r"^([+\-±]?)(\d+)(?:[.,](\d+))?$")


def parse_number(text: str | None) -> Decimal | None:
    """Decimal only when the whole text is one number ("36 036,5", "+52.420", "-3.200").
    Texts like ">=10274.2" or "36036 м3" are not numbers -> None."""
    if text is None:
        return None
    t = text.translate(_TYPOGRAPHY).strip()
    t = re.sub(r"(?<=\d)[\s    ](?=\d{3}(?:\D|$))", "", t)
    m = _NUMBER.match(t)
    if not m:
        return None
    sign, whole, frac = m.groups()
    v = Decimal(whole + ("." + frac if frac else ""))
    return -v if sign == "-" else v


def numbers_in_text(text: str) -> list[tuple[Decimal, bool]]:
    """All number candidates in the text: (value, is there a minus right before it)."""
    t = text.translate(_TYPOGRAPHY)
    t = re.sub(r"[    ]", " ", t)
    out: list[tuple[Decimal, bool]] = []
    for pattern in (r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", r"\d+(?:[.,]\d+)?"):
        for m in re.finditer(pattern, t):
            if m.start() > 0 and (t[m.start() - 1].isdigit() or t[m.start() - 1] in ".,"
                                   and m.start() > 1 and t[m.start() - 2].isdigit()):
                continue  # a fragment starting in the middle of a number
            if m.end() < len(t) and t[m.end()].isdigit():
                continue
            value = Decimal(m.group(0).replace(" ", "").replace(",", "."))
            out.append((value, m.start() > 0 and t[m.start() - 1] == "-"))
    return out


_UNIT_WORDS = {"m", "м", "m2", "м2", "m3", "м3", "mm", "sm", "см", "мм", "ta", "dona", "шт", "metr", "метр",
               "metrni", "kv", "кв", "kub", "куб"}
_WORD = re.compile(r"[^\W\d_]{3,}")


def has_label(source: str) -> bool:
    """Does the snippet contain a word besides numbers and units (a label, or the name in a table row).
    A bare number proves nothing about what the number means, so it is not accepted as a source."""
    return any(w.lower() not in _UNIT_WORDS for w in _WORD.findall(source))


def line_of_number(value: str | None, source: str | None, page_texts: list[str]) -> str | None:
    """When the model gave a bare number as its source (some models copy only the table cell), the line of the
    page that holds that number, with its label: but only when exactly one line of the page holds that number,
    so the line is the number's own context and not a guess between several places. Otherwise None."""
    number = parse_number(str(value)) if value is not None else None
    if number is None or source is None or has_label(normalize(source)) or parse_number(source) != number:
        return None
    found: dict[str, str] = {}
    for text in page_texts:  # the raw text layer and the same page grouped into lines / table rows
        for line in text.splitlines():
            # a line that is the bare cell itself gives no context and is the same place as its row
            if has_label(normalize(line)) and any(v == abs(number) for v, _minus in numbers_in_text(line)):
                found.setdefault(normalize(line), line.strip())
    return next(iter(found.values())) if len(found) == 1 else None


def verify_source(value: str | None, source: str | None, page_texts: list[str],
                  numeric: bool) -> tuple[str, str, Decimal | None]:
    """Verify a value against its source -> (status, reason, number).

    1) the source snippet must occur verbatim in the page text;
    2) a numeric value must be entirely a number and equal one of the numbers in the snippet,
       and the snippet must carry a label (not just numbers);
       a textual value must occur verbatim inside the snippet.
    """
    if value is None or not str(value).strip():
        return NOT_FOUND, "", None
    if source is None or not source.strip():
        return UNVERIFIED, "manba matni berilmagan", None
    src = normalize(source)
    if not any(src in normalize(t) for t in page_texts):
        joined = normalize_joined(source)
        if not any(joined in normalize_joined(t) for t in page_texts):
            return UNVERIFIED, "manba matni sahifa matnida so'zma-so'z topilmadi", None
    if not numeric:
        if normalize(str(value)) not in src and normalize_joined(str(value)) not in normalize_joined(source):
            return UNVERIFIED, "qiymat manba parchasida so'zma-so'z uchramaydi", None
        return ACCEPTED, "", None
    number = parse_number(str(value))
    if number is None:
        return UNVERIFIED, f"qiymat son emas: {value!r}", None
    if not has_label(src):
        return UNVERIFIED, "manba parchasida yorliq yo'q (faqat son) — son nimani bildirishi isbotlanmagan", None
    if not any(v == abs(number) and (number >= 0 or minus) for v, minus in numbers_in_text(src)):
        return UNVERIFIED, f"{value} soni manba parchasidan ajralmadi", None
    return ACCEPTED, "", number


# ---------- units ----------

def normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.lower().replace(" ", "").replace(".", "").replace("кв", "2").replace("куб", "3")
    u = u.replace("²", "2").replace("³", "3").replace("м", "m")
    if u in ("m3", "3m"):
        return "m3"
    if u in ("m2", "2m"):
        return "m2"
    return u


def unit_flag(expected: str, written: str | None) -> str | None:
    """Flag text when the unit written in the document does not fit the field."""
    w = normalize_unit(written)
    if w is None:
        return f"birlik yozilmagan; maydon mazmuni bo'yicha {expected} deb olindi"
    if w != expected:
        return (f"hujjatda birlik «{written}» deb yozilgan; maydon mazmuni bo'yicha {expected} "
                f"(birlik xato yozilgan)")
    return None


# ---------- check results ----------

OK, ROUNDING, MISMATCH, WARNING, NOT_RUN = "ok", "rounding", "mismatch", "warning", "not_run"
CHECK_LABELS = {OK: "to'g'ri", ROUNDING: "yaxlitlash farqi", MISMATCH: "farq", WARNING: "ogohlantirish",
                NOT_RUN: "bajarilmadi"}


@dataclass(frozen=True)
class Check:
    code: str
    name: str
    status: str
    detail: str

    def dict(self) -> dict:
        return asdict(self)


def _places(v: Decimal) -> int:
    return max(0, -int(v.as_tuple().exponent))


def is_rounding_difference(a: Decimal, b: Decimal, addends: int = 1) -> bool:
    """Do a and b differ only because of rounding/truncation.

    Both are rounded (or truncated) to the coarser precision and compared; for a sum each
    addend may have been truncated separately, so the gap may reach addends x unit.
    Equal precision means it is a real difference, not rounding.
    """
    if a == b or _places(a) == _places(b):
        return False
    unit = Decimal(1).scaleb(-min(_places(a), _places(b)))
    for mode in (ROUND_HALF_UP, ROUND_DOWN):
        if a.quantize(unit, rounding=mode) == b.quantize(unit, rounding=mode):
            return True
    return abs(a - b) < unit * max(1, addends)


def check_sum(code: str, name: str, parts: list[Decimal | None], total: Decimal | None) -> Check:
    if total is None or not parts or any(p is None for p in parts):
        return Check(code, name, NOT_RUN, "qiymatlar to'liq emas (topilmadi yoki tekshirilmadi)")
    present = [p for p in parts if p is not None]
    s = sum(present, Decimal(0))
    expr = " + ".join(fmt_num(p) for p in present)
    if s == total:
        return Check(code, name, OK, f"{expr} = {fmt_num(total)}")
    if is_rounding_difference(s, total, len(present)):
        return Check(code, name, ROUNDING,
                     f"{expr} = {fmt_num(s)}; hujjatda {fmt_num(total)} — yaxlitlash farqi {fmt_num(s - total)}")
    return Check(code, name, MISMATCH, f"{expr} = {fmt_num(s)}; hujjatda {fmt_num(total)}; farq {fmt_num(s - total)}")


def check_equal(code: str, name: str, project: Decimal | None, conclusion: Decimal | None) -> Check:
    if project is None or conclusion is None:
        missing = "loyihada" if project is None else "xulosada"
        return Check(code, name, NOT_RUN, f"{missing} qabul qilingan qiymat yo'q")
    if project == conclusion:
        return Check(code, name, OK, f"loyiha {fmt_num(project)} = xulosa {fmt_num(conclusion)}")
    if is_rounding_difference(project, conclusion):
        return Check(code, name, ROUNDING,
                     f"loyiha {fmt_num(project)}, xulosa {fmt_num(conclusion)} — yaxlitlash farqi")
    return Check(code, name, MISMATCH, f"loyiha {fmt_num(project)}, xulosa {fmt_num(conclusion)}; "
                                       f"farq {fmt_num(project - conclusion)}")


def check_volume_lower_bound(volume: Decimal | None, footprint: Decimal | None, heights: list[Decimal]) -> Check:
    code, name = "volume_lower_bound", "Hajm ≥ qurilish maydoni × balandliklar yig'indisi"
    if volume is None or footprint is None or not heights:
        return Check(code, name, NOT_RUN, "hajm, qurilish maydoni yoki qavat balandliklari yo'q")
    total_h = sum(heights, Decimal(0))
    bound = footprint * total_h
    expr = f"{fmt_num(footprint)} × {fmt_num(total_h)} = {fmt_num(bound)}; hajm {fmt_num(volume)}"
    if volume < bound:
        return Check(code, name, WARNING, expr + " — hajm kichik")
    return Check(code, name, OK, expr)


def check_deductions_bound(ha: Decimal | None, ht: Decimal | None, hu: Decimal | None, hb: Decimal | None) -> Check:
    code, name = "deductions_bound", "Ha + Ht + Hu ≤ Hb"
    if hb is None or ha is None or ht is None or hu is None:
        return Check(code, name, NOT_RUN, "hajmlardan biri aniqlanmagan")
    total = ha + ht + hu
    expr = f"{fmt_num(ha)} + {fmt_num(ht)} + {fmt_num(hu)} = {fmt_num(total)}; Hb = {fmt_num(hb)}"
    return Check(code, name, OK if total <= hb else MISMATCH, expr)


# ---------- address ----------

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
    # Latin with diacritics seen in conclusions ("Paxtaçi", "Şaxriston köçasi")
    "ş": "sh", "ç": "ch", "ö": "o", "ğ": "g", "ü": "u", "ı": "i", "ñ": "n",
}
_APOSTROPHES = re.compile(r"['ʼ’`‘ʻ\u02bb\u02bc]")


def transliterate(text: str) -> str:
    """Lower-case Latin form of Uzbek/Russian text: Cyrillic and diacritics mapped, apostrophes dropped."""
    return _APOSTROPHES.sub("", "".join(_TRANSLIT.get(ch, ch) for ch in text.lower()))
_ADDRESS_NOISE = {
    "g", "gor", "gorod", "sh", "shahar", "shahri", "ul", "ulitsa", "kochasi", "kocha", "k", "viloyati",
    "viloyat", "obl", "oblast", "tumani", "tuman", "rayon", "r", "n", "mfy", "mahalla", "massiv", "massivi",
    "dom", "uy", "d", "respublikasi", "ozbekiston", "uzbekistan", "respublika",
}


def address_tokens(address: str) -> set[str]:
    return {x for x in re.split(r"[^a-z0-9]+", transliterate(address))
            if x and x not in _ADDRESS_NOISE and not x.isdigit()}


def same_address(a: str, b: str) -> tuple[bool, int, int]:
    """Do two addresses name the same place -> (same, matching words, words compared)."""
    ta, tb = address_tokens(a), address_tokens(b)
    if not ta or not tb:
        return False, 0, 0
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    hits = sum(1 for x in small if any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.8 for y in large))
    return hits == len(small), hits, len(small)


def check_address(project: list[str], conclusion: str | None) -> Check:
    """Compare every address variant found in the project with the conclusion's address."""
    code, name = "address", "Manzil: loyiha va xulosa"
    if not project or not conclusion:
        return Check(code, name, NOT_RUN, "manzil hujjatlardan birida topilmadi")
    parts: list[str] = []
    worst = OK
    for variant in project:
        same, hits, total = same_address(variant, conclusion)
        if total == 0:
            parts.append(f"«{variant}» — solishtiriladigan so'z ajralmadi")
            worst = MISMATCH if worst == MISMATCH else NOT_RUN
        elif same:
            parts.append(f"«{variant}» — o'xshash ({hits}/{total})")
        else:
            parts.append(f"«{variant}» — FARQ ({hits}/{total})")
            worst = MISMATCH
    detail = f"xulosa: «{conclusion}»; loyiha: " + "; ".join(parts)
    if worst == OK:
        detail += " (xodim ko'zdan kechirsin)"
    return Check(code, name, worst, detail)


def check_equal_scaled(code: str, name: str, project: Decimal | None, unit: Decimal | None,
                       count: Decimal | None) -> Check:
    """Project value against the conclusion's per-unit value for `count` identical buildings.

    The project may state the value for one unit or for all units; both readings are tried.
    A gap within count x half a unit of the last written digit is a rounding difference.
    """
    if project is None or unit is None:
        missing = "loyihada" if project is None else "xulosada"
        return Check(code, name, NOT_RUN, f"{missing} qabul qilingan qiymat yo'q")
    n = count if count is not None and count > 0 else Decimal(1)
    if project == unit:
        note = f" (bir dona uchun; {fmt_num(n)} dona)" if n > 1 else ""
        return Check(code, name, OK, f"loyiha {fmt_num(project)} = xulosa {fmt_num(unit)}{note}")
    if n > 1:
        total = unit * n
        expr = f"loyiha {fmt_num(project)}; xulosa {fmt_num(n)} × {fmt_num(unit)} = {fmt_num(total)}"
        if project == total:
            return Check(code, name, OK, expr)
        tolerance = n * Decimal(5).scaleb(-(_places(unit) + 1))
        if abs(project - total) <= tolerance:
            return Check(code, name, ROUNDING, f"{expr} — yaxlitlash farqi {fmt_num(project - total)}")
        return Check(code, name, MISMATCH, f"{expr}; farq {fmt_num(project - total)}")
    if is_rounding_difference(project, unit):
        return Check(code, name, ROUNDING, f"loyiha {fmt_num(project)}, xulosa {fmt_num(unit)} — yaxlitlash farqi")
    return Check(code, name, MISMATCH, f"loyiha {fmt_num(project)}, xulosa {fmt_num(unit)}; "
                                       f"farq {fmt_num(project - unit)}")


def plan_dimensions(plan_size: str | None) -> list[Decimal]:
    """The numbers of a plan size such as "26.97 × 26.89" or "1,6x15,5 metr"."""
    if not plan_size:
        return []
    t = re.sub(r"(?<=\d)\s*[xх×*]\s*(?=\d)", " ", plan_size.lower())
    return [v for v, _minus in numbers_in_text(t)]


def check_plan_vs_footprint(code: str, name: str, plan_size: str | None, footprint: Decimal | None) -> Check:
    """The bounding rectangle of the plan cannot be much smaller than the built-up area."""
    dims = plan_dimensions(plan_size)
    if footprint is None or len(dims) != 2:
        return Check(code, name, NOT_RUN, "tarx o'lchami (ikki son) yoki qurilish maydoni yo'q")
    area = dims[0] * dims[1]
    expr = f"{fmt_num(dims[0])} × {fmt_num(dims[1])} = {fmt_num(area)} m²; qurilish maydoni {fmt_num(footprint)} m²"
    # Plan sizes are usually given between axes, so the product may be a little below the built-up
    # area (wall thickness, porches). Only a clear gap is reported.
    if area < footprint * Decimal("0.8"):
        return Check(code, name, WARNING, expr + " — tarx o'lchami qurilish maydoniga mos emas")
    return Check(code, name, OK, expr)


# ---------- agreement of the two models ----------

def model_agreement(v1: dict | None, v2: dict | None) -> str:
    """'match' | 'mismatch' | 'only_1' | 'only_2' | 'neither'."""
    has1 = bool(v1) and v1.get("status") == ACCEPTED
    has2 = bool(v2) and v2.get("status") == ACCEPTED
    if not has1 and not has2:
        return "neither"
    if has1 and not has2:
        return "only_1"
    if has2 and not has1:
        return "only_2"
    assert v1 is not None and v2 is not None
    if v1.get("number") is not None and v2.get("number") is not None:
        return "match" if Decimal(v1["number"]) == Decimal(v2["number"]) else "mismatch"
    same = normalize(str(v1.get("value"))).lower() == normalize(str(v2.get("value"))).lower()
    return "match" if same else "mismatch"
