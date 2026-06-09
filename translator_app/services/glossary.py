from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import ValidationError

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
                document = self._load_document_or_none(self._default_path)
                if document is not None:
                    self._write_document(self._default_path, document)
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
        path.write_text(
            json.dumps(document.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
    lowered = text.lower()
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
        if entry.source.lower() in lowered:
            matches.append(entry)
    matches.sort(key=lambda item: len(item.source), reverse=True)
    return matches


def _normalize_language_code(code: str) -> str:
    lowered = code.strip()
    aliases = {
        "zh": "zh-Hans",
        "zh-cn": "zh-Hans",
        "zh-tw": "zh-Hant",
        "iw": "he",
    }
    return aliases.get(lowered.lower(), lowered)
