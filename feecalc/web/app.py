"""Web interface (FastAPI + Jinja2 + plain JS, SQLite). UI text is Uzbek (Latin).

Run: python -m feecalc.web   (listens on 127.0.0.1:8000 only)
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import queue
import re
import secrets
import threading
import traceback
from types import SimpleNamespace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, unquote

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import classify, contract, decide, extract, fee, ingest, llm, norms, pipeline, trace, verify, volumes
from ..knowledge import KnowledgeBase
from . import api
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


AUDIT_LABELS = {
    "job_created": "ish yaratildi", "job_retried": "o'qish qayta boshlandi", "volume_confirmed": "hajm tasdiqlandi",
    "volume_changed": "hajm o'zgartirildi", "component_changed": "tarkib qatori o'zgartirildi",
    "permitted_floors_set": "ruxsat etilgan qavatlar soni kiritildi", "building_matched": "binolar juftlandi",
    "finalized": "ish yakunlandi", "reopened": "ish qayta ochildi",
}
ERROR_TITLES = {404: "Sahifa topilmadi", 409: "Bu amalni hozir bajarib bo'lmaydi"}
GROUPS = ("kt", "ko", "kz", "kj")
# What the calculation page used to save by itself before recommendations existed: not a staff choice.
OLD_DEFAULT_CALC = {"kt": "new", "ko": "other", "kz": "500k-1m", "kz_value": "", "kj": "central", "k_council": "1",
                    "bhm": "440000"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def explain_error(error: str) -> str:
    """A plain-language hint for a failed job. The full error text is always shown next to it."""
    if "FileDataError" in error or "Failed to open file" in error:
        return ("Yuklangan fayl PDF emas yoki buzilgan. Faylni kompyuteringizda ochib tekshiring va "
                "yangi ish sifatida qayta yuklang.")
    if "tayyor emas" in error or "ConnectError" in error or "Connection refused" in error:
        return ("O'qish modeli hozir ishlamayapti. Bir necha daqiqadan keyin «Qayta urinish»ni bosing; "
                "yordam bermasa, administratorga murojaat qiling.")
    if "MalformedResponse" in error or "DegenerateOutput" in error or "RepetitionLoop" in error:
        return "Model buzuq javob qaytardi, shuning uchun o'qish to'xtatildi. «Qayta urinish»ni bosing."
    return "Kutilmagan xato yuz berdi. «Qayta urinish»ni bosing; takrorlansa, quyidagi texnik ma'lumotni administratorga yuboring."


def seconds_since(stamp: str | None) -> int | None:
    if not stamp:
        return None
    return max(0, int((datetime.now() - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).total_seconds()))


def coverage(result: dict, plan_sheets: list[dict]) -> list[dict]:
    """How much of each document was read: pages sent to a model or read by code, and the rest by sheet kind."""
    requests = [r for doc in ("project", "conclusion") for r in result[doc].get("model1", {}).get("requests", [])]
    read = {(r["file"], n) for r in requests for n in r["pages"]} | {(s["file"], s["page"]) for s in plan_sheets if s["rooms"]}
    out: list[dict] = []
    for f in result["files"]:
        skipped: dict[str, int] = {}
        for p in f["pages"]:
            if (f["name"], p["number"]) not in read:
                kind = "matnsiz sahifa" if p["textless"] else classify.SHEET_LABELS.get((p["sheet"] or {}).get("kind", ""), "boshqa")
                skipped[kind] = skipped.get(kind, 0) + 1
        out.append({"role": f["role"], "name": f["name"], "pages": len(f["pages"]),
                    "read": len(f["pages"]) - sum(skipped.values()), "skipped": skipped})
    return out


def expertise(view: dict) -> dict:
    """Does the expertise conclusion agree with the project? Every figure that both documents state is compared."""
    rows = [(r["label"], r, f"f-s{i}") for i, r in enumerate(view["scalar_rows"], 1)]
    rows += [(f"{b['title']} — {r['label']}", r, f"f-b{bi}-{fi}") for bi, b in enumerate(view["buildings"], 1)
             for fi, r in enumerate(b["fields"], 1)]
    compared = [(label, r, anchor) for label, r, anchor in rows
                if r["check"] and r["check"]["status"] in ("ok", "rounding", "mismatch")]
    differing = [{"label": label, "project": (r["project"] or {}).get("value"), "conclusion": (r["conclusion"] or {}).get("value"),
                  "detail": r["check"]["detail"], "anchor": anchor, "file": (r["conclusion"] or {}).get("file"),
                  "page": (r["conclusion"] or {}).get("page"), "snippet": (r["conclusion"] or {}).get("source_text") or ""}
                 for label, r, anchor in compared if r["check"]["status"] == "mismatch"]
    only_conclusion = sum(1 for _l, r, _a in rows if r["conclusion"] and r["conclusion"].get("value") is not None
                          and not (r["project"] and r["project"].get("value") is not None))
    status = "not_run" if not view["has_project"] or not compared else ("mismatch" if differing else "ok")
    return {"status": status, "compared": len(compared), "agree": len(compared) - len(differing), "differing": differing,
            "only_in_conclusion": only_conclusion}


def attention(view: dict) -> list[dict]:
    """What the reviewer should look at first: values that failed verification, documents or models that disagree.

    Nothing is hidden by this list - every item is also shown in its table; `anchor` is the id of that row.
    """
    items: list[dict] = []
    row_checks: set[str] = set()

    def add(kind: str, text: str, anchor: str, value: dict | None = None) -> None:
        v = value or {}
        label = verify.STATUS_LABELS.get(kind) or verify.CHECK_LABELS[kind]
        items.append({"kind": kind, "label": label, "text": text, "anchor": anchor, "file": v.get("file"),
                      "page": v.get("page"), "snippet": v.get("source_text") or ""})

    def row(label: str, r: dict, anchor: str) -> None:
        for side, where in (("project", "loyihada"), ("conclusion", "xulosada")):
            v = r[side]
            if v and v["status"] == "conflict":
                add("conflict", f"{label}: {where} turli joyda turli qiymat yozilgan", anchor, v)
            if v and v["status"] == "unverified":
                add("unverified", f"{label}: {where} o'qilgan qiymat manba matni bilan tasdiqlanmadi", anchor, v)
        if r["check"]:
            row_checks.add(r["check"]["code"])
            if r["check"]["status"] == "mismatch":
                add("mismatch", f"{label}: loyiha va xulosada farq bor", anchor, r["conclusion"] or r["project"])
        for side, where in (("project", "loyihani"), ("conclusion", "xulosani")):
            if r[f"agreement_{side}"] == "mismatch":
                add("mismatch", f"{label}: {where} ikki model turlicha o'qidi", anchor, r[side])

    for i, r in enumerate(view["scalar_rows"], 1):
        row(r["label"], r, f"f-s{i}")
    for bi, b in enumerate(view["buildings"], 1):
        for fi, r in enumerate(b["fields"], 1):
            row(f"{b['title']} — {r['label']}", r, f"f-b{bi}-{fi}")
    for c in view["checks"]:
        if c["status"] in ("mismatch", "warning") and c["code"] not in row_checks:
            add(c["status"], c["name"], "checks")
    order = {"conflict": 0, "mismatch": 0, "unverified": 1, "warning": 2}
    return sorted(items, key=lambda x: order[x["kind"]])


def password_ok(header: str, password: str) -> bool:
    """Check an HTTP Basic `Authorization` header; the user name is ignored."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not re.fullmatch(r"[A-Za-z0-9+/]+=*", token) or len(token) % 4:
        return False
    given = base64.b64decode(token).partition(b":")[2]
    return secrets.compare_digest(given, password.encode("utf-8"))


def create_app(data_dir: Path = DATA_DIR, start_worker: bool = True, password: str = "",
               port_passwords: dict[int, str] | None = None, api_keys: dict[str, str] | None = None) -> FastAPI:
    """`password` guards every listener; `port_passwords` overrides it for requests that arrived on a given port."""
    # The interactive docs page would load scripts from a CDN, so only the machine-readable schema is served.
    app = FastAPI(title="Infratuzilma yig'imi", version="1", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    if password:
        @app.middleware("http")
        async def require_password(request: Request, call_next):  # type: ignore[no-untyped-def]
            expected = (port_passwords or {}).get(request.scope["server"][1], password)
            if request.url.path.startswith("/api/"):  # the integration API checks its own keys
                return await call_next(request)
            if password_ok(request.headers.get("authorization", ""), expected):
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
        JOB_STATUS={"queued": "navbatda", "running": "o'qilmoqda", "ready": "tekshirish kutilmoqda",
                    "failed": "xato bilan to'xtadi", "finalized": "yakunlangan"},
        AUDIT_LABELS=AUDIT_LABELS, GROUP_TITLES=contract.COEFFICIENT_NAMES, NORM_LABELS=norms.LABELS,
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
        store.set_job(job_id, status="running", stage="ingest", error="", started_at=now(),
                      requests_total=None, requests_done=None)
        result = pipeline.run_extraction(
            data_dir / "jobs" / str(job_id), project, conclusion[0],
            lambda stage: store.set_job(job_id, stage=stage),
            lambda done, total: store.set_job(job_id, requests_done=done, requests_total=total))
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

    # ---------- reference data and the system's recommendation ----------
    descriptions: dict[int, dict | None] = {}  # for jobs read before the object description was stored
    knowledge = KnowledgeBase(data_dir / "norms" / "knowledge.db")  # filled by scripts/sync_norms.py; may be empty
    app.state.knowledge = knowledge

    def load_reference() -> decide.Reference:
        rows = store.reference_rows()
        settings = {r["key"]: r for r in rows if r["kind"] == "setting"}
        streets: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            if r["kind"] == "central_street":
                settlement, _, street = r["key"].partition("|")
                streets.setdefault(settlement, []).append((street, r["basis"]))
        base = decide.Reference()
        return decide.Reference(
            bhm=Decimal(settings["bhm"]["value"]) if "bhm" in settings else base.bhm,
            bhm_basis=settings["bhm"]["basis"] if "bhm" in settings else base.bhm_basis,
            k_council=Decimal(settings["k_council"]["value"]) if "k_council" in settings else base.k_council,
            k_council_basis=settings["k_council"]["basis"] if "k_council" in settings else base.k_council_basis,
            zones={r["key"]: (r["value"], r["basis"]) for r in rows if r["kind"] == "zone"},
            central_streets=streets)

    def description(job: dict) -> dict | None:
        """The object as the conclusion describes it (read by code, see pipeline.object_description)."""
        result = job["result"]
        if "object" in result:
            return result["object"]
        if job["id"] not in descriptions:
            path = Path([f for f in result["files"] if f["role"] == "conclusion"][0]["path"])
            descriptions[job["id"]] = pipeline.object_description(ingest.ingest(path)) if path.exists() else None
        return descriptions[job["id"]]

    plan_cache: dict[int, list[dict]] = {}  # for jobs read before plans were stored in the result

    def plan_sheets(job: dict) -> list[dict]:
        result = job["result"]
        if "plans" in result:
            return result["plans"]
        if job["id"] not in plan_cache:
            present = all(Path(f["path"]).exists() for f in result["files"])
            plan_cache[job["id"]] = pipeline.read_plans(result["files"]) if present else []
        return plan_cache[job["id"]]

    index_cache: dict[int, list[dict]] = {}

    def plan_index(job: dict) -> list[dict]:
        result = job["result"]
        if "plan_index" in result:
            return result["plan_index"]
        if job["id"] not in index_cache:
            present = all(Path(f["path"]).exists() for f in result["files"])
            index_cache[job["id"]] = pipeline.read_plan_index(result["files"]) if present else []
        return index_cache[job["id"]]

    def saved_calc(job: dict) -> dict:
        saved = job["calc"] or {}
        return {} if saved == OLD_DEFAULT_CALC else saved

    def build_view(job: dict) -> dict:
        """Assessment, the system's recommendation, and the state of staff decisions."""
        names = {r["key"]: r["value"] for r in store.reference_rows() if r["kind"] == "room_name"}
        a = pipeline.assess(job["result"], store.overrides(job["id"]), job["permitted_floors"], job["matches"], names)
        a["plan_index"] = plan_index(job)
        rec = decide.recommend(a, description(job), load_reference(), job["permitted_floors"],
                               saved_calc(job).get("ko") or None)
        a["recommendation"] = rec
        decisions = store.decisions(job["id"])
        locked = job["status"] == "finalized"
        states: dict[str, dict] = {}
        for code in volumes.VOLUME_CODES:
            auto = rec["volumes"][code]
            a["volumes"][code]["auto"] = auto
            d = decisions.get(code)
            # a confirmation is tied to the value that was on screen; if the recommendation moves, confirm again
            stale = bool(d and not locked and d["status"] == "confirmed" and d["proposal"] != auto["value"])
            states[code] = {"decision": d, "stale": stale, "valid": bool(d) and not stale,
                            "value": d["value"] if d and not stale else None}
        a["decisions"] = states
        a["all_confirmed"] = all(s["valid"] for s in states.values())
        a["attention"] = attention(a)
        a["expertise"] = expertise(a)
        a["plans"] = plan_sheets(job)
        a["coverage"] = coverage(job["result"], a["plans"])
        a["norms"] = norms.check(a, description(job), knowledge)
        return a

    def recommended_calc(rec: dict) -> dict:
        return {**{g: rec["coefficients"][g]["code"] or "" for g in GROUPS}, "kz_value": "",
                "k_council": rec["k_council"]["value"], "bhm": rec["bhm"]["value"]}

    def calc_inputs(job: dict, rec: dict) -> dict:
        """The recommendation, overridden by whatever staff chose differently; a finalized job keeps its own."""
        if job["final"] is not None:
            return dict(job["final"]["calc"])
        return {**recommended_calc(rec), **saved_calc(job)}

    def save_calc(job_id: int, calc: dict, rec: dict) -> None:
        """Only choices that differ from the recommendation are stored, so that a later change of the
        reference data still reaches every job in which nobody decided otherwise."""
        base = recommended_calc(rec)
        store.set_job(job_id, calc={k: v for k, v in calc.items() if v != base[k]})

    def compute_fee(view: dict, calc: dict, proposed: bool = False) -> dict:
        """Fee from the volumes and the coefficients. FeeError -> HTTP 422 (handler below).

        Only confirmed volumes count, unless `proposed`: then a volume nobody has decided yet takes the
        system's recommendation (the proposal page shows what approval would produce)."""
        values = {c: view["decisions"][c]["value"] if view["decisions"][c]["valid"]
                  else (view["volumes"][c]["auto"]["value"] if proposed else None) for c in volumes.VOLUME_CODES}
        missing = [c for c in volumes.VOLUME_CODES if values[c] is None]
        undecided = [g for g in GROUPS if not calc[g]]
        if undecided:
            return {"missing": missing, "undecided": undecided, "coefficients": {}, "options": {}, "result": None,
                    "industrial": None}
        kz_entered = verify.parse_number(calc["kz_value"]) if calc["kz_value"] else None
        coefficients = {
            "kt": fee.option_value("kt", calc["kt"]), "ko": fee.option_value("ko", calc["ko"]),
            "kz": fee.option_value("kz", calc["kz"], kz_entered), "kj": fee.option_value("kj", calc["kj"]),
            "k_council": decimal_field(calc["k_council"], "Kengash koeffitsiyenti"),
            "bhm": decimal_field(calc["bhm"], "BHM"),
        }
        out: dict = {"missing": missing, "undecided": [], "coefficients": {k: str(v) for k, v in coefficients.items()},
                     "options": {g: fee.option(g, calc[g]).text for g in GROUPS},
                     "result": None, "industrial": None}
        if calc["ko"] == "industrial":
            out["industrial"] = view["industrial"]
        if missing:
            return out
        v = {c: Decimal(values[c]) for c in volumes.VOLUME_CODES}
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
            "volumes": {c: {"label": volumes.VOLUME_LABELS[c], "proposal": view["volumes"][c]["auto"]["value"],
                            "formula": view["volumes"][c]["auto"]["formula"] or view["volumes"][c]["formula"],
                            "notes": view["volumes"][c]["notes"], "assumptions": view["volumes"][c]["auto"]["assumptions"],
                            "decision": view["decisions"][c]["decision"]} for c in volumes.VOLUME_CODES},
            "object": description(job), "recommendation": view["recommendation"], "norms": view["norms"],
            "expertise": view["expertise"],
            "permitted_floors": job["permitted_floors"],
            "calc": calc, "fee": fee_data, "audit": store.audit_log(job["id"]),
        }

    # ---------- errors: a readable page for a browser, JSON for scripts ----------
    def error_page(request: Request, status: int, message: str) -> Response:
        return templates.TemplateResponse(request, "error.html", {
            "staff": staff(request), "status": status, "message": message,
            "heading": ERROR_TITLES.get(status, "Ma'lumotni tekshiring")}, status_code=status)

    def wants_html(request: Request) -> bool:
        return "text/html" in request.headers.get("accept", "")

    @app.exception_handler(fee.FeeError)
    async def fee_error(request: Request, exc: fee.FeeError) -> Response:
        if wants_html(request):
            return error_page(request, 422, str(exc))
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if not wants_html(request):
            return await http_exception_handler(request, exc)
        unknown = exc.status_code == 404 and exc.detail == "Not Found"
        return error_page(request, exc.status_code, "Bunday sahifa yo'q. Manzilni tekshiring." if unknown else str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        if not wants_html(request):
            return await request_validation_exception_handler(request, exc)
        return error_page(request, 422, "Forma to'liq to'ldirilmagan. Orqaga qaytib, majburiy maydonlarni to'ldiring.")

    def flash(response: Response, message: str) -> Response:
        """A one-time confirmation shown as a toast on the next page (read and removed by app.js)."""
        response.set_cookie("flash", quote(message), max_age=60, samesite="lax")
        return response

    def back(job_id: int, anchor: str = "", message: str = "") -> Response:
        r = RedirectResponse(f"/jobs/{job_id}/review" + (f"#{anchor}" if anchor else ""), status_code=303)
        return flash(r, message) if message else r

    # ---------- pages ----------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        m1, m2 = llm.model1(), llm.model2()
        return templates.TemplateResponse(request, "index.html", {
            "jobs": store.jobs(), "staff": staff(request), "decided": store.decision_counts(),
            "model1_up": llm.port_open(m1), "model2_up": llm.port_open(m2)})

    @app.post("/whoami")
    def whoami(name: str = Form(...), next: str = Form("/")) -> Response:
        if not next.startswith("/") or next.startswith("//"):
            raise HTTPException(400, "Noto'g'ri manzil")
        if not name.strip():
            raise HTTPException(400, "Ism bo'sh bo'lmasin")
        r = RedirectResponse(next, status_code=303)
        r.set_cookie("staff", quote(name.strip()[:80]), max_age=30 * 24 * 3600, httponly=True, samesite="lax")
        return r

    @app.post("/jobs")
    async def create_job(request: Request, title: str = Form(""), conclusion: UploadFile = File(...),
                         project: list[UploadFile] = File(default_factory=list)) -> Response:
        user = require_staff(request)
        job_id = await new_job(title, user, conclusion, project)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int) -> Response:
        job = get_job(job_id)
        return templates.TemplateResponse(request, "job.html", {
            "job": job, "files": store.files(job_id), "staff": staff(request), "state": job_state(job)})

    def job_state(job: dict) -> dict:
        """Everything the progress page shows; polled as JSON while the job is queued or running."""
        running = job["status"] == "running"
        return {"status": job["status"], "stage": job["stage"], "error": job["error"],
                "hint": explain_error(job["error"]) if job["status"] == "failed" else "",
                "requests_total": job["requests_total"] if running else None,
                "requests_done": job["requests_done"] if running else None,
                "elapsed_s": seconds_since(job["started_at"]) if running else None,
                "ahead": store.jobs_ahead(job["id"]) if job["status"] == "queued" else 0}

    @app.get("/jobs/{job_id}/status")
    def job_status(job_id: int) -> dict:
        return job_state(get_job(job_id))

    @app.post("/jobs/{job_id}/retry")
    def retry(request: Request, job_id: int) -> Response:
        user = require_staff(request)
        job = get_job(job_id)
        if job["status"] != "failed":
            raise HTTPException(409, "Faqat xato bilan to'xtagan ishni qayta ishga tushirish mumkin")
        store.set_job(job_id, status="queued", stage="", error="")
        store.audit(job_id, user, "job_retried")
        jobs.put(job_id)
        return flash(RedirectResponse(f"/jobs/{job_id}", status_code=303), "O'qish qayta navbatga qo'yildi")

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
        return back(job_id, "buildings", "Juftlash saqlandi")

    @app.post("/jobs/{job_id}/floors")
    def set_floors(request: Request, job_id: int, permitted_floors: str = Form(...)) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        if not re.fullmatch(r"\d{1,3}", permitted_floors.strip()):
            raise HTTPException(400, "Ruxsat etilgan qavatlar soni: butun son kiriting")
        store.set_job(job_id, permitted_floors=int(permitted_floors))
        store.audit(job_id, user, "permitted_floors_set", old=str(job["permitted_floors"]), new=permitted_floors.strip())
        return back(job_id, "vol-Hyu", "Ruxsat etilgan qavatlar soni saqlandi")

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
        return back(job_id, f"c-{component_id}", "O'zgarish saqlandi")

    @app.post("/jobs/{job_id}/volume/{code}/confirm")
    def confirm_volume(request: Request, job_id: int, code: str) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        if code not in volumes.VOLUME_CODES:
            raise HTTPException(404, "Bunday hajm yo'q")
        proposal = build_view(job)["volumes"][code]["auto"]["value"]
        if proposal is None:
            raise HTTPException(409, f"{code}: taklif yo'q — qiymatni «o'zgartirish» orqali sababi bilan kiriting")
        store.set_decision(job_id, code, "confirmed", proposal, proposal, "", user)
        store.audit(job_id, user, "volume_confirmed", target=code, new=proposal)
        return back(job_id, f"vol-{code}", f"{code} tasdiqlandi")

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
        store.set_decision(job_id, code, "changed", str(v), view["volumes"][code]["auto"]["value"], why, user)
        store.audit(job_id, user, "volume_changed", target=code, old=old["value"] if old else "",
                    new=str(v), reason=why)
        return back(job_id, f"vol-{code}", f"{code} o'zgartirildi")

    # ---------- calculation ----------
    @app.get("/jobs/{job_id}/calc", response_class=HTMLResponse)
    def calc_page(request: Request, job_id: int) -> Response:
        job = get_job(job_id, need_result=True)
        view = build_view(job)
        return templates.TemplateResponse(request, "calc.html", {
            "job": job, "view": view, "rec": view["recommendation"], "calc": calc_inputs(job, view["recommendation"]),
            "staff": staff(request), "locked": job["status"] == "finalized"})

    def read_calc(kt: str, ko: str, kz: str, kz_value: str, kj: str, k_council: str, bhm: str) -> dict:
        return {"kt": kt, "ko": ko, "kz": kz, "kz_value": kz_value.strip(), "kj": kj,
                "k_council": k_council.strip(), "bhm": bhm.strip()}

    @app.post("/jobs/{job_id}/calc/preview")
    def calc_preview(job_id: int, kt: str = Form(""), ko: str = Form(""), kz: str = Form(""),
                     kz_value: str = Form(""), kj: str = Form(""), k_council: str = Form("1"),
                     bhm: str = Form(...), proposed: int = 0) -> dict:
        job = get_job(job_id, need_result=True)
        calc = read_calc(kt, ko, kz, kz_value, kj, k_council, bhm)
        if job["status"] != "finalized":
            save_calc(job_id, calc, build_view(job)["recommendation"])
            job = get_job(job_id)  # the object type just chosen changes how Hb is counted
        return compute_fee(build_view(job), calc, proposed=bool(proposed))

    def finish(job_id: int, user: str, calc: dict, contract_number: str = "", contract_date: str = "") -> Response:
        finalize_job(job_id, user, calc, contract_number, contract_date)
        return flash(RedirectResponse(f"/jobs/{job_id}/sheet", status_code=303), "Ish yakunlandi")

    def finalize_job(job_id: int, user: str, calc: dict, contract_number: str = "", contract_date: str = "") -> dict:
        """Freeze the job: the sheet (and the contract documents made from it) never change afterwards."""
        job = get_job(job_id, need_result=True, editable=True)
        view = build_view(job)
        fee_data = compute_fee(view, calc)
        if fee_data["undecided"]:
            raise HTTPException(409, "Tanlanmagan koeffitsiyentlar bor: " + ", ".join(g.capitalize() for g in fee_data["undecided"]))
        if fee_data["missing"]:
            raise HTTPException(409, "Tasdiqlanmagan hajmlar bor: " + ", ".join(fee_data["missing"]))
        store.audit(job_id, user, "finalized", new=fee_data["result"]["fee"], reason=json.dumps(calc, ensure_ascii=False))
        sheet = build_sheet(job, view, calc, fee_data)
        sheet["finalized"] = {"by": user, "at": now()}
        sheet["contract"] = {"number": contract_number.strip()[:60], "date": contract_date.strip()[:20] or now()[:10]}
        store.set_job(job_id, final=sheet, status="finalized", finalized_at=sheet["finalized"]["at"])
        return sheet

    def approve_job(job_id: int, user: str, calc: dict, contract_number: str = "", contract_date: str = "") -> dict:
        """Accept the recommendation for every volume nobody decided otherwise, then finalize."""
        job = get_job(job_id, need_result=True, editable=True)
        save_calc(job_id, calc, build_view(job)["recommendation"])
        view = build_view(get_job(job_id))  # the object type just chosen changes how Hb is counted
        pending = [c for c in volumes.VOLUME_CODES if not view["decisions"][c]["valid"]]
        without = [c for c in pending if view["volumes"][c]["auto"]["value"] is None]
        if without:
            raise HTTPException(409, "Tizim bu hajmlar uchun taklif bera olmadi: " + ", ".join(without)
                                + " — qiymatini kiriting")
        for code in pending:
            auto = view["volumes"][code]["auto"]
            store.set_decision(job_id, code, "confirmed", auto["value"], auto["value"], "", user)
            store.audit(job_id, user, "volume_confirmed", target=code, new=auto["value"],
                        reason="tizim tavsiyasi tasdiqlandi" + (": " + "; ".join(auto["assumptions"]) if auto["assumptions"] else ""))
        return finalize_job(job_id, user, calc, contract_number, contract_date)

    async def new_job(title: str, user: str, conclusion: UploadFile, project: list[UploadFile],
                      client: str | None = None, external_id: str | None = None) -> int:
        """Store the uploaded PDFs and queue the job. The project is optional: a conclusion alone can be read."""
        uploads = [("project", f) for f in project if f.filename] + [("conclusion", conclusion)]
        for _role, f in uploads:
            if not (f.filename or "").lower().endswith(".pdf"):
                raise HTTPException(400, f"Faqat PDF qabul qilinadi: {f.filename!r}")
        job_id = store.create_job(title.strip() or (conclusion.filename or "Nomsiz ish"), user, client, external_id)
        folder = data_dir / "jobs" / str(job_id) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        for i, (role, f) in enumerate(uploads):
            safe = re.sub(r"[^\w.\-]+", "_", Path(f.filename or "file.pdf").name)
            path = folder / f"{i + 1:02d}_{role}_{safe}"
            path.write_bytes(await f.read())
            store.add_file(job_id, role, f.filename or safe, path)
        jobs.put(job_id)
        return job_id

    @app.post("/jobs/{job_id}/reopen")
    def reopen(request: Request, job_id: int, reason: str = Form("")) -> Response:
        """Take a finalized job back for correction. The earlier sheet stays in the audit log by its sum."""
        user = require_staff(request)
        job = get_job(job_id, need_result=True)
        if job["status"] != "finalized":
            raise HTTPException(409, "Faqat yakunlangan ishni qayta ochish mumkin")
        why = reason_field(reason)
        old = ((job["final"] or {}).get("fee") or {}).get("result") or {}
        store.audit(job_id, user, "reopened", old=str(old.get("fee", "")), reason=why)
        store.set_job(job_id, status="ready", final=None, finalized_at=None)
        return flash(RedirectResponse(f"/jobs/{job_id}/proposal", status_code=303), "Ish qayta ochildi")

    @app.get("/jobs/{job_id}/proposal", response_class=HTMLResponse)
    def proposal_page(request: Request, job_id: int) -> Response:
        """The system's whole recommendation on one screen; a person only approves it (or opens the details)."""
        job = get_job(job_id, need_result=True)
        if job["status"] == "finalized":
            return RedirectResponse(f"/jobs/{job_id}/sheet", status_code=303)
        view = build_view(job)
        rec = view["recommendation"]
        calc = calc_inputs(job, rec)
        values = {c: view["decisions"][c]["value"] if view["decisions"][c]["valid"] else view["volumes"][c]["auto"]["value"]
                  for c in volumes.VOLUME_CODES}
        negative = (all(v is not None for v in values.values())
                    and Decimal(values["Hb"]) + Decimal(values["Hyu"])
                    < Decimal(values["Ha"]) + Decimal(values["Ht"]) + Decimal(values["Hu"]))
        fee_data = ({"missing": [], "undecided": [], "result": None, "error": "Ha + Ht + Hu yig'indisi Hb + Hyu dan katta — "
                     "hisob hajmi manfiy chiqadi. Batafsil tekshirish sahifasida hajmlarni to'g'rilang."}
                    if negative else compute_fee(view, calc, proposed=True))
        return templates.TemplateResponse(request, "proposal.html", {
            "job": job, "view": view, "rec": rec, "calc": calc, "values": values, "fee": fee_data,
            "steps": trace.steps(job["result"], view, fee_data),
            "staff": staff(request), "today": now()[:10]})

    @app.post("/jobs/{job_id}/approve")
    def approve(request: Request, job_id: int, kt: str = Form(...), ko: str = Form(...), kz: str = Form(...),
                kz_value: str = Form(""), kj: str = Form(...), k_council: str = Form(...), bhm: str = Form(...),
                contract_number: str = Form(""), contract_date: str = Form("")) -> Response:
        """One step for the person: accept the recommendation for every volume nobody decided otherwise, then finalize."""
        user = require_staff(request)
        approve_job(job_id, user, read_calc(kt, ko, kz, kz_value, kj, k_council, bhm), contract_number, contract_date)
        return flash(RedirectResponse(f"/jobs/{job_id}/sheet", status_code=303), "Ish yakunlandi")

    @app.post("/jobs/{job_id}/finalize")
    def finalize(request: Request, job_id: int, kt: str = Form(""), ko: str = Form(""), kz: str = Form(""),
                 kz_value: str = Form(""), kj: str = Form(""), k_council: str = Form("1"),
                 bhm: str = Form(...)) -> Response:
        user = require_staff(request)
        job = get_job(job_id, need_result=True, editable=True)
        calc = read_calc(kt, ko, kz, kz_value, kj, k_council, bhm)
        save_calc(job_id, calc, build_view(job)["recommendation"])
        return finish(job_id, user, calc)

    def sheet_data(job_id: int) -> tuple[dict, dict]:
        job = get_job(job_id, need_result=True)
        if job["final"] is not None:
            return job, job["final"]
        view = build_view(job)
        calc = calc_inputs(job, view["recommendation"])
        return job, build_sheet(job, view, calc, compute_fee(view, calc))

    # ---------- contract documents ----------
    def contract_template() -> Path:
        return data_dir / "reference" / "contract_template.docx"

    def contract_pdf() -> Path:
        return data_dir / "reference" / "contract_template.pdf"

    def contract_layout() -> list[dict]:
        path = data_dir / "reference" / "contract_layout.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def template_pages() -> int:
        with ingest.MUPDF_LOCK, pymupdf.open(str(contract_pdf())) as doc:
            return len(doc)

    @app.get("/contract-designer", response_class=HTMLResponse)
    def designer_page(request: Request) -> Response:
        """Staff build the contract template by dragging fields onto the pages of their own contract PDF."""
        present = contract_pdf().exists()
        sizes: list[list[float]] = []
        if present:
            with ingest.MUPDF_LOCK, pymupdf.open(str(contract_pdf())) as doc:
                sizes = [[p.rect.width, p.rect.height] for p in doc]
        return templates.TemplateResponse(request, "designer.html", {
            "staff": staff(request), "present": present, "sizes": sizes, "layout": contract_layout() if present else [],
            "fields": contract.PLACEHOLDERS})

    @app.post("/contract-designer/pdf")
    async def designer_upload(request: Request, template: UploadFile = File(...)) -> Response:
        user = require_staff(request)
        if not (template.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "Shartnoma namunasi PDF fayl bo'lishi kerak")
        body = await template.read()
        if not body.startswith(b"%PDF"):
            raise HTTPException(400, "Fayl PDF emas")
        contract_pdf().parent.mkdir(parents=True, exist_ok=True)
        contract_pdf().write_bytes(body)
        (data_dir / "reference" / "contract_layout.json").write_text("[]", encoding="utf-8")
        store.set_reference("setting", "contract_pdf", template.filename or "shartnoma.pdf", "shartnoma namunasi (PDF)", user)
        return flash(RedirectResponse("/contract-designer", status_code=303), "Namuna yuklandi — endi maydonlarni joylashtiring")

    @app.get("/contract-designer/page/{number}.png")
    def designer_page_image(number: int) -> Response:
        if not contract_pdf().exists():
            raise HTTPException(404, "Shartnoma namunasi yuklanmagan")
        with ingest.MUPDF_LOCK, pymupdf.open(str(contract_pdf())) as doc:
            if not 1 <= number <= len(doc):
                raise HTTPException(404, "Bunday sahifa yo'q")
            image = doc[number - 1].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/contract-designer/layout")
    async def designer_save(request: Request) -> dict:
        user = require_staff(request)
        if not contract_pdf().exists():
            raise HTTPException(409, "Avval shartnoma namunasini yuklang")
        boxes = await request.json()
        problems = contract.layout_problems(boxes, template_pages())
        if problems:
            raise HTTPException(400, "Joylashuvda xato: " + problems)
        layout = contract.clean_layout(boxes, template_pages())
        (data_dir / "reference" / "contract_layout.json").write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
        store.set_reference("setting", "contract_layout", f"{len(layout)} ta maydon", "shartnoma konstruktori", user)
        return {"saved": len(layout)}

    @app.get("/contract-designer/preview.pdf")
    def designer_preview() -> Response:
        if not contract_pdf().exists():
            raise HTTPException(404, "Shartnoma namunasi yuklanmagan")
        with ingest.MUPDF_LOCK:
            body = contract.stamp(contract_pdf(), contract_layout(), contract.sample_values())
        return Response(body, media_type="application/pdf")

    @app.get("/jobs/{job_id}/contract.pdf")
    def contract_pdf_file(job_id: int) -> Response:
        """The contract: the organisation's PDF with this job's values stamped where staff placed the fields."""
        if not contract_pdf().exists() or not contract_layout():
            raise HTTPException(409, "Shartnoma shabloni tayyor emas: «Shartnoma konstruktori»da namunani yuklab, maydonlarni joylashtiring")
        with ingest.MUPDF_LOCK:
            body = contract.stamp(contract_pdf(), contract_layout(), contract.fields(final_sheet(job_id)))
        return Response(body, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="shartnoma_{job_id}.pdf"'})

    def final_sheet(job_id: int) -> dict:
        job = get_job(job_id, need_result=True)
        if job["final"] is None:
            raise HTTPException(409, "Avval ishni yakunlang — shartnoma hujjatlari yakunlangan hisob bo'yicha tuziladi")
        return job["final"]

    def word(body: bytes, name: str) -> Response:
        return Response(body, media_type=DOCX, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/jobs/{job_id}/annex.docx")
    def annex_file(job_id: int) -> Response:
        return word(contract.annex(final_sheet(job_id)), f"hisoblash_malumotnomasi_{job_id}.docx")

    @app.get("/jobs/{job_id}/contract.docx")
    def contract_file(job_id: int) -> Response:
        if not contract_template().exists():
            raise HTTPException(409, "Shartnoma shabloni yuklanmagan. «Ma'lumotnoma» sahifasida Word shablonini yuklang; "
                                     "ungacha hisoblash ma'lumotnomasini (ilova) yuklab olishingiz mumkin.")
        return word(contract.fill(contract_template(), final_sheet(job_id)), f"shartnoma_{job_id}.docx")

    @app.get("/jobs/{job_id}/sheet", response_class=HTMLResponse)
    def sheet_page(request: Request, job_id: int) -> Response:
        job, sheet = sheet_data(job_id)
        return templates.TemplateResponse(request, "sheet.html", {
            "job": job, "sheet": sheet, "staff": staff(request), "has_template": contract_template().exists(),
            "has_pdf_template": contract_pdf().exists() and bool(contract_layout())})

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

    # ---------- reference data kept by staff: what the recommendation relies on ----------
    def settlement_key(text: str) -> str:
        key = decide.locate(text).settlement
        if key is None:
            raise HTTPException(400, "Aholi punktini «Samarqand shahri» yoki «Payariq tumani» ko'rinishida yozing")
        return key

    @app.get("/reference", response_class=HTMLResponse)
    def reference_page(request: Request) -> Response:
        rows = store.reference_rows()
        streets: dict[str, list[dict]] = {}
        for r in rows:
            if r["kind"] == "central_street":
                streets.setdefault(r["key"].partition("|")[0], []).append(r)
        ref = load_reference()
        return templates.TemplateResponse(request, "reference.html", {
            "staff": staff(request), "ref": ref, "streets": streets,
            "zones": [r for r in rows if r["kind"] == "zone"], "log": store.reference_log(),
            "kz_options": [o for o in fee.GROUPS["kz"] if o.value is not None],
            "has_template": contract_template().exists(), "placeholders": contract.PLACEHOLDERS,
            "documents": knowledge.documents(), "unknown_names": unknown_names(),
            "room_names": [r for r in rows if r["kind"] == "room_name"]})

    def reference_saved(message: str) -> Response:
        return flash(RedirectResponse("/reference", status_code=303), message)

    @app.post("/reference/setting")
    def reference_setting(request: Request, name: str = Form(...), value: str = Form(...), basis: str = Form("")) -> Response:
        user = require_staff(request)
        if name not in ("bhm", "k_council"):
            raise HTTPException(400, "Noma'lum sozlama")
        number = decimal_field(value, "BHM" if name == "bhm" else "Kengash koeffitsiyenti", Decimal("0"))
        if name == "bhm" and number <= 0:
            raise HTTPException(400, "BHM musbat son bo'lishi kerak")
        if name == "k_council" and not fee.COUNCIL_RANGE[0] <= number <= fee.COUNCIL_RANGE[1]:
            raise HTTPException(400, "Kengash koeffitsiyenti 0,01 dan 1,5 gacha bo'lishi kerak")
        store.set_reference("setting", name, str(number), reason_field(basis), user)
        return reference_saved("Saqlandi")

    @app.post("/reference/zone")
    def reference_zone(request: Request, settlement: str = Form(...), code: str = Form(...), basis: str = Form("")) -> Response:
        user = require_staff(request)
        if code not in [o.code for o in fee.GROUPS["kz"] if o.value is not None]:
            raise HTTPException(400, "Noma'lum hududiy zona")
        store.set_reference("zone", settlement_key(settlement), code, reason_field(basis), user)
        return reference_saved("Hududiy zona saqlandi")

    @app.post("/reference/streets")
    def reference_streets(request: Request, settlement: str = Form(...), streets: str = Form(...), basis: str = Form("")) -> Response:
        """Central thoroughfare streets of one settlement, one name per line."""
        user = require_staff(request)
        place, why = settlement_key(settlement), reason_field(basis)
        names = [line.strip() for line in streets.splitlines() if decide.street_key(line)]
        if not names:
            raise HTTPException(400, "Kamida bitta ko'cha nomini yozing (har qatorda bittadan)")
        for name in names:
            store.set_reference("central_street", f"{place}|{decide.street_key(name)}", name[:120], why, user)
        return reference_saved(f"{len(names)} ta ko'cha saqlandi")

    def unknown_names() -> list[dict]:
        """Room names of recent jobs that are neither in the built-in dictionary nor decided by staff."""
        decided = {r["key"] for r in store.reference_rows() if r["kind"] == "room_name"}
        seen: dict[str, dict] = {}
        for listed in store.jobs()[:40]:
            job = store.job(listed["id"])
            if not job or not job["result"] or not job["result"]["project"]:
                continue
            project = job["result"]["project"]["model1"]
            guesses = job["result"].get("category_suggestions", {})
            for name in [b["name"] for b in project.get("area_breakdown", [])] + [r["name"] for r in project.get("rooms", [])]:
                key = classify.name_key(str(name))
                if key in decided or classify.room_category(str(name)).category is not None:
                    continue
                entry = seen.setdefault(key, {"key": key, "name": str(name), "count": 0, "jobs": set(), "guess": guesses.get(name)})
                entry["count"] += 1
                entry["jobs"].add(job["id"])
        return sorted(({**e, "jobs": sorted(e["jobs"])} for e in seen.values()), key=lambda e: -e["count"])

    @app.post("/reference/room-name")
    def reference_room_name(request: Request, name: str = Form(...), category: str = Form(...), basis: str = Form("")) -> Response:
        """Decide once what a room name means; it then applies to every job, old and new."""
        user = require_staff(request)
        if category not in classify.CATEGORIES or not classify.name_key(name):
            raise HTTPException(400, "Nom yoki toifa noto'g'ri")
        store.set_reference("room_name", classify.name_key(name), category, reason_field(basis), user)
        return flash(RedirectResponse("/reference#names", status_code=303), "Nom ro'yxatga qo'shildi")

    @app.post("/reference/delete")
    def reference_delete(request: Request, kind: str = Form(...), key: str = Form(...)) -> Response:
        user = require_staff(request)
        if kind not in ("setting", "zone", "central_street", "room_name"):
            raise HTTPException(400, "Noma'lum yozuv turi")
        store.delete_reference(kind, key, user)
        return reference_saved("O'chirildi")

    @app.post("/reference/template")
    async def reference_template(request: Request, template: UploadFile = File(...)) -> Response:
        """The organisation's own contract as a .docx with {{placeholders}}; unknown placeholders are refused."""
        user = require_staff(request)
        if not (template.filename or "").lower().endswith(".docx"):
            raise HTTPException(400, "Shablon Word (.docx) fayli bo'lishi kerak")
        target = contract_template()
        target.parent.mkdir(parents=True, exist_ok=True)
        candidate = target.with_suffix(".upload")
        candidate.write_bytes(await template.read())
        unknown = contract.unknown_placeholders(candidate)
        if unknown:
            candidate.unlink()
            raise HTTPException(400, "Shablonda noma'lum maydonlar bor: " + ", ".join("{{" + n + "}}" for n in unknown))
        candidate.replace(target)
        store.set_reference("setting", "contract_template", template.filename or "shablon.docx", "shartnoma shabloni", user)
        return reference_saved("Shartnoma shabloni saqlandi")

    api.register(app, SimpleNamespace(
        keys=api_keys or {}, store=store, data_dir=data_dir, get_job=get_job, job_state=job_state, build_view=build_view,
        calc_inputs=calc_inputs, compute_fee=compute_fee, approve_job=approve_job, new_job=new_job,
        decimal_field=decimal_field, reason_field=reason_field, final_sheet=final_sheet, contract_template=contract_template,
        find_page=find_page, load_reference=load_reference, knowledge=knowledge, settlement_key=settlement_key))
    return app
