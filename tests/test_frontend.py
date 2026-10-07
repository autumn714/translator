"""Browser regression tests for the static UI (translator_app/static).

The app runs in-process (mock engine) and every scenario is executed in a fresh headless Chrome/Edge
context driven over the DevTools protocol by a small Node script (Node >= 22 for the global WebSocket).
The tests are skipped when Node or a Chromium-based browser is not available
(TRANSLATOR_TEST_BROWSER=<path> selects a browser explicitly).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import make_settings

RUNNER_JS = r"""
import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const [browserPath, specPath, outPath] = process.argv.slice(2);
const spec = JSON.parse(readFileSync(specPath, "utf8"));
const results = {};
const child = spawn(browserPath, [
  "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${spec.profile}`, "--no-first-run",
  "--no-default-browser-check", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
  "--disable-sync", "--mute-audio", "--window-size=1280,900", "about:blank",
], { stdio: "ignore" });

async function main() {
  const portFile = join(spec.profile, "DevToolsActivePort");
  let lines = null;
  for (let i = 0; i < 300 && !lines; i += 1) {
    if (existsSync(portFile)) {
      const parts = readFileSync(portFile, "utf8").trim().split(/\r?\n/);
      if (parts.length >= 2) lines = parts;
    }
    if (!lines) await sleep(100);
  }
  if (!lines) throw new Error("browser did not start");
  const ws = new WebSocket(`ws://127.0.0.1:${lines[0].trim()}${lines[1].trim()}`);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("devtools connection failed"));
  });
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (!msg.id || !pending.has(msg.id)) return;
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(msg.error.message));
    else resolve(msg.result);
  };
  const send = (method, params = {}, sessionId) =>
    new Promise((resolve, reject) => {
      const id = (seq += 1);
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  const evaluate = async (expression, sessionId, timeout) => {
    const result = await Promise.race([
      send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId),
      sleep(timeout).then(() => { throw new Error(`scenario timed out after ${timeout} ms`); }),
    ]);
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    }
    return result.result.value;
  };
  for (const scenario of spec.scenarios) {
    let context = null;
    try {
      context = (await send("Target.createBrowserContext")).browserContextId;
      const { targetId } = await send("Target.createTarget", { url: "about:blank", browserContextId: context });
      const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
      await send("Page.enable", {}, sessionId);
      await send("Runtime.enable", {}, sessionId);
      if (scenario.viewport) {
        const [width, height, deviceScaleFactor = 1] = scenario.viewport;
        await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor, mobile: false }, sessionId);
      }
      await send("Page.addScriptToEvaluateOnNewDocument", { source: spec.prelude + "\n" + (scenario.init || "") }, sessionId);
      await send("Page.navigate", { url: spec.base + (scenario.path || "/") }, sessionId);
      let ready = false;
      for (let i = 0; i < 400 && !ready; i += 1) {
        try {
          ready = (await evaluate("document.documentElement.classList.contains('is-ready')", sessionId, 2000)) === true;
        } catch {
          ready = false;
        }
        if (!ready) await sleep(50);
      }
      if (!ready) throw new Error("page did not become ready");
      const value = await evaluate(`(${scenario.run})()`, sessionId, scenario.timeout || 45000);
      results[scenario.name] = { ok: true, value };
    } catch (error) {
      results[scenario.name] = { ok: false, error: String(error?.message || error) };
    } finally {
      if (context) await send("Target.disposeBrowserContext", { browserContextId: context }).catch(() => {});
    }
  }
  await send("Browser.close").catch(() => {});
  ws.close();
}

main()
  .catch((error) => {
    results.__error__ = { ok: false, error: String(error?.message || error) };
  })
  .finally(() => {
    writeFileSync(outPath, JSON.stringify(results));
    setTimeout(() => {
      try {
        child.kill();
      } catch {}
      process.exit(0);
    }, 300);
  });
"""

# Installed before the app's scripts in every page: records fetch calls, allows per-scenario mocks, helpers.
PRELUDE_JS = r"""
(() => {
  const t = (window.__t = { calls: [], mocks: [] });
  const realFetch = window.fetch.bind(window);
  t.realFetch = realFetch;
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const method = String(init.method || "GET").toUpperCase();
    const entry = { url, method, body: typeof init.body === "string" ? init.body : null, aborted: false, status: null };
    t.calls.push(entry);
    init.signal?.addEventListener("abort", () => { entry.aborted = true; });
    for (const mock of t.mocks) {
      const result = await mock(url, method, init);
      if (result) {
        entry.status = result.status;
        return result;
      }
    }
    const response = await realFetch(input, init);
    entry.status = response.status;
    return response;
  };
  t.json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  t.sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  t.until = async (fn, timeout = 8000, label = "") => {
    const end = Date.now() + timeout;
    for (;;) {
      let value = null;
      try { value = await fn(); } catch { value = null; }
      if (value) return value;
      if (Date.now() > end) throw new Error(`timeout: ${label || fn.toString().slice(0, 160)}`);
      await t.sleep(25);
    }
  };
  t.count = (pattern, method) => t.calls.filter((c) => c.url.includes(pattern) && (!method || c.method === method)).length;
  t.typeSource = (text) => {
    const src = document.querySelector("#srcText");
    src.value = text;
    src.dispatchEvent(new Event("input", { bubbles: true }));
  };
  t.translated = (contains = "", timeout = 15000) =>
    t.until(() => {
      const out = document.querySelector("#outText");
      return out.querySelector(".para") && out.getAttribute("aria-busy") === "false" &&
        !document.querySelector("#copyBtn").disabled && out.textContent.includes(contains);
    }, timeout, "translation");
  t.locate = (root, offset) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    let position = 0;
    while (node) {
      if (offset <= position + node.textContent.length) return [node, offset - position];
      position += node.textContent.length;
      node = walker.nextNode();
    }
    throw new Error("offset outside text");
  };
  const out = () => document.querySelector("#outText");
  t.clickAt = (node, offset, { button = 0 } = {}) => {
    out().dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button }));
    const selection = getSelection();
    selection.removeAllRanges();
    const range = document.createRange();
    range.setStart(node, offset);
    range.collapse(true);
    selection.addRange(range);
    out().dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button }));
  };
  t.selectRange = (node, start, end) => {
    out().dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 }));
    const selection = getSelection();
    selection.removeAllRanges();
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, end);
    selection.addRange(range);
    out().dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
  };
  t.button = (root, text) => [...root.querySelectorAll("button")].find((b) => b.textContent.includes(text));
  try {
    if (localStorage.getItem("translator.useGlossary") === null) localStorage.setItem("translator.useGlossary", "false");
  } catch {}
})();
"""

DOC_JOB = r"""
window.__job = (id, status, extra = {}) => ({
  id, filename: `${id}.txt`, size: 10, format: ".txt", source_lang: "en", target_lang: "ko", status,
  progress: { done: 1, total: 4, percent: status === "done" ? 100 : 25 },
  output_filename: status === "done" ? `${id}_ko.txt` : null, warnings: [], error: null,
  created_at: "2026-10-06T00:00:00Z", finished_at: status === "done" ? "2026-10-06T00:01:00Z" : null,
  expires_at: "2099-01-01T00:00:00Z", eta_seconds: null, chars: 10, report_count: 0, ...extra,
});
"""

# (width, height, device pixel ratio); 1536 × 1.25 is a 1920 px screen at 125 % scaling.
LANGBAR_VIEWPORTS = [(1536, 730, 1.25), (1280, 800, 1), (820, 900, 1), (390, 844, 1)]

# Long labels in both language bars; measures the selects against the swap button / arrow between them.
LANGBAR_RUN = r"""async () => {
  const q = (selector) => document.querySelector(selector);
  const box = (node) => node.getBoundingClientRect();
  const midY = (rect) => (rect.top + rect.bottom) / 2;
  const hits = (node, x, y) => node.contains(document.elementFromPoint(x, y));
  const setAutoLabel = (select, text) => { select.querySelector('option[data-auto="1"]').textContent = text; };
  const rows = [];
  const measure = (tab, bar, src, mid, tgt, centerX) => {
    tgt.focus();
    const b = box(bar), s = box(src), m = box(mid), t = box(tgt);
    const inside = (r) => r.left >= b.left - 0.5 && r.right <= b.right + 0.5 && r.top >= b.top - 0.5 && r.bottom <= b.bottom + 0.5;
    rows.push({
      tab,
      labels: `${src.selectedOptions[0].textContent} | ${tgt.selectedOptions[0].textContent}`,
      gap: Math.round(Math.min(m.left - s.right, t.left - m.right) * 10) / 10,
      inside: [s, m, t].every(inside),
      painted: hits(src, s.left + 1, midY(s)) && hits(src, s.right - 1, midY(s)) &&
        hits(mid, m.left + 2, midY(m)) && hits(mid, m.right - 2, midY(m)) &&
        hits(tgt, t.left + 1, midY(t)) && hits(tgt, t.right - 1, midY(t)),
      offCenter: Math.round(((m.left + m.right) / 2 - centerX) * 10) / 10,
    });
  };
  const variants = [
    ["auto", "한국어 (감지됨)", "zh-Hant"],
    ["auto", "중국어(번체)·인도네시아어 (감지됨)", "id"],
    ["sw", null, "et"],
  ];
  const fill = (src, tgt, [source, label, target]) => {
    src.value = source;
    if (label) setAutoLabel(src, label);
    tgt.value = target;
  };
  const textBar = q("#panel-text .langbar");
  // side-by-side panes: the swap button sits on the divider; stacked panes: centre of the bar
  const paneSrc = box(q(".pane-src")), paneOut = box(q(".pane-out"));
  const textCenter = paneOut.top < paneSrc.bottom - 1 ? paneOut.left : (box(textBar).left + box(textBar).right) / 2;
  for (const variant of variants) {
    fill(q("#srcLang"), q("#tgtLang"), variant);
    measure("text", textBar, q("#srcLang"), q("#swapBtn"), q("#tgtLang"), textCenter);
  }
  q("#tab-docs").click();
  const docBar = q("#panel-docs .langbar");
  for (const variant of variants) {
    fill(q("#docSrc"), q("#docTgt"), variant);
    measure("docs", docBar, q("#docSrc"), docBar.querySelector(".langbar-arrow"), q("#docTgt"), (box(docBar).left + box(docBar).right) / 2);
  }
  return rows;
}"""

# Glossary manager on a phone: every glossary name must fit the picker without being cut to "…".
GLOSSARY_PICKER_RUN = r"""async () => {
  document.querySelector("#glossaryBtn").click();
  const dialog = await __t.until(() => document.querySelector("dialog.gl-dialog[open]"));
  const picker = await __t.until(() => dialog.querySelector(".gl-picker option") && dialog.querySelector(".gl-picker"));
  const style = getComputedStyle(picker);
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const avail = picker.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const labels = [...picker.options].map((option) => option.textContent);
  const room = Math.min(...labels.map((label) => avail - ctx.measureText(label).width));
  const bar = dialog.querySelector(".gl-toolbar").getBoundingClientRect();
  const inside = [...dialog.querySelectorAll(".gl-toolbar button, .gl-toolbar select")].every((node) => {
    const r = node.getBoundingClientRect();
    return r.left >= bar.left - 0.5 && r.right <= bar.right + 0.5;
  });
  return { labels, room: Math.round(room), inside };
}"""


def scenarios(ids: dict[str, str]) -> list[dict[str, Any]]:
    big, rows, add, saved, draft = (json.dumps(ids[key]) for key in ("big", "rows", "add", "saved", "draft"))
    return [
        {
            "name": "status_chip",
            "init": r"""
window.__statusModel = { connected: true, name: "Secret-Model-27B", max_model_len: 32768, vision: true, running: 2, waiting: 0, error: null };
__t.mocks.push((url) => url.startsWith("/api/status") ? __t.json({
  app_version: "9.9.9", engine: "openai_compatible", model: { ...window.__statusModel },
  auth: { enabled: false, user: null }, limits: { text_max_chars: 30000, doc_max_mb: 50, doc_retention_hours: 24 },
  document_formats: [{ ext: ".txt", label: "텍스트", output: ".txt", layout: true, bilingual: true }],
}) : null);
""",
            "run": r"""async () => {
  const chip = document.querySelector("#statusChip");
  await __t.until(() => chip.dataset.state === "ok");
  const labels = [chip.textContent.trim()];
  chip.click();
  await __t.until(() => chip.dataset.state === "ok");
  const opened = document.documentElement.outerHTML;
  window.__statusModel.waiting = 3;
  chip.click();
  await __t.until(() => chip.dataset.state === "busy");
  labels.push(chip.textContent.trim());
  window.__statusModel.connected = false;
  chip.click();
  await __t.until(() => chip.dataset.state === "down");
  labels.push(chip.textContent.trim());
  const html = opened + document.documentElement.outerHTML;
  const leaks = ["Secret-Model", "32,768", "32768", "9.9.9", "openai_compatible", "토큰", "최대 길이"].filter((s) => html.includes(s));
  return { labels, leaks, popover: Boolean(document.querySelector(".popover")) };
}""",
        },
        {
            "name": "rewrite_debounce",
            "path": "/#write",
            "run": r"""async () => {
  const input = document.querySelector("#wInput");
  input.value = "This is a sentence that needs polishing.";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  document.querySelector("#wRun").click();
  await __t.until(() => !document.querySelector("#wFoot").hidden);
  const rewrites = () => __t.calls.filter((c) => c.url.includes("/api/rewrite"));
  const first = rewrites().length;
  const chips = document.querySelector("#wStyles");
  for (let i = 0; i < 7; i += 1) {
    chips.querySelector('[aria-checked="true"]').dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    await __t.sleep(60);
  }
  const during = rewrites().length;
  await __t.sleep(1000);
  const after = rewrites().length;
  const lastStyle = JSON.parse(rewrites().at(-1).body).style;
  for (const value of ["formal", "concise", "plain"]) {
    chips.querySelector(`[data-value="${value}"]`).click();
    await __t.sleep(80);
  }
  await __t.sleep(1000);
  const afterClicks = rewrites().length;
  const lastClickStyle = JSON.parse(rewrites().at(-1).body).style;
  await __t.until(() => document.querySelector("#wOut").textContent.includes("[plain]"));
  return { first, during, after, lastStyle, afterClicks, lastClickStyle };
}""",
        },
        {
            "name": "alternatives_explicit",
            "run": r"""async () => {
  __t.typeSource("Hydrogen is light. It burns cleanly. Water remains.");
  await __t.translated("Water remains.");
  const out = document.querySelector("#outText");
  const node = () => document.createTreeWalker(out, NodeFilter.SHOW_TEXT).nextNode();
  const alts = () => __t.count("/api/alternatives", "POST");
  for (const offset of [30, 3, 40, 10, 6]) {
    __t.clickAt(node(), offset);
    await __t.sleep(60);
  }
  await __t.sleep(600);
  const afterBurst = alts();
  await __t.until(() => document.querySelector(".pop-alt .alt-item"));
  __t.clickAt(node(), 12);
  await __t.sleep(600);
  const sameSentence = alts();
  const reopened = Boolean(document.querySelector(".pop-alt .alt-item"));
  __t.clickAt(node(), 30, { button: 2 });
  await __t.sleep(600);
  const rightClick = alts();
  out.focus();
  out.dispatchEvent(new InputEvent("input", { bubbles: true }));
  __t.clickAt(node(), 30);
  await __t.sleep(600);
  const editingClick = alts();
  __t.selectRange(node(), 5, 13);
  await __t.sleep(600);
  const selection = alts();
  return { afterBurst, sameSentence, reopened, rightClick, editingClick, selection };
}""",
        },
        {
            "name": "alternatives_long_paragraph",
            "run": r"""async () => {
  const lines = [];
  for (let i = 0; lines.join("\n").length < 22000; i += 1) lines.push(`Sentence number ${i} talks about hydrogen storage and safety rules.`);
  __t.typeSource(lines.join("\n"));
  await __t.translated("Sentence number 300 ", 30000);
  const out = document.querySelector("#outText");
  const para = out.querySelector(".para");
  const text = para.textContent;
  const [node, local] = __t.locate(para, text.indexOf("Sentence number 150 ") + 3);
  __t.clickAt(node, local);
  const call = await __t.until(() => __t.calls.find((c) => c.url.includes("/api/alternatives") && c.status));
  const body = JSON.parse(call.body);
  await __t.until(() => document.querySelector(".pop-alt .alt-item, .pop-alt .pop-error"));
  return {
    paras: out.querySelectorAll(".para").length, paraLength: text.length, status: call.status,
    sourceLength: body.source.length, translationLength: body.translation.length,
    spanInTranslation: body.translation.includes(body.span), spanInSource: body.source.includes("Sentence number 150 "),
    items: document.querySelectorAll(".pop-alt .alt-item").length,
  };
}""",
            "timeout": 60000,
        },
        {
            "name": "glossary_large",
            "init": f"localStorage.setItem('translator.glossaryId', JSON.stringify({big})); localStorage.setItem('translator.useGlossary', 'true');",
            "run": r"""async () => {
  const started = performance.now();
  document.querySelector("#glossaryBtn").click();
  const dialog = await __t.until(() => document.querySelector("dialog.gl-dialog[open]"));
  await __t.until(() => dialog.querySelector(".gl-meta .muted").textContent.includes("20,000") && dialog.querySelector("tbody tr [data-field='source']"), 30000);
  const openMs = performance.now() - started;
  const rows = dialog.querySelectorAll("tbody tr").length;
  const nodes = dialog.querySelectorAll("*").length;
  const more = dialog.querySelector(".gl-more");
  const moreText = more.textContent;
  more.click();
  const rowsAfterMore = dialog.querySelectorAll("tbody tr").length;
  const search = dialog.querySelector('input[type="search"]');
  search.value = "term19999";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  const found = [...dialog.querySelectorAll("tbody tr [data-field='source']")].map((input) => input.value);
  const target = dialog.querySelector("tbody tr [data-field='target']");
  target.value = "수정한 용어";
  target.dispatchEvent(new Event("input", { bubbles: true }));
  search.value = "";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  const rowsAfterClear = dialog.querySelectorAll("tbody tr").length;
  __t.typeSource("Check term19999 now.");
  const call = await __t.until(() => __t.calls.find((c) => c.url.includes("/api/translate/stream")));
  const body = JSON.parse(call.body);
  return {
    openMs, rows, nodes, moreText, rowsAfterMore, found, rowsAfterClear,
    draft: (body.glossary_entries || []).map((entry) => [entry.source, entry.target]),
  };
}""",
            "timeout": 90000,
        },
        {
            "name": "glossary_partial_row",
            "init": f"localStorage.setItem('translator.glossaryId', JSON.stringify({rows}));",
            "run": r"""async () => {
  document.querySelector("#glossaryBtn").click();
  const dialog = await __t.until(() => document.querySelector("dialog.gl-dialog[open]"));
  await __t.until(() => dialog.querySelectorAll("tbody tr [data-field='source']").length === 1);
  __t.button(dialog.querySelector(".modal-foot"), "행 추가").click();
  const source = dialog.querySelector("tbody tr [data-field='source']");
  source.value = "halfdone";
  source.dispatchEvent(new Event("input", { bubbles: true }));
  const save = __t.button(dialog.querySelector(".modal-foot"), "저장");
  save.click();
  await __t.sleep(300);
  const result = {
    putsAfterInvalid: __t.count("/api/glossaries/", "PUT"),
    rowsAfterInvalid: dialog.querySelectorAll("tbody tr").length,
    invalidRows: dialog.querySelectorAll("tbody tr.is-invalid").length,
    focusedField: document.activeElement?.dataset?.field || null,
    toast: [...document.querySelectorAll(".toast")].map((node) => node.textContent).join(" "),
  };
  const target = dialog.querySelector("tbody tr.is-invalid [data-field='target']");
  target.value = "반쯤";
  target.dispatchEvent(new Event("input", { bubbles: true }));
  save.click();
  const put = await __t.until(() => __t.calls.find((c) => c.method === "PUT" && c.status));
  result.saved = JSON.parse(put.body).entries.map((entry) => entry.source).sort();
  result.rowsAfterSave = dialog.querySelectorAll("tbody tr").length;
  return result;
}""",
        },
        {
            "name": "staged_focus_on_poll",
            "path": "/#docs",
            "run": r"""async () => {
  const input = document.querySelector("#fileInput");
  const transfer = new DataTransfer();
  transfer.items.add(new File(["hello"], "a.txt", { type: "text/plain" }));
  transfer.items.add(new File(["world"], "b.txt", { type: "text/plain" }));
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await __t.until(() => document.querySelectorAll("#stagedList li").length === 2);
  await __t.sleep(150);  // adding files focuses the start button on the next frame
  document.querySelector("#stagedList li:nth-child(2) button").focus();
  const focusedBefore = document.activeElement?.getAttribute("aria-label");
  const statusCalls = () => __t.calls.filter((c) => c.url.startsWith("/api/status"));
  const before = statusCalls().length;
  document.dispatchEvent(new Event("visibilitychange"));
  await __t.until(() => statusCalls().length > before && statusCalls().every((c) => c.status));
  await __t.sleep(100);
  return { focusedBefore, focused: document.activeElement?.getAttribute("aria-label") || document.activeElement?.tagName };
}""",
        },
        {
            "name": "docs_unavailable",
            "path": "/#docs",
            "init": r"""
__t.mocks.push(async (url, method, init) => {
  if (!url.startsWith("/api/status")) return null;
  const data = await (await __t.realFetch(url, init)).json();
  data.document_formats = [];
  return __t.json(data);
});
""",
            "run": r"""async () => {
  await __t.until(() => document.querySelector("#dropzone").classList.contains("is-disabled"));
  const input = document.querySelector("#fileInput");
  const transfer = new DataTransfer();
  transfer.items.add(new File(["hello"], "a.txt", { type: "text/plain" }));
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await __t.sleep(100);
  return {
    pickDisabled: document.querySelector("#pickBtn").disabled,
    title: document.querySelector("#dropzone .dz-title").textContent,
    staged: document.querySelectorAll("#stagedList li").length,
    toast: [...document.querySelectorAll(".toast")].map((node) => node.textContent).join(" "),
  };
}""",
        },
        {
            "name": "docs_formats_missing",
            "path": "/#docs",
            "init": r"""
__t.mocks.push(async (url, method, init) => {
  if (!url.startsWith("/api/status")) return null;
  const data = await (await __t.realFetch(url, init)).json();
  delete data.document_formats;
  return __t.json(data);
});
""",
            "run": r"""async () => {
  await __t.until(() => __t.calls.some((c) => c.url.startsWith("/api/status") && c.status));
  await __t.sleep(50);
  return {
    disabled: document.querySelector("#dropzone").classList.contains("is-disabled"),
    chips: document.querySelectorAll("#fmtChips .fmt-chip").length,
  };
}""",
        },
        {
            "name": "cancel_stale_poll",
            "path": "/#docs",
            "init": DOC_JOB + r"""
localStorage.setItem("translator.jobs", JSON.stringify(["job-race"]));
let lists = 0;
__t.mocks.push(async (url, method) => {
  if (url.startsWith("/api/documents?ids=")) {
    lists += 1;
    const snapshot = __job("job-race", "translating");
    if (lists > 1) await __t.sleep(500);
    return __t.json({ jobs: [snapshot] });
  }
  if (url === "/api/documents/job-race/cancel" && method === "POST") return __t.json(__job("job-race", "canceled"));
  return null;
});
""",
            "run": r"""async () => {
  const pill = () => document.querySelector(".job .pill")?.textContent || "";
  await __t.until(() => pill().includes("번역 중"));
  const lists = () => __t.calls.filter((c) => c.url.startsWith("/api/documents?ids="));
  await __t.until(() => lists().length >= 2 && lists().at(-1).status === null);
  const inFlight = lists().at(-1);
  __t.button(document.querySelector(".job"), "취소").click();
  await __t.until(() => __t.calls.some((c) => c.url.endsWith("/cancel") && c.status));
  await __t.until(() => inFlight.status !== null, 3000);
  await __t.sleep(100);
  return { pill: pill() };
}""",
        },
        {
            "name": "cancel_finished_job",
            "path": "/#docs",
            "init": DOC_JOB + r"""
localStorage.setItem("translator.jobs", JSON.stringify(["job-done"]));
__t.mocks.push(async (url, method) => {
  if (url.startsWith("/api/documents?ids=")) return __t.json({ jobs: [__job("job-done", "translating")] });
  if (url === "/api/documents/job-done/cancel" && method === "POST") return __t.json(__job("job-done", "done", { report_count: 2 }));
  if (url === "/api/documents/job-done/report") return __t.json({ items: [
    { source: "2026", target: "", issue: "숫자 누락" }, { source: "kg", target: "", issue: "단위 누락" },
  ] });
  return null;
});
""",
            "run": r"""async () => {
  await __t.until(() => (document.querySelector(".job .pill")?.textContent || "").includes("번역 중"));
  __t.button(document.querySelector(".job"), "취소").click();
  const review = await __t.until(() => __t.button(document.querySelector(".job"), "검수"), 4000);
  return { review: review.textContent, pill: document.querySelector(".job .pill").textContent };
}""",
        },
        {
            "name": "upload_cancel_after_send",
            "path": "/#docs",
            "init": DOC_JOB + r"""
class FakeXHR {
  constructor() {
    this.upload = new EventTarget();
    this.events = new EventTarget();
    this.status = 0;
    this.responseText = "";
    this.aborted = false;
  }
  open(method, url) { this.url = url; }
  setRequestHeader() {}
  addEventListener(type, fn) { this.events.addEventListener(type, fn); }
  send() {
    setTimeout(() => {
      this.upload.dispatchEvent(new ProgressEvent("progress", { lengthComputable: true, loaded: 10, total: 10 }));
      this.upload.dispatchEvent(new ProgressEvent("load"));
      window.__uploadSent = true;
    }, 50);
    setTimeout(() => {
      if (this.aborted) return;
      this.status = 202;
      this.responseText = JSON.stringify(__job("job-orphan", "queued"));
      this.events.dispatchEvent(new ProgressEvent("load"));
    }, 700);
  }
  abort() {
    this.aborted = true;
    this.events.dispatchEvent(new ProgressEvent("abort"));
  }
}
window.XMLHttpRequest = FakeXHR;
__t.mocks.push((url, method) => url === "/api/documents/job-orphan" && method === "DELETE" ? __t.json({ deleted: true }) : null);
__t.mocks.push((url) => url.startsWith("/api/documents?ids=") ? __t.json({ jobs: [] }) : null);
""",
            "run": r"""async () => {
  const input = document.querySelector("#fileInput");
  const transfer = new DataTransfer();
  transfer.items.add(new File(["hello upload"], "up.txt", { type: "text/plain" }));
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await __t.until(() => !document.querySelector("#startBtn").disabled);
  document.querySelector("#startBtn").click();
  await __t.until(() => window.__uploadSent);
  await __t.sleep(20);
  __t.button(document.querySelector(".job"), "취소").click();
  await __t.sleep(50);
  const pillAfterCancel = document.querySelector(".job .pill")?.textContent || "";
  await __t.until(() => __t.calls.some((c) => c.url === "/api/documents/job-orphan" && c.method === "DELETE"), 3000);
  await __t.sleep(50);
  return { pillAfterCancel, cards: document.querySelectorAll("#jobList .job").length, stored: localStorage.getItem("translator.jobs") };
}""",
        },
        {
            "name": "finished_jobs_revalidated",
            "path": "/#docs",
            "init": DOC_JOB + r"""
localStorage.setItem("translator.jobs", JSON.stringify(["job-a", "job-b", "job-c"]));
window.__gone = new Set(["job-a"]);
HTMLAnchorElement.prototype.click = function () { (window.__downloads ||= []).push(this.getAttribute("href")); };
__t.mocks.push((url) => {
  if (url.startsWith("/api/documents?ids=")) {
    const ids = decodeURIComponent(url.split("ids=")[1]).split(",");
    const first = !window.__listed;
    window.__listed = true;
    return __t.json({ jobs: ids.filter((id) => first || !window.__gone.has(id)).map((id) => __job(id, "done")) });
  }
  const match = url.match(/^\/api\/documents\/(job-[a-z])$/);
  if (match) {
    return window.__gone.has(match[1])
      ? __t.json({ detail: "작업을 찾을 수 없습니다. 보관 기간이 지나 삭제되었을 수 있습니다." }, 404)
      : __t.json(__job(match[1], "done"));
  }
  return null;
});
""",
            "run": r"""async () => {
  const card = (id) => [...document.querySelectorAll("#jobList .job")].find((li) => li.querySelector(".job-name").textContent === `${id}.txt`);
  await __t.until(() => card("job-a") && card("job-b") && card("job-c"));
  __t.button(card("job-a"), "내려받기").click();
  await __t.until(() => !card("job-a"), 3000);
  const toast = [...document.querySelectorAll(".toast")].map((node) => node.textContent).join(" ");
  const downloadsAfterGone = (window.__downloads || []).length;
  __t.button(card("job-c"), "내려받기").click();
  await __t.until(() => (window.__downloads || []).length === 1, 3000);
  window.__gone.add("job-b");
  document.dispatchEvent(new Event("visibilitychange"));
  await __t.until(() => !card("job-b"), 3000);
  return {
    toast, downloadsAfterGone, downloads: window.__downloads,
    cards: [...document.querySelectorAll("#jobList .job .job-name")].map((node) => node.textContent),
    stored: JSON.parse(localStorage.getItem("translator.jobs")),
  };
}""",
        },
        {
            "name": "history_keeps_different_texts",
            "init": "localStorage.setItem('translator.historyEnabled', 'true'); localStorage.setItem('translator.targetLang', JSON.stringify('en'));",
            "run": r"""async () => {
  const a = "안녕하십니까. 한국에너지경제연구원 지식정보화실입니다. 첫 번째 문서는 회의 자료입니다.";
  const b = "안녕하십니까. 한국에너지경제연구원 지식정보화실입니다. 두 번째 자료를 보내드립니다.";
  const items = () => JSON.parse(localStorage.getItem("translator.history") || "[]").map((item) => item.source);
  __t.typeSource(a);
  await __t.translated("첫 번째");
  await __t.until(() => items().includes(a));
  __t.typeSource(b);
  await __t.translated("두 번째");
  await __t.until(() => items().includes(b));
  const first = items();
  __t.typeSource("회의 결과를 공");
  await __t.translated("회의 결과를 공");
  await __t.until(() => items().includes("회의 결과를 공"));
  __t.typeSource("회의 결과를 공유드립니다.");
  await __t.translated("공유드립니다.");
  await __t.until(() => items().includes("회의 결과를 공유드립니다."));
  return { first, second: items() };
}""",
        },
        {
            "name": "history_merges_mid_edits",
            "init": "localStorage.setItem('translator.historyEnabled', 'true'); localStorage.setItem('translator.targetLang', JSON.stringify('en'));",
            "run": r"""async () => {
  const { sameDraft } = await import("/static/js/history.js");
  const items = () => JSON.parse(localStorage.getItem("translator.history") || "[]").map((item) => item.source);
  const a = "The quick brown fox jumps over the lazy dog near the river bank.";
  const b = "The quick brown cat jumps over the lazy dog near the river bank.";
  __t.typeSource(a);
  await __t.translated("fox");
  await __t.until(() => items().includes(a));
  __t.typeSource(b);
  await __t.translated("cat");
  await __t.until(() => items().includes(b));
  const now = Date.now();
  const rec = (source, ts = now) => ({ source, target_lang: "en", ts });
  return {
    items: items(),
    unrelated: sameDraft(rec("Annual report on hydrogen"), rec("Annual budget for solar panels"), now),
    stale: sameDraft(rec(a, now - 6 * 60 * 1000), rec(b), now),
    otherLang: sameDraft(rec(a), { source: b, target_lang: "ja" }, now),
  };
}""",
        },
        {
            "name": "glossary_draft_whitespace",
            "init": f"localStorage.setItem('translator.glossaryId', JSON.stringify({draft})); localStorage.setItem('translator.useGlossary', 'true');",
            "run": r"""async () => {
  document.querySelector("#glossaryBtn").click();
  const dialog = await __t.until(() => document.querySelector("dialog.gl-dialog[open]"));
  const source = await __t.until(() => dialog.querySelector("tbody tr [data-field='source']"));
  source.value = "Fuel   Cell";
  source.dispatchEvent(new Event("input", { bubbles: true }));
  __t.typeSource("A hydrogen fuel\n\n cell stack.");
  const call = await __t.until(() => __t.calls.find((c) => c.url.includes("/api/translate/stream")));
  return (JSON.parse(call.body).glossary_entries || []).map((entry) => entry.source);
}""",
        },
        {
            "name": "glossary_add_while_off",
            "init": f"localStorage.setItem('translator.glossaryId', JSON.stringify({add})); localStorage.setItem('translator.useGlossary', 'false');",
            "run": r"""async () => {
  __t.typeSource("Hydrogen fuel cells are efficient.");
  await __t.translated("efficient.");
  const streams = () => __t.count("/api/translate/stream");
  const before = streams();
  const out = document.querySelector("#outText");
  const para = out.querySelector(".para");
  para.textContent = `${para.textContent} (edited)`;
  out.dispatchEvent(new InputEvent("input", { bubbles: true }));
  __t.selectRange(para.firstChild, 5, 13);
  const pop = await __t.until(() => document.querySelector(".pop-alt"));
  __t.button(pop, "용어집에 추가").click();
  const form = await __t.until(() => document.querySelector(".gl-add-form"));
  const [source] = form.querySelectorAll("input");
  source.value = "Hydrogen";
  form.requestSubmit();
  await __t.until(() => __t.calls.some((c) => c.method === "PUT" && c.status === 200));
  await __t.sleep(800);
  return {
    streamsAdded: streams() - before,
    edited: out.textContent.includes("(edited)"),
    toast: [...document.querySelectorAll(".toast")].map((node) => node.textContent).join(" "),
  };
}""",
        },
        {
            "name": "glossary_load_retry",
            "init": f"""
localStorage.setItem('translator.glossaryId', JSON.stringify({saved}));
localStorage.setItem('translator.useGlossary', 'true');
let failed = false;
__t.mocks.push((url, method) => {{
  if (url === "/api/glossaries" && method === "GET" && !failed) {{
    failed = true;
    return __t.json({{ detail: "서버 오류" }}, 503);
  }}
  return null;
}});
""",
            "run": r"""async () => {
  await __t.until(() => __t.calls.some((c) => c.url === "/api/glossaries" && c.status === 503));
  await __t.sleep(100);
  const savedAfterFailure = JSON.parse(localStorage.getItem("translator.glossaryId"));
  await __t.until(() => __t.calls.some((c) => c.url === "/api/glossaries" && c.status === 200), 8000);
  await __t.sleep(100);
  return {
    savedAfterFailure,
    selected: document.querySelector("#glossSel").value,
    attempts: __t.calls.filter((c) => c.url === "/api/glossaries").length,
  };
}""",
        },
        {
            "name": "glossary_import_xlsx",
            "init": f"localStorage.setItem('translator.glossaryId', JSON.stringify({rows}));",
            "run": r"""async () => {
  document.querySelector("#glossaryBtn").click();
  const dialog = await __t.until(() => document.querySelector("dialog.gl-dialog[open]"));
  const importButton = await __t.until(() => {
    const button = __t.button(dialog.querySelector(".gl-io"), "가져오기");
    return button && !button.disabled && dialog.querySelector("tbody tr [data-field='source']") ? button : null;
  });
  importButton.click();
  const form = await __t.until(() => document.querySelector(".import-form"));
  return { accept: form.querySelector('input[type="file"]').accept, subtitle: form.querySelector(".pop-sub").textContent };
}""",
        },
        {
            "name": "output_live_region",
            "run": r"""async () => {
  const live = document.querySelector("#outStatus");
  const attrs = live ? { role: live.getAttribute("role"), live: live.getAttribute("aria-live") } : null;
  __t.typeSource("Hello there.");
  await __t.translated("Hello there.");
  await __t.until(() => live && live.textContent === "번역 완료", 2000);
  return { attrs, text: live.textContent };
}""",
        },
        {
            "name": "auto_switch_same_language",
            "init": "localStorage.setItem('translator.targetLang', JSON.stringify('ko')); localStorage.removeItem('translator.lastDetectedSource');",
            "run": r"""async () => {
  const target = document.querySelector("#tgtLang");
  __t.typeSource("수소 생산 설비의 탄소 배출 강도는 인증 기준 이하여야 합니다.");
  await __t.until(() => target.value === "en", 5000);
  await __t.translated("인증 기준");
  const afterKorean = target.value;
  __t.typeSource("The certification review checks the emission intensity of every hydrogen production facility.");
  await __t.until(() => target.value === "ko", 5000);
  await __t.translated("production facility.");
  const afterEnglish = target.value;
  return { afterKorean, afterEnglish, stored: JSON.parse(localStorage.getItem("translator.targetLang")) };
}""",
        },
        *({"name": f"langbar_{viewport[0]}", "viewport": viewport, "run": LANGBAR_RUN} for viewport in LANGBAR_VIEWPORTS),
        {
            "name": "glossary_picker_360",
            "viewport": [360, 740],
            "init": f"localStorage.setItem('translator.glossaryId', JSON.stringify({saved}));",
            "run": GLOSSARY_PICKER_RUN,
        },
    ]


# ---------------------------------------------------------------- harness
def _find_browser() -> str | None:
    explicit = os.environ.get("TRANSLATOR_TEST_BROWSER")
    if explicit:
        return explicit if Path(explicit).exists() else None
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return next((path for path in candidates if Path(path).exists()), None)


def _node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        major = int(version.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return node if major >= 22 else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def ui(tmp_path_factory: pytest.TempPathFactory):
    node, browser = _node(), _find_browser()
    if not node or not browser:
        pytest.skip("Node 22+ 또는 Chrome/Edge가 없어 화면 시험을 건너뜁니다")
    import uvicorn

    from translator_app.main import create_app

    root = tmp_path_factory.mktemp("frontend")
    app = create_app(make_settings(root))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                pytest.fail("앱 서버가 시작되지 않았습니다")
            time.sleep(0.05)

        def create(name: str, entries: list[dict[str, Any]]) -> str:
            response = httpx.post(f"{base}/api/glossaries", json={"name": name, "entries": entries}, timeout=60)
            response.raise_for_status()
            return response.json()["glossary"]["id"]

        ids = {
            "rows": create("행 시험", [{"source_lang": "en", "target_lang": "ko", "source": "alpha", "target": "알파"}]),
            "add": create("추가 시험", [{"source_lang": "en", "target_lang": "ko", "source": "zeta", "target": "제타"}]),
            "saved": create("저장된 선택", [{"source_lang": "en", "target_lang": "ko", "source": "beta", "target": "베타"}]),
            "draft": create("편집 중", [{"source_lang": "en", "target_lang": "ko", "source": "fuel cell", "target": "연료전지"}]),
            "big": create("대용량", [
                {"source_lang": "en", "target_lang": "ko", "source": f"term{index}", "target": f"용어{index}"}
                for index in range(20000)
            ]),
        }
        spec_path, out_path, runner = root / "spec.json", root / "results.json", root / "runner.mjs"
        runner.write_text(RUNNER_JS, encoding="utf-8")
        spec_path.write_text(json.dumps({
            "base": base,
            "profile": str(root / "profile"),
            "prelude": PRELUDE_JS,
            "scenarios": scenarios(ids),
        }), encoding="utf-8")
        completed = subprocess.run([node, str(runner), browser, str(spec_path), str(out_path)],
                                   capture_output=True, text=True, timeout=900)
        if not out_path.exists():
            pytest.fail(f"브라우저 시험 실행 실패: {completed.stderr[-2000:]}")
        results = json.loads(out_path.read_text(encoding="utf-8"))
        if "__error__" in results:
            pytest.fail(f"브라우저 시험 실행 실패: {results['__error__']['error']}")
        yield results | {"__ids__": ids}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def value(ui: dict[str, Any], name: str) -> Any:
    result = ui[name]
    assert result["ok"], f"{name}: {result.get('error')}"
    return result["value"]


# ---------------------------------------------------------------- tests
def test_status_chip_shows_state_only(ui) -> None:
    result = value(ui, "status_chip")
    assert result["labels"] == ["연결됨", "대기 3건", "연결 안 됨"]
    assert result["leaks"] == []
    assert result["popover"] is False


def test_rewrite_style_changes_are_debounced(ui) -> None:
    result = value(ui, "rewrite_debounce")
    assert result["first"] == 1
    assert result["during"] == 1  # arrowing through 7 styles sends nothing immediately
    assert result["after"] == 2 and result["lastStyle"] == "business"
    assert result["afterClicks"] == 3 and result["lastClickStyle"] == "plain"


def test_alternatives_only_on_explicit_action(ui) -> None:
    result = value(ui, "alternatives_explicit")
    assert result["afterBurst"] == 1  # five quick clicks → one request
    assert result["sameSentence"] == 1 and result["reopened"]  # same sentence again → served from cache
    assert result["rightClick"] == 1
    assert result["editingClick"] == 1  # caret move while editing the output
    assert result["selection"] == 2  # a real selection still asks


def test_alternatives_for_long_paragraph_use_bounded_window(ui) -> None:
    result = value(ui, "alternatives_long_paragraph")
    assert result["paras"] == 1 and result["paraLength"] > 20000
    assert result["status"] == 200
    assert result["sourceLength"] <= 6000 and result["translationLength"] <= 4000
    assert result["spanInTranslation"] and result["spanInSource"]
    assert result["items"] >= 1


def test_glossary_editor_pages_large_glossaries(ui) -> None:
    result = value(ui, "glossary_large")
    assert result["openMs"] < 8000
    assert result["rows"] == 100 and result["nodes"] < 20000
    assert result["moreText"] == "더 보기 (19,900)"
    assert result["rowsAfterMore"] == 300
    assert result["found"] == ["term19999"]
    assert result["rowsAfterClear"] == 100
    # only draft entries that can match the text are sent with the translation
    draft = dict(result["draft"])
    assert draft["term19999"] == "수정한 용어"
    assert set(draft) <= {"term1", "term19", "term199", "term1999", "term19999"}


def test_glossary_save_refuses_half_filled_rows(ui) -> None:
    result = value(ui, "glossary_partial_row")
    assert result["putsAfterInvalid"] == 0
    assert result["rowsAfterInvalid"] == 2 and result["invalidRows"] == 1
    assert result["focusedField"] == "target"
    assert "모두 입력" in result["toast"]
    assert result["saved"] == ["alpha", "halfdone"] and result["rowsAfterSave"] == 2


def test_status_poll_keeps_focus_in_staged_list(ui) -> None:
    result = value(ui, "staged_focus_on_poll")
    assert result["focusedBefore"] == "b.txt 제거"
    assert result["focused"] == "b.txt 제거"


def test_documents_tab_disabled_when_unavailable(ui) -> None:
    result = value(ui, "docs_unavailable")
    assert result["pickDisabled"] is True
    assert result["title"] == "문서 번역을 사용할 수 없습니다"
    assert result["staged"] == 0
    assert "문서 번역을 사용할 수 없습니다" in result["toast"]


def test_documents_formats_default_only_when_field_missing(ui) -> None:
    result = value(ui, "docs_formats_missing")
    assert result["disabled"] is False and result["chips"] >= 5


def test_cancel_result_not_overwritten_by_stale_poll(ui) -> None:
    assert value(ui, "cancel_stale_poll")["pill"] == "취소됨"


def test_cancel_hitting_finished_job_loads_report(ui) -> None:
    result = value(ui, "cancel_finished_job")
    assert result["review"] == "검수 2" and result["pill"] == "완료"


def test_cancel_after_upload_sent_deletes_created_job(ui) -> None:
    result = value(ui, "upload_cancel_after_send")
    assert result["pillAfterCancel"] == "취소 중"
    assert result["cards"] == 0
    assert "job-orphan" not in (result["stored"] or "")


def test_finished_jobs_are_revalidated(ui) -> None:
    result = value(ui, "finished_jobs_revalidated")
    assert "찾을 수 없습니다" in result["toast"]
    assert result["downloadsAfterGone"] == 0
    assert result["downloads"] == ["/api/documents/job-c/download"]
    assert result["cards"] == ["job-c.txt"]
    assert result["stored"] == ["job-c"]


def test_history_keeps_texts_with_same_opening(ui) -> None:
    result = value(ui, "history_keeps_different_texts")
    a = "안녕하십니까. 한국에너지경제연구원 지식정보화실입니다. 첫 번째 문서는 회의 자료입니다."
    b = "안녕하십니까. 한국에너지경제연구원 지식정보화실입니다. 두 번째 자료를 보내드립니다."
    assert result["first"] == [b, a]
    assert result["second"] == ["회의 결과를 공유드립니다.", b, a]  # typing on merges into one record


def test_history_merges_edits_in_the_middle(ui) -> None:
    result = value(ui, "history_merges_mid_edits")
    assert result["items"] == ["The quick brown cat jumps over the lazy dog near the river bank."]
    assert result["unrelated"] is False and result["stale"] is False and result["otherLang"] is False


def test_unsaved_glossary_terms_match_across_whitespace(ui) -> None:
    assert value(ui, "glossary_draft_whitespace") == ["Fuel   Cell"]


def test_glossary_add_while_disabled_keeps_edits(ui) -> None:
    result = value(ui, "glossary_add_while_off")
    assert result["streamsAdded"] == 0
    assert result["edited"] is True
    assert "적용 꺼짐" in result["toast"]


def test_glossary_load_failure_keeps_saved_choice(ui) -> None:
    result = value(ui, "glossary_load_retry")
    saved = ui["__ids__"]["saved"]
    assert result["savedAfterFailure"] == saved
    assert result["selected"] == saved
    assert result["attempts"] >= 2


def test_glossary_import_accepts_xlsx(ui) -> None:
    result = value(ui, "glossary_import_xlsx")
    assert ".xlsx" in result["accept"].split(",")
    assert result["subtitle"] == "CSV · TSV · XLSX"


def test_translation_result_is_announced(ui) -> None:
    result = value(ui, "output_live_region")
    assert result["attrs"] == {"role": "status", "live": "polite"}
    assert result["text"] == "번역 완료"


def test_same_language_input_switches_target_like_deepl(ui) -> None:
    result = value(ui, "auto_switch_same_language")
    # Korean typed with target Korean -> target becomes English; English typed with target English
    # -> target switches back to the last other detected source language (Korean).
    assert result["afterKorean"] == "en"
    assert result["afterEnglish"] == "ko"
    assert result["stored"] == "ko"


@pytest.mark.parametrize("width", [viewport[0] for viewport in LANGBAR_VIEWPORTS])
def test_language_selects_never_overlap_swap_control(ui, width: int) -> None:
    rows = value(ui, f"langbar_{width}")
    assert {row["tab"] for row in rows} == {"text", "docs"}
    for row in rows:
        assert row["gap"] >= 4, row  # select boxes keep clear of the swap button / arrow (focus ring is 3 px)
        assert row["inside"], row
        assert row["painted"], row  # nothing paints over the edges of either select or the middle control
        assert abs(row["offCenter"]) <= 1, row  # middle control stays on the pane divider


def test_glossary_picker_shows_whole_name_on_phones(ui) -> None:
    result = value(ui, "glossary_picker_360")
    assert len(result["labels"]) >= 5, result
    assert result["room"] >= 0, result  # the longest name fits without being cut to "…"
    assert result["inside"], result
