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
    status = client.get(f"/jobs/{job}/status").json()
    assert (status["status"], status["stage"], status["error"], status["ahead"]) == ("ready", "done", "", 0)
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
    # Ht: the strict proposal abstains (a height is missing); the recommendation names what it left out
    assert "Tizim taxmini" in unescape(client.get(f"/jobs/{job}/review").text)
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "168.32", "reason": ""}).status_code == 400
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "abc", "reason": "sabab"}).status_code == 400
    assert client.post(f"/jobs/{job}/volume/Ht/change",
                       data={"value": "168.32", "reason": "Loyihachi hisobi"}).status_code == 303
    # Hyu: without permitted floors the recommendation is 0, stated as an assumption; entering the floors keeps it
    assert client.post(f"/jobs/{job}/volume/Hyu/confirm").status_code == 303
    assert client.post(f"/jobs/{job}/floors", data={"permitted_floors": "16"}).status_code == 303
    assert "Hyu" not in client.post(f"/jobs/{job}/calc/preview", data=CALC).json()["missing"]
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
    assert "Loyiha berilmagan" in page and "Tip-1" in page and "88 433,2" in page


def test_password_gate(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path, start_worker=False, password="correct horse battery"), follow_redirects=False)
    assert c.get("/").status_code == 401
    assert c.get("/static/app.css").status_code == 401
    assert c.get("/", auth=("x", "wrong")).status_code == 401
    assert c.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert c.get("/", auth=("anyone", "correct horse battery")).status_code in (200, 303)


def test_password_per_port(tmp_path: Path) -> None:
    app = create_app(tmp_path, start_worker=False, password="correct horse battery", port_passwords={80: "local"})
    c = TestClient(app, follow_redirects=False)  # the test client arrives on port 80
    assert c.get("/", auth=("x", "correct horse battery")).status_code == 401
    assert c.get("/", auth=("x", "local")).status_code in (200, 303)


def test_progress_is_reported_while_reading(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    store = client.app.state.store  # type: ignore[attr-defined]
    saved = store.job(job)
    assert saved["requests_total"] == saved["requests_done"] > 0 and saved["started_at"]   # every request reported
    store.set_job(job, status="running", stage="project", requests_done=1)
    state = client.get(f"/jobs/{job}/status").json()
    assert (state["requests_done"], state["requests_total"]) == (1, saved["requests_total"]) and state["elapsed_s"] >= 0
    store.set_job(job, status="queued")
    queued = client.get(f"/jobs/{job}/status").json()
    assert queued["ahead"] == 0 and queued["requests_total"] is None
    assert "O'qish" in unescape(client.get(f"/jobs/{job}").text)


def test_failed_job_explains_itself(client: TestClient, sample_dir: Path, monkeypatch):
    login(client)
    monkeypatch.setattr(llm, "server_ready", lambda m: False)
    job = upload(client, sample_dir, replay=False)
    state = client.get(f"/jobs/{job}/status").json()
    assert "modeli hozir ishlamayapti" in state["hint"] and "Traceback" in state["error"]


def test_browser_gets_a_readable_error_page_and_scripts_get_json(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    html = {"accept": "text/html,application/xhtml+xml"}
    page = client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "5", "reason": ""}, headers=html)
    assert page.status_code == 400 and "<h1>" in page.text and "Sababini yozish majburiy" in unescape(page.text)
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "5", "reason": ""}).json() == {
        "detail": "Sababini yozish majburiy"}
    missing = client.get("/no-such-page", headers=html)
    assert missing.status_code == 404 and "Sahifa topilmadi" in missing.text
    assert client.get("/jobs/999", headers=html).status_code == 404
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={}, headers=html).status_code == 422


def test_saving_leaves_a_one_time_confirmation(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    r = client.post(f"/jobs/{job}/volume/Hb/confirm")
    assert r.status_code == 303 and r.headers["location"].endswith("#vol-Hb")
    assert "flash=" in r.headers["set-cookie"] and "Hb%20tasdiqlandi" in r.headers["set-cookie"]


def test_review_starts_with_what_needs_attention(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    page = unescape(client.get(f"/jobs/{job}/review").text)
    assert "Sizdan nima kerak" in page and "tasdiqlash kerak" in page and "E'tibor bering" in page
    assert page.index("Sizdan nima kerak") < page.index('id="vol-Hb"') < page.index('id="fields"')
    from feecalc import pipeline
    from feecalc.web.app import attention
    view = pipeline.assess(client.app.state.store.job(job)["result"])  # type: ignore[attr-defined]
    items = attention(view)
    assert any(i["kind"] == "mismatch" and i["anchor"] == "f-s1" for i in items)      # the address differs
    assert all(f'id="{i["anchor"]}"' in page for i in items)                           # every item has its row
    assert [i["kind"] in ("conflict", "mismatch") for i in items] == sorted(
        [i["kind"] in ("conflict", "mismatch") for i in items], reverse=True)            # the serious ones first


def test_home_lists_jobs_with_the_next_action(client: TestClient, sample_dir: Path):
    assert "Hali birorta ish yuklanmagan" in unescape(client.get("/").text)
    login(client)
    job = upload(client, sample_dir)
    client.post(f"/jobs/{job}/volume/Hb/confirm")
    page = unescape(client.get("/").text)
    assert "tasdiqlash" in page and "5 ta hajmdan 1 tasi tasdiqlangan" in page and f'href="/jobs/{job}/proposal"' in page


def test_old_database_gets_the_new_columns(tmp_path: Path):
    import sqlite3
    from feecalc.web.store import Store
    db = tmp_path / "feecalc.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE job (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created TEXT NOT NULL, "
                  "created_by TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', "
                  "error TEXT NOT NULL DEFAULT '', result TEXT, permitted_floors INTEGER, matches TEXT, calc TEXT, "
                  "final TEXT, finalized_at TEXT)")
        c.execute("INSERT INTO job (title, created, created_by, status) VALUES ('eski', '2026-11-24 10:00:00', 'A', 'ready')")
    store = Store(db)
    job = store.job(1)
    assert job is not None and job["title"] == "eski" and job["requests_total"] is None
    store.set_job(1, requests_total=7, requests_done=2)
    assert store.job(1)["requests_total"] == 7 and store.jobs_ahead(1) == 0


# ---------- the system recommends, a person approves ----------
def test_proposal_page_shows_the_whole_recommendation(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    page = unescape(client.get(f"/jobs/{job}/proposal").text)
    for text in ("Tizim tavsiyasi", "Hajmlar", "Koeffitsiyentlar", "Xulosada qurilish turi", "Tasdiqlash va shartnomani shakllantirish"):
        assert text in page
    assert "sahifadan" in page and "tasi o'qildi" in page                  # how much of each document was read
    # no list of central streets has been entered: Kj is asked, the fee is not computed yet
    assert 'name="kj" required' in page and "markaziy qatnov ko'chalari ro'yxati ma'lumotnomaga kiritilmagan" in page
    assert "Yig'imni hisoblash uchun chap tomonda tanlang" in page
    # the demo conclusion has no "Obyekt nomi" line: the object type rests on the largest building, said as an assumption
    assert 'name="ko" value="other"' in page and "eng katta bino nomi" in page


def test_one_step_approval_finalizes_and_produces_the_contract_annex(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    form = {**CALC, "contract_number": "ART-11-26-90", "contract_date": "2026-11-25"}
    assert client.post(f"/jobs/{job}/approve", data={k: v for k, v in form.items() if k != "kj"}).status_code == 422
    r = client.post(f"/jobs/{job}/approve", data=form)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job}/sheet"
    sheet = client.get(f"/jobs/{job}/sheet.json").json()
    assert sheet["finalized"]["by"] == "Sinov Xodim" and sheet["contract"] == {"number": "ART-11-26-90", "date": "2026-11-25"}
    assert all(sheet["volumes"][c]["decision"]["status"] == "confirmed" for c in ("Hb", "Hyu", "Ha", "Ht", "Hu"))
    assert sheet["volumes"]["Hyu"]["decision"]["value"] == "0" and sheet["volumes"]["Hyu"]["assumptions"]
    assert sheet["recommendation"]["coefficients"]["kt"]["code"] == "new" and sheet["calc"]["kj"] == "other"
    confirmations = [a for a in sheet["audit"] if a["action"] == "volume_confirmed"]
    assert len(confirmations) == 5 and all("tizim tavsiyasi" in a["reason"] for a in confirmations)
    annex = client.get(f"/jobs/{job}/annex.docx")
    assert annex.status_code == 200 and annex.content[:2] == b"PK" and "wordprocessingml" in annex.headers["content-type"]
    assert client.get(f"/jobs/{job}/contract.docx").status_code == 409          # no template uploaded
    assert client.get(f"/jobs/{job}/proposal").status_code == 303               # a finalized job goes to its sheet
    assert client.post(f"/jobs/{job}/approve", data=form).status_code == 409    # and cannot be approved twice
    html = unescape(client.get(f"/jobs/{job}/sheet").text)
    assert "Hisoblash ma'lumotnomasi (Word)" in html and "Tavsiya asoslari" in html and "ART-11-26-90" in html


def test_staff_decisions_survive_approval(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    assert client.post(f"/jobs/{job}/volume/Ht/change", data={"value": "168.32", "reason": "Loyihachi hisobi"}).status_code == 303
    assert client.post(f"/jobs/{job}/approve", data=CALC).status_code == 303
    sheet = client.get(f"/jobs/{job}/sheet.json").json()
    assert sheet["volumes"]["Ht"]["decision"]["status"] == "changed" and sheet["volumes"]["Ht"]["decision"]["value"] == "168.32"


def test_reference_data_drives_the_recommendation(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    from feecalc.web.app import create_app  # noqa: F401  (the view is built by the app; read it through the page)
    assert client.post("/reference/streets", data={"settlement": "Samarqand", "streets": "Maysazor", "basis": "sinov"}).status_code == 400
    assert client.post("/reference/streets", data={"settlement": "Samarqand shahri", "streets": "Maysazor", "basis": ""}).status_code == 400
    saved = client.post("/reference/streets", data={"settlement": "Samarqand shahri", "streets": "Rudakiy ko'chasi\nMaysazor",
                                                    "basis": "Hokim qarori 12.01.2026 №5"})
    assert saved.status_code == 303
    page = unescape(client.get(f"/jobs/{job}/proposal").text)
    assert "markaziy qatnov ko'chalari ro'yxatida" in page and 'name="kj" value="central"' in page
    assert "Yig'im summasi" in page                                  # everything is decided: the fee is shown
    assert client.post("/reference/setting", data={"name": "bhm", "value": "450000", "basis": "sinov uchun"}).status_code == 303
    assert "450 000" in unescape(client.get(f"/jobs/{job}/proposal").text)
    assert client.post("/reference/setting", data={"name": "k_council", "value": "2", "basis": "sinov"}).status_code == 400
    assert client.post("/reference/zone", data={"settlement": "Samarqand shahri", "code": "100k-250k", "basis": "sinov"}).status_code == 303
    assert 'name="kz" value="100k-250k"' in client.get(f"/jobs/{job}/proposal").text
    ref = unescape(client.get("/reference").text)
    assert "Maysazor" in ref and "Hokim qarori 12.01.2026" in ref and "samarqand shahri" in ref
    assert client.post("/reference/delete", data={"kind": "central_street", "key": "samarqand shahri|maysazor"}).status_code == 303
    assert 'name="kj" value="other"' in client.get(f"/jobs/{job}/proposal").text   # the street is no longer on the list
    client.cookies.clear()
    assert client.post("/reference/delete", data={"kind": "zone", "key": "samarqand shahri"}).status_code == 400


def test_contract_template_is_checked_and_filled(client: TestClient, sample_dir: Path, tmp_path: Path):
    from docx import Document
    login(client)
    job = upload(client, sample_dir)
    good, bad = Document(), Document()
    good.add_paragraph("Shartnoma № {{shartnoma_raqami}}: yig'im {{yigim}} ({{ yigim_soz }}) so'm, Hb = {{hb}} m3.")
    bad.add_paragraph("Summa {{summa}}")
    good.save(str(tmp_path / "good.docx")); bad.save(str(tmp_path / "bad.docx"))
    with open(tmp_path / "bad.docx", "rb") as f:
        refused = client.post("/reference/template", files={"template": ("bad.docx", f, "application/octet-stream")})
    assert refused.status_code == 400 and "summa" in refused.json()["detail"]
    with open(tmp_path / "good.docx", "rb") as f:
        assert client.post("/reference/template", files={"template": ("good.docx", f, "application/octet-stream")}).status_code == 303
    assert client.post(f"/jobs/{job}/approve", data={**CALC, "contract_number": "ART-1"}).status_code == 303
    filled = client.get(f"/jobs/{job}/contract.docx")
    assert filled.status_code == 200
    (tmp_path / "out.docx").write_bytes(filled.content)
    text = Document(str(tmp_path / "out.docx")).paragraphs[0].text
    fee_value = client.get(f"/jobs/{job}/sheet.json").json()["fee"]["result"]["fee"]
    from feecalc import contract
    assert "ART-1" in text and contract.money(fee_value) in text and contract.in_words(int(fee_value)) in text and "{{" not in text
