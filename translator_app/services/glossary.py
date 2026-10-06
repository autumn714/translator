from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from functools import lru_cache
from json import JSONDecodeError
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import ValidationError

from translator_app.languages import LANGUAGE_MAP
from translator_app.schemas import GlossaryDocument, GlossaryEntry, GlossarySummary


class GlossaryStore:
    DEFAULT_GLOSSARY_NAME = "기본 용어집"

    def __init__(self, path: Path) -> None:
        self._lock = Lock()
        self._default_path = path
        self._root = path.parent if path.suffix else path
        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        with self._lock:
            if self._default_path.exists():
                # never rewrite a valid existing file (the seed file is tracked in git)
                if self._load_document_or_none(self._default_path) is not None:
                    return

            existing_docs = self._load_documents_locked()
            if existing_docs:
                return

            self._write_default_document_locked()

    def list_glossaries(self) -> list[GlossarySummary]:
        with self._lock:
            docs = self._load_documents_locked()
            if not docs:
                self._write_default_document_locked()
                docs = [self._coerce_document(self._default_path)]

        docs.sort(key=lambda item: (item.id != "default", item.name.lower(), item.id))
        return [
            GlossarySummary(id=doc.id, name=doc.name, entry_count=len(doc.entries)) for doc in docs
        ]

    def load_glossary(self, glossary_id: str) -> GlossaryDocument | None:
        path = self._path_for_id(glossary_id)
        if not path.exists():
            return None
        with self._lock:
            document = self._load_document_or_none(path)
            if document is not None:
                return document
            if glossary_id == "default":
                self._write_default_document_locked()
                return self._coerce_document(self._default_path)
            return None

    def create_glossary(
        self,
        *,
        name: str,
        entries: list[GlossaryEntry],
    ) -> GlossaryDocument:
        glossary_id = f"glossary-{uuid4().hex[:12]}"
        document = GlossaryDocument(id=glossary_id, name=name, entries=entries)
        with self._lock:
            self._write_document(self._path_for_id(glossary_id), document)
        return document

    def save_glossary(self, document: GlossaryDocument) -> GlossaryDocument:
        with self._lock:
            self._write_document(self._path_for_id(document.id), document)
        return document

    def delete_glossary(self, glossary_id: str) -> bool:
        path = self._path_for_id(glossary_id)
        with self._lock:
            if not path.exists():
                return False

            path.unlink()

            if not any(self._root.glob("*.json")):
                self._write_document(
                    self._default_path,
                    GlossaryDocument(id="default", name=self.DEFAULT_GLOSSARY_NAME, entries=[]),
                )

        return True

    def default_glossary_id(self) -> str | None:
        summaries = self.list_glossaries()
        return summaries[0].id if summaries else None

    def _path_for_id(self, glossary_id: str) -> Path:
        safe_id = "".join(ch for ch in glossary_id if ch.isalnum() or ch in {"-", "_"})
        if not safe_id:
            raise ValueError("Invalid glossary id")
        return self._root / f"{safe_id}.json"

    def _coerce_document(self, path: Path) -> GlossaryDocument:
        raw = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(raw, list):
            return GlossaryDocument(
                id=path.stem,
                name=self.DEFAULT_GLOSSARY_NAME if path.stem == "default" else path.stem,
                entries=[
                    GlossaryEntry.model_validate(
                        {
                            **item,
                            "source_lang": _normalize_language_code(item.get("source_lang", "en")),
                            "target_lang": _normalize_language_code(item.get("target_lang", "ko")),
                        }
                    )
                    for item in raw
                ],
            )

        if isinstance(raw, dict):
            entry_defaults = {
                "source_lang": raw.get("source_lang", "en"),
                "target_lang": raw.get("target_lang", "ko"),
            }
            normalized_entries = [
                GlossaryEntry.model_validate(
                    {
                        **entry,
                        "source_lang": _normalize_language_code(
                            entry.get("source_lang", entry_defaults["source_lang"])
                        ),
                        "target_lang": _normalize_language_code(
                            entry.get("target_lang", entry_defaults["target_lang"])
                        ),
                    }
                )
                for entry in raw.get("entries", [])
            ]
            return GlossaryDocument(
                id=raw.get("id", path.stem),
                name=raw.get(
                    "name",
                    self.DEFAULT_GLOSSARY_NAME if path.stem == "default" else path.stem,
                ),
                entries=normalized_entries,
            )

        raise ValueError("Invalid glossary document format")

    def _write_document(self, path: Path, document: GlossaryDocument) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document.model_dump(), ensure_ascii=False, indent=2) + "\n"
        tmp = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        tmp.replace(path)

    def _write_default_document_locked(self) -> None:
        self._write_document(
            self._default_path,
            GlossaryDocument(id="default", name=self.DEFAULT_GLOSSARY_NAME, entries=[]),
        )

    def _load_documents_locked(self) -> list[GlossaryDocument]:
        documents: list[GlossaryDocument] = []
        for path in sorted(self._root.glob("*.json")):
            document = self._load_document_or_none(path)
            if document is not None:
                documents.append(document)
        return documents

    def _load_document_or_none(self, path: Path) -> GlossaryDocument | None:
        try:
            return self._coerce_document(path)
        except (JSONDecodeError, ValidationError, TypeError, ValueError, UnicodeDecodeError):
            self._quarantine_invalid_document(path)
            return None

    def _quarantine_invalid_document(self, path: Path) -> None:
        if not path.exists():
            return

        quarantine_path = path.with_name(f"{path.name}.invalid-{uuid4().hex[:8]}.bak")
        try:
            path.rename(quarantine_path)
        except OSError:
            return


def match_glossary_entries(
    text: str,
    entries: list[GlossaryEntry],
    *,
    target_lang: str,
    source_lang: str = "auto",
) -> list[GlossaryEntry]:
    haystack = _Haystack(text)
    matches = []
    normalized_target_lang = _normalize_language_code(target_lang)
    normalized_source_lang = _normalize_language_code(source_lang)
    for entry in entries:
        if not entry.enabled:
            continue
        if _normalize_language_code(entry.target_lang) != normalized_target_lang:
            continue
        if normalized_source_lang != "auto" and _normalize_language_code(entry.source_lang) != normalized_source_lang:
            continue
        if haystack.contains(entry.source):
            matches.append(entry)
    matches.sort(key=lambda item: len(item.source), reverse=True)
    return matches


# Scripts written without spaces between words (or with particles glued to words, like Korean):
# a term there is a plain substring. Other scripts match whole words only ("AI" is not in "maintain").
_SPACELESS_SCRIPT = re.compile(
    "[\u0e00-\u0e7f\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff"
    "\uac00-\ud7af\uf900-\ufaff\uff66-\uff9d]"
)


_WORD_SUFFIXES = ("", "s", "es", "'s", "’s")  # plural / possessive
_ACRONYM_SUFFIXES = ("", "s")


@lru_cache(maxsize=8192)
def _term_rule(term: str) -> tuple[str, str]:
    """(needle, mode): mode "substring" (spaceless scripts), "acronym" (case-sensitive whole word,
    so "IT" does not match the word "it") or "word" (case-insensitive whole word)."""
    needle = " ".join(term.split())
    if not needle or _SPACELESS_SCRIPT.search(needle):
        return needle.lower(), "substring"
    letters = [char for char in needle if char.isalpha()]
    if len(letters) >= 2 and all(char.isupper() for char in letters):
        return needle, "acronym"
    return needle.lower(), "word"


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _find_word(haystack: str, needle: str, suffixes: tuple[str, ...]) -> bool:
    """``needle`` as a whole word (optionally followed by one of ``suffixes``) somewhere in ``haystack``."""
    check_start = _is_word_char(needle[0])
    check_end = _is_word_char(needle[-1])
    allowed = suffixes if needle[-1].isalpha() else ("",)
    index = haystack.find(needle)
    while index != -1:
        if not (check_start and index > 0 and _is_word_char(haystack[index - 1])):
            if not check_end:
                return True
            end = index + len(needle)
            for suffix in allowed:
                stop = end + len(suffix)
                if haystack.startswith(suffix, end) and (stop >= len(haystack) or not _is_word_char(haystack[stop])):
                    return True
        index = haystack.find(needle, index + 1)
    return False


class _Haystack:
    """A text prepared once for many term look-ups (whitespace runs collapsed to one space)."""

    __slots__ = ("lowered", "text")

    def __init__(self, text: str) -> None:
        self.text = " ".join(text.split())
        self.lowered = self.text.lower()

    def contains(self, term: str) -> bool:
        needle, mode = _term_rule(term)
        if not needle:
            return False
        if mode == "substring":
            return needle in self.lowered
        if mode == "acronym":
            return _find_word(self.text, needle, _ACRONYM_SUFFIXES)
        return _find_word(self.lowered, needle, _WORD_SUFFIXES)


def term_occurs(term: str, text: str) -> bool:
    """True if the glossary source ``term`` occurs in ``text`` (whole words in space-delimited scripts)."""
    return _Haystack(text).contains(term)


_LANGUAGE_ALIASES = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "iw": "he",
}
_LANGUAGE_INDEX = {code.lower(): code for code in LANGUAGE_MAP}


def _normalize_language_code(code: str) -> str:
    """Case-insensitive mapping to a supported code: 'EN' → 'en', 'en-US' → 'en', 'zh-hans' → 'zh-Hans',
    'zh-TW' → 'zh-Hant'. Unknown codes are returned as given (trimmed) so validation can name them."""
    stripped = code.strip()
    lowered = stripped.lower().replace("_", "-")
    if lowered == "auto":
        return "auto"
    parts = lowered.split("-")
    for size in range(len(parts), 0, -1):
        candidate = "-".join(parts[:size])
        if candidate in _LANGUAGE_ALIASES:
            return _LANGUAGE_ALIASES[candidate]
        if candidate in _LANGUAGE_INDEX:
            return _LANGUAGE_INDEX[candidate]
    return stripped


def normalize_language_code(code: str) -> str:
    return _normalize_language_code(code)


# ---------------------------------------------------------------- CSV / TSV / XLSX import & export
EXPORT_COLUMNS = ("source_lang", "target_lang", "source", "target", "note", "enabled")
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 20000

_HEADER_ALIASES: dict[str, str] = {
    "source_lang": "source_lang", "sourcelang": "source_lang", "source language": "source_lang",
    "src_lang": "source_lang", "원문언어": "source_lang", "원문 언어": "source_lang", "출발어": "source_lang",
    "target_lang": "target_lang", "targetlang": "target_lang", "target language": "target_lang",
    "tgt_lang": "target_lang", "번역언어": "target_lang", "번역 언어": "target_lang", "도착어": "target_lang",
    "source": "source", "term": "source", "src": "source", "원문": "source", "원어": "source", "용어": "source",
    "target": "target", "translation": "target", "tgt": "target", "번역": "target", "번역어": "target",
    "대역어": "target",
    "note": "note", "notes": "note", "comment": "note", "memo": "note", "메모": "note", "비고": "note",
    "설명": "note",
    "enabled": "enabled", "enable": "enabled", "active": "enabled", "use": "enabled", "사용": "enabled",
    "사용 여부": "enabled",
}
_TRUE = {"true", "1", "yes", "y", "o", "on", "사용", "예"}
_FALSE = {"false", "0", "no", "n", "x", "off", "미사용", "아니오", "아니요"}
_LANG_CODE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")


class GlossaryImportError(ValueError):
    """Import failed; the message is shown to the user (Korean)."""


def export_entries(entries: list[GlossaryEntry], fmt: str = "csv") -> bytes:
    """UTF-8 with BOM (so Excel detects the encoding); columns EXPORT_COLUMNS."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                entry.source_lang,
                entry.target_lang,
                entry.source,
                entry.target,
                entry.note,
                "true" if entry.enabled else "false",
            ]
        )
    return "﻿".encode() + buffer.getvalue().encode("utf-8")


def parse_import(
    data: bytes,
    filename: str = "",
    *,
    default_source_lang: str = "en",
    default_target_lang: str = "ko",
) -> list[GlossaryEntry]:
    """Parse csv/tsv/txt (header optional, 2+ columns) or xlsx (first sheet) into entries."""
    if not data:
        raise GlossaryImportError("빈 파일입니다.")
    if len(data) > MAX_IMPORT_BYTES:
        raise GlossaryImportError("파일이 너무 큽니다 (최대 5MB).")
    ext = Path(filename or "").suffix.lower()
    if ext in (".xls", ".xlsm", ".ods"):
        raise GlossaryImportError("xlsx, csv, tsv, txt 파일만 가져올 수 있습니다.")
    if data[:2] == b"PK" or ext == ".xlsx":
        rows = _xlsx_rows(data)
    else:
        rows = _text_rows(_decode(data), ext)
    rows = [[cell.strip() for cell in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise GlossaryImportError("가져올 항목이 없습니다.")
    if len(rows) > MAX_IMPORT_ROWS + 1:
        raise GlossaryImportError(f"항목이 너무 많습니다 (최대 {MAX_IMPORT_ROWS:,}개).")

    columns = _header_columns(rows[0])
    start = 1
    if columns is None:
        start = 0
        columns = _guess_columns(rows)
    if "source" not in columns or "target" not in columns:
        raise GlossaryImportError("원문(source)·번역(target) 열을 찾지 못했습니다.")

    src_default = _normalize_language_code(default_source_lang or "en")
    tgt_default = _normalize_language_code(default_target_lang or "ko")
    entries: list[GlossaryEntry] = []
    bad: list[int] = []
    for number, row in enumerate(rows[start:], start=start + 1):
        record = {name: (row[index] if index < len(row) else "") for name, index in columns.items()}
        try:
            entries.append(
                GlossaryEntry(
                    source_lang=_normalize_language_code(record.get("source_lang") or src_default),
                    target_lang=_normalize_language_code(record.get("target_lang") or tgt_default),
                    source=record.get("source", ""),
                    target=record.get("target", ""),
                    note=record.get("note", ""),
                    enabled=_parse_enabled(record.get("enabled", "")),
                )
            )
        except (ValidationError, ValueError):
            bad.append(number)
    if bad:
        shown = ", ".join(str(n) for n in bad[:5]) + (" 등" if len(bad) > 5 else "")
        raise GlossaryImportError(f"{shown}행을 읽지 못했습니다 (원문·번역 필수, 200자 이하).")
    if not entries:
        raise GlossaryImportError("가져올 항목이 없습니다.")
    return entries


def merge_entries(existing: list[GlossaryEntry], imported: list[GlossaryEntry]) -> list[GlossaryEntry]:
    """Append mode: an imported entry replaces an existing one with the same direction + source term."""

    def key(entry: GlossaryEntry) -> tuple[str, str, str]:
        return (
            _normalize_language_code(entry.source_lang),
            _normalize_language_code(entry.target_lang),
            entry.source.strip().casefold(),
        )

    incoming = {key(entry): entry for entry in imported}
    merged: list[GlossaryEntry] = []
    used: set[tuple[str, str, str]] = set()
    for entry in existing:
        k = key(entry)
        if k in incoming:
            if k not in used:
                merged.append(incoming[k])
                used.add(k)
        else:
            merged.append(entry)
    for entry in imported:
        k = key(entry)
        if k not in used:
            merged.append(incoming[k])
            used.add(k)
    return merged


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "cp949"):  # Korean Excel saves CSV as CP949
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _text_rows(text: str, ext: str) -> list[list[str]]:
    first = next((line for line in text.splitlines() if line.strip()), "")
    if ext == ".tsv":
        delimiter = "\t"
    elif ext == ".csv":
        delimiter = "\t" if first.count("\t") > first.count(",") else ","
    else:
        counts = {d: first.count(d) for d in ("\t", ",", ";", "|")}
        delimiter = max(counts, key=lambda d: counts[d]) if any(counts.values()) else "\t"
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def _header_columns(row: list[str]) -> dict[str, int] | None:
    columns: dict[str, int] = {}
    for index, cell in enumerate(row):
        name = _HEADER_ALIASES.get(cell.replace("﻿", "").strip().lower())
        if name and name not in columns:
            columns[name] = index
    if "source" in columns and "target" in columns:
        return columns
    return None


def _guess_columns(rows: list[list[str]]) -> dict[str, int]:
    width = max(len(row) for row in rows[:50])
    if width < 2:
        raise GlossaryImportError("열이 2개 이상이어야 합니다 (원문, 번역).")
    sample = rows[:20]
    langs_first = width >= 4 and all(
        len(row) > 1 and _LANG_CODE.match(row[0]) and _LANG_CODE.match(row[1]) for row in sample
    )
    if langs_first:
        names = ["source_lang", "target_lang", "source", "target", "note", "enabled"]
    else:
        names = ["source", "target", "note", "enabled"]
    return {name: index for index, name in enumerate(names) if index < width}


def _parse_enabled(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered or lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(value)


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XLSX_MAX_XML = 50 * 1024 * 1024


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """First worksheet of an .xlsx file as rows of strings (stdlib only, no formulas evaluated)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())

            def read(name: str) -> bytes:
                if archive.getinfo(name).file_size > _XLSX_MAX_XML:
                    raise GlossaryImportError("파일이 너무 큽니다.")
                return archive.read(name)

            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(read("xl/sharedStrings.xml"))
                for si in root.iter(f"{_XLSX_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
            sheet = _first_sheet_path(names, read)
            root = ET.fromstring(read(sheet))
    except GlossaryImportError:
        raise
    except Exception as exc:  # noqa: BLE001 - any broken / encrypted workbook
        raise GlossaryImportError("엑셀 파일을 읽지 못했습니다. CSV로 저장해 올려 주세요.") from exc

    rows: list[list[str]] = []
    for row in root.iter(f"{_XLSX_NS}row"):
        cells: dict[int, str] = {}
        for position, cell in enumerate(row.iter(f"{_XLSX_NS}c")):
            ref = cell.get("r") or ""
            column = _column_index(ref) if ref else position
            if column >= 64:
                continue
            kind = cell.get("t", "")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{_XLSX_NS}t"))
            else:
                node = cell.find(f"{_XLSX_NS}v")
                raw = node.text if node is not None and node.text is not None else ""
                if kind == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                elif kind == "b":
                    value = "true" if raw == "1" else "false"
                else:
                    value = raw
            cells[column] = value
        if cells:
            rows.append([cells.get(index, "") for index in range(max(cells) + 1)])
        if len(rows) > MAX_IMPORT_ROWS + 1:
            break
    return rows


def _first_sheet_path(names: set[str], read: Callable[[str], bytes]) -> str:
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        workbook = ET.fromstring(read("xl/workbook.xml"))
        first = workbook.find(f"{_XLSX_NS}sheets/{_XLSX_NS}sheet")
        rel_id = first.get(f"{_XLSX_REL_NS}id") if first is not None else None
        rels = ET.fromstring(read("xl/_rels/workbook.xml.rels"))
        for rel in rels:
            if rel.get("Id") == rel_id:
                target = (rel.get("Target") or "").lstrip("/")
                path = target if target.startswith("xl/") else f"xl/{target}"
                if path in names:
                    return path
    candidates = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if not candidates:
        raise GlossaryImportError("엑셀 파일에서 시트를 찾지 못했습니다.")
    return candidates[0]


def _column_index(ref: str) -> int:
    index = 0
    for char in ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return max(0, index - 1)
