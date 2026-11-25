"""Automatic recommendation: everything a person would otherwise decide by hand, with its basis.

Pure code, no model. From the assessment (pipeline.assess), the object description read from the
conclusion and the reference data kept by staff, it proposes the four coefficients and a value for
each of the five volumes, so that a person only has to approve.

Nothing here is silent: every decision names its basis; whatever is an assumption rather than a fact
read from a document is listed as an assumption; whatever cannot be decided is listed as open and
blocks approval until a person fills it in. Money is still computed only in fee.py.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from decimal import Decimal

from . import fee, verify, volumes
from .fee import fmt_num

LEGAL = "VM 151-son qarori (08.04.2026), 4-ilova"
VOLUME_CODES = ("Hb", "Hyu", "Ha", "Ht", "Hu")


@dataclass(frozen=True)
class Reference:
    """Reference data entered by staff; the web app keeps it in SQLite (see web/store.py)."""
    bhm: Decimal = fee.BHM_DEFAULT
    bhm_basis: str = "PF-115 (23.06.2026), 3-band: 2026-yil 1-sentabrdan"
    k_council: Decimal = Decimal("1")
    k_council_basis: str = "Kengash qarori ma'lumotnomaga kiritilmagan"
    zones: dict[str, tuple[str, str]] = field(default_factory=dict)                    # settlement -> (Kz code, basis)
    central_streets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)    # settlement -> [(street, basis)]


@dataclass(frozen=True)
class Place:
    settlement: str | None    # canonical key, e.g. "samarqand shahri", "payariq tumani"
    kind: str | None          # "city" | "district"
    street: str | None        # street name without the word for street
    in_region: bool           # the text names Samarqand region


# ---------- where is the object ----------
_CITY = re.compile(r"\b([a-z]+)\s+(?:shahar|shahri|shaxar|shaxri)\b|\b(?:g\.|gor\.|gorod)\s*([a-z]+)")
_DISTRICT = re.compile(r"\b([a-z]+)\s+(?:tuman|tumani|rayon|rayoni)\b")
_STREET = re.compile(r"([a-z0-9 .\-]+?)\s+(?:shoh\s+)?k[ou]cha(?:si)?\b")
_STREET_RU = re.compile(r"\b(?:ul\.|ul|ulitsa)\s*([a-z0-9 \-]+?)(?=\s*(?:,|$|\bv\b|\bdom\b|\d))")
_BEFORE_STREET = {"shahar", "shahri", "tuman", "tumani", "mfy", "qfy", "mahallasi", "mahalla", "massivi", "massiv",
                  "qishlogi", "qishloq", "shaharchasi", "viloyat", "viloyati", "hududida", "joylashgan"}
_STREET_WORDS = {"kochasi", "kocha", "kuchasi", "kucha", "ulitsa", "ul", "shoh", "prospekti", "prospekt"}
_SPELLING = {"samarkand": "samarqand", "kattakurgan": "kattaqorgon"}


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9,.\- ]+", " ", verify.transliterate(text))).strip()


def street_key(text: str) -> str:
    """A street name reduced to what identifies it: lower-case Latin, without the word for street."""
    words = [w for w in re.split(r"[^a-z0-9]+", verify.transliterate(text)) if w and w not in _STREET_WORDS]
    return " ".join(words)


def locate(text: str) -> Place:
    """Settlement and street named in an address or an object description."""
    t = _plain(text)
    settlement = kind = street = None
    city, district = _CITY.search(t), _DISTRICT.search(t)
    if city:
        name = city.group(1) or city.group(2)
        settlement, kind = f"{_SPELLING.get(name, name)} shahri", "city"
    elif district:
        settlement, kind = f"{district.group(1)} tumani", "district"
    m = _STREET.search(t)
    if m:
        words = m.group(1).replace(",", " ").replace(".", " ").split()
        cut = max((i for i, w in enumerate(words) if w in _BEFORE_STREET), default=-1)
        street = street_key(" ".join(words[cut + 1:])) or None
    else:
        m = _STREET_RU.search(t)
        street = (street_key(m.group(1)) or None) if m else None
    return Place(settlement, kind, street, "samarqand viloyat" in t or "samarkandskaya obl" in t)


def same_street(a: str, b: str) -> bool:
    ka, kb = street_key(a), street_key(b)
    return bool(ka) and bool(kb) and (ka == kb or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.88)


# ---------- coefficients ----------
def _decision(code: str | None, basis: str, evidence: dict | None = None, assumed: bool = False) -> dict:
    e = evidence or {}
    return {"code": code, "basis": basis, "assumed": assumed, "file": e.get("file"), "page": e.get("page"),
            "snippet": e.get("source_text") or ""}


_KT = (("reconstruction", re.compile(r"rekonstruk")), ("renovation", re.compile(r"renovats")),
       ("new", re.compile(r"yangi\s+qurilish|yangidan\s+qur|novoe\s+stroitel")))


def decide_kt(construction: dict | None) -> dict:
    """Construction type as the conclusion states it."""
    if not construction or construction.get("status") != verify.ACCEPTED or not construction.get("value"):
        return _decision(None, "Xulosada qurilish turi topilmadi yoki tasdiqlanmadi")
    text = verify.transliterate(str(construction["value"]))
    for code, pattern in _KT:
        if pattern.search(text):
            return _decision(code, f"Xulosada qurilish turi: «{construction['value']}»", construction)
    return _decision(None, f"Xulosadagi qurilish turi «{construction['value']}» qaror jadvalidagi turlarga to'g'ri kelmadi",
                     construction)


_KO = (
    ("industrial", re.compile(r"sanoat|zavod|fabrika|ishlab\s+chiqarish|\btsex|\bsex\b|omborxona|\bombor|sklad"
                              r"|proizvodstv|promyshlen")),
    ("social", re.compile(r"oquv\s+markaz|talim|maktab|bogcha|litsey|kollej|universitet|institut|tibbiy|tibbiyot"
                          r"|klinika|shifoxona|poliklinika|diagnostik|stomatolog|meditsin|uchebn|shkol|bolnits")),
)
_KO_NAMES = {"industrial": "sanoat binosi", "social": "ijtimoiy xususiy obyekt"}


def decide_ko(description: dict | None) -> dict:
    """Object type from the words of the object description. A state share of 50% cannot be read from documents."""
    if not description or not description.get("value"):
        return _decision(None, "Xulosadan obyekt nomi o'qilmadi — obyekt turini tanlang")
    text = _plain(str(description["value"]))
    hits = [(m.start(), code, m.group(0)) for code, pattern in _KO for m in [pattern.search(text)] if m]
    note = " Davlat ulushi 50 foiz va undan ortiq bo'lgan loyiha bo'lsa, qo'lda o'zgartiring."
    if not hits:
        return _decision("other", "Obyekt nomida ijtimoiy yoki sanoat obyektiga xos so'z topilmadi — «Boshqa obyektlar»." + note,
                         description)
    _, code, word = min(hits)
    mixed = len({h[1] for h in hits}) > 1
    basis = f"Obyekt nomida «{word}» so'zi bor — {_KO_NAMES[code]}." + note
    if mixed:
        basis += " Nomda boshqa turga xos so'z ham bor: aralash obyekt bo'lishi mumkin."
    return _decision(code, basis, description, assumed=mixed)


def decide_kz(place: Place, reference: Reference) -> dict:
    """Territorial zone by the population bracket of the settlement."""
    if place.settlement is None:
        return _decision(None, "Manzildan aholi punkti aniqlanmadi — hududiy zonani tanlang")
    if place.settlement in reference.zones:
        code, basis = reference.zones[place.settlement]
        return _decision(code, f"Ma'lumotnoma: {place.settlement} — {basis or 'asos yozilmagan'}")
    if place.settlement == "samarqand shahri":
        return _decision("500k-1m", "Samarqand shahri — aholisi 500 ming – 1 mln bo'lgan shahar")
    if place.in_region:
        return _decision("under100k", f"{place.settlement} — Samarqand viloyatida Samarqand shahridan boshqa aholi "
                                      "punktlari 100 minggacha toifasida (boshqacha bo'lsa, ma'lumotnomaga kiriting)")
    return _decision(None, f"{place.settlement} ma'lumotnomada yo'q — hududiy zonani tanlang yoki ma'lumotnomaga kiriting")


def decide_kj(place: Place, reference: Reference) -> dict:
    """Location: on a central thoroughfare street (1) or elsewhere (0.6), by the list staff keeps."""
    if place.settlement is None:
        return _decision(None, "Manzildan aholi punkti aniqlanmadi — joylashuvni tanlang")
    streets = reference.central_streets.get(place.settlement, [])
    if not streets:
        return _decision(None, f"«{place.settlement}» uchun markaziy qatnov ko'chalari ro'yxati ma'lumotnomaga kiritilmagan "
                               "— joylashuvni tanlang yoki ro'yxatni kiriting")
    if place.street is None:
        return _decision("other", "Manzilda ko'cha nomi yo'q — markaziy qatnov ko'chasida emas deb olindi", assumed=True)
    for street, basis in streets:
        if same_street(place.street, street):
            return _decision("central", f"Manzildagi ko'cha «{place.street}» markaziy qatnov ko'chalari ro'yxatida"
                                        + (f" ({basis})" if basis else ""))
    return _decision("other", f"Manzildagi ko'cha «{place.street}» markaziy qatnov ko'chalari ro'yxatida yo'q "
                              f"({place.settlement}: {len(streets)} ta ko'cha kiritilgan)")


# ---------- exemptions: flagged, never applied ----------
_EXEMPT = (
    (re.compile(r"yakka\s+tartibdagi"), "yakka tartibdagi uy-joy (4-ilova, 1-band: yakka tartibdagi uy-joylar mustasno)"),
    (re.compile(r"masjid|cherkov|ibodatxona|diniy"), "diniy bino"),
    (re.compile(r"mahalla\s+(?:fuqarolar\s+yigini\s+)?(?:binosi|markazi|idorasi)"),
     "fuqarolarning o'zini o'zi boshqarish organi (mahalla) binosi"),
    (re.compile(r"maktabgacha|bogcha|(?:xususiy|hususiy|nodavlat)\s+maktab|umumtalim"),
     "nodavlat maktabgacha yoki umumiy o'rta ta'lim tashkiloti"),
    (re.compile(r"byudjet"), "byudjet mablag'lari hisobidan moliyalashtiriladigan obyekt"),
)


def exemptions(description: dict | None) -> list[dict]:
    """Words in the object description that may mean the fee is not due. The decision stays with a person."""
    if not description or not description.get("value"):
        return []
    text = _plain(str(description["value"]))
    out: list[dict] = []
    for pattern, what in _EXEMPT:
        m = pattern.search(text)
        if m:
            out.append({"text": f"Obyekt nomida «{m.group(0)}» so'zi bor: {what} bo'lishi mumkin — bunday obyektdan yig'im "
                                "undirilmasligi yoki qisman undirilishi mumkin. Qarorni siz qabul qilasiz.",
                        "file": description.get("file"), "page": description.get("page"),
                        "snippet": description.get("source_text") or ""})
    return out


# ---------- volumes ----------
_RESIDENTIAL = re.compile(r"turar|uy-joy|uy joy|xonadon|kvartir|zhil|kottej|yotoqxona|obshchezhit")


def _number(v: dict | None) -> Decimal | None:
    if v and v.get("status") == verify.ACCEPTED and v.get("number") is not None:
        return Decimal(v["number"])
    return None


def _volume(value: Decimal | None, formula: str, assumptions: list[str], strict: bool) -> dict:
    # a strict proposal keeps its spelling (confirmations are compared with it); a derived one drops trailing zeros
    text = None if value is None else (str(value) if strict else fmt_num(value))
    return {"value": text, "formula": formula, "assumptions": assumptions, "strict": strict}


def decide_hb(view: dict, industrial: bool) -> dict:
    """Total building volume. Where the strict proposal abstains, decide by stated rules:
    the project is the primary source, so where the conclusion disagrees the project value is taken
    and the disagreement is reported as a finding about the conclusion;
    an industrial object is charged for at most 3 m of height per storey."""
    strict = view["volumes"]["Hb"]["proposal"]
    rows = [r for r in view["hb_rows"] if r["included"]]
    if not view["hb_rows"]:
        return _volume(None, "", ["Hujjatlardan bino o'qilmadi — Hb ni kiriting"], False)
    if strict is not None and not industrial:
        return _volume(Decimal(strict), view["volumes"]["Hb"]["formula"], [], True)
    assumptions: list[str] = []
    parts: list[Decimal] = []
    for r in rows:
        count = Decimal(r["count"])
        volume = Decimal(r["volume"]) if r["volume"] is not None else None
        p, c = _number(r["project"]), _number(r["conclusion"])
        if volume is None and p is not None and c is not None:
            # the project is the primary source; the conclusion is what gets checked against it
            volume = p
            assumptions.append(f"{r['name']}: loyihada {fmt_num(p)} m³, xulosada {fmt_num(c)} m³ — loyihadagi qiymat olindi; "
                               "ekspertiza xulosasi bu yerda loyihaga mos emas")
        if industrial:
            footprint, floors = _number(r["footprint"]), _number(r["floors"]) or Decimal(1)
            if footprint is not None:
                # a row that stands for several identical buildings carries the footprint of one of them
                limit = footprint * fee.INDUSTRIAL_HEIGHT_LIMIT * floors * (count if (r["computed"] or volume is None) else Decimal(1))
                if volume is None or limit < volume:
                    assumptions.append(
                        f"{r['name']}: sanoat binosi — har qavatning 3 m gacha qismi olinadi: {fmt_num(footprint)} m² × 3 m × "
                        f"{fmt_num(floors)} qavat = {fmt_num(limit)} m³"
                        + (f" (to'liq hajm {fmt_num(volume)} m³)" if volume is not None else " (to'liq hajm yozilmagan)"))
                    volume = limit
            elif volume is not None:
                assumptions.append(f"{r['name']}: qurilish maydoni yo'q — 3 m qoidasini qo'llab bo'lmadi, to'liq hajm olindi")
        if volume is None:
            why = (f"faqat havola: «{r['reference']['value']}»" if (r.get("reference") or {}).get("status") == verify.ACCEPTED
                   else "hajm hujjatlarda topilmadi")
            assumptions.append(f"DIQQAT — {r['name']}: {why}; bu bino hisobga kiritilmadi. Hajmi ma'lum bo'lsa, "
                               "tekshirish sahifasida kiriting")
            continue
        parts.append(volume)
    if not parts:
        return _volume(None, "", assumptions + ["Birorta bino hajmi aniqlanmadi — Hb ni kiriting"], False)
    total = sum(parts, Decimal(0))
    return _volume(total, " + ".join(fmt_num(x) for x in parts) + f" = {fmt_num(total)} m³", assumptions, False)


def decide_hyu(view: dict, permitted_floors: int | None) -> dict:
    strict = view["volumes"]["Hyu"]
    if strict["proposal"] is not None:
        return _volume(Decimal(strict["proposal"]), strict["formula"], [], True)
    if permitted_floors is None:
        return _volume(Decimal(0), "0", ["Ruxsat etilgan qavatlar soni hujjatlarda ko'rsatilmagan — ortiqcha qavat yo'q deb "
                                         "olindi (Hyu = 0). ART'dagi son ma'lum bo'lsa, tekshirish sahifasida kiriting"], False)
    return _volume(None, "", list(strict["notes"]), False)


def typical_height(heights: list[dict]) -> tuple[Decimal, str] | None:
    """The storey height that holds for most storeys ("2-16-qavat: 3.25"), if one clearly does."""
    covered: dict[Decimal, tuple[int, str]] = {}
    for h in heights:
        floors = len(volumes.parse_level(h["level"]).floors())
        value = Decimal(h["value"])
        if floors and floors > covered.get(value, (0, ""))[0]:
            covered[value] = (floors, str(h["level"]))
    total = sum(n for n, _ in covered.values())
    if total < 2:
        return None
    value, (floors, label) = max(covered.items(), key=lambda item: item[1][0])
    return (value, label) if floors * 2 > total else None


def storey_heights(view: dict) -> tuple[Decimal, Decimal, str] | None:
    """(basement height, storey height, basis) consistent with the volumes the project states for one block:
    below-ground volume / built-up area, and above-ground volume / built-up area / storeys."""
    candidates = []
    for b in view.get("buildings") or []:
        f = {x["field"]: _number(x["project"]) for x in b["fields"]}
        if all(f.get(k) for k in ("volume", "volume_above_ground", "volume_below_ground", "footprint_area", "floors")):
            candidates.append((f["volume"], f, str(b["title"])))
    if not candidates:
        return None
    _, f, title = min(candidates, key=lambda c: c[0])             # the smallest: one block rather than their sum
    basement = (f["volume_below_ground"] / f["footprint_area"]).quantize(Decimal("0.01"))
    storey = (f["volume_above_ground"] / f["footprint_area"] / f["floors"]).quantize(Decimal("0.01"))
    return basement, storey, (f"balandliklar loyihadagi hajmlardan ({title[:60]}): yer osti {fmt_num(f['volume_below_ground'])} ÷ "
                              f"{fmt_num(f['footprint_area'])} = {fmt_num(basement)} m; yer usti {fmt_num(f['volume_above_ground'])} ÷ "
                              f"{fmt_num(f['footprint_area'])} ÷ {fmt_num(f['floors'])} qavat = {fmt_num(storey)} m")


def plan_volume(view: dict, code: str) -> dict | None:
    """Ha / Ht / Hu from the storey plans: every room of the category on a plan sheet, times the storeys
    that sheet shows, times the storey height. Sheets that repeat the same storeys (floor finishes) are
    not plan sheets here, so nothing is counted twice. None when the plans cannot carry the calculation."""
    index = {(s["file"], s["page"]): s for s in view.get("plan_index") or []}
    heights = storey_heights(view)
    rooms = [c for c in view["components"] if c["origin"] == "room" and (c.get("file"), c.get("page")) in index
             and c["area"] is not None]
    if not index or not heights or not rooms:
        return None
    basement, storey, basis = heights
    blocks: dict[str, set[int]] = {}
    for s in index.values():
        blocks.setdefault(s["block"], set()).update(s["floors"])
    if any(not floors or floors != set(range(1, max(floors) + 1)) for floors in blocks.values()):
        return None                                                # a storey without its plan: do not extrapolate
    parts: list[tuple[Decimal, str]] = []
    for c in rooms:
        if c["category"] != code or not str(c["category_method"]).startswith(("dictionary", "reference", "staff")):
            continue
        sheet = index[(c["file"], c["page"])]
        count, height = (1, basement) if sheet["level"] == "basement" else (len(sheet["floors"]), storey)
        volume = Decimal(c["area"]) * count * height
        where = "yerto'la" if sheet["level"] == "basement" else (f"{sheet['floors'][0]}–{sheet['floors'][-1]}-qavatlar" if count > 1 else f"{sheet['floors'][0]}-qavat")
        parts.append((volume, f"{sheet['block'] + '-blok, ' if sheet['block'] else ''}{where}: {c['name']} {fmt_num(Decimal(c['area']))} m²"
                              + (f" × {count} qavat" if count > 1 else "") + f" × {fmt_num(height)} m = {fmt_num(volume)}"))
    total = sum((v for v, _ in parts), Decimal(0))
    suggested = [c for c in rooms if c["category"] == code and str(c["category_method"]) == "model"]
    assumptions = [f"Qavat rejalari bo'yicha hisoblandi ({len(index)} ta reja varag'i, {len(blocks)} ta blok); {basis}"]
    assumptions += [text for _, text in parts]
    if suggested:
        names = sorted({str(c["name"]) for c in suggested})
        assumptions.append(f"{len(suggested)} ta xonaning toifasini model taxmin qilgan, lug'atda yo'q — qo'shilmadi: " + "; ".join(names[:4]))
    return _volume(total, (" + ".join(fmt_num(v) for v, _ in parts) + f" = {fmt_num(total)} m³") if parts else "0", assumptions, False)


def decide_category(view: dict, code: str, residential: bool) -> dict:
    """Ha / Ht / Hu. Only rows whose category comes from the dictionary are counted; a category that the
    model merely suggested stays out until a person confirms it. Order of trust:
    1. the project's own indicator table, when it has a row of this category;
    2. the storey plans (rooms x storeys x storey height), when they cover every storey;
    3. the room tables as read, with whatever cannot be computed named and left out."""
    strict = view["volumes"][code]
    if code == "Hu" and not residential:
        return _volume(Decimal(0), "0", ["Hu faqat turar joy binosining umumiy foydalanishdagi qismiga tegishli "
                                         "(4-ilova, hisoblash mexanizmi); obyektda turar joy binosi aniqlanmadi — 0"], False)
    all_rows = [c for c in view["components"] if c["category"] == code and c["included"]]
    own = [c for c in all_rows if str(c["category_method"]) != "model"]
    suggested = [c for c in all_rows if str(c["category_method"]) == "model"]
    from_table = [c for c in own if c["origin"] == "breakdown"]
    if not from_table:
        by_plans = plan_volume(view, code)
        if by_plans is not None:
            return by_plans
    if strict["proposal"] is not None and not suggested:
        return _volume(Decimal(strict["proposal"]), strict["formula"], [], True)
    parts = [Decimal(c["volume"]) for c in own if c["volume"] is not None]
    missing = [c for c in own if c["volume"] is None]
    assumptions: list[str] = []
    typical = typical_height(view["heights"]) if code == "Hu" else None
    for c in list(missing):
        whole_building = (c["level_kind"] == "unknown" and c["origin"] == "breakdown" and c["area"] is not None
                          and str(c["category_method"]).startswith("dictionary"))
        if typical and whole_building:
            height, label = typical
            volume = Decimal(c["area"]) * height
            parts.append(volume)
            missing.remove(c)
            assumptions.append(f"{c['name']}: maydon butun bino bo'yicha berilgan (sath ko'rsatilmagan) — ko'pchilik qavatlarga "
                               f"tegishli balandlik olindi: {fmt_num(Decimal(c['area']))} m² × {fmt_num(height)} m "
                               f"(«{label}») = {fmt_num(volume)} m³")
    if missing:
        names = "; ".join(str(c["name"]) for c in missing[:4]) + (" va boshqalar" if len(missing) > 4 else "")
        assumptions.append(f"{len(missing)} ta qator hajmi aniqlanmadi (balandlik yoki maydon yo'q) va qo'shilmadi: {names}. "
                           "Chegirma kamroq olindi — yig'im kamaymaydi")
    if suggested:
        names = sorted({str(c["name"]) for c in suggested})
        assumptions.append(f"{len(suggested)} ta qatorning toifasini model taxmin qilgan, lug'atda yo'q — chegirmaga qo'shilmadi: "
                           + "; ".join(names[:4]) + ". To'g'ri bo'lsa, tekshirish sahifasida toifasini tasdiqlang")
    total = sum(parts, Decimal(0))
    formula = (" + ".join(fmt_num(x) for x in parts) + f" = {fmt_num(total)} m³") if parts else "0"
    return _volume(total, formula, assumptions, False)


# ---------- the whole recommendation ----------
def _scalar(view: dict, label: str, side: str) -> dict | None:
    for r in view["scalar_rows"]:
        if r["label"] == label:
            return r[side]
    return None


def _ko_from_buildings(view: dict) -> dict:
    """Without an object description, the largest building's name stands in for it (said as an assumption)."""
    rows = [r for r in view["hb_rows"] if r["volume"] is not None]
    if not rows:
        return decide_ko(None)
    largest = max(rows, key=lambda r: Decimal(r["volume"]))
    source = largest["conclusion"] or largest["project"] or {}
    d = decide_ko({"value": largest["name"], "file": source.get("file"), "page": source.get("page"),
                   "source_text": source.get("source_text")})
    return {**d, "assumed": True,
            "basis": f"Xulosadan obyekt nomi o'qilmadi — eng katta bino nomi («{largest['name']}») bo'yicha: " + d["basis"]}


def recommend(view: dict, description: dict | None, reference: Reference, permitted_floors: int | None,
              chosen_ko: str | None = None) -> dict:
    """Everything the system proposes for one job. `chosen_ko` is the object type already picked by
    staff, if any: an industrial object changes how Hb is counted."""
    address = _scalar(view, "Manzil", "conclusion") or _scalar(view, "Manzil", "project")
    where = str((description or {}).get("value") or "") or str((address or {}).get("value") or "")
    place = locate(where)
    coefficients = {
        "kt": decide_kt(_scalar(view, "Qurilish turi", "conclusion")),
        "ko": decide_ko(description) if description else _ko_from_buildings(view),
        "kz": decide_kz(place, reference),
        "kj": decide_kj(place, reference),
    }
    ko = chosen_ko or coefficients["ko"]["code"]
    names = " ".join([str((description or {}).get("value") or "")] + [str(b["title"]) for b in view["buildings"]])
    residential = bool(_RESIDENTIAL.search(_plain(names)))
    volumes = {"Hb": decide_hb(view, ko == "industrial"), "Hyu": decide_hyu(view, permitted_floors),
               "Ha": decide_category(view, "Ha", residential), "Ht": decide_category(view, "Ht", residential),
               "Hu": decide_category(view, "Hu", residential)}
    assumptions = [{"scope": code, "text": text} for code in VOLUME_CODES for text in volumes[code]["assumptions"]
                   if volumes[code]["value"] is not None]
    assumptions += [{"scope": group.capitalize(), "text": d["basis"]} for group, d in coefficients.items() if d["assumed"]]
    open_items = [{"scope": code, "text": "; ".join(volumes[code]["assumptions"]) or "taklif yo'q"}
                  for code in VOLUME_CODES if volumes[code]["value"] is None]
    open_items += [{"scope": group.capitalize(), "text": d["basis"]} for group, d in coefficients.items() if d["code"] is None]
    return {
        "place": {"settlement": place.settlement, "street": place.street},
        "description": description, "coefficients": coefficients,
        "k_council": {"value": str(reference.k_council), "basis": reference.k_council_basis},
        "bhm": {"value": str(reference.bhm), "basis": reference.bhm_basis},
        "volumes": volumes, "exemptions": exemptions(description),
        "assumptions": assumptions, "open": open_items, "legal": LEGAL,
    }
