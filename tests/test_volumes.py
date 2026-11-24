from decimal import Decimal as D

import pytest

from feecalc import verify, volumes
from feecalc.volumes import Level


def val(number: str | None, **extra) -> dict:
    status = verify.ACCEPTED if number is not None else verify.NOT_FOUND
    return {"value": number, "number": number, "status": status, "file": "a.pdf", "page": 1,
            "source_text": number, "reason": "", **extra}


@pytest.mark.parametrize("text,mark,expected", [
    ("Подвал на отм. -3.200", "-3.200", Level("basement")),
    ("1-этаж", None, Level("floor", 1)), ("1 этаж", None, Level("floor", 1)), ("2-qavat", None, Level("floor", 2)),
    ("2–16 этажи", None, Level("range", 2, 16)), ("2-16 qavatlar", None, Level("range", 2, 16)),
    ("Тех. помещения на отм. +52.420", "+52.420", Level("technical")),
    ("этаж", None, Level("generic")), ("turar joy qavatlari", None, Level("generic")),
    ("Общая площадь (лестничная клетка)", None, Level("unknown")), (None, "-3.200", Level("basement")),
    ("yerto'la", None, Level("basement")),
])
def test_parse_level(text, mark, expected):
    assert volumes.parse_level(text, mark) == expected


HEIGHTS = volumes.collect_heights([val("3.0", level="этаж"), val("3.2", level="подвал")], [])


def test_resolve_height():
    assert volumes.resolve_height(Level("basement"), HEIGHTS)[0] == D("3.2")
    assert volumes.resolve_height(Level("floor", 5), HEIGHTS)[0] == D("3.0")
    assert volumes.resolve_height(Level("range", 2, 16), HEIGHTS)[0] == D("3.0")
    h, note = volumes.resolve_height(Level("technical"), HEIGHTS)
    assert h is None and "xodim" in note
    h, note = volumes.resolve_height(Level("unknown"), HEIGHTS)
    assert h == D("3.0") and "tasdiqlang" in note


def test_unverified_height_is_ignored():
    bad = {"value": "3.0", "number": None, "status": verify.UNVERIFIED, "level": "этаж"}
    assert volumes.collect_heights([bad], []) == []


def test_project_heights_win_over_conclusion():
    hs = volumes.collect_heights([val("3.0", level="этаж")], [val("3.3", level="qavat")])
    h, note = volumes.resolve_height(Level("floor", 1), hs)
    assert h == D("3.0") and note.startswith("loyiha")
    hs = volumes.collect_heights([], [val("3.3", level="qavat")])
    assert volumes.resolve_height(Level("floor", 1), hs)[1].startswith("xulosa")


def project(rooms=(), breakdown=()) -> dict:
    return {"scalars": {"volume_total": val("36036")},
            "rooms": [{"name": n, "level": lv, "area": val(a), "volume": val(v) if v else None}
                      for n, lv, a, v in rooms],
            "area_breakdown": [{**val(a), "name": n, "level_mark": m} for n, m, a in breakdown]}


def test_computed_volume_and_stated_volume():
    p = project(rooms=[("Насосная", "Подвал", "24.5", None), ("Паркинг", "Подвал", "400", "1300")])
    comps = volumes.build_components(p, HEIGHTS, {}, {})
    pump, parking = comps
    assert pump["category"] == "Ht" and pump["computed"] and D(pump["volume"]) == D("78.40")
    assert "24.5 m² × 3.2 m" in pump["formula"]
    assert parking["category"] == "Ha" and not parking["computed"] and parking["volume"] == "1300"
    assert volumes.category_volume("Ht", comps)["proposal"] == "78.40"
    assert volumes.category_volume("Ha", comps)["proposal"] == "1300"
    hu = volumes.category_volume("Hu", comps)
    assert hu["proposal"] == "0" and "tasdiqlang" in hu["notes"][0]


def test_missing_height_makes_volume_incomplete():
    p = project(breakdown=[("Тех. помещения на отм. +52.420", "+52.420", "35.8")])
    comps = volumes.build_components(p, HEIGHTS, {}, {})
    assert comps[0]["volume"] is None
    ht = volumes.category_volume("Ht", comps)
    assert ht["proposal"] is None and not ht["complete"]


def test_override_height_volume_category_and_inclusion():
    p = project(breakdown=[("Тех. помещения на отм. +52.420", "+52.420", "35.8")],
                rooms=[("Насосная", "Подвал", "24.5", None), ("Бассейн", "1 этаж", "50", None)])
    base = volumes.build_components(p, HEIGHTS, {"Бассейн": "other"}, {})
    tech, pump, pool = base
    assert not pump["included"] and "takror" in pump["exclusion_note"]
    assert pool["category_method"] == "model" and pool["needs_confirmation"]
    ov = {tech["id"]: {"height": "4.7", "reason": "kesim bo'yicha"},
          pump["id"]: {"included": True, "reason": "alohida xona"},
          pool["id"]: {"category": "Hu", "reason": "umumiy"}}
    tech2, pump2, pool2 = volumes.build_components(p, HEIGHTS, {"Бассейн": "other"}, ov)
    assert D(tech2["volume"]) == D("35.8") * D("4.7") and tech2["computed"]
    assert pump2["included"] and pool2["category"] == "Hu" and not pool2["needs_confirmation"]
    ov[tech["id"]] = {"volume": "168.32", "reason": "loyihachi hisobi"}
    tech3 = volumes.build_components(p, HEIGHTS, {}, ov)[0]
    assert tech3["volume"] == "168.32" and not tech3["computed"]


def test_unaccepted_area_gives_no_volume():
    p = project(rooms=[("Насосная", "Подвал", None, None)])
    comps = volumes.build_components(p, HEIGHTS, {}, {})
    assert comps[0]["volume"] is None and "maydon qabul qilinmagan" in comps[0]["formula"]


def test_level_rows_are_not_room_groups():
    p = project(breakdown=[("1-этаж", None, "442.6"), ("2–16 этажи", None, "7439.3")])
    comps = volumes.build_components(p, HEIGHTS, {}, {})
    assert all(c["category"] == "other" and c["category_method"] == "level_row" and not c["included"] for c in comps)
    assert not volumes.needs_model_suggestion("1-этаж", "breakdown", "1-этаж", None)
    assert volumes.needs_model_suggestion("Бассейн", "room", "1 этаж", None)


def test_hb():
    hb = volumes.hb_volume(project(), {"volume": val("36036.0")})
    assert hb["proposal"] == "36036" and hb["check"]["status"] == verify.OK
    hb = volumes.hb_volume(project(), {"volume": val("36100.0")})
    assert hb["check"]["status"] == verify.MISMATCH
    p = project()
    p["scalars"]["volume_total"] = {"value": "36036", "number": None, "status": verify.UNVERIFIED, "reason": "x"}
    assert volumes.hb_volume(p, None)["proposal"] is None


def test_hyu():
    p = project(breakdown=[("1-этаж", None, "442.6"), ("2–16 этажи", None, "7439.3")])
    comps = volumes.build_components(p, HEIGHTS, {}, {})
    assert volumes.hyu_volume(None, D("16"), comps, None)["proposal"] is None
    assert volumes.hyu_volume(16, D("16"), comps, None)["proposal"] == "0"
    assert volumes.hyu_volume(20, D("16"), comps, None)["proposal"] == "0"
    h = volumes.hyu_volume(12, D("16"), comps, None)
    assert h["proposal"] == "5951.44" and "13–16" in h["formula"]   # 7439.3 / 15 x 3.0 x 4
    h = volumes.hyu_volume(12, D("16"), [], D("34619.3"))
    assert h["proposal"] == "8654.83" and "teng bo'lingan" in h["notes"][0]  # 34619.3 / 16 x 4
    assert volumes.hyu_volume(12, D("16"), [], None)["proposal"] is None
    assert volumes.hyu_volume(12, None, comps, None)["proposal"] is None
