#!/usr/bin/env python3
"""개발·시험용 가짜 vLLM 서버 (OpenAI 호환, 표준 라이브러리만 사용).

실제 모델 없이 번역기의 연결·상태 표시·스트리밍·구조화 출력·문서 처리 흐름을 시험한다.
번역 결과는 "[대상언어] 원문" 형태의 가짜 번역이며, 원문의 <gN>..</gN>, <xN/> 태그를 그대로 둔다.

    python dev/fake_vllm.py --host 0.0.0.0 --port 8000 [--delay-ms 20] [--fail-rate 0.1]

지원: GET /health, GET /v1/models, GET /metrics, POST /v1/chat/completions (stream 포함),
      GET /fake/stats (시험용 요청 수)
요청 내용은 기록하지 않는다. 표준 오류에 요청 건수만 찍는다.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OCR_TEXT = "[OCR] 가짜 이미지 텍스트"
TARGET_RE = re.compile(r"Target language\s*:\s*([^\n]+)", re.IGNORECASE)
CODE_RE = re.compile(r"\(([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)?)\)")
NUMBERED_RE = re.compile(r"^\s*\[?(\d+)[\].):]\s+(.*\S)\s*$")
TOKEN_RE = re.compile(r"\S+\s*|\s+")


# ------------------------------------------------------------------ 상태
class State:
    def __init__(self, args: argparse.Namespace) -> None:
        self.model = args.model
        self.max_model_len = args.max_model_len
        self.delay = max(0.0, args.delay_ms) / 1000.0
        self.fail_rate = args.fail_rate
        self.think = args.think
        self.rng = random.Random(args.seed)
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max(1, args.max_seqs))
        self.running = 0
        self.waiting = 0
        self.requests = 0
        self.failures = 0

    def should_fail(self) -> bool:
        with self.lock:
            return self.fail_rate > 0 and self.rng.random() < self.fail_rate


# ------------------------------------------------------------------ 가짜 번역
def content_text(content) -> tuple[str, bool]:
    """메시지 content(문자열 또는 parts 목록)에서 글자와 이미지 포함 여부를 꺼낸다."""
    if isinstance(content, str):
        return content, False
    texts: list[str] = []
    image = False
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                texts.append(str(part.get("text", "")))
            elif kind in ("image_url", "input_image", "image"):
                image = True
    return "\n".join(texts), image


def target_label(messages: list) -> str:
    for m in reversed(messages):
        text, _ = content_text(m.get("content") if isinstance(m, dict) else "")
        found = TARGET_RE.search(text)
        if not found:
            continue
        value = found.group(1).strip()
        code = CODE_RE.search(value)
        if code:
            return code.group(1).upper()
        word = value.split()[0].strip(".,;:") if value.split() else ""
        if 2 <= len(word) <= 3 and word.isalpha():
            return word.upper()
        return word[:16] or "KO"
    return "KO"


def embedded_json(text: str) -> list:
    """글 안에 들어 있는 JSON 객체·배열을 모두 찾는다."""
    dec = json.JSONDecoder()
    found = []
    i, n = 0, len(text)
    while i < n:
        if text[i] in "[{":
            try:
                value, end = dec.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            found.append(value)
            i = end
            continue
        i += 1
    return found


def first_text(obj: dict) -> str | None:
    for key in ("text", "source", "src", "s", "content"):
        if isinstance(obj.get(key), str):
            return obj[key]
    for value in obj.values():
        if isinstance(value, str):
            return value
    return None


def collect_lists(value, lists: list, maps: list) -> None:
    if isinstance(value, list):
        if value and all(isinstance(v, str) for v in value):
            lists.append(list(value))
        elif value and all(isinstance(v, dict) for v in value):
            texts = [first_text(v) for v in value]
            if all(t is not None for t in texts):
                lists.append(texts)
        for v in value:
            collect_lists(v, lists, maps)
    elif isinstance(value, dict):
        values = list(value.values())
        if len(values) > 1 and all(isinstance(v, str) for v in values):
            maps.append(values)
        for v in values:
            collect_lists(v, lists, maps)


class Context:
    """한 요청의 가짜 번역 재료."""

    def __init__(self, messages: list, label: str, text: str, image: bool) -> None:
        self.label = label
        self.image = image
        self.text = text
        lists: list = []
        maps: list = []
        for value in embedded_json(text):
            collect_lists(value, lists, maps)
        numbered = [m.group(2) for m in (NUMBERED_RE.match(line) for line in text.splitlines()) if m]
        self.candidates = lists + maps + ([numbered] if numbered else [])

    def tr(self, s: str) -> str:
        if self.image:
            return OCR_TEXT
        return f"[{self.label}] {s}"

    def sources(self, n: int | None) -> list[str]:
        if n is None:
            return list(self.candidates[0]) if self.candidates else [self.text]
        for cand in self.candidates:
            if len(cand) == n:
                return list(cand)
        return [f"{self.text} ({i + 1})" if n > 1 else self.text for i in range(n)]


def resolve(schema, root: dict, depth: int = 0) -> dict:
    if not isinstance(schema, dict) or depth > 20:
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node = root
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return resolve(node, root, depth + 1)
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            options = [resolve(s, root, depth + 1) for s in schema[key]]
            options = [o for o in options if o.get("type") != "null"]
            if options:
                return options[0]
    if isinstance(schema.get("allOf"), list) and len(schema["allOf"]) == 1:
        return resolve(schema["allOf"][0], root, depth + 1)
    return schema


def build(schema, root: dict, ctx: Context, source: str | None = None, index: int = 0, name: str = ""):
    """JSON 스키마에 맞는 값을 만든다. 배열 길이(minItems/maxItems)를 지킨다."""
    s = resolve(schema, root)
    if "const" in s:
        return s["const"]
    if s.get("enum"):
        return s["enum"][0]
    kind = s.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "object" or (kind is None and "properties" in s):
        return {k: build(v, root, ctx, source, index, k) for k, v in (s.get("properties") or {}).items()}
    if kind == "array":
        lo, hi = s.get("minItems"), s.get("maxItems")
        n = lo if lo is not None else hi
        if lo is not None and hi is not None and lo != hi:
            n = None
        items = s.get("items", {"type": "string"})
        if source is not None and n is None:
            srcs = [source]
        else:
            srcs = ctx.sources(n)
            if n is None and lo is not None:
                srcs = (srcs + [ctx.text] * lo)[: max(lo, len(srcs))]
            if hi is not None:
                srcs = srcs[:hi]
        return [build(items, root, ctx, src, i, name) for i, src in enumerate(srcs)]
    if kind == "integer":
        return index if name.lower() in ("id", "index", "idx", "i", "n") else int(s.get("minimum", 0))
    if kind == "number":
        return float(s.get("minimum", 0))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    low = name.lower()
    if low.endswith("lang") or low.endswith("language"):
        return "en"
    return ctx.tr(source if source is not None else ctx.text)


def schema_of(body: dict):
    rf = body.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_schema":
            js = rf.get("json_schema") or {}
            return js.get("schema", js) if isinstance(js, dict) else {}
        if rf.get("type") == "json_object":
            return {"type": "object", "properties": {"translation": {"type": "string"}}}
    so = body.get("structured_outputs")
    if isinstance(so, dict) and so.get("json") is not None:
        js = so["json"]
        return json.loads(js) if isinstance(js, str) else js
    if body.get("guided_json") is not None:
        js = body["guided_json"]
        return json.loads(js) if isinstance(js, str) else js
    return None


def answer(body: dict) -> tuple[str, bool]:
    """가짜 답과 구조화 출력 여부."""
    messages = body.get("messages") or []
    last_text, image = "", False
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_text, image = content_text(m.get("content"))
            break
    ctx = Context(messages, target_label(messages), last_text, image)
    schema = schema_of(body)
    if schema is not None:
        return json.dumps(build(schema, schema, ctx), ensure_ascii=False), True
    return ctx.tr(last_text), False


# ------------------------------------------------------------------ HTTP
def error_body(message: str, kind: str, code: int) -> dict:
    return {"object": "error", "message": message, "type": kind, "param": None, "code": code}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-vllm/1.0"

    def log_message(self, format, *args):  # noqa: A002 - 접속 기록을 남기지 않는다
        return

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, obj) -> None:
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        st = self.state
        if path == "/health":
            self.send_bytes(200, b"", "application/json")
        elif path in ("/v1/models", "/models"):
            self.send_json(200, {"object": "list", "data": [{
                "id": st.model, "object": "model", "created": int(time.time()), "owned_by": "vllm",
                "root": "fake", "parent": None, "max_model_len": st.max_model_len}]})
        elif path == "/metrics":
            with st.lock:
                running, waiting = st.running, st.waiting
            lbl = f'engine="0",model_name="{st.model}"'
            text = (
                "# HELP vllm:num_requests_running Number of requests in model execution batches.\n"
                "# TYPE vllm:num_requests_running gauge\n"
                f"vllm:num_requests_running{{{lbl}}} {float(running)}\n"
                "# HELP vllm:num_requests_waiting Number of requests waiting to be processed.\n"
                "# TYPE vllm:num_requests_waiting gauge\n"
                f"vllm:num_requests_waiting{{{lbl}}} {float(waiting)}\n"
            )
            self.send_bytes(200, text.encode(), "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/version":
            self.send_json(200, {"version": "fake"})
        elif path == "/fake/stats":
            with st.lock:
                stats = {"requests": st.requests, "failures": st.failures, "running": st.running, "waiting": st.waiting}
            self.send_json(200, stats)
        else:
            self.send_json(404, {"detail": "Not Found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_json(404, {"detail": "Not Found"})
            return
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
                raise ValueError("messages")
        except ValueError:
            self.send_json(400, error_body("잘못된 요청 본문", "BadRequestError", 400))
            return
        st = self.state
        with st.lock:
            st.requests += 1
            count = st.requests
        stream = bool(body.get("stream"))
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} 요청 {count}건째 (stream={int(stream)}, "
                         f"schema={int(schema_of(body) is not None)})\n")
        sys.stderr.flush()
        model = body.get("model")
        if model and model != st.model:
            self.send_json(404, error_body(f"The model `{model}` does not exist.", "NotFoundError", 404))
            return
        if st.should_fail():
            with st.lock:
                st.failures += 1
            self.send_json(500, error_body("가짜 서버의 일부러 낸 오류", "InternalServerError", 500))
            return

        with st.lock:
            st.waiting += 1
        st.slots.acquire()
        with st.lock:
            st.waiting -= 1
            st.running += 1
        try:
            self.complete(body, stream)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        finally:
            with st.lock:
                st.running -= 1
            st.slots.release()

    def complete(self, body: dict, stream: bool) -> None:
        st = self.state
        text, structured = answer(body)
        if st.think:
            text = "<think>\n\n</think>\n\n" + text
        tokens = TOKEN_RE.findall(text) or [""]
        finish = "stop"
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
        if isinstance(max_tokens, int) and 0 < max_tokens < len(tokens):
            tokens, finish = tokens[:max_tokens], "length"
        n = max(1, int(body.get("n") or 1))
        prompt_chars = sum(len(content_text(m.get("content"))[0]) for m in body["messages"] if isinstance(m, dict))
        usage = {"prompt_tokens": prompt_chars // 4 + 1, "completion_tokens": len(tokens) * n,
                 "total_tokens": prompt_chars // 4 + 1 + len(tokens) * n}
        rid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        base = "".join(tokens)
        # n 개를 달라고 하면 서로 다른 답을 준다 (구조화 출력·잘린 답은 그대로)
        variants = [base if i == 0 or structured or finish != "stop" else f"{base} ({i + 1})" for i in range(n)]

        if not stream:
            time.sleep(st.delay * len(tokens))
            self.send_json(200, {
                "id": rid, "object": "chat.completion", "created": created, "model": st.model,
                "choices": [{"index": i, "message": {"role": "assistant", "content": v, "reasoning_content": None},
                             "logprobs": None, "finish_reason": finish} for i, v in enumerate(variants)],
                "usage": usage})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(obj) -> None:
            data = b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()) + b"\n\n"
            self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def chunk(i: int, delta: dict, reason=None) -> dict:
            return {"id": rid, "object": "chat.completion.chunk", "created": created, "model": st.model,
                    "choices": [{"index": i, "delta": delta, "logprobs": None, "finish_reason": reason}]}

        for i, v in enumerate(variants):
            emit(chunk(i, {"role": "assistant", "content": ""}))
            for piece in TOKEN_RE.findall(v):
                if st.delay:
                    time.sleep(st.delay)
                emit(chunk(i, {"content": piece}))
            emit(chunk(i, {}, finish))
        if (body.get("stream_options") or {}).get("include_usage"):
            emit({"id": rid, "object": "chat.completion.chunk", "created": created, "model": st.model,
                  "choices": [], "usage": usage})
        emit(b"[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="가짜 vLLM 서버 (개발·시험용)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default="Fake-Qwen", help="모델 이름 (/v1/models)")
    ap.add_argument("--max-model-len", type=int, default=32768)
    ap.add_argument("--delay-ms", type=float, default=0.0, help="토큰 조각마다 기다리는 시간")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="일부러 500 오류를 낼 비율 (0~1)")
    ap.add_argument("--max-seqs", type=int, default=8, help="동시에 처리하는 요청 수 (넘으면 대기)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--think", action="store_true", help="답 앞에 빈 <think></think> 를 붙인다")
    return ap.parse_args(argv)


def make_server(args: argparse.Namespace) -> Server:
    srv = Server((args.host, args.port), Handler)
    srv.state = State(args)  # type: ignore[attr-defined]
    return srv


def main(argv=None) -> None:
    args = parse_args(argv)
    srv = make_server(args)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    host, port = srv.server_address[:2]
    sys.stderr.write(f"가짜 vLLM 서버: http://{host}:{port} (모델 {args.model})\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
