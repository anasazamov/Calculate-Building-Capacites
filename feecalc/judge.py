"""The judgment stage: small, exact questions to the models where a fixed rule would be brittle.

Measured (HISOBOT.md): the local models answer such questions correctly when (1) the rule and the
definitions are written into the question, (2) they may reason briefly before answering, and
(3) "cannot tell" is an allowed answer. They err when asked for a bare answer without the rule.

Three questions are asked, one item per request:
- is a row of the building list a building, or a structure listed with its capacity (pit, reservoir)?
- which category is a room that the dictionary does not know: parking, technical, common-use, or none?
- which storeys does a plan sheet show, when its title is not in a form the code recognises?

A judgment is only USED when every model that answered gives the same answer and none says "cannot
tell" (see `agreed`). Arithmetic is never asked of a model: the code multiplies and adds.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import llm

PARALLEL = 8
MAX_ITEMS = 120          # per question kind; a longer list is cut and the cut is reported
UNSURE = "aniq_emas"

STRUCTURE_SYSTEM = """Siz qurilish hajmini hisoblovchi mutaxassissiz. Obyekt binolari ro'yxatidan bitta qator beriladi.
Savol: bu qator qaysi turga kiradi?
- "bino" — odamlar foydalanadigan oddiy bino: uy, kottej, do'kon, ofis, ombor, hojatxona binosi, ayvon, yotoqxona;
- "texnik" — alohida turgan TEXNIK bino yoki inshoot, ya'ni faqat muhandislik jihozlari uchun: issiqlik punkti, qozonxona,
  transformator podstansiyasi, nasos stansiyasi, generator xonasi, ventilyatsiya kamerasi binosi;
- "inshoot" — SIG'IM inshooti, yozilgan m3 uning sig'imi: handak, septik, suv havzasi, rezervuar, quduq, o'ra, idish.
Qoidalar (yig'im formulasi): sig'im inshootining m3 qiymati bino hajmi emas va hisobga kirmaydi; texnik bino hajmi binoning umumiy
hajmiga kiradi va "texnik qavatlar, inshootlar va xonalar hajmi" (Ht) sifatida chegiriladi. Nomdan aniq bilib bo'lmasa — "aniq_emas".
Avval bir-ikki jumla mulohaza, keyin javob. Faqat JSON: {"reason": "<mulohaza>", "answer": "bino" | "texnik" | "inshoot" | "aniq_emas"}"""
STRUCTURE_ANSWERS = ("bino", "texnik", "inshoot", UNSURE)

ROOM_SYSTEM = """Siz qurilish hajmini hisoblovchi mutaxassissiz. Bino xonalari jadvalidan bitta nom beriladi (o'zbek yoki rus tilida).
Toifalar:
- Ha — avtoturargoh (parking, garaj, mashina joyi);
- Ht — texnik xona yoki texnik qavat (nasosxona, elektr shchit xonasi, ventilyatsiya kamerasi, issiqlik punkti, kirish tuguni, qozonxona, server xonasi);
- Hu — turar joy binosining umumiy foydalanishdagi qismi (zinapoya, lift xolli, umumiy koridor, vestibyul, kvartira oldi maydonchasi);
- boshqa — kvartira xonalari, ofis, savdo, ombor, yordamchi xona va yuqoridagilarga kirmaydigan hamma narsa.
Qoida: nomda texnik yoki umumiy foydalanish ekani ANIQ ko'rsatilmagan bo'lsa, u "boshqa". Umumlashgan nomlar ("Umumiy maydon", "Общая площадь",
"Yerto'la xonasi", "Подвальное помещение", "Yordamchi xona") — "boshqa". Nom tushunarsiz yoki buzilgan bo'lsa — "aniq_emas".
Avval bir-ikki jumla mulohaza, keyin javob. Faqat JSON: {"reason": "<mulohaza>", "answer": "Ha" | "Ht" | "Hu" | "boshqa" | "aniq_emas"}"""
ROOM_ANSWERS = ("Ha", "Ht", "Hu", "boshqa", UNSURE)

SHEET_SYSTEM = """Siz loyiha albomi varaqlarini ajratuvchi mutaxassissiz. Chizma varag'ining shtampidagi matn beriladi.
Savol: bu varaq qaysi qavat(lar)ning REJASI?
- "yertola" — yerto'la (podval, sokol) rejasi;
- "qavatlar" — yer usti qavat(lar)i rejasi: first va last ga birinchi va oxirgi qavat raqamini yozing (bitta qavat bo'lsa ikkalasi bir xil;
  "2-4 qavatlar", "2....4 go etaja", "tipovoy etaj 2-9" kabi yozuvlar oraliqni bildiradi);
- "reja_emas" — qavat rejasi emas yoki xonalarni takrorlaydigan varaq: pollar rejasi, tom, chordoq, poydevor, yopma, kesim, fasad, umumiy ma'lumotlar;
- "aniq_emas" — matndan bilib bo'lmaydi.
Avval bir jumla mulohaza, keyin javob. Faqat JSON:
{"reason": "<mulohaza>", "answer": "yertola" | "qavatlar" | "reja_emas" | "aniq_emas", "first": <son yoki 0>, "last": <son yoki 0>}"""
SHEET_ANSWERS = ("yertola", "qavatlar", "reja_emas", UNSURE)


CANDIDATE_SYSTEM = """Siz qurilish hajmini hisoblovchi mutaxassissiz. Loyihadagi binolar ro'yxatida NOMI bor, lekin maydoni va hajmi
yozilmagan bitta qator beriladi; uning yonida ro'yxatdagi boshqa binolar ham ko'rsatilgan.
Savol: bu qator nima?
- "yangi_bino" — yangi quriladigan ALOHIDA bino yoki inshoot (xonalari bor yoki hajmi bor), ro'yxatdagi boshqa binolarning takrori emas;
  uning hajmi yig'imga kirishi kerak, lekin hujjatda yo'q;
- "takror" — ro'yxatdagi binolardan birining boshqacha yozilgan nomi (masalan, chizmadagi yozuv);
- "bino_emas" — maydoncha, uskuna, transformator, to'siq, yo'l, ko'kalamzor, mashina turadigan joy va shunga o'xshash;
- "mavjud" — saqlanadigan yoki buziladigan mavjud bino (yangi qurilish emas);
- "aniq_emas" — bilib bo'lmaydi.
Avval bir-ikki jumla mulohaza, keyin javob. Faqat JSON:
{"reason": "<mulohaza>", "answer": "yangi_bino" | "takror" | "bino_emas" | "mavjud" | "aniq_emas"}"""
CANDIDATE_ANSWERS = ("yangi_bino", "takror", "bino_emas", "mavjud", UNSURE)


def candidate_question(name: str, listed: list[str]) -> str:
    return f"Qator: «{name}»\nRo'yxatdagi boshqa binolar: " + "; ".join(f"«{n}»" for n in listed[:12])


def _schema(answers: tuple[str, ...], floors: bool = False) -> dict:
    properties: dict = {"reason": {"type": "string", "maxLength": 400}, "answer": {"type": "string", "enum": list(answers)}}
    if floors:
        properties |= {"first": {"type": "integer", "minimum": 0, "maximum": 80}, "last": {"type": "integer", "minimum": 0, "maximum": 80}}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _ask_all(models: list[llm.Model], system: str, schema: dict, items: list[str], raw_dir: Path) -> dict[str, dict[str, dict]]:
    """item -> model key -> the model's JSON. One item per request, so one odd name cannot disturb the others."""
    jobs = [(item, m) for item in items for m in models]

    def one(job: tuple[str, llm.Model]) -> tuple[str, str, dict]:
        item, m = job
        return item, m.key, llm.ask(m, system, item, schema, 600, raw_dir).data

    out: dict[str, dict[str, dict]] = {item: {} for item in items}
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for item, key, data in pool.map(one, jobs):  # an error is raised here
            out[item][key] = data
    return out


def run(models: list[llm.Model], structures: list[str], rooms: list[str], sheets: list[str], raw_dir: Path,
        candidates: list[str] | None = None) -> dict:
    """Ask the kinds of questions. Returns {"structures", "rooms", "sheets", "candidates", "cut"}: item -> model -> answer.
    `candidates`: questions about buildings named in the project without any figure (see candidate_question)."""
    candidates = candidates or []
    cut = {kind: max(0, len(items) - MAX_ITEMS) for kind, items in (("structures", structures), ("rooms", rooms), ("sheets", sheets),
                                                                       ("candidates", candidates))}
    return {
        "candidates": _ask_all(models, CANDIDATE_SYSTEM, _schema(CANDIDATE_ANSWERS), candidates[:MAX_ITEMS], raw_dir),
        "structures": _ask_all(models, STRUCTURE_SYSTEM, _schema(STRUCTURE_ANSWERS), structures[:MAX_ITEMS], raw_dir),
        "rooms": _ask_all(models, ROOM_SYSTEM, _schema(ROOM_ANSWERS), rooms[:MAX_ITEMS], raw_dir),
        "sheets": _ask_all(models, SHEET_SYSTEM, _schema(SHEET_ANSWERS, floors=True), sheets[:MAX_ITEMS], raw_dir),
        "cut": {k: v for k, v in cut.items() if v},
    }


def agreed(answers: dict[str, dict] | None) -> dict | None:
    """The judgment to act on: the common answer of all models, unless any of them cannot tell. Otherwise None."""
    if not answers:
        return None
    given = list(answers.values())
    key = lambda a: (a["answer"], a.get("first", 0), a.get("last", 0))  # noqa: E731
    if any(a["answer"] == UNSURE for a in given) or len({key(a) for a in given}) != 1:
        return None
    return given[0]


def storeys(answer: dict | None) -> dict | None:
    """An agreed sheet judgment in the form plans.sheet_storeys gives; None when it is not a storey plan."""
    if not answer:
        return None
    if answer["answer"] == "yertola":
        return {"level": "basement", "floors": []}
    if answer["answer"] == "qavatlar" and 1 <= answer["first"] <= answer["last"] <= 80:
        return {"level": "floors", "floors": list(range(answer["first"], answer["last"] + 1))}
    return None
