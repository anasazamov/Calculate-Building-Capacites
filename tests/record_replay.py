"""Re-record the raw model responses used by the offline replay tests.

Needed whenever prompts, schemas or the fixture documents change (the request cache key changes).
Requires model 1 to be running. Run from the repository root: python -m tests.record_replay
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from feecalc import llm, pipeline
from tests import fixtures

RAW = Path(__file__).parent / "data" / "raw"


def main() -> None:
    assert llm.server_ready(llm.model1()), "model 1 is not running"
    llm.server_ready = lambda m: m.key == "model1"  # type: ignore[assignment]  # record with model 1 only
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        conclusion = fixtures.build_conclusion(work / "demo_conclusion.pdf")
        project = fixtures.build_project(work / "demo_project.pdf")
        result = pipeline.run_extraction(work / "job", [project], conclusion, print)
        pipeline.run_extraction(work / "job_conclusion_only", [], conclusion, print)
        shutil.rmtree(RAW, ignore_errors=True)
        RAW.mkdir(parents=True)
        for folder in ("job", "job_conclusion_only"):
            for f in (work / folder / "raw").glob("*.json"):
                shutil.copy(f, RAW / f.name)
        print(f"recorded {len(list(RAW.glob('*.json')))} responses; timing {result['timing']}")


if __name__ == "__main__":
    main()
