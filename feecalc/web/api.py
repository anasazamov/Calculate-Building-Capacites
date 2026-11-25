"""Integration API (/api/v1): how another system drives this one.

The web pages are one client of the same functions; this is the other. JSON in and out, one API key
per integrating system (header X-API-Key), no cookies. A job is created with the documents, polled
until it is read, then its recommendation is fetched and approved (or individual values are changed
first); the finalized sheet and the Word documents are downloaded at the end.

The machine-readable description is served at /api/openapi.json.
"""
from __future__ import annotations

import secrets
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import contract, decide, fee, ingest, volumes

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CODES = volumes.VOLUME_CODES
GROUPS = ("kt", "ko", "kz", "kj")


class VolumeChange(BaseModel):
    value: str = Field(description="Volume in m3, e.g. \"36036.5\"")
    reason: str = Field(min_length=3, description="Why the value differs from the recommendation (kept in the audit log)")
    actor: str = Field(default="", description="The person in the calling system who decided")


class Approval(BaseModel):
    """Every field is optional: what is left out takes the system's recommendation."""
    kt: str | None = None
    ko: str | None = None
    kz: str | None = None
    kz_value: str | None = None
    kj: str | None = None
    k_council: str | None = None
    bhm: str | None = None
    contract_number: str = ""
    contract_date: str = ""
    actor: str = Field(default="", description="The person in the calling system who approved")


class StreetList(BaseModel):
    settlement: str = Field(description="\"Samarqand shahri\" or \"Payariq tumani\"")
    streets: list[str]
    basis: str = Field(min_length=3, description="Decision number and date the list rests on")
    actor: str = ""


def register(app: FastAPI, h: SimpleNamespace) -> None:
    """Attach the API to the application. `h` carries the application's own functions (see app.create_app)."""

    def client(x_api_key: str = Header(default="")) -> str:
        """The integrating system behind the key. Without configured keys the API is closed."""
        if not h.keys:
            raise HTTPException(503, "API kalitlari sozlanmagan (FEECALC_API_KEYS)")
        for key, name in h.keys.items():
            if secrets.compare_digest(key.encode(), x_api_key.encode()):
                return name
        raise HTTPException(401, "X-API-Key noto'g'ri yoki berilmagan")

    router = APIRouter(prefix="/api/v1", tags=["integration"])

    def who(name: str, actor: str) -> str:
        return f"{name}: {actor.strip()[:80]}" if actor.strip() else name

    def summary(job: dict) -> dict:
        state = h.job_state(job)
        return {"id": job["id"], "external_id": job.get("external_id"), "title": job["title"], "created": job["created"],
                "created_by": job["created_by"], **state, "finalized_at": job.get("finalized_at"),
                "links": {name: f"/api/v1/jobs/{job['id']}/{name}" for name in ("recommendation", "values", "sheet")}}

    def source(v: dict | None) -> dict | None:
        """A value as the integrating system needs it: what was read, how it fared, where it stands in the document."""
        if v is None:
            return None
        return {"value": v.get("value"), "number": v.get("number"), "unit": v.get("unit"), "status": v["status"],
                "reason": v.get("reason"), "file": v.get("file"), "page": v.get("page"), "snippet": v.get("source_text")}

    def recommendation(job: dict) -> dict:
        view = h.build_view(job)
        rec = view["recommendation"]
        calc = h.calc_inputs(job, rec)
        values = {c: view["decisions"][c]["value"] if view["decisions"][c]["valid"] else view["volumes"][c]["auto"]["value"]
                  for c in CODES}
        negative = (all(v is not None for v in values.values()) and Decimal(values["Hb"]) + Decimal(values["Hyu"])
                    < Decimal(values["Ha"]) + Decimal(values["Ht"]) + Decimal(values["Hu"]))
        fee_data = {"result": None, "missing": [], "undecided": [], "error": "negative chargeable volume"} if negative \
            else h.compute_fee(view, calc, proposed=True)
        return {
            "job": summary(job), "object": source(rec["description"]), "place": rec["place"],
            "volumes": {c: {"value": values[c], "recommended": view["volumes"][c]["auto"]["value"],
                            "formula": view["volumes"][c]["auto"]["formula"], "assumptions": view["volumes"][c]["auto"]["assumptions"],
                            "strict": view["volumes"][c]["auto"]["strict"],
                            "decision": view["decisions"][c]["decision"] if view["decisions"][c]["valid"] else None} for c in CODES},
            "coefficients": {g: {"code": calc[g] or None, "recommended": rec["coefficients"][g]["code"],
                                 "basis": rec["coefficients"][g]["basis"], "assumed": rec["coefficients"][g]["assumed"],
                                 "page": rec["coefficients"][g]["page"], "snippet": rec["coefficients"][g]["snippet"],
                                 "options": [{"code": o.code, "value": None if o.value is None else str(o.value), "text": o.text}
                                             for o in fee.GROUPS[g]]} for g in GROUPS},
            "k_council": {"value": calc["k_council"], "basis": rec["k_council"]["basis"]},
            "bhm": {"value": calc["bhm"], "basis": rec["bhm"]["basis"]},
            "fee": fee_data.get("result"), "fee_error": fee_data.get("error"),
            "open": rec["open"] if not fee_data.get("result") else [], "assumptions": rec["assumptions"],
            "exemptions": rec["exemptions"], "expertise": view.get("expertise"), "attention": view["attention"],
            "norms": view["norms"], "coverage": view["coverage"],
            "ready_to_approve": bool(fee_data.get("result")),
        }

    @router.get("/health")
    def health(_: str = Depends(client)) -> dict:
        """Whether jobs can be read right now."""
        from .. import llm
        return {"model1": llm.port_open(llm.model1()), "model2": llm.port_open(llm.model2()),
                "queued": len(h.store.unfinished_job_ids()), "norm_documents": len(h.knowledge.documents())}

    @router.post("/jobs", status_code=202)
    async def create_job(name: str = Depends(client), conclusion: UploadFile = File(..., description="Expertise conclusion, PDF"),
                         project: list[UploadFile] = File(default_factory=list, description="Project album PDFs (optional, several)"),
                         title: str = Form(""), external_id: str = Form("", description="Your identifier; repeating it returns the same job"),
                         actor: str = Form("")) -> dict:
        """Upload the documents; reading starts in the background. Poll GET /jobs/{id} until status is `ready`."""
        if external_id.strip():
            existing = h.store.job_by_external(name, external_id.strip())
            if existing is not None:
                return summary(h.get_job(existing))
        job_id = await h.new_job(title, who(name, actor), conclusion, project, name, external_id.strip() or None)
        return summary(h.get_job(job_id))

    @router.get("/jobs")
    def list_jobs(status: str = "", _: str = Depends(client)) -> list[dict]:
        return [{k: j[k] for k in ("id", "external_id", "client", "title", "created", "created_by", "status", "stage", "finalized_at")}
                for j in h.store.jobs() if not status or j["status"] == status]

    @router.get("/jobs/{job_id}")
    def job(job_id: int, _: str = Depends(client)) -> dict:
        """Status: queued, running (with requests_done / requests_total), ready, failed (with error and hint), finalized."""
        return summary(h.get_job(job_id))

    @router.get("/jobs/{job_id}/recommendation")
    def job_recommendation(job_id: int, _: str = Depends(client)) -> dict:
        """Everything the system proposes: five volumes, four coefficients, the fee, assumptions, open items, checks."""
        return recommendation(h.get_job(job_id, need_result=True))

    @router.get("/jobs/{job_id}/values")
    def job_values(job_id: int, _: str = Depends(client)) -> dict:
        """Every value read from the documents with its status and source (file, page, verbatim snippet)."""
        view = h.build_view(h.get_job(job_id, need_result=True))
        row = lambda r: {"label": r["label"], "project": source(r["project"]), "conclusion": source(r["conclusion"]),  # noqa: E731
                         "check": r["check"], "models_agree": {"project": r["agreement_project"], "conclusion": r["agreement_conclusion"]}}
        return {"fields": [row(r) for r in view["scalar_rows"]],
                "buildings": [{"title": b["title"], "count": b["count"], "fields": [row(r) for r in b["fields"]]} for b in view["buildings"]],
                "hb_rows": [{"id": r["id"], "name": r["name"], "count": r["count"], "volume": r["volume"], "formula": r["formula"],
                             "included": r["included"], "project": source(r["project"]), "conclusion": source(r["conclusion"])}
                            for r in view["hb_rows"]],
                "components": [{k: c[k] for k in ("id", "name", "level_text", "area", "category", "height", "volume", "formula",
                                                  "included", "file", "page")} for c in view["components"]],
                "plans": [{"file": s["file"], "page": s["page"], "count": s["count"], "area": s["area"],
                           "by_category": s["by_category"]} for s in view["plans"]],
                "checks": view["checks"]}

    @router.put("/jobs/{job_id}/volumes/{code}")
    def change_volume(job_id: int, code: str, body: VolumeChange, name: str = Depends(client)) -> dict:
        """Set a volume to another value than recommended; the reason is mandatory."""
        job = h.get_job(job_id, need_result=True, editable=True)
        if code not in CODES:
            raise HTTPException(404, "Bunday hajm yo'q")
        value = h.decimal_field(body.value, code, Decimal("0"))
        view = h.build_view(job)
        old = view["decisions"][code]["decision"]
        user = who(name, body.actor)
        h.store.set_decision(job_id, code, "changed", str(value), view["volumes"][code]["auto"]["value"], h.reason_field(body.reason), user)
        h.store.audit(job_id, user, "volume_changed", target=code, old=old["value"] if old else "", new=str(value), reason=body.reason)
        return recommendation(h.get_job(job_id))

    @router.post("/jobs/{job_id}/approve")
    def approve(job_id: int, body: Approval, name: str = Depends(client)) -> dict:
        """Accept the recommendation (with the overrides given here) and finalize. Answers 409 with what is still open."""
        job = h.get_job(job_id, need_result=True, editable=True)
        calc = h.calc_inputs(job, h.build_view(job)["recommendation"])
        for key in (*GROUPS, "kz_value", "k_council", "bhm"):
            if getattr(body, key) is not None:
                calc[key] = getattr(body, key).strip()
        return h.approve_job(job_id, who(name, body.actor), calc, body.contract_number, body.contract_date)

    @router.get("/jobs/{job_id}/sheet")
    def sheet(job_id: int, _: str = Depends(client)) -> dict:
        """The finalized calculation sheet: values with sources, checks, decisions, audit trail, versions."""
        return h.final_sheet(job_id)

    @router.get("/jobs/{job_id}/annex.docx")
    def annex(job_id: int, _: str = Depends(client)) -> Response:
        return Response(contract.annex(h.final_sheet(job_id)), media_type=DOCX)

    @router.get("/jobs/{job_id}/contract.docx")
    def contract_file(job_id: int, _: str = Depends(client)) -> Response:
        if not h.contract_template().exists():
            raise HTTPException(409, "Shartnoma shabloni yuklanmagan")
        return Response(contract.fill(h.contract_template(), h.final_sheet(job_id)), media_type=DOCX)

    @router.get("/jobs/{job_id}/pages/{file}/{page}.png")
    def page_image(job_id: int, file: str, page: int, _: str = Depends(client)) -> Response:
        """The page image a value's source points to."""
        _f, p = h.find_page(h.get_job(job_id, need_result=True), file, page)
        return FileResponse(p["image"], media_type="image/png")

    @router.get("/jobs/{job_id}/pages/{file}/{page}/rects")
    def rects(job_id: int, file: str, page: int, snippet: str, _: str = Depends(client)) -> dict:
        """Where a snippet stands on the page image (pixels): [x, y, width, height] rectangles."""
        from pathlib import Path
        f, p = h.find_page(h.get_job(job_id, need_result=True), file, page)
        z = p["zoom"]
        found = ingest.snippet_rects(Path(f["path"]), page, snippet)
        return {"width": p["width"] * z, "height": p["height"] * z,
                "rects": [[x0 * z, y0 * z, (x1 - x0) * z, (y1 - y0) * z] for x0, y0, x1, y1 in found]}

    @router.get("/reference")
    def reference(_: str = Depends(client)) -> dict:
        """The reference data the recommendation relies on."""
        ref = h.load_reference()
        return {"bhm": {"value": str(ref.bhm), "basis": ref.bhm_basis},
                "k_council": {"value": str(ref.k_council), "basis": ref.k_council_basis},
                "zones": {place: {"code": code, "basis": basis} for place, (code, basis) in ref.zones.items()},
                "central_streets": {place: [{"street": s, "basis": b} for s, b in streets] for place, streets in ref.central_streets.items()},
                "norm_documents": [{"id": d.doc_id, "designation": d.designation, "title": d.title, "url": d.url, "clauses": d.clauses}
                                   for d in h.knowledge.documents()]}

    @router.put("/reference/central-streets")
    def set_streets(body: StreetList, name: str = Depends(client)) -> dict:
        """Add central thoroughfare streets of one settlement (coefficient Kj)."""
        place = h.settlement_key(body.settlement)
        names = [s.strip() for s in body.streets if decide.street_key(s)]
        if not names:
            raise HTTPException(400, "Kamida bitta ko'cha nomi kerak")
        for street in names:
            h.store.set_reference("central_street", f"{place}|{decide.street_key(street)}", street[:120], body.basis, who(name, body.actor))
        return reference(name)

    app.include_router(router)
