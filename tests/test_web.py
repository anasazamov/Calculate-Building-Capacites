"""Web interface tests. The pipeline replays recorded model responses (see test_pipeline.py)."""
import re
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


def review_all(c: TestClient, job: int) -> int:
    """Mark every project-versus-conclusion difference of a job as checked; returns how many there were."""
    keys = re.findall(rf'action="/jobs/{job}/difference/([0-9a-f]+)"><button', c.get(f"/jobs/{job}/review").text)
    for key in keys:
        assert c.post(f"/jobs/{job}/difference/{key}").status_code == 303
    return len(keys)


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
    # every volume is confirmed, but the project and the conclusion differ: approval waits for staff to check them
    blocked = client.post(f"/jobs/{job}/finalize", data=CALC)
    assert blocked.status_code == 409 and "farq tekshirilmagan" in blocked.json()["detail"]
    assert review_all(client, job) >= 1
    assert client.post(f"/jobs/{job}/finalize", data=CALC).status_code == 303
    sheet = client.get(f"/jobs/{job}/sheet.json").json()
    assert sheet["fee"]["result"]["fee"] == "4294208160" and sheet["finalized"]["by"] == "Sinov Xodim"
    assert sheet["differences"] and all(d["review"]["user"] == "Sinov Xodim" for d in sheet["differences"])
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
    assert "Hajmlar holati" in page and "tasdiqlanmagan" in page
    assert page.index('id="todo"') < page.index('id="vol-Hb"') < page.index('id="fields"')
    from feecalc import pipeline
    from feecalc.web.app import attention
    view = pipeline.assess(client.app.state.store.job(job)["result"])  # type: ignore[attr-defined]
    items = attention(view)
    assert not any(i["anchor"] == "f-s1" and i["kind"] == "mismatch" and "loyiha va xulosa" in i["text"] for i in items)
    assert all(f'id="{i["anchor"]}"' in page for i in items)                           # every item has its row
    assert [i["kind"] in ("conflict", "mismatch") for i in items] == sorted(
        [i["kind"] in ("conflict", "mismatch") for i in items], reverse=True)            # the serious ones first
    # the address differs between the documents: it is in the differences list, with the differing words marked
    assert "Loyiha va xulosa farqlari" in page and page.index('id="differences"') < page.index('id="vol-Hb"')
    from feecalc.web.app import differences
    found = differences(view, {})
    address = [d for d in found if d["label"] == "Manzil"][0]
    assert address["words"] and any(changed for _w, changed in address["words"][0]) and address["anchor"] == "f-s1"


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
    # the order of work is shown step by step, each with its tool
    for text in ("Tizim qanday hisobladi", "Hujjatlarni ochish", "Har qiymatni manbasi bilan tekshirish",
                 "Hajmlar bo'yicha qaror", "kod (Decimal)"):
        assert text in page
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
    review_all(client, job)
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
    review_all(client, job)
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
    review_all(client, job)
    assert client.post(f"/jobs/{job}/approve", data={**CALC, "contract_number": "ART-1"}).status_code == 303
    filled = client.get(f"/jobs/{job}/contract.docx")
    assert filled.status_code == 200
    (tmp_path / "out.docx").write_bytes(filled.content)
    text = Document(str(tmp_path / "out.docx")).paragraphs[0].text
    fee_value = client.get(f"/jobs/{job}/sheet.json").json()["fee"]["result"]["fee"]
    from feecalc import contract
    assert "ART-1" in text and contract.money(fee_value) in text and contract.in_words(int(fee_value)) in text and "{{" not in text


def test_contract_designer_from_upload_to_a_filled_contract(client: TestClient, sample_dir: Path, tmp_path: Path):
    import pymupdf
    login(client)
    assert "Hali namuna yuklanmagan" in unescape(client.get("/contract-designer").text)
    assert client.get("/contract-designer/preview.pdf").status_code == 404
    doc = pymupdf.open()
    doc.new_page().insert_text((60, 80), "Shartnoma namunasi", fontsize=12)
    doc.save(str(tmp_path / "namuna.pdf"))
    assert client.post("/contract-designer/pdf", files={"template": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    with open(tmp_path / "namuna.pdf", "rb") as f:
        assert client.post("/contract-designer/pdf", files={"template": ("namuna.pdf", f, "application/pdf")}).status_code == 303
    page = unescape(client.get("/contract-designer").text)
    assert 'data-field="yigim"' in page and "/contract-designer/page/1.png" in page
    assert client.get("/contract-designer/page/1.png").headers["content-type"] == "image/png"
    assert client.get("/contract-designer/page/9.png").status_code == 404
    job = upload(client, sample_dir)
    review_all(client, job)
    assert client.post(f"/jobs/{job}/approve", data={**CALC, "contract_number": "ART-7"}).status_code == 303
    assert client.get(f"/jobs/{job}/contract.pdf").status_code == 409                    # no field placed yet
    bad = client.post("/contract-designer/layout", json=[{"field": "nomalum", "page": 1, "x": 1, "y": 1, "w": 50, "h": 10}])
    assert bad.status_code == 400
    saved = client.post("/contract-designer/layout", json=[
        {"field": "shartnoma_raqami", "page": 1, "x": 200, "y": 68, "w": 150, "h": 16, "size": 12, "bold": True, "align": "left", "cover": True},
        {"field": "yigim", "page": 1, "x": 60, "y": 120, "w": 200, "h": 16, "size": 11, "bold": False, "align": "left", "cover": False}])
    assert saved.json() == {"saved": 2}
    assert "«yig'im summasi, raqam bilan»" in pymupdf.open(stream=client.get("/contract-designer/preview.pdf").content, filetype="pdf")[0].get_text()
    text = pymupdf.open(stream=client.get(f"/jobs/{job}/contract.pdf").content, filetype="pdf")[0].get_text()
    fee_value = client.get(f"/jobs/{job}/sheet.json").json()["fee"]["result"]["fee"]
    from feecalc import contract
    assert "ART-7" in text and contract.money(fee_value) in text and "Shartnoma namunasi" in text
    assert "Shartnoma (PDF)" in unescape(client.get(f"/jobs/{job}/sheet").text)


def test_a_room_name_decided_once_applies_to_every_job(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    from feecalc import classify, pipeline
    result = client.app.state.store.job(job)["result"]  # type: ignore[attr-defined]
    rooms = [r["name"] for r in result["project"]["model1"]["rooms"]] + [b["name"] for b in result["project"]["model1"]["area_breakdown"]]
    unknown = [n for n in rooms if classify.room_category(n).category is None]
    page = unescape(client.get("/reference").text)
    assert "Xona nomlari" in page
    if unknown:                                                    # the demo album has names outside the dictionary
        name = unknown[0]
        assert name in page
        assert client.post("/reference/room-name", data={"name": name, "category": "Ht", "basis": ""}).status_code == 400
        assert client.post("/reference/room-name", data={"name": name, "category": "Ht", "basis": "Loyihachi izohi"}).status_code == 303
        names = {r["key"]: r["value"] for r in client.app.state.store.reference_rows() if r["kind"] == "room_name"}  # type: ignore[attr-defined]
        view = pipeline.assess(result, room_names=names)
        row = [c for c in view["components"] if c["name"] == name][0]
        assert (row["category"], row["category_method"], row["needs_confirmation"]) == ("Ht", "reference", False)
        assert pipeline.assess(result)["components"][view["components"].index(row)]["category"] == "other"


def test_judgment_stage_can_be_run_again_without_reading(client: TestClient, sample_dir: Path):
    login(client)
    job = upload(client, sample_dir)
    before = client.app.state.store.job(job)["result"]  # type: ignore[attr-defined]
    assert set(before["judgments"]) >= {"structures", "rooms", "sheets"}
    assert client.post(f"/jobs/{job}/rejudge").status_code == 303
    assert client.get(f"/jobs/{job}/status").json()["status"] == "queued"
    client.app.state.run_job(-job)  # type: ignore[attr-defined]
    after = client.app.state.store.job(job)  # type: ignore[attr-defined]
    assert after["status"] == "ready" and after["result"]["judgments"]["structures"] == before["judgments"]["structures"]
    assert after["result"]["conclusion"] == before["conclusion"]                 # the reading itself is untouched



def test_differences_are_shown_side_by_side_and_checked_one_by_one(client: TestClient, sample_dir: Path):
    from feecalc.web.app import word_diff
    left, right = word_diff("дом №11, ул. Майсазор, город Самарканд", "Samarqand shahar Maysazor ko'chasi 11-uy")
    assert ("Майсазор,", False) in left and ("Maysazor", False) in right           # the same word in two scripts
    assert any(changed for _w, changed in left) and any(changed for _w, changed in right)
    login(client)
    job = upload(client, sample_dir)
    page = unescape(client.get(f"/jobs/{job}/review").text)
    keys = re.findall(rf'action="/jobs/{job}/difference/([0-9a-f]+)"><button', page)
    assert keys and 'class="w-diff"' in page
    proposal = unescape(client.get(f"/jobs/{job}/proposal").text)
    assert "tekshirilmagan" in proposal and "Farqlarni ko'rish" in proposal
    r = client.post(f"/jobs/{job}/difference/{keys[0]}", data={"note": "loyiha to'g'ri"})
    assert r.status_code == 303 and r.headers["location"].endswith(f"#d-{keys[0]}")
    assert f"{1} / {len(keys)} tekshirildi" in unescape(client.get(f"/jobs/{job}/review").text)
    assert client.post(f"/jobs/{job}/difference/{keys[0]}", data={"undo": "1"}).status_code == 303
    assert f"0 / {len(keys)} tekshirildi" in unescape(client.get(f"/jobs/{job}/review").text)
    assert client.post(f"/jobs/{job}/difference/0000000000000000").status_code == 404
    actions = [a["action"] for a in client.app.state.store.audit_log(job)]  # type: ignore[attr-defined]
    assert "difference_reviewed" in actions and "difference_unreviewed" in actions



def test_numeric_difference_written_with_a_comma_is_shown(client: TestClient, sample_dir: Path):
    """Values are shown as the document writes them ("251,37"); the delta is computed from the parsed numbers."""
    from feecalc.web.app import differences
    row = {"id": "x1", "name": "Hojatxona", "count": "21", "check": {"status": "mismatch"},
           "project": {"value": "251,37", "number": "251.37", "status": "accepted", "file": "a.pdf", "page": 3},
           "conclusion": {"value": "10,2", "number": "10.2", "status": "accepted", "file": "b.pdf", "page": 3}}
    found = differences({"scalar_rows": [], "buildings": [], "hb_rows": [row]}, {})[0]
    assert found["project"] == "251,37" and found["conclusion"] == "21 × 10,2 = 214.2" and found["delta"] == "-37.17"
    login(client)
    job = upload(client, sample_dir)
    store = client.app.state.store  # type: ignore[attr-defined]
    result = store.job(job)["result"]
    result["conclusion"]["model1"]["buildings"][0]["volume"]["value"] = "88 433,2"   # a comma, as a document writes it
    store.set_job(job, result=result)
    assert client.get(f"/jobs/{job}/review").status_code == 200
