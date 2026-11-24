"""Load test with realistic extraction requests: the recorded conclusion request of the synthetic
fixture is sent N times at each concurrency level (a nonce defeats the cache, the prompt is identical).

Reports wall time, tokens/s, failures and whether the answers of identical requests differ under load.
Run: python scripts/load_test.py model1 [--levels 1 8 16] [--out test_results/load_model1.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from feecalc import extract, llm

RAW = Path(__file__).resolve().parent.parent / "tests" / "data" / "raw"


def conclusion_request() -> dict:
    for f in sorted(RAW.glob("*.json")):
        body = json.loads(f.read_text(encoding="utf-8"))["request"]
        if body["messages"][0]["content"] == extract.CONCLUSION_SYSTEM:
            return body
    raise SystemExit("no recorded conclusion request; run python -m tests.record_replay first")


def one(m: llm.Model, body: dict, i: int) -> dict:
    payload = dict(body, model=m.name, seed=i)
    start = time.monotonic()
    r = httpx.post(f"{m.url}/chat/completions", json=payload, timeout=1800.0)
    out: dict = {"seconds": round(time.monotonic() - start, 2), "status": "ok", "tokens": 0, "digest": ""}
    if r.status_code != 200:
        out["status"] = f"http_{r.status_code}"
        return out
    j = r.json()
    choice = j["choices"][0]
    text = choice["message"].get("content") or ""
    out["tokens"] = j["usage"]["completion_tokens"]
    if llm.degenerate_reason(text):
        out["status"] = "degenerate"
    elif llm.repetition_reason(text):
        out["status"] = "repetition_loop"
    elif not text.strip():
        out["status"] = "empty"
    elif choice["finish_reason"] != "stop":
        out["status"] = f"finish_{choice['finish_reason']}"
    else:
        # compare the values only (the fields that matter), ignoring source snippets
        data = json.loads(text)
        values = [[b[k]["value"] for k in ("floors", "footprint_area", "total_area", "volume")] for b in data["buildings"]]
        out["digest"] = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:12]
    return out


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("model", choices=sorted(llm.MODELS))
    a.add_argument("--levels", type=int, nargs="+", default=[1, 8, 16])
    a.add_argument("--out", type=Path, default=None)
    arg = a.parse_args()
    m = llm.MODELS[arg.model]()
    body = conclusion_request()
    report: dict = {"model": m.name, "vllm": llm.vllm_version(m), "levels": []}
    for level in arg.levels:
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=level) as pool:
            results = list(pool.map(lambda i: one(m, body, i), range(level)))
        wall = time.monotonic() - start
        tokens = sum(r["tokens"] for r in results)
        statuses: dict[str, int] = {}
        for r in results:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        report["levels"].append({
            "parallel": level, "wall_s": round(wall, 1), "statuses": statuses, "completion_tokens": tokens,
            "tokens_per_s_total": round(tokens / wall, 1),
            "request_s_min_max": [min(r["seconds"] for r in results), max(r["seconds"] for r in results)],
            "distinct_value_sets": len({r["digest"] for r in results if r["status"] == "ok"}),
        })
        print(json.dumps(report["levels"][-1]), flush=True)
    llm.canary(m, Path("data/llm_raw/canary"))
    report["canary_after"] = "ok"
    if arg.out:
        arg.out.write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
