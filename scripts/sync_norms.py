"""Fill the local normative knowledge base from the sources in feecalc/norm_sources.json.

Each source is downloaded once (raw file plus a record with url, time and sha256 under --raw),
split into clauses and stored in the knowledge base file. Only a summary table is printed; the
text of a document never goes to the terminal. A failed download or an empty parse stops the run.

Run: python scripts/sync_norms.py [--only ID ...] [--refresh] [--db data/norms/knowledge.db] [--raw data/norms/raw]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from feecalc import knowledge


def main() -> None:
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--only", nargs="*", metavar="ID", help="sync only these sources")
    a.add_argument("--refresh", action="store_true", help="download again even when a raw file is cached")
    a.add_argument("--db", type=Path, default=knowledge.DEFAULT_DB)
    a.add_argument("--raw", type=Path, default=knowledge.RAW_DIR)
    arg = a.parse_args()
    sources = knowledge.load_sources()
    unknown = sorted(set(arg.only or []) - {s.id for s in sources})
    if unknown:
        raise SystemExit(f"unknown source id: {', '.join(unknown)}; known: {', '.join(s.id for s in sources)}")
    kb = knowledge.KnowledgeBase(arg.db)
    print(f"{'document':<26} {'kind':<8} {'clauses':>8} {'characters':>11}  designation")
    total_clauses = total_characters = 0
    for source in sources:
        if arg.only and source.id not in arg.only:
            continue
        path = knowledge.fetch(source, arg.raw, refresh=arg.refresh)
        clauses = knowledge.parse(source, path)
        if not clauses:
            raise RuntimeError(f"{source.id}: no clauses parsed from {path} ({source.url})")
        record = knowledge.fetch_record(source, arg.raw)
        kb.add_document(knowledge.Document(source.id, source.title, source.kind, source.url, source.language,
                                           source.designation, record["fetched_at"], record["sha256"]), clauses)
        characters = sum(len(c.text) for c in clauses)
        total_clauses += len(clauses)
        total_characters += characters
        print(f"{source.id:<26} {source.kind:<8} {len(clauses):>8} {characters:>11}  {source.designation}", flush=True)
    print(f"{'total':<26} {'':<8} {total_clauses:>8} {total_characters:>11}  -> {arg.db}")


if __name__ == "__main__":
    main()
