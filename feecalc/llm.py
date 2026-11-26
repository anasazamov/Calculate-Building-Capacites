"""Requests to the local vLLM servers (127.0.0.1 only). No external API.

- Every response is stored raw on disk and an identical request is never sent twice.
- The response is constrained by a JSON schema; a malformed, empty or truncated response
  is an error (nothing is silently repaired).
- When a degenerate output such as "!!!!" is detected, a marker line is written so that
  the watchdog restarts the server.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jsonschema

SAMPLING_DEFAULT: dict[str, float | int] = {"temperature": 0.2, "top_p": 0.8, "top_k": 20, "presence_penalty": 0}
# Non-thinking settings from the model card (for comparison if repetition loops show up)
SAMPLING_CARD: dict[str, float | int] = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 0}
TIMEOUT_S = 600.0


def marker_file() -> Path:
    return Path(os.environ.get("FEECALC_DEGENERATE_MARKER", "logs/degenerate_marker.jsonl"))


class ModelError(RuntimeError):
    pass


class MalformedResponse(ModelError):
    pass


class DegenerateOutput(ModelError):
    """The response is a run of one repeated token (the "!!!!" failure)."""


class RepetitionLoop(MalformedResponse):
    """The response contains a repetition loop."""


@dataclass(frozen=True)
class Model:
    key: str                   # "model1": the reader (stored results are keyed by it)
    name: str                  # served-model-name
    url: str                   # http://127.0.0.1:8001/v1
    extra: dict = field(default_factory=dict)  # extra request-body fields

    def host_port(self) -> tuple[str, int]:
        u = urlparse(self.url)
        assert u.hostname in ("127.0.0.1", "localhost"), f"Model URL is not local: {self.url}"
        return u.hostname, u.port or 80


def model1() -> Model:
    """The one reading model: Gemma-4-31B-it (scripts/vllm_model1.sh).

    A second reader (Qwen3.8-27B) was removed on 2026-11-26: on the inspector's cases both models made
    the same mistakes, so their agreement was not an independent check and only doubled the cost.
    Results stay keyed "model1"; jobs read earlier with two models still carry a "model2" entry."""
    return Model("model1", os.environ.get("FEECALC_MODEL1_NAME", "gemma-4-31b-it"),
                 os.environ.get("FEECALC_MODEL1_URL", "http://127.0.0.1:8001/v1"),
                 {"chat_template_kwargs": {"enable_thinking": False}})


MODELS = {"model1": model1}


def port_open(m: Model) -> bool:
    """Is the server listening (check without exceptions)."""
    host, port = m.host_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2.0)
        return s.connect_ex((host, port)) == 0


def server_ready(m: Model) -> bool:
    if not port_open(m):
        return False
    return httpx.get(f"{m.url.removesuffix('/v1')}/health", timeout=10.0).status_code == 200


def vllm_version(m: Model) -> str:
    return str(httpx.get(f"{m.url.removesuffix('/v1')}/version", timeout=10.0).json()["version"])


def degenerate_reason(text: str) -> str | None:
    """Symptom of the persistent "!!!!" failure (the server must be restarted), or None."""
    t = text.strip()
    if re.search(r"!{4,}", t):
        return "javobda '!!!!' ketma-ketligi"
    m = re.search(r"([^\s\d|])\1{39,}", t)
    if m:
        return f"bir xil belgi {len(m.group(0))} marta ketma-ket: {m.group(1)!r}"
    return None


def repetition_reason(text: str) -> str | None:
    """A repetition loop inside one response (e.g. "| | | | ..."). It spoils that response only:
    the request fails, but the server is healthy and is not restarted."""
    m = re.search(r"(.{2,40}?)\1{11,}", text, flags=re.S)
    if m and not re.fullmatch(r"[\d\s.,]+", m.group(1)):
        return f"bir xil bo'lak 12+ marta takrorlangan: {m.group(1)[:20]!r}"
    return None


def _write_marker(m: Model, reason: str, key: str) -> None:
    path = marker_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": m.key,
                            "reason": reason, "request": key}, ensure_ascii=False) + "\n")


@dataclass
class Response:
    data: dict
    key: str                 # hash of the request body (name of the raw-response file)
    prompt_hash: str         # hash of system + user text
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_s: float
    cached: bool


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_body(m: Model, system: str, user: str, schema: dict, max_tokens: int,
                 sampling: dict | None = None) -> dict:
    body: dict = {
        "model": m.name,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "answer", "strict": True, "schema": schema}},
    }
    body.update(sampling or SAMPLING_DEFAULT)
    body.update(m.extra)
    return body


def ask(m: Model, system: str, user: str, schema: dict, max_tokens: int, raw_dir: Path,
        sampling: dict | None = None, cache: bool = True, nonce: str = "") -> Response:
    """One request to a model. The raw response is stored as raw_dir/<key>.json."""
    body = request_body(m, system, user, schema, max_tokens, sampling)
    key = sha256_text(json.dumps({"url": m.url, "body": body, "nonce": nonce}, sort_keys=True, ensure_ascii=False))
    prompt_hash = sha256_text(system + "\n\n" + user)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{key}.json"
    cached = cache and path.exists()
    if cached:
        record = json.loads(path.read_text(encoding="utf-8"))
    else:
        start = time.monotonic()
        r = httpx.post(f"{m.url}/chat/completions", json=body, timeout=TIMEOUT_S)
        duration = time.monotonic() - start
        if r.status_code != 200:
            raise ModelError(f"{m.key}: HTTP {r.status_code}: {r.text[:500]}")
        record = {"model": m.key, "url": m.url, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "duration_s": round(duration, 3), "prompt_hash": prompt_hash, "request": body,
                  "response": r.json()}
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    data = check_response(m, record["response"], schema, key)
    usage = record["response"].get("usage") or {}
    return Response(data, key, prompt_hash, m.name, int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)), float(record["duration_s"]), cached)


def check_response(m: Model, response: dict, schema: dict, key: str) -> dict:
    choice = response["choices"][0]
    text = choice["message"].get("content") or ""
    reason = degenerate_reason(text)
    if reason:
        _write_marker(m, reason, key)
        raise DegenerateOutput(f"{m.key}: aynish alomati — {reason} (xom javob: {key})")
    loop = repetition_reason(text)
    if loop:
        raise RepetitionLoop(f"{m.key}: takrorlanish halqasi — {loop} (xom javob: {key})")
    if not text.strip():
        raise MalformedResponse(f"{m.key}: content bo'sh (finish_reason={choice.get('finish_reason')}, "
                                f"xom javob: {key})")
    if choice.get("finish_reason") != "stop":
        raise MalformedResponse(f"{m.key}: javob to'liq emas, finish_reason={choice.get('finish_reason')} "
                                f"(max_tokens yetmagan bo'lishi mumkin; xom javob: {key})")
    data = json.loads(text)  # malformed JSON stops here with JSONDecodeError
    jsonschema.validate(data, schema)
    return data


CANARY_SCHEMA: dict = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}, "word": {"type": "string"}},
    "required": ["sum", "word"], "additionalProperties": False,
}


def canary(m: Model, raw_dir: Path) -> Response:
    """Short health request: 17 + 25 = 42 and the word 'ready'. Never served from cache."""
    r = ask(m, "Reply with JSON only.",
            'What is 17 + 25? Reply exactly in this shape: {"sum": <integer>, "word": "ready"}',
            CANARY_SCHEMA, 32, raw_dir, cache=False, nonce=f"canary-{time.time_ns()}")
    if r.data["sum"] != 42 or r.data["word"].strip().lower() != "ready":
        raise MalformedResponse(f"{m.key}: kanareyka javobi noto'g'ri: {r.data}")
    return r


if __name__ == "__main__":
    # python -m feecalc.llm canary model1  -> exit code 0 means healthy
    action, which = sys.argv[1], sys.argv[2]
    assert action == "canary", action
    chosen = MODELS[which]()
    result = canary(chosen, Path(os.environ.get("FEECALC_RAW_DIR", "data/llm_raw")) / "canary")
    print(json.dumps({"ok": True, "duration_s": result.duration_s, "tokens": result.completion_tokens}))
