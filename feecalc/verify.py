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
    dash and quote variants map to one character. Letters and digits are untouched."""
    return _SPACES.sub(" ", text.translate(_TYPOGRAPHY)).strip()


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


def verify_source(value: str | None, source: str | None, page_texts: list[str],
                  numeric: bool) -> tuple[str, str, Decimal | None]:
    """Verify a value against its source -> (status, reason, number).

    1) the source snippet must occur verbatim in the page text;
    2) a numeric value must be entirely a number and equal one of the numbers in the snippet;
       a textual value must occur verbatim inside the snippet.
    """
    if value is None or not str(value).strip():
        return NOT_FOUND, "", None
    if source is None or not source.strip():
        return UNVERIFIED, "manba matni berilmagan", None
    src = normalize(source)
    if not any(src in normalize(t) for t in page_texts):
        return UNVERIFIED, "manba matni sahifa matnida so'zma-so'z topilmadi", None
    if not numeric:
        if normalize(str(value)) not in src:
            return UNVERIFIED, "qiymat manba parchasida so'zma-so'z uchramaydi", None
        return ACCEPTED, "", None
    number = parse_number(str(value))
    if number is None:
        return UNVERIFIED, f"qiymat son emas: {value!r}", None
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
}
_ADDRESS_NOISE = {
    "g", "gor", "gorod", "sh", "shahar", "shahri", "ul", "ulitsa", "kochasi", "kocha", "k", "viloyati",
    "viloyat", "obl", "oblast", "tumani", "tuman", "rayon", "r", "n", "mfy", "mahalla", "massiv", "massivi",
    "dom", "uy", "d", "respublikasi", "ozbekiston", "uzbekistan", "respublika",
}


def address_tokens(address: str) -> set[str]:
    t = "".join(_TRANSLIT.get(ch, ch) for ch in address.lower())
    t = re.sub(r"['ʼ’`‘ʻ]", "", t)
    return {x for x in re.split(r"[^a-z0-9]+", t) if x and x not in _ADDRESS_NOISE and not x.isdigit()}


def check_address(project: str | None, conclusion: str | None) -> Check:
    code, name = "address", "Manzil: loyiha va xulosa"
    if not project or not conclusion:
        return Check(code, name, NOT_RUN, "manzil hujjatlardan birida topilmadi")
    a, b = address_tokens(project), address_tokens(conclusion)
    if not a or not b:
        return Check(code, name, NOT_RUN, "manzildan solishtiriladigan so'z ajralmadi")
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    hits = sum(1 for x in small if any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.8 for y in large))
    detail = f"loyiha: «{project}»; xulosa: «{conclusion}»; mos so'zlar {hits}/{len(small)}"
    if hits == len(small):
        return Check(code, name, OK, detail + " (o'xshash; xodim ko'zdan kechirsin)")
    return Check(code, name, MISMATCH, detail)


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
