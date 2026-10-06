"""Fixture builders and fake translators for the document-translation tests.

Every fixture is generated in code (python-docx / python-pptx / openpyxl are test
dependencies only).  The HWP 5.0 sample is the one binary fixture (tests/fixtures).
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from translator_app.llm.client import LLMUnavailable, TranslationCancelled, VisionUnavailable

FIXTURES = Path(__file__).parent / "fixtures"
TAG = re.compile(r"</?[gx]\d+/?>")
LETTER = re.compile(r"[^\W\d_]")

FAKE_IMAGE_MD = (
    "# 번역된 제목\n\n"
    "스캔한 문서의 **번역** 내용입니다. 압력은 30 bar 입니다.\n\n"
    "- 첫째 항목\n- 둘째 항목\n\n"
    "| 항목 | 값 |\n|---|---|\n| 압력 | 30 bar |\n"
)


# ====================================================================== fake translators
class FakeTranslator:
    """TranslatorService stand-in.

    mode: prefix (keeps tags) | reorder (moves the first <gN> group to the end) |
          drop_tags (removes all tags) | unavailable (LLMUnavailable) | noop (returns source)
    """

    def __init__(self, mode: str = "prefix", *, prefix: str = "[KO] ", vision: bool = True,
                 delay: float = 0.0, image_text: str = FAKE_IMAGE_MD) -> None:
        self.mode = mode
        self.prefix = prefix
        self.vision = vision
        self.delay = delay
        self.image_text = image_text
        self.calls: list[dict[str, Any]] = []
        self.image_calls: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None          # set by tests to hold translation

    def _one(self, t: str) -> str:
        if not LETTER.search(TAG.sub("", t)):
            return t
        if self.mode == "noop":
            return t
        if self.mode == "reorder":
            m = re.search(r"<(g\d+)>.*?</\1>", t)
            if m:
                t = (t[:m.start()] + t[m.end():]).rstrip() + " " + m.group(0)
            return self.prefix + t
        if self.mode == "drop_tags":
            return self.prefix + TAG.sub("", t)
        return self.prefix + t

    async def translate_batch(self, texts: list[str], opts: Any, *, priority: str = "document",
                              preceding: list[tuple[str, str]] | None = None, tags: bool = False,
                              cancel: asyncio.Event | None = None) -> list[str]:
        self.calls.append({"texts": list(texts), "priority": priority, "preceding": preceding, "tags": tags,
                           "target": getattr(opts, "target_lang", None)})
        if self.mode == "unavailable":
            raise LLMUnavailable()
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if cancel is not None and cancel.is_set():
            raise TranslationCancelled()
        return [self._one(t) for t in texts]

    async def describe_image(self, image_bytes: bytes, mime: str, opts: Any, *, mode: str = "translate",
                             priority: str = "document") -> str:
        self.image_calls.append({"size": len(image_bytes), "mime": mime, "mode": mode, "priority": priority})
        if self.mode == "unavailable":
            raise LLMUnavailable()
        if not self.vision:
            raise VisionUnavailable()
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.image_text

    async def resolve_glossary(self, opts: Any, text: str):
        entries = [e for e in (getattr(opts, "glossary_entries", None) or [])
                   if e.enabled and e.source.lower() in text.lower()]
        return entries, None


def settings(tmp_path: Path, **over: Any) -> SimpleNamespace:
    base = dict(data_dir=tmp_path / "data", doc_max_mb=50, doc_max_chars=600_000, doc_retention_hours=24,
                doc_job_concurrency=2, llm_doc_parallel=2, llm_vision="auto")
    base.update(over)
    return SimpleNamespace(**base)


async def run_job(manager, filename: str, data: bytes, timeout: float = 60.0, **options: Any):
    """Submit bytes and wait for a terminal status -> (job, output bytes | None)."""
    opts = {"source_lang": "en", "target_lang": "ko", **options}
    job = await manager.submit(data, filename, opts)
    await wait_done(job, timeout)
    out = manager.output_path(job)
    return job, (out.read_bytes() if out else None)


async def wait_done(job, timeout: float = 60.0, states=("done", "error", "canceled")) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while job.status not in states:
        if loop.time() > end:
            raise AssertionError(f"job did not finish: {job.status}")
        await asyncio.sleep(0.01)


def zip_text(data: bytes, member: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(member).decode("utf-8")


def zip_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.namelist()


# ====================================================================== DOCX
def make_docx() -> bytes:
    """Word file with bold/italic runs, a tab and line break, a hyperlink, a PAGE field,
    a table, header/footer, numbers and a formula-like literal."""
    import docx
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = "Hydrogen safety manual"
    d.sections[0].footer.paragraphs[0].text = "Internal use only"
    d.add_heading("Electrolyzer operation", level=1)
    p = d.add_paragraph("The ")
    p.add_run("electrolyzer").bold = True
    p.add_run(" produces hydrogen at ")
    p.add_run("30 bar").italic = True
    p.add_run(" and 1,250.5 kg per day.")
    p2 = d.add_paragraph("Before start\tcheck valves")
    p2.add_run().add_break()
    p2.add_run("Second line of the note.")
    # hyperlink
    p3 = d.add_paragraph("See the ")
    rid = d.part.relate_to("https://example.com/manual", docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK,
                           is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "online manual"
    r.append(t)
    link.append(r)
    p3._p.append(link)
    p3.add_run(" for details.")
    # PAGE field: begin / instr / separate / result / end
    p4 = d.add_paragraph("Page ")
    for kind, text in (("begin", None), ("instr", " PAGE "), ("separate", None), ("result", "1"), ("end", None)):
        run = OxmlElement("w:r")
        if kind == "instr":
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = text
            run.append(it)
        elif kind == "result":
            rt = OxmlElement("w:t")
            rt.text = text
            run.append(rt)
        else:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), kind)
            run.append(fc)
        p4._p.append(run)
    p4.add_run(" of the report")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Item"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Storage pressure"
    table.cell(1, 1).text = "700"
    d.add_paragraph("")
    d.add_paragraph("12345")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_minimal_docx(lines: list[str], sentence: bool = True) -> bytes:
    """Like translator_app.selftest: no styles.xml, no docProps."""
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    para = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    if sentence:
        para += ('<w:p><w:r><w:t xml:space="preserve">The </w:t></w:r><w:r><w:rPr><w:b/></w:rPr>'
                 '<w:t>electrolyzer</w:t></w:r><w:r><w:t xml:space="preserve"> splits water.</w:t></w:r></w:p>')
    return _zip({
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>'),
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{w}"><w:body>{para}'
            '<w:sectPr/></w:body></w:document>'),
    })


def _zip(members: dict[str, str | bytes], stored: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(name, text, compress_type=zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED)
    return buf.getvalue()


# ====================================================================== PPTX
def make_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Hydrogen roadmap 2030"
    tf = s.placeholders[1].text_frame
    tf.text = "Capacity will reach 12 GW"
    para = tf.add_paragraph()
    r1 = para.add_run()
    r1.text = "Safety "
    r1.font.bold = True
    r2 = para.add_run()
    r2.text = "comes first"
    para.add_line_break()
    r3 = para.add_run()
    r3.text = "always"
    rows = s.shapes.add_table(2, 2, Inches(1), Inches(4.5), Inches(6), Inches(1)).table
    rows.cell(0, 0).text = "Region"
    rows.cell(0, 1).text = "Share"
    rows.cell(1, 0).text = "Europe"
    rows.cell(1, 1).text = "35%"
    s.notes_slide.notes_text_frame.text = "Speaker note about storage"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ====================================================================== XLSX
def make_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Plants"
    ws.append(["Plant", "Status", "Capacity", "Check"])
    ws.append(["North plant", "Yes", 120, '=IF(B2="Yes","Running","Stopped")'])
    ws.append(["South plant", "No", 80, '=IF(B3="Yes","Running","Stopped")'])
    ws["A5"] = "Total capacity"
    ws["C5"] = "=SUM(C2:C3)"
    ws["A2"].comment = Comment("Commissioned in 2024", "tester")
    dv = DataValidation(type="list", formula1='"Yes,No"')
    ws.add_data_validation(dv)
    dv.add("B2:B3")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_minimal_xlsx() -> bytes:
    """Like translator_app.selftest: sharedStrings, no styles.xml, no calcPr."""
    strings = ["Item", "Quantity", "Hydrogen storage tank", "Pressure relief valve"]
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
    rows = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>2</v></c></row>'
            '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>4</v></c></row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    return _zip({
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{ct}.sheet.main+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{ct}.worksheet+xml"/>'
            f'<Override PartName="/xl/sharedStrings.xml" ContentType="{ct}.sharedStrings+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{ns}" xmlns:r="{rel}">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>'),
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{ns}">'
            f'<sheetData>{rows}</sheetData></worksheet>'),
        "xl/sharedStrings.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{ns}" count="{len(strings)}" uniqueCount="{len(strings)}">{sst}</sst>'),
    })


# ====================================================================== HWPX
HP_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"


def make_hwpx() -> bytes:
    """Minimal OWPML package: bold run (charPrIDRef 1), tab, line break, a table and
    linesegarray caches, Preview/PrvText.txt; mimetype first and STORED."""
    lineseg = ('<hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1000" textheight="1000" '
               'baseline="850" spacing="600" horzpos="0" horzsize="42520" flags="393216"/></hp:linesegarray>')
    cell = ('<hp:tc name="" header="0" hasMargin="0" protect="0" editable="0" dirty="0" borderFillIDRef="1">'
            '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" linkListIDRef="0" '
            'linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
            '<hp:p id="0" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
            '<hp:run charPrIDRef="0"><hp:t>{text}</hp:t></hp:run>' + lineseg + '</hp:p></hp:subList>'
            '<hp:cellAddr colAddr="{c}" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/>'
            '<hp:cellSz width="21260" height="1000"/><hp:cellMargin left="510" right="510" top="141" bottom="141"/>'
            '</hp:tc>')
    table = ('<hp:p id="0" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
             '<hp:run charPrIDRef="0"><hp:tbl id="1" zOrder="0" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" '
             'textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" pageBreak="CELL" repeatHeader="1" rowCnt="1" '
             'colCnt="2" cellSpacing="0" borderFillIDRef="1" noAdjust="0"><hp:tr>'
             + cell.format(text="Storage tank", c=0) + cell.format(text="Pressure gauge", c=1)
             + '</hp:tr></hp:tbl><hp:t/></hp:run>' + lineseg + '</hp:p>')
    section = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        f'<hs:sec xmlns:hp="{HP_NS}" xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section">'
        '<hp:p id="1" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        '<hp:run charPrIDRef="0"><hp:t>The </hp:t></hp:run>'
        '<hp:run charPrIDRef="1"><hp:t>electrolyzer</hp:t></hp:run>'
        '<hp:run charPrIDRef="0"><hp:t> produces hydrogen at 30 bar.</hp:t></hp:run>' + lineseg + '</hp:p>'
        '<hp:p id="2" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        '<hp:run charPrIDRef="0"><hp:t>Check valves<hp:tab width="4000" leader="0" type="1"/>daily'
        '<hp:lineBreak/>Report leaks at once.</hp:t></hp:run>' + lineseg + '</hp:p>'
        '<hp:p id="3" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        '<hp:run charPrIDRef="0"><hp:t>2024</hp:t></hp:run>' + lineseg + '</hp:p>'
        + table + '</hs:sec>')
    header = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" version="1.4" secCnt="1">'
        '<hh:refList><hh:charProperties itemCnt="2"><hh:charPr id="0" height="1000"/>'
        '<hh:charPr id="1" height="1000"><hh:bold/></hh:charPr></hh:charProperties></hh:refList></hh:head>')
    members: dict[str, str | bytes] = {
        "mimetype": "application/hwp+zip",
        "version.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
                        '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication='
                        '"WORDPROCESSOR" major="5" minor="1" micro="0" buildNumber="1" os="1" xmlVersion="1.4" '
                        'application="Test" appVersion="1"/>'),
        "Contents/header.xml": header,
        "Contents/section0.xml": section,
        "Preview/PrvText.txt": "The electrolyzer produces hydrogen".encode("utf-8"),
        "META-INF/manifest.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
                                  '<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>'),
    }
    return _zip(members, stored=("mimetype", "version.xml"))


# ====================================================================== PDF / images
def make_png(text: str = "Pressure 30 bar", size: tuple[int, int] = (480, 200)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (250, 250, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([5, 5, size[0] - 6, size[1] - 6], outline=(40, 40, 40), width=3)
    d.text((30, size[1] // 2 - 10), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_pdf(scanned: bool = True, ocr_layer: bool = False) -> bytes:
    """Page 1: digital text (title, paragraph with numbers, coloured caption, vector
    bars, an image). Page 2 (optional): scanned (raster only). Page 3 (optional):
    raster + invisible OCR text layer."""
    import pymupdf

    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.draw_rect(pymupdf.Rect(40, 40, 555, 90), color=None, fill=(0.85, 0.92, 1.0))
    p.insert_text((50, 72), "Clean Hydrogen Market Outlook", fontsize=20, fontname="hebo", color=(0, 0.2, 0.6))
    body = ("Global electrolyzer capacity reached 12 GW in 2025. Most new projects are located in "
            "regions with low-cost renewable electricity.")
    p.insert_textbox(pymupdf.Rect(50, 110, 545, 190), body, fontsize=11, fontname="helv")
    p.insert_text((50, 215), "Figure 1. Capacity by region", fontsize=9, fontname="heit", color=(0.4, 0.4, 0.4))
    for i, h in enumerate([60, 110, 40]):
        x = 70 + i * 60
        p.draw_rect(pymupdf.Rect(x, 330 - h, x + 35, 330), color=(0, 0, 0), fill=(0.2, 0.5, 0.8))
    p.insert_image(pymupdf.Rect(350, 230, 545, 330), stream=make_png("IMAGE TEXT", (300, 150)))
    if scanned or ocr_layer:
        pix = doc[0].get_pixmap(dpi=60)
    if scanned:
        p2 = doc.new_page(width=595, height=842)
        p2.insert_image(p2.rect, pixmap=pix)
    if ocr_layer:
        p3 = doc.new_page(width=595, height=842)
        p3.insert_image(p3.rect, pixmap=pix)
        p3.insert_text((50, 72), "Clean Hydrogen Market Outlook", fontsize=20, render_mode=3)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


def pdf_text(data: bytes) -> list[str]:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [page.get_text() for page in doc]


# ====================================================================== simple formats
HTML_DOC = (b"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Safety page</title>"
            b"<meta name=\"description\" content=\"Hydrogen safety rules\"></head><body>"
            b"<h1>Hydrogen safety</h1><p>Keep the <b>valve</b> closed. <img src=\"a.png\" alt=\"Valve photo\"> "
            b"Call <a href=\"tel:119\">emergency</a> at once.</p>"
            b"<pre>do_not_translate()</pre><p class=\"notranslate\">KEEI</p></body></html>")
MD_DOC = ("---\ntitle: test\n---\n# Safety rules\n\n- Close the `valve_1` before work\n"
          "- Read the [manual](https://example.com/m.pdf)\n\n```\ncode block\n```\n\n"
          "| Item | Value |\n|---|---|\n| Pressure | 30 bar |\n")
SRT_DOC = "1\r\n00:00:01,000 --> 00:00:03,000\r\nHello <i>world</i>\r\n\r\n2\r\n00:00:04,000 --> 00:00:05,000\r\nSecond line\r\n"
VTT_DOC = "WEBVTT\n\nNOTE comment stays\n\n00:00.000 --> 00:02.000 align:start\nWelcome to the plant\n"
TXT_DOC = "Safety notes\r\n\r\n  Indented line about hydrogen\r\n12345\r\nLast line"
