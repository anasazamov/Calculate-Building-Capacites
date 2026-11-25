"""The experimental agent: its tools and the source check of its answer (no model needed)."""
from decimal import Decimal
from pathlib import Path

import pytest

from feecalc import agent
from feecalc.knowledge import KnowledgeBase


def test_calculator_is_exact_and_refuses_anything_else():
    assert agent.calculate("82142.2 + 36036 + 6254.99") == Decimal("124433.19")
    assert agent.calculate("(335 + 420) * 3") == Decimal("2265") and agent.calculate("2 * 41071,1") == Decimal("82142.2")
    assert agent.calculate("-3 + 10 / 4") == Decimal("-0.5")
    for bad in ("2 +", "(1 + 2", "1 2", "__import__('os')", "2 ** 3"):
        with pytest.raises(ValueError):
            agent.calculate(bad)


def test_tools_and_answer_check(sample_dir: Path, tmp_path: Path):
    documents = agent.Documents(sample_dir / "demo_conclusion.pdf", sample_dir / "demo_project.pdf")
    kb = KnowledgeBase(tmp_path / "kb.db")
    assert "page 1:" in agent.run_tool("list_pages", {"doc": "xulosa"}, documents, kb)
    assert agent.run_tool("list_pages", {"doc": "boshqa"}, documents, kb).startswith("error")
    assert agent.run_tool("read_page", {"doc": "xulosa", "page": 99}, documents, kb).startswith("error")
    assert "36036" in agent.run_tool("find", {"doc": "loyiha", "query": "36036"}, documents, kb)
    assert agent.run_tool("find", {"doc": "xulosa", "query": "bunday matn yo'q"}, documents, kb) == "nothing found"
    assert agent.run_tool("calc", {"expression": "34619.3 + 1416.7"}, documents, kb) == "36036.0"
    assert agent.run_tool("calc", {"expression": "1/0"}, documents, kb).startswith("error")
    assert agent.run_tool("calc", {"expression": "import os"}, documents, kb).startswith("error")
    assert agent.run_tool("lookup_norm", {"query": "qurilish hajmi"}, documents, kb) == "nothing found"
    line = [x for x in agent.run_tool("find", {"doc": "loyiha", "query": "36036"}, documents, kb).splitlines()][0]
    page, snippet = int(line.split(":")[0].removeprefix("page ")), line.split(": ", 1)[1]
    good = {"name": "Tip-2", "volume": "36036", "count": "1", "doc": "loyiha", "page": page, "snippet": snippet}
    invented = {**good, "volume": "99999"}
    checked = agent.check_answer({"buildings": [good], "hb": "36036"}, documents)
    assert checked["buildings"][0]["status"] == "accepted" and checked["hb_from_verified_buildings"] == "36036"
    refused = agent.check_answer({"buildings": [invented], "hb": "99999"}, documents)
    assert refused["buildings"][0]["status"] != "accepted" and refused["hb_from_verified_buildings"] is None
    assert refused["construction_type"]["status"] != "accepted"                 # nothing given: nothing accepted
