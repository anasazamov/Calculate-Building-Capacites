"""Building-code checks: every rule cites a clause of the knowledge base and runs only against its current text."""
from pathlib import Path

from feecalc import norms
from feecalc.knowledge import Clause, Document, KnowledgeBase

ACCEPTED = {"status": "accepted", "file": "x.pdf", "page": 2, "source_text": "manba"}


def number(n: str, **extra) -> dict:
    return {**ACCEPTED, "value": n, "number": n, **extra}


def base(tmp_path: Path, residential_text: str = "38. Yashash xonalari balandligi poldan shiftgacha kamida 2,7 m boʻlishi lozim.") -> KnowledgeBase:
    kb = KnowledgeBase(tmp_path / "kb.db")
    kb.add_document(Document("shnq-2.08.01-24", "Turar joy obyektlari", "shnq", "https://lex.uz/docs/-7121730", "uz-Latn",
                             designation="SHNQ 2.08.01-24"),
                    [Clause("38-band", (), residential_text),
                     Clause("1-ilova, 9-band", (), "9. Qurilish hajmi ±0,00 belgidan yuqori va quyi qism hajmlari yigʻindisi."),
                     Clause("1-ilova, 11-band", (), "11. Yuqori qavat ustidagi texnik qavat qavatlar soniga kirmaydi.")])
    kb.add_document(Document("shnq-2.08.02-24", "Jamoat binolari", "shnq", "https://lex.uz/docs/-8563557", "uz-Latn",
                             designation="SHNQ 2.08.02-24"),
                    [Clause("12-band", (), "12. Xonalarning balandligi 3,0 m dan kam boʻlmasligi zarur.")])
    return kb


def building(title: str, heights: list[dict], floors: str = "5", fields: list[dict] | None = None) -> dict:
    return {"title": title, "heights": heights,
            "fields": [{"field": "floors", "project": None, "conclusion": number(floors)}] + (fields or [])}


def view(*buildings: dict) -> dict:
    return {"buildings": list(buildings), "site": {"project": [], "conclusion": []}}


def test_building_kind():
    assert norms.building_kind("Ko'p xonadonli turar joy binosi", "") == "residential"
    assert norms.building_kind("Savdo binosi: Noturar binosi (Magazin)", "turar joy majmuasi") == "public"
    assert norms.building_kind("Blok №3 – Ayvon", "qurilish sanoati korxonasi") == "industrial"
    assert norms.building_kind("Ma'muriyat binosi", "") == "public"


def test_height_below_the_norm_is_a_mismatch_with_the_clause_quoted(tmp_path: Path):
    low = building("Turar joy binosi", [number("2.5", level="2-9-qavat"), number("3.2", level="yerto‘la qavati")])
    found = norms.check(view(low), None, base(tmp_path))
    height = [f for f in found if f["code"] == "b1_height"][0]
    assert height["status"] == "mismatch" and height["label"] == "mos emas" and "2.5 m" in height["detail"]
    assert height["norm"] == "SHNQ 2.08.01-24, 38-band" and "kamida 2,7 m" in height["quote"] and height["page"] == 2
    assert found[0] is height                                                    # the serious finding comes first


def test_storey_height_near_the_norm_is_a_warning_unless_it_is_a_ceiling_height(tmp_path: Path):
    kb = base(tmp_path)
    near = norms.check(view(building("Turar joy binosi", [number("2.8", level="1-5-qavat")])), None, kb)
    assert [f["status"] for f in near if f["code"] == "b1_height"] == ["warning"]
    ceiling = norms.check(view(building("Turar joy binosi", [number("2.8", level="xonalar balandligi poldan shiftgacha")])), None, kb)
    assert [f["status"] for f in ceiling if f["code"] == "b1_height"] == ["ok"]
    high = norms.check(view(building("Savdo binosi", [number("3.6", level="1-3-qavat")])), None, kb)
    assert [(f["status"], f["norm"]) for f in high] == [("ok", "SHNQ 2.08.02-24, 12-band")]


def test_rule_switches_itself_off_when_the_clause_no_longer_has_its_figure(tmp_path: Path):
    kb = base(tmp_path, residential_text="38. Yashash xonalari balandligi kamida 2,8 m boʻlishi lozim.")
    found = norms.check(view(building("Turar joy binosi", [number("2.5", level="1-qavat")])), None, kb)
    height = [f for f in found if f["code"] == "b1_height"][0]
    assert height["status"] == "not_run" and "«2,7» topilmadi" in height["detail"]


def test_more_than_sixteen_storeys_is_outside_the_residential_code(tmp_path: Path):
    found = norms.check(view(building("Turar joy binosi", [], floors="20")), None, base(tmp_path))
    assert [(f["code"], f["status"]) for f in found if f["code"].startswith("b1")] == [("b1_storeys", "warning")]


def test_volume_is_above_plus_below_ground(tmp_path: Path):
    parts = [{"field": "volume", "project": number("36036"), "conclusion": None},
             {"field": "volume_above_ground", "project": number("34619.3"), "conclusion": None},
             {"field": "volume_below_ground", "project": number("1416.7"), "conclusion": None}]
    found = norms.check(view(building("Turar joy binosi", [], floors="16", fields=parts)), None, base(tmp_path))
    volume = [f for f in found if f["code"] == "b1_volume_parts"][0]
    assert volume["status"] == "ok" and "36036" in volume["detail"] and "1-ilova, 9-band" in volume["norm"]


def test_small_service_structures_and_an_empty_base_give_nothing(tmp_path: Path):
    toilet = building("Tashqi hojatxona binosi", [number("2.6", level="bino balandligi")], floors="1")
    assert norms.check(view(toilet), None, base(tmp_path)) == []
    assert norms.check(view(building("Turar joy binosi", [number("2.5", level="1-qavat")])), None,
                       KnowledgeBase(tmp_path / "empty.db")) == []
