"""Async client for an OpenAI-compatible chat completions server (vLLM v0.30 + Qwen3.8).

Request bodies follow SPEC §6a: every sampling field is sent explicitly, thinking is
disabled through ``chat_template_kwargs``, ``max_completion_tokens`` is always sized to the
input, ``repetition_detection`` is always on, and ``reasoning_effort`` / ``guided_*`` /
non-zero ``priority`` / ``stream_options`` without ``stream`` are never sent.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from translator_app.config import Settings
from translator_app.llm.limiter import PriorityLimiter

logger = logging.getLogger("translator.llm")


# ---------------------------------------------------------------- errors
class LLMError(Exception):
    """Base error for model-server problems (message is shown to users, Korean)."""

    default_message = "모델 서버 오류가 발생했습니다."

    def __init__(self, message: str | None = None, *, status: int | None = None) -> None:
        super().__init__(message or self.default_message)
        self.status = status

    @property
    def message(self) -> str:
        return str(self)


class LLMUnavailable(LLMError):
    default_message = "모델 서버에 연결할 수 없습니다."


class VisionUnavailable(LLMError):
    default_message = "모델 서버가 이미지 입력을 지원하지 않습니다."


class LLMOutputError(LLMError):
    """The model answered, but the output is unusable (truncated, invalid JSON, wrong shape)."""

    default_message = "모델 응답을 해석하지 못했습니다."


class LLMInputTooLong(LLMError):
    default_message = "입력이 너무 깁니다 (모델 최대 길이 초과)."


class TranslationCancelled(Exception):
    def __init__(self, message: str = "번역이 취소되었습니다.") -> None:
        super().__init__(message)


# ---------------------------------------------------------------- sampling presets
@dataclass(frozen=True, slots=True)
class Sampling:
    temperature: float
    top_p: float
    top_k: int
    min_p: float = 0.0
    presence_penalty: float = 0.0
    repetition_penalty: float = 1.0

    def with_(self, **changes: Any) -> Sampling:
        return replace(self, **changes)

    def as_body(self) -> dict[str, float | int]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
        }


TRANSLATE_SAMPLING = Sampling(temperature=0.2, top_p=0.8, top_k=20)
JSON_SAMPLING = Sampling(temperature=0.0, top_p=1.0, top_k=-1)
OCR_SAMPLING = Sampling(temperature=0.1, top_p=1.0, top_k=-1)
ALTERNATIVES_SAMPLING = Sampling(temperature=0.7, top_p=0.8, top_k=20)
REWRITE_SAMPLING = Sampling(temperature=0.3, top_p=0.8, top_k=20)

REPETITION_DETECTION = {"min_pattern_size": 8, "max_pattern_size": 64, "min_count": 4}
RETRY_FINISH_REASONS = frozenset({"length", "repetition"})
MIN_COMPLETION_TOKENS = 256
MAX_COMPLETION_TOKENS = 8192
OCR_MAX_COMPLETION_TOKENS = 6144
IMAGE_TOKEN_ESTIMATE = 2500

# Keys the client always controls; LLM_EXTRA_BODY cannot override them.
_PROTECTED_KEYS = frozenset({"model", "messages", "stream", "stream_options", "response_format", "max_completion_tokens", "max_tokens", "n"})
# Keys that must never reach vLLM v0.30 (see SPEC §6a).
_FORBIDDEN_KEYS = frozenset({"reasoning_effort", "priority"})

_CJK_RE = re.compile(r"[ᄀ-ᇿ぀-ヿ㄰-㆏㐀-䶿一-鿿가-힯豈-﫿ｦ-ﾝ]")
_THINK_BLOCK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: CJK ≈ 2 chars/token, other scripts ≈ 4 chars/token."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, (cjk + 1) // 2 + (other + 3) // 4)


def completion_budget(source: str | int, *, factor: float = 3.0, extra: int = 256,
                      low: int = MIN_COMPLETION_TOKENS, high: int = MAX_COMPLETION_TOKENS) -> int:
    """max_completion_tokens ≈ 3 × source tokens + 256, clamped to [256, 8192]."""
    tokens = source if isinstance(source, int) else estimate_tokens(source)
    return int(min(high, max(low, factor * tokens + extra)))


def strip_think(content: str | None) -> str:
    """Remove leaked <think>…</think> blocks and a stray leading </think>."""
    if not content:
        return ""
    text = content
    if "<think>" in text:
        text = _THINK_BLOCK_RE.sub("", text)
    stripped = text.lstrip()
    if stripped.startswith("</think>"):
        text = stripped[len("</think>"):]
    elif "</think>" in text and "<think>" not in content:
        # reasoning leaked without the opening tag: keep only what follows the marker
        head, _, tail = text.partition("</think>")
        if tail.strip():
            text = tail
    return text.strip()


class ThinkFilter:
    """Incremental version of strip_think for streamed deltas."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._state = "start"  # start | think | lead | pass

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self._state == "pass":
            return chunk
        self._buf += chunk
        return self._drain()

    def _drain(self) -> str:
        while True:
            if self._state == "start":
                stripped = self._buf.lstrip()
                if not stripped:
                    return ""
                if self._OPEN.startswith(stripped) or self._CLOSE.startswith(stripped):
                    return ""  # still ambiguous
                if stripped.startswith(self._OPEN):
                    self._state = "think"
                    self._buf = stripped[len(self._OPEN):]
                    continue
                if stripped.startswith(self._CLOSE):
                    self._state = "lead"
                    self._buf = stripped[len(self._CLOSE):]
                    continue
                self._state = "pass"
                out, self._buf = self._buf, ""
                return out
            if self._state == "think":
                idx = self._buf.find(self._CLOSE)
                if idx < 0:
                    self._buf = self._buf[-(len(self._CLOSE) - 1):]
                    return ""
                self._buf = self._buf[idx + len(self._CLOSE):]
                self._state = "lead"
                continue
            if self._state == "lead":
                stripped = self._buf.lstrip()
                if not stripped:
                    self._buf = ""
                    return ""
                self._state = "pass"
                self._buf = ""
                return stripped
            out, self._buf = self._buf, ""
            return out

    def flush(self) -> str:
        if self._state == "start":
            out, self._buf = self._buf, ""
            self._state = "pass"
            return out
        self._buf = ""
        return ""


# ---------------------------------------------------------------- results
@dataclass(slots=True)
class ChatResult:
    content: str
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str = ""
    reasoning: str = ""


@dataclass(slots=True)
class _StatusCache:
    at: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- JSON helpers
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_json_content(content: str) -> Any:
    text = strip_think(content)
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise
        return json.JSONDecoder().raw_decode(text, start)[0]


def validate_json(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    """Minimal JSON-schema check (type/properties/required/items/min/maxItems/additionalProperties)."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path}: object expected")
        props: dict[str, Any] = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                raise ValueError(f"{path}.{key}: missing")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise ValueError(f"{path}: unexpected keys {sorted(extra)}")
        for key, sub in props.items():
            if key in value:
                validate_json(sub, value[key], f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path}: array expected")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path}: too many items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                validate_json(items, item, f"{path}[{index}]")
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path}: string expected")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: integer expected")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: number expected")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path}: boolean expected")


# ---------------------------------------------------------------- images
MAX_IMAGE_PIXELS = 2_400_000
MAX_IMAGE_SIDE = 2000
_PASSTHROUGH_MIME = {"image/png", "image/jpeg"}


def prepare_image(data: bytes, mime: str | None = None) -> tuple[bytes, str]:
    """Downscale to ≤ 2.4 MP / longest side ≤ 2000 px; return (bytes, mime) as PNG or JPEG."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - Pillow is a runtime dependency
        raise VisionUnavailable("이미지 처리 모듈(Pillow)을 불러오지 못했습니다.") from exc

    mime = (mime or "").lower().split(";")[0].strip()
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            width, height = probe.size
            try:
                orientation = int(probe.getexif().get(0x0112, 1) or 1)
            except Exception:  # noqa: BLE001 - broken EXIF: treat as upright
                orientation = 1
    except Exception as exc:  # noqa: BLE001 - any decoder error
        raise ValueError("이미지를 읽을 수 없습니다.") from exc

    detected_mime = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(fmt, "")
    fits = width * height <= MAX_IMAGE_PIXELS and max(width, height) <= MAX_IMAGE_SIDE
    # the model server does not apply EXIF rotation: rotated photos are always re-encoded upright
    if fits and detected_mime in _PASSTHROUGH_MIME and orientation == 1:
        return data, detected_mime

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img.load()
            width, height = img.size  # after rotation (a portrait photo stored landscape + EXIF 6)
            scale = min(1.0, (MAX_IMAGE_PIXELS / float(width * height)) ** 0.5, MAX_IMAGE_SIDE / float(max(width, height)))
            if scale < 1.0:
                size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(size, Image.Resampling.LANCZOS)
            photo = fmt == "JPEG" or detected_mime == "image/jpeg"
            if img.mode in ("RGBA", "LA", "P") and not photo:
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            elif img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            if photo:
                img.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
                return out.getvalue(), "image/jpeg"
            img.save(out, format="PNG", optimize=True)
            png = out.getvalue()
            if len(png) > 4 * 1024 * 1024:  # photographic content: JPEG is far smaller
                out = io.BytesIO()
                img.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
                return out.getvalue(), "image/jpeg"
            return png, "image/png"
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError("이미지를 읽을 수 없습니다.") from exc


def image_data_uri(data: bytes, mime: str | None = None) -> str:
    payload, out_mime = prepare_image(data, mime)
    return f"data:{out_mime};base64,{base64.b64encode(payload).decode('ascii')}"


def image_part(data_uri: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_uri}}


# ---------------------------------------------------------------- metrics
_METRIC_NAMES = {
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:kv_cache_usage_perc": "kv_cache",
}


def parse_metrics(text: str) -> dict[str, float]:
    """Sum label variants of the vLLM load gauges (ignores *_by_reason and other metrics)."""
    sums: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[{\s]", line, maxsplit=1)[0]
        key = _METRIC_NAMES.get(name)
        if key is None:
            continue
        try:
            value = float(line.rsplit(None, 1)[1])
        except (IndexError, ValueError):
            continue
        if key == "kv_cache":
            sums[key] = max(sums.get(key, 0.0), value)
        else:
            sums[key] = sums.get(key, 0.0) + value
    return sums


# ---------------------------------------------------------------- client
_CONNECT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


def _messages_text(messages: Iterable[dict[str, Any]]) -> tuple[str, int]:
    parts: list[str] = []
    images = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif part.get("type") in ("image_url", "image"):
                    images += 1
    return "\n".join(parts), images


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:500]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        for key in ("message", "detail"):
            if isinstance(data.get(key), str):
                return data[key]
    return json.dumps(data, ensure_ascii=False)[:500]


class LLMClient:
    """OpenAI-compatible client with model discovery, status/metrics, limiter and retries."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")
        self.root_url = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._model: str | None = settings.llm_model.strip() or None
        self._max_model_len: int | None = None
        self._models_checked = False
        self._discovery_lock: asyncio.Lock | None = None
        self._vision_rejected = False
        self._status_cache = _StatusCache()
        self._status_task: asyncio.Task[dict[str, Any]] | None = None
        self._load: tuple[float, int | None, int | None] = (0.0, None, None)
        self._poller: asyncio.Task[None] | None = None
        self._closed = False
        self.retry_backoff: tuple[float, ...] = (1.0, 3.0)
        self.poll_interval = 3.0
        self.status_ttl = 5.0
        self.status_timeout = 2.5
        self.limiter = PriorityLimiter(
            settings.llm_max_parallel,
            settings.llm_doc_parallel,
            on_demand=self._ensure_poller,
        )

    # ------------------------------------------------------------------ plumbing
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            timeout = httpx.Timeout(self.settings.llm_timeout, connect=5.0)
            self._client = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                transport=self._transport,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self.settings.llm_api_key.strip()
        if key and key.upper() != "EMPTY":
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def aclose(self) -> None:
        self._closed = True
        tasks = [t for t in (self._poller, self._status_task) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001 - shutting down
                pass
        self._poller = None
        self._status_task = None
        self._discovery_lock = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._closed = False

    # ------------------------------------------------------------------ properties
    @property
    def model_name(self) -> str | None:
        return self._model

    @property
    def max_model_len(self) -> int | None:
        return self._max_model_len

    @property
    def vision_enabled(self) -> bool:
        mode = self.settings.llm_vision
        if mode == "off":
            return False
        if mode == "on":
            return True
        return not self._vision_rejected

    # ------------------------------------------------------------------ discovery
    async def fetch_models(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        request_timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0)) if timeout else httpx.Timeout(30.0, connect=5.0)
        try:
            response = await self._http().get(f"{self.base_url}/models", headers=self._headers(), timeout=request_timeout)
        except httpx.TimeoutException as exc:
            raise LLMUnavailable("모델 서버 응답 시간이 초과되었습니다.") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable() from exc
        if response.status_code >= 400:
            raise LLMUnavailable(f"모델 서버 응답 오류 (HTTP {response.status_code})", status=response.status_code)
        try:
            data = response.json().get("data") or []
        except (ValueError, AttributeError) as exc:
            raise LLMUnavailable("모델 서버 응답을 해석하지 못했습니다.") from exc
        models = [item for item in data if isinstance(item, dict) and item.get("id")]
        self._remember_models(models)
        return models

    def _remember_models(self, models: list[dict[str, Any]]) -> None:
        self._models_checked = True
        if not models:
            return
        configured = self.settings.llm_model.strip()
        chosen = next((m for m in models if m.get("id") == configured), None) if configured else None
        if chosen is None and not configured:
            chosen = models[0]
            self._model = str(chosen["id"])
        if chosen is not None and isinstance(chosen.get("max_model_len"), int):
            self._max_model_len = int(chosen["max_model_len"])

    async def model(self, *, refresh: bool = False) -> str:
        """Model id to use: LLM_MODEL, or the first id served by GET /models (cached)."""
        configured = self.settings.llm_model.strip()
        if configured:
            if not self._models_checked:
                # learn max_model_len once; a failure here must not block the request itself
                self._models_checked = True
                try:
                    await self.fetch_models(timeout=5.0)
                except LLMError:
                    pass
            return configured
        if self._model and not refresh:
            return self._model
        if self._discovery_lock is None:
            self._discovery_lock = asyncio.Lock()
        known = self._model
        async with self._discovery_lock:
            if self._model and self._model != known:
                return self._model  # another task refreshed it meanwhile
            if self._model and not refresh:
                return self._model
            self._model = None
            await self.fetch_models(timeout=10.0)
            if not self._model:
                raise LLMUnavailable("모델 서버에 제공 중인 모델이 없습니다.")
            return self._model

    # ------------------------------------------------------------------ status / metrics
    async def health(self, *, timeout: float = 2.0) -> bool:
        """GET {root}/health → True when the server answers 200 (never raises)."""
        try:
            response = await self._http().get(
                f"{self.root_url}/health",
                headers={k: v for k, v in self._headers().items() if k == "Authorization"},
                timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def fetch_metrics(self, *, timeout: float = 2.0) -> dict[str, float] | None:
        try:
            response = await self._http().get(
                f"{self.root_url}/metrics",
                headers={k: v for k, v in self._headers().items() if k == "Authorization"},
                timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        values = parse_metrics(response.text)
        if "running" not in values and "waiting" not in values:
            return None
        running = int(values.get("running", 0))
        waiting = int(values.get("waiting", 0))
        self._load = (time.monotonic(), running, waiting)
        return values

    async def status(self, *, force: bool = False) -> dict[str, Any]:
        """{"connected","name","max_model_len","vision","running","waiting","error"} — never raises."""
        now = time.monotonic()
        cache = self._status_cache
        if not force and cache.data and now - cache.at < self.status_ttl:
            return self._with_fresh_load(dict(cache.data))
        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.ensure_future(self._probe_status())
        task = self._status_task
        try:
            data = await asyncio.wait_for(asyncio.shield(task), timeout=self.status_timeout)
        except asyncio.TimeoutError:
            data = self._down_status("모델 서버 응답 시간이 초과되었습니다.")
            self._status_cache = _StatusCache(at=time.monotonic(), data=data)
        except Exception as exc:  # noqa: BLE001 - status must never raise
            logger.debug("status probe failed: %s", exc)
            data = self._down_status("모델 서버 상태를 확인하지 못했습니다.")
        return dict(data)

    def _with_fresh_load(self, data: dict[str, Any]) -> dict[str, Any]:
        at, running, waiting = self._load
        if data.get("connected") and at and time.monotonic() - at < self.status_ttl:
            data["running"], data["waiting"] = running, waiting
        return data

    def _down_status(self, error: str) -> dict[str, Any]:
        return {
            "connected": False,
            "name": self._model or (self.settings.llm_model.strip() or None),
            "max_model_len": self._max_model_len,
            "vision": self.vision_enabled,
            "running": None,
            "waiting": None,
            "error": error,
        }

    async def _probe_status(self) -> dict[str, Any]:
        models_result, metrics_result = await asyncio.gather(
            self.fetch_models(timeout=2.0),
            self.fetch_metrics(timeout=2.0),
            return_exceptions=True,
        )
        if isinstance(models_result, BaseException):
            message = str(models_result) if isinstance(models_result, LLMError) else LLMUnavailable.default_message
            data = self._down_status(message)
        else:
            name = self.settings.llm_model.strip() or self._model
            served = [str(m.get("id")) for m in models_result]
            error = None
            if not served:
                error = "모델 서버에 제공 중인 모델이 없습니다."
            elif self.settings.llm_model.strip() and self.settings.llm_model.strip() not in served:
                error = f"설정한 모델({self.settings.llm_model.strip()})이 서버에 없습니다: {', '.join(served)}"
            metrics = metrics_result if isinstance(metrics_result, dict) else None
            data = {
                "connected": bool(served) and error is None,
                "name": name,
                "max_model_len": self._max_model_len,
                "vision": self.vision_enabled,
                "running": int(metrics["running"]) if metrics and "running" in metrics else None,
                "waiting": int(metrics["waiting"]) if metrics and "waiting" in metrics else None,
                "error": error,
            }
        self._status_cache = _StatusCache(at=time.monotonic(), data=data)
        return data

    # ------------------------------------------------------------------ load poller
    def _ensure_poller(self) -> None:
        if self._closed or (self._poller is not None and not self._poller.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._poller = loop.create_task(self._poll_load())

    async def _poll_load(self) -> None:
        try:
            while True:
                metrics = await self.fetch_metrics(timeout=2.0)
                waiting = int(metrics.get("waiting", 0)) if metrics else 0
                if waiting > 0 and not self.limiter.congested:
                    logger.info("모델 서버 대기열 %d건: 번역기 동시 요청을 1건으로 줄입니다.", waiting)
                self.limiter.set_congested(waiting > 0)
                await asyncio.sleep(self.poll_interval)
                if self.limiter.idle:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the poller must never break requests
            logger.exception("load poller failed")
        finally:
            self.limiter.set_congested(False)

    # ------------------------------------------------------------------ request bodies
    def build_body(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        sampling: Sampling,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        body.update(sampling.as_body())
        body["max_completion_tokens"] = int(max_tokens)
        body["repetition_detection"] = dict(REPETITION_DETECTION)
        body["chat_template_kwargs"] = {"enable_thinking": False}
        if response_format is not None:
            body["response_format"] = response_format
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        for key, value in (self.settings.llm_extra_body or {}).items():
            if key in _PROTECTED_KEYS:
                continue
            if key == "chat_template_kwargs" and isinstance(value, dict):
                body[key] = {**value, "enable_thinking": False}  # thinking stays off (SPEC §6a)
            else:
                body[key] = value
        for key in list(body):
            if key in _FORBIDDEN_KEYS or key.startswith("guided_"):
                body.pop(key)
        if not body.get("stream"):
            body.pop("stream_options", None)
        return body

    def _fit_max_tokens(self, max_tokens: int, messages: list[dict[str, Any]]) -> int:
        limit = self._max_model_len
        if not limit:
            return max_tokens
        text, images = _messages_text(messages)
        prompt = estimate_tokens(text) + images * IMAGE_TOKEN_ESTIMATE + 64
        return max(64, min(max_tokens, limit - prompt))

    # ------------------------------------------------------------------ requests
    def _http_error(self, response: httpx.Response, *, has_images: bool) -> LLMError:
        status = response.status_code
        message = _error_message(response)
        lowered = message.lower()
        if status in (400, 422) and has_images and ("image" in lowered or "multimodal" in lowered or "vision" in lowered):
            if self.settings.llm_vision == "auto":
                self._vision_rejected = True
            return VisionUnavailable(status=status)
        if status in (400, 413) and ("context length" in lowered or "too long" in lowered or "max_model_len" in lowered
                                     or "maximum context" in lowered or "too many tokens" in lowered):
            return LLMInputTooLong(status=status)
        if status == 401 or status == 403:
            return LLMError("모델 서버 인증에 실패했습니다 (LLM_API_KEY 확인).", status=status)
        if status == 404:
            return LLMError(f"모델 서버에서 모델을 찾지 못했습니다: {message[:200]}", status=status)
        if status in (429, 503):
            return LLMUnavailable("모델 서버가 바쁩니다. 잠시 후 다시 시도하세요.", status=status)
        if status in (502, 504):
            return LLMUnavailable(status=status)
        if 400 <= status < 500:
            return LLMError(f"모델 서버가 요청을 거부했습니다: {message[:200]}", status=status)
        return LLMError(f"모델 서버 오류 (HTTP {status})", status=status)

    @staticmethod
    def _is_model_missing(response: httpx.Response) -> bool:
        return response.status_code == 404 and "does not exist" in _error_message(response).lower()

    async def _backoff(self, attempt: int) -> None:
        delays = self.retry_backoff or (0.0,)
        await asyncio.sleep(delays[min(attempt, len(delays)) - 1])

    async def _post(self, body: dict[str, Any], *, priority: str, has_images: bool) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        attempt = 0
        rediscovered = False
        while True:
            try:
                async with self.limiter.slot(priority):  # type: ignore[arg-type]
                    response = await self._http().post(url, json=body, headers=self._headers())
            except httpx.ReadTimeout as exc:
                raise LLMError("모델 응답 시간이 초과되었습니다.") from exc
            except _CONNECT_ERRORS as exc:
                if attempt < len(self.retry_backoff):
                    attempt += 1
                    logger.warning("모델 서버 연결 실패, 다시 시도합니다 (%d): %s", attempt, exc)
                    await self._backoff(attempt)
                    continue
                raise LLMUnavailable() from exc
            except httpx.HTTPError as exc:
                raise LLMUnavailable(f"모델 서버 통신 오류: {exc.__class__.__name__}") from exc

            if response.status_code in (429, 502, 503, 504) and attempt < len(self.retry_backoff):
                attempt += 1
                logger.warning("모델 서버 응답 HTTP %d, 다시 시도합니다 (%d)", response.status_code, attempt)
                await self._backoff(attempt)
                continue
            if self._is_model_missing(response) and not self.settings.llm_model.strip() and not rediscovered:
                rediscovered = True
                body["model"] = await self.model(refresh=True)
                continue
            if response.status_code >= 400:
                raise self._http_error(response, has_images=has_images)
            try:
                data = response.json()
            except ValueError as exc:
                raise LLMOutputError("모델 서버 응답을 해석하지 못했습니다.") from exc
            if not isinstance(data, dict):
                raise LLMOutputError("모델 서버 응답을 해석하지 못했습니다.")
            if isinstance(data.get("error"), dict):
                raise LLMError(f"모델 서버 오류: {str(data['error'].get('message', ''))[:200]}")
            return data

    @staticmethod
    def _parse_choice(data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise LLMOutputError("모델 서버 응답에 결과가 없습니다.")
        choice = choices[0]
        message = choice.get("message") or {}
        raw = message.get("content")
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        content = strip_think(raw if isinstance(raw, str) else "")
        if not content and isinstance(reasoning, str) and reasoning.strip():
            raise LLMError("모델 설정 오류: 생각 모드가 켜져 있습니다")
        return ChatResult(
            content=content,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            model=str(data.get("model") or ""),
            reasoning=reasoning if isinstance(reasoning, str) else "",
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        sampling: Sampling,
        max_tokens: int,
        priority: str = "interactive",
        response_format: dict[str, Any] | None = None,
        retry_truncated: bool = True,
        max_tokens_cap: int = MAX_COMPLETION_TOKENS,
    ) -> ChatResult:
        """Non-streaming chat completion. finish_reason length/repetition → one retry with presence_penalty 1.0."""
        _, images = _messages_text(messages)
        model = await self.model()
        tokens = self._fit_max_tokens(max_tokens, messages)
        body = self.build_body(messages, model=model, sampling=sampling, max_tokens=tokens, response_format=response_format)
        result = self._parse_choice(await self._post(body, priority=priority, has_images=images > 0))
        if retry_truncated and result.finish_reason in RETRY_FINISH_REASONS:
            logger.info("모델 출력이 끊김(%s): presence_penalty 1.0 으로 다시 요청합니다", result.finish_reason)
            bigger = min(max_tokens_cap, tokens * 2) if result.finish_reason == "length" else tokens
            retry_body = self.build_body(
                messages,
                model=body["model"],
                sampling=sampling.with_(presence_penalty=1.0),
                max_tokens=self._fit_max_tokens(max(bigger, tokens), messages),
                response_format=response_format,
            )
            result = self._parse_choice(await self._post(retry_body, priority=priority, has_images=images > 0))
        return result

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        sampling: Sampling,
        max_tokens: int,
        priority: str = "interactive",
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas. Closing the generator closes the upstream response (vLLM aborts)."""
        if meta is None:
            meta = {}
        _, images = _messages_text(messages)
        model = await self.model()
        body = self.build_body(
            messages,
            model=model,
            sampling=sampling,
            max_tokens=self._fit_max_tokens(max_tokens, messages),
            stream=True,
        )
        url = f"{self.base_url}/chat/completions"
        attempt = 0
        rediscovered = False
        while True:
            emitted = False
            retry = False
            try:
                async with self.limiter.slot(priority):  # type: ignore[arg-type]
                    async with self._http().stream("POST", url, json=body, headers=self._headers()) as response:
                        if response.status_code >= 400:
                            await response.aread()
                            if response.status_code in (429, 502, 503, 504) and attempt < len(self.retry_backoff):
                                retry = True
                            elif self._is_model_missing(response) and not self.settings.llm_model.strip() and not rediscovered:
                                retry = True
                                rediscovered = True
                                body["model"] = None  # resolved below, outside the slot
                            else:
                                raise self._http_error(response, has_images=images > 0)
                        else:
                            think = ThinkFilter()
                            saw_reasoning = False
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                payload = line[5:].strip()
                                if not payload:
                                    continue
                                if payload == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(payload)
                                except ValueError:
                                    continue
                                if isinstance(chunk.get("error"), dict):
                                    raise LLMError(f"모델 서버 오류: {str(chunk['error'].get('message', ''))[:200]}")
                                if chunk.get("usage"):
                                    meta["usage"] = chunk["usage"]
                                for choice in chunk.get("choices") or []:
                                    delta = choice.get("delta") or {}
                                    if delta.get("reasoning") or delta.get("reasoning_content"):
                                        saw_reasoning = True
                                    text = delta.get("content")
                                    if text:
                                        out = think.feed(text)
                                        if out:
                                            emitted = True
                                            yield out
                                    if choice.get("finish_reason"):
                                        meta["finish_reason"] = choice["finish_reason"]
                            tail = think.flush()
                            if tail:
                                emitted = True
                                yield tail
                            if not emitted and saw_reasoning:
                                raise LLMError("모델 설정 오류: 생각 모드가 켜져 있습니다")
            except httpx.ReadTimeout as exc:
                raise LLMError("모델 응답 시간이 초과되었습니다.") from exc
            except _CONNECT_ERRORS as exc:
                if emitted or attempt >= len(self.retry_backoff):
                    raise LLMUnavailable() from exc
                retry = True
            except httpx.HTTPError as exc:
                raise LLMUnavailable(f"모델 서버 통신 오류: {exc.__class__.__name__}") from exc
            if not retry:
                return
            if body["model"] is None:
                body["model"] = await self.model(refresh=True)
                continue
            attempt += 1
            await self._backoff(attempt)

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any],
        name: str,
        max_tokens: int,
        sampling: Sampling = JSON_SAMPLING,
        priority: str = "interactive",
        attempts: int = 1,
    ) -> Any:
        """Structured output via response_format json_schema; parsed and validated."""
        response_format = {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}
        error: LLMError = LLMOutputError()
        for _ in range(max(1, attempts)):
            result = await self.chat(
                messages,
                sampling=sampling,
                max_tokens=max_tokens,
                priority=priority,
                response_format=response_format,
                retry_truncated=False,
            )
            if result.finish_reason in RETRY_FINISH_REASONS:
                error = LLMOutputError("모델 출력이 잘렸습니다.")
                continue
            try:
                value = parse_json_content(result.content)
                validate_json(schema, value)
                return value
            except ValueError as exc:
                logger.info("structured output rejected: %s", exc)
                error = LLMOutputError()
        raise error
