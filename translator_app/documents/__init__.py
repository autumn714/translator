"""Document translation: format registry.

Importing this package is cheap (no lxml / PyMuPDF import); handler modules are
loaded on first use.  ``supported_formats()`` is the ``document_formats`` list of
``GET /api/status``.
"""
from __future__ import annotations

import importlib
import importlib.util
from typing import Any

# ext -> (label, output ext, layout kept, bilingual output possible, handler module, class, runtime deps)
_FORMATS: list[tuple[str, str, str, bool, bool, str, str, tuple[str, ...]]] = [
    (".docx", "Word", ".docx", True, True, "ooxml", "DocxHandler", ("lxml",)),
    (".pptx", "PowerPoint", ".pptx", True, False, "ooxml", "PptxHandler", ("lxml",)),
    (".xlsx", "Excel", ".xlsx", True, False, "ooxml", "XlsxHandler", ("lxml",)),
    (".hwpx", "한글", ".hwpx", True, True, "hwpx", "HwpxHandler", ("lxml",)),
    (".hwp", "한글(구버전)", ".docx", False, True, "hwp", "HwpHandler", ("olefile", "lxml")),
    (".pdf", "PDF", ".pdf", True, False, "pdf", "PdfHandler", ("pymupdf", "lxml")),
    (".txt", "텍스트", ".txt", True, True, "plaintext", "PlainTextHandler", ()),
    (".md", "Markdown", ".md", True, True, "plaintext", "PlainTextHandler", ()),
    (".html", "HTML", ".html", True, False, "html", "HtmlHandler", ("lxml",)),
    (".htm", "HTML", ".htm", True, False, "html", "HtmlHandler", ("lxml",)),
    (".srt", "자막", ".srt", True, False, "plaintext", "PlainTextHandler", ()),
    (".vtt", "자막", ".vtt", True, False, "plaintext", "PlainTextHandler", ()),
    (".png", "이미지", ".docx", False, False, "image", "ImageHandler", ("PIL", "lxml")),
    (".jpg", "이미지", ".docx", False, False, "image", "ImageHandler", ("PIL", "lxml")),
    (".jpeg", "이미지", ".docx", False, False, "image", "ImageHandler", ("PIL", "lxml")),
    (".webp", "이미지", ".docx", False, False, "image", "ImageHandler", ("PIL", "lxml")),
]
_BY_EXT = {f[0]: f for f in _FORMATS}
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})

# old / unsupported formats -> Korean hint
_REJECT = {
    ".doc": "Word 97-2003 문서(.doc)는 지원하지 않습니다. Word에서 .docx 로 저장해 올려 주세요.",
    ".dot": "Word 서식 파일은 지원하지 않습니다. Word에서 .docx 로 저장해 올려 주세요.",
    ".dotx": "Word 서식 파일은 지원하지 않습니다. Word에서 .docx 로 저장해 올려 주세요.",
    ".docm": "매크로 포함 문서는 지원하지 않습니다. Word에서 .docx 로 저장해 올려 주세요.",
    ".rtf": "RTF 문서는 지원하지 않습니다. Word에서 .docx 로 저장해 올려 주세요.",
    ".odt": "ODF 문서는 지원하지 않습니다. .docx 로 저장해 올려 주세요.",
    ".ppt": "PowerPoint 97-2003 파일(.ppt)은 지원하지 않습니다. PowerPoint에서 .pptx 로 저장해 올려 주세요.",
    ".pps": "PowerPoint 97-2003 파일은 지원하지 않습니다. PowerPoint에서 .pptx 로 저장해 올려 주세요.",
    ".ppsx": "슬라이드 쇼 파일은 지원하지 않습니다. PowerPoint에서 .pptx 로 저장해 올려 주세요.",
    ".pptm": "매크로 포함 파일은 지원하지 않습니다. PowerPoint에서 .pptx 로 저장해 올려 주세요.",
    ".odp": "ODF 프레젠테이션은 지원하지 않습니다. .pptx 로 저장해 올려 주세요.",
    ".xls": "Excel 97-2003 파일(.xls)은 지원하지 않습니다. Excel에서 .xlsx 로 저장해 올려 주세요.",
    ".xlsm": "매크로 포함 통합 문서는 지원하지 않습니다. Excel에서 .xlsx 로 저장해 올려 주세요.",
    ".xlsb": "Excel 바이너리 통합 문서는 지원하지 않습니다. Excel에서 .xlsx 로 저장해 올려 주세요.",
    ".ods": "ODF 스프레드시트는 지원하지 않습니다. .xlsx 로 저장해 올려 주세요.",
    ".csv": "CSV 파일은 지원하지 않습니다. Excel에서 .xlsx 로 저장해 올려 주세요.",
    ".hwt": "한글 서식 파일(.hwt)은 지원하지 않습니다. 한글에서 .hwpx 로 저장해 올려 주세요.",
    ".hwtx": "한글 서식 파일은 지원하지 않습니다. 한글에서 .hwpx 로 저장해 올려 주세요.",
    ".hml": "한글 HML 파일은 지원하지 않습니다. 한글에서 .hwpx 로 저장해 올려 주세요.",
    ".gif": "GIF 이미지는 지원하지 않습니다. PNG 또는 JPG로 저장해 올려 주세요.",
    ".bmp": "BMP 이미지는 지원하지 않습니다. PNG 또는 JPG로 저장해 올려 주세요.",
    ".tif": "TIFF 이미지는 지원하지 않습니다. PDF, PNG 또는 JPG로 저장해 올려 주세요.",
    ".tiff": "TIFF 이미지는 지원하지 않습니다. PDF, PNG 또는 JPG로 저장해 올려 주세요.",
    ".heic": "HEIC 이미지는 지원하지 않습니다. JPG로 저장해 올려 주세요.",
}
BILINGUAL_EXTS = frozenset(f[0] for f in _FORMATS if f[4])


def _available(deps: tuple[str, ...]) -> bool:
    return all(importlib.util.find_spec(d) is not None for d in deps)


def supported_formats(*, vision: bool = True) -> list[dict[str, Any]]:
    """[{"ext", "label", "output", "layout", "bilingual"}] for formats whose libraries are installed.
    vision=False drops the image formats (the model cannot read images)."""
    out = []
    for ext, label, output, layout, bilingual, _mod, _cls, deps in _FORMATS:
        if not vision and ext in IMAGE_EXTS:
            continue
        if _available(deps):
            out.append({"ext": ext, "label": label, "output": output, "layout": layout, "bilingual": bilingual})
    return out


def is_supported(ext: str) -> bool:
    return ext.lower() in _BY_EXT


def unsupported_message(ext: str) -> str:
    ext = (ext or "").lower()
    if ext in _REJECT:
        return _REJECT[ext]
    names = ", ".join(f[0] for f in _FORMATS)
    return f"지원하지 않는 파일 형식입니다. 지원 형식: {names}"


def output_ext(ext: str, *, pdf_mode: str = "layout") -> str:
    ext = ext.lower()
    if ext == ".pdf" and pdf_mode == "docx":
        return ".docx"
    return _BY_EXT[ext][2]


def supports_bilingual(ext: str) -> bool:
    return ext.lower() in BILINGUAL_EXTS


def get_handler(ext: str):
    """New handler instance for one job (handlers may keep per-job state)."""
    ext = ext.lower()
    if ext not in _BY_EXT:
        from translator_app.documents.base import DocumentError

        raise DocumentError(unsupported_message(ext), status_code=415)
    _e, _l, _o, _lay, _bi, mod, cls, _deps = _BY_EXT[ext]
    module = importlib.import_module(f"translator_app.documents.{mod}")
    return getattr(module, cls)()


__all__ = [
    "BILINGUAL_EXTS", "IMAGE_EXTS", "get_handler", "is_supported", "output_ext", "supported_formats",
    "supports_bilingual", "unsupported_message",
]
