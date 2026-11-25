"""Offline tests of the normative knowledge base. The fixtures under tests/data/norms are synthetic
pages in the layout of lex.uz; no test touches the network."""
from pathlib import Path

import httpx
import pymupdf
import pytest

from feecalc import knowledge
from feecalc.knowledge import Clause, Document, KnowledgeBase, Source

DATA = Path(__file__).parent / "data" / "norms"


def lex(name: str, **kw) -> list[Clause]:
    return knowledge.parse_lex((DATA / name).read_text(encoding="utf-8"), **kw)


def by_ref(clauses: list[Clause]) -> dict[str, Clause]:
    assert len({c.ref for c in clauses}) == len(clauses), "references must be unique"
    return {c.ref: c for c in clauses}


# ---------- parsing ----------

def test_act_body_points_and_preamble():
    c = by_ref(lex("lex_qaror.html"))
    assert c["muqaddima"].text == "Hududlar infratuzilmasini rivojlantirish maqsadida organ qaror qiladi:"
    assert c["1-band"].text.startswith("1. Yigʻim tartibi")
    assert c["2-band"].path == ()
    assert not any("Havola olish" in x.text or "eslatma" in x.text or "NAMUNAYEV" in x.text or "OKOZ" in x.text
                   for x in c.values()), "menus, classifier indexes and the signature block are not text"


def test_annex_points_keep_heading_path_and_paragraphs():
    c = by_ref(lex("lex_qaror.html"))
    point = c["1-ilova, 2-band"]
    assert point.path == ("1-ilova. Yigʻimni hisoblash va undirish tartibi toʻgʻrisida NIZOM", "2-bob. Yigʻimni hisoblash")
    assert point.text.split("\n") == [
        "2. Yigʻim quyidagi miqdorda undiriladi:",
        "jami qurilish hajmining har bir kub metri uchun bazaviy hisoblash miqdorining 1 baravari miqdorida;",
        "2,5 m dan baland qavatlar uchun qoʻshimcha hajm hisobga olinadi."]
    assert c["1-ilova, 1-band"].path[-1] == "1-bob. Umumiy qoidalar"
    assert "1-ilova, 3-band" in c


def test_superscript_point_and_amendment_note():
    c = by_ref(lex("lex_qaror.html"))
    inserted = c["1-ilova, 2¹-band"]
    assert inserted.text == "2¹. Binoning maydoni 100 m² dan kam boʻlsa, yigʻim undirilmaydi."
    assert "5-son qarorga asosan kiritilgan" in inserted.note
    assert "tahrirga qarang" not in inserted.text + inserted.note


def test_nested_annex_table_rows_and_group_rows():
    c = by_ref(lex("lex_qaror.html"))
    assert c["1-ilovaga 2-ilova, 1-band"].text == "1. Toʻlov miqdori quyidagi formula bilan aniqlanadi:\nT=B*H*Kt"
    assert c["1-ilovaga 2-ilova, 1-band"].path[1] == "2-ilova. Toʻlov miqdorini hisoblashning NAMUNAVIY MEXANIZMI"
    row = c["1-ilovaga 2-ilova, 2-band, jadval, 3-qator"]
    assert row.text == "2. | Renovatsiya | 0,5"
    assert row.path[-1] == "jadval: I. | Qurilish turi (Kt) | Koeffitsiyent"
    location = c["1-ilovaga 2-ilova, 2-band, jadval, 5-qator"]
    assert location.text == "1. | Markaziy qatnov koʻchalaridan boshqa hududlar | 0,6"
    assert location.path[-1] == "jadval: II. | Obyekt joylashuvi (Kj)", "a bold row is the group of the rows under it"


def test_notes_keep_their_lead_in():
    c = by_ref(lex("lex_qaror.html"))
    assert c["1-ilovaga 2-ilova, 2-band, izoh, 1-xatboshi"].text == "Izoh: Quyidagi holatlarda yigʻim undirilmaydi:"
    for k in (2, 3):
        assert c[f"1-ilovaga 2-ilova, 2-band, izoh, {k}-xatboshi"].path[-1] == "Izoh: Quyidagi holatlarda yigʻim undirilmaydi:"
    assert "undirilmaydi" not in " ".join(c["1-ilovaga 2-ilova, 2-band, izoh, 4-xatboshi"].path)


def test_labelled_table_and_row_span():
    c = by_ref(lex("lex_qaror.html"))
    assert c["1-ilovaga 2-ilova, 1-jadval, 1-qator"].text == "Bino turi | Qavat | Balandlik, m"
    assert c["1-ilovaga 2-ilova, 1-jadval, 3-qator"].text == "Turar joy | boshqa | 2,7", "a spanning cell is repeated"
    assert c["1-ilovaga 2-ilova, 1-jadval, 3-qator"].path[-1] == "1-jadval: Bino turi | Qavat | Balandlik, m"


def test_code_inside_an_approving_order():
    c = by_ref(lex("lex_qaror.html", body_annex="1"))
    assert "buyruq, 1-band" in c and "buyruq, muqaddima" in c
    assert "2-band" in c and "2¹-band" in c, "the annex that is the code itself needs no prefix"
    assert "2-ilova, 2-band, jadval, 3-qator" in c and "2-ilova, 1-jadval, 2-qator" in c
    assert c["2-band"].path == ("2-bob. Yigʻimni hisoblash",)


def test_old_style_dotted_numbering():
    c = by_ref(lex("lex_qmq_cyrillic.html"))
    assert list(c) == ["1.1-band", "1.2-band", "2-bo'lim, 1-xatboshi", "2-bo'lim, 2-xatboshi", "3.1-band", "3.12*-band"]
    assert c["1.1-band"].path == ("1. ҚЎЛЛАНИШ СОҲАСИ",)
    assert c["2-bo'lim, 2-xatboshi"].path[-1].startswith("Ушбу меъёрларда"), "the list item keeps its lead-in"
    assert c["3.1-band"].text.count("\n") == 2, "'2.5 м ...' and 'Примечания: 1. ...' do not open points"


def test_long_point_is_split_into_paragraphs(monkeypatch):
    monkeypatch.setattr(knowledge, "SPLIT_CHARS", 60)
    c = by_ref(lex("lex_qaror.html"))
    assert "1-ilova, 2-band" not in c
    assert c["1-ilova, 2-band, 1-xatboshi"].text == "2. Yigʻim quyidagi miqdorda undiriladi:"
    assert c["1-ilova, 2-band, 2-xatboshi"].path[-1] == "2. Yigʻim quyidagi miqdorda undiriladi:"
    assert c["1-ilova, 2-band, 3-xatboshi"].text.startswith("2,5 m dan baland")


def test_page_without_text_in_this_language_is_an_error():
    with pytest.raises(ValueError, match="no text of the act"):
        lex("lex_stub.html")


def test_pdf_points_annex_and_table(tmp_path: Path):
    css = "* {font-family: sans-serif; font-size: 10pt;} td {border: 1px solid black; padding: 3px;} p {margin: 6px 0;}"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_htmlbox(pymupdf.Rect(40, 40, 555, 400), """
<p>Namuna kengashining qarori</p>
<p style="text-indent: 30px">1. Koeffitsiyentlar ilovaga muvofiq tasdiqlansin va ular har bir mahalla uchun alohida qoʻllansin, bunda hududning joylashuvi hisobga olinadi.</p>
<p style="text-indent: 30px">2. Ushbu qaror eʼlon qilingan kundan kuchga kiradi.</p>""", css=css)
    page = doc.new_page(width=595, height=842)
    page.insert_htmlbox(pymupdf.Rect(40, 40, 555, 400), """
<p style="text-align: right">Namuna kengashining 1-son qaroriga</p>
<p style="text-align: right">1-ilova</p>
<p style="text-align: center">Mahallalar boʻyicha qiymatlar</p>
<table><tr><td>T/r</td><td>MFY nomi</td><td>Qiymat</td></tr>
<tr><td>1</td><td>Registon MFY</td><td>1,4</td></tr><tr><td>2</td><td>Bogʻishamol MFY</td><td>1,2</td></tr></table>""", css=css)
    path = tmp_path / "decision.pdf"
    doc.save(path)
    doc.close()
    c = by_ref(knowledge.parse(Source("d", "t", "kengash", "u", "uz-Latn", format="pdf"), path))
    assert knowledge.fold(c["1-band"].text).startswith("1. koeffitsiyentlar ilovaga muvofiq"), "ligatures fold away"
    assert c["1-band"].text.endswith("hisobga olinadi.")
    assert c["2-band"].text == "2. Ushbu qaror eʼlon qilingan kundan kuchga kiradi."
    row = c["1-ilova, jadval, 2-qator"]
    assert row.text == "1 | Registon MFY | 1,4"
    assert row.path == ("1-ilova. Mahallalar boʻyicha qiymatlar", "jadval: T/r | MFY nomi | Qiymat")


# ---------- fetch ----------

def test_fetch_downloads_once_and_records_the_file(tmp_path: Path, monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **kw) -> httpx.Response:
        calls.append((url, kw["headers"]))
        return httpx.Response(200, content=b"<html>raw</html>", headers={"content-type": "text/html"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(knowledge.httpx, "get", fake_get)
    monkeypatch.setattr(knowledge, "MIN_INTERVAL", 0.0)
    source = Source("doc-1", "Title", "qaror", "https://example.invalid/docs/1", "uz-Latn")
    path = knowledge.fetch(source, tmp_path)
    assert path.read_bytes() == b"<html>raw</html>" and path.name == "doc-1.html"
    record = knowledge.fetch_record(source, tmp_path)
    assert record["url"] == source.url and len(record["sha256"]) == 64 and record["fetched_at"] and record["bytes"] == 16
    assert knowledge.fetch(source, tmp_path) == path and len(calls) == 1, "the cached file is reused"
    assert calls[0][1] == {"User-Agent": knowledge.USER_AGENT}
    knowledge.fetch(source, tmp_path, refresh=True)
    moved = Source("doc-1", "Title", "qaror", "https://example.invalid/docs/2", "uz-Latn")
    knowledge.fetch(moved, tmp_path)
    assert [u for u, _ in calls] == [source.url, source.url, moved.url], "a changed address is downloaded again"


def test_failed_download_raises_and_writes_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(knowledge.httpx, "get",
                        lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)))
    monkeypatch.setattr(knowledge, "MIN_INTERVAL", 0.0)
    with pytest.raises(httpx.HTTPStatusError):
        knowledge.fetch(Source("gone", "t", "qaror", "https://example.invalid/x", "ru"), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_registry_is_well_formed():
    sources = knowledge.load_sources()
    assert sources[0].id == "vm-151" and sources[0].kind == "qaror"
    assert {s.kind for s in sources} >= {"qaror", "shnq", "qmq"}
    assert all(s.url.startswith("https://") and s.format in ("lex", "pdf") and s.title for s in sources)


# ---------- knowledge base ----------

def meta(doc_id: str, kind: str, title: str = "Title") -> Document:
    return Document(doc_id, title, kind, f"https://example.invalid/{doc_id}", "uz-Latn", doc_id.upper())


@pytest.fixture()
def kb(tmp_path: Path) -> KnowledgeBase:
    base = KnowledgeBase(tmp_path / "norms" / "knowledge.db")
    base.add_document(meta("qaror-1", "qaror", "Namunaviy yigʻim tartibi"), lex("lex_qaror.html"))
    base.add_document(meta("qmq-1", "qmq", "Намунавий бинолар"), lex("lex_qmq_cyrillic.html"))
    return base


def test_documents_and_replacing(kb: KnowledgeBase):
    first, second = kb.documents()
    assert (first.doc_id, first.kind, second.doc_id) == ("qaror-1", "qaror", "qmq-1")
    assert first.clauses == len(lex("lex_qaror.html")) and first.characters > 500
    kb.add_document(meta("qaror-1", "qaror", "New title"), [Clause("1-band", ("Bob",), "1. Yangi matn hajmi haqida.")])
    first, second = kb.documents()
    assert (first.doc_id, first.title, first.clauses, second.doc_id) == ("qaror-1", "New title", 1, "qmq-1")
    assert kb.get("qaror-1", "1-ilova, 1-band") is None
    assert [h.ref for h in kb.search("renovatsiya")] == [], "clauses of the older copy are gone from the index"
    assert [(h.doc_id, h.ref) for h in kb.search("yangi matn")] == [("qaror-1", "1-band")]


def test_get_exact_and_tolerant_reference(kb: KnowledgeBase):
    clause = kb.get("qaror-1", "1-ilova, 3-band")
    assert clause == Clause("1-ilova, 3-band", clause.path, "3. Toʻlov 20 foiz oldindan toʻlanadi.")
    assert kb.get("qaror-1", "1-Илова 3-банд") == clause, "script, case and commas do not matter"
    assert kb.get("qaror-1", "1-ilova, 2¹-band").text.startswith("2¹.")
    assert kb.get("qmq-1", "3.12*-band").text.startswith("3.12*.")
    assert kb.get("qaror-1", "9-band") is None and kb.get("nothing", "1-band") is None


def test_get_parent_reference_joins_its_clauses(kb: KnowledgeBase):
    point = kb.get("qaror-1", "1-ilovaga 2-ilova, 2-band")
    lines = point.text.split("\n")
    assert lines[0].startswith("2. Koeffitsiyentlar") and "2. | Renovatsiya | 0,5" in lines
    assert lines[-1] == "Mexanizm har chorakda qayta koʻrib chiqilishi mumkin."
    table = kb.get("qaror-1", "1-ilovaga 2-ilova, 1-jadval")
    assert table.text.split("\n")[0] == "Bino turi | Qavat | Balandlik, m" and len(table.text.split("\n")) == 3
    assert "2¹" not in kb.get("qaror-1", "1-ilova, 2-band").text, "2¹-band is not a part of 2-band"


def test_search_in_both_scripts(kb: KnowledgeBase):
    latin = kb.search("qurilish hajmi")
    assert {h.doc_id for h in latin} == {"qaror-1", "qmq-1"}, "a Latin query finds Cyrillic Uzbek text"
    cyrillic = kb.search("қурилиш ҳажми")
    assert [(h.doc_id, h.ref) for h in cyrillic] == [(h.doc_id, h.ref) for h in latin]
    assert kb.search("марказий қатнов кўчалари")[0].ref == "1-ilovaga 2-ilova, 2-band, jadval, 5-qator"
    assert kb.search("yer uchastkasi obyekt")[0].ref == "1.2-band", "ер -> yer, объект -> obyekt"
    hit = kb.search("строительный объём")[0]
    assert (hit.doc_id, hit.ref, hit.title, hit.url) == ("qmq-1", "3.12*-band", "Намунавий бинолар", "https://example.invalid/qmq-1")
    assert hit.text.startswith("3.12*. Строительный объём") and hit.score > 0 and hit.kind == "qmq"


def test_search_apostrophes_and_case(kb: KnowledgeBase):
    expected = "1-ilovaga 2-ilova, 2-band, jadval, 5-qator"
    for query in ("koʻchalaridan", "ko‘chalaridan", "ko’chalaridan", "ko`chalaridan", "ko'chalaridan", "KO'CHALARIDAN"):
        assert kb.search(query)[0].ref == expected, query
    assert knowledge.fold("Toʻgʻrisida") == knowledge.fold("ТЎҒРИСИДА") == "togrisida"


def test_search_prefix_and_inflection(kb: KnowledgeBase):
    assert kb.search("renovats")[0].text == "2. | Renovatsiya | 0,5", "a word prefix matches"
    assert kb.search("hajmlarining")[0].ref in ("1-ilova, 2-band", "1.1-band"), "another ending of the same word"
    assert kb.search("объёмов здания")[0].ref == "3.12*-band"
    assert kb.search("koeffitsiyentlarni")[0].text.startswith("2. Koeffitsiyentlar")
    best = kb.search("qavatlar soni")
    assert best[0].ref == "1.1-band" and best[0].score > best[-1].score, "the exact phrase ranks first"


def test_search_kinds_filter_and_limits(kb: KnowledgeBase):
    assert {h.doc_id for h in kb.search("hajmi", kinds={"qmq"})} == {"qmq-1"}
    assert {h.doc_id for h in kb.search("hajmi", kinds={"qaror"})} == {"qaror-1"}
    assert {h.doc_id for h in kb.search("hajmi", kinds={"qaror", "qmq"})} == {"qaror-1", "qmq-1"}
    assert kb.search("hajmi", kinds={"shnq"}) == [] and kb.search("hajmi", kinds=set()) == []
    assert len(kb.search("yigʻim", k=2)) == 2
    assert kb.search("") == [] and kb.search("va ... ; \"") == [] and kb.search("zzzzqqq") == []
    assert kb.search('hajmi" OR (NEAR* ^')[0].doc_id in ("qaror-1", "qmq-1"), "query syntax is never passed through"


def test_search_finds_a_clause_by_its_heading(kb: KnowledgeBase):
    hits = kb.search("umumiy qoidalar")
    assert hits[0].ref == "1-ilova, 1-band" and hits[0].path[-1] == "1-bob. Umumiy qoidalar"
