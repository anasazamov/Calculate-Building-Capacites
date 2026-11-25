"""Recommendation rules (pure code): where the object is, the coefficients, exemptions, the volumes."""
from decimal import Decimal

from feecalc import decide

ACCEPTED = {"status": "accepted", "file": "x.pdf", "page": 1, "source_text": "manba"}


def value(text: str) -> dict:
    return {**ACCEPTED, "value": text}


def number(n: str) -> dict:
    return {**ACCEPTED, "value": n, "number": n}


def test_locate_reads_settlement_and_street():
    p = decide.locate("Samarqand viloyat Samarqand shahar Maysazor mahallasi Maysazor ko‘chasi 11-uyda joylashgan")
    assert (p.settlement, p.kind, p.street, p.in_region) == ("samarqand shahri", "city", "maysazor", True)
    p = decide.locate('Samarqand shahar "Xo‘ja Axrori Vali" MFY "Xo‘ja Axrori Vali" ko‘chasi 125-uy hududida')
    assert (p.settlement, p.street) == ("samarqand shahri", "xoja axrori vali")
    p = decide.locate("Samarqand viloyati Payariq tumani, Tomoyrot MFY, Iforli ko`chasi 36-manzilda")
    assert (p.settlement, p.kind, p.street) == ("payariq tumani", "district", "iforli")
    p = decide.locate("Samarqand viloyati Nurobod tumani “Jarquduq” MFY hududidan")
    assert (p.settlement, p.street) == ("nurobod tumani", None)
    # the district of the same name is not the city
    assert decide.locate("Samarqand viloyati Samarqand tumani Gulobod qishlog'i").settlement == "samarqand tumani"
    p = decide.locate("дом №11, ул. Майсазор в махале Майсазор, город Самарканд")
    assert (p.settlement, p.street) == ("samarqand shahri", "maysazor")
    assert decide.locate("manzil ko'rsatilmagan").settlement is None


def test_street_names_are_compared_without_the_word_for_street():
    assert decide.same_street("Mirzo Ulug'bek ko'chasi", "мирзо улугбек") and decide.same_street("Rudakiy", "Rudakiy ko`chasi")
    assert not decide.same_street("Rudakiy", "Registon")


def test_construction_type_comes_from_the_conclusion():
    assert decide.decide_kt(value("Yangi qurilish"))["code"] == "new"
    assert decide.decide_kt(value("Rekonstruksiya qilish"))["code"] == "reconstruction"
    assert decide.decide_kt(value("Реновация"))["code"] == "renovation"
    undecided = decide.decide_kt(value("Kapital ta'mirlash"))
    assert undecided["code"] is None and "to'g'ri kelmadi" in undecided["basis"]
    assert decide.decide_kt({"status": "unverified", "value": "Yangi qurilish"})["code"] is None
    assert decide.decide_kt(None)["code"] is None


def test_object_type_from_the_description():
    assert decide.decide_ko(value("MCHJga qurilish sanoati korxonalari binosini qurish"))["code"] == "industrial"
    social = decide.decide_ko(value("Birinchi qavatda tibbiy markaz va hususiy maktab binosi"))
    assert social["code"] == "social" and not social["assumed"]
    other = decide.decide_ko(value("savdo va maishiy xizmat ko`rsatish binosi qurilishi"))
    assert other["code"] == "other" and "Davlat ulushi" in other["basis"]
    mixed = decide.decide_ko(value("tibbiy markaz va omborxona"))
    assert mixed["code"] == "social" and mixed["assumed"]
    assert decide.decide_ko(None)["code"] is None


def test_zone_and_location_use_the_reference_data():
    city = decide.locate("Samarqand viloyat Samarqand shahar Maysazor ko'chasi 11")
    village = decide.locate("Samarqand viloyati Payariq tumani, Iforli ko'chasi 36")
    empty = decide.Reference()
    assert decide.decide_kz(city, empty)["code"] == "500k-1m" and decide.decide_kz(village, empty)["code"] == "under100k"
    assert decide.decide_kz(decide.locate("г. Наманган, ул. Темир йул"), empty)["code"] is None      # outside the region
    assert decide.decide_kj(city, empty)["code"] is None                                             # no list entered
    ref = decide.Reference(zones={"payariq tumani": ("100k-250k", "sinov")},
                           central_streets={"samarqand shahri": [("maysazor", "Qaror №5"), ("rudakiy", "")]})
    assert decide.decide_kz(village, ref)["code"] == "100k-250k"
    central = decide.decide_kj(city, ref)
    assert central["code"] == "central" and "Qaror №5" in central["basis"]
    assert decide.decide_kj(decide.locate("Samarqand shahar Registon ko'chasi 1"), ref)["code"] == "other"
    no_street = decide.decide_kj(decide.locate("Samarqand shahar, Sartepo MFY hududida"), ref)
    assert no_street["code"] == "other" and no_street["assumed"]
    assert decide.decide_kj(village, ref)["code"] is None                                            # no list for that district


def test_possible_exemptions_are_flagged_not_applied():
    flags = decide.exemptions(value("yer maydonidan, yakka tartibdagi turar-joy (kottedj) qurish"))
    assert len(flags) == 1 and "yakka tartibdagi" in flags[0]["text"] and flags[0]["page"] == 1
    assert decide.exemptions(value("savdo binosi")) == [] and decide.exemptions(None) == []
    assert any("ta'lim" in f["text"] for f in decide.exemptions(value("tibbiy markaz va hususiy maktab binosi")))


def hb_row(name, volume, footprint=None, floors="1", count="1", project=None, conclusion=None, reference=None, computed=False):
    return {"name": name, "included": True, "volume": volume, "count": count, "computed": computed,
            "project": project, "conclusion": conclusion, "reference": reference,
            "footprint": number(footprint) if footprint else None, "floors": number(floors)}


def view_with(rows, strict):
    return {"hb_rows": rows, "volumes": {"Hb": {"proposal": strict, "formula": "strict"}}}


def test_hb_keeps_the_strict_proposal_when_there_is_one():
    out = decide.decide_hb(view_with([hb_row("A", "100")], "100"), industrial=False)
    assert out == {"value": "100", "formula": "strict", "assumptions": [], "strict": True}


def test_hb_prefers_the_conclusion_when_documents_disagree():
    rows = [hb_row("Uy", "1000"), hb_row("Hojatxona", None, project=number("251.37"), conclusion=number("10.2"))]
    out = decide.decide_hb(view_with(rows, None), industrial=False)
    assert out["value"] == "1010.2" and not out["strict"]
    assert "xulosadagi qiymat olindi" in out["assumptions"][0] and "11-band" in out["assumptions"][0]


def test_hb_of_an_industrial_object_counts_three_metres_per_storey():
    rows = [hb_row("Ma'muriyat", "1507.5", footprint="335"), hb_row("Ayvon", "113500", footprint="13350"),
            hb_row("Past bino", "500", footprint="200"),                                   # lower than 3 m: full volume
            hb_row("Blok 6", None, reference={"status": "accepted", "value": "Blok 3 bilan bir xil"})]
    out = decide.decide_hb(view_with(rows, None), industrial=True)
    assert Decimal(out["value"]) == Decimal("1005") + Decimal("40050") + Decimal("500")
    assert sum("3 m gacha" in a for a in out["assumptions"]) == 2
    assert any(a.startswith("DIQQAT") and "Blok 6" in a and "havola" in a for a in out["assumptions"])


def test_hb_without_any_building_stays_open():
    assert decide.decide_hb(view_with([], None), industrial=False)["value"] is None


def test_hyu_is_zero_and_says_so_when_permitted_floors_are_unknown():
    view = {"volumes": {"Hyu": {"proposal": None, "formula": "", "notes": ["ruxsat etilgan qavatlar sonini xodim kiritadi"]}}}
    out = decide.decide_hyu(view, None)
    assert out["value"] == "0" and "Ruxsat etilgan qavatlar soni" in out["assumptions"][0]
    assert decide.decide_hyu(view, 9)["value"] is None                    # floors entered but the volume cannot be computed
    strict = {"volumes": {"Hyu": {"proposal": "120.5", "formula": "f", "notes": []}}}
    assert decide.decide_hyu(strict, 9) == {"value": "120.5", "formula": "f", "assumptions": [], "strict": True}


def test_deductions_sum_what_is_known_and_name_the_rest():
    row = {"level_kind": "floor", "origin": "room", "area": "10", "category_method": "dictionary"}
    components = [{**row, "category": "Ht", "included": True, "volume": "168.32", "name": "Nasosxona"},
                  {**row, "category": "Ht", "included": True, "volume": None, "name": "Texnik xona"},
                  {**row, "category": "Hu", "included": True, "volume": "50", "name": "Zinapoya"}]
    view = {"components": components, "heights": [],
            "volumes": {"Ht": {"proposal": None, "formula": "to'liq emas", "notes": []},
                        "Hu": {"proposal": "50", "formula": "50", "notes": []}}}
    ht = decide.decide_category(view, "Ht", residential=True)
    assert ht["value"] == "168.32" and "Texnik xona" in ht["assumptions"][0] and "yig'im kamaymaydi" in ht["assumptions"][0]
    assert decide.decide_category(view, "Hu", residential=True)["value"] == "50"
    not_residential = decide.decide_category(view, "Hu", residential=False)
    assert not_residential["value"] == "0" and "turar joy" in not_residential["assumptions"][0]


HEIGHTS = [{"level": "yerto‘la qavati", "kind": "basement", "value": "3.20", "origin": "xulosa"},
           {"level": "1-qavat", "kind": "floor", "value": "3.71", "origin": "xulosa"},
           {"level": "2-16-qavat", "kind": "range", "value": "3.25", "origin": "xulosa"}]


def test_typical_height_is_the_one_most_storeys_share():
    assert decide.typical_height(HEIGHTS) == (Decimal("3.25"), "2-16-qavat")
    assert decide.typical_height(HEIGHTS[:2]) is None                      # one storey is not "most storeys"
    even = [{"level": "1-qavat", "value": "3.3"}, {"level": "2-qavat", "value": "3.0"}]
    assert decide.typical_height(even) is None and decide.typical_height([]) is None


def test_common_area_given_for_the_whole_building_uses_the_typical_height():
    whole = {"category": "Hu", "included": True, "volume": None, "name": "Umumiy maydon (zinapoya, lift xolli)",
             "level_kind": "unknown", "origin": "breakdown", "area": "1026.4", "category_method": "dictionary:mixed"}
    roof = {"category": "Hu", "included": True, "volume": None, "name": "Lift tuguni +52.420", "level_kind": "technical",
            "origin": "breakdown", "area": "35.8", "category_method": "dictionary"}
    view = {"components": [whole, roof], "heights": HEIGHTS,
            "volumes": {"Hu": {"proposal": None, "formula": "to'liq emas", "notes": []},
                        "Ht": {"proposal": None, "formula": "to'liq emas", "notes": []}}}
    hu = decide.decide_category(view, "Hu", residential=True)
    assert hu["value"] == "3335.8"                                          # the value of TASK.md's control example
    assert "1026.4 m² × 3.25 m" in hu["assumptions"][0] and "Lift tuguni" in hu["assumptions"][1]
    # the same gap in a technical row is NOT closed: technical rooms sit in basements and on roofs
    technical = {**whole, "category": "Ht"}
    ht = decide.decide_category({**view, "components": [technical]}, "Ht", residential=True)
    assert ht["value"] == "0" and "qo'shilmadi" in ht["assumptions"][0]
