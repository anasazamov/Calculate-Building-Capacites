"""Pipeline: ingest -> sheet types -> extraction (each document separately) -> categories.

`run_extraction` is the slow, model-backed part and produces an immutable result.
`assess` is pure code: it combines that result with staff decisions (overrides, permitted
floors, chosen building) into components, volume proposals and checks.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from . import classify, extract, ingest, llm, verify, volumes

STAGES: tuple[tuple[str, str], ...] = (
    ("ingest", "Sahifalarni o'qish (matn qatlami va rasm)"),
    ("sheets", "Varaq turlarini aniqlash"),
    ("project", "Loyihani o'qish"),
    ("conclusion", "Xulosani o'qish"),
    ("categories", "Xonalarni toifalash"),
    ("done", "Tayyor"),
)

Progress = Callable[[str], None]


def _page_meta(p: ingest.Page, st: classify.SheetType | None) -> dict:
    return {"number": p.number, "width": p.width, "height": p.height, "char_count": p.char_count,
            "textless": p.textless, "image_ratio": p.image_ratio, "image": p.image_path, "zoom": p.image_zoom,
            "sheet": None if st is None else asdict(st)}


def classify_sheets(m: llm.Model, pages: list[ingest.Page], raw_dir: Path) -> dict[int, classify.SheetType]:
    """Rules first; the model is asked only for pages the rules could not settle."""
    kinds = {p.number: classify.sheet_type(p) for p in pages}
    unsure = [p for p in pages if kinds[p.number].ambiguous]

    def one(p: ingest.Page) -> tuple[int, str]:
        return p.number, extract.sheet_type_by_model(m, p, raw_dir)

    with ThreadPoolExecutor(max_workers=extract.PARALLEL) as pool:
        for number, kind in pool.map(one, unsure):
            old = kinds[number]
            kinds[number] = classify.SheetType(kind, "model", False, old.basis, old.has_tep, old.has_rooms)
    return kinds


def run_extraction(job_dir: Path, project_pdfs: list[Path], conclusion_pdf: Path,
                   progress: Progress = lambda stage: None) -> dict:
    """Read both documents with every available model. Any failure propagates to the job boundary."""
    m1 = llm.model1()
    if not llm.server_ready(m1):
        raise llm.ModelError(f"1-model serveri tayyor emas: {m1.url}")
    m2 = llm.model2()
    models = [m1] + ([m2] if llm.server_ready(m2) else [])
    raw_dir, image_dir = job_dir / "raw", job_dir / "images"
    timing: dict[str, float] = {}
    warnings: list[str] = []

    start = time.monotonic()
    progress("ingest")
    project_pages = [(pdf, ingest.ingest(pdf, image_dir)) for pdf in project_pdfs]
    conclusion_pages = ingest.ingest(conclusion_pdf, image_dir)
    timing["ingest"] = time.monotonic() - start

    start = time.monotonic()
    progress("sheets")
    project_files = [(pdf, pages, classify_sheets(m1, pages, raw_dir)) for pdf, pages in project_pages]
    timing["sheets"] = time.monotonic() - start

    files = [{"role": "project", "name": pdf.name, "path": str(pdf), "sha256": ingest.sha256_file(pdf),
              "pages": [_page_meta(p, kinds[p.number]) for p in pages]} for pdf, pages, kinds in project_files]
    files.append({"role": "conclusion", "name": conclusion_pdf.name, "path": str(conclusion_pdf),
                  "sha256": ingest.sha256_file(conclusion_pdf),
                  "pages": [_page_meta(p, None) for p in conclusion_pages]})

    start = time.monotonic()
    progress("project")
    project = {m.key: extract.read_project(m, project_files, raw_dir) for m in models}
    timing["project"] = time.monotonic() - start

    start = time.monotonic()
    progress("conclusion")
    conclusion = {m.key: extract.read_conclusion(m, conclusion_pages, raw_dir) for m in models}
    timing["conclusion"] = time.monotonic() - start

    start = time.monotonic()
    progress("categories")
    pm = project["model1"]
    unknown = sorted(
        {x["name"] for x in pm["area_breakdown"]
         if volumes.needs_model_suggestion(x["name"], "breakdown", x["name"], x.get("level_mark"))}
        | {r["name"] for r in pm["rooms"] if volumes.needs_model_suggestion(r["name"], "room", r.get("level"), None)})
    suggestions = extract.suggest_categories(m1, unknown, raw_dir) if unknown else {}
    timing["categories"] = time.monotonic() - start

    marks: list[dict] = []
    for _pdf, pages, kinds in project_files:
        for p in pages:
            if kinds[p.number].kind == "section":
                marks.extend(extract.level_marks(p))
    for doc in (project["model1"], conclusion["model1"]):
        warnings.extend(doc["warnings"])

    progress("done")
    return {
        "files": files,
        "models": {m.key: {"name": m.name, "vllm_version": llm.vllm_version(m)} for m in models},
        "prompt_version": extract.PROMPT_VERSION,
        "project": project, "conclusion": conclusion,
        "category_suggestions": suggestions, "level_marks": marks, "warnings": warnings,
        "timing": {k: round(v, 2) for k, v in timing.items()},
    }


# ---------- assessment (pure code) ----------

# (label, project scalar, conclusion building field or "scalar:<name>", checked for equality?)
COMPARISON_ROWS: tuple[tuple[str, str | None, str | None, bool], ...] = (
    ("Qurilish hajmi (jami), m³", "volume_total", "volume", True),
    ("Qurilish hajmi (yer usti), m³", "volume_above_ground", None, False),
    ("Qurilish hajmi (yer osti), m³", "volume_below_ground", None, False),
    ("Qurilish maydoni, m²", "footprint_area", "footprint_area", True),
    ("Umumiy maydon, m²", "total_area", "total_area", True),
    ("Qavatlar soni", "floors", "floors", True),
    ("Xonadonlar soni", "apartments", "apartments", True),
    ("Tarx o'lchami", "plan_size", "plan_size", False),
    ("Manzil", "address", "scalar:address", False),
    ("Bino nomi", None, "name", False),
    ("Bino turi", None, "type", False),
    ("Xulosa raqami", None, "scalar:conclusion_number", False),
    ("Xulosa sanasi", None, "scalar:conclusion_date", False),
    ("Buyurtmachi", None, "scalar:customer", False),
    ("Qurilish turi", None, "scalar:construction_type", False),
)


def _num(v: dict | None) -> Decimal | None:
    if v and v.get("status") == verify.ACCEPTED and v.get("number") is not None:
        return Decimal(v["number"])
    return None


def choose_building(project: dict, conclusion: dict, chosen: int | None) -> tuple[int | None, str]:
    """Which row of the conclusion's buildings table this album describes."""
    buildings = conclusion["buildings"]
    if not buildings:
        return None, "xulosadan binolar jadvali o'qilmadi"
    if chosen is not None:
        if not 0 <= chosen < len(buildings):
            raise ValueError(f"Bino indeksi noto'g'ri: {chosen} (binolar soni {len(buildings)})")
        return chosen, "xodim tanlagan"
    if len(buildings) == 1:
        return 0, "xulosada bitta bino"
    for field_p, field_c, what in (("volume_total", "volume", "hajm"), ("footprint_area", "footprint_area",
                                                                        "qurilish maydoni")):
        target = _num(project["scalars"][field_p])
        hits = [i for i, b in enumerate(buildings) if target is not None and _num(b[field_c]) == target]
        if len(hits) == 1:
            return hits[0], f"{what} loyihadagi bilan teng bo'lgan yagona bino (xodim tekshirsin)"
    return None, "xulosada bir nechta bino bor, loyihaga mosini xodim tanlaydi"


def _conclusion_value(conclusion: dict | None, building: dict | None, ref: str | None, model: str) -> dict | None:
    if ref is None or conclusion is None:
        return None
    if ref.startswith("scalar:"):
        return conclusion["scalars"][ref.removeprefix("scalar:")]
    return building[ref] if building else extract.empty_value(ref, model)


def _same_building(conclusion2: dict | None, building1: dict | None) -> dict | None:
    """The second model's row for the same building (matched by name, then by position)."""
    if conclusion2 is None or building1 is None:
        return None
    name = verify.normalize(str(building1["name"].get("value"))).lower()
    hits = [b for b in conclusion2["buildings"] if verify.normalize(str(b["name"].get("value"))).lower() == name]
    return hits[0] if len(hits) == 1 else None


def assess(result: dict, overrides: dict[str, dict] | None = None, permitted_floors: int | None = None,
           chosen_building: int | None = None) -> dict:
    overrides = overrides or {}
    p1, c1 = result["project"]["model1"], result["conclusion"]["model1"]
    p2, c2 = result["project"].get("model2"), result["conclusion"].get("model2")
    index, index_note = choose_building(p1, c1, chosen_building)
    b1 = c1["buildings"][index] if index is not None else None
    b2 = _same_building(c2, b1)

    rows: list[dict] = []
    checks: list[verify.Check] = []
    for label, pf, cf, compare in COMPARISON_ROWS:
        pv1 = p1["scalars"][pf] if pf else None
        pv2 = p2["scalars"][pf] if (pf and p2) else None
        cv1 = _conclusion_value(c1, b1, cf, "model1")
        cv2 = _conclusion_value(c2, b2, cf, "model2") if c2 else None
        row = {"label": label, "project": pv1, "conclusion": cv1, "project_model2": pv2, "conclusion_model2": cv2,
               "agreement_project": verify.model_agreement(pv1, pv2) if (pf and p2) else None,
               "agreement_conclusion": verify.model_agreement(cv1, cv2) if (cf and c2) else None,
               "check": None}
        if compare:
            check = verify.check_equal(f"equal_{pf}", f"{label}: loyiha = xulosa", _num(pv1), _num(cv1))
            row["check"] = check.dict()
            checks.append(check)
        rows.append(row)

    address_p = p1["scalars"]["address"]
    address_c = c1["scalars"]["address"]
    address_check = verify.check_address(
        address_p["value"] if address_p["status"] == verify.ACCEPTED else None,
        address_c["value"] if address_c["status"] == verify.ACCEPTED else None)
    checks.append(address_check)
    for row in rows:
        if row["label"] == "Manzil":
            row["check"] = address_check.dict()

    s = p1["scalars"]
    checks.append(verify.check_sum("sum_volume", "Yer usti + yer osti = jami hajm (loyiha)",
                                   [_num(s["volume_above_ground"]), _num(s["volume_below_ground"])],
                                   _num(s["volume_total"])))
    checks.append(verify.check_sum("sum_area", "Sathlar maydoni yig'indisi = umumiy maydon (loyiha)",
                                   [_num(x) for x in p1["area_breakdown"]], _num(s["total_area"])))
    if len(c1["buildings"]) > 1 or any(v["status"] != verify.NOT_FOUND for v in c1["totals"].values()):
        for f in extract.TOTAL_FIELDS:
            checks.append(verify.check_sum(f"conclusion_total_{f}", f"Xulosa «Jami» qatori: {extract.FIELD_LABELS[f]}",
                                           [_num(b[f]) for b in c1["buildings"]], _num(c1["totals"][f])))

    heights = volumes.collect_heights(p1["floor_heights"], b1["floor_heights"] if b1 else [])
    components = volumes.build_components(p1, heights, result.get("category_suggestions", {}), overrides)
    floors = _num(s["floors"]) or (_num(b1["floors"]) if b1 else None)

    per_floor: list[Decimal] = []
    complete = floors is not None
    if floors is not None:
        for f in range(1, int(floors) + 1):
            h, _ = volumes.resolve_height(volumes.Level("floor", f), heights)
            if h is None:
                complete = False
                break
            per_floor.append(h)
        basement, _ = volumes.resolve_height(volumes.Level("basement"), heights)
        if basement is not None and any(c["level_kind"] == "basement" for c in components):
            per_floor.append(basement)
    checks.append(verify.check_volume_lower_bound(_num(s["volume_total"]), _num(s["footprint_area"]),
                                                  per_floor if complete else []))

    vols = {
        "Hb": volumes.hb_volume(p1, b1),
        "Hyu": volumes.hyu_volume(permitted_floors, floors, components, _num(s["volume_above_ground"])),
        "Ha": volumes.category_volume("Ha", components),
        "Ht": volumes.category_volume("Ht", components),
        "Hu": volumes.category_volume("Hu", components),
    }

    def proposal(code: str) -> Decimal | None:
        return None if vols[code]["proposal"] is None else Decimal(vols[code]["proposal"])

    checks.append(verify.check_deductions_bound(proposal("Ha"), proposal("Ht"), proposal("Hu"), proposal("Hb")))

    disagreements: list[str] = []
    if p2 or c2:
        for row in rows:
            for side, key in (("loyiha", "agreement_project"), ("xulosa", "agreement_conclusion")):
                if row[key] == "mismatch":
                    disagreements.append(f"{row['label']} ({side})")
        status = verify.MISMATCH if disagreements else verify.OK
        checks.append(verify.Check("model_agreement", "Ikki model javoblari tengligi", status,
                                   "mos emas: " + ", ".join(disagreements) if disagreements
                                   else "solishtirilgan maydonlarda ikki model javobi mos"))

    return {
        "building_index": index, "building_note": index_note, "building": b1,
        "building_count": len(c1["buildings"]),
        "rows": rows, "checks": [c.dict() for c in checks], "components": components, "volumes": vols,
        "heights": [{"level": h.label, "kind": h.level.kind, "value": str(h.value), "origin": h.origin}
                    for h in heights],
        "floors": None if floors is None else str(floors),
        "industrial_floors": [(n, str(a), str(h)) for n, a, h in volumes.industrial_floors(components)],
    }
