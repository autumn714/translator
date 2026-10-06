"""Pydantic models of the document-translation API (SPEC §7)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from translator_app.schemas import TranslateOptions

OutputMode = Literal["translated", "bilingual"]
PdfMode = Literal["layout", "docx"]
JobStatus = Literal["queued", "extracting", "translating", "writing", "done", "error", "canceled"]


class DocumentOptions(TranslateOptions):
    """Form field ``options`` of ``POST /api/documents`` (JSON string)."""

    model_config = ConfigDict(extra="ignore")

    output: OutputMode = "translated"
    pdf_mode: PdfMode = "layout"

    def translate_options(self) -> TranslateOptions:
        return TranslateOptions.model_validate(self.model_dump(exclude={"output", "pdf_mode"}))


class JobProgress(BaseModel):
    done: int = 0
    total: int = 0
    percent: float = 0.0


class DocumentJob(BaseModel):
    id: str
    filename: str
    size: int
    format: str                                   # input extension with dot, e.g. ".docx"
    source_lang: str
    target_lang: str
    status: JobStatus
    progress: JobProgress = Field(default_factory=JobProgress)
    output_filename: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    expires_at: str
    eta_seconds: int | None = None
    chars: int | None = None
    report_count: int = 0
    output: OutputMode = "translated"
    pdf_mode: PdfMode = "layout"
    detected_source_lang: str | None = None


class DocumentJobList(BaseModel):
    jobs: list[DocumentJob]


class PreviewResponse(BaseModel):
    text: str


class ReportItem(BaseModel):
    source: str
    target: str
    issue: str


class ReportResponse(BaseModel):
    items: list[ReportItem]
