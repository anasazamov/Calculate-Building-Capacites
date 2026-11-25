"""Checks of the project's figures against the building codes (ShNQ / QMQ).

Pure code. Every rule cites one clause of the local knowledge base (knowledge.py, filled from
lex.uz) and shows it verbatim. A rule's threshold is written here, but it is only applied if the
same number is still found in the clause text: when a code is amended, the rule turns itself off
("not run") instead of checking against an outdated figure.

These are the few requirements that can be verified from the figures the system reads today
(storey heights, storeys, plot areas). They are advice for the reviewer, not an expertise conclusion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from . import decide, verify, volumes
from .fee import fmt_num
from .knowledge import KnowledgeBase

LABELS = {"ok": "mos", "warning": "e'tibor bering", "mismatch": "mos emas", "not_run": "tekshirib bo'lmadi"}
SLAB = Decimal("0.3")  # a storey height includes the floor slab; a ceiling height does not


@dataclass(frozen=True)
class Norm:
    doc_id: str
    ref: str
    figure: str   # the number as the clause writes it; the rule runs only while the clause still contains it


RESIDENTIAL_HEIGHT = Norm("shnq-2.08.01-24", "38-band", "2,7")
PUBLIC_HEIGHT = Norm("shnq-2.08.02-24", "12-band", "3,0")
INDUSTRIAL_HEIGHT = Norm("shnq-2.09.02-23", "49-band", "3 m")
GREENERY = Norm("shnq-2.07.01-23", "86-band", "30 foiz")
VOLUME_RESIDENTIAL = Norm("shnq-2.08.01-24", "1-ilova, 9-band", "±0,00")
VOLUME_PUBLIC = Norm("shnq-2.08.02-24", "1-ilova, 6-band", "±0,00")
STOREYS_RESIDENTIAL = Norm("shnq-2.08.01-24", "1-ilova, 11-band", "texnik qavat")
_INDUSTRIAL = re.compile(r"sanoat|zavod|fabrika|ishlab\s+chiqarish|\btsex|\bsex\b|omborxona|\bombor|sklad|ayvon")
_NOT_RESIDENTIAL = re.compile(r"noturar|nezhil|ne zhil")
# small service structures: room-height requirements of the codes are not meant for them
_AUXILIARY = re.compile(r"hojatxona|xojatxona|handak|havza|\bura\b|punkt|nasos|transformator|qozonxona|tualet|rezervuar")
_CEILING = re.compile(r"shiftgacha|shipgacha|potolk|ferma ostigacha")


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", verify.transliterate(text))


def building_kind(title: str, description: str) -> str:
    """residential | industrial | public - which code speaks about this building."""
    own = _plain(title)
    if _NOT_RESIDENTIAL.search(own):
        return "industrial" if _INDUSTRIAL.search(own) else "public"
    if decide._RESIDENTIAL.search(own):
        return "residential"
    if _INDUSTRIAL.search(own):
        return "industrial"
    whole = _plain(description)
    if _INDUSTRIAL.search(whole):
        return "industrial"
    return "residential" if decide._RESIDENTIAL.search(whole) and not own else "public"


def _number(v: dict | None) -> Decimal | None:
    if v and v.get("status") == verify.ACCEPTED and v.get("number") is not None:
        return Decimal(v["number"])
    return None


def _field(building: dict, name: str, side: str = "conclusion") -> dict | None:
    for f in building["fields"]:
        if f["field"] == name:
            return f[side] or f["project" if side == "conclusion" else "conclusion"]
    return None


class Checker:
    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        self.documents = {d.doc_id: d for d in kb.documents()}
        self.findings: list[dict] = []

    def cite(self, norm: Norm) -> tuple[dict, bool]:
        """The clause as the knowledge base holds it, and whether the rule may run against it."""
        document = self.documents.get(norm.doc_id)
        clause = self.kb.get(norm.doc_id, norm.ref) if document else None
        citation = {"norm": f"{document.designation or document.title}, {norm.ref}" if document else f"{norm.doc_id}, {norm.ref}",
                    "quote": clause.text if clause else "", "url": document.url if document else ""}
        return citation, bool(clause) and norm.figure in clause.text

    def add(self, code: str, title: str, norm: Norm, status: str, detail: str, source: dict | None = None) -> None:
        citation, usable = self.cite(norm)
        if not usable:
            status = "not_run"
            detail = ("me'yor matni bilimlar bazasida topilmadi" if not citation["quote"]
                      else f"me'yor matnida «{norm.figure}» topilmadi — band o'zgargan bo'lishi mumkin, qoida qo'llanmadi")
        s = source or {}
        self.findings.append({"code": code, "title": title, "status": status, "label": LABELS[status], "detail": detail,
                              **citation, "file": s.get("file"), "page": s.get("page"), "snippet": s.get("source_text") or ""})

    def heights(self, n: int, building: dict, kind: str) -> None:
        norm, minimum, what = {
            "residential": (RESIDENTIAL_HEIGHT, Decimal("2.7"), "yashash xonalari balandligi kamida 2,7 m"),
            "public": (PUBLIC_HEIGHT, Decimal("3.0"), "jamoat binosi xonalari balandligi kamida 3,0 m"),
            "industrial": (INDUSTRIAL_HEIGHT, Decimal("3"), "ishlab chiqarish binosi balandligi kamida 3 m"),
        }[kind]
        title = f"{building['title']}: {what}"
        storeys = [(h, _number(h)) for h in building["heights"]
                   if volumes.parse_level(h.get("level")).kind in ("floor", "range", "generic", "unknown")]
        storeys = [(h, value) for h, value in storeys if value is not None]
        if not storeys:
            return  # nothing to compare: silence is better than a row per building saying so
        h, value = min(storeys, key=lambda item: item[1])
        where = f"eng past qavat balandligi {fmt_num(value)} m ({h.get('level') or 'sath yozilmagan'})"
        to_ceiling = bool(_CEILING.search(_plain(str(h.get("level") or ""))))
        if value < minimum:
            self.add(f"b{n}_height", title, norm, "mismatch", f"{where} — me'yordagi {fmt_num(minimum)} m dan kam", h)
        elif value < minimum + SLAB and not to_ceiling:
            self.add(f"b{n}_height", title, norm, "warning",
                     f"{where}. Bu qavat balandligi (poldan polgacha) bo'lsa, yopma qalinligi ayirilgach poldan shiftgacha "
                     f"balandlik {fmt_num(minimum)} m dan kam chiqishi mumkin — tekshiring", h)
        else:
            self.add(f"b{n}_height", title, norm, "ok", where, h)

    def volume_parts(self, n: int, building: dict, kind: str) -> None:
        """The code defines the building volume as above-ground plus below-ground."""
        norm = VOLUME_RESIDENTIAL if kind == "residential" else VOLUME_PUBLIC
        total = _field(building, "volume", "project")
        above, below = _field(building, "volume_above_ground", "project"), _field(building, "volume_below_ground", "project")
        t, a, b = _number(total), _number(above), _number(below)
        if t is None or a is None or b is None:
            return
        title = f"{building['title']}: qurilish hajmi = yer usti + yer osti"
        detail = f"{fmt_num(a)} + {fmt_num(b)} = {fmt_num(a + b)} m³; hujjatda jami {fmt_num(t)} m³"
        self.add(f"b{n}_volume_parts", title, norm, "ok" if abs(a + b - t) <= Decimal("1") else "mismatch", detail, total)

    def storeys(self, n: int, building: dict) -> None:
        """A residential building above 16 storeys is outside the residential code; the top technical floor is not a storey."""
        floors_value = _field(building, "floors")
        floors = _number(floors_value)
        if floors is None:
            return
        title = f"{building['title']}: qavatlar soni"
        technical_top = any(volumes.parse_level(h.get("level")).kind == "technical" for h in building["heights"])
        if floors > 16:
            self.add(f"b{n}_storeys", title, STOREYS_RESIDENTIAL, "warning",
                     f"{fmt_num(floors)} qavat — SHNQ 2.08.01-24 faqat 16 qavatgacha (16-qavat ham kiradi) bo'lgan turar joylarga "
                     "tegishli; bu bino uchun boshqa me'yor qo'llanadi", floors_value)
        else:
            self.add(f"b{n}_storeys", title, STOREYS_RESIDENTIAL, "ok",
                     f"{fmt_num(floors)} qavat" + ("; yuqoridagi texnik qavat qavatlar soniga kirmaydi — Hyu hisobida e'tiborga oling"
                                                   if technical_top else ""), floors_value)

    def greenery(self, site: list[dict]) -> None:
        total = [x for x in site if x["kind"] == "total" and _number(x) is not None]
        green = [x for x in site if x["kind"] == "greenery" and _number(x) is not None]
        title = "Hudud: ko'kalamzorlashtirilgan maydon ulushi kamida 30 foiz"
        if len(total) != 1 or len(green) != 1 or _number(total[0]) == 0:
            self.add("site_greenery", title, GREENERY, "not_run", "uchastka va ko'kalamzor maydoni hujjatdan bir qiymatli o'qilmadi")
            return
        share = _number(green[0]) / _number(total[0]) * 100  # type: ignore[operator]
        detail = (f"{fmt_num(_number(green[0]))} / {fmt_num(_number(total[0]))} m² = {fmt_num(share.quantize(Decimal('0.1')))} foiz. "  # type: ignore[arg-type]
                  "Me'yor mavze hududi uchun yozilgan; bu yerda bitta uchastka bo'yicha hisoblandi")
        self.add("site_greenery", title, GREENERY, "ok" if share >= 30 else "warning", detail, green[0])


def check(view: dict, description: dict | None, kb: KnowledgeBase) -> list[dict]:
    """Findings for one job, the serious ones first. Empty when the knowledge base has not been filled."""
    checker = Checker(kb)
    if not checker.documents:
        return []
    text = str((description or {}).get("value") or "")
    kinds: list[str] = []
    for n, building in enumerate(view["buildings"], 1):
        kind = building_kind(str(building["title"]), text)
        kinds.append(kind)
        if _AUXILIARY.search(_plain(str(building["title"]))):
            continue
        checker.heights(n, building, kind)
        if kind != "industrial":
            checker.volume_parts(n, building, kind)
        if kind == "residential":
            checker.storeys(n, building)
    if "residential" in kinds:
        checker.greenery(view["site"]["conclusion"] or view["site"]["project"])
    order = {"mismatch": 0, "warning": 1, "not_run": 2, "ok": 3}
    return sorted(checker.findings, key=lambda f: order[f["status"]])
