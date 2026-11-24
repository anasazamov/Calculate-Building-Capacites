from decimal import Decimal as D

import pytest

from feecalc.fee import FeeError, FeeInput, compute, industrial_3m_indicator, option_value

CONTROL_CASES = [
    # bhm, hb, ha, ht, hu, kt, ko, kz, kj, fee, 20%, 80%
    ("440000", "1000", "0", "0", "0", "1", "1", "0.5", "1", "220000000", "44000000", "176000000"),
    ("412000", "12000", "800", "300", "0", "1", "1", "0.5", "1", "2245400000", "449080000", "1796320000"),
    ("440000", "1000", "0", "0", "0", "0.5", "0.5", "0.05", "0.6", "4400000", "880000", "3520000"),
    ("440000", "36036", "0", "168.32", "3335.8", "1", "1", "0.5", "0.6", "4294208160", "858841632", "3435366528"),
]


@pytest.mark.parametrize("bhm,hb,ha,ht,hu,kt,ko,kz,kj,fee,p20,p80", CONTROL_CASES)
def test_control_examples(bhm, hb, ha, ht, hu, kt, ko, kz, kj, fee, p20, p80):
    r = compute(FeeInput(D(bhm), D(hb), D("0"), D(ha), D(ht), D(hu), D(kt), D(ko), D(kz), D(kj)))
    assert r.fee == D(fee)
    assert r.advance_20 == D(p20)
    assert r.remainder_80 == D(p80)
    assert r.advance_20 + r.remainder_80 == r.fee


def test_floor_applied():
    r = compute(FeeInput(D("440000"), D("1000"), D("0"), D("0"), D("0"), D("0"), D("0.5"), D("0.5"), D("0.05"), D("0.6")))
    assert r.multiplier_raw == D("0.0075")
    assert r.multiplier == D("0.01")
    assert r.floor_applied
    assert "0,01 olindi" in r.formula


def test_floor_not_applied():
    r = compute(FeeInput(D("440000"), D("1000"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("0.5"), D("1")))
    assert not r.floor_applied


def test_negative_volume_is_error():
    with pytest.raises(FeeError, match="manfiy"):
        compute(FeeInput(D("440000"), D("100"), D("0"), D("200"), D("0"), D("0"), D("1"), D("1"), D("0.5"), D("1")))


def test_hyu_is_added():
    r = compute(FeeInput(D("440000"), D("1000"), D("100"), D("0"), D("0"), D("0"), D("1"), D("1"), D("0.5"), D("1")))
    assert r.chargeable_volume == D("1100")
    assert r.fee == D("242000000")


def test_council_coefficient():
    r = compute(FeeInput(D("440000"), D("1000"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("0.5"), D("1"), D("1.5")))
    assert r.fee == D("330000000")
    with pytest.raises(FeeError):
        compute(FeeInput(D("440000"), D("1000"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("0.5"), D("1"), D("1.6")))


def test_round_half_up():
    # exact half: bhm=1, volume=0.5, multiplier=1 -> 0.5 -> 1
    r = compute(FeeInput(D("1"), D("0.5"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("1"), D("1")))
    assert r.fee == D("1")
    # advance rounding: fee 3 -> 0.6 -> 1; remainder 2
    r = compute(FeeInput(D("1"), D("3"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("1"), D("1")))
    assert (r.advance_20, r.remainder_80) == (D("1"), D("2"))


@pytest.mark.parametrize("field,value", [("kt", "0.7"), ("ko", "0.9"), ("kz", "0.3"), ("kz", "2.5"), ("kj", "0.5")])
def test_invalid_coefficient(field, value):
    base = dict(bhm=D("440000"), hb=D("1000"), hyu=D("0"), ha=D("0"), ht=D("0"), hu=D("0"),
                kt=D("1"), ko=D("1"), kz=D("0.5"), kj=D("1"))
    base[field] = D(value)
    with pytest.raises(FeeError):
        compute(FeeInput(**base))


def test_float_rejected():
    with pytest.raises(FeeError, match="Decimal"):
        compute(FeeInput(440000.0, D("1000"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("0.5"), D("1")))  # type: ignore[arg-type]


def test_kz_over_1m_entered_by_staff():
    assert option_value("kz", "1m+", D("1.5")) == D("1.5")
    with pytest.raises(FeeError):
        option_value("kz", "1m+", None)
    with pytest.raises(FeeError):
        option_value("kz", "1m+", D("2.1"))
    assert option_value("kz", "500k-1m") == D("0.5")
    r = compute(FeeInput(D("440000"), D("1000"), D("0"), D("0"), D("0"), D("0"), D("1"), D("1"), D("1.5"), D("1")))
    assert r.fee == D("660000000")


def test_industrial_3m():
    floors, total = industrial_3m_indicator([("1-qavat", D("1000"), D("4.5")), ("2-qavat", D("800"), D("3.0"))])
    assert floors[0].excess_volume == D("1500.0")
    assert floors[1].excess_volume == D("0")
    assert total == D("1500.0")
