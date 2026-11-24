from pathlib import Path

import pytest

from feecalc import classify, ingest


def test_sheet_types_by_stamp(sample_dir: Path):
    pages = ingest.ingest(sample_dir / "demo_project.pdf")
    kinds = [classify.sheet_type(p) for p in pages]
    assert [k.kind for k in kinds] == ["general_data", "finishing_schedule", "section", "floor_plan", "other"]
    assert [k.method for k in kinds[:4]] == ["rule:stamp"] * 4
    assert kinds[4].method == "textless"
    assert kinds[0].has_tep and kinds[1].has_rooms and not kinds[2].has_tep


def test_conclusion_page_is_ambiguous_for_sheet_rules(sample_dir: Path):
    page = ingest.ingest(sample_dir / "demo_conclusion.pdf")[0]
    assert classify.sheet_type(page).ambiguous


@pytest.mark.parametrize("name,category", [
    ("Паркинг", "Ha"), ("Автостоянка на 40 м/м", "Ha"), ("Avtoturargoh", "Ha"),
    ("Насосная", "Ht"), ("Электрощитовая", "Ht"), ("Венткамера", "Ht"), ("Тех. помещение", "Ht"),
    ("Texnik xona", "Ht"), ("Qozonxona", "Ht"),
    ("Лестничная клетка", "Hu"), ("Лифтовой холл", "Hu"), ("Приквартирная площадка", "Hu"),
    ("Zinapoya", "Hu"), ("Lift xolli", "Hu"), ("Koridor", "Hu"),
    ("Жилая комната", "other"), ("Кухня", "other"), ("Xonadon", "other"),
])
def test_room_dictionary(name, category):
    r = classify.room_category(name)
    assert r.category == category and r.method == "dictionary" and not r.needs_confirmation


def test_room_not_in_dictionary():
    r = classify.room_category("Бассейн")
    assert r.category is None and r.needs_confirmation


def test_mixed_group_needs_confirmation():
    r = classify.room_category(
        "Общая площадь (лестничная клетка, приквартирная площадка, лифтовой холл, электрощитовая, инвентарная)")
    assert r.category == "Hu" and r.method == "dictionary:mixed" and r.needs_confirmation
    assert "Texnik" in r.note
