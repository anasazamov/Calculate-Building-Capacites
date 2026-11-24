import json
from pathlib import Path

import pytest

from feecalc import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
M = llm.Model("model1", "test", "http://127.0.0.1:1/v1")


def response(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def test_valid_response():
    assert llm.check_response(M, response('{"a": "x"}'), SCHEMA, "k") == {"a": "x"}


@pytest.mark.parametrize("text", ["!!!!", "!!!!!!!!!!!!!!!!", '{"a": "!!!!!!"}', "a" * 60])
def test_degenerate_output_detected_and_marked(text, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FEECALC_DEGENERATE_MARKER", str(tmp_path / "marker.jsonl"))
    with pytest.raises(llm.DegenerateOutput):
        llm.check_response(M, response(text), SCHEMA, "k")
    assert json.loads((tmp_path / "marker.jsonl").read_text().splitlines()[0])["model"] == "model1"


@pytest.mark.parametrize("text", ['{"a": "1 ' + "| " * 40 + '"}', "ab " * 30])
def test_repetition_loop_fails_the_request_without_restart_marker(text, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FEECALC_DEGENERATE_MARKER", str(tmp_path / "marker.jsonl"))
    with pytest.raises(llm.RepetitionLoop):
        llm.check_response(M, response(text), SCHEMA, "k")
    assert not (tmp_path / "marker.jsonl").exists()


def test_numbers_are_not_degenerate():
    assert llm.degenerate_reason('{"a": "1000000000000000000000000000000000000000000000000"}') is None
    assert llm.repetition_reason("0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000") is None


def test_empty_truncated_and_invalid_are_errors():
    with pytest.raises(llm.MalformedResponse, match="bo'sh"):
        llm.check_response(M, response(""), SCHEMA, "k")
    with pytest.raises(llm.MalformedResponse, match="to'liq emas"):
        llm.check_response(M, response('{"a": "x', "length"), SCHEMA, "k")
    with pytest.raises(json.JSONDecodeError):
        llm.check_response(M, response('{"a": '), SCHEMA, "k")
    with pytest.raises(Exception, match="is not of type"):
        llm.check_response(M, response('{"a": 5}'), SCHEMA, "k")


def test_request_body_settings():
    body = llm.request_body(llm.model1(), "s", "u", SCHEMA, 100)
    assert (body["temperature"], body["top_p"], body["top_k"], body["presence_penalty"]) == (0.2, 0.8, 20, 0)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 100 and body["response_format"]["json_schema"]["schema"] == SCHEMA


def test_cached_response_is_not_requested_again(tmp_path: Path):
    body = llm.request_body(M, "s", "u", SCHEMA, 10)
    key = llm.sha256_text(json.dumps({"url": M.url, "body": body, "nonce": ""}, sort_keys=True, ensure_ascii=False))
    (tmp_path / f"{key}.json").write_text(json.dumps({"duration_s": 1.5, "response": response('{"a": "kesh"}')}))
    r = llm.ask(M, "s", "u", SCHEMA, 10, tmp_path)   # port 1 is closed: a real request would fail
    assert r.cached and r.data == {"a": "kesh"} and r.key == key


def test_only_local_urls():
    with pytest.raises(AssertionError):
        llm.Model("x", "x", "http://example.com/v1").host_port()
    assert not llm.port_open(M)
