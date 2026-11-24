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
    s = result["project"]["model1"]["scalars"]
    expected = {"footprint_area": "600.3", "volume_total": "36036", "volume_above_ground": "34619.3",
                "volume_below_ground": "1416.7", "total_area": "9410.0", "apartments": "96"}
    for field, number in expected.items():
        assert s[field]["status"] == verify.ACCEPTED and D(s[field]["number"]) == D(number), field
        assert s[field]["page"] == 1 and s[field]["source_text"]
    areas = [D(x["number"]) for x in result["project"]["model1"]["area_breakdown"]]
    assert areas == [D("465.9"), D("442.6"), D("7439.3"), D("1026.4"), D("35.8")]
    c = result["conclusion"]["model1"]
    b = c["buildings"][0]
    for field, number in {"floors": "16", "footprint_area": "600.3", "total_area": "9410.0",
                          "volume": "36036.0", "apartments": "96"}.items():
        assert b[field]["status"] == verify.ACCEPTED and D(b[field]["number"]) == D(number), field
    assert b["plan_size"]["value"] == "26.97 × 26.89"
    assert c["scalars"]["conclusion_number"]["value"] == "284459"
    assert any("xato yozilgan" in f for f in b["volume"]["flags"])  # "m2" written for a volume


def test_assessment(result: dict):
    a = pipeline.assess(result, permitted_floors=12)
    assert a["building_index"] == 0 and a["building_count"] == 2
    by_code = {c["code"]: c for c in a["checks"]}
    assert by_code["address"]["status"] == verify.MISMATCH          # Namangan vs Samarqand
    assert by_code["sum_volume"]["status"] == verify.OK
    assert by_code["sum_area"]["status"] == verify.OK
    assert by_code["equal_volume_total"]["status"] == verify.OK
    assert by_code["conclusion_total_volume"]["status"] == verify.OK
    assert by_code["volume_lower_bound"]["status"] == verify.OK
    v = a["volumes"]
    assert v["Hb"]["proposal"] == "36036" and v["Ha"]["proposal"] == "0"
    assert v["Ht"]["proposal"] is None                               # technical level has no height
    assert D(v["Hu"]["proposal"]) == D("1026.4") * D("3.0")
    assert v["Hyu"]["proposal"] == "5951.44"


def test_staff_can_choose_building(result: dict):
    a = pipeline.assess(result, chosen_building=1)
    assert a["building"]["name"]["value"] == "Tip-1"
    assert {c["code"]: c for c in a["checks"]}["equal_volume_total"]["status"] == verify.MISMATCH
    with pytest.raises(ValueError):
        pipeline.assess(result, chosen_building=5)


def test_model_server_down_is_an_error(sample_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(llm, "server_ready", lambda m: False)
    with pytest.raises(llm.ModelError, match="tayyor emas"):
        pipeline.run_extraction(tmp_path, [sample_dir / "demo_project.pdf"], sample_dir / "demo_conclusion.pdf")
