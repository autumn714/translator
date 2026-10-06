"""Run-level inline-tag engine shared by the DOCX / PPTX / XLSX / HWPX recipes.

A paragraph is described as a list of Pieces:
  - text piece : formatted text (key = canonical formatting, proto = element to clone)
  - atom piece : something that must not be translated (field, image, footnote ref...)
encode() merges adjacent text pieces with equal key, picks the "base" key (most
characters) whose text stays untagged, wraps the other groups in <gN>..</gN> and
atoms in <xN/>, then decode() parses the translated string back into an ordered
list of (slot, text) / (atom) operations.  If the output is not trustworthy,
decode() returns None and the caller uses the fallback (all text into the first
text piece, empty the others, keep atoms in place).
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from translator_app.documents.base import Translate, has_letters, strip_tags

__all__ = [
    "Adapter", "Encoded", "Piece", "Slot", "apply_ops", "decode", "encode", "fallback",
    "has_letters", "merge", "strip_tags", "translate_paragraphs", "translate_unique",
]


@dataclass
class Piece:
    kind: str                      # "text" | "atom"
    key: object = None             # formatting key (text only)
    text: str = ""                 # \t and \n are allowed (tabs / line breaks)
    nodes: list = field(default_factory=list)   # original top-level nodes (removed on rebuild)
    proto: object = None           # element used as template for new text nodes


@dataclass
class Slot:
    kind: str                      # "base" | "g" | "x"
    piece: Piece


@dataclass
class Encoded:
    src: str                       # string sent to the translator
    slots: dict                    # "g1"/"x1"/"base" -> Slot
    order_x: list                  # atom ids in original order
    pieces: list                   # merged pieces in document order
    tagged: bool


def merge(pieces: list[Piece]) -> list[Piece]:
    out: list[Piece] = []
    for p in pieces:
        if out and p.kind == "text" and out[-1].kind == "text" and out[-1].key == p.key:
            out[-1].text += p.text
            out[-1].nodes += p.nodes
        else:
            out.append(Piece(p.kind, p.key, p.text, list(p.nodes), p.proto))
    return out


def encode(pieces: list[Piece]) -> Encoded | None:
    """Return None when there is nothing to translate (no letters)."""
    ps = merge(pieces)
    texts = [p for p in ps if p.kind == "text"]
    if not texts or not has_letters("".join(p.text for p in texts)):
        return None
    # leading / trailing atoms (section props, footnote marks, anchors...) stay where
    # they are and are not shown to the translator
    i0 = next(i for i, p in enumerate(ps) if p.kind == "text")
    i1 = max(i for i, p in enumerate(ps) if p.kind == "text")
    ps = ps[i0:i1 + 1]
    weight: dict = {}
    for p in texts:
        weight[p.key] = weight.get(p.key, 0) + len(p.text)
    base_key = max(weight, key=weight.get)
    atoms = [p for p in ps if p.kind == "atom"]
    non_base = [p for p in texts if p.key != base_key]
    slots = {"base": Slot("base", next(p for p in texts if p.key == base_key))}
    if not atoms and not non_base:
        return Encoded("".join(p.text for p in texts), slots, [], ps, False)
    parts, gi, xi, order_x = [], 0, 0, []
    for p in ps:
        if p.kind == "atom":
            xi += 1
            sid = f"x{xi}"
            slots[sid] = Slot("x", p)
            order_x.append(sid)
            parts.append(f"<{sid}/>")
        elif p.key == base_key:
            parts.append(html.escape(p.text, quote=False))
        else:
            gi += 1
            sid = f"g{gi}"
            slots[sid] = Slot("g", p)
            parts.append(f"<{sid}>{html.escape(p.text, quote=False)}</{sid}>")
    return Encoded("".join(parts), slots, order_x, ps, True)


_TOK = re.compile(r"<(/?)([gx]\d+)(/?)>")


def decode(enc: Encoded, out: str):
    """-> list of ("text", Slot, str) / ("atom", Slot) or None if malformed."""
    if not enc.tagged:
        return [("text", enc.slots["base"], out)]
    ops, pos, open_id, seen_x = [], 0, None, []
    buf = ""
    for m in _TOK.finditer(out):
        buf += out[pos:m.start()]
        pos = m.end()
        closing, sid, selfclose = m.group(1), m.group(2), m.group(3)
        if sid not in enc.slots:
            return None
        if sid.startswith("x"):
            if closing or not selfclose:
                return None
            if buf:
                ops.append(("text", enc.slots[open_id or "base"], html.unescape(buf)))
                buf = ""
            ops.append(("atom", enc.slots[sid]))
            seen_x.append(sid)
            continue
        if selfclose:
            return None
        if not closing:
            if open_id:            # nested tags are not produced by encode()
                return None
            if buf:
                ops.append(("text", enc.slots["base"], html.unescape(buf)))
                buf = ""
            open_id = sid
        else:
            if open_id != sid:
                return None
            ops.append(("text", enc.slots[sid], html.unescape(buf)))
            buf, open_id = "", None
    if open_id:
        return None
    buf += out[pos:]
    if buf:
        ops.append(("text", enc.slots["base"], html.unescape(buf)))
    if seen_x != enc.order_x:      # atoms missing / duplicated / reordered -> unsafe
        return None
    return ops


def translate_unique(srcs: list[str], translate: Translate, batch: int = 64) -> dict[str, str]:
    """Translate distinct strings once (DOCX textbox fallbacks, repeated cells...)."""
    uniq = list(dict.fromkeys(srcs))
    res: dict[str, str] = {}
    for i in range(0, len(uniq), batch):
        chunk = uniq[i:i + batch]
        outs = translate(chunk)
        if len(outs) != len(chunk):
            raise ValueError("translator returned wrong number of items")
        res.update(zip(chunk, outs))
    return res


# ---------------------------------------------------------------- driver
class Adapter:
    """Format-specific glue. Subclasses implement these three methods."""

    def pieces(self, para) -> list[Piece]:
        raise NotImplementedError

    def build(self, piece: Piece, text: str, first_use: bool) -> list:
        """Return new top-level nodes rendering `text` with piece's formatting."""
        raise NotImplementedError

    def set_text(self, piece: Piece, text: str) -> None:
        """Fallback: write text into piece in place (first node), blank the others."""
        raise NotImplementedError


def apply_ops(adapter: Adapter, para, enc: Encoded, ops) -> None:
    old_nodes = [n for p in enc.pieces for n in p.nodes]
    if not old_nodes:
        return
    parent = old_nodes[0].getparent()
    if any(n.getparent() is not parent for n in old_nodes):
        raise ValueError("pieces must share one parent")
    idx = parent.index(old_nodes[0])
    used: set = set()
    new_nodes = []
    for op in ops:
        if op[0] == "atom":
            new_nodes += op[1].piece.nodes
        else:
            slot, text = op[1], op[2]
            if text == "":
                continue
            first = id(slot.piece) not in used
            used.add(id(slot.piece))
            new_nodes += adapter.build(slot.piece, text, first)
    for n in old_nodes:
        if n.getparent() is parent:
            parent.remove(n)
    for k, n in enumerate(new_nodes):
        parent.insert(idx + k, n)


def fallback(adapter: Adapter, enc: Encoded, out: str) -> None:
    texts = [p for p in enc.pieces if p.kind == "text"]
    plain = strip_tags(out) if enc.tagged else out
    adapter.set_text(texts[0], plain)
    for p in texts[1:]:
        adapter.set_text(p, "")


def translate_paragraphs(
    adapter: Adapter,
    paras: Iterable,
    translate: Translate,
    stats: dict | None = None,
    *,
    clone: Callable[[object], object | None] | None = None,
) -> dict:
    """Translate paragraphs in place.

    clone (bilingual output): called with each paragraph, returns a detached copy
    (or None to skip).  The copy is translated and inserted right after the
    original, which stays untouched."""
    stats = stats if stats is not None else {}
    jobs = []
    for para in paras:
        target = para
        if clone is not None:
            target = clone(para)
            if target is None:
                continue
        enc = encode(adapter.pieces(target))
        if enc:
            if clone is not None:
                para.addnext(target)
            jobs.append((target, enc))
    table = translate_unique([e.src for _, e in jobs], translate) if jobs else {}
    for para, enc in jobs:
        out = table[enc.src]
        ops = decode(enc, out)
        if ops is None:
            fallback(adapter, enc, out)
            stats["fallback"] = stats.get("fallback", 0) + 1
        else:
            apply_ops(adapter, para, enc, ops)
            key = "tagged" if enc.tagged else "plain"
            stats[key] = stats.get(key, 0) + 1
    return stats
