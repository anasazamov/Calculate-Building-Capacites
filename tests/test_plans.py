"""Rooms of a floor plan, paired by coordinates from the text layer."""
from pathlib import Path

import pymupdf

from feecalc import plans


def plan(path: Path, labels: list[tuple[float, float, list[str]]]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for x, y, lines in labels:
        for i, text in enumerate(lines):
            width = pymupdf.get_text_length(text, fontname="tiro", fontsize=10)
            page.insert_text((x - width / 2, y + i * 10.7), text, fontname="tiro", fontsize=10)
    page.insert_text((300, 560), "D-3  Ok-2  1200", fontname="tiro", fontsize=10)        # marks and dimensions are not rooms
    doc.save(str(path))
    return path


def test_name_above_area_and_the_loggia_form(tmp_path: Path):
    pdf = plan(tmp_path / "plan.pdf", [
        (150, 100, ["Yotoqxona", "18.0m2"]), (400, 100, ["Umumiy xona", "20,2m2"]), (650, 100, ["Zinapoya", "14.4m2"]),
        (150, 300, ["3.3m2*0.5=1.65m2", "Lodjiya"]), (400, 300, ["9.2m2"])])
    rooms = plans.plan_rooms(pdf, 1)
    by_name = {r["name"]: r for r in rooms}
    assert {n: r["area"] for n, r in by_name.items()} == {
        "Yotoqxona": "18.0", "Umumiy xona": "20.2", "Zinapoya": "14.4", "Lodjiya": "1.65", None: "9.2"}
    first = by_name["Yotoqxona"]
    assert first["file"] == "plan.pdf" and first["page"] == 1 and first["source_text"] == "18.0m2"
    assert by_name["Zinapoya"]["category"] == "Hu" and first["category"] == "other" and by_name[None]["category"] == "other"
    total = plans.summary(rooms)
    assert total["count"] == 5 and total["unnamed"] == 1 and total["by_category"]["Hu"] == "14.4"
    assert total["area"] == "54.25"                                           # the unnamed area is not summed


def test_neighbouring_labels_on_one_line_do_not_mix(tmp_path: Path):
    pdf = plan(tmp_path / "near.pdf", [(200, 100, ["Oshxona", "13.8m2"]), (290, 100, ["Xoll", "13.1m2"])])
    assert [(r["name"], r["area"]) for r in plans.plan_rooms(pdf, 1)] == [("Oshxona", "13.8"), ("Xoll", "13.1")]


def test_a_sheet_without_labels_gives_nothing(tmp_path: Path):
    doc = pymupdf.open()
    doc.new_page().insert_text((60, 80), "Eksplikatsiya: 1 Yotoqxona 18.0", fontsize=10)
    doc.save(str(tmp_path / "table.pdf"))
    assert plans.plan_rooms(tmp_path / "table.pdf", 1) == [] and plans.summary([])["count"] == 0


def test_storeys_of_a_plan_sheet_come_from_its_title():
    assert plans.sheet_storeys("БЛОК (1) РП 3 ПЛАН ПОДВАЛА") == {"block": "1", "level": "basement", "floors": []}
    assert plans.sheet_storeys("БЛОК (2) РП 25 ПЛАН 2....4 ГО ЭТАЖА") == {"block": "2", "level": "floors", "floors": [2, 3, 4]}
    assert plans.sheet_storeys("ПЛАН 1 ГО ЭТАЖА") == {"block": "", "level": "floors", "floors": [1]}
    assert plans.sheet_storeys("План 2-16 этажей")["floors"] == list(range(2, 17))
    assert plans.sheet_storeys("5-qavat rejasi")["floors"] == [5]
    for other in ("БЛОК (1) РП 7 ПЛАН ПОЛОВ", "ПЛАН ЧЕРДАКА", "План кровли", "Разрез 1-1", "Общие данные"):
        assert plans.sheet_storeys(other) is None
