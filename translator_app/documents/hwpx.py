"""In-place HWPX (OWPML, KS X 6101) translation: Contents/section*.xml.

hp:p > hp:run[@charPrIDRef] > (hp:t | hp:ctrl | hp:tbl | hp:pic | hp:secPr ...)
hp:t is MIXED content: text + <hp:tab/>, <hp:lineBreak/>, <hp:nbSpace/>, <hp:fwSpace/>,
<hp:hyphen/>, <hp:markpenBegin/>/<hp:markpenEnd/>, <hp:titleMark/>, track-change marks.
Formatting key = charPrIDRef (formats are ID references into header.xml).

hp:linesegarray (cached line layout) is removed from every changed paragraph so
Hancom re-lays the text out; Preview/PrvText.txt is rewritten with the
translation.  The mimetype member stays first and STORED.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from lxml import etree

from translator_app.documents.base import DocumentError, HandlerContext, Translate, TwoPassHandler
from translator_app.documents.inline_tags import Adapter, Piece, translate_paragraphs
from translator_app.documents.ziputil import check_zip, parse_xml, read_member, replace_member, rewrite_zip

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"


def hp(t: str) -> str:
    return f"{{{HP}}}{t}"


HWPX_SECTIONS = re.compile(r"Contents/section\d+\.xml")
CHAR_ELEMS = {hp("tab"): "\t", hp("lineBreak"): "\n", hp("nbSpace"): "\u00a0",
              hp("fwSpace"): "\u3000", hp("hyphen"): "\u00ad"}
REV_CHAR = {"\t": "tab", "\n": "lineBreak", "\u00a0": "nbSpace", "\u3000": "fwSpace", "\u00ad": "hyphen"}
_SPLIT = re.compile("([\t\n\u00a0\u3000\u00ad])")


def _t_segments(t):
    """Split one hp:t into [('text', str, [tab elems]) | ('atom', elem)]."""
    segs, buf, tabs = [], [t.text or ""], []
    for ch in t:
        if ch.tag in CHAR_ELEMS:
            buf.append(CHAR_ELEMS[ch.tag])
            if ch.tag == hp("tab"):
                tabs.append(ch)
        else:
            segs.append(("text", "".join(buf), tabs))
            segs.append(("atom", ch))
            buf, tabs = [], []
        buf.append(ch.tail or "")
    segs.append(("text", "".join(buf), tabs))
    return [s for s in segs if not (s[0] == "text" and s[1] == "")]


def _new_run(run, child):
    r = etree.Element(hp("run"), attrib=dict(run.attrib))
    r.append(child)
    return r


class HwpxAdapter(Adapter):
    def __init__(self):
        self.tab_proto = None        # first original <hp:tab> seen (keeps leader/type attrs)

    def _normalize(self, p):
        """Make every run either pure text (one hp:t, text-ish content only) or pure atom."""
        for run in list(p.iterchildren(hp("run"))):
            kids = list(run)
            ts = [k for k in kids if k.tag == hp("t")]
            if not ts or not any(s[0] == "text" and s[1].strip() for t in ts for s in _t_segments(t)):
                continue                                   # no visible text -> whole run is an atom
            if len(kids) == 1 and all(s[0] == "text" for s in _t_segments(kids[0])):
                continue                                   # already pure text
            new = []
            for k in kids:
                if k.tag != hp("t"):
                    new.append(_new_run(run, k))
                    continue
                for seg in _t_segments(k):
                    t = etree.Element(hp("t"))
                    if seg[0] == "atom":
                        el = seg[1]
                        el.tail = None
                        t.append(el)
                    else:
                        self._fill_t(t, seg[1], seg[2])
                    new.append(_new_run(run, t))
            parent, idx = run.getparent(), run.getparent().index(run)
            parent.remove(run)
            for i, n in enumerate(new):
                parent.insert(idx + i, n)

    def pieces(self, p):
        self._normalize(p)
        out = []
        for run in p.iterchildren(hp("run")):
            kids = list(run)
            if not kids:
                continue
            if len(kids) == 1 and kids[0].tag == hp("t") and all(s[0] == "text" for s in _t_segments(kids[0])):
                t = kids[0]
                tabs = list(t.iter(hp("tab")))
                if tabs and self.tab_proto is None:
                    self.tab_proto = copy.deepcopy(tabs[0])
                text = "".join(s[1] for s in _t_segments(t))
                out.append(Piece("text", key=run.get("charPrIDRef"), text=text, nodes=[run], proto=run))
            else:
                out.append(Piece("atom", nodes=[run]))
        return out

    def _fill_t(self, t, text, tabs=None):
        for ch in list(t):
            t.remove(ch)
        t.text = None
        tabs = list(tabs or [])
        last = None
        for tok in _SPLIT.split(text):
            if tok in REV_CHAR:
                if tok == "\t":
                    src = tabs.pop(0) if tabs else self.tab_proto
                    el = copy.deepcopy(src) if src is not None else etree.Element(hp("tab"))
                    el.tail = None
                else:
                    el = etree.Element(hp(REV_CHAR[tok]))
                t.append(el)
                last = el
            elif tok:
                if last is None:
                    t.text = (t.text or "") + tok
                else:
                    last.tail = (last.tail or "") + tok

    def _write(self, run, text):
        tabs = [copy.deepcopy(x) for x in run.iter(hp("tab"))]
        for ch in list(run):
            run.remove(ch)
        t = etree.SubElement(run, hp("t"))
        self._fill_t(t, text, tabs)

    def build(self, piece, text, first_use):
        run = piece.proto if first_use else copy.deepcopy(piece.proto)
        self._write(run, text)
        return [run]

    def set_text(self, piece, text):
        self._write(piece.nodes[0], text)
        for n in piece.nodes[1:]:
            self._write(n, "")


def _in_header_footer(p) -> bool:
    return any(anc.tag in (hp("header"), hp("footer")) for anc in p.iterancestors())


HH = "http://www.hancom.co.kr/hwpml/2011/head"
HWPX_HEADER = "Contents/header.xml"


def hh(t: str) -> str:
    return f"{{{HH}}}{t}"


def _needs_plain_twin(ppr) -> bool:
    return (any(h.get("type", "NONE") != "NONE" for h in ppr.iter(hh("heading")))
            or any(b.get("pageBreakBefore") == "1" for b in ppr.iter(hh("breakSetting"))))


def plain_para_pr_map(header_root) -> dict[str, str]:
    """Paragraph shapes with outline / list numbering (개요, 1. 가. …) or 'page break
    before' -> id of the plain twin that add_plain_para_prs() appends."""
    props = header_root.find(f".//{hh('paraProperties')}")
    if props is None:
        return {}
    ids = [int(x.get("id")) for x in props.iterfind(hh("paraPr")) if (x.get("id") or "").isdigit()]
    nxt = max(ids, default=-1) + 1
    out: dict[str, str] = {}
    for ppr in props.iterfind(hh("paraPr")):
        pid = ppr.get("id")
        if pid is not None and pid not in out and _needs_plain_twin(ppr):
            out[pid] = str(nxt)
            nxt += 1
    return out


def add_plain_para_prs(header_root, mapping: dict[str, str]) -> None:
    props = header_root.find(f".//{hh('paraProperties')}")
    if props is None or not mapping:
        return
    for ppr in list(props.iterfind(hh("paraPr"))):
        twin_id = mapping.get(ppr.get("id"))
        if twin_id is None:
            continue
        twin = copy.deepcopy(ppr)
        twin.set("id", twin_id)
        for h in twin.iter(hh("heading")):
            h.set("type", "NONE")
            h.set("idRef", "0")
            h.set("level", "0")
        for b in twin.iter(hh("breakSetting")):
            b.set("pageBreakBefore", "0")
        twin.tail = None
        props.append(twin)
    props.set("itemCnt", str(len(props.findall(hh("paraPr")))))


def hwpx_clone_paragraph(p, para_pr_map: dict[str, str] | None = None):
    """Bilingual copy: text runs only (no section/column definitions, tables,
    pictures, footnotes or other controls, which must stay unique).  The copy
    neither starts a new page/column nor takes an outline/list number."""
    if _in_header_footer(p):
        return None
    c = copy.deepcopy(p)
    c.set("id", "0")
    for attr in ("pageBreak", "columnBreak"):
        if c.get(attr) is not None:
            c.set(attr, "0")
    if para_pr_map and c.get("paraPrIDRef") in para_pr_map:
        c.set("paraPrIDRef", para_pr_map[c.get("paraPrIDRef")])
    for ls in c.findall(hp("linesegarray")):
        c.remove(ls)
    for run in list(c.iterchildren(hp("run"))):
        for ch in list(run):
            if ch.tag != hp("t"):
                run.remove(ch)
        if len(run) == 0:
            c.remove(run)
    for el in list(c):
        if el.tag != hp("run"):
            c.remove(el)
    if not any(t.xpath("string()").strip() for t in c.iter(hp("t"))):
        return None
    return c


def _para_plain_text(p) -> str:
    """Text of one paragraph (not of paragraphs nested in its tables / footnotes)."""
    out = []
    for t in p.iter(hp("t")):
        if next(t.iterancestors(hp("p")), None) is not p:
            continue
        for seg in _t_segments(t):
            if seg[0] == "text":
                out.append(seg[1].replace("\n", " ").replace("\t", " "))
    return "".join(out)


def translate_hwpx(src, dst, translate: Translate, *, bilingual: bool = False, drop_lineseg: bool = True,
                   update_preview: bool = True) -> dict:
    stats: dict = {}
    ad = HwpxAdapter()
    preview: list[str] = []
    para_pr_map: dict[str, str] = {}
    if bilingual:
        header = read_member(src, HWPX_HEADER)
        if header is not None:
            para_pr_map = plain_para_pr_map(parse_xml(header).getroot())

    def clone(p):
        return hwpx_clone_paragraph(p, para_pr_map)

    def tf(name, tree):
        root = tree.getroot()
        if name == HWPX_HEADER:
            add_plain_para_prs(root, para_pr_map)
            return
        paras = list(root.iter(hp("p")))
        before = {id(p): etree.tostring(p) for p in paras} if drop_lineseg and not bilingual else {}
        if bilingual:
            # headers/footers are translated in place (as in DOCX); body paragraphs get a copy
            body = [p for p in paras if not _in_header_footer(p)]
            translate_paragraphs(ad, body, translate, stats, clone=clone)
            translate_paragraphs(ad, [p for p in paras if _in_header_footer(p)], translate, stats)
        else:
            translate_paragraphs(ad, paras, translate, stats)
        if drop_lineseg:
            # stale layout cache: remove it where text changed (bilingual: everywhere,
            # because inserted paragraphs shift every following line); Hancom recomputes
            n = 0
            for p in root.iter(hp("p")):
                if bilingual or (id(p) in before and etree.tostring(p) != before[id(p)]):
                    for ls in p.findall(hp("linesegarray")):
                        p.remove(ls)
                        n += 1
            stats["lineseg_removed"] = stats.get("lineseg_removed", 0) + n
        if dst is not None:
            for p in root.iter(hp("p")):
                s = _para_plain_text(p)
                if s.strip():
                    preview.append(s)

    def select(n):
        return bool(HWPX_SECTIONS.fullmatch(n)) or (n == HWPX_HEADER and bool(para_pr_map))

    stats["parts"] = rewrite_zip(src, dst, select, tf)
    if update_preview and dst is not None and read_member(dst, "Preview/PrvText.txt") is not None:
        replace_member(dst, "Preview/PrvText.txt", "\r\n".join(preview)[:1024].encode("utf-8"))
    return stats


class HwpxHandler(TwoPassHandler):
    def validate(self, ctx: HandlerContext) -> None:
        check_zip(ctx.input_path, ("Contents/header.xml",),
                  wrong_type="형식이 올바르지 않은 파일입니다. 확장자와 내용이 다릅니다.")
        manifest = read_member(ctx.input_path, "META-INF/manifest.xml") or b""
        if b"encryption" in manifest.lower():
            raise DocumentError("암호가 걸린 한글 문서입니다. 암호를 해제한 뒤 다시 올려 주세요.")
        mt = (read_member(ctx.input_path, "mimetype") or b"").strip()
        if mt and mt != b"application/hwp+zip":
            raise DocumentError("형식이 올바르지 않은 파일입니다. 확장자와 내용이 다릅니다.")

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        translate_hwpx(src, dst, translate, bilingual=ctx.output_mode == "bilingual")
