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
