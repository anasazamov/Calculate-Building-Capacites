"""Web interface (FastAPI + Jinja2 + plain JS, SQLite). UI text is Uzbek (Latin).

Run: python -m feecalc.web   (listens on 127.0.0.1:8000 only)
"""
from __future__ import annotations

import csv
import io
import json
import os
import base64
import queue
import re
import secrets
import threading
import traceback
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import classify, extract, fee, ingest, llm, pipeline, verify, volumes
from .store import Store, now

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("FEECALC_DATA", "data"))


def money(v: object) -> str:
    """Whole numbers with thin-space thousands separators: 4294208160 -> 4 294 208 160."""
    s = fee.fmt_num(Decimal(str(v)))
    whole, _, frac = s.partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    grouped = " ".join(reversed([digits[max(0, i - 3):i] for i in range(len(digits), 0, -3)]))
    return sign + grouped + ("," + frac if frac else "")


def num(v: object) -> str:
    return "" if v is None else fee.fmt_num(Decimal(str(v))).replace(".", ",")


def password_ok(header: str, password: str) -> bool:
    """Check an HTTP Basic `Authorization` header; the user name is ignored."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not re.fullmatch(r"[A-Za-z0-9+/]+=*", token) or len(token) % 4:
        return False
    given = base64.b64decode(token).partition(b":")[2]
    return secrets.compare_digest(given, password.encode("utf-8"))


def create_app(data_dir: Path = DATA_DIR, start_worker: bool = True, password: str = "") -> FastAPI:
    app = FastAPI(title="Infratuzilma yig'imi", docs_url=None, redoc_url=None, openapi_url=None)
    if password:
        @app.middleware("http")
        async def require_password(request: Request, call_next):  # type: ignore[no-untyped-def]
            if password_ok(request.headers.get("authorization", ""), password):
                return await call_next(request)
            return Response("Parol kerak", status_code=401, headers={"WWW-Authenticate": 'Basic realm="feecalc"'})
    store = Store(data_dir / "feecalc.db")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["money"] = money
    templates.env.filters["num"] = num
    templates.env.filters["urlq"] = lambda s: quote(str(s), safe="")
    templates.env.globals.update(
        STATUS_LABELS=verify.STATUS_LABELS, CHECK_LABELS=verify.CHECK_LABELS, SHEET_LABELS=classify.SHEET_LABELS,
        CATEGORY_LABELS=classify.CATEGORY_LABELS, CATEGORIES=classify.CATEGORIES,
        VOLUME_LABELS=volumes.VOLUME_LABELS, VOLUME_CODES=volumes.VOLUME_CODES, STAGES=pipeline.STAGES,
        JOB_STATUS={"queued": "navbatda", "running": "bajarilmoqda", "ready": "tekshirishga tayyor",
                    "failed": "xato", "finalized": "yakunlangan"},
        AGREEMENT={"match": "mos", "mismatch": "MOS EMAS", "only_1": "faqat 1-model", "only_2": "faqat 2-model",
                   "neither": "ikkalasida yo'q"},
        FEE_GROUPS=fee.GROUPS, BHM_DEFAULT=fee.BHM_DEFAULT,
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    jobs: queue.Queue[int] = queue.Queue()
    app.state.store, app.state.queue = store, jobs

    # ---------- worker: the single job boundary ----------
    def process(job_id: int) -> None:
        files = store.files(job_id)
        project = [Path(f["path"]) for f in files if f["role"] == "project"]
        conclusion = [Path(f["path"]) for f in files if f["role"] == "conclusion"]
        store.set_job(job_id, status="running", stage="ingest", error="")
        result = pipeline.run_extraction(data_dir / "jobs" / str(job_id), project, conclusion[0],
                                         lambda stage: store.set_job(job_id, stage=stage))
        store.set_job(job_id, status="ready", stage="done", result=result)

    def run_job(job_id: int) -> None:
        # Job boundary: the only place where an exception is caught. The full error (type, file and
        # line, message, stage) is stored and shown; this job stops, the worker moves on.
        try:
            process(job_id)
        except Exception:  # noqa: BLE001
            job = store.job(job_id)
            stage = job["stage"] if job else "?"
            store.set_job(job_id, status="failed", error=f"Bosqich: {stage}\n\n{traceback.format_exc()}")

    def worker() -> None:
        while True:
            run_job(jobs.get())

    if start_worker:
        for job_id in store.unfinished_job_ids():
            jobs.put(job_id)
        threading.Thread(target=worker, daemon=True, name="feecalc-worker").start()
    app.state.run_job = run_job

    # ---------- helpers ----------
    def staff(request: Request) -> str:
        return unquote(request.cookies.get("staff", "")).strip()

    def require_staff(request: Request) -> str:
        name = staff(request)
        if not name:
            raise HTTPException(400, "Avval sahifa tepasida xodim ismini kiriting")
        return name

    def get_job(job_id: int, need_result: bool = False, editable: bool = False) -> dict:
        job = store.job(job_id)
        if job is None:
            raise HTTPException(404, "Bunday ish yo'q")
        if need_result and job["result"] is None:
            raise HTTPException(409, "Ish hali tayyor emas")
        if editable and job["status"] == "finalized":
            raise HTTPException(409, "Ish yakunlangan, o'zgartirib bo'lmaydi")
        return job

    def decimal_field(text: str, name: str, minimum: Decimal | None = None) -> Decimal:
        v = verify.parse_number(text)
        if v is None:
            raise HTTPException(400, f"{name}: son kiriting (berilgan: {text!r})")
        if minimum is not None and v < minimum:
            raise HTTPException(400, f"{name}: {minimum} dan kichik bo'lmasin")
        return v

    def reason_field(text: str) -> str:
        if len(text.strip()) < 3:
            raise HTTPException(400, "Sababini yozish majburiy")
        return text.strip()

    def build_view(job: dict) -> dict:
        """Assessment plus the state of staff decisions."""
        a = pipeline.assess(job["result"], store.overrides(job["id"]), job["permitted_floors"], job["matches"])
        decisions = store.decisions(job["id"])
        states: dict[str, dict] = {}
        for code in volumes.VOLUME_CODES:
            d, proposal = decisions.get(code), a["volumes"][code]["proposal"]
            stale = bool(d and d["status"] == "confirmed" and d["proposal"] != proposal)
            states[code] = {"decision": d, "stale": stale, "valid": bool(d) and not stale,
                            "value": d["value"] if d and not stale else None}
        a["decisions"] = states
        a["all_confirmed"] = all(s["valid"] for s in states.values())
        return a

    def calc_inputs(job: dict) -> dict:
        saved = job["calc"] or {}
        return {"kt": saved.get("kt", "new"), "ko": saved.get("ko", "other"), "kz": saved.get("kz", "500k-1m"),
                "kz_value": saved.get("kz_value", ""), "kj": saved.get("kj", "central"),
                "k_council": saved.get("k_council", "1"), "bhm": saved.get("bhm", str(fee.BHM_DEFAULT))}

    def compute_fee(view: dict, calc: dict) -> dict:
        """Fee from confirmed volumes and staff-chosen coefficients. FeeError -> HTTP 422 (handler below)."""
        missing = [c for c in volumes.VOLUME_CODES if not view["decisions"][c]["valid"]]
        kz_entered = verify.parse_number(calc["kz_value"]) if calc["kz_value"] else None
        coefficients = {
            "kt": fee.option_value("kt", calc["kt"]), "ko": fee.option_value("ko", calc["ko"]),
            "kz": fee.option_value("kz", calc["kz"], kz_entered), "kj": fee.option_value("kj", calc["kj"]),
            "k_council": decimal_field(calc["k_council"], "Kengash koeffitsiyenti"),
            "bhm": decimal_field(calc["bhm"], "BHM"),
        }
        out: dict = {"missing": missing, "coefficients": {k: str(v) for k, v in coefficients.items()},
                     "options": {g: fee.option(g, calc[g]).text for g in ("kt", "ko", "kz", "kj")},
                     "result": None, "industrial": None}
        if calc["ko"] == "industrial":
            out["industrial"] = view["industrial"]
        if missing:
            return out
        v = {c: Decimal(view["decisions"][c]["value"]) for c in volumes.VOLUME_CODES}
        r = fee.compute(fee.FeeInput(coefficients["bhm"], v["Hb"], v["Hyu"], v["Ha"], v["Ht"], v["Hu"],
                                     coefficients["kt"], coefficients["ko"], coefficients["kz"],
                                     coefficients["kj"], coefficients["k_council"]))
        out["result"] = {"chargeable_volume": str(r.chargeable_volume), "multiplier_raw": str(r.multiplier_raw),
                         "multiplier": str(r.multiplier), "floor_applied": r.floor_applied, "fee": str(r.fee),
                         "advance_20": str(r.advance_20), "remainder_80": str(r.remainder_80), "formula": r.formula}
        return out

    def build_sheet(job: dict, view: dict, calc: dict, fee_data: dict) -> dict:
        result = job["result"]
        requests = [r for doc in ("project", "conclusion") for m in result[doc].values() for r in m["requests"]]
        return {
            "job": {"id": job["id"], "title": job["title"], "created": job["created"],
                    "created_by": job["created_by"]},
            "legal_basis": "Vazirlar Mahkamasining 151-son qarori (08.04.2026), 4-ilova",
            "files": [{"role": f["role"], "name": f["name"], "sha256": f["sha256"], "pages": len(f["pages"])}
                      for f in result["files"]],
            "models": result["models"], "prompt_version": result["prompt_version"],
            "prompt_hashes": sorted({r["prompt_hash"] for r in requests}),
            "requests": [{k: r[k] for k in ("key", "prompt_hash", "model", "kind", "file", "pages")} for r in requests],
            "has_project": view["has_project"], "scalars": view["scalar_rows"], "buildings": view["buildings"],
            "site": view["site"], "totals": view["totals"], "checks": view["checks"],
            "warnings": result["warnings"], "hb_rows": view["hb_rows"], "components": view["components"],
            "volumes": {c: {"label": volumes.VOLUME_LABELS[c], "proposal": view["volumes"][c]["proposal"],
                            "formula": view["volumes"][c]["formula"], "notes": view["volumes"][c]["notes"],
                            "decision": view["decisions"][c]["decision"]} for c in volumes.VOLUME_CODES},
            "permitted_floors": job["permitted_floors"],
            "calc": calc, "fee": fee_data, "audit": store.audit_log(job["id"]),
        }

    @app.exception_handler(fee.FeeError)
    async def fee_error(_request: Request, exc: fee.FeeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def back(job_id: int, anchor: str = "") -> RedirectResponse:
        return RedirectResponse(f"/jobs/{job_id}/review" + (f"#{anchor}" if anchor else ""), status_code=303)

    # ---------- pages ----------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        m1, m2 = llm.model1(), llm.model2()
        return templates.TemplateResponse(request, "index.html", {
            "jobs": store.jobs(), "staff": staff(request),
            "model1_up": llm.port_open(m1), "model2_up": llm.port_open(m2)})

    @app.post("/whoami")
    def whoami(name: str = Form(...), next: str = Form("/")) -> Response:
        if not next.startswith("/") or next.startswith("//"):
            raise HTTPException(400, "Noto'g'ri manzil")
        r = RedirectResponse(next, status_code=303)
        r.set_cookie("staff", quote(name.strip()[:80]), max_age=30 * 24 * 3600, httponly=True, samesite="lax")
        return r

    @app.post("/jobs")
    async def create_job(request: Request, title: str = Form(""), conclusion: UploadFile = File(...),
                         project: list[UploadFile] = File(default_factory=list)) -> Response:
        user = require_staff(request)
        # the project is optional: a conclusion alone can be read ("project not provided")
        uploads = [("project", f) for f in project if f.filename] + [("conclusion", conclusion)]
        for _role, f in uploads:
            if not (f.filename or "").lower().endswith(".pdf"):
                raise HTTPException(400, f"Faqat PDF qabul qilinadi: {f.filename!r}")
        job_id = store.create_job(title.strip() or (conclusion.filename or "Nomsiz ish"), user)
        folder = data_dir / "jobs" / str(job_id) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        for i, (role, f) in enumerate(uploads):
            safe = re.sub(r"[^\w.\-]+", "_", Path(f.filename or "file.pdf").name)
            path = folder / f"{i + 1:02d}_{role}_{safe}"
            path.write_bytes(await f.read())
            store.add_file(job_id, role, f.filename or safe, path)
        jobs.put(job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int) -> Response:
        job = get_job(job_id)
        return templates.TemplateResponse(request, "job.html", {
            "job": job, "files": store.files(job_id), "staff": staff(request)})

    @app.get("/jobs/{job_id}/status")
    def job_status(job_id: int) -> dict:
        job = get_job(job_id)
        return {"status": job["status"], "stage": job["stage"], "error": job["error"]}

    @app.post("/jobs/{job_id}/retry")
    def retry(request: Request, job_id: int) -> Response:
        user = require_staff(request)
        job = get_job(job_id)
        if job["status"] != "failed":
            raise HTTPException(409, "Faqat xato bilan to'xtagan ishni qayta ishga tushirish mumkin")
        store.set_job(job_id, status="queued", stage="", error="")
        store.audit(job_id, user, "job_retried")
        jobs.put(job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
    def review(request: Request, job_id: int) -> Response:
        job = get_job(job_id, need_result=True)
        return templates.TemplateResponse(request, "review.html", {
            "job": job, "view": build_view(job), "result": job["result"], "staff": staff(request),
            "locked": job["status"] == "finalized", "two": "model2" in job["result"]["models"],
            "SITE_KIND_LABELS": extract.SITE_KIND_LABELS})

    def find_page(job: dict, file_name: str, page: int) -> tuple[dict, dict]:
        for f in job["result"]["files"]:
            if f["name"] == file_name:
                for p in f["pages"]:
                    if p["number"] == page:
                        return f, p
        raise HTTPException(404, "Sahifa topilmadi")

    @app.get("/jobs/{job_id}/page-image")
    def page_image(job_id: int, file: str, page: int) -> Response:
        _f, p = find_page(get_job(job_id, need_result=True), file, page)
        return FileResponse(p["image"], media_type="image/png")

    @app.get("/jobs/{job_id}/rects")
    def rects(job_id: int, file: str, page: int, snippet: str = "") -> dict:
        f, p = find_page(get_job(job_id, need_result=True), file, page)
        found = ingest.snippet_rects(Path(f["path"]), page, snippet) if snippet.strip() else []
        z = p["zoom"]
        return {"width": p["width"] * z, "height": p["height"] * z,
                "rects": [[x0 * z, y0 * z, (x1 - x0) * z, (y1 - y0) * z] for x0, y0, x1, y1 in found],
                "sheet": classify.SHEET_LABELS.get((p["sheet"] or {}).get("kind", ""), ""),
                "textless": p["textless"]}

    # ---------- staff decisions ----------
    @app.post("/jobs/{job_id}/match")
    def set_match(request: Request, job_id: int, project_index: int = Form(...), conclusion_index: int = Form(...),
                  reason: str = Form("")) -> Response:
        """Staff pairs a project building with a conclusion building (-1: no pair)."""
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        view = build_view(job)
        if not 0 <= project_index < len(view["project_buildings"]):
            raise HTTPException(400, "Loyihadagi bino indeksi noto'g'ri")
        if not -1 <= conclusion_index < len(view["conclusion_buildings"]):
            raise HTTPException(400, "Xulosadagi bino indeksi noto'g'ri")
        why = reason_field(reason)
        matches = dict(job["matches"] or {})
        old = matches.get(str(project_index))
        matches[str(project_index)] = conclusion_index
        store.set_job(job_id, matches=matches)
        store.audit(job_id, user, "building_matched", target=view["project_buildings"][project_index],
                    old="" if old is None else str(old), new=str(conclusion_index), reason=why)
        return back(job_id, "buildings")

    @app.post("/jobs/{job_id}/floors")
    def set_floors(request: Request, job_id: int, permitted_floors: str = Form(...)) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        if not re.fullmatch(r"\d{1,3}", permitted_floors.strip()):
            raise HTTPException(400, "Ruxsat etilgan qavatlar soni: butun son kiriting")
        store.set_job(job_id, permitted_floors=int(permitted_floors))
        store.audit(job_id, user, "permitted_floors_set", old=str(job["permitted_floors"]), new=permitted_floors.strip())
        return back(job_id, "vol-Hyu")

    @app.post("/jobs/{job_id}/component/{component_id}")
    def set_component(request: Request, job_id: int, component_id: str, category: str = Form(""),
                      height: str = Form(""), volume: str = Form(""), included: str = Form(""),
                      reason: str = Form("")) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        view = build_view(job)
        current = [c for c in view["components"] + view["hb_rows"] if c["id"] == component_id]
        if not current:
            raise HTTPException(404, "Bunday komponent yo'q")
        data: dict = {"reason": reason_field(reason), "user": user, "time": now()}
        if category:
            if category not in classify.CATEGORIES:
                raise HTTPException(400, f"Noto'g'ri toifa: {category!r}")
            data["category"] = category
        if height.strip():
            data["height"] = str(decimal_field(height, "Balandlik", Decimal("0")))
        if volume.strip():
            data["volume"] = str(decimal_field(volume, "Hajm", Decimal("0")))
        if included in ("yes", "no"):
            data["included"] = included == "yes"
        if len(data) == 3:
            raise HTTPException(400, "Hech narsa o'zgartirilmadi")
        old = store.overrides(job_id).get(component_id, {})
        store.set_override(job_id, component_id, data)
        store.audit(job_id, user, "component_changed", target=f"{current[0]['name']} [{component_id}]",
                    old=json.dumps(old, ensure_ascii=False), new=json.dumps(data, ensure_ascii=False),
                    reason=data["reason"])
        return back(job_id, "vol-Hb" if any(r["id"] == component_id for r in view["hb_rows"]) else "components")

    @app.post("/jobs/{job_id}/volume/{code}/confirm")
    def confirm_volume(request: Request, job_id: int, code: str) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        if code not in volumes.VOLUME_CODES:
            raise HTTPException(404, "Bunday hajm yo'q")
        proposal = build_view(job)["volumes"][code]["proposal"]
        if proposal is None:
            raise HTTPException(409, f"{code}: taklif yo'q — qiymatni «o'zgartirish» orqali sababi bilan kiriting")
        store.set_decision(job_id, code, "confirmed", proposal, proposal, "", user)
        store.audit(job_id, user, "volume_confirmed", target=code, new=proposal)
        return back(job_id, f"vol-{code}")

    @app.post("/jobs/{job_id}/volume/{code}/change")
    def change_volume(request: Request, job_id: int, code: str, value: str = Form(...),
                      reason: str = Form("")) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        if code not in volumes.VOLUME_CODES:
            raise HTTPException(404, "Bunday hajm yo'q")
        v = decimal_field(value, code, Decimal("0"))
        why = reason_field(reason)
        view = build_view(job)
        old = view["decisions"][code]["decision"]
        store.set_decision(job_id, code, "changed", str(v), view["volumes"][code]["proposal"], why, user)
        store.audit(job_id, user, "volume_changed", target=code, old=old["value"] if old else "",
                    new=str(v), reason=why)
        return back(job_id, f"vol-{code}")

    # ---------- calculation ----------
    @app.get("/jobs/{job_id}/calc", response_class=HTMLResponse)
    def calc_page(request: Request, job_id: int) -> Response:
        job = get_job(job_id, need_result=True)
        return templates.TemplateResponse(request, "calc.html", {
            "job": job, "view": build_view(job), "calc": calc_inputs(job), "staff": staff(request),
            "locked": job["status"] == "finalized"})

    def read_calc(kt: str, ko: str, kz: str, kz_value: str, kj: str, k_council: str, bhm: str) -> dict:
        return {"kt": kt, "ko": ko, "kz": kz, "kz_value": kz_value.strip(), "kj": kj,
                "k_council": k_council.strip(), "bhm": bhm.strip()}

    @app.post("/jobs/{job_id}/calc/preview")
    def calc_preview(job_id: int, kt: str = Form(...), ko: str = Form(...), kz: str = Form(...),
                     kz_value: str = Form(""), kj: str = Form(...), k_council: str = Form("1"),
                     bhm: str = Form(...)) -> dict:
        job = get_job(job_id, need_result=True)
        calc = read_calc(kt, ko, kz, kz_value, kj, k_council, bhm)
        out = compute_fee(build_view(job), calc)
        if job["status"] != "finalized":
            store.set_job(job_id, calc=calc)
        return out

    @app.post("/jobs/{job_id}/finalize")
    def finalize(request: Request, job_id: int, kt: str = Form(...), ko: str = Form(...), kz: str = Form(...),
                 kz_value: str = Form(""), kj: str = Form(...), k_council: str = Form("1"),
                 bhm: str = Form(...)) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        calc = read_calc(kt, ko, kz, kz_value, kj, k_council, bhm)
        view = build_view(job)
        fee_data = compute_fee(view, calc)
        if fee_data["missing"]:
            raise HTTPException(409, "Tasdiqlanmagan hajmlar bor: " + ", ".join(fee_data["missing"]))
        store.audit(job_id, user, "finalized", new=fee_data["result"]["fee"],
                    reason=json.dumps(calc, ensure_ascii=False))
        store.set_job(job_id, calc=calc)
        job = get_job(job_id)
        sheet = build_sheet(job, view, calc, fee_data)
        sheet["finalized"] = {"by": user, "at": now()}
        store.set_job(job_id, final=sheet, status="finalized", finalized_at=sheet["finalized"]["at"])
        return RedirectResponse(f"/jobs/{job_id}/sheet", status_code=303)

    def sheet_data(job_id: int) -> tuple[dict, dict]:
        job = get_job(job_id, need_result=True)
        if job["final"] is not None:
            return job, job["final"]
        view, calc = build_view(job), calc_inputs(job)
        return job, build_sheet(job, view, calc, compute_fee(view, calc))

    @app.get("/jobs/{job_id}/sheet", response_class=HTMLResponse)
    def sheet_page(request: Request, job_id: int) -> Response:
        job, sheet = sheet_data(job_id)
        return templates.TemplateResponse(request, "sheet.html", {"job": job, "sheet": sheet, "staff": staff(request)})

    @app.get("/jobs/{job_id}/sheet.json")
    def sheet_json(job_id: int) -> Response:
        _job, sheet = sheet_data(job_id)
        return Response(json.dumps(sheet, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="hisob_varaqasi_{job_id}.json"'})

    @app.get("/export.csv")
    def export_csv() -> Response:
        buffer = io.StringIO()
        w = csv.writer(buffer, delimiter=";")
        w.writerow(["id", "nomi", "holati", "yaratilgan", "xodim", "Hb", "Hyu", "Ha", "Ht", "Hu", "hisob_hajmi",
                    "yigim_som", "oldindan_20", "qolgan_80", "yakunlangan"])
        for j in store.jobs():
            final = j["final"] or {}
            vols = final.get("volumes", {})
            result = (final.get("fee") or {}).get("result") or {}
            w.writerow([j["id"], j["title"], j["status"], j["created"], j["created_by"]]
                       + [(vols.get(c, {}).get("decision") or {}).get("value", "") for c in volumes.VOLUME_CODES]
                       + [result.get(k, "") for k in ("chargeable_volume", "fee", "advance_20", "remainder_80")]
                       + [j["finalized_at"] or ""])
        return Response("﻿" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="ishlar.csv"'})

    return app
