"""HWP 5.0 (binary, OLE2/CFB) -> translated DOCX.

HWP 5.0 cannot be written in place, so the paragraphs are extracted with olefile
and the translation is saved as a Word file (with a warning).

Layout:  FileHeader (256 B: 32 B signature, u32 version, u32 flags)
         DocInfo, BodyText/Section0..N  (raw-deflate if flags & 1)
         ViewText/Section* instead of BodyText for distribution docs (flags & 4, encrypted)
Record header u32: tag = h & 0x3FF, level = (h >> 10) & 0x3FF, size = h >> 20
                   (size == 0xFFF -> next u32 is the real size)
HWPTAG_PARA_TEXT = 67 (UTF-16LE).  Level 1 = body text, deeper = tables/footnotes.
"""
from __future__ import annotations

import asyncio
import struct
import zipfile
import zlib
from pathlib import Path

from translator_app.documents.base import (
    MSG_BROKEN,
    MSG_HWP_AS_DOCX,
    MSG_NO_TEXT,
    DocumentError,
    HandlerContext,
    HandlerResult,
    has_letters,
    preview_from_pairs,
)
from translator_app.documents.docx_writer import DocxWriter
from translator_app.documents.ooxml import lang_tag

HWPTAG_PARA_TEXT = 67

# control characters inside PARA_TEXT (UTF-16 code units 0..31)
CHAR_CTRL = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}            # 1 code unit
INLINE_CTRL = {4, 5, 6, 7, 8, 9, 19, 20}                            # 8 code units
EXTENDED_CTRL = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}   # 8 code units
CHAR_MAP = {9: "\t", 10: "\n", 24: "-", 30: " ", 31: " "}

# A tiny .hwp can hold huge sections of tiny records, so extraction has one shared
# budget for all sections (decompressed bytes, records, paragraphs, text).
MAX_TOTAL_BYTES = 128 * 1024 * 1024            # decompressed BodyText, all sections together
MAX_RECORDS = 4_000_000
MAX_PARAGRAPHS = 200_000
MAX_PARA_BYTES = 2 * 1024 * 1024               # one PARA_TEXT record (≈ 1M characters)

MSG_HWP_PASSWORD = "암호가 걸린 한글 문서입니다. 한글에서 암호를 해제한 뒤 다시 올려 주세요."
MSG_HWP_DISTRIBUTION = "배포용 한글 문서는 내용을 읽을 수 없습니다. 한글에서 일반 문서로 저장해 올려 주세요."
MSG_HWP_IS_HWPX = "HWPX 형식의 파일입니다. 확장자를 .hwpx 로 바꿔 올려 주세요."
MSG_HWP_OLD = "한글 97 이전 형식은 지원하지 않습니다. 한글에서 HWPX로 저장해 올려 주세요."
MSG_HWP_TOO_BIG = "압축을 풀면 너무 큰 파일입니다."


def read_header(ole) -> dict:
    hdr = ole.openstream("FileHeader").read()
    if not hdr.startswith(b"HWP Document File"):
        raise DocumentError(MSG_HWP_OLD)
    ver, flags = struct.unpack_from("<II", hdr, 32)
    return {"version": f"{ver >> 24}.{(ver >> 16) & 0xFF}.{(ver >> 8) & 0xFF}.{ver & 0xFF}",
            "compressed": bool(flags & 1), "password": bool(flags & 2),
            "distribution": bool(flags & 4), "flags": flags}


def iter_records(data: bytes):
    pos, n = 0, len(data)
    while pos + 4 <= n:
        h, = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, h >> 20
        if size == 0xFFF:
            if pos + 4 > n:
                break
            size, = struct.unpack_from("<I", data, pos)
            pos += 4
        yield tag, level, data[pos:pos + size]
        pos += size


def para_text(payload: bytes) -> str:
    out, i = [], 0
    units = len(payload) // 2
    while i < units:
        c, = struct.unpack_from("<H", payload, i * 2)
        if c >= 32:
            out.append(chr(c))
            i += 1
        elif c in CHAR_CTRL:
            if c in CHAR_MAP:
                out.append(CHAR_MAP[c])
            i += 1                                   # 13 = paragraph end -> dropped
        elif c in INLINE_CTRL or c in EXTENDED_CTRL:
            if c == 9:
                out.append("\t")
            i += 8
        else:
            i += 1
    s = "".join(out)
    # surrogate pairs come through as two code units -> recombine
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _inflate(raw: bytes, limit: int = MAX_TOTAL_BYTES) -> bytes:
    d = zlib.decompressobj(-15)
    out = d.decompress(raw, max(1, limit))
    if d.unconsumed_tail or len(out) > limit:
        raise DocumentError(MSG_HWP_TOO_BIG)
    return out


def _too_long(limit: int) -> DocumentError:
    return DocumentError(f"문서가 너무 깁니다. 최대 {limit:,}자까지 번역할 수 있습니다.")


def hwp5_paragraphs(path: str | Path, max_chars: int | None = None) -> list[tuple[int, str]]:
    """[(record level, paragraph text)] in stream order; level > 0 = nested
    (table cells, footnotes, headers, text boxes).

    Stops with DocumentError as soon as the shared budget is used up, or when the
    distinct paragraph text to translate exceeds max_chars."""
    import olefile

    path = str(path)
    if zipfile.is_zipfile(path):
        raise DocumentError(MSG_HWP_IS_HWPX)
    if not olefile.isOleFile(path):
        raise DocumentError(MSG_BROKEN)
    try:
        with olefile.OleFileIO(path) as ole:
            if not ole.exists("FileHeader"):
                raise DocumentError(MSG_BROKEN)
            info = read_header(ole)
            if info["password"]:
                raise DocumentError(MSG_HWP_PASSWORD)
            if info["distribution"]:
                raise DocumentError(MSG_HWP_DISTRIBUTION)
            secs = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"
                           and e[1].startswith("Section") and e[1][7:].isdigit()), key=lambda e: int(e[1][7:]))
            out: list[tuple[int, str]] = []
            budget = MAX_TOTAL_BYTES
            records = 0
            seen: set[str] = set()
            chars = 0
            for e in secs:
                raw = ole.openstream(e).read()
                try:
                    data = _inflate(raw, budget) if info["compressed"] else raw
                except zlib.error as exc:
                    raise DocumentError(MSG_BROKEN) from exc
                budget -= len(data)
                if budget < 0:
                    raise DocumentError(MSG_HWP_TOO_BIG)
                for tag, level, payload in iter_records(data):
                    records += 1
                    if records > MAX_RECORDS:
                        raise DocumentError(MSG_HWP_TOO_BIG)
                    if tag != HWPTAG_PARA_TEXT:
                        continue
                    if len(out) >= MAX_PARAGRAPHS or len(payload) > MAX_PARA_BYTES:
                        raise DocumentError(MSG_HWP_TOO_BIG)
                    text = para_text(payload)
                    out.append((level, text))
                    key = text.strip()
                    if max_chars is not None and key not in seen and has_letters(key):
                        seen.add(key)
                        chars += len(key)
                        if chars > max_chars:
                            raise _too_long(max_chars)
                del data
            return out
    except DocumentError:
        raise
    except (OSError, struct.error, ValueError) as exc:
        raise DocumentError(MSG_BROKEN) from exc


def build_docx(paras: list[tuple[int, str]], table: dict[str, str], *, bilingual: bool, title: str,
               lang: str | None) -> bytes:
    w = DocxWriter(title=title, lang=lang)
    blank = True
    for level, text in paras:
        body = text.rstrip("\r\n")
        indent = 1 if level > 1 else 0
        if not body.strip():
            if not blank:
                w.paragraph("")
                blank = True
            continue
        blank = False
        key = body.strip()
        if bilingual and key in table:
            w.paragraph(body, color="767676", indent_level=indent)
            w.paragraph(table[key], indent_level=indent)
        else:
            w.paragraph(table.get(key, body), indent_level=indent)
    return w.to_bytes()


class HwpHandler:
    output_ext = ".docx"

    async def run(self, ctx: HandlerContext) -> HandlerResult:
        ctx.set_status("extracting")
        paras = await asyncio.to_thread(hwp5_paragraphs, ctx.input_path, ctx.max_chars)
        ctx.check_cancel()
        texts = list(dict.fromkeys(t.strip() for _, t in paras if has_letters(t)))
        if not texts:
            raise DocumentError(MSG_NO_TEXT)
        table = await ctx.translate_strings(texts)
        ctx.check_cancel()
        ctx.set_status("writing")
        out = ctx.output_with_ext(self.output_ext)
        data = await asyncio.to_thread(
            build_docx, paras, table, bilingual=ctx.output_mode == "bilingual",
            title="", lang=lang_tag(ctx.target_lang),
        )
        await asyncio.to_thread(out.write_bytes, data)
        ctx.warn(MSG_HWP_AS_DOCX)
        pairs = [(s, table.get(s, s)) for s in texts]
        return HandlerResult(preview=preview_from_pairs(pairs), pairs=pairs, output_ext=self.output_ext)
