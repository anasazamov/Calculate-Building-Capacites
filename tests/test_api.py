"""Integration API: another system creates a job, polls it, reads the recommendation, approves, downloads."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from feecalc import llm
from feecalc.web.app import create_app

RAW = Path(__file__).parent / "data" / "raw"
KEY = {"X-API-Key": "billing-key-0123456789abcdef"}


@pytest.fixture()
def api(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(llm, "server_ready", lambda m: m.key == "model1")
    monkeypatch.setattr(llm, "port_open", lambda m: m.key == "model1")
    monkeypatch.setattr(llm, "vllm_version", lambda m: "replay")
    c = TestClient(create_app(tmp_path, start_worker=False, password="ui password for people",
                              api_keys={KEY["X-API-Key"]: "billing"}), follow_redirects=False)
    c.data_dir = tmp_path  # type: ignore[attr-defined]
    return c


def create(c: TestClient, sample_dir: Path, external_id: str = "ORD-1") -> dict:
    with open(sample_dir / "demo_project.pdf", "rb") as p, open(sample_dir / "demo_conclusion.pdf", "rb") as k:
        r = c.post("/api/v1/jobs", headers=KEY, data={"title": "API ishi", "external_id": external_id, "actor": "Operator"},
                   files=[("project", ("demo_project.pdf", p, "application/pdf")), ("conclusion", ("demo_conclusion.pdf", k, "application/pdf"))])
    assert r.status_code == 202
    return r.json()


def read(c: TestClient, job_id: int) -> None:
    shutil.copytree(RAW, c.data_dir / "jobs" / str(job_id) / "raw")  # type: ignore[attr-defined]
    c.app.state.run_job(job_id)  # type: ignore[attr-defined]


def test_key_is_required_and_separate_from_the_ui_password(api: TestClient, tmp_path: Path):
    assert api.get("/api/v1/health").status_code == 401
    assert api.get("/api/v1/health", headers={"X-API-Key": "wrong"}).status_code == 401
    assert api.get("/api/v1/health", headers=KEY).json()["model1"] is True
    assert api.get("/").status_code == 401                                       # the pages still ask for the password
    assert "/api/v1/jobs/{job_id}/recommendation" in api.get("/api/openapi.json").json()["paths"]
    closed = TestClient(create_app(tmp_path / "other", start_worker=False))
    assert closed.get("/api/v1/health", headers=KEY).status_code == 503         # no keys configured: the API is closed


def test_whole_journey_through_the_api(api: TestClient, sample_dir: Path):
    job = create(api, sample_dir)
    assert job["status"] == "queued" and job["external_id"] == "ORD-1" and job["created_by"] == "billing: Operator"
    assert create(api, sample_dir)["id"] == job["id"]                           # the same external id: the same job
    jid = job["id"]
    assert api.get(f"/api/v1/jobs/{jid}/recommendation", headers=KEY).status_code == 409      # not read yet
    read(api, jid)
    assert api.get(f"/api/v1/jobs/{jid}", headers=KEY).json()["status"] == "ready"
    rec = api.get(f"/api/v1/jobs/{jid}/recommendation", headers=KEY).json()
    assert set(rec["volumes"]) == {"Hb", "Hyu", "Ha", "Ht", "Hu"} and rec["volumes"]["Hb"]["value"] == "88433.2"
    assert rec["coefficients"]["kt"]["code"] == "new" and rec["coefficients"]["kj"]["code"] is None
    assert not rec["ready_to_approve"] and rec["fee"] is None and any(o["scope"] == "Kj" for o in rec["open"])
    assert rec["expertise"]["status"] in ("ok", "mismatch") and rec["expertise"]["compared"] > 0
    values = api.get(f"/api/v1/jobs/{jid}/values", headers=KEY).json()
    volume = [f for f in values["buildings"][0]["fields"] if f["conclusion"] and f["conclusion"]["page"]][0]
    assert volume["conclusion"]["snippet"] and volume["conclusion"]["file"].endswith(".pdf")
    assert api.post(f"/api/v1/jobs/{jid}/approve", headers=KEY, json={}).status_code == 409    # Kj is still open
    changed = api.put(f"/api/v1/jobs/{jid}/volumes/Ht", headers=KEY, json={"value": "168.32", "reason": "Loyihachi hisobi", "actor": "Operator"})
    assert changed.status_code == 200 and changed.json()["volumes"]["Ht"]["value"] == "168.32"
    assert api.put(f"/api/v1/jobs/{jid}/volumes/Ht", headers=KEY, json={"value": "1", "reason": ""}).status_code == 422
    blocked = api.post(f"/api/v1/jobs/{jid}/approve", headers=KEY, json={"kj": "other"})
    assert blocked.status_code == 409 and "farq" in blocked.json()["detail"]             # differences not checked yet
    for d in api.get(f"/api/v1/jobs/{jid}/recommendation", headers=KEY).json()["differences"]:
        assert d["project_source"] and api.put(f"/api/v1/jobs/{jid}/differences/{d['key']}", headers=KEY,
                                               json={"actor": "Operator", "note": "loyiha to'g'ri"}).status_code == 200
    sheet = api.post(f"/api/v1/jobs/{jid}/approve", headers=KEY,
                     json={"kj": "other", "contract_number": "ART-1", "actor": "Rahbar"})
    assert sheet.status_code == 200 and sheet.json()["finalized"]["by"] == "billing: Rahbar"
    assert sheet.json()["volumes"]["Ht"]["decision"]["value"] == "168.32" and sheet.json()["contract"]["number"] == "ART-1"
    assert api.get(f"/api/v1/jobs/{jid}/sheet", headers=KEY).json()["fee"]["result"]["fee"] == sheet.json()["fee"]["result"]["fee"]
    annex = api.get(f"/api/v1/jobs/{jid}/annex.docx", headers=KEY)
    assert annex.status_code == 200 and annex.content[:2] == b"PK"
    assert api.post(f"/api/v1/jobs/{jid}/approve", headers=KEY, json={}).status_code == 409    # finalized: no second approval
    assert [j["id"] for j in api.get("/api/v1/jobs", headers=KEY, params={"status": "finalized"}).json()] == [jid]
    page = api.get(f"/api/v1/jobs/{jid}/pages/{volume['conclusion']['file']}/{volume['conclusion']['page']}.png", headers=KEY)
    assert page.headers["content-type"] == "image/png"


def test_reference_data_through_the_api(api: TestClient, sample_dir: Path):
    job = create(api, sample_dir, "ORD-2")
    read(api, job["id"])
    saved = api.put("/api/v1/reference/central-streets", headers=KEY,
                    json={"settlement": "Samarqand shahri", "streets": ["Maysazor"], "basis": "Qaror №5, 12.01.2026"})
    assert saved.status_code == 200 and saved.json()["central_streets"]["samarqand shahri"][0]["street"] == "maysazor"
    rec = api.get(f"/api/v1/jobs/{job['id']}/recommendation", headers=KEY).json()
    assert rec["coefficients"]["kj"]["code"] == "central" and rec["fee"]["fee"]
    assert rec["differences"] and not rec["ready_to_approve"]                              # the differences come first
    for d in rec["differences"]:
        api.put(f"/api/v1/jobs/{job['id']}/differences/{d['key']}", headers=KEY, json={})
    assert api.get(f"/api/v1/jobs/{job['id']}/recommendation", headers=KEY).json()["ready_to_approve"]
    first = rec["differences"][0]["key"]
    undone = api.delete(f"/api/v1/jobs/{job['id']}/differences/{first}", headers=KEY, params={"actor": "Operator"})
    assert undone.status_code == 200 and not undone.json()["ready_to_approve"]
    assert api.delete(f"/api/v1/jobs/{job['id']}/differences/0000000000000000", headers=KEY).status_code == 404
    assert api.put("/api/v1/reference/central-streets", headers=KEY,
                   json={"settlement": "Samarqand", "streets": ["X"], "basis": "sinov"}).status_code == 400
