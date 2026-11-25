"""Pipeline: ingest -> sheet types -> extraction (each document separately) -> categories.

`run_extraction` is the slow, model-backed part and produces an immutable result.
`assess` is pure code: it combines that result with staff decisions (overrides, permitted
floors, chosen building) into components, volume proposals and checks.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from . import classify, extract, ingest, llm, plans, verify, volumes
from .fee import fmt_num

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
                   progress: Progress = lambda stage: None,
                   report: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """Read both documents with every available model. Any failure propagates to the job boundary.

    `project_pdfs` may be empty: then only the conclusion is read and every comparison stays
    in the "project not provided" state. `report(done, total)` is called as reading requests
    finish (a response found in the raw store counts as finished), so a caller can show progress.
    """
    m1 = llm.model1()
    if not llm.server_ready(m1):
        raise llm.ModelError(f"1-model serveri tayyor emas: {m1.url}")
    m2 = llm.model2()
    models = [m1] + ([m2] if llm.server_ready(m2) else [])
    raw_dir, image_dir = job_dir / "raw", job_dir / "images"
    timing: dict[str, float] = {}

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

    total = len(models) * (len(extract.conclusion_chunks(conclusion_pages).chunks) + sum(
        len(extract.project_chunks(pdf, pages, kinds).chunks) for pdf, pages, kinds in project_files))
    finished = 0
    counter = threading.Lock()  # requests finish on several threads

    def tick() -> None:
        nonlocal finished
        with counter:
            finished += 1
            report(finished, total)

    report(0, total)

    # Each document is read separately; the two models run side by side (separate servers).
    def read_all(reader: Callable[[llm.Model], dict]) -> dict[str, dict]:
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            return dict(zip([m.key for m in models], pool.map(reader, models)))

    def timed(name: str, reader: Callable[[llm.Model], dict]) -> dict[str, dict]:
        start = time.monotonic()
        out = read_all(reader)
        timing[name] = time.monotonic() - start
        return out

    # The two documents are independent, so they are read at the same time (still in separate requests:
    # no request ever contains text from both). The conclusion is one long generation that leaves the
    # server mostly idle; the project's many short requests fill that idle capacity.
    progress("project" if project_files else "conclusion")
    with ThreadPoolExecutor(max_workers=2) as documents:
        conclusion_job = documents.submit(timed, "conclusion",
                                          lambda m: extract.read_conclusion(m, conclusion_pages, raw_dir, tick))
        project = (timed("project", lambda m: extract.read_project(m, project_files, raw_dir, tick))
                   if project_files else {})
        progress("conclusion")
        conclusion = conclusion_job.result()  # an error is raised here

    start = time.monotonic()
    progress("categories")
    suggestions: dict[str, str] = {}
    if project:
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
    warnings = list(conclusion["model1"]["warnings"]) + (list(project["model1"]["warnings"]) if project else [])
    if not project_files:
        warnings.append("Loyiha berilmagan: faqat xulosa o'qildi, solishtirishlar bajarilmaydi")

    progress("done")
    return {
        "files": files,
        "models": {m.key: {"name": m.name, "vllm_version": llm.vllm_version(m)} for m in models},
        "prompt_version": extract.PROMPT_VERSION,
        "project": project, "conclusion": conclusion,
        "category_suggestions": suggestions, "level_marks": marks, "warnings": warnings,
        "object": object_description(conclusion_pages), "plans": read_plans(files), "plan_index": read_plan_index(files),
        "timing": {k: round(v, 2) for k, v in timing.items()},
    }


# ---------- floor plans, read by code from the text layer ----------
def read_plans(files: list[dict]) -> list[dict]:
    """Room labels of every floor-plan sheet of the project (see plans.py). `files` is result["files"]."""
    out: list[dict] = []
    for f in files:
        if f["role"] != "project":
            continue
        for p in f["pages"]:
            if (p["sheet"] or {}).get("kind") == "floor_plan" and not p["textless"]:
                rooms = plans.plan_rooms(Path(f["path"]), p["number"])
                out.append({"file": f["name"], "page": p["number"], "rooms": rooms, **plans.summary(rooms)})
    return out


def read_plan_index(files: list[dict]) -> list[dict]:
    """Which storeys every plan sheet of the project shows (from the title blocks; see plans.sheet_storeys)."""
    out: list[dict] = []
    for f in files:
        if f["role"] != "project":
            continue
        for page in ingest.ingest(Path(f["path"])):
            found = None if page.textless else plans.sheet_storeys(ingest.stamp_text(page))
            if found:
                out.append({"file": f["name"], "page": page.number, **found})
    return out


# ---------- the object as the conclusion describes it (pure code) ----------
_OBJECT_LABEL = re.compile(r"^\s*(?:Obyekt(?:ning)?\s+nomi|Объект(?:нинг)?\s+номи|Наименование\s+объекта)\s*[:\-–]\s*", re.I)
_NEXT_FIELD = re.compile(r"^\s*(?:\d+(?:\.\d+)+[.)]?\s|[^:]{2,45}:\s)")


def object_description(pages: list[ingest.Page]) -> dict | None:
    """The "Obyekt nomi" paragraph of a conclusion, verbatim: what is built and where.

    Read by code from the page text, so it needs no verification against a model. It carries the
    full address and the kind of object, which the recommendation (decide.py) relies on.
    """
    for page in pages[:2]:
        lines = extract.page_line_text(page).splitlines()
        for i, line in enumerate(lines):
            m = _OBJECT_LABEL.match(line)
            if not m:
                continue
            text = line[m.end():].strip()
            for following in lines[i + 1:i + 8]:
                if _NEXT_FIELD.match(following):
                    break
                # a word broken at the line end ("36-" / "manzilda") is joined without a space
                text += ("" if text.endswith("-") else " ") + following.strip()
            return {"field": "object", "value": text.strip(), "number": None, "unit": None, "file": page.file,
                    "page": page.number, "source_text": line.strip(), "status": verify.ACCEPTED, "reason": None,
                    "flags": []}
    return None


# ---------- assessment (pure code) ----------

SCALAR_ROWS: tuple[tuple[str, str | None, str | None], ...] = (
    ("Manzil", "address", "address"),
    ("Xulosa raqami", None, "conclusion_number"),
    ("Xulosa sanasi", None, "conclusion_date"),
    ("Murojaat raqami", None, "application_number"),
    ("Buyurtmachi", None, "customer"),
    ("Qurilish turi", None, "construction_type"),
    ("Loyiha shifri", "project_code", None),
)
# (field in the pair table, project field, conclusion field, how it is compared)
PAIR_FIELDS: tuple[tuple[str, str | None, str | None, str], ...] = (
    ("volume", "volume", "volume", "scaled"),
    ("volume_above_ground", "volume_above_ground", None, ""),
    ("volume_below_ground", "volume_below_ground", None, ""),
    ("footprint_area", "footprint_area", "footprint_area", "scaled"),
    ("total_area", "total_area", "total_area", "scaled"),
    ("usable_area", None, "usable_area", ""),
    ("count", "count", "count", ""),
    ("floors", "floors", "floors", "equal"),
    ("apartments", "apartments", "apartments", "scaled"),
    ("plan_size", "plan_size", "plan_size", "plan"),
    ("reference", None, "reference", ""),
)
EMPTY_PROJECT: dict = {"doc": "project", "scalars": {k: extract.empty_value(k, "model1")
                                                       for k in (*extract.PROJECT_SCALARS, "address")},
                       "site_areas": [], "buildings": [], "area_breakdown": [], "floor_heights": [], "rooms": [],
                       "requests": [], "warnings": []}


def _num(v: dict | None) -> Decimal | None:
    if v and v.get("status") == verify.ACCEPTED and v.get("number") is not None:
        return Decimal(v["number"])
    return None


def _text(v: dict | None) -> str | None:
    return str(v["value"]) if v and v.get("status") == verify.ACCEPTED and v.get("value") else None


def _other_model_building(other: dict | None, building: dict | None, alone: bool) -> dict | None:
    """The second model's record of the same building. The two models may word the name differently,
    so the match is by name, then by being the only building on both sides, then by equal volume
    and built-up area."""
    if other is None or building is None:
        return None
    key = extract.name_key(building["name"])
    hits = [b for b in other["buildings"] if key and extract.name_key(b["name"]) == key]
    if len(hits) == 1:
        return hits[0]
    if alone and len(other["buildings"]) == 1:
        return other["buildings"][0]
    hits = [b for b in other["buildings"]
            if all(_num(b[f]) is not None and _num(b[f]) == _num(building[f]) for f in ("volume", "footprint_area"))]
    return hits[0] if len(hits) == 1 else None


def _site_checks(doc_label: str, code: str, site: list[dict], footprints: list[Decimal | None]) -> list[verify.Check]:
    """Land-plot balance of one document: parts against the total, building footprints against "built-up"."""
    checks: list[verify.Check] = []
    seen: set[tuple[str, Decimal]] = set()
    accepted: list[dict] = []
    for x in site:  # the same figure repeated on several sheets counts once
        if x["status"] == verify.ACCEPTED and x["number"] is not None and (x["kind"], Decimal(x["number"])) not in seen:
            seen.add((x["kind"], Decimal(x["number"])))
            accepted.append(x)
    totals = [Decimal(x["number"]) for x in accepted if x["kind"] == "total"]
    parts = [x for x in accepted if x["kind"] != "total"]
    kinds = [x["kind"] for x in parts]
    if len(totals) == 1 and parts:
        name = f"Hudud balansi ({doc_label})"
        if len(set(kinds)) < len(kinds):
            checks.append(verify.Check(f"site_balance_{code}", name, verify.NOT_RUN,
                                       "bir turdagi ko'rsatkich bir necha marta berilgan (bosqichlar yoki variantlar) — "
                                       "balansni xodim tekshiradi"))
        else:
            s = sum((Decimal(x["number"]) for x in parts), Decimal(0))
            expr = " + ".join(fmt_num(Decimal(x["number"])) for x in parts) + f" = {fmt_num(s)}; hudud maydoni {fmt_num(totals[0])}"
            status = verify.OK if s == totals[0] else verify.WARNING
            checks.append(verify.Check(f"site_balance_{code}", name, status,
                                       expr + ("" if status == verify.OK else f" — balans yopilmaydi (farq {fmt_num(totals[0] - s)})")))
    built = [Decimal(x["number"]) for x in accepted if x["kind"] == "built_up"]
    known = [f for f in footprints if f is not None]
    if built and known and len(known) == len(footprints):
        s = sum(known, Decimal(0))
        nearest = min(built, key=lambda b: abs(b - s))
        status = verify.OK if s == nearest else verify.WARNING
        checks.append(verify.Check(f"site_footprints_{code}", f"Binolar qurilish maydonlari yig'indisi = hududdagi qurilish osti maydoni ({doc_label})",
                                   status, f"binolar yig'indisi {fmt_num(s)}; hudud ko'rsatkichi {fmt_num(nearest)}"
                                   + ("" if status == verify.OK else f"; farq {fmt_num(s - nearest)}")))
    return checks


def _building_heights(building: dict) -> tuple[list[Decimal], str]:
    """Heights for the lower-bound check of one conclusion building: basement (if stated), every
    storey, and the lowest attic height (if stated). When the storey count is not accepted, only
    the storeys the height statements name explicitly ("1,2,3 qavatlarda h=3,0") are used."""
    heights = volumes.collect_heights([], building.get("floor_heights", []))
    if not heights:
        return [], "qavat balandliklari yo'q"
    floors = _num(building["floors"])
    named = sorted({f for h in heights for f in h.level.floors()})
    numbers = list(range(1, int(floors) + 1)) if floors is not None else named
    if not numbers:
        return [], "qavatlar soni qabul qilinmagan va balandliklarda qavatlar ko'rsatilmagan"
    out: list[Decimal] = []
    parts: list[str] = []
    basement, _ = volumes.resolve_height(volumes.Level("basement"), heights)
    if basement is not None:
        out.append(basement)
        parts.append(f"yerto'la {fmt_num(basement)}")
    for f in numbers:
        h, _ = volumes.resolve_height(volumes.Level("floor", f), heights)
        if h is None:
            return [], f"{f}-qavat balandligi hujjatda aniqlanmadi"
        out.append(h)
    parts.append(f"{len(numbers)} qavat" + ("" if floors is not None else " (balandliklarda nomlangan)"))
    attic = [h.value for h in heights if h.level.kind == "technical"]
    if attic:
        out.append(min(attic))
        parts.append(f"mansarda/texnik (eng pasti) {fmt_num(min(attic))}")
    return out, " + ".join(parts)


def assess(result: dict, overrides: dict[str, dict] | None = None, permitted_floors: int | None = None,
           matches: dict[str, int] | None = None) -> dict:
    """Combine the extraction result with staff decisions. Pure code, no model."""
    overrides = overrides or {}
    has_project = bool(result["project"])
    p1 = result["project"]["model1"] if has_project else EMPTY_PROJECT
    c1 = result["conclusion"]["model1"]
    p2 = result["project"].get("model2") if has_project else None
    c2 = result["conclusion"].get("model2")
    two_models = c2 is not None
    checks: list[verify.Check] = []
    disagreements: list[str] = []

    def agreement(a: dict | None, b: dict | None, where: str) -> str | None:
        if not two_models or a is None:
            return None
        verdict = verify.model_agreement(a, b)
        if verdict == "mismatch":
            disagreements.append(where)
        return verdict

    # ----- object-level fields -----
    scalar_rows: list[dict] = []
    for label, pf, cf in SCALAR_ROWS:
        pv, cv = (p1["scalars"][pf] if pf and has_project else None), (c1["scalars"][cf] if cf else None)
        scalar_rows.append({
            "label": label, "project": pv, "conclusion": cv,
            "project_model2": p2["scalars"][pf] if (pf and p2) else None,
            "conclusion_model2": c2["scalars"][cf] if (cf and c2) else None,
            "agreement_project": agreement(pv, p2["scalars"][pf] if (pf and p2) else None, f"{label} (loyiha)") if pf and p2 else None,
            "agreement_conclusion": agreement(cv, c2["scalars"][cf] if (cf and c2) else None, f"{label} (xulosa)") if cf else None,
            "check": None})
    pa = p1["scalars"]["address"]
    variants = [str(v["value"]) for v in pa.get("variants", [])] or ([str(pa["value"])] if _text(pa) else [])
    address_check = verify.check_address(variants, _text(c1["scalars"]["address"]))
    if not has_project:
        address_check = verify.Check("address", address_check.name, verify.NOT_RUN, "loyiha berilmagan")
    checks.append(address_check)
    scalar_rows[0]["check"] = address_check.dict()

    # ----- buildings: pairs of (project building, conclusion building) -----
    pairs = volumes.match_buildings(p1["buildings"], c1["buildings"], matches)
    building_rows: list[dict] = []
    for n, pair in enumerate(pairs):
        pb = p1["buildings"][pair["p"]] if pair["p"] is not None else None
        cb = c1["buildings"][pair["c"]] if pair["c"] is not None else None
        pb2 = _other_model_building(p2, pb, len(p1["buildings"]) == 1)
        cb2 = _other_model_building(c2, cb, len(c1["buildings"]) == 1)
        title = " / ".join(str(b["name"]["value"]) for b in (cb, pb) if b and b["name"].get("value")) or "(nomsiz)"
        if cb and cb.get("type", {}).get("value"):
            title = f"{cb['type']['value']}: {title}"
        count = (_num(cb["count"]) if cb else None) or (_num(pb["count"]) if pb else None)
        fields: list[dict] = []
        for field, pf, cf, how in PAIR_FIELDS:
            pv = pb[pf] if (pb and pf) else None
            cv = cb[cf] if (cb and cf) else None
            if (pv is None or pv["status"] == verify.NOT_FOUND) and (cv is None or cv["status"] == verify.NOT_FOUND):
                continue
            label = extract.FIELD_LABELS[field]
            check: verify.Check | None = None
            if pb and cb and how:
                code = f"b{n}_{field}"
                name = f"{title}: {label} — loyiha = xulosa"
                if how == "scaled":
                    check = verify.check_equal_scaled(code, name, _num(pv), _num(cv), count)
                elif how == "equal":
                    check = verify.check_equal(code, name, _num(pv), _num(cv))
                else:
                    dp, dc = verify.plan_dimensions(_text(pv)), verify.plan_dimensions(_text(cv))
                    if not dp or not dc:
                        check = verify.Check(code, name, verify.NOT_RUN, "tarx o'lchami hujjatlardan birida yo'q")
                    else:
                        same = sorted(dp) == sorted(dc)
                        check = verify.Check(code, name, verify.OK if same else verify.MISMATCH,
                                             f"loyiha «{_text(pv)}», xulosa «{_text(cv)}»")
                checks.append(check)
            where = f"{title}: {label}"
            fields.append({
                "field": field, "label": label, "project": pv, "conclusion": cv,
                "project_model2": pb2[pf] if (pb2 and pf) else None,
                "conclusion_model2": cb2[cf] if (cb2 and cf) else None,
                "agreement_project": agreement(pv, pb2[pf] if (pb2 and pf) else None, where + " (loyiha)") if (p2 and pv) else None,
                "agreement_conclusion": agreement(cv, cb2[cf] if (cb2 and cf) else None, where + " (xulosa)") if cv else None,
                "check": check.dict() if check else None})
        building_rows.append({"title": title, "pair": pair, "fields": fields, "count": None if count is None else str(count),
                              "heights": (cb or {}).get("floor_heights", [])})
        if pb:
            s = pb
            if any(s[k]["status"] != verify.NOT_FOUND for k in ("volume_above_ground", "volume_below_ground")):
                checks.append(verify.check_sum(f"b{n}_sum_volume", f"{title}: yer usti + yer osti = jami hajm (loyiha)",
                                               [_num(s["volume_above_ground"]), _num(s["volume_below_ground"])],
                                               _num(s["volume"])))
        if cb:
            hs, how_h = _building_heights(cb)
            bound = verify.check_volume_lower_bound(_num(cb["volume"]), _num(cb["footprint_area"]), hs)
            checks.append(verify.Check(f"b{n}_volume_lower_bound", f"{title}: {bound.name} (xulosa)", bound.status,
                                       bound.detail + (f" [{how_h}]" if how_h else "")))
            if cb["plan_size"]["status"] != verify.NOT_FOUND:
                checks.append(verify.check_plan_vs_footprint(f"b{n}_plan", f"{title}: tarx o'lchami va qurilish maydoni (xulosa)",
                                                             _text(cb["plan_size"]), _num(cb["footprint_area"])))

    # ----- sums and balances -----
    if len(p1["buildings"]) == 1 and p1["area_breakdown"]:
        checks.append(verify.check_sum("sum_area", "Sathlar maydoni yig'indisi = umumiy maydon (loyiha)",
                                       [_num(x) for x in p1["area_breakdown"]], _num(p1["buildings"][0]["total_area"])))
    if any(v["status"] != verify.NOT_FOUND for v in c1["totals"].values()):
        for f in extract.TOTAL_FIELDS:
            # a row that stands for several identical buildings ("Tip-1 (2 ta)") counts that many times
            parts = [None if _num(b[f]) is None else _num(b[f]) * (_num(b["count"]) or 1) for b in c1["buildings"]]
            checks.append(verify.check_sum(f"conclusion_total_{f}", f"Xulosa «Jami» qatori: {extract.FIELD_LABELS[f]}",
                                           parts, _num(c1["totals"][f])))
    checks.extend(_site_checks("xulosa", "conclusion", c1["site_areas"],
                               [(_num(b["footprint_area"]) * (_num(b["count"]) or 1)) if _num(b["footprint_area"]) is not None
                                else None for b in c1["buildings"] if b["reference"]["status"] == verify.NOT_FOUND]))
    if has_project:
        checks.extend(_site_checks("loyiha", "project", p1["site_areas"],
                                   [_num(b["footprint_area"]) for b in p1["buildings"]]))
        # a project sheet for one building states that building's footprint, not the plot balance:
        # plot figures are compared only when the project gives the plot total as well
        project_has_total = any(x["kind"] == "total" and x["status"] == verify.ACCEPTED for x in p1["site_areas"])
        for kind in (("total", "built_up", "paving", "greenery") if project_has_total else ()):
            a = [Decimal(x["number"]) for x in p1["site_areas"] if x["kind"] == kind and x["status"] == verify.ACCEPTED]
            b = [Decimal(x["number"]) for x in c1["site_areas"] if x["kind"] == kind and x["status"] == verify.ACCEPTED]
            if len(set(a)) == 1 and len(set(b)) == 1:
                checks.append(verify.check_equal(f"site_{kind}", f"Hudud: {extract.SITE_KIND_LABELS[kind]} — loyiha = xulosa",
                                                 a[0], b[0]))

    # ----- volumes -----
    rows = volumes.hb_rows(p1["buildings"], c1["buildings"], pairs, overrides)
    for r in rows:
        if r["check"]["status"] == verify.MISMATCH:
            checks.append(verify.Check(r["check"]["code"], r["check"]["name"], verify.MISMATCH, r["check"]["detail"]))
    paired = [pair for pair in pairs if pair["p"] is not None and pair["c"] is not None]
    album_building = c1["buildings"][paired[0]["c"]] if (len(p1["buildings"]) == 1 and paired) else None
    heights = volumes.collect_heights(p1["floor_heights"], album_building["floor_heights"] if album_building else [])
    components = volumes.build_components(p1, heights, result.get("category_suggestions", {}), overrides)
    all_floors = [f for f in (_num(b["floors"]) for b in c1["buildings"] + p1["buildings"]) if f is not None]
    if len(p1["buildings"]) == 1:
        floors = _num(p1["buildings"][0]["floors"]) or (_num(album_building["floors"]) if album_building else None)
        above = _num(p1["buildings"][0]["volume_above_ground"])
        hyu = volumes.hyu_volume(permitted_floors, floors, components, above)
    elif len(c1["buildings"]) == 1 and not p1["buildings"]:
        floors = _num(c1["buildings"][0]["floors"])
        hyu = volumes.hyu_volume(permitted_floors, floors, components, None)
    else:
        floors = max(all_floors) if all_floors else None
        if permitted_floors is not None and floors is not None and permitted_floors >= floors:
            hyu = {"code": "Hyu", "component_ids": [], "proposal": "0", "complete": True,
                   "formula": f"ruxsat etilgan {permitted_floors} ≥ eng baland bino qavatlari {fmt_num(floors)} → 0", "notes": []}
        else:
            hyu = {"code": "Hyu", "component_ids": [], "proposal": None, "complete": False, "formula": "",
                   "notes": ["bir nechta bino: ruxsat etilgan qavatlar sonini kiriting; undan baland binolar bo'lsa "
                             "Hyu ni xodim bino bo'yicha hisoblab kiritadi" if permitted_floors is None else
                             "bir nechta bino va ruxsat etilganidan baland bino bor — Hyu ni xodim kiritadi"]}
    vols = {"Hb": volumes.hb_volume(rows), "Hyu": hyu,
            "Ha": volumes.category_volume("Ha", components), "Ht": volumes.category_volume("Ht", components),
            "Hu": volumes.category_volume("Hu", components)}

    def proposal(code: str) -> Decimal | None:
        return None if vols[code]["proposal"] is None else Decimal(vols[code]["proposal"])

    checks.append(verify.check_deductions_bound(proposal("Ha"), proposal("Ht"), proposal("Hu"), proposal("Hb")))
    if two_models:
        checks.append(verify.Check(
            "model_agreement", "Ikki model javoblari tengligi", verify.MISMATCH if disagreements else verify.OK,
            "mos emas: " + "; ".join(disagreements) if disagreements else "solishtirilgan maydonlarda ikki model javobi mos"))
    industrial, industrial_total = volumes.industrial_indicator(rows)
    return {
        "has_project": has_project, "scalar_rows": scalar_rows, "buildings": building_rows, "pairs": pairs,
        "site": {"project": p1["site_areas"], "conclusion": c1["site_areas"],
                 "project_model2": p2["site_areas"] if p2 else None, "conclusion_model2": c2["site_areas"] if c2 else None},
        "totals": c1["totals"], "checks": [c.dict() for c in checks],
        "hb_rows": rows, "components": components, "volumes": vols,
        "heights": [{"level": h.label, "kind": h.level.kind, "value": str(h.value), "origin": h.origin} for h in heights],
        "floors": None if floors is None else str(floors),
        "industrial": {"rows": industrial, "total": str(industrial_total)},
        "project_buildings": [str(b["name"].get("value") or "(nomsiz)") for b in p1["buildings"]],
        "conclusion_buildings": [str(b["name"].get("value") or "(nomsiz)") for b in c1["buildings"]],
    }
