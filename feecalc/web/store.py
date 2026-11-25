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
    finalized_at TEXT,
    requests_total INTEGER,          -- reading requests planned for this job
    requests_done INTEGER,           -- reading requests finished so far
    started_at TEXT,                 -- when the worker started (or restarted) reading
    client TEXT,                     -- integration client that created the job through the API (NULL: the web UI)
    external_id TEXT                 -- the client's own identifier of this job (idempotent creation)
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
CREATE TABLE IF NOT EXISTS difference_review (
    job_id INTEGER NOT NULL REFERENCES job(id),
    key TEXT NOT NULL,               -- identifies the difference and both values (a new reading asks again)
    user TEXT NOT NULL,
    time TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, key)
);
CREATE TABLE IF NOT EXISTS reference (
    kind TEXT NOT NULL,              -- setting | zone | central_street
    key TEXT NOT NULL,               -- setting name | settlement | "settlement|street"
    value TEXT NOT NULL,
    basis TEXT NOT NULL DEFAULT '',  -- the decision or document the entry rests on
    user TEXT NOT NULL,
    time TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS reference_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,            -- set | delete
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    old TEXT NOT NULL DEFAULT '',
    new TEXT NOT NULL DEFAULT ''
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

# Columns added after the first release: (table, column, type). Applied to databases created before them.
MIGRATIONS = (("job", "requests_total", "INTEGER"), ("job", "requests_done", "INTEGER"), ("job", "started_at", "TEXT"),
              ("job", "client", "TEXT"), ("job", "external_id", "TEXT"))


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)
            for table, column, kind in MIGRATIONS:
                if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        with c:  # commits on success, rolls back on error
            yield c
        c.close()

    # ----- jobs -----
    def create_job(self, title: str, user: str, client: str | None = None, external_id: str | None = None) -> int:
        with self.connect() as c:
            cur = c.execute("INSERT INTO job (title, created, created_by, status, client, external_id) "
                            "VALUES (?, ?, ?, 'queued', ?, ?)", (title, now(), user, client, external_id))
            job_id = int(cur.lastrowid or 0)
        self.audit(job_id, user, "job_created", new=title)
        return job_id

    def job_by_external(self, client: str, external_id: str) -> int | None:
        with self.connect() as c:
            row = c.execute("SELECT id FROM job WHERE client = ? AND external_id = ? ORDER BY id LIMIT 1",
                            (client, external_id)).fetchone()
        return None if row is None else int(row["id"])

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
            rows = c.execute("SELECT id, title, created, created_by, status, stage, final, finalized_at, client, external_id "
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
                   "finalized_at", "requests_total", "requests_done", "started_at"}
        assert fields and set(fields) <= allowed, fields
        values = [json.dumps(v, ensure_ascii=False) if k in ("result", "calc", "final", "matches") and v is not None else v
                  for k, v in fields.items()]
        with self.connect() as c:
            c.execute(f"UPDATE job SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*values, job_id))

    def unfinished_job_ids(self) -> list[int]:
        with self.connect() as c:
            return [r["id"] for r in c.execute("SELECT id FROM job WHERE status IN ('queued', 'running') ORDER BY id")]

    def jobs_ahead(self, job_id: int) -> int:
        """Unfinished jobs that were uploaded before this one."""
        with self.connect() as c:
            row = c.execute("SELECT COUNT(*) FROM job WHERE status IN ('queued', 'running') AND id < ?", (job_id,))
            return int(row.fetchone()[0])

    def decision_counts(self) -> dict[int, int]:
        """Number of volumes with a staff decision, per job (for the job list)."""
        with self.connect() as c:
            return {int(r[0]): int(r[1]) for r in
                    c.execute("SELECT job_id, COUNT(*) FROM volume_decision GROUP BY job_id")}

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

    # ----- differences between project and conclusion that staff have looked at -----
    def reviewed_differences(self, job_id: int) -> dict[str, dict]:
        with self.connect() as c:
            return {r["key"]: dict(r) for r in c.execute("SELECT * FROM difference_review WHERE job_id = ?", (job_id,))}

    def review_difference(self, job_id: int, key: str, user: str, note: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO difference_review (job_id, key, user, time, note) VALUES (?, ?, ?, ?, ?) "
                      "ON CONFLICT (job_id, key) DO UPDATE SET user = excluded.user, time = excluded.time, note = excluded.note",
                      (job_id, key, user, now(), note))

    def unreview_difference(self, job_id: int, key: str) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM difference_review WHERE job_id = ? AND key = ?", (job_id, key))

    # ----- reference data kept by staff (central streets, zones, BHM, council coefficient) -----
    def reference_rows(self) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM reference ORDER BY kind, key")]

    def set_reference(self, kind: str, key: str, value: str, basis: str, user: str) -> None:
        with self.connect() as c:
            old = c.execute("SELECT value FROM reference WHERE kind = ? AND key = ?", (kind, key)).fetchone()
            c.execute("INSERT INTO reference (kind, key, value, basis, user, time) VALUES (?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT (kind, key) DO UPDATE SET value = excluded.value, basis = excluded.basis, "
                      "user = excluded.user, time = excluded.time", (kind, key, value, basis, user, now()))
            c.execute("INSERT INTO reference_log (time, user, action, kind, key, old, new) VALUES (?, ?, 'set', ?, ?, ?, ?)",
                      (now(), user, kind, key, old["value"] if old else "", value))

    def delete_reference(self, kind: str, key: str, user: str) -> None:
        with self.connect() as c:
            old = c.execute("SELECT value FROM reference WHERE kind = ? AND key = ?", (kind, key)).fetchone()
            c.execute("DELETE FROM reference WHERE kind = ? AND key = ?", (kind, key))
            c.execute("INSERT INTO reference_log (time, user, action, kind, key, old, new) VALUES (?, ?, 'delete', ?, ?, ?, '')",
                      (now(), user, kind, key, old["value"] if old else ""))

    def reference_log(self, limit: int = 40) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM reference_log ORDER BY id DESC LIMIT ?", (limit,))]

    # ----- audit -----
    def audit(self, job_id: int, user: str, action: str, target: str = "", old: str = "", new: str = "",
              reason: str = "") -> None:
        with self.connect() as c:
            c.execute("INSERT INTO audit (job_id, time, user, action, target, old, new, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (job_id, now(), user, action, target, old, new, reason))

    def audit_log(self, job_id: int) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit WHERE job_id = ? ORDER BY id", (job_id,))]
