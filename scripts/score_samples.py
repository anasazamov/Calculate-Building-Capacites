"""Score extraction results (scripts/read_samples.py) against the etalon of the sample set.

For every etalon row the system's answer is classified as:
  correct      - accepted by the system and equal to the etalon
  silent_error - accepted by the system but different from the etalon  (the key metric, must be 0)
  caught       - the model returned something, but verification refused it (unverified / conflict)
  not_found    - the system has no value
  out_of_scope - the system does not read this kind of field at all
  partial      - (addresses only) an accepted address that covers part of the etalon's address line
Fields absent from the etalon are not scored. Only this script reads the etalon files.

Detailed results (they contain document content) go to <out>/<model>/score.json, which is not
committed; the committed summary holds field names and counts only.

Run: python scripts/score_samples.py model1 [model2 ...] [--samples ~/samples] [--out data/eval]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from feecalc import verify

SCALARS = {"xulosa_raqami": "conclusion_number", "xulosa_sanasi": "conclusion_date",
           "murojaat_raqami": "application_number", "manzil": "address", "buyurtmachi": "customer",
           "qurilish_turi": "construction_type", "loyiha_shifri": "project_code"}
BUILDING = {"qurilish_maydoni": "footprint_area", "umumiy_maydon": "total_area", "qurilish_hajmi": "volume",
            "qavatlar_soni": "floors", "xonadonlar_soni": "apartments", "tarx_olchami": "plan_size",
            "foydali_maydon": "usable_area", "havola": "reference", "nomi": "name", "soni": "count",
            "hajm_yer_usti": "volume_above_ground", "hajm_yer_osti": "volume_below_ground"}
TOTALS = {"jami_qurilish_maydoni": "footprint_area", "jami_umumiy_maydon": "total_area", "jami_hajm": "volume"}
TEXT_FIELDS = {"address", "customer", "construction_type", "project_code", "reference", "name"}
NOISE = {"binosi", "bino", "binolari", "binolar", "zdanie", "dom", "loyihaviy"}
MONTHS = {"yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6, "iyul": 7, "avgust": 8,
          "sentabr": 9, "sentyabr": 9, "oktabr": 10, "oktyabr": 10, "noyabr": 11, "dekabr": 12,
          "yanvarya": 1, "fevralya": 2, "marta": 3, "aprelya": 4, "maya": 5, "iyunya": 6, "iyulya": 7,
          "avgusta": 8, "sentyabrya": 9, "oktyabrya": 10, "noyabrya": 11, "dekabrya": 12}


def parse_date(text: str) -> str | None:
    """A date as written ("11.06.2026", "2026 - yil 8 - oktabrdagi") -> YYYY-MM-DD."""
    t = verify.transliterate(text)
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    year = re.search(r"(19|20)\d{2}", t)
    month = next((n for name, n in sorted(MONTHS.items(), key=lambda x: -len(x[0])) if name in t), None)
    rest = t.replace(year.group(0), " ", 1) if year else t
    day = re.search(r"\b(\d{1,2})\b", rest)
    if year and month and day:
        return f"{year.group(0)}-{month:02d}-{int(day.group(1)):02d}"
    return None


def tokens(text: str) -> set[str]:
    return {x for x in re.split(r"[^a-z0-9]+", verify.transliterate(text)) if x and x not in NOISE}


def plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", verify.transliterate(text)).strip()


def text_equal(field: str, system: str, etalon: str) -> bool:
    if field == "address":
        a, b = verify.address_tokens(system), verify.address_tokens(etalon)
        return bool(a) and bool(b) and (b <= a or a <= b)
    a, b = plain(system), plain(etalon)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def classify_address(value: dict | None, page: int, etalon: str, source: str) -> str:
    """Address rows. A project may state its location several times (title block, general data):
    every accepted statement is a variant; the row is correct when one of them names the etalon's
    place. A statement that covers only part of the etalon line is "partial" (incomplete, not wrong)."""
    if value is None or value["status"] == verify.NOT_FOUND:
        return "not_found"
    if value["status"] == verify.UNVERIFIED:
        return "caught"
    variants = value.get("variants") or [{"value": value["value"], "page": value["page"]}]
    if any(text_equal("address", str(v["value"]), etalon) for v in variants):
        return "correct"
    wanted, line = verify.address_tokens(etalon), verify.address_tokens(source)
    on_page = [v for v in variants if v["page"] == page]
    if any(verify.address_tokens(str(v["value"])) & wanted and verify.address_tokens(str(v["value"])) <= (wanted | line)
           for v in on_page):
        return "partial"
    if not on_page:
        return "not_found"
    return "silent_error"


def value_equal(field: str, value: dict, etalon: str) -> bool:
    if field == "conclusion_date":
        return parse_date(str(value["value"])) == etalon
    if field == "plan_size":
        return verify.plan_dimensions(str(value["value"])) == verify.plan_dimensions(etalon)
    if field in TEXT_FIELDS or value.get("number") is None:
        if value.get("variants"):
            return any(text_equal(field, str(v["value"]), etalon) for v in value["variants"])
        return text_equal(field, str(value["value"]), etalon)
    expected = verify.parse_number(etalon)
    return expected is not None and Decimal(value["number"]) == expected


def classify_value(field: str, value: dict | None, etalon: str) -> str:
    if value is None or value["status"] == verify.NOT_FOUND:
        return "not_found"
    equal = value_equal(field, value, etalon)
    if value["status"] == verify.ACCEPTED:
        return "correct" if equal else "silent_error"
    if value["status"] == verify.CONFLICT and equal:
        return "caught"  # the right value is among the variants; the system refused to pick one
    return "caught"


def classify_list(items: list[dict], page: int, etalon: str) -> str:
    """List-type fields (site areas, heights, area breakdown, rooms, level marks): the etalon value
    must be among the system's items read from the same page."""
    expected = verify.parse_number(etalon)
    same_page = [x for x in items if x.get("page") == page and x.get("number") is not None]
    if expected is not None and any(Decimal(x["number"]) == expected and x.get("status", verify.ACCEPTED)
                                    == verify.ACCEPTED for x in same_page):
        return "correct"
    raw = [x for x in items if x.get("page") == page and x.get("status") in (verify.UNVERIFIED, verify.CONFLICT)]
    if expected is not None and any(verify.parse_number(str(x.get("value"))) == expected for x in raw):
        return "caught"
    return "not_found"


def match_building(buildings: list[dict], label: str, alone: bool) -> dict | None:
    if not buildings:
        return None
    if alone and len(buildings) == 1:
        return buildings[0]
    wanted = tokens(label)
    best, best_score = None, 0.0
    for b in buildings:
        have = tokens(f"{b.get('type', {}).get('value') or ''} {b['name'].get('value') or ''}")
        score = len(wanted & have) / max(1, len(wanted | have))   # Jaccard similarity
        if score > best_score:
            best, best_score = b, score
    return best if best_score >= 0.2 else None


def score_row(row: dict, doc: dict | None, level_marks: list[dict], single_building: bool) -> tuple[str, dict | None]:
    """-> (class, the system value used, if any)."""
    field, label, page, etalon = row["maydon"], row["bino"], int(row["sahifa"]), row["qiymat"]
    if doc is None:
        return "not_found", None
    if label == "obyekt":
        if field == "manzil" or field.startswith("manzil_"):
            v = doc["scalars"]["address"]
            return classify_address(v, page, etalon, row["manba_matn"]), v
        if field in SCALARS and SCALARS[field] in doc["scalars"]:
            v = doc["scalars"][SCALARS[field]]
            return classify_value(SCALARS[field], v, etalon), v
        if field.startswith("hudud_"):
            return classify_list(doc["site_areas"], page, etalon), None
        return "out_of_scope", None
    if label == "Jami":
        if field in TOTALS:
            v = doc["totals"][TOTALS[field]]
            return classify_value(TOTALS[field], v, etalon), v
        return "out_of_scope", None
    if field.startswith("balandlik_") and field != "balandlik_bino_max":
        b = match_building(doc["buildings"], label, single_building)
        heights = (b or {}).get("floor_heights", []) + doc.get("floor_heights", [])
        return classify_list(heights, page, etalon), None
    if field.startswith("maydon_"):
        return classify_list(doc.get("area_breakdown", []), page, etalon), None
    if field.startswith("xona_maydoni_"):
        return classify_list([r["area"] for r in doc.get("rooms", [])], page, etalon), None
    if field.startswith("sath_belgisi_"):
        # level marks: read by code on section sheets, and as part of area-breakdown rows elsewhere
        from_rows = []
        for x in doc.get("area_breakdown", []):
            for number, minus in verify.numbers_in_text(x.get("level_mark") or ""):
                from_rows.append({"page": x["page"], "number": str(-number if minus else number), "status": x["status"]})
        return classify_list(level_marks + from_rows, page, etalon), None
    if field in BUILDING:
        b = match_building(doc["buildings"], label, single_building)
        key = BUILDING[field]
        if b is None or key not in b:
            return ("not_found" if b is None or key in ("count", "reference", "usable_area", "volume_above_ground",
                                                         "volume_below_ground", "name") else "out_of_scope"), None
        return classify_value(key, b[key], etalon), b[key]
    return "out_of_scope", None


def score_model(model: str, samples: Path, out: Path) -> dict:
    rows = list(csv.DictReader(open(samples / "etalon_maydonlar.csv", encoding="utf-8-sig")))
    buildings_per_doc = Counter()
    for key in {(r["id"], r["hujjat"], r["bino"]) for r in rows if r["bino"] not in ("obyekt", "Jami")}:
        buildings_per_doc[key[:2]] += 1
    details: list[dict] = []
    errors: dict[str, str] = {}
    results: dict[str, dict] = {}
    for sample_id in sorted({r["id"] for r in rows}):
        path = out / model / f"{sample_id}.json"
        results[sample_id] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"error": "no result"}
        if results[sample_id].get("error"):
            errors[sample_id] = results[sample_id]["error"].strip().splitlines()[-1][:300]
    for r in rows:
        res = results[r["id"]]
        if res.get("error"):
            cls, used = "job_failed", None
        else:
            doc = res["conclusion"] if r["hujjat"] == "xulosa" else res["project"]
            cls, used = score_row(r, doc, res.get("level_marks", []), buildings_per_doc[(r["id"], r["hujjat"])] == 1)
        details.append({"id": r["id"], "doc": r["hujjat"], "page": r["sahifa"], "building": r["bino"],
                        "field": r["maydon"], "etalon": r["qiymat"], "class": cls,
                        "system": None if used is None else {k: used.get(k) for k in
                                                             ("value", "status", "page", "reason", "source_text")}})
    timing = {i: {"conclusion_s": res.get("seconds_conclusion"), "project_s": res.get("seconds_project"),
                  "total_s": res.get("seconds_total")} for i, res in results.items()}
    summary = {
        "model": model, "rows": len(rows), "classes": dict(Counter(d["class"] for d in details)),
        "by_sample": {i: dict(Counter(d["class"] for d in details if d["id"] == i)) for i in results},
        "by_field": {f"{doc}:{field}": dict(Counter(d["class"] for d in details if d["doc"] == doc and d["field"] == field))
                     for doc, field in sorted({(d["doc"], d["field"]) for d in details})},
        "silent_errors": [{k: d[k] for k in ("id", "doc", "page", "field")} for d in details if d["class"] == "silent_error"],
        "failed_samples": errors, "timing": timing,
    }
    (out / model / "score.json").write_text(json.dumps({"summary": summary, "details": details},
                                                       ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def score_pair(first: str, second: str, out: Path) -> dict:
    """The primary model checked by the second one, as in the full system: a value the primary
    model accepted counts as flagged when the second model did not accept the same value."""
    a = json.loads((out / first / "score.json").read_text(encoding="utf-8"))["details"]
    b = json.loads((out / second / "score.json").read_text(encoding="utf-8"))["details"]
    counts: Counter[str] = Counter()
    unflagged: list[dict] = []
    for x, y in zip(a, b):
        assert (x["id"], x["doc"], x["page"], x["building"], x["field"]) == (
            y["id"], y["doc"], y["page"], y["building"], y["field"])
        agree = x["class"] == y["class"] and x["class"] in ("correct", "silent_error") and (
            x["class"] == "correct" or (x["system"] or {}).get("value") == (y["system"] or {}).get("value"))
        if x["class"] == "correct":
            counts["correct_confirmed" if agree else "correct_but_flagged"] += 1
        elif x["class"] == "silent_error":
            counts["silent_error_unflagged" if agree else "silent_error_flagged"] += 1
            if agree:
                unflagged.append({k: x[k] for k in ("id", "doc", "page", "field")})
        else:
            counts[x["class"]] += 1
    return {"primary": first, "second": second, "classes": dict(counts), "silent_errors_unflagged": unflagged}


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("models", nargs="+")
    a.add_argument("--samples", type=Path, default=Path.home() / "samples")
    a.add_argument("--out", type=Path, default=Path("data/eval"))
    a.add_argument("--summary", type=Path, default=Path("test_results"))
    arg = a.parse_args()
    for model in arg.models:
        s = score_model(model, arg.samples, arg.out)
        arg.summary.mkdir(parents=True, exist_ok=True)
        (arg.summary / f"sample_score_{model}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1),
                                                                 encoding="utf-8")
        print(f"== {model}: {s['rows']} etalon rows -> {s['classes']}")
        for sample_id, counts in s["by_sample"].items():
            print(f"   {sample_id}: {counts}  {s['timing'][sample_id]}")
        for e in s["silent_errors"]:
            print(f"   SILENT ERROR: {e}")
        for sample_id, e in s["failed_samples"].items():
            print(f"   FAILED {sample_id}: {e}")
    if len(arg.models) >= 2:
        for first, second in ((arg.models[0], arg.models[1]), (arg.models[1], arg.models[0])):
            pair = score_pair(first, second, arg.out)
            (arg.summary / f"sample_score_{first}_checked_by_{second}.json").write_text(
                json.dumps(pair, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"== {first} checked by {second}: {pair['classes']}")


if __name__ == "__main__":
    main()
