"""Document translation jobs: queue, workers, progress, persistence and cleanup.

Job files live in ``{data_dir}/jobs/<id>/``:
    input<ext>     uploaded file
    output<ext>    translated file (extension may differ: HWP/images -> .docx)
    meta.json      job record (written atomically on every status change and at
                   most every 2 s while progress changes)
    preview.txt    translated text (first 20k chars)
    report.json    rule-based quality report

Privacy: document text and file names are never logged; only ids, extensions and counts.
Job directories are created 0700 and their files 0600.  A job that fails or is
canceled loses its input at once (only meta.json stays until it expires), and the
whole of ``jobs/`` is kept under DOC_DISK_QUOTA_MB (new uploads get 507 beyond it).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
import traceback
import unicodedata
import uuid
from collections import deque
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from translator_app.documents import get_handler, is_supported, supports_bilingual, unsupported_message
from translator_app.documents.base import (
    MSG_FAILED,
    MSG_INTERRUPTED,
    MSG_LLM_UNAVAILABLE,
    MSG_VISION_UNAVAILABLE,
    DocumentError,
    HandlerContext,
    HandlerResult,
    has_tags,
    needs_translation,
    strip_tags,
)
from translator_app.documents.report import build_report
from translator_app.documents.schemas import DocumentJob, DocumentOptions, JobProgress
from translator_app.llm.client import LLMError, LLMUnavailable, TranslationCancelled, VisionUnavailable
from translator_app.schemas import TranslateOptions

logger = logging.getLogger("translator.documents")

T = TypeVar("T")

ACTIVE = frozenset({"queued", "extracting", "translating", "writing"})
TERMINAL = frozenset({"done", "error", "canceled"})
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

CHUNK_CHARS = 2000              # source chars per translate_batch call (≈ one LLM request)
CHUNK_ITEMS = 30
PRECEDING_PAIRS = 3
CLEANUP_INTERVAL = 600.0        # seconds
PERSIST_INTERVAL = 2.0
ETA_WINDOW = 120.0              # seconds of progress samples used for the ETA
MAX_ACTIVE_JOBS = 200
MAX_WARNINGS = 20
INCOMING_MAX_AGE = 6 * 3600

MSG_NOT_READY = "문서 번역 기능이 준비되지 않았습니다. 잠시 후 다시 시도하세요."
MSG_TOO_MANY = "대기 중인 문서가 너무 많습니다. 잠시 후 다시 올려 주세요."
MSG_EMPTY_FILE = "빈 파일입니다."
MSG_CANCELED = "취소되었습니다."
MSG_DISK_FULL = "서버 저장 공간이 부족합니다. 잠시 후 다시 시도하세요."

DIR_MODE = 0o700
FILE_MODE = 0o600
MB = 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)


def _write_private(path: Path, data: bytes | str) -> None:
    """Write a file readable by the owner only (0600, whatever the umask)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    with contextlib.suppress(OSError):
        os.chmod(path, FILE_MODE)


def _restrict_files(d: Path) -> None:
    """Handlers write their output with the default mode: tighten it to 0600."""
    with contextlib.suppress(OSError):
        for p in d.iterdir():
            if p.is_file():
                with contextlib.suppress(OSError):
                    os.chmod(p, FILE_MODE)


def _tree_size(root: Path, skip: Path | None = None) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if skip is not None and Path(dirpath) == skip.parent:
            dirnames[:] = [n for n in dirnames if n != skip.name]
        for name in filenames:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sanitize_filename(name: str | None, fallback: str = "document") -> str:
    """Display name: no directories, control characters or characters Windows forbids."""
    s = unicodedata.normalize("NFC", name or "")
    s = s.replace("\\", "/").split("/")[-1]
    s = "".join(c for c in s if unicodedata.category(c)[0] != "C")
    s = re.sub(r'[<>:"|?*]', "_", s).strip().rstrip(". ")
    if len(s) > 180:
        stem, dot, ext = s.rpartition(".")
        s = (stem[: 180 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else s[:180]
    return s or fallback


def output_name(filename: str, ext_out: str, source: str, target: str, bilingual: bool) -> str:
    stem = Path(filename).stem or "document"
    if bilingual:
        return f"{stem}_{source}-{target}{ext_out}"
    return f"{stem}_{target}{ext_out}"


def _chunks(texts: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    n = 0
    for t in texts:
        size = len(t)
        if cur and (n + size > CHUNK_CHARS or len(cur) >= CHUNK_ITEMS):
            out.append(cur)
            cur, n = [], 0
        cur.append(t)
        n += size
    if cur:
        out.append(cur)
    return out


def _lanes(chunks: list[list[str]], n: int) -> list[list[list[str]]]:
    """Split chunks into n contiguous runs of similar size (each run keeps its own context)."""
    n = max(1, min(n, len(chunks)))
    sizes = [sum(len(t) for t in c) for c in chunks]
    total = sum(sizes) or 1
    lanes: list[list[list[str]]] = [[] for _ in range(n)]
    acc = 0
    for c, s in zip(chunks, sizes):
        idx = min(n - 1, int(acc * n / total))
        lanes[idx].append(c)
        acc += s
    return [lane for lane in lanes if lane]


def _detect_language(texts: list[str]) -> str | None:
    sample = "\n\n".join(strip_tags(t) for t in texts[:200])[:6000]
    if not sample.strip():
        return None
    try:
        from translator_app.services.language_detection import detect_source_languages

        summary = detect_source_languages(sample)
        return summary.primary_language
    except Exception:  # noqa: BLE001 - detection is a nicety only
        return None


# ====================================================================== job record
@dataclass
class Job:
    id: str
    filename: str
    size: int
    ext: str
    options: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    status: str = "queued"
    done: int = 0
    total: int = 0
    chars_done: int = 0
    chars_total: int = 0                 # progress weight (text chars + nominal image work)
    text_chars: int = 0                  # source characters sent for translation
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    finished_at: datetime | None = None
    output_file: str | None = None       # name inside the job dir
    output_filename: str | None = None   # download name
    report_count: int = 0
    detected_source_lang: str | None = None
    # runtime only
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    deleted: bool = False
    samples: deque = field(default_factory=lambda: deque(maxlen=400), repr=False)
    last_persist: float = 0.0
    shown_percent: float = 0.0           # progress never moves backwards on screen

    @property
    def source_lang(self) -> str:
        return str(self.options.get("source_lang") or "auto")

    @property
    def target_lang(self) -> str:
        return str(self.options.get("target_lang") or "ko")

    @property
    def input_name(self) -> str:
        return f"input{self.ext}"

    # ------------------------------------------------------------ progress
    def percent(self) -> float:
        if self.status == "done":
            return 100.0
        if self.status == "queued":
            return 0.0
        if self.chars_total > 0:
            p = 100.0 * self.chars_done / self.chars_total
        elif self.total > 0:
            p = 100.0 * self.done / self.total
        else:
            p = 0.0
        p = max(round(min(p, 99.0), 1), self.shown_percent)
        self.shown_percent = p
        return p

    def eta_seconds(self) -> int | None:
        if self.status != "translating" or len(self.samples) < 2 or self.chars_total <= 0:
            return None
        now = time.monotonic()
        recent = [s for s in self.samples if now - s[0] <= ETA_WINDOW] or [self.samples[-1]]
        t0, c0 = recent[0] if len(recent) > 1 else self.samples[0]
        t1, c1 = self.samples[-1]
        if t1 - t0 < 1.0 or c1 <= c0:
            return None
        rate = (c1 - c0) / (t1 - t0)
        remaining = max(0, self.chars_total - self.chars_done)
        return int(remaining / rate + 0.5)

    # ------------------------------------------------------------ (de)serialisation
    def to_meta(self) -> dict[str, Any]:
        return {
            "id": self.id, "filename": self.filename, "size": self.size, "ext": self.ext,
            "options": self.options, "status": self.status, "done": self.done, "total": self.total,
            "chars_done": self.chars_done, "chars_total": self.chars_total, "text_chars": self.text_chars,
            "warnings": self.warnings, "error": self.error,
            "created_at": _iso(self.created_at), "finished_at": _iso(self.finished_at),
            "expires_at": _iso(self.expires_at), "output_file": self.output_file,
            "output_filename": self.output_filename, "report_count": self.report_count,
            "detected_source_lang": self.detected_source_lang,
        }

    @classmethod
    def from_meta(cls, d: dict[str, Any]) -> Job:
        created = _parse_dt(d.get("created_at")) or _now()
        return cls(
            id=str(d["id"]), filename=str(d.get("filename") or "document"), size=int(d.get("size") or 0),
            ext=str(d.get("ext") or ""), options=dict(d.get("options") or {}),
            created_at=created, expires_at=_parse_dt(d.get("expires_at")) or created,
            status=str(d.get("status") or "error"), done=int(d.get("done") or 0), total=int(d.get("total") or 0),
            chars_done=int(d.get("chars_done") or 0), chars_total=int(d.get("chars_total") or 0),
            text_chars=int(d.get("text_chars") or 0),
            warnings=[str(w) for w in d.get("warnings") or []], error=d.get("error"),
            finished_at=_parse_dt(d.get("finished_at")), output_file=d.get("output_file"),
            output_filename=d.get("output_filename"), report_count=int(d.get("report_count") or 0),
            detected_source_lang=d.get("detected_source_lang"),
        )

    def public(self) -> DocumentJob:
        return DocumentJob(
            id=self.id, filename=self.filename, size=self.size, format=self.ext,
            source_lang=self.source_lang, target_lang=self.target_lang, status=self.status,  # type: ignore[arg-type]
            progress=JobProgress(done=self.done, total=self.total, percent=self.percent()),
            output_filename=self.output_filename if self.status == "done" else None,
            warnings=list(self.warnings), error=self.error,
            created_at=_iso(self.created_at) or "", finished_at=_iso(self.finished_at),
            expires_at=_iso(self.expires_at) or "", eta_seconds=self.eta_seconds(),
            chars=self.text_chars or None, report_count=self.report_count,
            output=self.options.get("output", "translated"), pdf_mode=self.options.get("pdf_mode", "layout"),
            detected_source_lang=self.detected_source_lang,
        )


# ====================================================================== manager
class JobManager:
    def __init__(self, *, settings: Any, translator: Any) -> None:
        self.settings = settings
        self.translator = translator
        self.root = Path(settings.data_dir) / "jobs"
        self.incoming = self.root / ".incoming"
        self.jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None
        self._running: set[str] = set()
        self._started = False
        self._closing = False
        self._usage = 0                          # bytes in job directories (refreshed on every change)
        self._uploads: dict[str, int] = {}       # bytes of uploads still streaming into .incoming

    # ------------------------------------------------------------ settings
    @property
    def max_bytes(self) -> int:
        return int(self.settings.doc_max_mb) * 1024 * 1024

    @property
    def retention(self) -> timedelta:
        return timedelta(hours=float(self.settings.doc_retention_hours))

    @property
    def disk_quota(self) -> int:
        """DOC_DISK_QUOTA_MB: total size of jobs/ (uploads + results)."""
        try:
            mb = float(getattr(self.settings, "doc_disk_quota_mb", 2048) or 2048)
        except (TypeError, ValueError):
            mb = 2048.0
        return int(max(mb, 1.0) * MB)

    @property
    def font_dir(self) -> Path:
        return Path(self.settings.data_dir) / "fonts"

    @property
    def ready(self) -> bool:
        return self._started and not self._closing

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._started:
            return
        await asyncio.to_thread(self._load_existing)
        self._queue = asyncio.Queue()
        n = max(1, int(getattr(self.settings, "doc_job_concurrency", 1) or 1))
        self._workers = [asyncio.create_task(self._worker(), name=f"doc-worker-{i}") for i in range(n)]
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="doc-cleanup")
        self._started = True
        self._closing = False
        logger.info("문서 작업 관리자 시작: 보관 중 %d건, 동시 처리 %d건", len(self.jobs), n)

    async def aclose(self) -> None:
        if not self._started:
            return
        self._closing = True
        tasks = [*self._workers]
        if self._cleanup_task is not None:
            tasks.append(self._cleanup_task)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._cleanup_task = None
        for job in self.jobs.values():                  # queued jobs never started
            if job.status in ACTIVE:
                self._finish(job, "error", MSG_INTERRUPTED)
        self._started = False

    def _load_existing(self) -> None:
        _private_dir(self.root)
        shutil.rmtree(self.incoming, ignore_errors=True)
        now = _now()
        for d in self.root.iterdir():
            if not d.is_dir() or not JOB_ID_RE.match(d.name):
                continue
            meta = d / "meta.json"
            try:
                job = Job.from_meta(json.loads(meta.read_text("utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                job = None
            if job is None or job.id != d.name:
                # unreadable record: drop it once it is older than the retention period
                try:
                    age = now.timestamp() - d.stat().st_mtime
                except OSError:
                    age = 0
                if age > self.retention.total_seconds():
                    shutil.rmtree(d, ignore_errors=True)
                continue
            if job.status not in TERMINAL:
                job.status, job.error = "error", MSG_INTERRUPTED
                job.finished_at = now
                job.expires_at = now + self.retention
                self._write_meta(job)
            # a shorter DOC_RETENTION_HOURS also applies to files kept from before
            limit = (job.finished_at or job.created_at) + self.retention
            if job.expires_at > limit:
                job.expires_at = limit
                self._write_meta(job)
            if job.expires_at <= now:
                shutil.rmtree(d, ignore_errors=True)
                continue
            if job.status != "done":
                self._discard_files(d)
            with contextlib.suppress(OSError):
                os.chmod(d, DIR_MODE)
            _restrict_files(d)
            self.jobs[job.id] = job
        self._refresh_usage()

    # ------------------------------------------------------------ disk quota
    def _refresh_usage(self) -> None:
        self._usage = _tree_size(self.root, skip=self.incoming) if self.root.is_dir() else 0

    def disk_used(self) -> int:
        """Bytes in job directories plus uploads still streaming in."""
        return self._usage + sum(self._uploads.values())

    def begin_upload(self) -> str:
        token = uuid.uuid4().hex
        self._refresh_usage()
        self._uploads[token] = 0
        return token

    def track_upload(self, token: str, size: int) -> bool:
        """Record the bytes received so far. -> False when the quota would be exceeded."""
        self._uploads[token] = size
        return self.disk_used() <= self.disk_quota

    def end_upload(self, token: str) -> None:
        self._uploads.pop(token, None)

    # ------------------------------------------------------------ public API
    def new_upload_path(self) -> Path:
        _private_dir(self.root)
        _private_dir(self.incoming)
        path = self.incoming / f"{uuid.uuid4().hex}.part"
        _write_private(path, b"")
        return path

    async def submit(self, source: Path | bytes, filename: str,
                     options: DocumentOptions | dict[str, Any] | None = None) -> Job:
        """Register an uploaded file (a path is moved into the job dir) and queue it."""
        tmp = source if isinstance(source, Path) else None
        try:
            if not self.ready or self._queue is None:
                raise DocumentError(MSG_NOT_READY, 503)
            name = sanitize_filename(filename)
            ext = Path(name).suffix.lower()
            if not is_supported(ext):
                raise DocumentError(unsupported_message(ext), 415)
            opts = options if isinstance(options, DocumentOptions) else DocumentOptions.model_validate(options or {})
            if opts.output == "bilingual" and not supports_bilingual(ext):
                opts = opts.model_copy(update={"output": "translated"})
            size = len(source) if isinstance(source, (bytes, bytearray)) else source.stat().st_size
            if size <= 0:
                raise DocumentError(MSG_EMPTY_FILE)
            if size > self.max_bytes:
                raise DocumentError(f"파일이 너무 큽니다. 최대 {self.settings.doc_max_mb}MB까지 올릴 수 있습니다.", 413)
            if sum(1 for j in self.jobs.values() if j.status in ACTIVE) >= MAX_ACTIVE_JOBS:
                raise DocumentError(MSG_TOO_MANY, 429)
            await asyncio.to_thread(self._refresh_usage)
            if self.disk_used() + size > self.disk_quota:
                raise DocumentError(MSG_DISK_FULL, 507)

            job_id = uuid.uuid4().hex
            d = self.job_dir(job_id)
            _private_dir(self.root)
            d.mkdir(mode=DIR_MODE)
            with contextlib.suppress(OSError):
                os.chmod(d, DIR_MODE)
            target = d / f"input{ext}"
            if isinstance(source, (bytes, bytearray)):
                await asyncio.to_thread(_write_private, target, bytes(source))
            else:
                await asyncio.to_thread(shutil.move, str(source), str(target))
                with contextlib.suppress(OSError):
                    os.chmod(target, FILE_MODE)
            tmp = None
            self._usage += size
            now = _now()
            job = Job(id=job_id, filename=name, size=size, ext=ext,
                      options=opts.model_dump(mode="json"), created_at=now, expires_at=now + self.retention)
            self.jobs[job_id] = job
            self._persist(job, force=True)
            self._queue.put_nowait(job_id)
            logger.info("문서 작업 접수 job=%s 형식=%s 크기=%d", job_id, ext, size)
            return job
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink()

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.match(job_id or ""):
            return None
        job = self.jobs.get(job_id)
        return None if job is None or job.deleted else job

    def list(self, ids: list[str]) -> list[Job]:
        out: list[Job] = []
        seen: set[str] = set()
        for i in ids:
            job = self.get(i.strip())
            if job is not None and job.id not in seen:
                seen.add(job.id)
                out.append(job)
        return out

    async def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        if job.status == "queued":
            self._finish(job, "canceled", None)
            await asyncio.to_thread(self._discard_files, self.job_dir(job.id))
            await asyncio.to_thread(self._refresh_usage)
        elif job.status in ACTIVE:
            job.cancel_event.set()
            for _ in range(50):                          # usually immediate (requests are aborted)
                if job.status not in ACTIVE:
                    break
                await asyncio.sleep(0.02)
        return job

    async def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.deleted = True
        self.jobs.pop(job.id, None)
        if job.id in self._running:
            job.cancel_event.set()                       # the runner removes the directory when it stops
        else:
            if job.status in ACTIVE:
                job.status = "canceled"
            await asyncio.to_thread(shutil.rmtree, self.job_dir(job.id), True)
            await asyncio.to_thread(self._refresh_usage)
        logger.info("문서 작업 삭제 job=%s", job.id)
        return True

    def output_path(self, job: Job) -> Path | None:
        if job.status != "done" or not job.output_file:
            return None
        p = self.job_dir(job.id) / job.output_file
        return p if p.is_file() else None

    def preview_text(self, job: Job) -> str:
        p = self.job_dir(job.id) / "preview.txt"
        try:
            return p.read_text("utf-8")
        except OSError:
            return ""

    def report_items(self, job: Job) -> list[dict[str, str]]:
        p = self.job_dir(job.id) / "report.json"
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    async def cleanup_expired(self) -> int:
        """Delete expired jobs and stale partial uploads. -> number of jobs removed."""
        now = _now()
        expired = [j for j in self.jobs.values()
                   if j.status not in ACTIVE and j.id not in self._running and j.expires_at <= now]
        for job in expired:
            self.jobs.pop(job.id, None)
            job.deleted = True
        await asyncio.to_thread(self._cleanup_files, [j.id for j in expired])
        await asyncio.to_thread(self._refresh_usage)
        if expired:
            logger.info("보관 기간이 지난 문서 작업 %d건 삭제", len(expired))
        return len(expired)

    def _cleanup_files(self, ids: list[str]) -> None:
        for job_id in ids:
            shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        now = time.time()
        if self.incoming.is_dir():
            for p in self.incoming.iterdir():
                with contextlib.suppress(OSError):
                    if now - p.stat().st_mtime > INCOMING_MAX_AGE:
                        p.unlink()
        if self.root.is_dir():                          # directories without a live record
            for d in self.root.iterdir():
                if d.is_dir() and JOB_ID_RE.match(d.name) and d.name not in self.jobs:
                    with contextlib.suppress(OSError):
                        if now - d.stat().st_mtime > self.retention.total_seconds():
                            shutil.rmtree(d, ignore_errors=True)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                await self.cleanup_expired()
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.warning("문서 작업 정리 실패", exc_info=True)

    # ------------------------------------------------------------ worker
    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            try:
                job = self.jobs.get(job_id)
                if job is None or job.deleted or job.status != "queued":
                    continue
                await self._run_job(job)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        d = self.job_dir(job.id)
        self._running.add(job.id)
        started = time.monotonic()
        try:
            opts = DocumentOptions.model_validate(job.options)
            topts = opts.translate_options()
            handler = get_handler(job.ext)
            ctx = HandlerContext(
                input_path=d / job.input_name,
                output_path=d / f"output{job.ext}",
                ext=job.ext,
                opts=topts,
                output_mode=opts.output,
                pdf_mode=opts.pdf_mode,
                target_lang=opts.target_lang,
                font_dir=self.font_dir,
                translate_strings=partial(self._translate_strings, job, topts),
                describe_image=partial(self._describe_image, job, topts),
                set_status=partial(self._set_status, job),
                add_units=partial(self._add_units, job),
                unit_done=partial(self._unit_done, job),
                warn=partial(self._warn, job),
                cancel=job.cancel_event,
                max_chars=int(self.settings.doc_max_chars),
            )
            result: HandlerResult = await handler.run(ctx)
            ctx.check_cancel()
            out_ext = result.output_ext or job.ext
            out = d / f"output{out_ext}"
            if not out.is_file():
                raise DocumentError(MSG_FAILED)
            await asyncio.to_thread(_write_private, d / "preview.txt", result.preview[:20_000])
            items = await self._report(job, topts, result.pairs)
            await asyncio.to_thread(_write_private, d / "report.json",
                                    json.dumps({"items": items}, ensure_ascii=False))
            job.report_count = len(items)
            job.output_file = out.name
            source = opts.source_lang if opts.source_lang != "auto" else (job.detected_source_lang or "auto")
            job.output_filename = output_name(job.filename, out_ext, source, opts.target_lang,
                                              opts.output == "bilingual")
            job.done = max(job.done, job.total)
            job.chars_done = job.chars_total
            self._finish(job, "done", None)
            logger.info("문서 번역 완료 job=%s 형식=%s 단위=%d 글자=%d 검수=%d %.1f초", job.id, job.ext,
                        job.total, job.text_chars, job.report_count, time.monotonic() - started)
        except TranslationCancelled:
            self._discard_files(d)
            self._finish(job, "canceled", None)
            logger.info("문서 번역 취소 job=%s", job.id)
        except DocumentError as exc:
            self._discard_files(d)
            self._finish(job, "error", exc.message)
            logger.info("문서 번역 실패 job=%s 형식=%s: 입력 문제", job.id, job.ext)
        except VisionUnavailable:
            self._discard_files(d)
            self._finish(job, "error", MSG_VISION_UNAVAILABLE)
            logger.warning("문서 번역 실패 job=%s: 이미지 입력 미지원 모델", job.id)
        except LLMUnavailable:
            self._discard_files(d)
            self._finish(job, "error", MSG_LLM_UNAVAILABLE)
            logger.warning("문서 번역 실패 job=%s: 모델 서버 연결 안 됨", job.id)
        except LLMError as exc:
            self._discard_files(d)
            self._finish(job, "error", str(exc) or MSG_FAILED)
            logger.warning("문서 번역 실패 job=%s: 모델 오류 %s", job.id, type(exc).__name__)
        except asyncio.CancelledError:
            self._discard_files(d)
            self._finish(job, "error", MSG_INTERRUPTED)
            raise
        except Exception as exc:  # noqa: BLE001 - any parser error ends the job, not the worker
            self._discard_files(d)
            self._finish(job, "error", MSG_FAILED)
            # frames only: exception messages may quote document content
            logger.warning("문서 번역 실패 job=%s 형식=%s: %s\n%s", job.id, job.ext, type(exc).__name__,
                           "".join(traceback.format_tb(exc.__traceback__)).rstrip())
        finally:
            self._running.discard(job.id)
            if job.deleted:
                await asyncio.to_thread(shutil.rmtree, d, True)
            else:
                if job.status != "done":                 # files a handler thread wrote late
                    await asyncio.to_thread(self._discard_files, d)
                await asyncio.to_thread(_restrict_files, d)
            await asyncio.to_thread(self._refresh_usage)

    @staticmethod
    def _discard_files(d: Path) -> None:
        """A failed / canceled job keeps only its record: the upload and partial results go."""
        for pattern in ("input*", "output*", "preview.txt", "report.json"):
            for p in d.glob(pattern):
                with contextlib.suppress(OSError):
                    p.unlink()

    # ------------------------------------------------------------ handler callbacks
    def _finish(self, job: Job, status: str, error: str | None) -> None:
        job.status = status
        job.error = error
        now = _now()
        job.finished_at = now
        job.expires_at = max(job.expires_at, now + self.retention) if status == "done" else job.expires_at
        self._persist(job, force=True)

    def _set_status(self, job: Job, status: str) -> None:
        if job.status == status or job.status in TERMINAL:
            return
        job.status = status
        if status == "translating" and not job.samples:
            job.samples.append((time.monotonic(), job.chars_done))
        self._persist(job, force=True)

    def _add_units(self, job: Job, units: int, chars: int) -> None:
        """Negative values release work reserved earlier (never below what is done)."""
        job.total = max(job.done, job.total + units)
        job.chars_total = max(job.chars_done, job.chars_total + chars)
        self._persist(job)

    def _unit_done(self, job: Job, units: int, chars: int) -> None:
        job.done = min(job.total, job.done + max(0, units))
        job.chars_done = min(job.chars_total, job.chars_done + max(0, chars))
        job.samples.append((time.monotonic(), job.chars_done))
        self._persist(job)

    def _warn(self, job: Job, message: str) -> None:
        if message and message not in job.warnings and len(job.warnings) < MAX_WARNINGS:
            job.warnings.append(message)
            self._persist(job, force=True)

    # ------------------------------------------------------------ translation
    async def _translate_strings(self, job: Job, opts: TranslateOptions, texts: list[str]) -> dict[str, str]:
        self._set_status(job, "translating")
        table: dict[str, str] = {}
        todo: list[str] = []
        for t in dict.fromkeys(texts):
            if needs_translation(t):
                todo.append(t)
            else:
                table[t] = t
        chars = sum(len(strip_tags(t)) for t in todo)
        limit = int(self.settings.doc_max_chars)
        if job.text_chars + chars > limit:
            raise DocumentError(f"문서가 너무 깁니다. 번역할 글자 수 {job.text_chars + chars:,}자 (최대 {limit:,}자)")
        job.text_chars += chars
        if job.detected_source_lang is None and opts.source_lang == "auto" and todo:
            job.detected_source_lang = await asyncio.to_thread(_detect_language, todo)
        self._add_units(job, len(todo), chars)
        if not todo:
            return table
        if not job.samples:
            job.samples.append((time.monotonic(), job.chars_done))

        async def lane(chunks: list[list[str]]) -> None:
            preceding: list[tuple[str, str]] | None = None
            for chunk in chunks:
                if job.cancel_event.is_set():
                    raise TranslationCancelled()
                outs = await self._translate_chunk(job, opts, chunk, preceding)
                table.update(zip(chunk, outs))
                tail = list(zip(chunk, outs))[-PRECEDING_PAIRS:]
                preceding = [(strip_tags(s), strip_tags(o)) for s, o in tail]
                self._unit_done(job, len(chunk), sum(len(strip_tags(s)) for s in chunk))

        parallel = int(getattr(self.settings, "llm_doc_parallel", 1) or 1)
        await _all_or_nothing([lane(group) for group in _lanes(_chunks(todo), parallel)])
        return table

    async def _translate_chunk(self, job: Job, opts: TranslateOptions, chunk: list[str],
                               preceding: list[tuple[str, str]] | None) -> list[str]:
        # The service already retries items whose tags came back broken; whatever is
        # still broken is handled by the recipes' fallback and listed in the report.
        tagged = any(has_tags(s) for s in chunk)
        outs = await self._race(job, self.translator.translate_batch(
            list(chunk), opts, priority="document", preceding=preceding, tags=tagged, cancel=job.cancel_event))
        return _sanitize_outputs(chunk, outs)

    async def _describe_image(self, job: Job, opts: TranslateOptions, data: bytes, mime: str) -> str:
        self._set_status(job, "translating")
        result = await self._race(job, self.translator.describe_image(
            data, mime, opts, mode="translate", priority="document"))
        return result if isinstance(result, str) else ""

    async def _race(self, job: Job, coro: Awaitable[T]) -> T:
        """Await coro, abort it as soon as the job is canceled (closes the upstream request)."""
        if job.cancel_event.is_set():
            if asyncio.iscoroutine(coro):
                coro.close()
            raise TranslationCancelled()
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(job.cancel_event.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            waiter.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        waiter.cancel()
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise TranslationCancelled()

    # ------------------------------------------------------------ report
    async def _report(self, job: Job, opts: TranslateOptions, pairs: list[tuple[str, str]]) -> list[dict]:
        if not pairs:
            return []
        entries: list[Any] = []
        if opts.use_glossary:
            sample = "\n".join(strip_tags(s) for s, _ in pairs)[:200_000]
            try:
                resolver = getattr(self.translator, "resolve_glossary", None)
                if resolver is not None:
                    entries, _name = await resolver(opts, sample)
                elif opts.glossary_entries:
                    entries = list(opts.glossary_entries)
            except Exception:  # noqa: BLE001 - the report must never fail the job
                logger.warning("검수용 용어집을 읽지 못했습니다 job=%s", job.id)
                entries = []
        source = opts.source_lang if opts.source_lang != "auto" else (job.detected_source_lang or "auto")
        try:
            return await asyncio.to_thread(build_report, pairs, source_lang=source,
                                           target_lang=opts.target_lang, glossary=entries or ())
        except Exception:  # noqa: BLE001
            logger.warning("검수 결과를 만들지 못했습니다 job=%s", job.id)
            return []

    # ------------------------------------------------------------ persistence
    def _persist(self, job: Job, *, force: bool = False) -> None:
        if job.deleted:
            return
        now = time.monotonic()
        if not force and now - job.last_persist < PERSIST_INTERVAL:
            return
        job.last_persist = now
        self._write_meta(job)

    def _write_meta(self, job: Job) -> None:
        d = self.job_dir(job.id)
        if not d.is_dir():
            return
        tmp = d / "meta.json.tmp"
        data = json.dumps(job.to_meta(), ensure_ascii=False, indent=1)
        try:
            _write_private(tmp, data)
            os.replace(tmp, d / "meta.json")
        except OSError:
            logger.warning("작업 기록을 저장하지 못했습니다 job=%s", job.id)


def _sanitize_outputs(chunk: list[str], outs: Any) -> list[str]:
    """Same length as chunk; empty / non-string answers fall back to the source text."""
    res: list[str] = []
    outs = list(outs) if isinstance(outs, (list, tuple)) else []
    for i, src in enumerate(chunk):
        o = outs[i] if i < len(outs) else None
        if not isinstance(o, str) or (not o.strip() and src.strip()):
            o = src
        res.append(o)
    return res


async def _all_or_nothing(coros: list[Coroutine[Any, Any, Any]]) -> None:
    """Run coroutines concurrently; on the first failure cancel the rest and re-raise it."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for t in tasks:
        if t.done() and not t.cancelled() and t.exception() is not None:
            raise t.exception()  # type: ignore[misc]


__all__ = ["Job", "JobManager", "output_name", "sanitize_filename"]
