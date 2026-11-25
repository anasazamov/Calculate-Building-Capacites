"""EXPERIMENT: the tool-using agent (feecalc/agent.py) on the sample pairs, next to the fixed pipeline.

Run: python scripts/agent_experiment.py model1 [--only 298814 ...] [--out data/agent]
Prints one summary line per sample (counts and numbers only); full steps go to <out>/<model>/<id>.json.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from feecalc import agent, llm
from feecalc.knowledge import KnowledgeBase


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("model", choices=sorted(llm.MODELS))
    a.add_argument("--samples", type=Path, default=Path.home() / "samples")
    a.add_argument("--out", type=Path, default=Path("data/agent"))
    a.add_argument("--only", nargs="*", default=None)
    arg = a.parse_args()
    m = llm.MODELS[arg.model]()
    kb = KnowledgeBase()
    out = arg.out / arg.model
    out.mkdir(parents=True, exist_ok=True)
    ids = sorted(p.name.split("_")[0] for p in arg.samples.glob("*_xulosa.pdf"))
    for sample_id in [i for i in ids if not arg.only or i in arg.only]:
        project = arg.samples / f"{sample_id}_loyiha.pdf"
        start = time.monotonic()
        result = agent.run(m, arg.samples / f"{sample_id}_xulosa.pdf", project if project.exists() else None, kb,
                           out / sample_id / "raw")
        result["seconds"] = round(time.monotonic() - start, 1)
        (out / f"{sample_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        checked = result["checked"] or {}
        buildings = checked.get("buildings") or []
        print(json.dumps({
            "id": sample_id, "steps": len(result["steps"]), "finished": result["answer"] is not None,
            "seconds": result["seconds"], "tools": [s["tool"] for s in result["steps"]],
            "buildings": len(buildings), "buildings_verified": sum(1 for b in buildings if b["status"] == "accepted"),
            "hb_claimed": checked.get("hb_claimed"), "hb_from_verified": checked.get("hb_from_verified_buildings"),
            "construction_type": (checked.get("construction_type") or {}).get("status"),
            "object": (checked.get("object") or {}).get("status")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
