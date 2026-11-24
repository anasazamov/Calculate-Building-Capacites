"""SQLite storage for jobs, files, staff decisions and the audit log."""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | ready | failed | finalized
    stage TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    result TEXT,                     -- JSON produced by pipeline.run_extraction
    permitted_floors INTEGER,
    matches TEXT,                    -- JSON: staff pairing of project buildings with conclusion buildings
    calc TEXT,                       -- JSON: coefficient choices
    final TEXT,                      -- JSON: finalized calculation sheet
    finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS file (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES job(id),
    role TEXT NOT NULL,              -- project | conclusion
    name TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS component_override (
    job_id INTEGER NOT NULL REFERENCES job(id),
    component_id TEXT NOT NULL,
    data TEXT NOT NULL,              -- JSON: category / height / volume / included / reason / user / time
    PRIMARY KEY (job_id, component_id)
);
CREATE TABLE IF NOT EXISTS volume_decision (
    job_id INTEGER NOT NULL REFERENCES job(id),
    code TEXT NOT NULL,              -- Hb | Hyu | Ha | Ht | Hu
    status TEXT NOT NULL,            -- confirmed | changed
    value TEXT NOT NULL,
    proposal TEXT,                   -- the proposal the decision was made against
    reason TEXT NOT NULL DEFAULT '',
    user TEXT NOT NULL,
    time TEXT NOT NULL,
    PRIMARY KEY (job_id, code)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES job(id),
    time TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    old TEXT NOT NULL DEFAULT '',
    new TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        with c:  # commits on success, rolls back on error
            yield c
        c.close()

    # ----- jobs -----
    def create_job(self, title: str, user: str) -> int:
        with self.connect() as c:
            cur = c.execute("INSERT INTO job (title, created, created_by, status) VALUES (?, ?, ?, 'queued')",
                            (title, now(), user))
            job_id = int(cur.lastrowid or 0)
        self.audit(job_id, user, "job_created", new=title)
        return job_id

    def add_file(self, job_id: int, role: str, name: str, path: Path) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO file (job_id, role, name, path) VALUES (?, ?, ?, ?)",
                      (job_id, role, name, str(path)))

    def job(self, job_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        for key in ("result", "calc", "final", "matches"):
            job[key] = json.loads(job[key]) if job[key] else None
        return job

    def jobs(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT id, title, created, created_by, status, stage, final, finalized_at "
                             "FROM job ORDER BY id DESC").fetchall()
        out = []
        for row in rows:
            job = dict(row)
            job["final"] = json.loads(job["final"]) if job["final"] else None
            out.append(job)
        return out

    def files(self, job_id: int) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM file WHERE job_id = ? ORDER BY id", (job_id,))]

    def set_job(self, job_id: int, **fields: object) -> None:
        allowed = {"status", "stage", "error", "result", "permitted_floors", "matches", "calc", "final",
                   "finalized_at"}
        assert fields and set(fields) <= allowed, fields
        values = [json.dumps(v, ensure_ascii=False) if k in ("result", "calc", "final", "matches") and v is not None else v
                  for k, v in fields.items()]
        with self.connect() as c:
            c.execute(f"UPDATE job SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*values, job_id))

    def unfinished_job_ids(self) -> list[int]:
        with self.connect() as c:
            return [r["id"] for r in c.execute("SELECT id FROM job WHERE status IN ('queued', 'running') ORDER BY id")]

    # ----- staff decisions -----
    def overrides(self, job_id: int) -> dict[str, dict]:
        with self.connect() as c:
            rows = c.execute("SELECT component_id, data FROM component_override WHERE job_id = ?", (job_id,))
            return {r["component_id"]: json.loads(r["data"]) for r in rows}

    def set_override(self, job_id: int, component_id: str, data: dict) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO component_override (job_id, component_id, data) VALUES (?, ?, ?) "
                      "ON CONFLICT (job_id, component_id) DO UPDATE SET data = excluded.data",
                      (job_id, component_id, json.dumps(data, ensure_ascii=False)))

    def decisions(self, job_id: int) -> dict[str, dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM volume_decision WHERE job_id = ?", (job_id,))
            return {r["code"]: dict(r) for r in rows}

    def set_decision(self, job_id: int, code: str, status: str, value: str, proposal: str | None,
                     reason: str, user: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO volume_decision (job_id, code, status, value, proposal, reason, user, time) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (job_id, code) DO UPDATE SET "
                      "status = excluded.status, value = excluded.value, proposal = excluded.proposal, "
                      "reason = excluded.reason, user = excluded.user, time = excluded.time",
                      (job_id, code, status, value, proposal, reason, user, now()))

    # ----- audit -----
    def audit(self, job_id: int, user: str, action: str, target: str = "", old: str = "", new: str = "",
              reason: str = "") -> None:
        with self.connect() as c:
            c.execute("INSERT INTO audit (job_id, time, user, action, target, old, new, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, now(), user, action, target, old, new, reason))

    def audit_log(self, job_id: int) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit WHERE job_id = ? ORDER BY id", (job_id,))]
