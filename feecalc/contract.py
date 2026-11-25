"""Word documents made from a finalized calculation sheet.

Two routes:
- `annex(sheet)` builds the calculation annex of the fee contract from scratch: the object, the five
  volumes, the coefficients, the formula with the numbers, the sum in digits and in words, 20% / 80%.
- `fill(template, sheet)` fills the organisation's own contract template (.docx) by replacing
  {{placeholders}}; the list of placeholders is `fields(sheet)`.

Everything is taken from the sheet (already computed in Decimal by fee.py); nothing is calculated here.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal
from pathlib import Path

import pymupdf
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from .fee import fmt_num

_ONES = ("", "bir", "ikki", "uch", "to'rt", "besh", "olti", "yetti", "sakkiz", "to'qqiz")
_TENS = ("", "o'n", "yigirma", "o'ttiz", "qirq", "ellik", "oltmish", "yetmish", "sakson", "to'qson")
_SCALES = ("", "ming", "million", "milliard", "trillion")
VOLUME_NAMES = {"Hb": "Binoning umumiy hajmi", "Hyu": "Ruxsat etilgan qavatlar sonidan yuqori hajm",
                "Ha": "Avtoturargoh hajmi", "Ht": "Texnik qavat va xonalar hajmi",
                "Hu": "Umumiy foydalanishdagi qism hajmi"}
COEFFICIENT_NAMES = {"kt": "Kt — qurilish turi", "ko": "Ko — obyekt turi", "kz": "Kz — hududiy zona",
                     "kj": "Kj — obyekt joylashuvi"}
BLANK = "____________"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def in_words(number: int) -> str:
    """A whole non-negative number in Uzbek words: 293832000 -> "ikki yuz to'qson uch million sakkiz yuz o'ttiz ikki ming"."""
    assert 0 <= number < 10 ** 15, number
    if number == 0:
        return "nol"
    parts: list[str] = []
    for scale, name in enumerate(_SCALES):
        group = number // 1000 ** scale % 1000
        if not group:
            continue
        words = [f"{_ONES[group // 100]} yuz"] if group >= 100 else []
        words += [w for w in (_TENS[group % 100 // 10], _ONES[group % 10]) if w]
        parts.append(" ".join(words + ([name] if name else [])))
    return " ".join(reversed(parts))


def money(value: object) -> str:
    """4294208160 -> "4 294 208 160"; a fraction is kept with a comma."""
    whole, _, fraction = fmt_num(Decimal(str(value))).partition(".")
    digits = whole.lstrip("-")
    grouped = " ".join(reversed([digits[max(0, i - 3):i] for i in range(len(digits), 0, -3)]))
    return ("-" if whole.startswith("-") else "") + grouped + ("," + fraction if fraction else "")


def _scalar(sheet: dict, label: str) -> str:
    for row in sheet["scalars"]:
        if row["label"] == label:
            value = (row["conclusion"] or row["project"] or {}).get("value")
            return str(value) if value is not None else ""
    return ""


def fields(sheet: dict) -> dict[str, str]:
    """Placeholder name -> text. The same values feed the built-in annex and a custom template."""
    result = sheet["fee"]["result"]
    contract = sheet.get("contract") or {}
    out = {
        "shartnoma_raqami": contract.get("number") or BLANK, "shartnoma_sanasi": contract.get("date") or BLANK,
        "obyekt": str((sheet.get("object") or {}).get("value") or sheet["job"]["title"]),
        "manzil": _scalar(sheet, "Manzil"), "buyurtmachi": _scalar(sheet, "Buyurtmachi") or BLANK,
        "xulosa_raqami": _scalar(sheet, "Xulosa raqami") or BLANK, "xulosa_sanasi": _scalar(sheet, "Xulosa sanasi") or BLANK,
        "qurilish_turi": sheet["fee"]["options"]["kt"], "obyekt_turi": sheet["fee"]["options"]["ko"],
        "hududiy_zona": sheet["fee"]["options"]["kz"], "joylashuv": sheet["fee"]["options"]["kj"],
        "bhm": money(sheet["fee"]["coefficients"]["bhm"]),
        "hisob_hajmi": money(result["chargeable_volume"]), "koeffitsiyent": fmt_num(Decimal(result["multiplier"])).replace(".", ","),
        "formula": result["formula"],
        "yigim": money(result["fee"]), "yigim_soz": in_words(int(Decimal(result["fee"]))),
        "oldindan_20": money(result["advance_20"]), "oldindan_20_soz": in_words(int(Decimal(result["advance_20"]))),
        "qolgan_80": money(result["remainder_80"]), "qolgan_80_soz": in_words(int(Decimal(result["remainder_80"]))),
        "yakunlagan": sheet["finalized"]["by"], "yakunlangan_sana": sheet["finalized"]["at"],
        "murojaat_raqami": _scalar(sheet, "Murojaat raqami") or BLANK, "oldindan_foiz": "20%",
    }
    for code in VOLUME_NAMES:
        out[code.lower()] = money(sheet["volumes"][code]["decision"]["value"])
    for group in COEFFICIENT_NAMES:
        out[group] = fmt_num(Decimal(sheet["fee"]["coefficients"][group])).replace(".", ",")
    out["kkengash"] = fmt_num(Decimal(sheet["fee"]["coefficients"]["k_council"])).replace(".", ",")
    return out


# Placeholder -> what it stands for (shown to staff next to the template upload).
PLACEHOLDERS = {
    "shartnoma_raqami": "shartnoma raqami", "shartnoma_sanasi": "shartnoma sanasi", "obyekt": "obyekt nomi (xulosadan)",
    "manzil": "manzil", "buyurtmachi": "ariza beruvchi (buyurtmachi)", "xulosa_raqami": "ekspertiza xulosasi raqami",
    "xulosa_sanasi": "ekspertiza xulosasi sanasi", "qurilish_turi": "qurilish turi (Kt holati)",
    "obyekt_turi": "obyekt turi (Ko holati)", "hududiy_zona": "hududiy zona (Kz holati)", "joylashuv": "joylashuv (Kj holati)",
    "bhm": "BHM, so'm", "hisob_hajmi": "hisob hajmi, m³", "koeffitsiyent": "koeffitsiyentlar ko'paytmasi",
    "formula": "formula sonlar bilan", "yigim": "yig'im summasi, raqam bilan", "yigim_soz": "yig'im summasi, so'z bilan",
    "oldindan_20": "20% oldindan to'lov", "oldindan_20_soz": "20% oldindan to'lov, so'z bilan",
    "qolgan_80": "qolgan 80%", "qolgan_80_soz": "qolgan 80%, so'z bilan", "yakunlagan": "tasdiqlagan xodim",
    "yakunlangan_sana": "tasdiqlangan vaqt", "hb": "Hb, m³", "hyu": "Hyu, m³", "ha": "Ha, m³", "ht": "Ht, m³", "hu": "Hu, m³",
    "kt": "Kt qiymati", "ko": "Ko qiymati", "kz": "Kz qiymati", "kj": "Kj qiymati", "kkengash": "Kengash koeffitsiyenti",
    "murojaat_raqami": "ariza (murojaat) raqami", "oldindan_foiz": "oldindan to'lov foizi",
}


def _paragraphs(doc: Document) -> list:
    return list(doc.paragraphs) + [p for t in doc.tables for row in t.rows for cell in row.cells for p in cell.paragraphs]


def unknown_placeholders(template: Path) -> list[str]:
    """Placeholders of a template that this system cannot fill (checked when the template is uploaded)."""
    names = {n for p in _paragraphs(Document(str(template))) for n in _PLACEHOLDER.findall(p.text)}
    return sorted(names - set(PLACEHOLDERS))


def _table(doc: Document, rows: list[tuple[str, ...]], header: tuple[str, ...] | None = None) -> None:
    table = doc.add_table(rows=0, cols=len(rows[0]))
    table.style = "Table Grid"
    for i, row in enumerate(([header] if header else []) + rows):
        cells = table.add_row().cells
        for cell, text in zip(cells, row):
            cell.text = text
            if header and i == 0:
                cell.paragraphs[0].runs[0].bold = True
    doc.add_paragraph()


def _save(doc: Document) -> bytes:
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def annex(sheet: dict) -> bytes:
    """The calculation annex of the contract as a .docx file."""
    f = fields(sheet)
    doc = Document()
    doc.styles["Normal"].font.name = "Times New Roman"
    doc.styles["Normal"].font.size = Pt(12)
    for text, bold in ((f"{f['shartnoma_sanasi']} dagi {f['shartnoma_raqami']}-sonli shartnomaga ilova", False),
                       ("Muhandislik-kommunikatsiya tarmoqlari va transport infratuzilmasi yig'imini", True),
                       ("HISOBLASH MA'LUMOTNOMASI", True)):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if bold else WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run(text).bold = bold
    doc.add_paragraph(f"Asos: {sheet['legal_basis']}; yig'im ekspertiza xulosasi natijasiga muvofiq hisoblangan (4-ilova, 11-band).")

    doc.add_paragraph().add_run("1. Obyekt").bold = True
    _table(doc, [("Obyekt nomi", f["obyekt"]), ("Manzil", f["manzil"]), ("Ariza beruvchi (buyurtmachi)", f["buyurtmachi"]),
                 ("Ekspertiza xulosasi", f"№ {f['xulosa_raqami']}, {f['xulosa_sanasi']}")])

    doc.add_paragraph().add_run("2. Hajmlar").bold = True
    _table(doc, [(code, name, f[code.lower()]) for code, name in VOLUME_NAMES.items()]
           + [("", "Hisob hajmi: (Hb + Hyu) − (Ha + Ht + Hu)", f["hisob_hajmi"])], ("Belgi", "Ko'rsatkich", "Hajm, m³"))

    doc.add_paragraph().add_run("3. Koeffitsiyentlar").bold = True
    names = {"kt": "qurilish_turi", "ko": "obyekt_turi", "kz": "hududiy_zona", "kj": "joylashuv"}
    _table(doc, [(title, f[names[group]], f[group]) for group, title in COEFFICIENT_NAMES.items()]
           + [("Kengash koeffitsiyenti", "", f["kkengash"]), ("Koeffitsiyentlar ko'paytmasi", "", f["koeffitsiyent"])],
           ("Koeffitsiyent", "Qo'llangan holat", "Qiymat"))

    doc.add_paragraph().add_run("4. Hisob").bold = True
    doc.add_paragraph(f"Bazaviy hisoblash miqdori (BHM): {f['bhm']} so'm.")
    doc.add_paragraph("Ti = BHM × ((Hb + Hyu) − (Ha + Ht + Hu)) × Kt × Ko × Kz × Kj × Kkengash")
    doc.add_paragraph(f"Ti = {f['formula']}")
    _table(doc, [("Yig'im summasi", f"{f['yigim']} so'm", f"({f['yigim_soz']} so'm)"),
                 ("Oldindan to'lov — 20 foiz", f"{f['oldindan_20']} so'm", f"({f['oldindan_20_soz']} so'm)"),
                 ("Qolgan qism — 80 foiz", f"{f['qolgan_80']} so'm", f"({f['qolgan_80_soz']} so'm)")])
    doc.add_paragraph(f"Hisob varaqasi: ish № {sheet['job']['id']}; tasdiqlagan xodim: {f['yakunlagan']}, {f['yakunlangan_sana']}.")
    doc.add_paragraph()
    _table(doc, [("Kompaniya nomidan:", "Ariza beruvchi nomidan:"), (f"\n{BLANK}  /{BLANK}/", f"\n{BLANK}  /{BLANK}/")])
    return _save(doc)


def fill(template: Path, sheet: dict) -> bytes:
    """The organisation's own .docx template with every {{placeholder}} replaced. An unknown
    placeholder stops with an error that names it, so a typo in the template cannot pass silently."""
    values = fields(sheet)
    doc = Document(str(template))
    for paragraph in _paragraphs(doc):
        text = paragraph.text
        names = _PLACEHOLDER.findall(text)
        if not names:
            continue
        unknown = [n for n in names if n not in values]
        assert not unknown, f"Shablonda noma'lum maydon: {unknown}; mavjud maydonlar: {sorted(values)}"
        # Word splits a paragraph into runs arbitrarily; the first run keeps the formatting
        paragraph.runs[0].text = _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
        for run in paragraph.runs[1:]:
            run.text = ""
    return _save(doc)


# ---------- a contract made by placing fields on the pages of the organisation's own PDF ----------
# The template is any PDF (a scan is fine). Staff drag fields onto its pages in the designer; the layout is a
# list of boxes in page points. Filling stamps each value into its box, over a white patch if asked.
_FONTS = {False: Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
          True: Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf")}
_ALIGN = {"left": pymupdf.TEXT_ALIGN_LEFT, "center": pymupdf.TEXT_ALIGN_CENTER, "right": pymupdf.TEXT_ALIGN_RIGHT}


def layout_problems(boxes: object, pages: int) -> str:
    """Why a layout sent by the designer cannot be stored ("" when it can)."""
    if not isinstance(boxes, list) or len(boxes) > 400:
        return "ro'yxat kutilgan (ko'pi bilan 400 ta maydon)"
    for b in boxes:
        if not isinstance(b, dict) or b.get("field") not in PLACEHOLDERS:
            return "noma'lum maydon"
        if not all(isinstance(b.get(k), (int, float)) for k in ("page", "x", "y", "w", "h")):
            return "maydon o'lchami son emas"
        if not 1 <= int(b["page"]) <= pages or b["w"] < 10 or b["h"] < 6 or b["x"] < 0 or b["y"] < 0:
            return "maydon sahifadan tashqarida yoki juda kichik"
        if not isinstance(b.get("size", 11), (int, float)) or not 5 <= b.get("size", 11) <= 40:
            return "harf o'lchami 5 dan 40 gacha bo'lishi kerak"
        if b.get("align", "left") not in _ALIGN:
            return "tekislash noto'g'ri"
    return ""


def clean_layout(boxes: object, pages: int) -> list[dict]:
    """A layout as received from the designer, checked and reduced to what is stored. Bad input is an error."""
    assert isinstance(boxes, list) and len(boxes) <= 400, "layout: a list of at most 400 boxes is expected"
    out: list[dict] = []
    for b in boxes:
        assert isinstance(b, dict) and b.get("field") in PLACEHOLDERS, f"layout: unknown field {b!r}"
        page, size = int(b["page"]), float(b.get("size", 11))
        x, y, w, h = (float(b[k]) for k in ("x", "y", "w", "h"))
        assert 1 <= page <= pages and 5 <= size <= 40 and w >= 10 and h >= 6 and x >= 0 and y >= 0, f"layout: bad box {b!r}"
        assert b.get("align", "left") in _ALIGN, f"layout: bad alignment {b!r}"
        out.append({"field": b["field"], "page": page, "x": round(x, 1), "y": round(y, 1), "w": round(w, 1), "h": round(h, 1),
                    "size": size, "bold": bool(b.get("bold")), "align": b.get("align", "left"), "cover": bool(b.get("cover", True))})
    return out


def stamp(template: Path, layout: list[dict], values: dict[str, str]) -> bytes:
    """The template PDF with every placed field filled. Text that does not fit its box is shrunk, never cut."""
    doc = pymupdf.open(str(template))
    for box in layout:
        page = doc[box["page"] - 1]
        rect = pymupdf.Rect(box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"])
        font = _FONTS[box["bold"]]
        name = ("contractbold" if box["bold"] else "contract") if font.exists() else ("tibo" if box["bold"] else "tiro")
        text, options = values[box["field"]], {"fontname": name, "fontfile": str(font) if font.exists() else None,
                                               "align": _ALIGN[box["align"]]}
        # Fit is tried on a scratch page: first a smaller letter, then a taller box - the text is never cut.
        scratch = pymupdf.open().new_page(width=page.rect.width, height=page.rect.height)
        size = box["size"]
        while size > 6 and scratch.insert_textbox(rect, text, fontsize=size, **options) < 0:
            size -= 0.5
        while rect.y1 < page.rect.height and scratch.insert_textbox(rect, text, fontsize=size, **options) < 0:
            rect.y1 += size
        if box["cover"]:
            page.draw_rect(rect, color=None, fill=(1, 1, 1))
        page.insert_textbox(rect, text, fontsize=size, **options)
    return doc.tobytes(deflate=True)


def sample_values() -> dict[str, str]:
    """What the designer's preview prints: each field's own name, so that the placement can be judged."""
    return {name: f"«{meaning}»" for name, meaning in PLACEHOLDERS.items()}
