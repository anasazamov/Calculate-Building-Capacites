"""Offline replay of the full pipeline: recorded raw model responses (tests/data/raw) are served
from the request cache, so no model server is needed. If prompts or fixtures change, the cache
keys change and the test fails with a connection error: re-record with a running model."""
import shutil
from decimal import Decimal as D
from pathlib import Path

import pytest

from feecalc import llm, pipeline, verify

RAW = Path(__file__).parent / "data" / "raw"


@pytest.fixture(scope="module")
def result(sample_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> dict:
    job = tmp_path_factory.mktemp("job")
    shutil.copytree(RAW, job / "raw")
    mp = pytest.MonkeyPatch()
    mp.setattr(llm, "server_ready", lambda m: m.key == "model1")
    mp.setattr(llm, "vllm_version", lambda m: "replay")
    stages: list[str] = []
    out = pipeline.run_extraction(job, [sample_dir / "demo_project.pdf"], sample_dir / "demo_conclusion.pdf",
                                  stages.append)
    mp.undo()
    assert stages == [s for s, _ in pipeline.STAGES]
    return out


def test_expected_values_are_read_and_accepted(result: dict):
    project = result["project"]["model1"]
    assert len(project["buildings"]) == 1
    b = project["buildings"][0]
    expected = {"footprint_area": "600.3", "volume": "36036", "volume_above_ground": "34619.3",
                "volume_below_ground": "1416.7", "total_area": "9410.0", "apartments": "96"}
    for field, number in expected.items():
        assert b[field]["status"] == verify.ACCEPTED and D(b[field]["number"]) == D(number), field
        assert b[field]["page"] == 1 and b[field]["source_text"]
    areas = [D(x["number"]) for x in project["area_breakdown"]]
    assert areas == [D("465.9"), D("442.6"), D("7439.3"), D("1026.4"), D("35.8")]
    assert "Наманган" in project["scalars"]["address"]["value"]
    c = result["conclusion"]["model1"]
    assert [x["name"]["value"] for x in c["buildings"]] == ["Tip-2", "Tip-1"]
    tip2 = c["buildings"][0]
    for field, number in {"floors": "16", "footprint_area": "600.3", "total_area": "9410.0",
                          "volume": "36036.0", "apartments": "96"}.items():
        assert tip2[field]["status"] == verify.ACCEPTED and D(tip2[field]["number"]) == D(number), field
    assert tip2["plan_size"]["value"] == "26.97 × 26.89"
    assert c["scalars"]["conclusion_number"]["value"] == "284459"
    assert any("xato yozilgan" in f for f in tip2["volume"]["flags"])  # "m2" written for a volume


def test_assessment(result: dict):
    a = pipeline.assess(result, permitted_floors=12)
    assert [(x["p"], x["c"]) for x in a["pairs"]] == [(0, 0), (None, 1)]
    by_code = {c["code"]: c for c in a["checks"]}
    assert by_code["address"]["status"] == verify.MISMATCH          # Namangan vs Samarqand
    assert by_code["b0_sum_volume"]["status"] == verify.OK
    assert by_code["sum_area"]["status"] == verify.OK
    assert by_code["b0_volume"]["status"] == verify.OK
    assert by_code["b0_footprint_area"]["status"] == verify.OK
    assert by_code["conclusion_total_volume"]["status"] == verify.OK
    assert by_code["b0_volume_lower_bound"]["status"] == verify.OK
    rows = a["hb_rows"]
    assert rows[0]["volume"] == "36036" and "xulosa bilan mos" in rows[0]["formula"]
    assert rows[1]["volume"] == "52397.2" and "loyiha berilmagan" in rows[1]["formula"]
    v = a["volumes"]
    assert D(v["Hb"]["proposal"]) == D("88433.2") and v["Ha"]["proposal"] == "0"
    assert v["Ht"]["proposal"] is None                               # technical level has no height
    assert D(v["Hu"]["proposal"]) == D("1026.4") * D("3.0")
    assert v["Hyu"]["proposal"] == "5951.44"


def test_staff_can_exclude_a_building_and_rematch(result: dict):
    a = pipeline.assess(result)
    other = a["hb_rows"][1]["id"]
    b = pipeline.assess(result, overrides={other: {"included": False, "reason": "boshqa bosqich"}})
    assert b["volumes"]["Hb"]["proposal"] == "36036"
    c = pipeline.assess(result, matches={"0": 1})                    # pair the album with Tip-1 instead
    assert [(x["p"], x["c"]) for x in c["pairs"]] == [(None, 0), (0, 1)]
    assert {k["code"]: k for k in c["checks"]}["b1_volume"]["status"] == verify.MISMATCH
    assert c["hb_rows"][1]["volume"] is None                         # disagreement: staff decides


def test_conclusion_only_job(sample_dir: Path, tmp_path_factory: pytest.TempPathFactory):
    job = tmp_path_factory.mktemp("job_conclusion_only")
    shutil.copytree(RAW, job / "raw")
    mp = pytest.MonkeyPatch()
    mp.setattr(llm, "server_ready", lambda m: m.key == "model1")
    mp.setattr(llm, "vllm_version", lambda m: "replay")
    result = pipeline.run_extraction(job, [], sample_dir / "demo_conclusion.pdf")
    mp.undo()
    assert result["project"] == {} and any("Loyiha berilmagan" in w for w in result["warnings"])
    a = pipeline.assess(result)
    assert not a["has_project"] and [(x["p"], x["c"]) for x in a["pairs"]] == [(None, 0), (None, 1)]
    assert {c["code"]: c for c in a["checks"]}["address"]["status"] == verify.NOT_RUN
    assert D(a["volumes"]["Hb"]["proposal"]) == D("88433.2")
    assert a["volumes"]["Ha"]["proposal"] == "0" and a["components"] == []


def test_model_server_down_is_an_error(sample_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(llm, "server_ready", lambda m: False)
    with pytest.raises(llm.ModelError, match="tayyor emas"):
        pipeline.run_extraction(tmp_path, [sample_dir / "demo_project.pdf"], sample_dir / "demo_conclusion.pdf")


def _v(number: str | None, **extra) -> dict:
    return {"value": number, "number": number, "status": verify.ACCEPTED if number else verify.NOT_FOUND,
            "file": "c.pdf", "page": 2, "source_text": number, "reason": "", "flags": [], **extra}


def test_volume_lower_bound_uses_named_storeys_when_count_is_missing():
    # "yerto'lada h=2,6 va 1,2,3 qavatlarda h=3,0": 222.6 x (2.6 + 3 x 3.0) = 2582.16 > 2226
    building = {"floors": _v(None), "floor_heights": [_v("2.6", level="yerto’lada"), _v("3.0", level="1,2,3 qavatlarda"),
                                                      _v("0.45", level="Poydevor")]}
    heights, how = pipeline._building_heights(building)
    assert sum(heights) == D("11.6") and "nomlangan" in how
    check = verify.check_volume_lower_bound(D("2226.0"), D("222.6"), heights)
    assert check.status == verify.WARNING and "2582.16" in check.detail


def test_volume_lower_bound_adds_basement_and_lowest_attic():
    # basement + 4 storeys at 3.00 and an attic of at least 1.80: 645 x 16.8 = 10836 > 10640
    building = {"floors": _v("4"), "floor_heights": [
        _v("3.00", level="Yerto`la qavati va qavatlarning ichki balandligi"),
        _v("1.80", level="Mansarda qavatida eng past"), _v("2.40", level="Mansarda qavatida eng yuqori")]}
    heights, _ = pipeline._building_heights(building)
    assert sum(heights) == D("16.80")
    assert verify.check_volume_lower_bound(D("10640.00"), D("645.0"), heights).status == verify.WARNING


def _site(kind: str, number: str) -> dict:
    return {**_v(number), "kind": kind, "name": kind}


def test_site_balance_checks():
    site = [_site("total", "1400.0"), _site("built_up", "222.6"), _site("paving", "400.0"), _site("greenery", "449.76")]
    balance, footprints = pipeline._site_checks("xulosa", "conclusion", site, [D("222.6")])
    assert balance.status == verify.WARNING and "327.64" in balance.detail
    assert footprints.status == verify.OK
    # the same figures repeated on another sheet count once
    again = site + [dict(x, page=8) for x in site]
    assert pipeline._site_checks("loyiha", "project", again, [])[0].detail == balance.detail
    # several figures of one kind (construction stages): the balance is left to staff
    staged = [_site("total", "73531"), _site("built_up", "17345"), _site("built_up", "15690"), _site("paving", "12549")]
    checks = pipeline._site_checks("xulosa", "conclusion", staged, [D("17347.9")])
    assert checks[0].status == verify.NOT_RUN
    assert checks[1].status == verify.WARNING and "2.9" in checks[1].detail      # 17347.9 vs 17345


def test_object_description_is_read_by_code(tmp_path: Path):
    import pymupdf
    from feecalc import decide, ingest
    doc = pymupdf.open()
    page = doc.new_page()
    lines = ["EKSPERTIZA XULOSASI", "Obyekt nomi: Samarqand viloyati Payariq tumani, Tomoyrot MFY, Iforli ko`chasi 36-",
             "manzilda joylashgan savdo va maishiy xizmat ko`rsatish binosi qurilishi.", "Buyurtmachi: «Namuna» MCHJ",
             "1.1. Loyiha hujjatlari"]
    for i, line in enumerate(lines):
        page.insert_text((60, 80 + 22 * i), line, fontsize=11)
    path = tmp_path / "conclusion.pdf"
    doc.save(str(path))
    found = pipeline.object_description(ingest.ingest(path))
    assert found is not None and found["page"] == 1 and found["status"] == "accepted"
    assert found["value"] == ("Samarqand viloyati Payariq tumani, Tomoyrot MFY, Iforli ko`chasi 36-manzilda joylashgan "
                              "savdo va maishiy xizmat ko`rsatish binosi qurilishi.")
    assert found["source_text"].startswith("Obyekt nomi:") and found["file"] == "conclusion.pdf"
    place = decide.locate(found["value"])
    assert (place.settlement, place.street) == ("payariq tumani", "iforli")
    blank = pymupdf.open()
    blank.new_page().insert_text((60, 80), "Boshqa hujjat", fontsize=11)
    blank.save(str(tmp_path / "blank.pdf"))
    assert pipeline.object_description(ingest.ingest(tmp_path / "blank.pdf")) is None
