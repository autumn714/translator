"""번역기 동작 시험 — 화면이 쓰는 HTTP API 만 불러 본다 (표준 라이브러리 + PyMuPDF).

    bash run.sh selftest
    (= docker exec translator-app python -m translator_app.selftest)

    --base URL     시험할 번역기 주소 (기본 http://127.0.0.1:7860)
    --skip-docs    문서 번역 시험 생략
    --timeout 초   문서 하나를 기다리는 최대 시간 (기본 600)
    --probe        모델 서버 상태 한 줄만 출력 (bash run.sh status 가 쓴다)

공개해도 되는 짧은 문장과 이 파일이 만드는 작은 문서만 쓰고, 시험이 끝나면 올린 문서를 지운다.
로그인이 켜져 있으면 컨테이너 환경 변수 UI_USER / UI_PASSWORD 로 로그인한다.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile

EN_TEXT = "The electrolyzer produces hydrogen at a pressure of 30 bar."
KO_TEXT = "수소 저장 탱크는 매일 압력을 점검한다."
STREAM_TEXT = (
    "Hydrogen is the lightest element in the universe.\n\n"
    "It can be produced from water by electrolysis.\n\n"
    "Fuel cells convert hydrogen into electricity."
)
DOC_LINES = [
    "Hydrogen safety notes",
    "Store hydrogen cylinders in a well ventilated area.",
    "Check the pressure gauge before every use.",
]
TERMINAL = {"done", "error", "canceled"}
FAKE_KO = "[KO] "   # dev/fake_vllm.py 의 가짜 한국어 번역 표시


# ------------------------------------------------------------------ HTTP
class Client:
    def __init__(self, base: str, timeout: float = 300) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cookies: dict[str, str] = {}

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        h.update(extra or {})
        return h

    def _remember(self, headers) -> None:
        for raw in headers.get_all("Set-Cookie") or []:
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                self.cookies[k.strip()] = v.strip()

    def open(self, method: str, path: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self._headers(headers))
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310 - 주소는 시험 대상 번역기
        except urllib.error.HTTPError as e:
            resp = e
        self._remember(resp.headers)
        return resp

    def call(self, method: str, path: str, body=None, data: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes]:
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", **(headers or {})}
        with self.open(method, path, data, headers) as resp:
            return resp.status if hasattr(resp, "status") else resp.code, resp.read()

    def json(self, method: str, path: str, body=None) -> tuple[int, object]:
        status, raw = self.call(method, path, body)
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw[:200].decode("utf-8", "replace")


def detail(obj) -> str:
    if isinstance(obj, dict) and obj.get("detail"):
        return str(obj["detail"])[:120]
    return str(obj)[:120] if obj else ""


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "selftest" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
              "Content-Type: application/octet-stream\r\n\r\n".encode())
    out.write(content)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


# ------------------------------------------------------------------ 시험 문서
def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buf.getvalue()


def make_docx() -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    para = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in DOC_LINES)
    para += ('<w:p><w:r><w:t xml:space="preserve">The </w:t></w:r><w:r><w:rPr><w:b/></w:rPr>'
             '<w:t>electrolyzer</w:t></w:r><w:r><w:t xml:space="preserve"> splits water into hydrogen and oxygen.</w:t></w:r></w:p>')
    return _zip({
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'),
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{w}"><w:body>{para}'
            '<w:sectPr/></w:body></w:document>'),
    })


def make_xlsx() -> bytes:
    strings = ["Item", "Quantity", "Hydrogen storage tank", "Pressure relief valve"]
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
    rows = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>2</v></c></row>'
            '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>4</v></c></row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    return _zip({
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{ct}.sheet.main+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{ct}.worksheet+xml"/>'
            f'<Override PartName="/xl/sharedStrings.xml" ContentType="{ct}.sharedStrings+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{ns}" xmlns:r="{rel}">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>'),
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{ns}">'
            f'<sheetData>{rows}</sheetData></worksheet>'),
        "xl/sharedStrings.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{ns}" count="{len(strings)}" uniqueCount="{len(strings)}">{sst}</sst>'),
    })


def make_pdf() -> bytes | None:
    try:
        import pymupdf
    except ImportError:
        return None
    doc = pymupdf.open()
    page = doc.new_page()
    y = 90
    for i, line in enumerate(DOC_LINES):
        page.insert_text((72, y), line, fontsize=16 if i == 0 else 11)
        y += 28
    data = doc.tobytes()
    doc.close()
    return data


def make_txt() -> bytes:
    return ("\n\n".join(DOC_LINES) + "\n").encode("utf-8")


# 번역본 확인: 열리는지, 원문과 달라졌는지
def zip_text(data: bytes, member: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(member).decode("utf-8")


def check_zip(member: str, source: bytes):
    def check(data: bytes) -> str | None:
        try:
            out = zip_text(data, member)
        except (zipfile.BadZipFile, KeyError) as e:
            return f"번역본을 열 수 없음: {e}"
        return None if out != zip_text(source, member) else "번역본 내용이 원문과 같음"
    return check


def check_pdf(data: bytes) -> str | None:
    if not data.startswith(b"%PDF"):
        return "PDF 가 아님"
    try:
        import pymupdf
    except ImportError:
        return None
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = " ".join(" ".join(page.get_text().split()) for page in doc)
    # 시험용 가짜 모델(dev/fake_vllm.py)은 "[KO] 원문" 으로 답한다. 이 표시가 붙은 문장은 번역된 것으로 본다
    rest = text
    for line in DOC_LINES:
        rest = rest.replace(FAKE_KO + line, "")
    if any(line in rest for line in DOC_LINES):
        return "번역본에 원문 문장이 그대로 있음"
    if FAKE_KO not in text and not any("가" <= ch <= "힣" for ch in text):
        return "번역본에 한국어가 없음"
    return None


def check_txt(data: bytes) -> str | None:
    text = data.decode("utf-8-sig", "replace")
    return None if text.strip() and text.strip() != make_txt().decode().strip() else "번역본 내용이 원문과 같음"


# ------------------------------------------------------------------ 시험
class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, float | None, str]] = []

    def add(self, name: str, ok: bool | None, seconds: float | None, note: str = "") -> None:
        self.rows.append((name, "PASS" if ok else ("건너뜀" if ok is None else "FAIL"), seconds, note))

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r[1] == "FAIL")


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def pad(s: str, n: int) -> str:
    return s + " " * max(0, n - width(s))


def short(s: str, n: int = 48) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def login_if_needed(c: Client, rep: Report) -> bool:
    status, _ = c.json("GET", "/api/status")
    if status != 401 and os.environ.get("UI_AUTH", "0") != "1":
        return True
    user, pw = os.environ.get("UI_USER", ""), os.environ.get("UI_PASSWORD", "")
    t0 = time.monotonic()
    if not pw:
        rep.add("로그인", False, None, "UI_PASSWORD 환경 변수가 없음")
        return False
    status, body = c.json("POST", "/api/login", {"username": user, "password": pw})
    ok = status in (200, 204)
    rep.add("로그인", ok, time.monotonic() - t0, user if ok else f"HTTP {status} {detail(body)}")
    return ok


def test_status(c: Client, rep: Report) -> str | None:
    t0 = time.monotonic()
    status, body = c.json("GET", "/api/status")
    took = time.monotonic() - t0
    if status != 200 or not isinstance(body, dict):
        rep.add("상태 확인", False, took, f"HTTP {status} {detail(body)}")
        return None
    model = body.get("model") or {}
    name = model.get("name")
    if model.get("connected"):
        load = ""
        if model.get("running") is not None:
            load = f" · 처리 중 {model.get('running')} · 대기 {model.get('waiting')}"
        rep.add("상태 확인", True, took, f"{name}{load}")
    else:
        rep.add("상태 확인", False, took, f"모델 연결 안 됨: {model.get('error') or ''}")
    return name


def test_translate(c: Client, rep: Report, label: str, text: str, src: str, tgt: str) -> None:
    t0 = time.monotonic()
    status, body = c.json("POST", "/api/translate", {"text": text, "source_lang": src, "target_lang": tgt})
    took = time.monotonic() - t0
    out = body.get("translation", "") if isinstance(body, dict) else ""
    if status == 200 and out.strip() and out.strip() != text.strip():
        rep.add(label, True, took, short(out))
    else:
        rep.add(label, False, took, f"HTTP {status} {detail(body) if status != 200 else '번역 결과가 비었거나 원문과 같음'}")


def test_stream(c: Client, rep: Report) -> None:
    t0 = time.monotonic()
    counts: dict[str, int] = {}
    first: float | None = None
    error = ""
    data = json.dumps({"text": STREAM_TEXT, "source_lang": "en", "target_lang": "ko"}).encode()
    try:
        with c.open("POST", "/api/translate/stream", data,
                    {"Content-Type": "application/json", "Accept": "application/x-ndjson"}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} {detail(resp.read().decode('utf-8', 'replace'))}")
            for raw in resp:
                line = raw.strip()
                if not line:
                    continue
                event = json.loads(line)
                kind = event.get("type", "?")
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "delta" and first is None:
                    first = time.monotonic() - t0
                if kind == "error":
                    error = str(event.get("detail", ""))
    except Exception as e:  # noqa: BLE001
        error = error or str(e)
    took = time.monotonic() - t0
    summary = " · ".join(f"{k} {v}" for k, v in counts.items())
    if first is not None:
        summary += f" · 첫 글자 {first:.1f}초"
    ok = not error and counts.get("start") == 1 and counts.get("done") == 1 and counts.get("unit", 0) >= 1
    rep.add("스트리밍 번역", ok, took, summary if ok else short(error or summary or "이벤트 없음", 60))


def test_documents(c: Client, rep: Report, wait: float) -> None:
    docx, xlsx, pdf, txt = make_docx(), make_xlsx(), make_pdf(), make_txt()
    cases = [
        ("문서 DOCX", "selftest.docx", docx, check_zip("word/document.xml", docx)),
        ("문서 XLSX", "selftest.xlsx", xlsx, check_zip("xl/sharedStrings.xml", xlsx)),
        ("문서 PDF", "selftest.pdf", pdf, check_pdf),
        ("문서 TXT", "selftest.txt", txt, check_txt),
    ]
    options = json.dumps({"source_lang": "en", "target_lang": "ko", "output": "translated", "pdf_mode": "layout"})
    jobs: list[dict] = []
    for label, filename, content, check in cases:
        if content is None:
            rep.add(label, None, None, "PyMuPDF 없음")
            continue
        body, ctype = multipart({"options": options}, filename, content)
        t0 = time.monotonic()
        status, raw = c.call("POST", "/api/documents", data=body, headers={"Content-Type": ctype})
        try:
            job = json.loads(raw.decode("utf-8"))
        except ValueError:
            job = {}
        if status not in (200, 201, 202) or not job.get("id"):
            rep.add(label, False, time.monotonic() - t0, f"올리기 실패: HTTP {status} {detail(job)}")
            continue
        jobs.append({"label": label, "id": job["id"], "t0": t0, "check": check, "job": job, "took": None})

    deadline = time.monotonic() + wait
    try:
        while any(j["took"] is None for j in jobs) and time.monotonic() < deadline:
            for j in jobs:
                if j["took"] is not None:
                    continue
                status, body = c.json("GET", f"/api/documents/{j['id']}")
                if status == 200 and isinstance(body, dict):
                    j["job"] = body
                    if body.get("status") in TERMINAL:
                        j["took"] = time.monotonic() - j["t0"]
            time.sleep(1)
        for j in jobs:
            job = j["job"]
            if j["took"] is None:
                rep.add(j["label"], False, time.monotonic() - j["t0"], f"{wait:.0f}초 안에 끝나지 않음 ({job.get('status')})")
                continue
            if job.get("status") != "done":
                rep.add(j["label"], False, j["took"], f"{job.get('status')}: {short(job.get('error') or '', 60)}")
                continue
            status, data = c.call("GET", f"/api/documents/{j['id']}/download")
            problem = f"내려받기 실패: HTTP {status}" if status != 200 else j["check"](data)
            note = f"{job.get('output_filename') or ''} · {len(data):,} 바이트"
            if job.get("warnings"):
                note += f" · 주의 {len(job['warnings'])}건"
            if job.get("report_count"):
                note += f" · 검수 {job['report_count']}건"
            rep.add(j["label"], problem is None, j["took"], note if problem is None else problem)
    finally:
        for j in jobs:
            try:
                c.call("DELETE", f"/api/documents/{j['id']}")
            except Exception:  # noqa: BLE001
                pass


def print_report(base: str, model: str | None, rep: Report, total: float) -> None:
    print(f"\n번역기 동작 시험 — {base}" + (f" · 모델 {model}" if model else ""))
    print()
    print(f"  {pad('항목', 16)}{pad('결과', 8)}{pad('시간', 9)}비고")
    print("  " + "-" * 72)
    for name, result, secs, note in rep.rows:
        t = f"{secs:.1f}초" if secs is not None else "-"
        print(f"  {pad(name, 16)}{pad(result, 8)}{pad(t, 9)}{note}")
    print("  " + "-" * 72)
    passed = sum(1 for r in rep.rows if r[1] == "PASS")
    print(f"  {len(rep.rows)}건 중 통과 {passed} · 실패 {rep.failed} · 전체 {total:.1f}초")
    print("  결과: " + ("통과" if rep.failed == 0 else "실패"))


def run(base: str, skip_docs: bool, wait: float) -> int:
    c = Client(base)
    rep = Report()
    t0 = time.monotonic()
    model = None
    try:
        if login_if_needed(c, rep):
            model = test_status(c, rep)
            test_translate(c, rep, "번역 영→한", EN_TEXT, "en", "ko")
            test_translate(c, rep, "번역 한→영", KO_TEXT, "ko", "en")
            test_stream(c, rep)
            if not skip_docs:
                test_documents(c, rep, wait)
    except (urllib.error.URLError, OSError) as e:
        rep.add("연결", False, None, f"{base} 에 닿지 않음: {e}")
    print_report(base, model, rep, time.monotonic() - t0)
    return 1 if rep.failed else 0


# ------------------------------------------------------------------ 모델 서버 상태 (run.sh status)
def probe() -> int:
    base = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "http://llm:8000/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key and key != "EMPTY" else {}
    try:
        req = urllib.request.Request(base + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - 설정된 모델 서버 주소
            models = json.load(r).get("data") or []
    except Exception as e:  # noqa: BLE001
        print(f"모델 서버 응답 없음: {e}")
        return 1
    if not models:
        print("모델 서버에 모델이 없습니다")
        return 1
    m = models[0]
    line = f"모델 {m.get('id')}"
    if m.get("max_model_len"):
        line += f" · 최대 문맥 {int(m['max_model_len']):,} 토큰"
    root = base[:-3] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(urllib.request.Request(root + "/metrics", headers=headers), timeout=5) as r:  # noqa: S310
            text = r.read().decode("utf-8", "replace")
        sums = {"running": 0.0, "waiting": 0.0}
        for raw in text.splitlines():
            for key_name in sums:
                if raw.startswith(f"vllm:num_requests_{key_name}"):
                    try:
                        sums[key_name] += float(raw.rsplit(" ", 1)[1])
                    except (IndexError, ValueError):
                        pass
        line += f" · 처리 중 {int(sums['running'])} · 대기 {int(sums['waiting'])} (도면 분석기 요청 포함)"
    except Exception:  # noqa: BLE001
        pass
    print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m translator_app.selftest", description="번역기 동작 시험")
    ap.add_argument("--base", default="http://127.0.0.1:7860")
    ap.add_argument("--skip-docs", action="store_true")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if a.probe:
        return probe()
    return run(a.base, a.skip_docs, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
