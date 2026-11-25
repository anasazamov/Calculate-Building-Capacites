"""EXPERIMENT: an agent that chooses its own steps, with tools, instead of the fixed pipeline.

The model is given tools a person would use (list the pages, read a page, search the text, look a
rule up in the knowledge base, calculate) and decides the order itself, then reports the building
volume and the facts behind the coefficients. Every value it reports is afterwards checked by the
same code as in the pipeline (the snippet must be on the page word for word).

This is not used by the web application. It deliberately departs from two rules of the pipeline -
the model sees both documents, and it does arithmetic through a tool - so that the two ways of
working can be measured side by side (scripts/agent_experiment.py).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from . import classify, extract, ingest, llm, verify
from .knowledge import KnowledgeBase

MAX_STEPS = 24
OBSERVATION_CHARS = 6000      # what one tool call may return
REMEMBERED_CHARS = 700        # how much of an older observation stays in the transcript
TOOLS = ("list_pages", "read_page", "find", "lookup_norm", "calc", "finish")

SYSTEM = """You are an inspector computing the building volume for an infrastructure fee in Uzbekistan.
Two documents are available: "xulosa" (the state expertise conclusion) and, if present, "loyiha" (the project album).
Work step by step with the tools. One tool per reply. Reply with JSON only, exactly in this shape:
{"thought": "<one short sentence>", "tool": "<tool name>", "args": {...}}
Tools:
- list_pages {"doc": "xulosa"|"loyiha"} -> pages with sheet type and text length
- read_page {"doc": ..., "page": <number>} -> text of the page (tables marked)
- find {"doc": ..., "query": "<text>"} -> lines that contain the text, with page numbers
- lookup_norm {"query": "<Uzbek words>"} -> clauses of the regulations (fee resolution, building codes)
- calc {"expression": "<numbers with + - * / and brackets>"} -> exact result
- finish {"answer": {...}} -> your result; ends the work
The answer for finish:
{"buildings": [{"name": "<as written>", "volume": "<number>", "count": "<number of identical buildings>",
                "doc": "xulosa"|"loyiha", "page": <number>, "snippet": "<text copied word for word from that page, containing the volume>"}],
 "hb": "<total building volume, m3>",
 "construction_type": {"value": "<as written in the conclusion>", "page": <number>, "snippet": "<copied word for word>"},
 "object": {"value": "<what is being built, as written>", "page": <number>, "snippet": "<copied word for word>"},
 "notes": "<anything the reviewer must know: disagreements, missing values, assumptions>"}
Rules: copy snippets exactly from the page text; never invent a number; if a value is not in the documents, leave it out and say so in notes;
compute sums with calc, not in your head; the conclusion is the authority when the documents disagree."""

SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "maxLength": 300},
        "tool": {"type": "string", "enum": list(TOOLS)},
        "args": {"type": "object"},
    },
    "required": ["thought", "tool", "args"], "additionalProperties": False,
}


# ---------- exact arithmetic for the calc tool (no eval) ----------
_TOKEN = re.compile(r"\s*(\d+(?:[.,]\d+)?|[-+*/()])")


def calculate(expression: str) -> Decimal:
    """Decimal value of an expression with + - * / and brackets. Anything else is an error."""
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        m = _TOKEN.match(expression, position)
        if not m:
            if expression[position:].strip() == "":
                break
            raise ValueError(f"calc: unexpected text at {position}: {expression[position:position + 12]!r}")
        tokens.append(m.group(1).replace(",", "."))
        position = m.end()
    index = 0

    def peek() -> str | None:
        return tokens[index] if index < len(tokens) else None

    def take() -> str:
        nonlocal index
        index += 1
        return tokens[index - 1]

    def factor() -> Decimal:
        token = peek()
        if token is None:
            raise ValueError("calc: expression ends too early")
        take()
        if token == "(":
            value = total()
            if peek() != ")":
                raise ValueError("calc: missing closing bracket")
            take()
            return value
        if token == "-":
            return -factor()
        if token in "+*/)":
            raise ValueError(f"calc: unexpected {token!r}")
        return Decimal(token)

    def product() -> Decimal:
        value = factor()
        while peek() in ("*", "/"):
            value = value * factor() if take() == "*" else value / factor()
        return value

    def total() -> Decimal:
        value = product()
        while peek() in ("+", "-"):
            value = value + product() if take() == "+" else value - product()
        return value

    result = total()
    if peek() is not None:
        raise ValueError(f"calc: unexpected {peek()!r}")
    return result


# ---------- the documents as the tools see them ----------
class Documents:
    def __init__(self, conclusion: Path, project: Path | None) -> None:
        self.paths = {"xulosa": conclusion, **({"loyiha": project} if project else {})}
        self.pages = {name: ingest.ingest(path) for name, path in self.paths.items()}

    def text(self, doc: str, page: int) -> str:
        p = self.pages[doc][page - 1]
        return ingest.structured_text(self.paths[doc], p) if doc == "loyiha" else extract.page_line_text(p)

    def list_pages(self, doc: str) -> str:
        rows = []
        for p in self.pages[doc]:
            kind = classify.SHEET_LABELS.get(classify.sheet_type(p).kind, "") if doc == "loyiha" else ""
            rows.append(f"page {p.number}: {p.char_count} characters" + (f", {kind}" if kind else "") + (", no text layer" if p.textless else ""))
        return "\n".join(rows)

    def find(self, doc: str, query: str) -> str:
        needle = verify.normalize(query)
        hits = [f"page {p.number}: {line.strip()[:220]}" for p in self.pages[doc]
                for line in extract.page_line_text(p).splitlines() if needle and needle in verify.normalize(line)]
        return "\n".join(hits[:40]) or "nothing found"


def run_tool(tool: str, args: dict, documents: Documents, kb: KnowledgeBase) -> str:
    """One tool call -> the observation text. A bad call is answered with an explanation the model can act on."""
    doc = args.get("doc")
    if tool in ("list_pages", "read_page", "find") and doc not in documents.pages:
        return f"error: doc must be one of {sorted(documents.pages)}"
    if tool == "list_pages":
        return documents.list_pages(str(doc))
    if tool == "read_page":
        page = args.get("page")
        if not isinstance(page, int) or not 1 <= page <= len(documents.pages[str(doc)]):
            return f"error: page must be a number from 1 to {len(documents.pages[str(doc)])}"
        text = documents.text(str(doc), page)
        return text[:OBSERVATION_CHARS] + (f"\n[cut: the page has {len(text)} characters; use find for the rest]" if len(text) > OBSERVATION_CHARS else "")
    if tool == "find":
        return documents.find(str(doc), str(args.get("query") or ""))
    if tool == "lookup_norm":
        hits = kb.search(str(args.get("query") or ""), k=4)
        return "\n".join(f"{h.designation or h.doc_id}, {h.ref}: {h.text[:500]}" for h in hits) or "nothing found"
    if tool == "calc":
        expression = str(args.get("expression") or "")
        if not re.fullmatch(r"[\d\s.,+\-*/()]+", expression) or expression.count("(") != expression.count(")") \
                or re.search(r"[+\-*/]\s*[*/]|[+\-*/]\s*$|/\s*0+(?:[.,]0*)?(?!\d|[.,]\d)", expression):
            return "error: only numbers, + - * / and brackets; no division by zero"
        return str(calculate(expression))
    return "error: unknown tool"


# ---------- the loop ----------
def run(m: llm.Model, conclusion: Path, project: Path | None, kb: KnowledgeBase, raw_dir: Path) -> dict:
    """Let the model work until it calls finish (or the step limit). Returns the steps and the checked answer."""
    documents = Documents(conclusion, project)
    steps: list[dict] = []
    answer: dict | None = None
    for number in range(1, MAX_STEPS + 1):
        history = "\n".join(
            f"STEP {s['n']}: {json.dumps({'thought': s['thought'], 'tool': s['tool'], 'args': s['args']}, ensure_ascii=False)}\n"
            f"RESULT {s['n']}: {s['observation'] if s['n'] >= number - 2 else s['observation'][:REMEMBERED_CHARS]}"
            for s in steps)
        user = (f"Documents: {', '.join(documents.pages)}.\n" + (history + "\n" if history else "")
                + f"This is step {number} of at most {MAX_STEPS}. Reply with the next JSON action.")
        response = llm.ask(m, SYSTEM, user, SCHEMA, 2500, raw_dir)
        action = response.data
        if action["tool"] == "finish":
            answer = action["args"].get("answer") if isinstance(action["args"].get("answer"), dict) else action["args"]
            steps.append({"n": number, **action, "observation": "finished", "seconds": response.duration_s,
                          "tokens": response.completion_tokens})
            break
        steps.append({"n": number, **action, "observation": run_tool(action["tool"], action["args"], documents, kb),
                      "seconds": response.duration_s, "tokens": response.completion_tokens})
    return {"model": m.name, "steps": steps, "answer": answer, "checked": check_answer(answer, documents) if answer else None}


def _checked(value: object, doc: object, page: object, snippet: object, numeric: bool, documents: Documents) -> dict:
    """The same source check as in the pipeline: the snippet is on that page and the value is in the snippet."""
    if doc not in documents.pages or not isinstance(page, int) or not 1 <= page <= len(documents.pages[str(doc)]):
        return {"value": value, "status": verify.UNVERIFIED, "reason": "sahifa ko'rsatilmagan yoki noto'g'ri"}
    texts = [extract.page_line_text(documents.pages[str(doc)][page - 1]), documents.text(str(doc), page)]
    status, reason, number = verify.verify_source(None if value is None else str(value), None if snippet is None else str(snippet),
                                                  texts, numeric)
    return {"value": value, "status": status, "reason": reason, "number": None if number is None else str(number),
            "doc": doc, "page": page, "snippet": snippet}


def check_answer(answer: dict, documents: Documents) -> dict:
    buildings = [{"name": b.get("name"), "count": b.get("count"),
                  **_checked(b.get("volume"), b.get("doc"), b.get("page"), b.get("snippet"), True, documents)}
                 for b in answer.get("buildings") or [] if isinstance(b, dict)]
    accepted = [b for b in buildings if b["status"] == verify.ACCEPTED]
    total = sum((Decimal(b["number"]) * (verify.parse_number(str(b["count"])) or Decimal(1)) for b in accepted), Decimal(0))
    out = {"buildings": buildings, "hb_claimed": answer.get("hb"),
           "hb_from_verified_buildings": str(total) if buildings and len(accepted) == len(buildings) else None,
           "notes": answer.get("notes")}
    for key in ("construction_type", "object"):
        v = answer.get(key) if isinstance(answer.get(key), dict) else {}
        out[key] = _checked(v.get("value"), "xulosa", v.get("page"), v.get("snippet"), False, documents)
    return out
