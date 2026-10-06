"""HTML / HTM translation with lxml.html.

Blocks that contain only inline markup are sent as one string with nested
<gN>..</gN> tags; <br>, <img>, <code> and no-translate spans become <xN/>
placeholders.  Attributes alt/title/placeholder/aria-label, <title> and meta
description/keywords are translated.  script/style/code/pre/svg and elements
marked translate="no" / class="notranslate" are skipped.
"""
from __future__ import annotations

import codecs
import copy
import html as htmllib
import re
from pathlib import Path

from lxml import etree
from lxml import html as lhtml

from translator_app.documents.base import (
    MSG_BROKEN,
    DocumentError,
    HandlerContext,
    Translate,
    TwoPassHandler,
    has_letters,
)
from translator_app.documents.inline_tags import translate_unique
from translator_app.documents.plaintext import guess_legacy_encoding

SKIP = {"script", "style", "code", "pre", "kbd", "samp", "var", "textarea", "svg", "math",
        "noscript", "template", "head"}
INLINE = {"a", "abbr", "b", "bdi", "bdo", "cite", "data", "dfn", "em", "i", "mark", "q", "s",
          "small", "span", "strong", "sub", "sup", "time", "u", "font", "label", "del", "ins"}
ATOMIC_INLINE = {"br", "img", "wbr", "input", "code", "kbd", "samp", "var", "svg", "math", "button", "select"}
ATTRS = ("alt", "title", "placeholder", "aria-label")

# The source is decoded in Python (see decode_html) and always handed to libxml2
# as UTF-8: without a declaration libxml2 would read it as Latin-1, and it does
# not know every legacy label (euc-kr gave an empty document).
HTML_PARSER = lhtml.HTMLParser(no_network=True, remove_comments=False, remove_pis=False, huge_tree=False,
                               encoding="utf-8")

_META_CHARSET = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", re.I)
_XML_DECL_ENCODING = re.compile(rb"^\s*<\?xml\b[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._:-]+)", re.I)
# WHATWG encoding labels -> the Python codec browsers effectively use
_LABELS = {
    "euc-kr": "cp949", "ks_c_5601-1987": "cp949", "ks_c_5601-1989": "cp949", "ksc5601": "cp949",
    "ksc_5601": "cp949", "korean": "cp949", "windows-949": "cp949", "x-windows-949": "cp949", "uhc": "cp949",
    "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "l1": "cp1252", "ascii": "cp1252",
    "us-ascii": "cp1252", "windows-1252": "cp1252",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "chinese": "gb18030", "gb_2312-80": "gb18030",
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "ms_kanji": "cp932",
    "windows-31j": "cp932", "ms932": "cp932",
    "big5": "big5hkscs", "big5-hkscs": "big5hkscs", "cn-big5": "big5hkscs", "x-x-big5": "big5hkscs",
    "utf-16": "utf-8", "utf-16le": "utf-8", "utf-16be": "utf-8", "unicode": "utf-8",
}


def _codec(label: bytes) -> str | None:
    name = label.decode("ascii", "ignore").strip().lower()
    name = _LABELS.get(name, name)
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_html(data: bytes) -> str:
    """BOM, then <meta charset> / http-equiv / XML declaration in the first 4 KB,
    then strict UTF-8, then a legacy-encoding guess (CP949, GBK, Shift-JIS…)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    head = data[:4096]
    m = _META_CHARSET.search(head) or _XML_DECL_ENCODING.match(head)
    enc = _codec(m.group(1)) if m else None
    if enc:
        return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(guess_legacy_encoding(data), errors="replace")


def _no_translate(el) -> bool:
    return (el.get("translate") == "no" or "notranslate" in (el.get("class") or "").split()
            or el.tag in SKIP)


def _is_inline_only(el) -> bool:
    for ch in el:
        if not isinstance(ch.tag, str):
            continue
        if ch.tag in ATOMIC_INLINE or (ch.tag in INLINE and _no_translate(ch)):
            continue                                   # becomes an <xN/> atom
        if ch.tag not in INLINE or _no_translate(ch) or not _is_inline_only(ch):
            return False
    return True


def _encode_block(el):
    """inner HTML -> '<g1>..</g1> <x1/>' string + id map."""
    ids: dict = {}
    cnt = {"g": 0, "x": 0}

    def enc(node) -> str:
        out = htmllib.escape(node.text or "", quote=False)
        for ch in node:
            if not isinstance(ch.tag, str) or ch.tag in ATOMIC_INLINE or _no_translate(ch):
                cnt["x"] += 1
                ids[f"x{cnt['x']}"] = ch
                out += f"<x{cnt['x']}/>"
            else:
                cnt["g"] += 1
                gid = f"g{cnt['g']}"
                ids[gid] = ch
                out += f"<{gid}>{enc(ch)}</{gid}>"
            out += htmllib.escape(ch.tail or "", quote=False)
        return out
    return enc(el), ids


_BARE_AMP = re.compile(r"&(?!(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)")
_FRAG_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False)


def _decode_block(el, out: str, ids: dict) -> bool:
    out = _BARE_AMP.sub("&amp;", out)                 # LLMs often unescape &amp;
    try:
        frag = etree.fromstring(f"<root>{out}</root>".encode(), _FRAG_PARSER)
    except etree.XMLSyntaxError:
        return False
    used_x: list[str] = []

    def build(src, dst):
        dst.text = src.text
        for ch in src:
            if not isinstance(ch.tag, str) or ch.tag not in ids:
                raise KeyError(str(ch.tag))
            orig = ids[ch.tag]
            if ch.tag.startswith("x"):
                new = copy.deepcopy(orig) if ch.tag in used_x else orig
                used_x.append(ch.tag)
            else:
                new = etree.Element(orig.tag, attrib=dict(orig.attrib))
                build(ch, new)
            new.tail = ch.tail
            dst.append(new)
    tmp = etree.Element("tmp")
    try:
        build(frag, tmp)
    except KeyError:
        return False
    for k, orig in ids.items():                       # never drop images / code / <br>
        if k.startswith("x") and k not in used_x:
            orig.tail = None
            tmp.append(orig)
    for ch in list(el):
        el.remove(ch)
    el.text = tmp.text
    for ch in list(tmp):
        el.append(ch)
    return True


def _plain_with_atoms(el, out: str, ids: dict) -> None:
    """Last resort: the translation as plain text, followed by the original images /
    code / <br> atoms in their original order (never dropped)."""
    for ch in list(el):
        el.remove(ch)
    el.text = htmllib.unescape(re.sub(r"</?[gx]\d+\s*/?>", "", out))
    for k, orig in ids.items():
        if k.startswith("x"):
            orig.tail = None
            el.append(orig)


def _fix_charset(doc) -> None:
    """Output is always UTF-8: declare it with <meta charset="utf-8"> (libxml2 drops
    http-equiv Content-Type metas when serialising, and a file without any
    declaration is read as a legacy encoding by browsers)."""
    metas = list(doc.iterfind(".//meta"))
    has_charset = False
    for meta in metas:
        if meta.get("charset") is not None:
            meta.set("charset", "utf-8")
            has_charset = True
    for meta in metas:
        if (meta.get("http-equiv") or "").lower() == "content-type":
            if has_charset:
                meta.getparent().remove(meta)
            else:
                for at in ("http-equiv", "content"):
                    meta.attrib.pop(at, None)
                meta.set("charset", "utf-8")
                has_charset = True
    if has_charset or doc.tag != "html":
        return
    head = doc.find("head")
    if head is None:
        head = doc.makeelement("head", {})
        doc.insert(0, head)
    head.insert(0, doc.makeelement("meta", {"charset": "utf-8"}))


def translate_html(data: bytes, translate: Translate, target_lang: str = "ko") -> bytes:
    text = decode_html(data)
    try:
        doc = lhtml.document_fromstring(text.encode("utf-8"), parser=HTML_PARSER)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
        raise DocumentError(MSG_BROKEN) from exc
    blocks, plain = [], []          # plain: (element, "text"|"tail"|"@attr")

    def visit(el):
        if not isinstance(el.tag, str) or _no_translate(el):
            return
        for at in ATTRS:
            if has_letters(el.get(at) or ""):
                plain.append((el, "@" + at))
        if el.tag not in INLINE and _is_inline_only(el):
            if has_letters(el.text_content()):
                blocks.append(el)
            for ch in el.iter():
                if isinstance(ch.tag, str) and ch is not el:
                    for at in ATTRS:
                        if has_letters(ch.get(at) or ""):
                            plain.append((ch, "@" + at))
            return
        if has_letters(el.text or ""):
            plain.append((el, "text"))
        for ch in el:
            visit(ch)
            if has_letters(ch.tail or ""):
                plain.append((ch, "tail"))

    visit(doc.body if doc.find("body") is not None else doc)
    title = doc.find(".//title")
    if title is not None and has_letters(title.text or ""):
        plain.append((title, "text"))
    for meta in doc.iterfind(".//meta"):
        if (meta.get("name") or "").lower() in ("description", "keywords") and has_letters(meta.get("content") or ""):
            plain.append((meta, "@content"))

    encs = []
    for b in blocks:
        s, ids = _encode_block(b)
        if not ids:                                   # no inline markup: send plain text
            s = htmllib.unescape(s)
        encs.append((s, ids))

    def get(el, where):
        return el.get(where[1:]) if where.startswith("@") else getattr(el, where)
    srcs = [s.strip() for s, _ in encs] + [get(el, wh).strip() for el, wh in plain]
    table = translate_unique(srcs, translate)
    for b, (s, ids) in zip(blocks, encs):
        lead, trail = s[:len(s) - len(s.lstrip())], s[len(s.rstrip()):]
        out = lead + table[s.strip()] + trail
        if not ids:
            for ch in list(b):                       # only comments could be here
                b.remove(ch)
            b.text = out
        elif not _decode_block(b, out, ids):
            # drop inline formatting but keep atoms (also when the model wrote <x1> or
            # <x1></x1> for <x1/>); finally plain text followed by the atoms
            no_g = re.sub(r"</?g\d+>", "", out)
            if (not _decode_block(b, no_g, ids)
                    and not _decode_block(b, re.sub(r"<(x\d+)\s*/?>", r"<\1/>", re.sub(r"</x\d+>", "", no_g)), ids)):
                _plain_with_atoms(b, out, ids)
    for el, wh in plain:
        orig = get(el, wh)
        new = orig[:len(orig) - len(orig.lstrip())] + table[orig.strip()] + orig[len(orig.rstrip()):]
        if wh.startswith("@"):
            el.set(wh[1:], new)
        else:
            setattr(el, wh, new)
    html_el = doc.getroottree().getroot()
    if html_el.get("lang"):
        html_el.set("lang", target_lang or "ko")
    _fix_charset(doc)
    if "<!doctype" not in text[:1024].lower():       # libxml2 would invent an HTML 4.0 doctype
        return lhtml.tostring(doc, encoding="utf-8")
    return lhtml.tostring(doc.getroottree(), encoding="utf-8", doctype=doc.getroottree().docinfo.doctype)


class HtmlHandler(TwoPassHandler):
    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        out = translate_html(src.read_bytes(), translate, ctx.target_lang)
        if dst is not None:
            dst.write_bytes(out)
