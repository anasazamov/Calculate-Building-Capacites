"""Read every sample pair in a folder with one model and store the raw extraction results.

Naming: <id>_xulosa.pdf (conclusion, required) and <id>_loyiha.pdf (project, optional).
Results go to <out>/<model>/<id>.json; raw model responses to <out>/<model>/<id>/raw.
The etalon files are NOT read here (see scripts/score_samples.py).

Run: python scripts/read_samples.py model1 [--samples ~/samples] [--out data/eval]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from feecalc import extract, ingest, llm, pipeline


def read_one(m: llm.Model, samples: Path, out: Path, sample_id: str) -> dict:
    raw = out / sample_id / "raw"
    result: dict = {"id": sample_id, "model": m.name, "prompt_version": extract.PROMPT_VERSION,
                    "project": None, "sheet_kinds": {}, "level_marks": [], "error": None}
    start = time.monotonic()
    conclusion_pages = ingest.ingest(samples / f"{sample_id}_xulosa.pdf")
    result["conclusion"] = extract.read_conclusion(m, conclusion_pages, raw)
    result["seconds_conclusion"] = round(time.monotonic() - start, 1)
    project_pdf = samples / f"{sample_id}_loyiha.pdf"
    start = time.monotonic()
    if project_pdf.exists():
        pages = ingest.ingest(project_pdf)
        kinds = pipeline.classify_sheets(m, pages, raw)
        result["sheet_kinds"] = {str(n): [k.kind, k.method] for n, k in kinds.items()}
        result["project"] = extract.read_project(m, [(project_pdf, pages, kinds)], raw)
        result["level_marks"] = [mark for p in pages if kinds[p.number].kind == "section"
                                 for mark in extract.level_marks(p)]
    result["seconds_project"] = round(time.monotonic() - start, 1)
    return result


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("model", choices=["model1", "model2"])
    a.add_argument("--samples", type=Path, default=Path.home() / "samples")
    a.add_argument("--out", type=Path, default=Path("data/eval"))
    a.add_argument("--only", nargs="*", default=None)
    arg = a.parse_args()
    m = llm.model1() if arg.model == "model1" else llm.model2()
    out = arg.out / arg.model
    out.mkdir(parents=True, exist_ok=True)
    ids = sorted(p.name.split("_")[0] for p in arg.samples.glob("*_xulosa.pdf"))
    if arg.only:
        ids = [i for i in ids if i in arg.only]

    def one(sample_id: str) -> None:
        # Per-sample boundary of this test harness: a failed sample is recorded with its full
        # error and the other samples still run (same idea as the job boundary in the web app).
        import traceback
        start = time.monotonic()
        try:
            result = read_one(m, arg.samples, out, sample_id)
        except Exception:  # noqa: BLE001
            result = {"id": sample_id, "model": m.name, "error": traceback.format_exc()}
        result["seconds_total"] = round(time.monotonic() - start, 1)
        (out / f"{sample_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(sample_id, "ERROR" if result.get("error") else "ok", result["seconds_total"], "s", flush=True)

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        list(pool.map(one, ids))


if __name__ == "__main__":
    main()
