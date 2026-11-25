"""Contract documents: numbers in words, the fields, the built-in annex."""
import io

from docx import Document

from feecalc import contract


def test_numbers_in_uzbek_words():
    assert contract.in_words(0) == "nol" and contract.in_words(21) == "yigirma bir" and contract.in_words(100) == "bir yuz"
    assert contract.in_words(1000) == "bir ming" and contract.in_words(1001010) == "bir million bir ming o'n"
    assert contract.in_words(293832000) == "ikki yuz to'qson uch million sakkiz yuz o'ttiz ikki ming"
    assert contract.in_words(4294208160) == ("to'rt milliard ikki yuz to'qson to'rt million ikki yuz sakkiz ming "
                                             "bir yuz oltmish")


def test_money_groups_thousands():
    assert contract.money("4294208160") == "4 294 208 160" and contract.money("36036.5") == "36 036,5"


SHEET = {
    "job": {"id": 7, "title": "Sinov obyekti"}, "legal_basis": "Vazirlar Mahkamasining 151-son qarori (08.04.2026), 4-ilova",
    "object": {"value": "Samarqand shahar Maysazor ko'chasi 11-uyda ko'p xonadonli turar joy binosi"},
    "contract": {"number": "ART-11-26-90", "date": "2026-11-25"},
    "scalars": [{"label": "Manzil", "project": None, "conclusion": {"value": "Samarqand shahar, Maysazor ko'chasi 11"}},
                {"label": "Buyurtmachi", "project": None, "conclusion": {"value": "«Namuna» MCHJ"}},
                {"label": "Xulosa raqami", "project": None, "conclusion": {"value": "284459"}},
                {"label": "Xulosa sanasi", "project": None, "conclusion": None}],
    "volumes": {code: {"decision": {"value": value}} for code, value in
                (("Hb", "36036"), ("Hyu", "0"), ("Ha", "0"), ("Ht", "168.32"), ("Hu", "3335.8"))},
    "fee": {"options": {"kt": "Yangi qurilish — 1", "ko": "Boshqa obyektlar — 1", "kz": "Samarqand shahri — 0,5",
                        "kj": "Boshqa hududlar — 0,6"},
            "coefficients": {"kt": "1", "ko": "1", "kz": "0.5", "kj": "0.6", "k_council": "1", "bhm": "440000"},
            "result": {"chargeable_volume": "32531.88", "multiplier": "0.3", "formula": "440000 × 32531.88 × 0.3 = 4294208160 so'm",
                       "fee": "4294208160", "advance_20": "858841632", "remainder_80": "3435366528"}},
    "finalized": {"by": "Sinov Xodim", "at": "2026-11-25 10:00:00"},
}


def test_fields_cover_every_documented_placeholder():
    fields = contract.fields(SHEET)
    assert set(fields) == set(contract.PLACEHOLDERS)
    assert fields["yigim"] == "4 294 208 160" and fields["yigim_soz"].startswith("to'rt milliard")
    assert fields["oldindan_20"] == "858 841 632" and fields["ht"] == "168,32" and fields["kz"] == "0,5"
    assert fields["xulosa_sanasi"] == contract.BLANK and fields["shartnoma_raqami"] == "ART-11-26-90"


def test_annex_is_a_word_document_with_the_figures():
    doc = Document(io.BytesIO(contract.annex(SHEET)))
    text = "\n".join([p.text for p in doc.paragraphs]
                      + [cell.text for table in doc.tables for row in table.rows for cell in row.cells])
    for part in ("HISOBLASH MA'LUMOTNOMASI", "ART-11-26-90", "«Namuna» MCHJ", "36 036", "3 335,8", "4 294 208 160 so'm",
                 "to'rt milliard", "858 841 632", "3 435 366 528", "440 000", "Sinov Xodim"):
        assert part in text, part


def test_fields_are_stamped_onto_the_pages_of_a_pdf_template(tmp_path):
    import pymupdf
    template = tmp_path / "template.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((60, 80), "SHARTNOMA  ESKI-RAQAM", fontsize=12)
    doc.new_page()
    doc.save(str(template))
    layout = contract.clean_layout([
        {"field": "shartnoma_raqami", "page": 1, "x": 130, "y": 68, "w": 160, "h": 16, "size": 12, "bold": True},
        {"field": "yigim", "page": 2, "x": 60, "y": 100, "w": 200, "h": 16, "align": "right", "cover": False},
        {"field": "obyekt", "page": 2, "x": 60, "y": 140, "w": 120, "h": 12}], pages=2)       # long text in a small box: shrunk
    assert layout[0]["cover"] and layout[1]["align"] == "right" and layout[1]["size"] == 11
    out = pymupdf.open(stream=contract.stamp(template, layout, contract.fields(SHEET)), filetype="pdf")
    assert "ART-11-26-90" in out[0].get_text() and "4 294 208 160" in out[1].get_text()
    assert "Maysazor" in out[1].get_text()                                                 # nothing was cut off
    assert contract.layout_problems([{"field": "summa", "page": 1, "x": 0, "y": 0, "w": 50, "h": 10}], 2) == "noma'lum maydon"
    assert contract.layout_problems([{"field": "yigim", "page": 5, "x": 0, "y": 0, "w": 50, "h": 10}], 2) != ""
    assert contract.layout_problems(layout, 2) == "" and set(contract.sample_values()) == set(contract.PLACEHOLDERS)
