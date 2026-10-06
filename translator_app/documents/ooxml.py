"""In-place DOCX / PPTX / XLSX translation (lxml + zipfile only, no python-docx).

DOCX: document, headers, footers, footnotes, endnotes, comments, SmartArt text;
      tables, text boxes, hyperlinks, content controls; field codes, deleted text
      and other non-text objects are kept as untranslated placeholders.
PPTX: slides, notes, SmartArt text (masters/layouts are not translated).
XLSX: shared strings (incl. rich text), inline strings, comments, drawing shapes.
      Formulas, numbers, sheet names and strings used as formula literals are kept;
      fullCalcOnLoad is set so cached formula results are recomputed on open.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from lxml import etree

from translator_app.documents.base import DocumentError, HandlerContext, Translate, TwoPassHandler
from translator_app.documents.inline_tags import Adapter, Piece, translate_paragraphs
from translator_app.documents.ziputil import check_zip, parse_xml, rewrite_zip

# ====================================================================== DOCX
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def w(t: str) -> str:
    return f"{{{W}}}{t}"


DOCX_PARTS = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml")
DOCX_BILINGUAL_PARTS = re.compile(r"word/(document|footnotes|endnotes|comments)\.xml")
DOCX_DIAGRAMS = re.compile(r"word/diagrams/(data|drawing)\d*\.xml")

TEXTISH = {w("t"), w("tab"), w("br"), w("cr"), w("noBreakHyphen"), w("softHyphen")}
DROP = {w("lastRenderedPageBreak")}                       # layout cache only
MARKERS = {w("bookmarkStart"), w("bookmarkEnd"), w("commentRangeStart"), w("commentRangeEnd"),
           w("permStart"), w("permEnd"), w("moveFromRangeStart"), w("moveFromRangeEnd"),
           w("moveToRangeStart"), w("moveToRangeEnd")}
CONTAINERS = {w("hyperlink"), w("smartTag"), w("customXml"), w("ins"), w("moveTo")}
IGNORED_RPR = {w("lang"), w("noProof"), w("proofErr")}


def _rpr_key(r):
    rpr = r.find(w("rPr"))
    if rpr is None:
        return b""
    c = copy.deepcopy(rpr)
    for el in list(c):
        if el.tag in IGNORED_RPR:
            c.remove(el)
    rf = c.find(w("rFonts"))
    if rf is not None:
        rf.attrib.pop(w("hint"), None)
        if not rf.attrib:
            c.remove(rf)
    if len(c) == 0 and not c.attrib:
        return b""
    return etree.tostring(c, method="c14n")


def _is_textish(ch) -> bool:
    if ch.tag == w("br"):
        return ch.get(w("type")) in (None, "textWrapping")
    return ch.tag in TEXTISH


def _run_text(r) -> str:
    out = []
    for ch in r:
        t = ch.tag
        if t == w("t"):
            out.append(ch.text or "")
        elif t == w("tab"):
            out.append("\t")
        elif t in (w("br"), w("cr")):
            out.append("\n")
        elif t == w("noBreakHyphen"):
            out.append("\u2011")
        elif t == w("softHyphen"):
            out.append("\u00ad")
    return "".join(out)


def set_run_text(r, text: str) -> None:
    for ch in list(r):
        if ch.tag != w("rPr"):
            r.remove(ch)
    for tok in re.split(r"([\t\n])", text):
        if tok == "\t":
            etree.SubElement(r, w("tab"))
        elif tok == "\n":
            etree.SubElement(r, w("br"))
        elif tok:
            t = etree.SubElement(r, w("t"))
            t.text = tok
            t.set(f"{{{XML_NS}}}space", "preserve")


def _split_mixed_runs(p) -> None:
    """Split runs that mix text with atoms (footnoteReference, drawing, fldChar...)
    into one run per kind. Semantics-preserving; makes every run pure."""
    for r in list(p.iterchildren(w("r"))):
        for ch in list(r):
            if ch.tag in DROP:
                r.remove(ch)
        kids = [ch for ch in r if ch.tag != w("rPr")]
        groups, cur = [], []
        for ch in kids:
            kind = "t" if _is_textish(ch) else "a"
            if kind == "t" and cur and cur[0][0] == "t":
                cur.append((kind, ch))
            else:
                if cur:
                    groups.append(cur)
                cur = [(kind, ch)]
        if cur:
            groups.append(cur)
        if len(groups) <= 1:
            continue
        rpr = r.find(w("rPr"))
        parent, idx = r.getparent(), r.getparent().index(r)
        for k, g in enumerate(groups):
            nr = etree.Element(w("r"), attrib=dict(r.attrib))
            if rpr is not None:
                nr.append(copy.deepcopy(rpr))
            for _, ch in g:
                nr.append(ch)
            parent.insert(idx + k, nr)
        parent.remove(r)


def _fld_type(r):
    fc = r.find(w("fldChar"))
    return fc.get(w("fldCharType")) if fc is not None else None


def _container_runs(c):
    host = c.find(w("sdtContent")) if c.tag == w("sdt") else c
    return [] if host is None else list(host.iterchildren(w("r")))


def _container_is_plain(c) -> bool:
    host = c.find(w("sdtContent")) if c.tag == w("sdt") else c
    if host is None:
        return False
    for ch in host:
        if ch.tag == w("r"):
            if any(not (_is_textish(k) or k.tag in DROP) for k in ch if k.tag != w("rPr")):
                return False
        elif ch.tag not in MARKERS and ch.tag != w("proofErr"):
            return False
    return bool(_container_runs(c))


class DocxAdapter(Adapter):
    def pieces(self, p):
        for pe in list(p.iterchildren(w("proofErr"))):
            p.remove(pe)
        _split_mixed_runs(p)
        out: list[Piece] = []
        field: list | None = None
        depth = 0
        for ch in p:
            tag = ch.tag
            if tag == w("pPr") or tag in MARKERS:
                continue
            if tag == w("r"):
                ft = _fld_type(ch)
                if field is not None:                       # inside field code
                    field.append(ch)
                    if ft == "begin":
                        depth += 1
                    elif ft in ("separate", "end"):
                        if depth == 1:
                            out.append(Piece("atom", nodes=field))
                            field = None
                            if ft == "end":
                                depth = 0
                        elif ft == "end":
                            depth -= 1
                    continue
                if ft == "begin":
                    field, depth = [ch], 1
                    continue
                if ft == "end":                               # end of the field result
                    out.append(Piece("atom", nodes=[ch]))
                    continue
                if ch.find(w("instrText")) is not None:
                    out.append(Piece("atom", nodes=[ch]))
                    continue
                kids = [k for k in ch if k.tag != w("rPr")]
                if kids and all(_is_textish(k) for k in kids):
                    out.append(Piece("text", key=_rpr_key(ch), text=_run_text(ch), nodes=[ch], proto=ch))
                elif not kids:
                    continue                                  # empty run: leave in place
                else:
                    out.append(Piece("atom", nodes=[ch]))
                continue
            if field is not None:
                field.append(ch)
                continue
            if (tag in CONTAINERS or tag == w("sdt")) and _container_is_plain(ch):
                text = "".join(_run_text(r) for r in _container_runs(ch))
                out.append(Piece("text", key=("container", id(ch)), text=text, nodes=[ch], proto=ch))
            else:                                             # fldSimple, del, oMath, sdt w/ objects ...
                out.append(Piece("atom", nodes=[ch]))
        if field is not None:                                 # field continues in next paragraph
            out.append(Piece("atom", nodes=field))
        return out

    def _write(self, node, text):
        if node.tag == w("r"):
            set_run_text(node, text)
            return
        runs = _container_runs(node)
        set_run_text(runs[0], text)
        for r in runs[1:]:
            r.getparent().remove(r)

    def build(self, piece, text, first_use):
        node = piece.proto if first_use else copy.deepcopy(piece.proto)
        self._write(node, text)
        return [node]

    def set_text(self, piece, text):
        self._write(piece.nodes[0], text)
        for n in piece.nodes[1:]:
            self._write(n, "")


# --- bilingual copy: things that must stay unique in a document are removed
_CLONE_DROP_CHILDREN = MARKERS | {
    w("del"), w("moveFrom"), w("proofErr"), f"{{{MC}}}AlternateContent",
}
_CLONE_UNWRAP = {w("ins"), w("moveTo"), w("fldSimple"), w("smartTag"), w("customXml")}
_CLONE_DROP_IN_RUN = {
    w("footnoteReference"), w("endnoteReference"), w("commentReference"), w("footnoteRef"),
    w("endnoteRef"), w("annotationRef"), w("drawing"), w("pict"), w("object"), w("fldChar"),
    w("instrText"), w("delText"), w("delInstrText"), w("separator"), w("continuationSeparator"),
    w("lastRenderedPageBreak"), f"{{{MC}}}AlternateContent",
}


def _unwrap(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    if el.tag == w("sdt"):
        host = el.find(w("sdtContent"))
        kids = [] if host is None else list(host)
    else:
        kids = list(el)
    parent.remove(el)
    for k, ch in enumerate(kids):
        parent.insert(idx + k, ch)


def docx_clone_paragraph(p):
    c = copy.deepcopy(p)
    for attr in (f"{{{W14}}}paraId", f"{{{W14}}}textId"):
        c.attrib.pop(attr, None)
    ppr = c.find(w("pPr"))
    if ppr is not None:
        for tag in ("sectPr", "pPrChange"):
            for el in ppr.findall(w(tag)):
                ppr.remove(el)
        prpr = ppr.find(w("rPr"))
        if prpr is not None:
            for tag in ("ins", "del", "moveFrom", "moveTo", "rPrChange"):
                for el in prpr.findall(w(tag)):
                    prpr.remove(el)
        numpr = ppr.find(w("numPr"))
        if numpr is not None:                     # the copy must not take a list number
            for ch in list(numpr):
                numpr.remove(ch)
            etree.SubElement(numpr, w("ilvl")).set(w("val"), "0")
            etree.SubElement(numpr, w("numId")).set(w("val"), "0")
    for el in list(c.iter()):
        if el is not c and el.tag in _CLONE_DROP_CHILDREN and el.getparent() is not None:
            el.getparent().remove(el)
    for el in reversed([e for e in c.iter() if e is not c and (e.tag in _CLONE_UNWRAP or e.tag == w("sdt"))]):
        _unwrap(el)
    for r in list(c.iter(w("r"))):
        for ch in list(r):
            if ch.tag in _CLONE_DROP_IN_RUN or (ch.tag == w("br") and ch.get(w("type")) in ("page", "column")):
                r.remove(ch)
            elif ch.tag == w("rPr"):
                for tag in ("ins", "del", "rPrChange"):
                    for el in ch.findall(w(tag)):
                        ch.remove(el)
    if not any((t.text or "").strip() for t in c.iter(w("t"))):
        return None
    return c


def translate_docx(src, dst, translate: Translate, *, bilingual: bool = False) -> dict:
    stats: dict = {}
    ad = DocxAdapter()

    def tf(name, tree):
        if DOCX_DIAGRAMS.fullmatch(name):
            translate_drawingml_tree(tree, translate, stats)
            return
        paras = list(tree.getroot().iter(w("p")))
        clone = docx_clone_paragraph if bilingual and DOCX_BILINGUAL_PARTS.fullmatch(name) else None
        translate_paragraphs(ad, paras, translate, stats, clone=clone)

    def select(n):
        return bool(DOCX_PARTS.fullmatch(n) or DOCX_DIAGRAMS.fullmatch(n))

    stats["parts"] = rewrite_zip(src, dst, select, tf)
    return stats


# ====================================================================== DrawingML (PPTX)
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def a(t: str) -> str:
    return f"{{{A}}}{t}"


PPTX_PARTS = re.compile(r"ppt/(slides/slide|notesSlides/notesSlide|diagrams/data|diagrams/drawing)\d+\.xml")
IGNORED_RPR_ATTRS = {"lang", "altLang", "dirty", "err", "noProof", "smtClean", "smtId", "bmk"}


def _a_rpr_key(el):
    rpr = el.find(a("rPr"))
    if rpr is None:
        return b""
    c = copy.deepcopy(rpr)
    for k in IGNORED_RPR_ATTRS:
        c.attrib.pop(k, None)
    if len(c) == 0 and not c.attrib:
        return b""
    return etree.tostring(c, method="c14n")


class DrawingMLAdapter(Adapter):
    def __init__(self, set_lang: str | None = None):
        self.set_lang = set_lang          # e.g. "ko-KR" -> rPr@lang on rewritten runs

    def pieces(self, p):
        out: list[Piece] = []
        pending_br: list = []
        for ch in p:
            tag = ch.tag
            if tag in (a("pPr"), a("endParaRPr")):
                continue
            if tag == a("r"):
                key = _a_rpr_key(ch)
                for br in pending_br:   # line breaks before the first run inherit its key
                    out.append(Piece("text", key=key, text="\n", nodes=[br], proto=ch))
                pending_br = []
                t = ch.find(a("t"))
                out.append(Piece("text", key=key, text=(t.text or "") if t is not None else "",
                                 nodes=[ch], proto=ch))
            elif tag == a("br"):
                prev = next((q for q in reversed(out) if q.kind == "text"), None)
                if prev is not None:
                    out.append(Piece("text", key=prev.key, text="\n", nodes=[ch], proto=prev.proto))
                else:
                    pending_br.append(ch)
            else:                                 # a:fld (slide number/date), math, ...
                out.append(Piece("atom", nodes=[ch]))
        for br in pending_br:
            out.append(Piece("atom", nodes=[br]))
        return out

    def _run(self, proto, text):
        r = copy.deepcopy(proto)
        for ch in list(r):
            if ch.tag != a("rPr"):
                r.remove(ch)
        t = etree.SubElement(r, a("t"))
        t.text = text
        if self.set_lang:
            rpr = r.find(a("rPr"))
            if rpr is None:
                rpr = etree.Element(a("rPr"))
                r.insert(0, rpr)
            rpr.set("lang", self.set_lang)
            rpr.attrib.pop("altLang", None)
        return r

    def _br(self, proto):
        br = etree.Element(a("br"))
        rpr = proto.find(a("rPr"))
        if rpr is not None:
            br.append(copy.deepcopy(rpr))
        return br

    def build(self, piece, text, first_use):
        nodes = []
        for i, line in enumerate(text.split("\n")):
            if i:
                nodes.append(self._br(piece.proto))
            if line:
                nodes.append(self._run(piece.proto, line))
        return nodes

    def set_text(self, piece, text):
        nodes = piece.nodes
        parent = nodes[0].getparent()
        idx = parent.index(nodes[0])
        for n in nodes:
            parent.remove(n)
        for k, n in enumerate(self.build(piece, text, True)):
            parent.insert(idx + k, n)


def translate_drawingml_tree(tree, translate: Translate, stats: dict, set_lang: str | None = None) -> None:
    paras = list(tree.getroot().iter(a("p")))
    translate_paragraphs(DrawingMLAdapter(set_lang), paras, translate, stats)


def translate_pptx(src, dst, translate: Translate, set_lang: str | None = None) -> dict:
    stats: dict = {}
    stats["parts"] = rewrite_zip(src, dst, lambda n: bool(PPTX_PARTS.fullmatch(n)),
                                 lambda n, t: translate_drawingml_tree(t, translate, stats, set_lang))
    return stats


LANG_TAGS = {
    "ko": "ko-KR", "en": "en-US", "ja": "ja-JP", "zh": "zh-CN", "zh-cn": "zh-CN", "zh-tw": "zh-TW",
    "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "ru": "ru-RU", "pt": "pt-PT",
    "vi": "vi-VN", "th": "th-TH", "id": "id-ID", "ar": "ar-SA", "nl": "nl-NL", "pl": "pl-PL",
    "tr": "tr-TR", "uk": "uk-UA",
}


def lang_tag(code: str) -> str | None:
    c = (code or "").strip().lower()
    return LANG_TAGS.get(c) or LANG_TAGS.get(c.split("-")[0])


# ====================================================================== XLSX
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def s_(t: str) -> str:
    return f"{{{S}}}{t}"


_ESC = re.compile(r"_x([0-9A-Fa-f]{4})_")


def xl_unescape(s: str) -> str:            # Excel encodes control chars as _xHHHH_
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)


def xl_escape(s: str) -> str:
    s = _ESC.sub(lambda m: "_x005F" + m.group(0), s)        # literal "_x000D_" text
    return "".join(c if c in "\t\n" or ord(c) >= 0x20 else f"_x{ord(c):04X}_" for c in s)


def _s_rpr_key(r):
    rpr = r.find(s_("rPr"))
    return b"" if rpr is None else etree.tostring(rpr, method="c14n")


def _set_t(t, text):
    t.text = xl_escape(text)
    if text != text.strip() or "\n" in text:
        t.set(f"{{{XML_NS}}}space", "preserve")


XLSX_MAX_CELL = 32767


class StringItemAdapter(Adapter):
    """<si>, <is> (inline string) and comment <text> share the CT_Rst model."""

    def pieces(self, si):
        for rph in si.findall(s_("rPh")):         # phonetic runs hold char offsets -> invalid after translation
            si.remove(rph)
        t = si.find(s_("t"))
        if t is not None:
            return [Piece("text", key=b"", text=xl_unescape(t.text or ""), nodes=[t], proto=t)]
        out = []
        for r in si.findall(s_("r")):
            rt = r.find(s_("t"))
            out.append(Piece("text", key=_s_rpr_key(r), text=xl_unescape((rt.text if rt is not None else "") or ""),
                             nodes=[r], proto=r))
        return out

    def _write(self, node, text):
        text = text[:XLSX_MAX_CELL]
        if node.tag == s_("t"):
            _set_t(node, text)
        else:
            rt = node.find(s_("t"))
            if rt is None:
                rt = etree.SubElement(node, s_("t"))
            _set_t(rt, text)

    def build(self, piece, text, first_use):
        node = piece.proto if first_use else copy.deepcopy(piece.proto)
        self._write(node, text)
        return [node]

    def set_text(self, piece, text):
        self._write(piece.nodes[0], text)


XLSX_SST = "xl/sharedStrings.xml"
XLSX_SHEET = re.compile(r"xl/worksheets/sheet\d+\.xml")
XLSX_COMMENTS = re.compile(r"xl/comments\d*\.xml|xl/comments/comment\d+\.xml")
XLSX_DRAWING = re.compile(r"xl/drawings/drawing\d+\.xml")

_LIT = re.compile(r'"((?:[^"]|"")*)"')


def formula_literals(path) -> set[str]:
    """String literals used in formulas / data-validation lists, e.g. =IF(C2="Yes",..)
    or list "Yes,No".  Cells whose whole text equals one of them are left
    untranslated, otherwise the formula logic silently changes."""
    import zipfile

    lits: set[str] = set()
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if not XLSX_SHEET.fullmatch(n):
                continue
            root = parse_xml(z.read(n)).getroot()
            for f in list(root.iter(s_("f"))) + list(root.iter(s_("formula1"))):
                for m in _LIT.finditer(f.text or ""):
                    v = m.group(1).replace('""', '"')
                    lits.add(v)
                    if f.tag == s_("formula1"):
                        lits.update(x.strip() for x in v.split(","))
    lits.discard("")
    return lits


def force_full_calc(tree) -> None:
    """Cached <v> results of formulas referring to translated cells are stale;
    ask Excel/LibreOffice to recalculate on open."""
    root = tree.getroot()
    calc = root.find(s_("calcPr"))
    if calc is None:
        calc = etree.Element(s_("calcPr"))
        anchor = None
        for tag in ("sheets", "functionGroups", "externalReferences", "definedNames"):
            el = root.find(s_(tag))
            if el is not None:
                anchor = el
        if anchor is None:
            return
        anchor.addnext(calc)
    calc.set("fullCalcOnLoad", "1")


def translate_xlsx(src, dst, translate: Translate, drawings: bool = True,
                   protect_formula_literals: bool = True, recalc_on_open: bool = True) -> dict:
    stats: dict = {}
    ad = StringItemAdapter()
    protected = formula_literals(src) if protect_formula_literals else set()
    stats["protected"] = len(protected)

    def keep(item):
        t = item.find(s_("t"))
        return not (t is not None and xl_unescape(t.text or "") in protected)

    def select(n):
        return ((recalc_on_open and n == "xl/workbook.xml") or n == XLSX_SST or bool(XLSX_SHEET.fullmatch(n))
                or bool(XLSX_COMMENTS.fullmatch(n)) or (drawings and bool(XLSX_DRAWING.fullmatch(n))))

    def tf(name, tree):
        root = tree.getroot()
        if name == "xl/workbook.xml":
            force_full_calc(tree)
            return
        if name == XLSX_SST:
            items = root.findall(s_("si"))
        elif XLSX_SHEET.fullmatch(name):
            # inline strings only; a cell with <f> is a formula -> never touched
            items = [c.find(s_("is")) for c in root.iter(s_("c"))
                     if c.get("t") == "inlineStr" and c.find(s_("f")) is None and c.find(s_("is")) is not None]
        elif XLSX_COMMENTS.fullmatch(name):
            items = list(root.iter(s_("text")))
        else:
            translate_drawingml_tree(tree, translate, stats)
            return
        translate_paragraphs(ad, [i for i in items if keep(i)], translate, stats)

    stats["parts"] = rewrite_zip(src, dst, select, tf)
    return stats


# ====================================================================== handlers
_WRONG = "형식이 올바르지 않은 파일입니다. 확장자와 내용이 다릅니다."


class DocxHandler(TwoPassHandler):
    def validate(self, ctx: HandlerContext) -> None:
        check_zip(ctx.input_path, ("word/document.xml",), wrong_type=_WRONG)

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        translate_docx(src, dst, translate, bilingual=ctx.output_mode == "bilingual")


class PptxHandler(TwoPassHandler):
    def validate(self, ctx: HandlerContext) -> None:
        check_zip(ctx.input_path, ("ppt/presentation.xml",), wrong_type=_WRONG)

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        translate_pptx(src, dst, translate, set_lang=lang_tag(ctx.target_lang))


class XlsxHandler(TwoPassHandler):
    def validate(self, ctx: HandlerContext) -> None:
        check_zip(ctx.input_path, ("xl/workbook.xml",), wrong_type=_WRONG)

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        translate_xlsx(src, dst, translate)


__all__ = [
    "DocxHandler", "PptxHandler", "XlsxHandler", "DocumentError",
    "translate_docx", "translate_pptx", "translate_xlsx", "translate_drawingml_tree",
]
