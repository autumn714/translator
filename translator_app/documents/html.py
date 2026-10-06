"""HTML / HTM translation with lxml.html.

Blocks that contain only inline markup are sent as one string with nested
<gN>..</gN> tags; <br>, <img>, <code> and no-translate spans become <xN/>
placeholders.  Attributes alt/title/placeholder/aria-label, <title> and meta
description/keywords are translated.  script/style/code/pre/svg and elements
marked translate="no" / class="notranslate" are skipped.
"""
from __future__ import annotations

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

SKIP = {"script", "style", "code", "pre", "kbd", "samp", "var", "textarea", "svg", "math",
        "noscript", "template", "head"}
INLINE = {"a", "abbr", "b", "bdi", "bdo", "cite", "data", "dfn", "em", "i", "mark", "q", "s",
          "small", "span", "strong", "sub", "sup", "time", "u", "font", "label", "del", "ins"}
ATOMIC_INLINE = {"br", "img", "wbr", "input", "code", "kbd", "samp", "var", "svg", "math", "button", "select"}
ATTRS = ("alt", "title", "placeholder", "aria-label")

HTML_PARSER = lhtml.HTMLParser(no_network=True, remove_comments=False, remove_pis=False, huge_tree=False)


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


def _fix_charset(doc) -> None:
    """Output is always UTF-8: rewrite a declared legacy charset."""
    for meta in doc.iterfind(".//meta"):
        if meta.get("charset"):
            meta.set("charset", "utf-8")
        elif (meta.get("http-equiv") or "").lower() == "content-type":
            meta.set("content", "text/html; charset=utf-8")


def translate_html(data: bytes, translate: Translate, target_lang: str = "ko") -> bytes:
    try:
        doc = lhtml.document_fromstring(data, parser=HTML_PARSER)
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
            # drop inline formatting but keep atoms; finally plain text
            if not _decode_block(b, re.sub(r"</?g\d+>", "", out), ids):
                for ch in list(b):
                    b.remove(ch)
                b.text = htmllib.unescape(re.sub(r"</?[gx]\d+/?>", "", out))
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
    if b"<!doctype" not in data[:1024].lower():      # libxml2 would invent an HTML 4.0 doctype
        return lhtml.tostring(doc, encoding="utf-8")
    return lhtml.tostring(doc.getroottree(), encoding="utf-8", doctype=doc.getroottree().docinfo.doctype)


class HtmlHandler(TwoPassHandler):
    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        out = translate_html(src.read_bytes(), translate, ctx.target_lang)
        if dst is not None:
            dst.write_bytes(out)
