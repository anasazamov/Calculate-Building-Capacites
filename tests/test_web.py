"""Web interface tests. The pipeline replays recorded model responses (see test_pipeline.py)."""
import shutil
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from feecalc import llm
from feecalc.web.app import create_app, money

RAW = Path(__file__).parent / "data" / "raw"
CALC = {"kt": "new", "ko": "other", "kz": "500k-1m", "kz_value": "", "kj": "other", "k_council": "1", "bhm": "440000"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(llm, "server_ready", lambda m: m.key == "model1")
    monkeypatch.setattr(llm, "port_open", lambda m: m.key == "model1")
    monkeypatch.setattr(llm, "vllm_version", lambda m: "replay")
    c = TestClient(create_app(tmp_path, start_worker=False), follow_redirects=False)
    c.data_dir = tmp_path  # type: ignore[attr-defined]
    return c


def login(c: TestClient, name: str = "Sinov Xodim") -> None:
    assert c.post("/whoami", data={"name": name, "next": "/"}).status_code == 303


def upload(c: TestClient, sample_dir: Path, replay: bool = True) -> int:
    with open(sample_dir / "demo_project.pdf", "rb") as p, open(sample_dir / "demo_conclusion.pdf", "rb") as k:
        r = c.post("/jobs", data={"title": "Sinov"}, files=[("project", ("demo_project.pdf", p, "application/pdf")),
                                                             ("conclusion", ("demo_conclusion.pdf", k, "application/pdf"))])
    assert r.status_code == 303
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    if replay:
        shutil.copytree(RAW, c.data_dir / "jobs" / str(job_id) / "raw")  # type: ignore[attr-defined]
    c.app.state.run_job(job_id)  # type: ignore[attr-defined]
    return job_id


def test_money_format():
    assert money("4294208160") == "4 294 208 160" and money("880000") == "880 000" and money("12.5") == "12,5"


def test_upload_requires_staff_name_and_pdf(client: TestClient, sample_dir: Path):
    with open(sample_dir / "demo_project.pdf", "rb") as p:
        files = [("project", ("a.pdf", p, "application/pdf")), ("conclusion", ("b.pdf", b"x", "application/pdf"))]
        assert client.post("/jobs", files=files).status_code == 400
    login(client)
    files = [("project", ("a.txt", b"x", "text/plain")), ("conclusion", ("b.pdf", b"x", "application/pdf"))]
    assert client.post("/jobs", files=files).status_code == 400


def test_full_flow(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    assert client.get(f"/jobs/{job}/status").json() == {"status": "ready", "stage": "done", "error": ""}
    page = client.get(f"/jobs/{job}/review")
    assert page.status_code == 200
    for text in ("36036", "Наманган", "Samarqand", "Tasdiqlanmagan hajmlar bor", "matnsiz", "Tip-1"):
        assert text in page.text
    project_file = "01_project_demo_project.pdf"
    assert client.get(f"/jobs/{job}/page-image", params={"file": project_file, "page": 1}).headers[
        "content-type"] == "image/png"
    rects = client.get(f"/jobs/{job}/rects", params={"file": project_file, "page": 1,
                                                     "snippet": "Строительный объём | м3 | 36036"}).json()
    assert len(rects["rects"]) >= 3 and rects["width"] > 1000
    assert client.get(f"/jobs/{job}/rects", params={"file": "x.pdf", "page": 1}).status_code == 404

    # finalize is refused until every volume is confirmed
    assert client.post(f"/jobs/{job}/finalize", data=CALC).status_code == 409
    preview = client.post(f"/jobs/{job}/calc/preview", data=CALC).json()
    assert preview["result"] is None and set(preview["missing"]) == {"Hb", "Hyu", "Ha", "Ht", "Hu"}

    # Hb is the sum over the conclusion's buildings; staff leaves the other building out (reason required)
    from feecalc import pipeline
    state = client.app.state.store  # type: ignore[attr-defined]
    view = pipeline.assess(state.job(job)["result"])
    assert view["volumes"]["Hb"]["proposal"] == "88433.2"
    other = view["hb_rows"][1]["id"]
    assert client.post(f"/jobs/{job}/component/{other}", data={"included": "no", "reason": ""}).status_code == 400
    assert client.post(f"/jobs/{job}/component/{other}",
                       data={"included": "no", "reason": "Bu ish faqat Tip-2 uchun"}).status_code == 303
    assert client.post(f"/jobs/{job}/volume/Hb/confirm").status_code == 303
    assert client.post(f"/jobs/{job}/volume/Ha/confirm").status_code == 303
    assert client.post(f"/jobs/{job}/volume/Ht/confirm").status_code == 409          # no proposal
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "168.32", "reason": ""}).status_code == 400
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "abc", "reason": "sabab"}).status_code == 400
    assert client.post(f"/jobs/{job}/volume/Ht/change",
                       data={"value": "168.32", "reason": "Loyihachi hisobi"}).status_code == 303
    assert client.post(f"/jobs/{job}/volume/Hyu/confirm").status_code == 409         # floors not entered
    assert client.post(f"/jobs/{job}/floors", data={"permitted_floors": "16"}).status_code == 303
    assert client.post(f"/jobs/{job}/volume/Hyu/confirm").status_code == 303
    assert client.post(f"/jobs/{job}/volume/Hu/confirm").status_code == 303

    # changing a component afterwards makes the confirmed Hu stale
    view = pipeline.assess(state.job(job)["result"], {}, 16, None)
    group = [c for c in view["components"] if c["category"] == "Hu" and c["origin"] == "breakdown"][0]
    url = f"/jobs/{job}/component/{group['id']}"
    assert client.post(url, data={"height": "3.25", "reason": ""}).status_code == 400
    assert client.post(url, data={"reason": "faqat sabab"}).status_code == 400
    assert client.post(url, data={"height": "3.25", "reason": "Kesim bo'yicha 3.25 m"}).status_code == 303
    assert "taklif o'zgardi" in unescape(client.get(f"/jobs/{job}/review").text)
    assert client.post(f"/jobs/{job}/finalize", data=CALC).status_code == 409
    assert client.post(f"/jobs/{job}/volume/Hu/confirm").status_code == 303

    # control example 4 from TASK.md: 4 294 208 160 / 858 841 632 / 3 435 366 528
    result = client.post(f"/jobs/{job}/calc/preview", data=CALC).json()["result"]
    assert (result["fee"], result["advance_20"], result["remainder_80"]) == ("4294208160", "858841632", "3435366528")
    bad = client.post(f"/jobs/{job}/calc/preview", data={**CALC, "kz": "1m+", "kz_value": "3"})
    assert bad.status_code == 422 and "oralig'ida emas" in bad.json()["detail"]
    industrial = client.post(f"/jobs/{job}/calc/preview", data={**CALC, "ko": "industrial"}).json()
    assert industrial["industrial"]["rows"] and industrial["coefficients"]["ko"] == "0.5"
    assert client.get(f"/jobs/{job}/calc").status_code == 200

    assert client.post(f"/jobs/{job}/match", data={"project_index": "0", "conclusion_index": "0",
                                                   "reason": "Albom Tip-2 uchun"}).status_code == 303
    assert client.post(f"/jobs/{job}/match", data={"project_index": "5", "conclusion_index": "0",
                                                   "reason": "sinov"}).status_code == 400
    assert client.post(f"/jobs/{job}/finalize", data=CALC).status_code == 303
    sheet = client.get(f"/jobs/{job}/sheet.json").json()
    assert sheet["fee"]["result"]["fee"] == "4294208160" and sheet["finalized"]["by"] == "Sinov Xodim"
    assert sheet["models"]["model1"]["vllm_version"] == "replay" and sheet["prompt_hashes"]
    actions = [a["action"] for a in sheet["audit"]]
    for action in ("job_created", "volume_confirmed", "volume_changed", "component_changed",
                   "permitted_floors_set", "building_matched", "finalized"):
        assert action in actions
    assert all(a["time"] and a["user"] == "Sinov Xodim" for a in sheet["audit"])
    html = unescape(client.get(f"/jobs/{job}/sheet").text)
    assert "4 294 208 160" in html and "Kesim bo'yicha 3.25 m" in html
    # a finalized job is locked
    assert client.post(f"/jobs/{job}/volume/Ha/change", data={"value": "1", "reason": "sinov"}).status_code == 409
    csv_text = client.get("/export.csv").text
    assert "4294208160" in csv_text and "168.32" in csv_text
    assert "4 294 208 160" in client.get("/").text


def test_negative_chargeable_volume_is_an_error(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    client.post(f"/jobs/{job}/floors", data={"permitted_floors": "16"})
    for code, value in (("Hb", "100"), ("Ha", "200"), ("Ht", "0"), ("Hu", "0"), ("Hyu", "0")):
        assert client.post(f"/jobs/{job}/volume/{code}/change", data={"value": value, "reason": "sinov"}).status_code == 303
    r = client.post(f"/jobs/{job}/calc/preview", data=CALC)
    assert r.status_code == 422 and "manfiy" in r.json()["detail"]
    assert client.post(f"/jobs/{job}/finalize", data=CALC).status_code == 422


def test_mutations_require_staff_name(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    client.cookies.clear()
    assert client.post(f"/jobs/{job}/volume/Hb/confirm").status_code == 400
    assert client.post(f"/jobs/{job}/floors", data={"permitted_floors": "9"}).status_code == 400


def test_failed_job_shows_full_error_and_can_retry(client: TestClient, sample_dir: Path, monkeypatch):
    login(client)
    monkeypatch.setattr(llm, "server_ready", lambda m: False)
    job = upload(client, sample_dir, replay=False)
    status = client.get(f"/jobs/{job}/status").json()
    assert status["status"] == "failed"
    for part in ("Bosqich: ingest", "Traceback", "ModelError", "pipeline.py", "tayyor emas"):
        assert part in status["error"]
    assert client.get(f"/jobs/{job}/review").status_code == 409
    assert client.post(f"/jobs/{job}/retry").status_code == 303
    assert client.get(f"/jobs/{job}/status").json()["status"] == "queued"


def test_whoami_rejects_external_redirect(client: TestClient):
    assert client.post("/whoami", data={"name": "A", "next": "//evil.example"}).status_code == 400


def test_conclusion_only_upload(client: TestClient, sample_dir: Path):
    login(client)
    with open(sample_dir / "demo_conclusion.pdf", "rb") as k:
        r = client.post("/jobs", data={"title": "Faqat xulosa"},
                        files=[("conclusion", ("demo_conclusion.pdf", k, "application/pdf"))])
    assert r.status_code == 303
    job = int(r.headers["location"].rsplit("/", 1)[1])
    shutil.copytree(RAW, client.data_dir / "jobs" / str(job) / "raw")  # type: ignore[attr-defined]
    client.app.state.run_job(job)  # type: ignore[attr-defined]
    assert client.get(f"/jobs/{job}/status").json()["status"] == "ready"
    page = unescape(client.get(f"/jobs/{job}/review").text)
    assert "Loyiha berilmagan" in page and "Tip-1" in page and "88433,2" in page


def test_password_gate(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path, start_worker=False, password="correct horse battery"), follow_redirects=False)
    assert c.get("/").status_code == 401
    assert c.get("/static/app.css").status_code == 401
    assert c.get("/", auth=("x", "wrong")).status_code == 401
    assert c.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert c.get("/", auth=("anyone", "correct horse battery")).status_code in (200, 303)
