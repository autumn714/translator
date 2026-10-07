"""PDF translation with PyMuPDF (verified on 1.28.2; AGPL-3.0).

layout mode
  1. classify pages: digital / scanned (big image, at most a stamp or page number as
     text) / ocr_layer (scan + invisible text) / garbled (no usable ToUnicode) / empty
  2. digital pages: text units = paragraphs rebuilt from the visible text lines (a line
     joins the paragraph above only when it sits right below it, overlaps it
     horizontally and has a similar size, so table cells stay apart).  Only the glyphs
     of translated units are removed: every redaction rectangle is a thin band through
     the middle of its own glyphs and never touches a visible glyph that stays, because
     MuPDF drops any glyph whose box (shrunk by about 10 %) meets a redaction rectangle.
     A glyph that physically overlaps text that stays (stamp, watermark) is covered with
     a white box of its own size instead of being removed.
  3. the translations of all pages are laid out (HTML story, shrunk to fit the
     original box) on ONE separate overlay document and stamped onto the pages, so
     the font is embedded once per document instead of once per text box
  4. scanned / garbled / OCR-layer pages: the page is rendered (~2 MP) and read by
     the vision model; a page with the translated text is inserted after it.  Without
     vision, an OCR layer is translated onto that page and a scan's own visible text
     (title, stamp) is translated in place
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
from collections import Counter
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

# a page whose images cover more than half of it and whose visible text is no more than
# a stamp / page number / header is a scan: its content is in the image.  Real text over a
# full-page picture (a slide title and subtitle) keeps the page digital.
SCAN_IMAGE_SHARE = 0.5
SCAN_MAX_VISIBLE_CHARS = 200
SCAN_MAX_TEXT_SHARE = 0.05
SCAN_MAX_LETTERS = 20                  # fewer letters than this is a page number / stamp ...
SCAN_SHORT_TOKEN = 3                   # ... and so are only digits / tokens this short

# text extraction without image data (images are only measured, never needed here)
TEXT_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES

# redaction geometry (fractions of a glyph box).  MuPDF removes a glyph when a
# redaction rectangle meets its box shrunk by ~10 % on every side; the rectangles
# used here stay inside the middle band of their own glyphs, and glyphs that must
# stay are protected with their box shrunk by only 5 % (a safety margin).
BAND_TOP, BAND_BOTTOM = 0.3, 0.7
BAND_INSET_X = 0.3
PROTECT_INSET = 0.05
BUCKET = 16.0                          # pt; vertical buckets for the collision test

MSG_NOT_PDF = "PDF 파일이 아니거나 손상된 파일입니다."


# ------------------------------------------------------------------ analysis
@dataclass
class Unit:
    lines: list = field(default_factory=list)       # line dicts (rawdict; spans carry "text")

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

    def spans(self):
        for ln in self.lines:
            yield from ln["spans"]


def text_blocks(page) -> list[dict]:
    """Text blocks of get_text("rawdict") with a "text" field added to every span."""
    blocks = [b for b in page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"] if b.get("type") == 0]
    for b in blocks:
        for ln in b["lines"]:
            for sp in ln["spans"]:
                sp["text"] = "".join(c["c"] for c in sp["chars"])
    return blocks


def _visible(sp: dict) -> bool:
    return sp.get("alpha", 255) != 0                # alpha 0 = render mode 3/7 (OCR layers)


def _nonspace(s: str) -> int:
    return sum(1 for c in s if not c.isspace())


def _negligible(texts: list[str]) -> bool:
    """Page numbers, stamps, codes: nothing worth translating in place of the scan."""
    letters = [sum(1 for c in token if c.isalpha()) for token in " ".join(texts).split()]
    return sum(letters) < SCAN_MAX_LETTERS or max(letters, default=0) <= SCAN_SHORT_TOKEN


def _image_share(page) -> float:
    area = abs(page.rect) or 1.0
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001 - odd image dictionaries
        return 0.0
    return sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in infos) / area


def classify_page(page, blocks: list[dict] | None = None) -> str:
    blocks = text_blocks(page) if blocks is None else blocks
    area = abs(page.rect) or 1.0
    visible: list[str] = []
    vis_chars = hidden_chars = 0
    vis_area = 0.0
    for b in blocks:
        for ln in b["lines"]:
            for sp in ln["spans"]:
                n = _nonspace(sp["text"])
                if not n:
                    continue
                if _visible(sp):
                    vis_chars += n
                    vis_area += abs(pymupdf.Rect(sp["bbox"]) & page.rect)
                    visible.append(sp["text"])
                else:
                    hidden_chars += n
    if not vis_chars and not hidden_chars:
        return "scanned" if _image_share(page) > SCAN_IMAGE_SHARE else "empty"
    if not vis_chars:
        return "ocr_layer"
    if (vis_chars < SCAN_MAX_VISIBLE_CHARS and vis_area < SCAN_MAX_TEXT_SHARE * area
            and _image_share(page) > SCAN_IMAGE_SHARE):
        # a scan with a digital stamp / page number on top: the body is in the image
        if hidden_chars > vis_chars:
            return "ocr_layer"
        if _negligible(visible):
            return "ocr_layer" if hidden_chars else "scanned"
    # fonts without a usable ToUnicode map extract as U+FFFD / private-use garbage
    text = "".join(visible)
    bad = sum(1 for c in text if c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF)
    if bad > 0.2 * _nonspace(text):
        return "garbled"
    return "digital"


def _line_size(ln: dict) -> float:
    sizes = [sp["size"] for sp in ln["spans"] if sp["text"].strip()]
    return max(sizes) if sizes else 0.0


def _continues(prev: dict, ln: dict) -> bool:
    """Does line ln continue the paragraph whose last line is prev?"""
    p, q = prev["bbox"], ln["bbox"]
    h = p[3] - p[1]
    if q[1] <= p[3] - 0.5 * h or q[1] - p[3] > 0.8 * h:
        return False                                # side by side, or a paragraph gap
    size_p, size_q = _line_size(prev), _line_size(ln)
    if abs(size_p - size_q) > 0.2 * max(size_p, size_q, 1.0):
        return False                                # heading -> body text and the like
    overlap = min(p[2], q[2]) - max(p[0], q[0])
    narrow = min(p[2] - p[0], q[2] - q[0])
    same_left = abs(q[0] - p[0]) <= 0.6 * max(size_p, size_q, 1.0)
    return overlap > 0.5 * narrow or same_left      # otherwise another column / table cell


def page_units(page, *, invisible: bool = False, blocks: list[dict] | None = None) -> list[Unit]:
    """Paragraph units of a page.  invisible=False drops OCR-layer text (alpha 0), which
    must never be redacted or drawn over on a digital page."""
    units: list[Unit] = []
    for b in text_blocks(page) if blocks is None else blocks:
        cur: Unit | None = None
        for ln in b["lines"]:
            if abs(ln["dir"][1]) > 1e-3:
                continue                            # rotated / vertical text left as is
            spans = [sp for sp in ln["spans"] if invisible or _visible(sp)]
            if not "".join(sp["text"] for sp in spans).strip():
                continue
            if len(spans) != len(ln["spans"]):
                r = pymupdf.Rect(spans[0]["bbox"])
                for sp in spans[1:]:
                    r |= pymupdf.Rect(sp["bbox"])
                ln = {"bbox": tuple(r), "dir": ln["dir"], "spans": spans}
            if cur is not None and not _continues(cur.lines[-1], ln):
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
            blocks = text_blocks(page)
            kind = classify_page(page, blocks)
            texts: list[str] = []
            if kind in ("digital", "ocr_layer", "scanned"):
                if page.rotation:
                    page.remove_rotation()              # same coordinates as in write_layout()
                    blocks = text_blocks(page)
                # scanned: its visible text (translated in place when there is no vision)
                texts = [u.text for u in page_units(page, invisible=kind == "ocr_layer", blocks=blocks)]
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


# ------------------------------------------------------------------ redaction plan
def _shrink(bbox, fx: float, fy: float) -> pymupdf.Rect:
    x0, y0, x1, y1 = bbox
    dx, dy = (x1 - x0) * fx, (y1 - y0) * fy
    return pymupdf.Rect(x0 + dx, y0 + dy, x1 - dx, y1 - dy)


class _Boxes:
    """Glyph boxes that must survive, bucketed by height for quick collision tests."""

    def __init__(self) -> None:
        self.rows: dict[int, list[pymupdf.Rect]] = {}

    def add(self, r: pymupdf.Rect) -> None:
        if r.is_empty:
            return
        for k in range(int(r.y0 // BUCKET), int(r.y1 // BUCKET) + 1):
            self.rows.setdefault(k, []).append(r)

    def hits(self, r: pymupdf.Rect) -> list[pymupdf.Rect]:
        seen: list[pymupdf.Rect] = []
        for k in range(int(r.y0 // BUCKET), int(r.y1 // BUCKET) + 1):
            for b in self.rows.get(k, ()):
                if b.x0 < r.x1 and r.x0 < b.x1 and b.y0 < r.y1 and r.y0 < b.y1 and b not in seen:
                    seen.append(b)
        return seen


def _band(bbox) -> tuple[float, float]:
    h = bbox[3] - bbox[1]
    return bbox[1] + BAND_TOP * h, bbox[1] + BAND_BOTTOM * h


def _clear_of(r: pymupdf.Rect, boxes: _Boxes, min_h: float) -> pymupdf.Rect | None:
    """Shrink r vertically away from protected boxes above / below it; None if impossible."""
    r = pymupdf.Rect(r)
    for _ in range(8):
        hits = boxes.hits(r)
        if not hits:
            return r
        mid = (r.y0 + r.y1) / 2
        for b in hits:
            if (b.y0 + b.y1) / 2 <= mid:
                r.y0 = max(r.y0, b.y1 + 0.01)
            else:
                r.y1 = min(r.y1, b.y0 - 0.01)
        if r.y1 - r.y0 < min_h:
            return None
    return None if boxes.hits(r) else r


def _span_rects(sp: dict, boxes: _Boxes) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """-> (redaction rectangles, glyph boxes to cover with white instead).  The rectangles
    remove the glyphs of one span and touch no glyph that stays; a glyph that physically
    overlaps one that stays (a stamp, a watermark) cannot be removed alone and is covered."""
    chars = [c for c in sp["chars"] if not c["c"].isspace() and c["bbox"][2] > c["bbox"][0]]
    if not chars:
        return [], []
    y0, y1 = _band(sp["bbox"])
    if y1 <= y0:
        return [], []
    left = min(chars, key=lambda c: c["bbox"][0])["bbox"]
    right = max(chars, key=lambda c: c["bbox"][2])["bbox"]
    x0 = left[0] + BAND_INSET_X * (left[2] - left[0])
    x1 = right[2] - BAND_INSET_X * (right[2] - right[0])
    whole = pymupdf.Rect(x0, y0, max(x1, x0 + 0.02), y1)
    if not boxes.hits(whole):
        return [whole], []
    rects: list[pymupdf.Rect] = []                  # something stays close by: glyph by glyph
    cover: list[pymupdf.Rect] = []
    min_h = 0.04 * (sp["bbox"][3] - sp["bbox"][1])
    for c in chars:
        b = c["bbox"]
        r = pymupdf.Rect(b[0] + BAND_INSET_X * (b[2] - b[0]), y0, b[2] - BAND_INSET_X * (b[2] - b[0]), y1)
        r = _clear_of(r, boxes, min_h)
        if r is None:
            cover.append(pymupdf.Rect(b))
        else:
            rects.append(r)
    return rects, cover


def _glyph_key(c: dict) -> tuple:
    return c["c"], round(c["bbox"][0], 1), round(c["bbox"][1], 1)


@dataclass
class RedactPlan:
    units: list[Unit] = field(default_factory=list)      # original removed, translation goes on top
    covered: int = 0                                     # glyphs covered with white instead of removed
    lost: int = 0                                        # visible glyphs of other text removed anyway


def redact_page(page, table: dict[str, str], blocks: list[dict] | None = None) -> RedactPlan:
    """Remove the original glyphs of every unit that has a (different) translation and
    nothing else.  Invisible text (OCR layers) is not protected: removing it changes nothing
    on screen, and protecting it would turn a duplicated text layer into white boxes."""
    blocks = text_blocks(page) if blocks is None else blocks
    units = [u for u in page_units(page, blocks=blocks) if u.text in table and table[u.text] != u.text]
    plan = RedactPlan(units=units)
    if not units:
        return plan
    replaced = {id(sp) for u in units for sp in u.spans()}
    boxes = _Boxes()
    kept: Counter = Counter()
    for b in blocks:
        for ln in b["lines"]:
            for sp in ln["spans"]:
                if id(sp) in replaced or not _visible(sp):
                    continue
                for c in sp["chars"]:
                    if not c["c"].isspace():
                        kept[_glyph_key(c)] += 1
                        boxes.add(_shrink(c["bbox"], PROTECT_INSET, PROTECT_INSET))
    rects: list[pymupdf.Rect] = []
    cover: list[pymupdf.Rect] = []
    for u in units:
        for sp in u.spans():
            r, c = _span_rects(sp, boxes)
            rects += r
            cover += c
    if rects:
        for r in rects:
            page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        after: Counter = Counter(_glyph_key(c) for b in text_blocks(page) for ln in b["lines"]
                                 for sp in ln["spans"] if _visible(sp) for c in sp["chars"] if not c["c"].isspace())
        plan.lost = sum((kept - after).values())
    if cover:
        shape = page.new_shape()
        for r in cover:
            shape.draw_rect(r)
        shape.finish(color=None, fill=(1, 1, 1), width=0)
        shape.commit(overlay=True)
        plan.covered = len(cover)
    return plan


# ------------------------------------------------------------------ layout mode
def _css(sp: dict, family: str) -> str:
    c = sp.get("color", 0) or 0
    flags = sp.get("flags", 0) or 0
    return (f"font-size:{sp['size']:.1f}pt; color:#{c:06x}; line-height:1.15;"
            f"font-weight:{'bold' if flags & FLAG_BOLD else 'normal'};"
            f"font-style:{'italic' if flags & FLAG_ITALIC else 'normal'};"
            f"font-family:{family};")


def _fit_rect(page_rect: pymupdf.Rect, u: Unit, out: str, size: float, measure) -> pymupdf.Rect:
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
            r.x1 = min(page_rect.x1 - 5, r.x0 + need)
    return r


def _draw_unit(dev, page_rect: pymupdf.Rect, u: Unit, out: str, font: PdfFont) -> bool:
    """Lay out one translation into its box on the overlay page. -> True if shrunk."""
    sp = u.style()
    rect = _fit_rect(page_rect, u, out, sp["size"], font.measure)
    if rect.is_empty:
        return False
    body = html.escape(out).replace("\n", "<br/>")
    story = pymupdf.Story(html=f'<div style="{_css(sp, font.family)}">{body}</div>',
                          user_css="body {margin:1px;}" + font.css_head, archive=font.archive)
    fit = story.fit_scale(pymupdf.Rect(0, 0, rect.width, rect.height), scale_min=1, scale_max=None,
                          flags=pymupdf.mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW)
    # the original text is already gone: never drop a translation, at worst draw it tiny
    factor = fit.parameter if fit.big_enough and fit.parameter else 20.0
    story.reset()
    story.place(pymupdf.Rect(0, 0, rect.width * factor, rect.height * factor))
    scale = 1 / factor
    story.draw(dev, pymupdf.Matrix(scale, 0, 0, scale, rect.x0, rect.y0))
    return scale < 0.999


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


def write_story(writer, body_html: str, width: float, height: float, font: PdfFont) -> int:
    """Flow HTML over as many pages of the given size as needed. -> pages written."""
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
    return n


def translation_page_html(pno: int, text: str) -> str:
    head = f'<p class="hdr">p.{pno + 1} 번역</p>'
    return head + mdlite.to_html(mdlite.parse(text))


def write_layout(src: Path, dst: Path, table: dict[str, str], added: dict[int, str], font: PdfFont,
                 digital: set[int] | None = None) -> dict:
    """digital: page numbers whose text is replaced in place (None = every page)."""
    doc = open_pdf(src)
    stats = {"units": 0, "overflow": 0, "covered": 0, "lost": 0, "added_pages": 0}
    try:
        # 1. remove the original text page by page and lay out every translation on one
        #    overlay document (one embedded font for the whole file)
        buf = io.BytesIO()
        writer = pymupdf.DocumentWriter(buf)
        shown: list[tuple[int, int]] = []
        try:
            for page in doc:
                if digital is not None and page.number not in digital:
                    continue                            # scanned / OCR-layer pages stay untouched
                if page.rotation:
                    page.remove_rotation()
                plan = redact_page(page, table)
                units = plan.units
                if not units:
                    continue
                r = page.rect
                dev = writer.begin_page(pymupdf.Rect(0, 0, r.width, r.height))
                for u in units:
                    if _draw_unit(dev, pymupdf.Rect(0, 0, r.width, r.height), u, table[u.text], font):
                        stats["overflow"] += 1
                writer.end_page()
                shown.append((page.number, len(shown)))
                stats["units"] += len(units)
                stats["covered"] += plan.covered
                stats["lost"] += plan.lost
        finally:
            writer.close()
        if shown:
            overlay = pymupdf.open("pdf", buf.getvalue())
            try:
                for pno, idx in shown:
                    page = doc[pno]
                    page.show_pdf_page(page.rect, overlay, idx)
            finally:
                overlay.close()
        del buf

        # 2. translations of scanned pages: one document, inserted once, then moved into place
        texts = {pno: t for pno, t in added.items() if t.strip()}
        if texts:
            buf2 = io.BytesIO()
            writer2 = pymupdf.DocumentWriter(buf2)
            counts: dict[int, int] = {}
            try:
                for pno in sorted(texts):
                    r = doc[pno].rect
                    counts[pno] = write_story(writer2, translation_page_html(pno, texts[pno]), r.width, r.height, font)
            finally:
                writer2.close()
            extra = pymupdf.open("pdf", buf2.getvalue())
            try:
                n0 = doc.page_count
                doc.insert_pdf(extra)
            finally:
                extra.close()
            order: list[int] = []
            cursor = n0
            for pno in range(n0):
                order.append(pno)
                k = counts.get(pno, 0)
                order.extend(range(cursor, cursor + k))
                cursor += k
            doc.select(order)
            stats["added_pages"] = sum(counts.values())
        try:
            doc.subset_fonts()
        except Exception:  # noqa: BLE001 - subsetting is an optimisation only
            pass
        doc.save(str(dst), garbage=4, deflate=True)
    finally:
        doc.close()
    if stats["lost"]:
        logger.warning("PDF 번역: 번역하지 않은 글자 %d개가 함께 지워졌습니다", stats["lost"])
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
def _scan_weight(info: PageInfo) -> int:
    """Progress weight of a vision-model page.  An OCR-layer page weighs at least its
    text, so translating that text instead (no vision) never moves progress back."""
    if info.kind == "ocr_layer":
        return max(SCAN_UNIT_CHARS, sum(len(t) for t in dict.fromkeys(info.texts)))
    return SCAN_UNIT_CHARS


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
        weights = {i: _scan_weight(pages[i]) for i in vlm_pages}
        if vlm_pages:
            ctx.add_units(len(vlm_pages), sum(weights.values()))
        table = await ctx.translate_strings(texts) if texts else {}

        added: dict[int, str] = {}
        vision_off = False
        deferred: list[int] = []
        in_place: list[int] = []
        for pno in vlm_pages:
            ctx.check_cancel()
            ctx.set_status("translating")
            if not vision_off:
                png = await asyncio.to_thread(render_page, ctx.input_path, pno)
                try:
                    added[pno] = (await ctx.describe_image(png, "image/png")).strip()
                    ctx.unit_done(1, weights[pno])
                    continue
                except VisionUnavailable:
                    vision_off = True
            if pages[pno].kind == "ocr_layer" and pages[pno].texts:
                deferred.append(pno)                   # translated from its text layer below
            elif pages[pno].kind == "scanned" and any(has_letters(t) for t in pages[pno].texts):
                in_place.append(pno)                   # its visible text is translated in place below
            else:
                ctx.unit_done(1, weights[pno])

        if in_place:
            # a scan without vision: at least its visible text (title, stamp) is translated in place
            ctx.add_units(-len(in_place), -sum(weights[i] for i in in_place))
            more = [t for t in dict.fromkeys(t for i in in_place for t in pages[i].texts) if t not in table]
            if more:
                table.update(await ctx.translate_strings(more))
                texts += more
            for i in in_place:
                pages[i] = PageInfo("digital", pages[i].texts)
        if deferred:
            # OCR-layer pages can still be translated from their (invisible) text layer.
            # The work reserved for the vision model is released first; it covers the text.
            ctx.add_units(-len(deferred), -sum(weights[i] for i in deferred))
            ocr_texts = list(dict.fromkeys(t for i in deferred for t in pages[i].texts))
            todo = [t for t in ocr_texts if t not in table]
            ocr_table = {**table, **(await ctx.translate_strings(todo) if todo else {})}
            for i in deferred:
                added[i] = "\n\n".join(ocr_table.get(t, t) for t in pages[i].texts)
        if vision_off:
            missing = [i + 1 for i in vlm_pages if i not in added and i not in in_place]
            if missing:
                ctx.warn(f"{MSG_VISION_UNAVAILABLE} 스캔 페이지({_page_list(missing)})는 번역하지 않았습니다.")
            if in_place:
                pages_text = _page_list([i + 1 for i in in_place])
                ctx.warn(f"{MSG_VISION_UNAVAILABLE} 스캔 페이지({pages_text})는 이미지 속 글자를 빼고 번역했습니다.")

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
