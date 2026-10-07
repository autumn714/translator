"""Minimal DOCX builder (zipfile + string templates, no python-docx).

Used for outputs that cannot keep the source format: HWP 5.0, images, PDF in
"docx" mode.  Supports headings, paragraphs (bold/italic/colour runs, tabs and
line breaks), bullets, simple tables, page breaks and inline PNG/JPEG images.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from translator_app.documents import mdlite

EMU_PER_INCH = 914400
TEXT_WIDTH_IN = 6.27            # A4 with 1-inch margins
MAX_IMAGE_HEIGHT_IN = 8.5

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def _x(s: str) -> str:
    return escape(_BAD_XML.sub("", s))


NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="맑은 고딕" w:cs="Calibri"/><w:sz w:val="21"/><w:szCs w:val="21"/><w:lang w:val="en-US" w:eastAsia="ko-KR" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/><w:szCs w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="23"/><w:szCs w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:ind w:left="567"/></w:pPr><w:rPr><w:color w:val="555555"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:qFormat/><w:rPr><w:color w:val="767676"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr></w:style>
<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:space="0" w:color="A0A0A0"/><w:left w:val="single" w:sz="4" w:space="0" w:color="A0A0A0"/><w:bottom w:val="single" w:sz="4" w:space="0" w:color="A0A0A0"/><w:right w:val="single" w:sz="4" w:space="0" w:color="A0A0A0"/><w:insideH w:val="single" w:sz="4" w:space="0" w:color="A0A0A0"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="A0A0A0"/></w:tblBorders></w:tblPr></w:style>
</w:styles>"""

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>번역기</Application></Properties>"""

SECT = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
        'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')


class DocxWriter:
    def __init__(self, title: str = "", lang: str | None = None) -> None:
        self.title = title
        self.lang = lang
        self.body: list[str] = []
        self.media: list[tuple[str, bytes]] = []       # (name in word/media, data)
        self._pic_id = 0

    # ------------------------------------------------------------ runs
    def _run(self, text: str, *, bold: bool = False, italic: bool = False, color: str | None = None,
             size_pt: float | None = None, mono: bool = False) -> str:
        props = []
        if mono:
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        if bold:
            props.append("<w:b/><w:bCs/>")
        if italic:
            props.append("<w:i/><w:iCs/>")
        if color:
            props.append(f'<w:color w:val="{color}"/>')
        if size_pt:
            hp = int(round(size_pt * 2))
            props.append(f'<w:sz w:val="{hp}"/><w:szCs w:val="{hp}"/>')
        if self.lang:
            props.append(f'<w:lang w:val="{_x(self.lang)}" w:eastAsia="{_x(self.lang)}"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        parts = []
        for tok in re.split(r"([\t\n])", text):
            if tok == "\t":
                parts.append("<w:tab/>")
            elif tok == "\n":
                parts.append("<w:br/>")
            elif tok:
                parts.append(f'<w:t xml:space="preserve">{_x(tok)}</w:t>')
        return f"<w:r>{rpr}{''.join(parts)}</w:r>" if parts else ""

    def _md_runs(self, text: str, **kw) -> str:
        out = []
        for t, st in mdlite.inline_runs(text):
            out.append(self._run(t, bold=kw.get("bold", False) or st == "b",
                                 italic=kw.get("italic", False) or st == "i",
                                 color=kw.get("color"), size_pt=kw.get("size_pt"), mono=st == "code"))
        return "".join(out)

    def _p(self, runs: str, style: str | None = None, indent_twips: int = 0, extra_ppr: str = "") -> None:
        ppr = ""
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        ppr += extra_ppr
        if indent_twips:
            ppr += f'<w:ind w:left="{indent_twips}"/>'
        self.body.append(f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{runs}</w:p>")

    # ------------------------------------------------------------ blocks
    def heading(self, text: str, level: int = 1) -> None:
        style = "Title" if level <= 0 else f"Heading{min(level, 3)}"
        self._p(self._md_runs(text), style)

    def paragraph(self, text: str, *, bold: bool = False, italic: bool = False, color: str | None = None,
                  size_pt: float | None = None, style: str | None = None, indent_level: int = 0,
                  markdown: bool = False) -> None:
        kw = {"bold": bold, "italic": italic, "color": color, "size_pt": size_pt}
        runs = self._md_runs(text, **kw) if markdown else self._run(text, **kw)
        self._p(runs, style, indent_twips=360 * indent_level)

    def bullet(self, text: str, level: int = 0, mark: str = "•") -> None:
        left = 360 + 360 * level
        runs = self._run(mark + "\t") + self._md_runs(text)
        self._p(runs, extra_ppr=f'<w:tabs><w:tab w:val="left" w:pos="{left}"/></w:tabs>'
                                f'<w:ind w:left="{left}" w:hanging="283"/>')

    def table(self, rows: list[list[str]], header: bool = True) -> None:
        if not rows:
            return
        ncol = max(len(r) for r in rows)
        width = int(TEXT_WIDTH_IN * 1440)
        cw = width // max(ncol, 1)
        grid = "".join(f'<w:gridCol w:w="{cw}"/>' for _ in range(ncol))
        trs = []
        for ri, r in enumerate(rows):
            tcs = []
            for ci in range(ncol):
                text = r[ci] if ci < len(r) else ""
                runs = self._md_runs(text, bold=header and ri == 0)
                tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{cw}" w:type="dxa"/></w:tcPr>'
                           f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{runs}</w:p></w:tc>')
            trh = "<w:trPr><w:tblHeader/></w:trPr>" if header and ri == 0 else ""
            trs.append(f"<w:tr>{trh}{''.join(tcs)}</w:tr>")
        self.body.append(
            f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="{width}" w:type="dxa"/>'
            f'<w:tblLayout w:type="fixed"/><w:tblLook w:val="04A0"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
            f"{''.join(trs)}</w:tbl>"
        )
        self.body.append('<w:p><w:pPr><w:spacing w:after="0"/></w:pPr></w:p>')   # a table needs a following paragraph

    def page_break(self) -> None:
        self.body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    def image(self, data: bytes, ext: str, width_px: int, height_px: int, dpi: float = 96.0) -> None:
        """ext: "png" or "jpeg"."""
        self._pic_id += 1
        n = self._pic_id
        name = f"image{n}.{ext}"
        self.media.append((name, data))
        w_in = width_px / dpi
        h_in = height_px / dpi
        scale = min(1.0, TEXT_WIDTH_IN / w_in if w_in else 1.0, MAX_IMAGE_HEIGHT_IN / h_in if h_in else 1.0)
        cx, cy = int(w_in * scale * EMU_PER_INCH), int(h_in * scale * EMU_PER_INCH)
        rid = f"rIdImg{n}"
        self.body.append(
            '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
            f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{n}" name="Picture {n}"/>'
            '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
            "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        )

    def markdown(self, md: str) -> None:
        for b in mdlite.parse(md):
            if b.kind == "heading":
                self.heading(b.text, b.level)
            elif b.kind == "para":
                self.paragraph(b.text, markdown=True)
            elif b.kind == "bullet":
                self.bullet(b.text, b.level)
            elif b.kind == "numbered":
                self.paragraph(b.text, markdown=True, indent_level=b.level + 1)
            elif b.kind == "quote":
                self.paragraph(b.text, style="Quote", markdown=True)
            elif b.kind == "code":
                for line in b.text.split("\n"):
                    self.paragraph(line, style="Code")
            elif b.kind == "hr":
                self.body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                                 'w:color="A0A0A0"/></w:pBdr></w:pPr></w:p>')
            elif b.kind == "table":
                self.table(b.rows)

    # ------------------------------------------------------------ output
    def to_bytes(self) -> bytes:
        rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                'Target="styles.xml"/>']
        for i, (name, _) in enumerate(self.media, 1):
            rels.append(f'<Relationship Id="rIdImg{i}" Type="http://schemas.openxmlformats.org/officeDocument/'
                        f'2006/relationships/image" Target="media/{name}"/>')
        rels.append("</Relationships>")
        body = "".join(self.body) or "<w:p/>"
        document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {NS}><w:body>'
                    f"{body}{SECT}</w:body></w:document>")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f"<dc:title>{_x(self.title)}</dc:title><dc:creator>번역기</dc:creator>"
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", CONTENT_TYPES)
            z.writestr("_rels/.rels", ROOT_RELS)
            z.writestr("docProps/app.xml", APP)
            z.writestr("docProps/core.xml", core)
            z.writestr("word/document.xml", document)
            z.writestr("word/styles.xml", STYLES)
            z.writestr("word/_rels/document.xml.rels", "".join(rels))
            for name, data in self.media:
                z.writestr(f"word/media/{name}", data, compress_type=zipfile.ZIP_STORED)
        return buf.getvalue()

    def save(self, path) -> None:
        with open(path, "wb") as fh:
            fh.write(self.to_bytes())
