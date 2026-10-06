"""PDF translation with PyMuPDF (verified on 1.28.2; AGPL-3.0).

layout mode
  1. classify pages: digital / scanned (no text, big image) / ocr_layer (only invisible
     text) / garbled (no usable ToUnicode) / empty
  2. digital pages: text units = paragraphs rebuilt from get_text("dict") lines
     (side-by-side lines split), redact ONLY the text (images and vector art stay),
     insert the translation with insert_htmlbox (shrinks to fit the original box)
  3. scanned / garbled / OCR-layer pages: the page is rendered (~2 MP) and read by
     the vision model; a page with the translated text is inserted after it
docx mode
  the same text (and vision-model pages) written as a Word document.

Hangul renders with the font built into PyMuPDF (Droid Sans Fallback, no bold).
If ``{data_dir}/fonts/NanumGothic.ttf`` (+ ``NanumGothicBold.ttf``) exists it is used.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from translator_app.documents import mdlite
from translator_app.documents.base import (
    MSG_BROKEN,
    MSG_ENCRYPTED,
    MSG_NO_TEXT,
    MSG_VISION_UNAVAILABLE,
    DocumentError,
    HandlerContext,
    HandlerResult,
    has_letters,
)
from translator_app.documents.docx_writer import DocxWriter
from translator_app.documents.ooxml import lang_tag

logger = logging.getLogger("translator.documents")

FLAG_ITALIC, FLAG_BOLD = 2, 16
VLM_KINDS = ("scanned", "garbled", "ocr_layer")
TARGET_PIXELS = 2_000_000              # rendered page size for the vision model
SCAN_UNIT_CHARS = 1500                 # progress weight of one vision-model page
MAX_STORY_PAGES = 40                   # safety cap for one inserted translation

MSG_NOT_PDF = "PDF 파일이 아니거나 손상된 파일입니다."


# ------------------------------------------------------------------ analysis
@dataclass
class Unit:
    lines: list = field(default_factory=list)       # line dicts of get_text("dict")

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.lines[0]["bbox"])
        for ln in self.lines[1:]:
            r |= pymupdf.Rect(ln["bbox"])
        return r

    @property
    def text(self) -> str:
        out = ""
        for ln in self.lines:
            s = "".join(sp["text"] for sp in ln["spans"]).strip()
            if out.endswith("-") and s[:1].islower():
                out = out[:-1] + s                  # de-hyphenate
            else:
                out = (out + " " + s) if out else s
        return out

    def style(self) -> dict:
        best, n = self.lines[0]["spans"][0], -1
        for ln in self.lines:
            for sp in ln["spans"]:
                if len(sp["text"].strip()) > n:
                    best, n = sp, len(sp["text"].strip())
        return best


def classify_page(page) -> str:
    text = page.get_text("text").strip()
    area = abs(page.rect) or 1.0
    if not text:
        img_area = sum(abs(pymupdf.Rect(b["bbox"]) & page.rect)
                       for b in page.get_text("dict")["blocks"] if b["type"] == 1)
        return "scanned" if img_area > 0.5 * area else "empty"
    try:
        types = {t["type"] for t in page.get_texttrace()}
    except Exception:  # noqa: BLE001 - very old/odd content streams
        types = set()
    if types and types <= {3}:                     # render mode 3 = invisible (OCR layer)
        return "ocr_layer"
    # fonts without a usable ToUnicode map extract as U+FFFD / private-use garbage
    bad = sum(1 for c in text if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    if bad > 0.2 * len(text):
        return "garbled"
    return "digital"


def page_units(page) -> list[Unit]:
    units: list[Unit] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        cur: Unit | None = None
        for ln in b["lines"]:
            if abs(ln["dir"][1]) > 1e-3 or not "".join(s["text"] for s in ln["spans"]).strip():
                continue                            # rotated / vertical text left as is
            if cur and cur.lines:
                prev = cur.lines[-1]
                h = prev["bbox"][3] - prev["bbox"][1]
                below = ln["bbox"][1] > prev["bbox"][3] - 0.5 * h
                gap = ln["bbox"][1] - prev["bbox"][3]
                if not below or gap > 0.8 * h:
                    units.append(cur)
                    cur = None
            if cur is None:
                cur = Unit()
            cur.lines.append(ln)
        if cur:
            units.append(cur)
    return [u for u in units if has_letters(u.text)]


def page_png(page, pixels: int = TARGET_PIXELS) -> bytes:
    r = page.rect
    zoom = math.sqrt(pixels / max(r.width * r.height, 1.0))
    zoom = min(max(zoom, 0.5), 4.0)
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")


def open_pdf(path: str | Path):
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - MuPDF raises several types
        raise DocumentError(MSG_NOT_PDF) from exc
    if doc.needs_pass:
        doc.close()
        raise DocumentError(MSG_ENCRYPTED)
    if doc.page_count == 0:
        doc.close()
        raise DocumentError(MSG_NO_TEXT)
    return doc


@dataclass
class PageInfo:
    kind: str
    texts: list[str] = field(default_factory=list)      # digital units / OCR-layer units


def analyze(path: str | Path) -> list[PageInfo]:
    doc = open_pdf(path)
    try:
        pages: list[PageInfo] = []
        for page in doc:
            kind = classify_page(page)
            if page.rotation and kind in ("digital", "ocr_layer"):
                page.remove_rotation()                  # same coordinates as in write_layout()
            texts = [u.text for u in page_units(page)] if kind in ("digital", "ocr_layer") else []
            pages.append(PageInfo(kind, texts))
        return pages
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(MSG_BROKEN) from exc
    finally:
        doc.close()


def render_page(path: str | Path, pno: int) -> bytes:
    doc = open_pdf(path)
    try:
        return page_png(doc[pno])
    finally:
        doc.close()


# ------------------------------------------------------------------ fonts
@dataclass
class PdfFont:
    family: str = "sans-serif"
    css_head: str = ""
    archive: object = None
    measure: object = None


def load_font(font_dir: Path | None) -> PdfFont:
    regular = font_dir / "NanumGothic.ttf" if font_dir else None
    if regular is not None and regular.is_file():
        try:
            archive = pymupdf.Archive(str(font_dir))
            css = "@font-face {font-family: tfont; src: url(NanumGothic.ttf);}\n"
            if (font_dir / "NanumGothicBold.ttf").is_file():
                css += "@font-face {font-family: tfont; font-weight: bold; src: url(NanumGothicBold.ttf);}\n"
            return PdfFont("tfont", css, archive, pymupdf.Font(fontfile=str(regular)))
        except Exception:  # noqa: BLE001 - broken font file -> stock fallback
            logger.warning("PDF 글꼴 파일을 읽지 못해 기본 글꼴을 씁니다")
    return PdfFont("sans-serif", "", None, pymupdf.Font("cjk"))


# ------------------------------------------------------------------ layout mode
def _css(sp: dict, family: str) -> str:
    c = sp.get("color", 0) or 0
    flags = sp.get("flags", 0) or 0
    return (f"font-size:{sp['size']:.1f}pt; color:#{c:06x}; line-height:1.15;"
            f"font-weight:{'bold' if flags & FLAG_BOLD else 'normal'};"
            f"font-style:{'italic' if flags & FLAG_ITALIC else 'normal'};"
            f"font-family:{family};")


def _fit_rect(page, u: Unit, out: str, size: float, measure) -> pymupdf.Rect:
    """Single-line units (labels, headings): widen to the measured translation width so
    the text is not shrunk; always add a little height for the CSS line-height."""
    r = pymupdf.Rect(u.bbox)
    r.y1 += 0.35 * size
    if len(u.lines) == 1:
        try:
            need = measure.text_length(out, fontsize=size) * 1.08 + 2
        except Exception:  # noqa: BLE001
            need = r.width
        if need > r.width:
            r.x1 = min(page.rect.x1 - 5, r.x0 + need)
    return r


def overlay_page(page, table: dict[str, str], font: PdfFont) -> tuple[int, int]:
    """Replace the text of one digital page. -> (units, overflowing units)."""
    if page.rotation:
        page.remove_rotation()
    units = [u for u in page_units(page) if u.text in table and table[u.text] != u.text]
    if not units:
        return 0, 0
    for u in units:
        for ln in u.lines:
            page.add_redact_annot(pymupdf.Rect(ln["bbox"]), fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    overflow = 0
    for u in units:
        out = table[u.text]
        sp = u.style()
        rect = _fit_rect(page, u, out, sp["size"], font.measure)
        body = html.escape(out).replace("\n", "<br/>")
        spare, _scale = page.insert_htmlbox(rect, f'<div style="{_css(sp, font.family)}">{body}</div>',
                                            css=font.css_head, scale_low=0, archive=font.archive)
        if spare < 0:
            overflow += 1
    return len(units), overflow


STORY_CSS = """
* {font-family: %(family)s;}
body {font-size: 10.5pt; line-height: 1.45; color: #111111;}
p {margin: 0 0 6pt 0;}
h1 {font-size: 16pt; margin: 0 0 8pt 0;}
h2 {font-size: 14pt; margin: 0 0 6pt 0;}
h3, h4 {font-size: 12pt; margin: 0 0 6pt 0;}
table {border-collapse: collapse; margin: 0 0 8pt 0;}
td, th {border: 0.5pt solid #999999; padding: 2pt 4pt; font-size: 9.5pt;}
pre {font-size: 9pt;}
.hdr {font-size: 8pt; color: #888888; margin: 0 0 12pt 0;}
"""


def story_pages(body_html: str, width: float, height: float, font: PdfFont):
    """Flow HTML over as many pages of the given size as needed -> pymupdf.Document."""
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    css = font.css_head + STORY_CSS % {"family": font.family}
    story = pymupdf.Story(html=body_html, user_css=css, archive=font.archive)
    mediabox = pymupdf.Rect(0, 0, width, height)
    margin = min(54.0, width * 0.08, height * 0.08)
    where = mediabox + (margin, margin, -margin, -margin)
    more, n = 1, 0
    while more and n < MAX_STORY_PAGES:
        dev = writer.begin_page(mediabox)
        more, _filled = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    return pymupdf.open("pdf", buf.getvalue())


def translation_page_html(pno: int, text: str) -> str:
    head = f'<p class="hdr">p.{pno + 1} 번역</p>'
    return head + mdlite.to_html(mdlite.parse(text))


def write_layout(src: Path, dst: Path, table: dict[str, str], added: dict[int, str], font: PdfFont,
                 digital: set[int] | None = None) -> dict:
    """digital: page numbers whose text is replaced in place (None = every page)."""
    doc = open_pdf(src)
    stats = {"units": 0, "overflow": 0, "added_pages": 0}
    try:
        for page in doc:
            if digital is not None and page.number not in digital:
                continue                                # scanned / OCR-layer pages stay untouched
            n, over = overlay_page(page, table, font)
            stats["units"] += n
            stats["overflow"] += over
        for pno in sorted(added, reverse=True):       # insert from the back: indices stay valid
            text = added[pno]
            if not text.strip():
                continue
            r = doc[pno].rect
            extra = story_pages(translation_page_html(pno, text), r.width, r.height, font)
            doc.insert_pdf(extra, start_at=pno + 1)
            stats["added_pages"] += extra.page_count
            extra.close()
        try:
            doc.subset_fonts()
        except Exception:  # noqa: BLE001 - subsetting is an optimisation only
            pass
        # every insert_htmlbox() call embeds its own font copy -> garbage=4 merges duplicates
        doc.save(str(dst), garbage=4, deflate=True)
    finally:
        doc.close()
    return stats


# ------------------------------------------------------------------ docx mode
def write_docx(src: Path, dst: Path, pages: list[PageInfo], table: dict[str, str], added: dict[int, str],
               lang: str | None) -> None:
    doc = open_pdf(src)
    w = DocxWriter(lang=lang)
    try:
        sizes = []
        page_units_list: list[list[Unit]] = []
        for page, info in zip(doc, pages):
            if page.rotation and info.kind == "digital":
                page.remove_rotation()
            units = page_units(page) if info.kind == "digital" else []
            page_units_list.append(units)
            sizes += [u.style()["size"] for u in units]
        body = statistics.median(sizes) if sizes else 11.0
        first = True
        for pno, units in enumerate(page_units_list):
            if not units and pno not in added:
                continue
            if not first:
                w.page_break()
            first = False
            for u in units:
                sp = u.style()
                out = table.get(u.text, u.text)
                bold = bool((sp.get("flags") or 0) & FLAG_BOLD)
                if sp["size"] >= body * 1.3 and len(out) < 200:
                    w.heading(out, 1 if sp["size"] >= body * 1.7 else 2)
                else:
                    w.paragraph(out, bold=bold)
            if pno in added and added[pno].strip():
                w.paragraph(f"p.{pno + 1} 번역", color="888888", size_pt=8)
                w.markdown(added[pno])
    finally:
        doc.close()
    dst.write_bytes(w.to_bytes())


# ------------------------------------------------------------------ handler
class PdfHandler:
    output_ext: str | None = None

    async def run(self, ctx: HandlerContext) -> HandlerResult:
        from translator_app.llm.client import VisionUnavailable

        ctx.set_status("extracting")
        pages = await asyncio.to_thread(analyze, ctx.input_path)
        ctx.check_cancel()
        vlm_pages = [i for i, p in enumerate(pages) if p.kind in VLM_KINDS]
        texts = list(dict.fromkeys(t for p in pages if p.kind == "digital" for t in p.texts))
        if not texts and not vlm_pages:
            raise DocumentError(MSG_NO_TEXT)
        if vlm_pages:
            ctx.add_units(len(vlm_pages), len(vlm_pages) * SCAN_UNIT_CHARS)
        table = await ctx.translate_strings(texts) if texts else {}

        added: dict[int, str] = {}
        vision_off = False
        for pno in vlm_pages:
            ctx.check_cancel()
            ctx.set_status("translating")
            if not vision_off:
                png = await asyncio.to_thread(render_page, ctx.input_path, pno)
                try:
                    added[pno] = (await ctx.describe_image(png, "image/png")).strip()
                except VisionUnavailable:
                    vision_off = True
            ctx.unit_done(1, SCAN_UNIT_CHARS)

        if vision_off:
            # OCR-layer pages can still be translated from their (invisible) text layer
            fallback = [i for i in vlm_pages if i not in added and pages[i].kind == "ocr_layer" and pages[i].texts]
            if fallback:
                ocr_texts = list(dict.fromkeys(t for i in fallback for t in pages[i].texts))
                ocr_table = await ctx.translate_strings(ocr_texts)
                for i in fallback:
                    added[i] = "\n\n".join(ocr_table.get(t, t) for t in pages[i].texts)
            missing = [i + 1 for i in vlm_pages if i not in added]
            if missing:
                ctx.warn(f"{MSG_VISION_UNAVAILABLE} 스캔 페이지({_page_list(missing)})는 번역하지 않았습니다.")

        if ctx.pdf_mode == "layout" and any(t.strip() for t in added.values()):
            ctx.warn(f"스캔 페이지({_page_list([i + 1 for i in sorted(added)])}) 뒤에 번역 페이지를 넣었습니다.")

        ctx.check_cancel()
        ctx.set_status("writing")
        if ctx.pdf_mode == "docx":
            out = ctx.output_with_ext(".docx")
            await asyncio.to_thread(write_docx, ctx.input_path, out, pages, table, added, lang_tag(ctx.target_lang))
            out_ext = ".docx"
        else:
            out = ctx.output_with_ext(".pdf")
            font = await asyncio.to_thread(load_font, ctx.font_dir)
            digital = {i for i, p in enumerate(pages) if p.kind == "digital"}
            stats = await asyncio.to_thread(write_layout, ctx.input_path, out, table, added, font, digital)
            if stats["overflow"]:
                logger.info("PDF 번역: 글자 크기를 줄여 넣은 구간 %d개", stats["overflow"])
            out_ext = ".pdf"

        pairs = [(s, table.get(s, s)) for s in texts]
        preview_parts: list[str] = []
        for pno, info in enumerate(pages):
            if info.kind == "digital":
                preview_parts += [table.get(t, t) for t in info.texts]
            elif pno in added and added[pno].strip():
                preview_parts.append(f"[p.{pno + 1}]\n{added[pno]}")
        preview = "\n".join(preview_parts)[:20_000]
        return HandlerResult(preview=preview, pairs=pairs, output_ext=out_ext)


def _page_list(nums: list[int], limit: int = 8) -> str:
    s = ", ".join(f"{n}쪽" for n in nums[:limit])
    return s + (f" 외 {len(nums) - limit}쪽" if len(nums) > limit else "")
