"""Safe zip / XML helpers for OOXML and HWPX packages.

- XML is parsed without entity expansion, DTD loading or network access; parts
  that declare a DTD are refused outright (OOXML/OWPML never need one).
- Zip packages are checked before use: member count, total declared size and
  member names (no absolute paths / ``..``).  ``zipfile`` never yields more than
  the declared size of a member, so the size check bounds memory use.
- An lxml tree needs about 20x the size of its XML in memory, so every parsed
  part is capped in bytes and in elements (a sub-1 MB zip can declare a part of
  hundreds of MB made of tiny empty elements).
- ``rewrite_zip`` keeps member order, names, timestamps and compression type
  (HWPX/ODF ``mimetype`` stays first and STORED) and copies untouched members
  with identical content.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Callable

from lxml import etree

from translator_app.documents.base import MSG_BROKEN, DocumentError

MAX_MEMBERS = 5000
MAX_TOTAL_UNCOMPRESSED = 150 * 1024 * 1024
MAX_XML_PART_BYTES = 48 * 1024 * 1024        # one parsed XML part
MAX_XML_ELEMENTS = 4_000_000                  # ≈ 450 MB of lxml nodes at most
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    remove_blank_text=False,
    strip_cdata=False,
    load_dtd=False,
    dtd_validation=False,
)

MSG_UNSAFE_XML = "허용되지 않는 XML 구조(DTD/외부 엔터티)가 포함된 파일입니다."
MSG_UNSAFE_ZIP = "압축 구조가 안전하지 않은 파일입니다."
MSG_ZIP_TOO_BIG = "압축을 풀면 너무 큰 파일입니다."


def _count_elements(data: bytes, limit: int) -> int:
    """Element count with a streaming parse that frees the nodes it has seen."""
    n = 0
    events = etree.iterparse(io.BytesIO(data), events=("end",), resolve_entities=False, no_network=True,
                             huge_tree=False, load_dtd=False)
    try:
        for _ev, el in events:
            n += 1
            if n > limit:
                break
            el.clear(keep_tail=True)
            parent = el.getparent()
            if parent is not None:
                while el.getprevious() is not None:
                    del parent[0]
    except etree.XMLSyntaxError as exc:
        raise DocumentError(MSG_BROKEN) from exc
    return n


def parse_xml(data: bytes):
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise DocumentError(MSG_UNSAFE_XML)
    if len(data) > MAX_XML_PART_BYTES:
        raise DocumentError(MSG_ZIP_TOO_BIG)
    # every element has at least one "<": only count exactly when that bound is exceeded
    if data.count(b"<") > MAX_XML_ELEMENTS and _count_elements(data, MAX_XML_ELEMENTS) > MAX_XML_ELEMENTS:
        raise DocumentError(MSG_ZIP_TOO_BIG)
    try:
        root = etree.fromstring(data, PARSER)
    except etree.XMLSyntaxError as exc:
        raise DocumentError(MSG_BROKEN) from exc
    tree = root.getroottree()
    if tree.docinfo.internalDTD is not None or tree.docinfo.doctype:
        raise DocumentError(MSG_UNSAFE_XML)
    return tree


def dump_xml(tree) -> bytes:
    di = tree.docinfo
    return etree.tostring(tree, xml_declaration=True, encoding=di.encoding or "UTF-8", standalone=di.standalone)


def _unsafe_name(name: str) -> bool:
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or "\x00" in n:
        return True
    return any(part == ".." for part in n.split("/"))


def check_zip(path: str | os.PathLike, required: tuple[str, ...] = (), *, wrong_type: str | None = None) -> None:
    """Raise DocumentError when the file is not a safe zip package."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head == OLE_MAGIC:
        # encrypted OOXML (Agile/Standard encryption) is an OLE compound file
        raise DocumentError("암호가 걸린 문서입니다. 암호를 해제한 뒤 다시 올려 주세요.")
    if not zipfile.is_zipfile(path):
        raise DocumentError(MSG_BROKEN)
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_MEMBERS:
                raise DocumentError(MSG_UNSAFE_ZIP)
            total = 0
            names = set()
            for info in infos:
                if _unsafe_name(info.filename):
                    raise DocumentError(MSG_UNSAFE_ZIP)
                if info.flag_bits & 0x1:
                    raise DocumentError("암호가 걸린 문서입니다. 암호를 해제한 뒤 다시 올려 주세요.")
                total += info.file_size
                names.add(info.filename)
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise DocumentError(MSG_ZIP_TOO_BIG)
            for req in required:
                if req not in names:
                    raise DocumentError(wrong_type or "형식이 올바르지 않은 파일입니다. 확장자와 내용이 다릅니다.")
    except zipfile.BadZipFile as exc:
        raise DocumentError(MSG_BROKEN) from exc


def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    zi.compress_type = info.compress_type
    zi.external_attr = info.external_attr
    zi.create_system = info.create_system
    zi.comment = info.comment
    return zi


def rewrite_zip(
    src: str | os.PathLike,
    dst: str | os.PathLike | None,
    select: Callable[[str], bool],
    transform: Callable[[str, object], None],
) -> list[str]:
    """Parse every member matching select(), call transform(name, tree) (in place)
    and write the result to dst.  dst=None runs the transforms only (pass 1)."""
    changed: list[str] = []
    with zipfile.ZipFile(src) as zin:
        zout = zipfile.ZipFile(dst, "w") if dst is not None else None
        try:
            for info in zin.infolist():
                selected = select(info.filename)
                if zout is None and not selected:
                    continue
                if selected and info.file_size > MAX_XML_PART_BYTES:
                    raise DocumentError(MSG_ZIP_TOO_BIG)
                data = zin.read(info.filename)
                if selected:
                    tree = parse_xml(data)
                    transform(info.filename, tree)
                    if zout is not None:                # pass 1 only collects: nothing to serialise
                        data = dump_xml(tree)
                    changed.append(info.filename)
                if zout is not None:
                    zout.writestr(_copy_info(info), data)
            if zout is not None:
                zout.comment = zin.comment
        finally:
            if zout is not None:
                zout.close()
    return changed


def read_member(path: str | os.PathLike, name: str) -> bytes | None:
    with zipfile.ZipFile(path) as z:
        try:
            return z.read(name)
        except KeyError:
            return None


def replace_member(path: str | os.PathLike, name: str, data: bytes) -> None:
    """Rewrite one member (order/compression of all members kept)."""
    tmp = f"{os.fspath(path)}.tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for info in zin.infolist():
            d = data if info.filename == name else zin.read(info.filename)
            zout.writestr(_copy_info(info), d)
        zout.comment = zin.comment
    os.replace(tmp, path)
