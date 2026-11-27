"""The order of work for one job, step by step: which tool did what and what came out.

The system follows the sequence a careful person (or an assistant with tools) would follow:
open the documents, find the sheets that matter, read them, check every value against its source,
cross-check the documents and the arithmetic, look up the rules, decide, compute, write it down.
This module only reports that sequence from data that already exists; it decides nothing.
"""
from __future__ import annotations

from collections import Counter

from . import classify, verify

VOLUME_CODES = ("Hb", "Hyu", "Ha", "Ht", "Hu")


def _values(view: dict) -> list[dict]:
    rows = list(view["scalar_rows"]) + [f for b in view["buildings"] for f in b["fields"]]
    return [v for r in rows for v in (r["project"], r["conclusion"]) if v]


def _step(steps: list[dict], title: str, tool: str, result: str, status: str = "ok", anchor: str = "") -> None:
    steps.append({"n": len(steps) + 1, "title": title, "tool": tool, "result": result, "status": status, "anchor": anchor})


def steps(result: dict, view: dict, fee_data: dict | None = None) -> list[dict]:
    out: list[dict] = []
    files = result["files"]
    pages = sum(len(f["pages"]) for f in files)
    textless = sum(1 for f in files for p in f["pages"] if p["textless"])
    _step(out, "Hujjatlarni ochish", "PDF matn qatlami va sahifa rasmlari (PyMuPDF)",
          f"{len(files)} ta fayl, {pages} sahifa" + (f"; {textless} sahifada na matn qatlami, na rasmda o'qiladigan yozuv bor"
                                                     if textless else ""),
          "warning" if textless else "ok")

    with_images = [p for f in files for p in f["pages"] if p.get("image_ratio")]
    vision = result.get("vision_model")
    if with_images and vision:
        words = sum(p.get("image_words") or 0 for p in with_images)
        unclear = sum(p.get("image_unclear") or 0 for p in with_images)
        _step(out, "Rasm ichidagi yozuvni o'qish", f"rasm modeli (VLM, {vision['name']}): har katak va yozuv koordinatasi bilan; "
              "matn qatlamida allaqachon bor yozuv takror qo'shilmaydi",
              f"{len(with_images)} ta sahifada rasm; {words} ta so'z rasmdan o'qildi"
              + (f"; {unclear} ta rasmdagi yozuv ishonchli o'qilmadi — qo'lda ko'ring" if unclear else ""),
              "warning" if unclear else "ok")
    elif with_images:
        _step(out, "Rasm ichidagi yozuvni o'qish", "bu ish rasm modeli qo'shilishidan oldin o'qilgan",
              f"{len(with_images)} ta sahifada rasm bor — rasm ichidagi yozuv uchun ishni «Boshidan qayta o'qish» kerak", "warning")

    kinds = Counter((p["sheet"] or {}).get("kind") for f in files if f["role"] == "project" for p in f["pages"])
    if kinds:
        by_model = sum(1 for f in files for p in f["pages"] if (p["sheet"] or {}).get("method") == "model")
        _step(out, "Kerakli varaqlarni topish", "shtamp va sarlavha bo'yicha qoidalar" + (f"; {by_model} varaq uchun model" if by_model else ""),
              ", ".join(f"{classify.SHEET_LABELS.get(k, 'boshqa').lower()} — {n}" for k, n in kinds.most_common()))

    requests = [r for doc in ("project", "conclusion") for m in result[doc].values() for r in m["requests"]]
    models = ", ".join(m["name"] for m in result["models"].values())
    _step(out, "Xulosa va jadvalli varaqlarni o'qish", f"til modeli ({models}); har hujjat alohida so'rovda",
          f"{len(requests)} ta so'rov; loyiha va xulosa bir so'rovda birga berilmadi")

    statuses = Counter(v["status"] for v in _values(view))
    weak = statuses.get("unverified", 0) + statuses.get("conflict", 0)
    _step(out, "Har qiymatni manbasi bilan tekshirish", "kod: parcha sahifada so'zma-so'z topilishi, sondan qiymat ajralishi",
          ", ".join(f"{verify.STATUS_LABELS[k]} — {n}" for k, n in statuses.most_common()) or "qiymat o'qilmadi",
          "warning" if weak else "ok", "fields")

    plans = view.get("plans") or []
    if plans:
        unnamed = sum(s["unnamed"] for s in plans)
        _step(out, "Qavat rejalarini o'qish", "kod: xona nomi va maydonini koordinata bo'yicha juftlash (model ishlatilmaydi)",
              f"{sum(1 for s in plans if s['rooms'])} ta varaqdan {sum(s['count'] for s in plans)} ta xona yozuvi"
              + (f"; {unnamed} tasining nomi topilmadi" if unnamed else ""), "warning" if unnamed else "ok", "plans")
    if result.get("level_marks"):
        _step(out, "Kesimdan sath belgilarini olish", "kod: matn qatlamidan belgilar",
              f"{len(result['level_marks'])} ta sath belgisi", "ok", "heights")

    checks = Counter(c["status"] for c in view["checks"])
    _step(out, "Hujjatlarni solishtirish va arifmetikani tekshirish", "kod: yig'indilar, loyiha = xulosa, hajm chegaralari",
          ", ".join(f"{verify.CHECK_LABELS[k]} — {n}" for k, n in checks.most_common()) or "tekshiruv yo'q",
          "mismatch" if checks.get("mismatch") else ("warning" if checks.get("warning") else "ok"), "checks")

    if len(result["models"]) > 1:
        disagreements = sum(1 for a in view["attention"] if "ikki model" in a["text"])
        _step(out, "Ikki modelning o'qishini solishtirish", "kod: ikki mustaqil o'qish tengligi",
              f"{disagreements} ta joyda turlicha o'qilgan" if disagreements else "farq topilmadi",
              "warning" if disagreements else "ok", "todo")

    norms = view.get("norms") or []
    if norms:
        n = Counter(f["status"] for f in norms)
        _step(out, "Qoidalarni me'yoriy hujjatlardan qarash", "bilimlar bazasi (lex.uz matnlari): band so'zma-so'z keltiriladi",
              f"{len(norms)} ta talab: " + ", ".join(f"{f['label']} — {c}" for f, c in
                                                   ((next(x for x in norms if x["status"] == k), c) for k, c in n.most_common())),
              "mismatch" if n.get("mismatch") else ("warning" if n.get("warning") else "ok"), "norms")

    judgments = result.get("judgments")
    if judgments:
        asked = {k: len(judgments[k]) for k in ("structures", "rooms", "sheets")}
        from . import judge
        open_items = sum(1 for k in asked for answers in judgments[k].values() if judge.agreed(answers) is None)
        _step(out, "Mulohaza: bino yoki inshoot, xona toifasi, reja qavatlari", "til modeli: qoida va ta'rif savolda, mulohaza bilan; faqat modellar bir xil javob bersa qo'llanadi",
              f"{asked['structures']} ta bino qatori, {asked['rooms']} ta xona nomi, {asked['sheets']} ta reja sarlavhasi so'raldi"
              + (f"; {open_items} tasida modellar kelishmadi yoki aniq emas dedi — ular qo'llanmadi" if open_items else ""),
              "warning" if open_items else "ok")

    rec = view["recommendation"]
    assumed = [c for c in VOLUME_CODES if rec["volumes"][c]["assumptions"] and rec["volumes"][c]["value"] is not None]
    missing = [c for c in VOLUME_CODES if rec["volumes"][c]["value"] is None]
    _step(out, "Hajmlar bo'yicha qaror", "kod qoidalari: xulosa ustunligi, 3 m qoidasi, tipik qavat balandligi",
          "; ".join(f"{c} = {rec['volumes'][c]['value'] if rec['volumes'][c]['value'] is not None else 'aniqlanmadi'}" for c in VOLUME_CODES)
          + (f". Taxmin bilan: {', '.join(assumed)}" if assumed else ""),
          "mismatch" if missing else ("warning" if assumed else "ok"), "volumes")

    undecided = [g.capitalize() for g, d in rec["coefficients"].items() if d["code"] is None]
    _step(out, "Koeffitsiyentlar bo'yicha tavsiya", "kod qoidalari + ma'lumotnoma (ko'chalar, zonalar, BHM)",
          "; ".join(f"{g.capitalize()}: {d['code'] or 'aniqlanmadi'}" for g, d in rec["coefficients"].items())
          + (f". Sizdan kerak: {', '.join(undecided)}" if undecided else ""), "mismatch" if undecided else "ok")

    if fee_data is not None:
        done = bool(fee_data.get("result"))
        _step(out, "Yig'imni hisoblash", "kod (Decimal), formula: 4-ilova hisoblash mexanizmi",
              fee_data["result"]["formula"] if done else "hali hisoblanmadi — yuqoridagi ochiq savollar hal bo'lishi kerak",
              "ok" if done else "warning")
    _step(out, "Tasdiqlash va hujjat", "xodim tasdig'i; hisob varaqasi va shartnoma ilovasi (Word)",
          "xodim tasdig'i kutilmoqda", "warning")
    return out
