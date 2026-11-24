"""Fee core: infrastructure fee per Cabinet of Ministers Resolution No. 151 (08.04.2026), Annex 4.

    Ti = BHM x ((Hb + Hyu) - (Ha + Ht + Hu)) x Kt x Ko x Kz x Kj x Kcouncil

BHM is the "base calculation amount" (bazaviy hisoblash miqdori), in UZS.
Decimal only. The language model never feeds this module: coefficients are chosen by staff.
User-facing strings (labels, error messages) are in Uzbek because the UI is Uzbek.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

D = Decimal

BHM_DEFAULT = D("440000")  # from 1 September 2026 (per TASK.md)
MULTIPLIER_FLOOR = D("0.01")
ADVANCE_SHARE = D("0.2")
INDUSTRIAL_HEIGHT_LIMIT = D("3")
COUNCIL_RANGE: tuple[Decimal, Decimal] = (D("0.01"), D("1.5"))


class FeeError(ValueError):
    """An input value that makes the calculation impossible."""


@dataclass(frozen=True)
class Option:
    code: str
    value: Decimal | None  # None: staff enters a value from `range`
    label: str
    text: str              # wording of the resolution (from the reference sheet)
    range: tuple[Decimal, Decimal] | None = None


KT_OPTIONS: tuple[Option, ...] = (
    Option("new", D("1"), "Yangi qurilish", "Yangi qurilish (rivojlanish) — 1"),
    Option("renovation", D("0.5"), "Renovatsiya", "Renovatsiya — 0,5"),
    Option("reconstruction", D("0.85"), "Rekonstruksiya", "Obyektni rekonstruksiya qilish — 0,85"),
)
KO_OPTIONS: tuple[Option, ...] = (
    Option("social", D("0.8"), "Ijtimoiy xususiy obyekt",
           "Ijtimoiy xususiy obyektlar (o'quv markazlari, xususiy tibbiyot muassasalari va b.) — 0,8"),
    Option("state50", D("0.5"), "Davlat ulushi 50%+ loyiha obyekti",
           "Davlat ulushi 50 foiz va undan ortiq bo'lgan davlat/munitsipal mulk negizida amalga "
           "oshiriladigan investitsiya loyihalari obyektlari — 0,5"),
    Option("industrial", D("0.5"), "Sanoat binosi",
           "Sanoat binolari (ishlab chiqarish, omborxona, sanoat-sinov laboratoriyalari) — "
           "har qavat uchun 3 m gacha balandlikda — 0,5"),
    Option("other", D("1"), "Boshqa obyekt", "Boshqa obyektlar — 1"),
)
KZ_OPTIONS: tuple[Option, ...] = (
    Option("1m+", None, "Aholisi 1 mln dan ortiq",
           "Aholisi 1 mln dan ortiq shaharlar — 1 dan 2 gacha (qiymatni xodim kiritadi)", range=(D("1"), D("2"))),
    Option("500k-1m", D("0.5"), "500 ming – 1 mln", "Aholisi 500 ming – 1 mln (Samarqand shahri) — 0,5"),
    Option("250k-500k", D("0.2"), "250 – 500 ming", "Aholisi 250 ming – 500 ming — 0,2"),
    Option("100k-250k", D("0.1"), "100 – 250 ming", "Aholisi 100 ming – 250 ming — 0,1"),
    Option("under100k", D("0.05"), "100 minggacha",
           "Aholisi 100 minggacha shaharlar va boshqa aholi punktlari — 0,05"),
)
KJ_OPTIONS: tuple[Option, ...] = (
    Option("central", D("1"), "Markaziy qatnov ko'chasi", "Markaziy qatnov ko'chasi — 1"),
    Option("other", D("0.6"), "Boshqa hudud", "Markaziy qatnov ko'chalaridan boshqa hududlar — 0,6"),
)
GROUPS: dict[str, tuple[Option, ...]] = {"kt": KT_OPTIONS, "ko": KO_OPTIONS, "kz": KZ_OPTIONS, "kj": KJ_OPTIONS}


def option(group: str, code: str) -> Option:
    if group not in GROUPS:
        raise FeeError(f"Noma'lum koeffitsiyent guruhi: {group!r}")
    found = [o for o in GROUPS[group] if o.code == code]
    if not found:
        raise FeeError(f"{group.capitalize()} uchun noma'lum variant: {code!r}")
    return found[0]


def option_value(group: str, code: str, entered: Decimal | None = None) -> Decimal:
    """Value of the chosen option; for a ranged option the staff-entered value is validated."""
    o = option(group, code)
    if o.value is not None:
        return o.value
    if entered is None:
        raise FeeError(f"{group.capitalize()} ({o.label}): qiymatni xodim kiritishi shart")
    assert o.range is not None
    if not (o.range[0] <= entered <= o.range[1]):
        raise FeeError(f"{group.capitalize()} ({o.label}): {entered} — {o.range[0]}..{o.range[1]} oralig'ida emas")
    return entered


@dataclass(frozen=True)
class FeeInput:
    bhm: Decimal
    hb: Decimal
    hyu: Decimal
    ha: Decimal
    ht: Decimal
    hu: Decimal
    kt: Decimal
    ko: Decimal
    kz: Decimal
    kj: Decimal
    k_council: Decimal = D("1")


@dataclass(frozen=True)
class FeeResult:
    chargeable_volume: Decimal   # (Hb + Hyu) - (Ha + Ht + Hu), m3
    multiplier_raw: Decimal      # Kt x Ko x Kz x Kj x Kcouncil
    multiplier: Decimal          # after the 0.01 floor
    floor_applied: bool
    fee: Decimal                 # whole UZS
    advance_20: Decimal          # whole UZS
    remainder_80: Decimal        # fee - advance_20
    formula: str


_KT = {o.value for o in KT_OPTIONS}
_KO = {o.value for o in KO_OPTIONS}
_KJ = {o.value for o in KJ_OPTIONS}
_KZ_FIXED = {o.value for o in KZ_OPTIONS if o.value is not None}


def _require_decimal(name: str, v: object) -> Decimal:
    if not isinstance(v, Decimal):
        raise FeeError(f"{name}: Decimal bo'lishi shart, berilgan tur {type(v).__name__}")
    if not v.is_finite():
        raise FeeError(f"{name}: chekli son emas ({v})")
    return v


def _whole_uzs(v: Decimal) -> Decimal:
    return v.quantize(D("1"), rounding=ROUND_HALF_UP)


def fmt_num(v: Decimal) -> str:
    """Decimal as plain text: no exponent, no trailing zeros."""
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def compute(i: FeeInput) -> FeeResult:
    for name in ("bhm", "hb", "hyu", "ha", "ht", "hu", "kt", "ko", "kz", "kj", "k_council"):
        _require_decimal(name, getattr(i, name))
    if i.bhm <= 0:
        raise FeeError(f"BHM musbat bo'lishi shart: {i.bhm}")
    for name in ("hb", "hyu", "ha", "ht", "hu"):
        if getattr(i, name) < 0:
            raise FeeError(f"{name.capitalize()} manfiy bo'lishi mumkin emas: {getattr(i, name)}")
    if i.kt not in _KT:
        raise FeeError(f"Kt ruxsat etilgan qiymatlardan emas (1; 0,5; 0,85): {i.kt}")
    if i.ko not in _KO:
        raise FeeError(f"Ko ruxsat etilgan qiymatlardan emas (0,8; 0,5; 1): {i.ko}")
    if i.kz not in _KZ_FIXED and not (D("1") <= i.kz <= D("2")):
        raise FeeError(f"Kz ruxsat etilgan qiymatlardan emas (0,05; 0,1; 0,2; 0,5; 1..2): {i.kz}")
    if i.kj not in _KJ:
        raise FeeError(f"Kj ruxsat etilgan qiymatlardan emas (1; 0,6): {i.kj}")
    if not (COUNCIL_RANGE[0] <= i.k_council <= COUNCIL_RANGE[1]):
        raise FeeError(f"Kengash koeffitsiyenti 0,01..1,5 oralig'ida emas: {i.k_council}")

    volume = (i.hb + i.hyu) - (i.ha + i.ht + i.hu)
    if volume < 0:
        raise FeeError(
            f"Hisob hajmi manfiy: (Hb {fmt_num(i.hb)} + Hyu {fmt_num(i.hyu)}) − "
            f"(Ha {fmt_num(i.ha)} + Ht {fmt_num(i.ht)} + Hu {fmt_num(i.hu)}) = {fmt_num(volume)} m³")
    raw = i.kt * i.ko * i.kz * i.kj * i.k_council
    floored = raw < MULTIPLIER_FLOOR
    mult = MULTIPLIER_FLOOR if floored else raw
    fee = _whole_uzs(i.bhm * volume * mult)
    advance = _whole_uzs(fee * ADVANCE_SHARE)
    formula = (
        f"{fmt_num(i.bhm)} × (({fmt_num(i.hb)} + {fmt_num(i.hyu)}) − "
        f"({fmt_num(i.ha)} + {fmt_num(i.ht)} + {fmt_num(i.hu)})) "
        f"× {fmt_num(i.kt)} × {fmt_num(i.ko)} × {fmt_num(i.kz)} × {fmt_num(i.kj)} × {fmt_num(i.k_council)}"
        f" = {fmt_num(i.bhm)} × {fmt_num(volume)} × {fmt_num(mult)}"
        + (f" (ko'paytma {fmt_num(raw)} < 0,01, shuning uchun 0,01 olindi)" if floored else "")
        + f" = {fmt_num(fee)} so'm")
    return FeeResult(volume, raw, mult, floored, fee, advance, fee - advance, formula)


@dataclass(frozen=True)
class IndustrialFloor:
    name: str
    area: Decimal
    height: Decimal
    excess_volume: Decimal  # area x (height - 3) when height > 3


def industrial_3m_indicator(floors: list[tuple[str, Decimal, Decimal]]) -> tuple[list[IndustrialFloor], Decimal]:
    """Industrial building: volume of the part of each floor above 3 m. Shown only, never subtracted."""
    out: list[IndustrialFloor] = []
    total = D("0")
    for name, area, height in floors:
        _require_decimal(f"{name}: maydon", area)
        _require_decimal(f"{name}: balandlik", height)
        excess = area * (height - INDUSTRIAL_HEIGHT_LIMIT) if height > INDUSTRIAL_HEIGHT_LIMIT else D("0")
        out.append(IndustrialFloor(name, area, height, excess))
        total += excess
    return out, total
