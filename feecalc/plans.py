"""Rooms read from a floor-plan drawing through the PDF text layer and its coordinates. No model, no image.

On a vector plan every room carries a label: its name and, on the line directly under it, its area
("Спальня" / "18.0м²"). The label is found by geometry:
- the area sits on the line under the last line of the name, centred on it; a name has one or two lines;
- if nothing is above, the name is the line directly below (a loggia: "3.3м²*0.5=1.65м²" / "Лоджия");
- a label rotated by 90 degrees has its name in the neighbouring column.
An area whose name cannot be found is kept without a name; nothing is guessed. A plan whose rooms are
numbered and listed in a table gives no pairs at all - it fails empty, not wrong.

Measured on the sample albums: every label of four plan sheets named correctly (41, 41, 47 and 48),
while reading the same sheet from an image left one or two plausible wrong numbers per sheet.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pymupdf

from . import classify, verify
from .ingest import MUPDF_LOCK

_AREA = re.compile(r"^(\d{1,3}[.,]\d{1,2})(?:м²|м2|m²|m2|²)$")             # "9.2²" (the unit letter forgotten) occurs
_EXPRESSION = re.compile(r"^(\d{1,3}[.,]\d{1,2})(?:м|m)[²2]\*(\d[.,]\d+)=(\d{1,3}[.,]\d{1,2})(?:м|m)[²2]$")
_NAME_WORD = re.compile(r"^[^\W\d_][^\d]*$")                                # starts with a letter, has no digit
_LINE_TOLERANCE, _SIDE_REACH, _WORD_GAP, _CENTRE_REACH = 4, 75, 9, 30        # points


def _words(pdf_path: Path, page_number: int) -> list[dict]:
    """Words of the page with their box, writing direction and font size."""
    out: list[dict] = []
    with MUPDF_LOCK, pymupdf.open(pdf_path) as doc:
        page = doc[page_number - 1]
        if page.rotation != 0:
            page.remove_rotation()
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                dx, dy = line["dir"]
                vertical = abs(dy) > abs(dx)
                text = "".join(span["text"] for span in line["spans"])
                x0, y0, x1, y1 = line["bbox"]
                # the text layer gives whole lines; word boxes are interpolated along the line
                for m in re.finditer(r"\S+", text):
                    a, b = m.start() / max(1, len(text)), m.end() / max(1, len(text))
                    if vertical and dy < 0:
                        box = (x0, y0 + (y1 - y0) * (1 - b), x1, y0 + (y1 - y0) * (1 - a))
                    elif vertical:
                        box = (x0, y0 + (y1 - y0) * a, x1, y0 + (y1 - y0) * b)
                    else:
                        box = (x0 + (x1 - x0) * a, y0, x0 + (x1 - x0) * b, y1)
                    out.append({"text": m.group(0), "box": box, "vertical": vertical,
                                "size": max(span["size"] for span in line["spans"])})
    return out


def _cx(w: dict) -> float:
    return (w["box"][0] + w["box"][2]) / 2


def _cy(w: dict) -> float:
    return (w["box"][1] + w["box"][3]) / 2


def _centred_run(line: list[dict], cx: float) -> list[dict]:
    """The words of one text line that form a single label around x = cx (a neighbouring label may share the line)."""
    if not line:
        return []
    runs, run = [], [line[0]]
    for previous, w in zip(line, line[1:]):
        if w["box"][0] - previous["box"][2] < _WORD_GAP:
            run.append(w)
        else:
            runs.append(run)
            run = [w]
    runs.append(run)
    best = min(runs, key=lambda r: abs((r[0]["box"][0] + r[-1]["box"][2]) / 2 - cx))
    return best if abs((best[0]["box"][0] + best[-1]["box"][2]) / 2 - cx) < _CENTRE_REACH else []


def _name_parts(area: dict, names: list[dict]) -> list[dict]:
    if area["vertical"]:
        height = area["size"] * 1.25
        near = [w for w in names if w["vertical"] and abs(_cy(w) - _cy(area)) < (area["box"][3] - area["box"][1]) / 2
                and 0 < abs(_cx(w) - _cx(area)) < 2.5 * height]
        return sorted(near, key=lambda w: abs(_cx(w) - _cx(area)))[:1]

    def line_at(k: int) -> list[dict]:
        """Name words k lines above the area label (k = -1: the line below), within reach of its centre."""
        y = area["box"][1] - k * (area["box"][3] - area["box"][1]) * 1.07
        return sorted((w for w in names if not w["vertical"] and abs(w["box"][1] - y) < _LINE_TOLERANCE
                       and area["box"][0] - _SIDE_REACH < _cx(w) < area["box"][2] + _SIDE_REACH), key=_cx)

    first = _centred_run(line_at(1), _cx(area))
    second = _centred_run(line_at(2), _cx(area)) if first else []
    return (second + first) or _centred_run(line_at(-1), _cx(area))


def plan_rooms(pdf_path: Path, page_number: int) -> list[dict]:
    """Room labels of one plan sheet: name, area, the label text as written (for highlighting), category."""
    words = _words(pdf_path, page_number)
    names = [w for w in words if _NAME_WORD.match(w["text"]) and len(w["text"]) >= 2]   # door and window marks have digits
    rooms: list[dict] = []
    for label in words:
        plain, expression = _AREA.match(label["text"]), _EXPRESSION.match(label["text"])
        if not plain and not expression:
            continue
        # a loggia is written as "area x factor = counted area": the counted area is what the plan states for it
        number = (expression.group(3) if expression else plain.group(1)).replace(",", ".")  # type: ignore[union-attr]
        name = " ".join(w["text"] for w in _name_parts(label, names)) or None
        category = classify.room_category(name) if name else None
        rooms.append({"name": name, "area": str(Decimal(number)), "source_text": label["text"],
                      "file": Path(pdf_path).name, "page": page_number,
                      "category": (category.category if category else None) or "other", "x": round(_cx(label), 1), "y": round(_cy(label), 1)})
    rooms.sort(key=lambda r: (round(r["y"] / 12), r["x"]))
    return rooms


def summary(rooms: list[dict]) -> dict:
    """Counts and area sums of one sheet, by category; areas without a name are counted apart."""
    named = [r for r in rooms if r["name"]]
    totals: dict[str, Decimal] = {}
    for r in named:
        totals[r["category"]] = totals.get(r["category"], Decimal(0)) + Decimal(r["area"])
    return {"count": len(rooms), "unnamed": len(rooms) - len(named),
            "area": str(sum((Decimal(r["area"]) for r in named), Decimal(0))),
            "by_category": {k: str(v) for k, v in sorted(totals.items())}}


# ---------- which storeys a plan sheet shows (from its title block) ----------
_BLOCK = re.compile(r"blok\W{0,3}(\d+)\W{0,2}(?!\s*,)")
_SKIP = re.compile(r"plan\s+polov|pollar\s+rejasi|cherdak|chordoq|krovl|tom\s+rejasi|fundament|poydevor|perekryt|potolk")
_BASEMENT = re.compile(r"plan\s+podval|plan\s+tsokol|plan\s+texpodpol|yertola\s+(?:qavati\s+)?rejasi|podval\w*\s+rejasi")
_RANGE = re.compile(r"plan\s+(\d+)\s*(?:\.{2,}|…|-|–|—|\s+i\s+|\s+va\s+)\s*(\d+)\s*-?\s*(?:go|y|chi)?\s*(?:etaj|qavat)")
_SINGLE = re.compile(r"plan\s+(\d+)\s*-?\s*(?:go|y|chi)?\s*(?:etaj|qavat)|(\d+)\s*-?\s*qavat\s+rejasi")


def sheet_storeys(title: str) -> dict | None:
    """What a plan sheet covers, read from its title block: {"block", "level": basement|floors, "floors": [...]}.

    None for anything that is not a storey plan (floor finishes, attic, roof): those repeat rooms or show none.
    """
    t = verify.transliterate(title)
    if _SKIP.search(t):
        return None
    block = _BLOCK.search(t)
    base = {"block": block.group(1) if block else ""}
    if _BASEMENT.search(t):
        return {**base, "level": "basement", "floors": []}
    m = _RANGE.search(t)
    if m and int(m.group(1)) < int(m.group(2)) <= 60:
        return {**base, "level": "floors", "floors": list(range(int(m.group(1)), int(m.group(2)) + 1))}
    m = _SINGLE.search(t)
    if m:
        return {**base, "level": "floors", "floors": [int(m.group(1) or m.group(2))]}
    return None
