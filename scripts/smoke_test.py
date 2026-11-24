"""Model smoke test: canary; N sequential and M parallel short JSON requests.

Counts malformed, empty and wrong responses, tokens/s and the "!!!!" symptom.
Run: python scripts/smoke_test.py model1 [--sequential 50] [--parallel 16] [--sampling default|card]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from feecalc import llm

SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "volume": {"type": "string"}, "unit": {"type": "string"}},
    "required": ["id", "volume", "unit"], "additionalProperties": False,
}
# The schema is flat and short, so validity can be checked with a regex (no try/except needed).
VALID = re.compile(r'^\{\s*"id":\s*-?\d+\s*,\s*"volume":\s*"[^"\\]*"\s*,\s*"unit":\s*"[^"\\]*"\s*\}$')


def one(m: llm.Model, i: int, sampling: dict, shape_in_prompt: bool = True) -> dict:
    volume = f"{10000 + i * 137}.{i % 10}"
    text = (f"Document fragment: «Объект №{i}. Строительный объём здания — {volume} м3, площадь застройки — "
            f"{300 + i}.5 м2.» Put {i} into id, the building volume exactly as written into volume, "
            f"and its unit into unit.")
    if shape_in_prompt:
        text += ' Reply exactly in this shape: {"id": <integer>, "volume": "<number as a string>", "unit": "<text>"}'
    body = llm.request_body(m, "Reply with JSON only.", text, SCHEMA, 64, sampling)
    start = time.monotonic()
    r = httpx.post(f"{m.url}/chat/completions", json=body, timeout=300.0)
    out: dict = {"i": i, "duration": time.monotonic() - start, "status": "ok", "tokens": 0, "raw": r.text[:300]}
    if r.status_code != 200:
        out["status"] = f"http_{r.status_code}"
        return out
    j = r.json()
    choice = j["choices"][0]
    content = choice["message"].get("content") or ""
    out["tokens"] = j["usage"]["completion_tokens"]
    out["raw"] = content[:300]
    if llm.degenerate_reason(content) or llm.repetition_reason(content):
        out["status"] = "degenerate"
    elif not content.strip():
        out["status"] = "empty"
    elif choice["finish_reason"] != "stop" or not VALID.match(content.strip()):
        out["status"] = "malformed"
    else:
        d = json.loads(content)
        if d["id"] != i or d["volume"].replace(",", ".") != volume:
            out["status"] = "wrong"
    return out


def summary(name: str, results: list[dict], wall: float) -> dict:
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    tokens = sum(r["tokens"] for r in results)
    return {
        "test": name, "count": len(results), "statuses": statuses, "wall_s": round(wall, 2),
        "completion_tokens": tokens, "tokens_per_s_total": round(tokens / wall, 1),
        "mean_request_s": round(sum(r["duration"] for r in results) / len(results), 3),
        "sample_failures": [r for r in results if r["status"] != "ok"][:5],
    }


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("model", choices=sorted(llm.MODELS))
    a.add_argument("--sequential", type=int, default=50)
    a.add_argument("--parallel", type=int, default=16)
    a.add_argument("--sampling", choices=["default", "card"], default="default")
    a.add_argument("--out", type=Path, default=None)
    a.add_argument("--no-shape-in-prompt", action="store_true",
                   help="do not describe the response shape in the prompt (reproduces the failure)")
    arg = a.parse_args()
    m = llm.MODELS[arg.model]()
    sampling = llm.SAMPLING_DEFAULT if arg.sampling == "default" else llm.SAMPLING_CARD
    shape = not arg.no_shape_in_prompt

    c = llm.canary(m, Path("data/llm_raw/canary"))
    report: dict = {"model": m.name, "vllm": llm.vllm_version(m), "sampling": sampling, "shape_in_prompt": shape,
                    "canary": {"ok": True, "duration_s": c.duration_s}}

    start = time.monotonic()
    seq = [one(m, i, sampling, shape) for i in range(arg.sequential)]
    report["sequential"] = summary("sequential", seq, time.monotonic() - start)

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=arg.parallel) as pool:
        par = list(pool.map(lambda i: one(m, i, sampling, shape), range(1000, 1000 + arg.parallel)))
    report["parallel"] = summary(f"parallel-{arg.parallel}", par, time.monotonic() - start)

    c2 = llm.canary(m, Path("data/llm_raw/canary"))
    report["canary_after"] = {"ok": True, "duration_s": c2.duration_s}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if arg.out:
        arg.out.parent.mkdir(parents=True, exist_ok=True)
        arg.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
