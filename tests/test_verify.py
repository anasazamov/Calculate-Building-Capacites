from decimal import Decimal as D

import pytest

from feecalc import verify as v


@pytest.mark.parametrize("text,expected", [
    ("36036", D("36036")), ("36 036,5", D("36036.5")), ("9410.0", D("9410.0")), ("+52.420", D("52.420")),
    ("-3.200", D("-3.200")), ("−3.200", D("-3.200")), ("±0.000", D("0.000")), ("600,3", D("600.3")),
    (">=10274.2", None), ("> 10411.3", None), ("36036 м3", None), ("", None), ("value 10000.0", None),
    ("2 16", None), ("1.2.3", None),
])
def test_parse_number(text, expected):
    assert v.parse_number(text) == expected


def test_numbers_in_text():
    nums = [q for q, _ in v.numbers_in_text("Строительный объём | м3 | 36 036,5 в т.ч. 2–16 этажи 7439.3")]
    assert D("36036.5") in nums and D("7439.3") in nums and D("16") in nums
    assert (D("3.200"), True) in v.numbers_in_text("подвал на отм. −3.200")


PAGE = ["Площадь застройки\nм2\n600.3\nСтроительный объём\nм3\n36036\nподвал на отм. −3.200 465.9"]


def test_source_accepted():
    assert v.verify_source("36036", "Строительный объём | м3 | 36036", PAGE, True) == (v.ACCEPTED, "", D("36036"))
    st, _, num = v.verify_source("-3.200", "подвал на отм. -3.200", PAGE, True)
    assert (st, num) == (v.ACCEPTED, D("-3.200"))


def test_source_invented_snippet():
    st, reason, num = v.verify_source("36036", "Строительный объём здания 36036 м3", PAGE, True)
    assert st == v.UNVERIFIED and "so'zma-so'z" in reason and num is None


def test_source_number_not_in_snippet():
    st, reason, _ = v.verify_source("36063", "Строительный объём м3 36036", PAGE, True)
    assert st == v.UNVERIFIED and "ajralmadi" in reason
    st, reason, _ = v.verify_source(">=36036", "Строительный объём м3 36036", PAGE, True)
    assert st == v.UNVERIFIED and "son emas" in reason
    # a negative value is rejected when the snippet has no minus sign
    assert v.verify_source("-465.9", "465.9", PAGE, True)[0] == v.UNVERIFIED
    # matching a part of a number is not enough
    assert v.verify_source("6036", "Строительный объём м3 36036", PAGE, True)[0] == v.UNVERIFIED


def test_source_not_found_and_textual():
    assert v.verify_source(None, None, PAGE, True)[0] == v.NOT_FOUND
    assert v.verify_source("36036", None, PAGE, True)[0] == v.UNVERIFIED
    texts = ["Манзил: г. Наманган, ул. Темир йул"]
    assert v.verify_source("г. Наманган, ул. Темир йул", "Манзил: г. Наманган, ул. Темир йул", texts, False)[0] == v.ACCEPTED
    assert v.verify_source("г. Самарканд", "Манзил: г. Наманган, ул. Темир йул", texts, False)[0] == v.UNVERIFIED


def test_sum():
    parts = [D("465.9"), D("442.6"), D("7439.3"), D("1026.4"), D("35.8")]
    assert v.check_sum("k", "n", parts, D("9410.0")).status == v.OK
    assert v.check_sum("k", "n", [D("34619.3"), D("1416.7")], D("36036")).status == v.OK
    assert v.check_sum("k", "n", [D("34619.3"), D("1416.7")], D("36136")).status == v.MISMATCH
    assert v.check_sum("k", "n", [D("34619.3"), None], D("36036")).status == v.NOT_RUN


def test_total_row_truncated():
    # 124432 instead of 124433.19: each addend truncated to an integer
    t = v.check_sum("k", "n", [D("88396.60"), D("36036.59")], D("124432"))
    assert t.status == v.ROUNDING and "yaxlitlash farqi" in t.detail
    assert v.check_sum("k", "n", [D("88396.60"), D("36036.59")], D("124430")).status == v.MISMATCH


def test_equal():
    assert v.check_equal("k", "n", D("36036"), D("36036.0")).status == v.OK
    assert v.check_equal("k", "n", D("36036"), D("36036.4")).status == v.ROUNDING
    assert v.check_equal("k", "n", D("36036.0"), D("36037.0")).status == v.MISMATCH
    assert v.check_equal("k", "n", D("36036"), D("36136.4")).status == v.MISMATCH
    assert v.check_equal("k", "n", None, D("1")).status == v.NOT_RUN


def test_volume_bounds():
    assert v.check_volume_lower_bound(D("36036"), D("600.3"), [D("3.2")] + [D("3.0")] * 16).status == v.OK
    assert v.check_volume_lower_bound(D("20000"), D("600.3"), [D("3.0")] * 16).status == v.WARNING
    assert v.check_volume_lower_bound(D("20000"), D("600.3"), []).status == v.NOT_RUN
    assert v.check_deductions_bound(D("0"), D("168.32"), D("3335.8"), D("36036")).status == v.OK
    assert v.check_deductions_bound(D("200"), D("0"), D("0"), D("100")).status == v.MISMATCH
    assert v.check_deductions_bound(None, D("0"), D("0"), D("100")).status == v.NOT_RUN


def test_address():
    t = v.check_address("г. Наманган, ул. Темир йул", "Samarqand shahri, Maysazor ko'chasi")
    assert t.status == v.MISMATCH
    assert v.check_address("г. Самарканд, ул. Майсазор", "Samarqand shahri, Maysazor ko'chasi").status == v.OK
    assert v.check_address(None, "Samarqand").status == v.NOT_RUN


def test_unit():
    assert v.normalize_unit("м3") == "m3" and v.normalize_unit("м²") == "m2" and v.normalize_unit("куб.м") == "m3"
    assert v.unit_flag("m3", "m2") is not None and "xato" in v.unit_flag("m3", "m2")
    assert v.unit_flag("m3", "м3") is None


def test_model_agreement():
    a = {"status": "accepted", "number": "36036", "value": "36036"}
    assert v.model_agreement(a, {"status": "accepted", "number": "36036.0", "value": "36036.0"}) == "match"
    assert v.model_agreement(a, {"status": "accepted", "number": "36063", "value": "36063"}) == "mismatch"
    assert v.model_agreement(a, {"status": "unverified"}) == "only_1"
    assert v.model_agreement(None, None) == "neither"
