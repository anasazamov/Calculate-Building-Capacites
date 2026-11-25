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
    return {"rooms": [{"name": n, "level": lv, "area": val(a), "volume": val(v) if v else None}
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
    # not clearly named as technical or common-use: usable area, no deduction, nothing left for staff to confirm
    assert (pool["category"], pool["category_method"], pool["needs_confirmation"]) == ("other", "not_explicit", False)
    guessed = volumes.build_components(p, HEIGHTS, {"Бассейн": "Hu"}, {})[2]
    assert guessed["category"] == "other" and "model taxmini: Hu" in guessed["category_note"]
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


def building(name: str, volume: str | None = None, count: str | None = None, footprint: str | None = None,
             floors: str | None = None, reference: str | None = None, kind: str | None = None) -> dict:
    text = lambda t: {"value": t, "number": None, "status": verify.ACCEPTED if t else verify.NOT_FOUND,
                      "file": "a.pdf", "page": 1, "source_text": t, "reason": ""}
    return {"name": text(name), "type": text(kind), "volume": val(volume), "count": val(count),
            "footprint_area": val(footprint), "floors": val(floors), "reference": text(reference)}


def test_match_buildings_by_name_and_values():
    project = [building("16-ти этажный жилой дом Тип-2", "36036", footprint="600.3")]
    conclusion = [building("1-tip", "44198.6", kind="Turar joy binosi"), building("2-tip", "36036.0", footprint="600.3"),
                  building("Magazin", "1200")]
    pairs = volumes.match_buildings(project, conclusion)
    assert [(x["p"], x["c"]) for x in pairs] == [(None, 0), (0, 1), (None, 2)]
    assert "hajm mos" in pairs[1]["basis"] and "loyiha berilmagan" in pairs[0]["basis"]
    # staff pairs it differently / removes the pair
    assert [(x["p"], x["c"]) for x in volumes.match_buildings(project, conclusion, {"0": 2})] == [
        (None, 0), (None, 1), (0, 2)]
    assert [(x["p"], x["c"]) for x in volumes.match_buildings(project, conclusion, {"0": -1})] == [
        (None, 0), (None, 1), (None, 2), (0, None)]
    assert [(x["p"], x["c"]) for x in volumes.match_buildings([], conclusion)] == [(None, 0), (None, 1), (None, 2)]
    single = volumes.match_buildings([building("Дом")], [building("Bino")])
    assert (single[0]["p"], single[0]["c"]) == (0, 0)


def rows_for(project: list[dict], conclusion: list[dict], overrides: dict | None = None) -> list[dict]:
    return volumes.hb_rows(project, conclusion, volumes.match_buildings(project, conclusion), overrides or {})


def test_hb_rows_project_total_matches_count_times_unit():
    # 21 cottages: project gives the total, the conclusion one unit; 21 x 943.94 = 19822.74 vs 19822.65
    rows = rows_for([building("Turar-joy binolari", "19822.65", count="21")], [building("Turar-joy binosi", "943.94")])
    assert rows[0]["volume"] == "19822.65" and rows[0]["check"]["status"] == verify.ROUNDING
    assert rows[0]["count"] == "21" and rows[0]["count_note"] == "loyihadan" and not rows[0]["computed"]


def test_hb_rows_mismatch_gives_no_proposal():
    rows = rows_for([building("Hojatxona", "251.37", count="21")], [building("Hojatxona binosi", "10.2")])
    assert rows[0]["volume"] is None and rows[0]["check"]["status"] == verify.MISMATCH
    hb = volumes.hb_volume(rows)
    assert hb["proposal"] is None and not hb["complete"] and "Hojatxona" in hb["notes"][-1]
    # staff resolves it with a reason
    fixed = rows_for([building("Hojatxona", "251.37", count="21")], [building("Hojatxona binosi", "10.2")],
                     {rows[0]["id"]: {"volume": "251.37", "reason": "loyiha bo'yicha"}})
    assert volumes.hb_volume(fixed)["proposal"] == "251.37"


def test_hb_rows_conclusion_only_and_reference():
    conclusion = [building("Blok №3 – Ayvon", "113500.0"), building("Blok №5", "8306.6", count="2"),
                  building("Blok №6", reference="Blok №3 – Ayvon bilan bir xil")]
    rows = rows_for([], conclusion)
    assert rows[0]["volume"] == "113500.0" and "loyiha berilmagan" in rows[0]["formula"]
    assert rows[1]["volume"] == "16613.2" and rows[1]["computed"] and "soni × birlik" in rows[1]["formula"]
    assert rows[2]["volume"] is None and "havola" in rows[2]["formula"]   # never copied from the other block
    hb = volumes.hb_volume(rows)
    assert hb["proposal"] is None and not hb["complete"]
    # staff enters the referenced block's volume, or leaves the block out
    done = rows_for([], conclusion, {rows[2]["id"]: {"volume": "113500", "reason": "Blok №3 bilan bir xil"}})
    assert volumes.hb_volume(done)["proposal"] == str(D("113500.0") + D("16613.2") + D("113500"))
    skipped = rows_for([], conclusion, {rows[2]["id"]: {"included": False, "reason": "2-etap"}})
    assert volumes.hb_volume(skipped)["proposal"] == "130113.2"


def test_hb_rows_equal_values_and_no_buildings():
    rows = rows_for([building("Дом Тип-2", "36036")], [building("2-tip", "36036.0")])
    assert rows[0]["volume"] == "36036" and rows[0]["check"]["status"] == verify.OK
    assert volumes.hb_volume(rows)["proposal"] == "36036"
    assert volumes.hb_volume([])["proposal"] is None


def test_industrial_indicator():
    rows = rows_for([], [building("Ayvon", "21528.0", footprint="1794.0", floors="1"),
                         building("Ofis", "1507.5", footprint="251.25", floors="2")])
    items, total = volumes.industrial_indicator(rows)
    assert items[0]["within_3m"] == "5382.0" and items[0]["excess"] == "16146.0"
    assert items[1]["within_3m"] == "1507.50" and D(items[1]["excess"]) == 0
    assert total == D("16146.0")


def test_floor_lists_and_mansard_levels():
    assert volumes.parse_level("1,2,3 qavatlarda") == Level("range", 1, 3)
    assert volumes.parse_level("mansarda qavati") == Level("technical")


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


@pytest.mark.parametrize("text,expected", [
    ("yerto‘la qavati", [Level("basement")]),
    ("Yerto`la qavati va qavatlarning poldan shiftgacha bo‘lgan ichki balandligi", [Level("basement"), Level("generic")]),
    ("1-çi va 2-qavatlar içki xonalarining balandligi", [Level("range", 1, 2)]),
    ("Poydevor balandligi", [Level("foundation")]),
    ("xonalar balandligi poldan shiftgacha", [Level("generic")]),
    ("1-5qavat", [Level("range", 1, 5)]),
    ("План 2-16 го этажа на отм. +3.710м", [Level("range", 2, 16)]),
    ("ichki qismi balandligi poldan metal ferma ostigacha", [Level("unknown")]),
])
def test_parse_levels_real_wordings(text, expected):
    assert volumes.parse_levels(text) == expected


def test_one_statement_can_give_two_heights():
    hs = volumes.collect_heights([], [val("3.00", level="Yerto`la qavati va qavatlarning ichki balandligi")])
    assert volumes.resolve_height(Level("basement"), hs)[0] == D("3.00")
    assert volumes.resolve_height(Level("floor", 2), hs)[0] == D("3.00")
    # the foundation height is never used as a storey height
    hs = volumes.collect_heights([], [val("0.45", level="Poydevor balandligi")])
    assert volumes.resolve_height(Level("floor", 1), hs)[0] is None


def test_underground_floor_is_a_basement_level():
    from feecalc import volumes
    assert volumes.parse_level("План подземного этажа на отм. -3.200м").kind == "basement"
    assert volumes.parse_level("yer osti qavati").kind == "basement"
