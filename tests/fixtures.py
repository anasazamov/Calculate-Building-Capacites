"""Synthetic test documents built with PyMuPDF.

They imitate the structure described in TASK.md (a ShaffofQurilish-style conclusion and a
16-storey "Tip-2" AR album) using the expected values listed there. They are NOT real
documents; real samples live outside the repository (~/samples).
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

CSS = ("* {font-family: sans-serif; font-size: 9pt;} table {border-collapse: collapse; width: 100%;} "
       "td, th {border: 1px solid black; padding: 3px;} h1 {font-size: 13pt;} h2 {font-size: 11pt;}")

EXPECTED = {
    "conclusion": {"floors": "16", "footprint_area": "600.3", "total_area": "9410.0", "volume": "36036.0",
                   "apartments": "96", "plan_size": "26.97 × 26.89"},
    "project": {"footprint_area": "600.3", "volume_total": "36036", "volume_above_ground": "34619.3",
                "volume_below_ground": "1416.7", "total_area": "9410.0", "apartments": "96",
                "area_breakdown": ["465.9", "442.6", "7439.3", "1026.4", "35.8"]},
}


def _page(doc: pymupdf.Document, width: float, height: float, html: str, margin: float = 36) -> pymupdf.Page:
    page = doc.new_page(width=width, height=height)
    spare, _ = page.insert_htmlbox(pymupdf.Rect(margin, margin, width - margin, height - margin), html, css=CSS)
    assert spare >= 0, "fixture html does not fit the page"
    return page


def _stamp(page: pymupdf.Page, title: str, sheet: str) -> None:
    w, h = page.rect.width, page.rect.height
    html = (f"<table><tr><td colspan='2'>Жилой комплекс. г. Наманган, ул. Темир йул</td></tr>"
            f"<tr><td>16-ти этажный жилой дом Тип-2</td><td>Стадия РП</td></tr>"
            f"<tr><td>{title}</td><td>Лист {sheet}</td></tr></table>")
    page.insert_htmlbox(pymupdf.Rect(w - 520, h - 150, w - 20, h - 20), html, css=CSS)


def build_conclusion(path: Path, truncated_total: bool = False, unit_typo: bool = True) -> Path:
    """Conclusion with a buildings table. Two buildings so that the total row can be checked."""
    unit = "m2" if unit_typo else "m3"
    total_volume = "88432" if truncated_total else "88433.2"
    doc = pymupdf.open()
    _page(doc, 595, 842, f"""
<h1>DAVLAT EKSPERTIZASI XULOSASI № 284459</h1>
<p>Sana: 12.10.2026</p>
<p>Buyurtmachi: «Namuna Qurilish Invest» MChJ</p>
<p>Obyekt manzili: Samarqand shahri, Maysazor ko'chasi</p>
<p>Qurilish turi: Yangi qurilish</p>
<h2>Bino va inshootlar</h2>
<table>
<tr><th>Turi</th><th>Nomi</th><th>Qavati</th><th>Qurilish maydoni, m2</th><th>Umumiy maydoni, m2</th><th>Hajmi, {unit}</th></tr>
<tr><td>Turar joy binosi</td><td>Tip-2</td><td>16</td><td>600.3</td><td>9410.0</td><td>36036.0</td></tr>
<tr><td>Turar joy binosi</td><td>Tip-1</td><td>12</td><td>812.5</td><td>9980.4</td><td>52397.2</td></tr>
<tr><td colspan="3">Jami</td><td>1412.8</td><td>19390.4</td><td>{total_volume}</td></tr>
</table>
""")
    _page(doc, 595, 842, """
<h2>Asosiy texnik ko'rsatkichlar</h2>
<p>Tip-2 turar joy binosi 16 qavatli, tarxdagi o'lchami 26.97 × 26.89 m.</p>
<p>Tip-2: xonadonlar soni 96 ta.</p>
<p>Tip-2: yerto'la balandligi 3.2 m, turar joy qavatlari balandligi 3.0 m.</p>
<p>Tip-1 turar joy binosi 12 qavatli, tarxdagi o'lchami 30.20 × 27.10 m, xonadonlar soni 88 ta.</p>
""")
    doc.save(path)
    doc.close()
    return path


def build_project(path: Path) -> Path:
    doc = pymupdf.open()
    a3 = (1191, 842)
    p = _page(doc, *a3, """
<h1>Общие данные</h1>
<h2>Технико-экономические показатели</h2>
<table>
<tr><th>Наименование</th><th>Ед. изм.</th><th>Количество</th></tr>
<tr><td>Площадь застройки</td><td>м2</td><td>600.3</td></tr>
<tr><td>Строительный объём</td><td>м3</td><td>36036</td></tr>
<tr><td>в том числе надземная часть</td><td>м3</td><td>34619.3</td></tr>
<tr><td>подземная часть</td><td>м3</td><td>1416.7</td></tr>
<tr><td>Подвал на отм. -3.200</td><td>м2</td><td>465.9</td></tr>
<tr><td>1-этаж</td><td>м2</td><td>442.6</td></tr>
<tr><td>2–16 этажи</td><td>м2</td><td>7439.3</td></tr>
<tr><td>Общая площадь (лестничная клетка, приквартирная площадка, лифтовой холл, электрощитовая, инвентарная)</td><td>м2</td><td>1026.4</td></tr>
<tr><td>Тех. помещения на отм. +52.420</td><td>м2</td><td>35.8</td></tr>
<tr><td>Общая площадь здания</td><td>м2</td><td>9410.0</td></tr>
<tr><td>Количество квартир</td><td>шт</td><td>96</td></tr>
</table>
<p>Высота этажа - 3.0 м. Высота подвала - 3.2 м.</p>
""")
    _stamp(p, "Общие данные", "1")
    p = _page(doc, *a3, """
<h1>Ведомость отделки помещений</h1>
<table>
<tr><th>Наименование помещения</th><th>Этаж</th><th>Площадь, м2</th></tr>
<tr><td>Насосная</td><td>Подвал</td><td>24.5</td></tr>
<tr><td>Электрощитовая</td><td>Подвал</td><td>12.3</td></tr>
<tr><td>Венткамера</td><td>Подвал</td><td>15.8</td></tr>
<tr><td>Лестничная клетка</td><td>1 этаж</td><td>18.6</td></tr>
<tr><td>Лифтовой холл</td><td>1 этаж</td><td>9.4</td></tr>
<tr><td>Колясочная</td><td>1 этаж</td><td>6.2</td></tr>
<tr><td>Помещение консьержа</td><td>1 этаж</td><td>7.5</td></tr>
<tr><td>Диспетчерская</td><td>1 этаж</td><td>8.1</td></tr>
</table>
""")
    _stamp(p, "Ведомость отделки помещений", "2")
    p = doc.new_page(width=a3[0], height=a3[1])
    for i, mark in enumerate(["-3.200", "±0.000", "+3.000", "+6.000", "+9.000", "+12.000", "+48.000", "+52.420"]):
        p.insert_text((100, 700 - i * 60), mark, fontsize=10)
    _stamp(p, "Разрез 1-1", "3")
    p = doc.new_page(width=a3[0], height=a3[1])
    p.insert_text((100, 100), "6000  3000  6000  12000", fontsize=10)
    _stamp(p, "План на отм. 0.000", "4")
    doc.new_page(width=a3[0], height=a3[1])  # textless page
    doc.save(path)
    doc.close()
    return path
